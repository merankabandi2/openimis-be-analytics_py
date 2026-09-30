import hashlib
import json
import os
from contextlib import contextmanager
import pandas as pd
from django.apps import apps
from django.core.cache import cache
from django.db import OperationalError, connection, transaction
from typing import Dict, List, Any, NamedTuple

from django.core.exceptions import PermissionDenied


class QueryResult(NamedTuple):
    rows: List[Dict]
    truncated: bool
    # True when grievance tickets are left out because the query reads fields
    # the user may not see on them, and some of those tickets match the filters
    # on fields the user does see there. Filters on hidden fields count as
    # matching, so the flag can report tickets the full filter would exclude.
    restricted_rows_withheld: bool = False


class QueryTimeout(ValueError):
    """An analytics query ran past `analytics_query_timeout` and was cancelled."""


# Filter operators accepted in a query config, mapped to the Django lookup they
# apply and whether the condition is negated.
FILTER_LOOKUPS = {
    'exact': ('exact', False),
    'ne': ('exact', True),
    'contains': ('icontains', False),
    'startswith': ('istartswith', False),
    'endswith': ('iendswith', False),
    'gt': ('gt', False),
    'gte': ('gte', False),
    'lt': ('lt', False),
    'lte': ('lte', False),
    'in': ('in', False),
    'not_in': ('in', True),
    'range': ('range', False),
    'isnull': ('isnull', False),
    'is_not_null': ('isnull', True),
}

# Operators that compare a column as text (UPPER(column::text) LIKE ...). On a
# JSON field this reads and casts every document of the table, and no index
# serves it.
TEXT_OPERATORS = {'contains', 'startswith', 'endswith'}

# SQLSTATE of a statement cancelled by statement_timeout.
QUERY_CANCELED = '57014'


def _sqlstate(exc):
    cause = exc.__cause__
    return getattr(cause, 'pgcode', None) or getattr(cause, 'sqlstate', None)


@contextmanager
def _statement_timeout(seconds):
    """Run the block in a transaction where PostgreSQL cancels any statement
    running longer than `seconds`, then put the previous statement_timeout back.
    A cancelled statement raises QueryTimeout. Other database vendors, and a
    timeout of 0 or less, run the block unchanged."""
    if connection.vendor != 'postgresql' or not seconds or seconds <= 0:
        yield
        return
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('statement_timeout')")
            previous = cursor.fetchone()[0]
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", [f'{int(seconds * 1000)}ms'])
        try:
            yield
        except OperationalError as exc:
            if _sqlstate(exc) == QUERY_CANCELED:
                raise QueryTimeout(
                    f"The query ran longer than {seconds:g} seconds and was stopped; narrow the filters"
                ) from exc
            raise
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous])


# Relation paths that configs may reference in addition to the entity's own
# concrete fields. Each path ends on a non-personal attribute (programme or
# location names), so it cannot walk into Individual/Group PII.
RELATION_PATHS = {
    'group_beneficiary': {
        'benefit_plan__code',
        'benefit_plan__name',
        'group__location__name',
        'group__location__parent__name',
        'group__location__parent__parent__name',
    },
    'group': {
        'location__name',
        'location__parent__name',
        'location__parent__parent__name',
    },
    'individual': {
        'location__name',
        'location__parent__name',
        'location__parent__parent__name',
    },
}


