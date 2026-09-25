import hashlib
import json
import logging
import pandas as pd
from django.apps import apps
from django.core.cache import cache
from typing import Dict, List, Any, NamedTuple

from django.core.exceptions import PermissionDenied

from .models import AnalyticsQuery, AnalyticsDashboard, AnalyticsWidget, AnalyticsExport

logger = logging.getLogger(__name__)


class QueryResult(NamedTuple):
    rows: List[Dict]
    truncated: bool


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
            result = cls._execute_orm_query(entity_type, query_config, user, max_rows=max_rows)

        cache.set(cache_key, tuple(result), AnalyticsConfig.analytics_cache_ttl)
        return result

    @classmethod
    def get_entity_fields(cls, entity_type: str) -> List[Dict]:
        """Get available fields for an entity type."""
        if cls._use_opensearch():
            from analytics.opensearch_service import OpenSearchQueryService
            return OpenSearchQueryService.get_entity_fields(entity_type)
        return cls._get_orm_entity_fields(entity_type)

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

    @staticmethod
    def _row_security_applies(user):
        from django.conf import settings
        return bool(getattr(settings, 'ROW_SECURITY', False)) and not user.is_imis_admin

    @classmethod
    def _scoped_queryset(cls, entity_type, model, user, referenced_fields):
        """Rows of `model` the user may read: never soft-deleted rows, then the
        same row security as the owning module's own list queries."""
        queryset = model.objects.filter(is_deleted=False)
        if entity_type == 'grievance':
            return cls._scope_grievance(queryset, user, referenced_fields)
        if not cls._row_security_applies(user):
            return queryset
        if entity_type == 'payment':
            from individual.models import Individual
            return queryset.filter(individual__in=Individual.get_queryset(None, user))
        return model.get_queryset(queryset, user)

    @classmethod
    def _scope_grievance(cls, queryset, user, referenced_fields):
        """Apply the grievance module's read rules to a Ticket queryset.

        The caller needs the base ticket read right. Tickets of categories the user
        cannot see are dropped. A ticket the user may only see in restricted form
        (restricted category or flag) is kept only when every referenced field is
        among the category's visible fields, which mirrors TicketGQLType's field
        masking.
        """
        from django.db.models import Q
        from grievance_social_protection.apps import TicketConfig
        if not user.has_perms(TicketConfig.gql_query_tickets_perms):
            raise PermissionDenied("Reading grievance tickets requires the grievance read right")
        try:
            from grievance_social_protection.access_control import GrievanceAccessControl as access
        except ImportError:
            return queryset

        queryset = access.filter_ticket_queryset(queryset, user)
        referenced = cls._canonical_ticket_fields(referenced_fields)

        covered_categories = []
        for category in (TicketConfig.processed_categories or {}):
            if not access.has_category_restrictions(category):
                continue
            visible = access.get_visible_fields(user, category)
            if visible is None:
                continue
            if referenced <= cls._canonical_ticket_fields(visible):
                covered_categories.append(category)
            else:
                queryset = queryset.exclude(category=category)

        flag_q = getattr(access, '_flag_stored_q', None) or (lambda flag: Q(flags__icontains=flag))
        for flag, info in (TicketConfig.processed_flags or {}).items():
            if not info.get('generated_rights'):
                continue
            level = access.get_user_access_level(user, None, [flag])
            if level in (access.ACCESS_FULL, access.ACCESS_READ):
                continue
            queryset = queryset.exclude(flag_q(flag) & ~Q(category__in=covered_categories))
        return queryset

    @classmethod
    def _canonical_ticket_fields(cls, names):
        """Map Ticket field names/attnames (and the `reporter` generic relation) to
        model field names, so config references compare with visible_fields."""
        from django.core.exceptions import FieldDoesNotExist
        from grievance_social_protection.models import Ticket
        canonical = set()
        for name in names:
            if name == 'reporter':
                canonical.update({'reporter_type', 'reporter_id'})
                continue
            try:
                canonical.add(Ticket._meta.get_field(name).name)
            except FieldDoesNotExist:
                canonical.add(name)
        return canonical

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

        allowed_fields = cls._allowed_field_names(model, entity_type)

        def _require_allowed(name, context):
            if name not in allowed_fields:
                raise ValueError(
                    f"Field '{name}' is not allowed in {context} for entity '{entity_type}'"
                )

        filters, group_by, aggregations, order_by, fields = cls._normalise_config(query_config)

        for field, _ in filters:
            _require_allowed(field, 'filters')
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
        referenced = {f for f, _ in filters} | set(group_by)
        referenced |= {name[1:] if name.startswith('-') else name for name in order_by} - set(aggregations)
        for agg_config in aggregations.values():
            agg_field = agg_config.get('field') or 'id'
            # Counting rows by primary key reads no field value.
            if not (agg_config['function'] == 'count' and agg_field in ('id', 'pk')):
                referenced.add(agg_field)
        if not grouped:
            referenced |= set(fields) if fields else cls._concrete_field_names(model)

        queryset = cls._scoped_queryset(entity_type, model, user, referenced)
        for field, condition in filters:
            queryset = queryset.filter(cls._filter_q(field, condition))

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
        return QueryResult([{k: _coerce(v) for k, v in row.items()} for row in rows], truncated)

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
        filepath = f"/tmp/{filename}.xlsx"
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
        filepath = f"/tmp/{filename}.csv"
        df.to_csv(filepath, index=False)
        return filepath


class DashboardService:
    """Service to manage analytics dashboards."""

    @classmethod
    def create_default_dashboards(cls):
        """Create the built-in dashboards, owned by the Admin account.

        They are not flagged `is_default`: the default dashboard is the one the
        seed_analytics_dashboards command creates.
        """
        from django.contrib.auth import get_user_model
        system_user = get_user_model().objects.filter(username__iexact='admin').first()
        if not system_user:
            logger.warning("No 'admin' user: built-in analytics dashboards not created")
            return

        for name, desc in [
            ("System Overview", "Key system metrics"),
            ("Beneficiary Analytics", "Beneficiary management analytics"),
            ("Payment Analytics", "Payment tracking and analysis"),
        ]:
            AnalyticsDashboard.objects.get_or_create(
                name=name,
                defaults={
                    'description': desc,
                    'is_public': True,
                    'is_default': False,
                    'created_by': system_user,
                    'layout_config': {"columns": 12, "rowHeight": 100},
                },
            )
