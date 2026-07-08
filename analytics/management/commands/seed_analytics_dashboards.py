"""
Seed default analytics dashboards with widgets and saved queries.
Idempotent — safe to rerun on fresh or existing databases.

Usage:
    python manage.py seed_analytics_dashboards
"""
import uuid

from django.core.management.base import BaseCommand
from django.db import connection

from analytics.models import AnalyticsDashboard, AnalyticsWidget, AnalyticsQuery
from core.models import User


DEFAULT_QUERIES = [
    {
        'name': 'Bénéficiaires par programme',
        'entity_type': 'beneficiary',
        'query_config': {'measures': ['count'], 'dimensions': ['benefit_plan__name'], 'filters': []},
    },
    {
        'name': 'Paiements par statut',
        'entity_type': 'payment',
        'query_config': {'measures': ['count'], 'dimensions': ['status'], 'filters': []},
    },
    {
        'name': 'Plaintes par catégorie',
        'entity_type': 'grievance',
        'query_config': {'measures': ['count'], 'dimensions': ['category'], 'filters': []},
    },
    {
        'name': 'Bénéficiaires par province',
        'entity_type': 'beneficiary',
        'query_config': {'measures': ['count'], 'dimensions': ['location__parent__name'], 'filters': []},
    },
    {
        'name': 'Activités par statut',
        'entity_type': 'beneficiary',
        'query_config': {'measures': ['count'], 'dimensions': ['status'], 'filters': []},
    },
]

DEFAULT_WIDGETS = [
    {
        'query_name': 'Bénéficiaires par programme',
        'widget_type': 'bar_chart',
        'title': 'Bénéficiaires par programme',
        'position': {'x': 0, 'y': 0, 'w': 6, 'h': 4},
    },
    {
        'query_name': 'Paiements par statut',
        'widget_type': 'pie_chart',
        'title': 'Paiements par statut',
        'position': {'x': 6, 'y': 0, 'w': 6, 'h': 4},
    },
    {
        'query_name': 'Plaintes par catégorie',
        'widget_type': 'bar_chart',
        'title': 'Plaintes par catégorie',
        'position': {'x': 0, 'y': 4, 'w': 12, 'h': 4},
    },
]


class Command(BaseCommand):
    help = 'Seed default analytics dashboards, saved queries, and widgets (idempotent)'

    def handle(self, *args, **options):
        admin = User.objects.filter(username='Admin').first()
        if not admin:
            admin = User.objects.first()
        if not admin:
            self.stderr.write('No user found to own analytics objects')
            return

        created_queries = 0
        created_widgets = 0

        # Seed queries — also update existing rows so seed-driven config changes
        # (e.g. UI compat fixes) propagate without manual DB cleanup.
        query_map = {}
        for q_def in DEFAULT_QUERIES:
            q, was_created = AnalyticsQuery.objects.get_or_create(
                name=q_def['name'],
                defaults={
                    'entity_type': q_def['entity_type'],
                    'query_config': q_def['query_config'],
                    'is_public': True,
                    'created_by': admin,
                },
            )
            query_map[q_def['name']] = q
            if was_created:
                created_queries += 1
            else:
                # Refresh the canonical config/entity_type on reseed in case the
                # schema evolved (dimensions→group_by, measures→aggregations, …).
                changed = False
                if q.entity_type != q_def['entity_type']:
                    q.entity_type = q_def['entity_type']
                    changed = True
                if q.query_config != q_def['query_config']:
                    q.query_config = q_def['query_config']
                    changed = True
                if not q.is_public:
                    q.is_public = True
                    changed = True
                if changed:
                    q.save(update_fields=['entity_type', 'query_config', 'is_public'])

        # Seed dashboard
        dash, dash_created = AnalyticsDashboard.objects.get_or_create(
            name='Tableau de Bord Principal',
            defaults={
                'description': 'Vue d\'ensemble des indicateurs clés du programme Merankabandi',
                'is_public': True,
                'is_default': True,
                'created_by': admin,
                'layout_config': {'columns': 12, 'rowHeight': 100},
            },
        )
        if dash_created:
            self.stdout.write(f'  Created dashboard: {dash.name}')

        # Seed widgets (only if dashboard has none)
        if AnalyticsWidget.objects.filter(dashboard=dash).count() == 0:
            for w_def in DEFAULT_WIDGETS:
                query = query_map.get(w_def['query_name'])
                if not query:
                    continue
                # Use raw SQL to bypass VersionedModel validation
                wid = uuid.uuid4()
                with connection.cursor() as c:
                    c.execute("""
                        INSERT INTO analytics_widget
                        (id, dashboard_id, query_id, widget_type, title, config, position, "ValidityFrom")
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, NOW())
                        ON CONFLICT DO NOTHING
                    """, [
                        str(wid), str(dash.id), str(query.id),
                        w_def['widget_type'], w_def['title'],
                        '{"display": "default"}',
                        str(w_def['position']).replace("'", '"'),
                    ])
                created_widgets += 1
        else:
            self.stdout.write(f'  Dashboard already has {AnalyticsWidget.objects.filter(dashboard=dash).count()} widgets')

        self.stdout.write(self.style.SUCCESS(
            f'Seed complete: {created_queries} new queries, '
            f'{created_widgets} new widgets, '
            f'{1 if dash_created else 0} new dashboards'
        ))