class QueryBuilderService:
    """
    Service to build and execute analytics queries.

    Queries run through the Django ORM, scoped to the requesting user: soft-deleted
    rows are excluded, location row security applies to beneficiary and payment
    entities, and grievance tickets follow the grievance module's category and
    flag access rules. OpenSearch cannot apply that scoping, so it only serves
    superusers.
    """

    ENTITY_TYPES = ('individual', 'group', 'beneficiary', 'group_beneficiary', 'payment', 'grievance')

    @classmethod
    def _use_opensearch(cls):
        return 'opensearch_reports' in apps.app_configs

    @classmethod
    def execute_query(cls, entity_type: str, query_config: Dict, user, max_rows: int = None) -> QueryResult:
        """Execute a query for `user` and return its rows.

        `max_rows` overrides the config's `limit` (exports); otherwise the limit is
        capped by `analytics_max_query_rows`. `truncated` is True when more rows
        matched than were returned.
        """
        from analytics.apps import AnalyticsConfig
        if entity_type not in cls.ENTITY_TYPES:
            raise ValueError(f"Unknown entity type: {entity_type}")
        config_digest = hashlib.sha256(
            json.dumps(query_config, sort_keys=True, default=str).encode()
        ).hexdigest()
        cache_key = f"analytics_query_{getattr(user, 'id', None)}_{entity_type}_{max_rows}_{config_digest}"
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            return QueryResult(*cached_result)

        if cls._use_opensearch() and user.is_superuser:
            from analytics.opensearch_service import OpenSearchQueryService
            rows = OpenSearchQueryService.execute_query(entity_type, query_config)
            result = QueryResult(rows, False)
        else:
            with _statement_timeout(AnalyticsConfig.analytics_query_timeout):
                result = cls._execute_orm_query(entity_type, query_config, user, max_rows=max_rows)

        cache.set(cache_key, tuple(result), AnalyticsConfig.analytics_cache_ttl)
        return result

    @classmethod
    def get_entity_fields(cls, entity_type: str, user) -> List[Dict]:
        """Fields of an entity type that `user` may reference. For anyone but a
        superuser, only the fields of the entity's allowlist. For grievances,
        none without the ticket read right, and a field hidden to the user on
        every category they can read is left out."""
        if cls._use_opensearch():
            from analytics.opensearch_service import OpenSearchQueryService
            fields = OpenSearchQueryService.get_entity_fields(entity_type)
        else:
            fields = cls._get_orm_entity_fields(entity_type)
        model = cls._get_orm_model(entity_type)
        if model is None:
            return []
        if not user.is_superuser:
            allowlisted = cls._allowlisted_field_names(model, entity_type)
            fields = [f for f in fields if f['name'] in allowlisted]
        if entity_type == 'grievance':
            from analytics import grievance_access
            try:
                grievance_access.check_read_right(user)
            except PermissionDenied:
                return []
            selectable = grievance_access.selectable_fields(user)
            fields = [f for f in fields if grievance_access.canonical_fields([f['name']]) <= selectable]
        return fields

    # ── ORM ───────────────────────────────────────────────────────

    @classmethod
    def _get_orm_model(cls, entity_type):
        from individual.models import Individual, Group
        from social_protection.models import Beneficiary, GroupBeneficiary
        from grievance_social_protection.models import Ticket
        from payroll.models import BenefitConsumption
        models = {
            'individual': Individual,
            'group': Group,
            'beneficiary': Beneficiary,
            'group_beneficiary': GroupBeneficiary,
            'grievance': Ticket,
            # The Merankabandi model maps "payment" to BenefitConsumption rows
            # (each bi-monthly disbursement to a beneficiary).
            'payment': BenefitConsumption,
        }
        return models.get(entity_type)

    @classmethod
    def _concrete_field_names(cls, model):
        """Concrete field names and attnames of the entity itself."""
        allowed = set()
        for field in model._meta.get_fields():
            if getattr(field, 'concrete', False) and not field.many_to_many:
                allowed.add(field.name)
                attname = getattr(field, 'attname', None)
                if attname:
                    allowed.add(attname)
        return allowed

    @classmethod
    def _allowed_field_names(cls, model, entity_type=None):
        """Names client configs may reference: the entity's concrete fields plus the
        relation paths listed in RELATION_PATHS. Any other `__` traversal is
        rejected so a query cannot walk relations into Individual/Group PII."""
        return cls._concrete_field_names(model) | RELATION_PATHS.get(entity_type, set())

    @classmethod
    def _allowlisted_field_names(cls, model, entity_type):
        """Names a non-superuser may reference: the entity's entry in
        `analytics_field_allowlist`, or its default entry when the setting has
        none, limited to _allowed_field_names. A foreign key listed by name or
        by column admits both. An entry that is not a list admits nothing."""
        from analytics.apps import AnalyticsConfig, DEFAULT_FIELD_ALLOWLIST
        configured = AnalyticsConfig.analytics_field_allowlist
        if not isinstance(configured, dict):
            configured = {}
        listed = configured.get(entity_type, DEFAULT_FIELD_ALLOWLIST.get(entity_type, []))
        if not isinstance(listed, (list, tuple)):
            return set()
        listed = {name for name in listed if isinstance(name, str)}
        for field in model._meta.get_fields():
            if getattr(field, 'concrete', False) and not field.many_to_many:
                attname = getattr(field, 'attname', None) or field.name
                if field.name in listed or attname in listed:
                    listed |= {field.name, attname}
        return listed & cls._allowed_field_names(model, entity_type)

    @classmethod
    def _referenceable_field_names(cls, model, entity_type, user):
        """Names `user` may reference in a config: every allowed name for a
        superuser, the allowlisted ones for anyone else."""
        if user.is_superuser:
            return cls._allowed_field_names(model, entity_type)
        return cls._allowlisted_field_names(model, entity_type)

    @staticmethod
    def _default_columns(model, referenceable):
        """Columns (attnames) of the entity's concrete fields in `referenceable`,
        in model order: what a row holds when the config selects no field."""
        columns = []
        for field in model._meta.get_fields():
            if getattr(field, 'concrete', False) and not field.many_to_many:
                attname = getattr(field, 'attname', None) or field.name
                if field.name in referenceable or attname in referenceable:
                    columns.append(attname)
        return columns

    @staticmethod
    def _row_security_applies(user):
        from django.conf import settings
        return bool(getattr(settings, 'ROW_SECURITY', False)) and not user.is_imis_admin

    @classmethod
    def _scoped_queryset(cls, entity_type, model, user, referenced_fields):
        """Rows of `model` the user may read: never soft-deleted rows, then the
        same row security as the owning module's own list queries.

        Returns (queryset, withheld). `withheld` is None, or for grievances a
        (readable, parts) pair describing the tickets the user can see but that
        are left out because the query references fields hidden on them (see
        _scope_grievance).
        """
        queryset = model.objects.filter(is_deleted=False)
        if entity_type == 'grievance':
            return cls._scope_grievance(queryset, user, referenced_fields)
        if not cls._row_security_applies(user):
            return queryset, None
        if entity_type == 'payment':
            return cls._scope_payments(queryset, user), None
        return model.get_queryset(queryset, user), None

    @classmethod
    def _scope_payments(cls, queryset, user):
        """Payments of the individuals Individual.get_queryset lets the user read.

        That rule admits an individual whose location, or the location of one of
        its groups, is allowed or empty. Joining every individual to its groups
        costs a scan of both tables, so when fewer locations are denied than
        allowed the same rule is applied as its complement: only payments of
        individuals located in a denied location and in no admitted group are
        dropped, and none when no location is denied.
        """
        from django.db.models import Exists, OuterRef
        from individual.models import Individual, GroupIndividual
        from location.models import Location, LocationManager
        core_user = getattr(user, '_u', user)
        manager = LocationManager()
        allowed_locations = manager.build_user_location_filter_query(core_user, prefix='id')
        if not allowed_locations:
            return queryset.filter(individual__in=Individual.get_queryset(None, user))
        denied = list(Location.objects.exclude(allowed_locations).values_list('id', flat=True))
        if not denied:
            return queryset
        if len(denied) * 2 > Location.objects.count():
            return queryset.filter(individual__in=Individual.get_queryset(None, user))
        admitted_group = GroupIndividual.objects.filter(
            individual=OuterRef('pk'), group__isnull=False,
        ).filter(manager.build_user_location_filter_query(core_user, prefix='group__location'))
        hidden = Individual.objects.filter(location_id__in=denied).exclude(Exists(admitted_group))
        return queryset.exclude(Exists(hidden.filter(pk=OuterRef('individual_id'))))

    @classmethod
    def _scope_grievance(cls, queryset, user, referenced_fields):
        """Apply the grievance module's read rules to a Ticket queryset.

        The caller needs the base ticket read right. Tickets the module does not
        list for the user are dropped, and so is any ticket on which one of the
        referenced fields is hidden to the user (see analytics.grievance_access).

        Returns (queryset, withheld). `withheld` is None when no readable ticket
        is dropped for a hidden field, else (readable, parts): `readable` is the
        queryset of tickets the user can see, and each part pairs a Q selecting
        dropped tickets with the canonical names of the referenced fields the
        user sees on them.
        """
        from analytics import grievance_access
        return grievance_access.scope(queryset, user, referenced_fields)

    @classmethod
    def _withheld_tickets_match(cls, withheld, filters):
        """True when some withheld ticket matches the filters the user can
        evaluate on it.

        A filter applies to a part only when its field is visible there, so the
        answer never depends on a field value hidden from the user; a filter on
        a hidden field is treated as matching.
        """
        if withheld is None:
            return False
        readable, parts = withheld
        match = None
        for condition, visible in parts:
            for field, filter_condition in filters:
                if cls._canonical_ticket_fields([field]) <= visible:
                    condition &= cls._filter_q(field, filter_condition)
            match = condition if match is None else match | condition
        return readable.filter(match).exists()

    @classmethod
    def _canonical_ticket_fields(cls, names):
        from analytics.grievance_access import canonical_fields
        return canonical_fields(names)

    @staticmethod
    def _normalise_config(query_config):
        """Return (filters, group_by, aggregations, order_by, fields) in canonical shape.

        Accepts `group_by`/`aggregations` (builder) or `dimensions`/`measures`
        (seeded queries), and filters/aggregations in dict or list form.
        """
        filters_cfg = query_config.get('filters', {})
        if isinstance(filters_cfg, dict):
            filters = list(filters_cfg.items())
        elif isinstance(filters_cfg, list):
            filters = [
                (f.get('field'), {'operator': f.get('operator', 'exact'), 'value': f.get('value')})
                for f in filters_cfg
                if isinstance(f, dict) and f.get('field')
            ]
        else:
            filters = []

        group_by = query_config.get('group_by') or query_config.get('dimensions') or []
        # A single group-by field may be stored as a bare string (e.g. `"group_by": "status"`).
        if isinstance(group_by, str):
            group_by = [group_by] if group_by else []

        aggregations = query_config.get('aggregations') or {}
        measures = query_config.get('measures')
        if measures and not aggregations:
            measure_map = {}
            for m in measures:
                if isinstance(m, str):
                    measure_map[f'{m}_value'] = {'function': m, 'field': 'id'}
                elif isinstance(m, dict) and m.get('function'):
                    name = m.get('name') or f"{m['function']}_{m.get('field', 'id')}"
                    measure_map[name] = {'function': m['function'], 'field': m.get('field', 'id')}
            aggregations = measure_map
        if isinstance(aggregations, list):
            aggregations = {
                a.get('name') or f"{a.get('function', 'count')}_{a.get('field', 'id')}":
                {'function': a.get('function', 'count'), 'field': a.get('field', 'id')}
                for a in aggregations if isinstance(a, dict)
            }

        order_by = query_config.get('order_by') or []
        if isinstance(order_by, str):
            order_by = [order_by]
        fields = query_config.get('fields') or []
        if isinstance(fields, str):
            fields = [fields]
        return filters, list(group_by), aggregations, list(order_by), list(fields)

    @staticmethod
    def _resolve_field(model, path):
        """The model field that `path` (possibly a relation path) ends on, or None."""
        from django.core.exceptions import FieldDoesNotExist
        field = None
        for part in path.split('__'):
            try:
                field = model._meta.get_field(part)
            except FieldDoesNotExist:
                return None
            if field.is_relation and field.related_model is not None:
                model = field.related_model
        return field

    @classmethod
    def _is_date_only_field(cls, model, path):
        """True when `path` ends on a date column that holds no time of day."""
        from django.db import models as dj_models
        field = cls._resolve_field(model, path)
        return isinstance(field, dj_models.DateField) and not isinstance(field, dj_models.DateTimeField)

    @classmethod
    def _is_json_field(cls, model, path):
        from django.db import models as dj_models
        return isinstance(cls._resolve_field(model, path), dj_models.JSONField)

    @staticmethod
    def _check_date_values(field, condition):
        """A date column compared with a timestamp never matches: openIMIS's
        DateField turns the value into a datetime. Only YYYY-MM-DD is accepted."""
        import datetime
        import re
        if not isinstance(condition, dict):
            condition = {'value': condition}
        if condition.get('operator') in ('isnull', 'is_not_null'):
            return
        value = condition.get('value')
        values = value if isinstance(value, (list, tuple)) else (
            value.split(',') if isinstance(value, str) and condition.get('operator') in ('in', 'not_in', 'range')
            else [value]
        )
        for item in values:
            text = str(item).strip()
            try:
                valid = bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', text)) and datetime.date.fromisoformat(text)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(f"Filter on date field '{field}' needs a YYYY-MM-DD value, got '{item}'")

    @staticmethod
    def _filter_q(field, condition):
        from django.db.models import Q
        if not isinstance(condition, dict):
            return Q(**{field: condition})
        op = condition.get('operator', 'exact')
        if op not in FILTER_LOOKUPS:
            raise ValueError(f"Unsupported filter operator '{op}'")
        lookup, negated = FILTER_LOOKUPS[op]
        value = condition.get('value')
        if op == 'is_not_null':
            value = True
        elif lookup == 'isnull':
            value = bool(value)
        elif lookup == 'in' and isinstance(value, str):
            value = [v.strip() for v in value.split(',') if v.strip()]
        q = Q(**{field if lookup == 'exact' else f"{field}__{lookup}": value})
        return ~q if negated else q

    @classmethod
    def _execute_orm_query(cls, entity_type: str, query_config: Dict, user, max_rows: int = None) -> QueryResult:
        import datetime
        import decimal
        import uuid as uuid_mod
        from django.db.models import Count, Sum, Avg, Min, Max
        from analytics.apps import AnalyticsConfig
        agg_funcs = {'count': Count, 'sum': Sum, 'avg': Avg, 'min': Min, 'max': Max}

        model = cls._get_orm_model(entity_type)
        if not model:
            raise ValueError(f"Unknown entity type: {entity_type}")

        allowed_fields = cls._referenceable_field_names(model, entity_type, user)

        def _require_allowed(name, context):
            if name not in allowed_fields:
                raise ValueError(
                    f"Field '{name}' is not allowed in {context} for entity '{entity_type}'"
                )

        filters, group_by, aggregations, order_by, fields = cls._normalise_config(query_config)

        for field, condition in filters:
            _require_allowed(field, 'filters')
            if cls._is_date_only_field(model, field):
                cls._check_date_values(field, condition)
            operator = condition.get('operator', 'exact') if isinstance(condition, dict) else 'exact'
            if operator in TEXT_OPERATORS and cls._is_json_field(model, field):
                raise ValueError(
                    f"Text filter '{operator}' is not allowed on JSON field '{field}' for entity '{entity_type}'"
                )
        for name in group_by:
            _require_allowed(name, 'group_by')
        for agg_name, agg_config in aggregations.items():
            if agg_config.get('function') not in agg_funcs:
                raise ValueError(f"Unsupported aggregation function '{agg_config.get('function')}'")
            _require_allowed(agg_config.get('field') or 'id', 'aggregations')
        for name in order_by:
            bare = name[1:] if name.startswith('-') else name
            if bare in aggregations:
                continue
            _require_allowed(bare, 'order_by')
            if group_by and bare not in group_by:
                raise ValueError(f"Field '{bare}' in order_by must be one of the group_by fields")
        for name in fields:
            _require_allowed(name, 'fields')

        grouped = bool(group_by or aggregations)
        if not grouped and not fields and not user.is_superuser:
            fields = cls._default_columns(model, allowed_fields)
            if not fields:
                raise ValueError(f"No field of entity '{entity_type}' is allowed")
        referenced = {f for f, _ in filters} | set(group_by)
        referenced |= {name[1:] if name.startswith('-') else name for name in order_by} - set(aggregations)
        for agg_config in aggregations.values():
            agg_field = agg_config.get('field') or 'id'
            # Counting rows by primary key reads no field value.
            if not (agg_config['function'] == 'count' and agg_field in ('id', 'pk')):
                referenced.add(agg_field)
        if not grouped:
            referenced |= set(fields) if fields else cls._concrete_field_names(model)

        queryset, withheld = cls._scoped_queryset(entity_type, model, user, referenced)
        for field, condition in filters:
            queryset = queryset.filter(cls._filter_q(field, condition))
        restricted_rows_withheld = cls._withheld_tickets_match(withheld, filters)

        if max_rows is not None:
            limit = max_rows
        else:
            try:
                limit = int(query_config.get('limit', 1000))
            except (TypeError, ValueError):
                limit = 1000
            limit = max(1, min(limit, AnalyticsConfig.analytics_max_query_rows))

        def _aggregate_expr(agg_config):
            return agg_funcs[agg_config['function']](agg_config.get('field') or 'id')

        if aggregations and not group_by:
            rows = [queryset.aggregate(**{
                name: _aggregate_expr(agg_config) for name, agg_config in aggregations.items()
            })]
        else:
            if group_by:
                # Clearing the model's default ordering keeps it out of GROUP BY / DISTINCT.
                queryset = queryset.order_by().values(*group_by)
                if aggregations:
                    queryset = queryset.annotate(**{
                        name: _aggregate_expr(agg_config) for name, agg_config in aggregations.items()
                    })
                else:
                    queryset = queryset.distinct()
            else:
                queryset = queryset.values(*fields)
            if order_by:
                queryset = queryset.order_by(*order_by)
            elif group_by:
                # Without an ORDER BY the database returns groups in any order, and
                # the row limit keeps an arbitrary subset.
                queryset = queryset.order_by(*group_by)
            rows = list(queryset[:limit + 1])
        truncated = len(rows) > limit
        rows = rows[:limit]

        # Django's .values() returns UUIDs / dates / Decimals as Python objects;
        # Graphene's JSONString cannot serialise these, so coerce to primitives.
        def _coerce(v):
            if isinstance(v, uuid_mod.UUID):
                return str(v)
            if isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
                return v.isoformat()
            if isinstance(v, decimal.Decimal):
                return float(v)
            return v
        return QueryResult(
            [{k: _coerce(v) for k, v in row.items()} for row in rows], truncated, restricted_rows_withheld,
        )

    @classmethod
    def _get_orm_entity_fields(cls, entity_type: str) -> List[Dict]:
        model = cls._get_orm_model(entity_type)
        if not model:
            return []
        fields = []
        for field in model._meta.get_fields():
            if field.concrete and not field.many_to_many:
                fields.append({
                    'name': field.name,
                    'type': field.get_internal_type(),
                    'label': field.verbose_name,
                    'filterable': True,
                    'aggregatable': field.get_internal_type() in (
                        'IntegerField', 'DecimalField', 'FloatField',
                    ),
                })
        return fields


