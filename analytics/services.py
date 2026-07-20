import json
import logging
import pandas as pd
from django.apps import apps
from django.core.cache import cache
from typing import Dict, List, Any

from .models import AnalyticsQuery, AnalyticsDashboard, AnalyticsWidget, AnalyticsExport

logger = logging.getLogger(__name__)


class QueryBuilderService:
    """
    Service to build and execute analytics queries.
    Delegates to OpenSearch when available, falls back to Django ORM.
    """

    @classmethod
    def _use_opensearch(cls):
        return 'opensearch_reports' in apps.app_configs

    @classmethod
    def execute_query(cls, entity_type: str, query_config: Dict) -> List[Dict]:
        """Execute a query and return results."""
        cache_key = f"analytics_query_{entity_type}_{json.dumps(query_config, sort_keys=True)}"
        cached_result = cache.get(cache_key)
        if cached_result:
            return cached_result

        if cls._use_opensearch():
            from analytics.opensearch_service import OpenSearchQueryService
            results = OpenSearchQueryService.execute_query(entity_type, query_config)
        else:
            results = cls._execute_orm_query(entity_type, query_config)

        from analytics.apps import AnalyticsConfig
        cache.set(cache_key, results, AnalyticsConfig.analytics_cache_ttl)
        return results

    @classmethod
    def get_entity_fields(cls, entity_type: str) -> List[Dict]:
        """Get available fields for an entity type."""
        if cls._use_opensearch():
            from analytics.opensearch_service import OpenSearchQueryService
            return OpenSearchQueryService.get_entity_fields(entity_type)
        return cls._get_orm_entity_fields(entity_type)

    # ── ORM fallback ──────────────────────────────────────────────

    @classmethod
    def _get_orm_model(cls, entity_type):
        from individual.models import Individual, Group, GroupIndividual
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
    def _allowed_field_names(cls, model):
        """Concrete field names of the entity itself — the only names client
        configs may reference. Rejecting anything else (in particular `__`
        FK traversals) keeps a query scoped to the allowlisted entity instead
        of walking relations into Individual/Group PII."""
        allowed = set()
        for field in model._meta.get_fields():
            if getattr(field, 'concrete', False) and not field.many_to_many:
                allowed.add(field.name)
                attname = getattr(field, 'attname', None)
                if attname:
                    allowed.add(attname)
        return allowed

    @classmethod
    def _execute_orm_query(cls, entity_type: str, query_config: Dict) -> List[Dict]:
        import datetime
        import decimal
        import uuid as uuid_mod
        from django.db.models import Q, Count, Sum, Avg, Min, Max
        agg_funcs = {'count': Count, 'sum': Sum, 'avg': Avg, 'min': Min, 'max': Max}

        model = cls._get_orm_model(entity_type)
        if not model:
            raise ValueError(f"Unknown entity type: {entity_type}")

        allowed_fields = cls._allowed_field_names(model)

        def _require_allowed(name, context):
            if name not in allowed_fields:
                raise ValueError(
                    f"Field '{name}' is not allowed in {context} for entity '{entity_type}'"
                )

        queryset = model.objects.all()

        # Filters — accept either dict form ({field: {operator, value}}) or list form
        # ([{field, operator, value}]) for compatibility with FE builder state.
        filters_cfg = query_config.get('filters', {})
        filter_q = Q()
        if isinstance(filters_cfg, dict):
            iterable = filters_cfg.items()
        elif isinstance(filters_cfg, list):
            iterable = [
                (f.get('field'), {'operator': f.get('operator', 'exact'), 'value': f.get('value')})
                for f in filters_cfg
                if isinstance(f, dict) and f.get('field')
            ]
        else:
            iterable = []

        for field, condition in iterable:
            if not field:
                continue
            _require_allowed(field, 'filters')
            if isinstance(condition, dict):
                op = condition.get('operator', 'exact')
                val = condition.get('value')
                lookup = {
                    'contains': 'icontains', 'gt': 'gt', 'gte': 'gte',
                    'lt': 'lt', 'lte': 'lte', 'in': 'in', 'range': 'range',
                    'isnull': 'isnull',
                }.get(op, 'exact')
                filter_q &= Q(**{f"{field}__{lookup}" if lookup != 'exact' else field: val})
            else:
                filter_q &= Q(**{field: condition})
        queryset = queryset.filter(filter_q)

        # Group by + aggregations — accept either `group_by`/`aggregations` (UI builder)
        # or `dimensions`/`measures` (seeded queries).
        group_by = query_config.get('group_by') or query_config.get('dimensions') or []
        # Guard against the legacy shape where a single group-by field is stored as a bare
        # string (e.g. `"group_by": "status"`). Without this coercion the `*group_by` splat
        # below iterates character-by-character and crashes in Django's `values()`.
        if isinstance(group_by, str):
            group_by = [group_by] if group_by else []
        aggregations = query_config.get('aggregations') or {}

        # Normalise shorthand measures=["count"] into a proper aggregations map.
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

        # Accept aggregations as either dict ({name: {function, field}}) or list ([{name, function, field}]).
        if isinstance(aggregations, list):
            aggregations = {
                a.get('name') or f"{a.get('function', 'count')}_{a.get('field', 'id')}":
                {'function': a.get('function', 'count'), 'field': a.get('field', 'id')}
                for a in aggregations if isinstance(a, dict)
            }

        if group_by:
            for name in group_by:
                _require_allowed(name, 'group_by')
            queryset = queryset.values(*group_by)
            for agg_name, agg_config in aggregations.items():
                func = agg_funcs.get(agg_config['function'])
                if func:
                    agg_field = agg_config.get('field', 'id')
                    _require_allowed(agg_field, 'aggregations')
                    queryset = queryset.annotate(**{agg_name: func(agg_field)})

        # Order + limit
        order_by = query_config.get('order_by', [])
        if order_by:
            for name in order_by:
                bare = name[1:] if isinstance(name, str) and name.startswith('-') else name
                if bare not in aggregations:
                    _require_allowed(bare, 'order_by')
            queryset = queryset.order_by(*order_by)
        from analytics.apps import AnalyticsConfig
        try:
            limit = int(query_config.get('limit', 1000))
        except (TypeError, ValueError):
            limit = 1000
        limit = max(1, min(limit, AnalyticsConfig.analytics_max_query_rows))
        if not group_by and not aggregations:
            rows = list(queryset.values()[:limit])
        else:
            rows = list(queryset[:limit])

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
        return [{k: _coerce(v) for k, v in row.items()} for row in rows]

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

    @classmethod
    def export_to_excel(cls, data: List[Dict], filename: str) -> str:
        df = pd.DataFrame(data)
        filepath = f"/tmp/{filename}.xlsx"
        with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Data', index=False)
            worksheet = writer.sheets['Data']
            for col_idx, column in enumerate(df.columns):
                width = max(df[column].astype(str).map(len).max(), len(str(column)))
                worksheet.column_dimensions[chr(65 + col_idx)].width = min(width + 2, 50)
        return filepath

    @classmethod
    def export_to_csv(cls, data: List[Dict], filename: str) -> str:
        df = pd.DataFrame(data)
        filepath = f"/tmp/{filename}.csv"
        df.to_csv(filepath, index=False)
        return filepath

    @classmethod
    def export_to_pdf(cls, data: List[Dict], filename: str, title: str = "Analytics Report") -> str:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter, landscape
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet

        df = pd.DataFrame(data)
        filepath = f"/tmp/{filename}.pdf"
        doc = SimpleDocTemplate(filepath, pagesize=landscape(letter))
        elements = []
        styles = getSampleStyleSheet()
        elements.append(Paragraph(title, styles['Title']))
        elements.append(Spacer(1, 12))

        data_list = [df.columns.tolist()] + df.values.tolist()
        table = Table(data_list)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ]))
        elements.append(table)
        doc.build(elements)
        return filepath


class DashboardService:
    """Service to manage analytics dashboards."""

    @classmethod
    def create_default_dashboards(cls):
        from django.contrib.auth.models import User
        system_user = User.objects.filter(username='admin').first()
        if not system_user:
            return

        for name, desc, is_default in [
            ("System Overview", "Key system metrics", True),
            ("Beneficiary Analytics", "Beneficiary management analytics", False),
            ("Payment Analytics", "Payment tracking and analysis", False),
        ]:
            AnalyticsDashboard.objects.get_or_create(
                name=name,
                defaults={
                    'description': desc,
                    'is_public': True,
                    'is_default': is_default,
                    'created_by': system_user,
                    'layout_config': {"columns": 12, "rowHeight": 100},
                },
            )
