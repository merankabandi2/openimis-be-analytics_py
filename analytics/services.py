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
        models = {
            'individual': Individual,
            'group': Group,
            'beneficiary': Beneficiary,
            'group_beneficiary': GroupBeneficiary,
            'grievance': Ticket,
        }
        return models.get(entity_type)

    @classmethod
    def _execute_orm_query(cls, entity_type: str, query_config: Dict) -> List[Dict]:
        from django.db.models import Q, Count, Sum, Avg, Min, Max
        agg_funcs = {'count': Count, 'sum': Sum, 'avg': Avg, 'min': Min, 'max': Max}

        model = cls._get_orm_model(entity_type)
        if not model:
            raise ValueError(f"Unknown entity type: {entity_type}")

        queryset = model.objects.all()

        # Filters
        filter_q = Q()
        for field, condition in query_config.get('filters', {}).items():
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

        # Group by + aggregations
        group_by = query_config.get('group_by', [])
        aggregations = query_config.get('aggregations', {})
        if group_by:
            queryset = queryset.values(*group_by)
            for agg_name, agg_config in aggregations.items():
                func = agg_funcs.get(agg_config['function'])
                if func:
                    queryset = queryset.annotate(**{agg_name: func(agg_config.get('field', 'id'))})

        # Order + limit
        order_by = query_config.get('order_by', [])
        if order_by:
            queryset = queryset.order_by(*order_by)
        limit = query_config.get('limit', 1000)
        return list(queryset[:limit])

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