class ExportService:
    """Service to handle data exports in multiple formats."""

    EXPORTERS = {
        'excel': 'export_to_excel',
        'csv': 'export_to_csv',
    }
    # Under MEDIA_ROOT; export files are handed out only by the download view.
    EXPORT_SUBDIR = 'analytics_exports'

    @classmethod
    def export_dir(cls) -> str:
        from django.conf import settings
        directory = os.path.join(settings.MEDIA_ROOT, cls.EXPORT_SUBDIR)
        os.makedirs(directory, exist_ok=True)
        return directory

    @classmethod
    def export(cls, data: List[Dict], filename: str, export_format: str) -> str:
        """Dispatch to the exporter for `export_format`, or raise if unsupported."""
        exporter_name = cls.EXPORTERS.get(export_format)
        if not exporter_name:
            raise ValueError(f"Unsupported export format: {export_format}")
        return getattr(cls, exporter_name)(data, filename)

    @classmethod
    def export_to_excel(cls, data: List[Dict], filename: str) -> str:
        from openpyxl.utils import get_column_letter
        df = pd.DataFrame(data)
        filepath = os.path.join(cls.export_dir(), f"{filename}.xlsx")
        with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Data', index=False)
            worksheet = writer.sheets['Data']
            for col_idx, column in enumerate(df.columns):
                width = max(df[column].astype(str).map(len).max(), len(str(column)))
                worksheet.column_dimensions[get_column_letter(col_idx + 1)].width = min(width + 2, 50)
        return filepath

    @classmethod
    def export_to_csv(cls, data: List[Dict], filename: str) -> str:
        df = pd.DataFrame(data)
        filepath = os.path.join(cls.export_dir(), f"{filename}.csv")
        df.to_csv(filepath, index=False)
        return filepath
