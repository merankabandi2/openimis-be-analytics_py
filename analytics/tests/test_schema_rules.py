import base64
import datetime
import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest import mock

import graphene
from django.apps import apps
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from graphql_jwt.shortcuts import get_token

from core.test_helpers import create_test_interactive_user
from individual.models import Individual

from analytics.apps import AnalyticsConfig
from analytics.models import AnalyticsDashboard, AnalyticsExport, AnalyticsQuery, AnalyticsWidget
from analytics.schema import (
    AddAnalyticsWidgetMutation,
    AnalyticsQueryType,
    CreateAnalyticsDashboardMutation,
    CreateAnalyticsQueryMutation,
    DeleteAnalyticsDashboardMutation,
    DeleteAnalyticsQueryMutation,
    DeleteAnalyticsWidgetMutation,
    ExportAnalyticsDataMutation,
    Mutation,
    Query,
    UpdateAnalyticsDashboardLayoutMutation,
    UpdateAnalyticsDashboardMutation,
    UpdateAnalyticsQueryMutation,
    _can_edit_dashboard,
)
from analytics.services import ExportService, QueryBuilderService
from analytics.tests.test_permissions import _info, _query_input
from analytics.tests.test_query_builder import _marker, _role_user

VIEW, QUERY, EXPORT, DASHBOARD_EDIT, SHARE, CREATE, UPDATE = (
    200001, 200002, 200003, 200004, 200005, 200006, 200007,
)


def _relay_id(type_name, pk):
    return base64.b64encode(f'{type_name}:{pk}'.encode()).decode()


def _saved_query(owner, **extra):
    data = dict(
        name='Saved', entity_type='individual', query_config={'limit': 5}, is_public=False, created_by=owner,
    )
    data.update(extra)
    return AnalyticsQuery.objects.create(**data)


class ShareRightTest(TestCase):
    def setUp(self):
        cache.clear()
        self.marker = _marker()
        self.creator = _role_user(f'an_share_no_{self.marker}', [QUERY, CREATE, UPDATE])
        self.sharer = _role_user(f'an_share_yes_{self.marker}', [QUERY, CREATE, UPDATE, SHARE])

    def test_public_create_requires_share_right(self):
        with self.assertRaises(PermissionDenied):
            CreateAnalyticsQueryMutation.mutate(None, _info(self.creator), _query_input(is_public=True))
        self.assertFalse(AnalyticsQuery.objects.filter(created_by=self.creator).exists())

    def test_public_create_with_share_right(self):
        result = CreateAnalyticsQueryMutation.mutate(None, _info(self.sharer), _query_input(is_public=True))
        self.assertTrue(result.query.is_public)

    def test_private_create_needs_no_share_right(self):
        result = CreateAnalyticsQueryMutation.mutate(None, _info(self.creator), _query_input(is_public=False))
        self.assertFalse(result.query.is_public)

    def test_making_an_existing_query_public_requires_share_right(self):
        obj = _saved_query(self.creator)
        with self.assertRaises(PermissionDenied):
            UpdateAnalyticsQueryMutation.mutate(None, _info(self.creator), obj.id, _query_input(is_public=True))
        obj.refresh_from_db()
        self.assertFalse(obj.is_public)


class SavedQueryMutationTest(TestCase):
    def setUp(self):
        cache.clear()
        self.marker = _marker()
        self.owner = _role_user(f'an_owner_{self.marker}', [QUERY, CREATE, UPDATE])
        self.other = _role_user(f'an_other_{self.marker}', [QUERY, CREATE, UPDATE])

    def test_update_accepts_the_relay_id_the_list_returns(self):
        obj = _saved_query(self.owner)
        result = UpdateAnalyticsQueryMutation.mutate(
            None, _info(self.owner), _relay_id('AnalyticsQueryType', obj.id), _query_input(name='Renamed'),
        )
        self.assertEqual(result.query.id, obj.id)
        self.assertEqual(AnalyticsQuery.objects.filter(created_by=self.owner).count(), 1)

    def test_group_beneficiary_query_can_be_saved(self):
        result = CreateAnalyticsQueryMutation.mutate(
            None, _info(self.owner), _query_input(entity_type='group_beneficiary'),
        )
        self.assertEqual(result.query.entity_type, 'group_beneficiary')

    def test_unknown_entity_type_is_rejected(self):
        with self.assertRaises(ValueError):
            CreateAnalyticsQueryMutation.mutate(None, _info(self.owner), _query_input(entity_type='activity'))

    def test_delete_retires_the_query(self):
        obj = _saved_query(self.owner)
        DeleteAnalyticsQueryMutation.mutate(None, _info(self.owner), _relay_id('AnalyticsQueryType', obj.id))
        obj.refresh_from_db()
        self.assertIsNotNone(obj.validity_to)
        listed = Query().resolve_analytics_queries(_info(self.owner))
        self.assertFalse(listed.filter(pk=obj.id).exists())

    def test_delete_of_another_users_query_is_refused(self):
        obj = _saved_query(self.owner)
        with self.assertRaises(PermissionDenied):
            DeleteAnalyticsQueryMutation.mutate(None, _info(self.other), str(obj.id))
        obj.refresh_from_db()
        self.assertIsNone(obj.validity_to)

    def test_can_edit_is_true_only_for_the_owner(self):
        obj = _saved_query(self.owner, is_public=True)
        self.assertTrue(AnalyticsQueryType.resolve_can_edit(obj, _info(self.owner)))
        self.assertFalse(AnalyticsQueryType.resolve_can_edit(obj, _info(self.other)))

    def test_can_edit_needs_the_update_right(self):
        reader = _role_user(f'an_reader_{self.marker}', [QUERY, CREATE])
        obj = _saved_query(reader)
        self.assertFalse(AnalyticsQueryType.resolve_can_edit(obj, _info(reader)))

    def test_can_edit_holds_for_a_superuser_on_any_query(self):
        admin = create_test_interactive_user(username=f'an_admin_{self.marker}')
        self.assertTrue(admin.is_superuser)
        obj = _saved_query(self.owner, is_public=True)
        self.assertTrue(AnalyticsQueryType.resolve_can_edit(obj, _info(admin)))

    def test_delete_of_a_query_used_by_a_widget_is_refused(self):
        obj = _saved_query(self.owner)
        dashboard = AnalyticsDashboard.objects.create(name='D', created_by=self.owner)
        AnalyticsWidget.objects.create(
            dashboard=dashboard, query=obj, widget_type='table', title='W', config={'a': 1}, position={'x': 0},
        )
        with self.assertRaises(ValueError):
            DeleteAnalyticsQueryMutation.mutate(None, _info(self.owner), str(obj.id))


class DashboardTest(TestCase):
    def setUp(self):
        cache.clear()
        self.marker = _marker()
        self.owner = _role_user(f'an_dash_owner_{self.marker}', [VIEW, QUERY, DASHBOARD_EDIT])
        self.viewer = _role_user(f'an_dash_viewer_{self.marker}', [VIEW])
        Individual(
            first_name='W', last_name=self.marker, dob=datetime.date(1990, 1, 1), json_ext={},
        ).save(user=self.owner)
        self.query = _saved_query(self.owner, query_config={
            'filters': {'last_name': {'operator': 'exact', 'value': self.marker}},
            'aggregations': {'n': {'function': 'count', 'field': 'id'}},
        })
        self.dashboard = AnalyticsDashboard.objects.create(name='D', created_by=self.owner, is_public=True)
        self.widget = AnalyticsWidget.objects.create(
            dashboard=self.dashboard, query=self.query, widget_type='metric', title='N',
            config={'a': 1}, position={'x': 0, 'y': 0, 'w': 4, 'h': 4},
        )

    def _execute_widget(self, user):
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            return Query().resolve_execute_analytics_widget(
                _info(user), _relay_id('AnalyticsWidgetType', self.widget.id)
            )

    def test_dashboard_viewer_gets_widget_data_without_the_query_right(self):
        result = self._execute_widget(self.viewer)
        self.assertEqual(result.data, [{'n': 1}])

    def test_widget_of_a_private_dashboard_is_refused(self):
        self.dashboard.is_public = False
        self.dashboard.save()
        with self.assertRaises(PermissionDenied):
            self._execute_widget(self.viewer)

    def test_owner_saves_the_layout(self):
        UpdateAnalyticsDashboardLayoutMutation.mutate(
            None, _info(self.owner), _relay_id('AnalyticsDashboardType', self.dashboard.id),
            {_relay_id('AnalyticsWidgetType', self.widget.id): {'x': 6, 'y': 1, 'w': 6, 'h': 3}},
        )
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.position, {'x': 6, 'y': 1, 'w': 6, 'h': 3})

    def test_layout_of_another_users_dashboard_is_refused(self):
        other = _role_user(f'an_dash_other_{self.marker}', [VIEW, DASHBOARD_EDIT])
        self.assertFalse(_can_edit_dashboard(other, self.dashboard))
        self.assertTrue(_can_edit_dashboard(self.owner, self.dashboard))
        with self.assertRaises(PermissionDenied):
            UpdateAnalyticsDashboardLayoutMutation.mutate(
                None, _info(other), str(self.dashboard.id),
                {str(self.widget.id): {'x': 1, 'y': 1, 'w': 1, 'h': 1}},
            )


class ExportTest(TestCase):
    def setUp(self):
        cache.clear()
        self.marker = _marker()
        self.user = _role_user(f'an_export_{self.marker}', [QUERY, EXPORT])
        for name in ('A', 'B', 'C'):
            Individual(
                first_name=name, last_name=self.marker, dob=datetime.date(1990, 1, 1), json_ext={},
            ).save(user=self.user)
        self.config = {
            'filters': {'last_name': {'operator': 'exact', 'value': self.marker}},
            'fields': ['first_name'],
            'limit': 1,
        }

    def _export(self):
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            result = ExportAnalyticsDataMutation.mutate(
                None, _info(self.user), entity_type='individual', query_config=self.config, export_format='csv',
            )
        record = AnalyticsExport.objects.get(pk=result.export_id)
        self.addCleanup(lambda: os.path.exists(record.file_path) and os.remove(record.file_path))
        return result

    def test_export_is_not_cut_to_the_on_screen_limit(self):
        self.assertEqual(self._export().row_count, 3)

    def test_export_over_the_export_cap_is_refused(self):
        with mock.patch.object(AnalyticsConfig, 'analytics_max_export_rows', 2):
            with self.assertRaises(ValueError):
                self._export()


class ExcelExportTest(TestCase):
    def test_more_than_26_columns(self):
        filepath = ExportService.export_to_excel([{f'c{i}': i for i in range(30)}], f'analytics_wide_{_marker()}')
        self.addCleanup(lambda: os.path.exists(filepath) and os.remove(filepath))
        self.assertTrue(os.path.exists(filepath))


class DownloadWithJwtTest(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        filepath = os.path.join(self.tmpdir, 'export.csv')
        with open(filepath, 'w') as fh:
            fh.write('a\n1\n')
        self.user = _role_user(f'an_dl_{_marker()}', [EXPORT])
        export = AnalyticsExport.objects.create(
            export_format='csv', filters_applied={}, row_count=1, file_path=filepath, exported_by=self.user,
        )
        self.url = reverse('analytics_download_export', kwargs={'export_id': export.id})

    def test_jwt_cookie_of_the_web_client_is_accepted(self):
        client = Client()
        client.cookies['JWT'] = get_token(self.user)
        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b'a\n1\n')

    def test_invalid_jwt_is_sent_to_login(self):
        client = Client()
        client.cookies['JWT'] = 'not-a-token'
        self.assertEqual(client.get(self.url).status_code, 302)


BUILT_IN_DASHBOARDS = [
    ('System Overview', 'Key system metrics'),
    ('Beneficiary Analytics', 'Beneficiary management analytics'),
    ('Payment Analytics', 'Payment tracking and analysis'),
]


class BuiltInDashboardsTest(TestCase):
    def test_app_start_creates_no_dashboard(self):
        create_test_interactive_user(username='Admin')
        before = AnalyticsDashboard.objects.count()
        apps.get_app_config('analytics').ready()
        self.assertEqual(AnalyticsDashboard.objects.count(), before)
        self.assertFalse(AnalyticsDashboard.objects.filter(
            name__in=[name for name, _ in BUILT_IN_DASHBOARDS]
        ).exists())

    @override_settings(IS_TESTING=False)
    def test_reseed_retires_the_empty_built_in_dashboards_only(self):
        admin = create_test_interactive_user(username='Admin')
        layout = {"columns": 12, "rowHeight": 100}
        empty = [
            AnalyticsDashboard.objects.create(
                name=name, description=desc, is_public=True, created_by=admin, layout_config=layout,
            )
            for name, desc in BUILT_IN_DASHBOARDS
        ]
        filled = AnalyticsDashboard.objects.create(
            name='System Overview', description='Key system metrics', is_public=True, created_by=admin,
            layout_config=layout,
        )
        AnalyticsWidget.objects.create(
            dashboard=filled, query=_saved_query(admin), widget_type='table', title='W',
            config={'display': 'default'}, position={'x': 0, 'y': 0, 'w': 6, 'h': 4},
        )
        renamed = AnalyticsDashboard.objects.create(
            name='Payment Analytics', description='Paiements par commune', is_public=True, created_by=admin,
            layout_config=layout,
        )
        call_command('seed_analytics_dashboards', stdout=open(os.devnull, 'w'))
        for dashboard in empty:
            dashboard.refresh_from_db()
            self.assertIsNotNone(dashboard.validity_to, dashboard.name)
        for dashboard in (filled, renamed):
            dashboard.refresh_from_db()
            self.assertIsNone(dashboard.validity_to, dashboard.description)
        admin_listing = Query().resolve_analytics_dashboards(_info(admin))
        self.assertFalse(admin_listing.filter(pk__in=[d.pk for d in empty]).exists())


class DashboardEditingTest(TestCase):
    """A holder of 200004 creates and edits their own dashboards; 200005 is
    needed to make one public."""

    def setUp(self):
        cache.clear()
        self.marker = _marker()
        self.editor = _role_user(f'an_ed_{self.marker}', [VIEW, QUERY, DASHBOARD_EDIT])
        self.sharer = _role_user(f'an_ed_sh_{self.marker}', [VIEW, QUERY, DASHBOARD_EDIT, SHARE])
        self.viewer = _role_user(f'an_ed_view_{self.marker}', [VIEW])
        self.other = _role_user(f'an_ed_other_{self.marker}', [VIEW, QUERY, DASHBOARD_EDIT])
        Individual(
            first_name='E', last_name=self.marker, dob=datetime.date(1990, 1, 1), json_ext={},
        ).save(user=self.editor)
        self.public_query = _saved_query(self.other, name='Public', is_public=True, query_config={
            'filters': {'last_name': {'operator': 'exact', 'value': self.marker}},
            'aggregations': {'n': {'function': 'count', 'field': 'id'}},
        })
        self.private_query = _saved_query(self.other, name='Private')

    def _create(self, user, **fields):
        data = dict(name='Mon tableau', description=None, is_public=False)
        data.update(fields)
        return CreateAnalyticsDashboardMutation.mutate(None, _info(user), SimpleNamespace(**data)).dashboard

    def _add_widget(self, user, dashboard, query, **extra):
        data = dict(widget_type='bar_chart', title='Nombre')
        data.update(extra)
        return AddAnalyticsWidgetMutation.mutate(
            None, _info(user), _relay_id('AnalyticsDashboardType', dashboard.id),
            _relay_id('AnalyticsQueryType', query.id), **data,
        ).widget

    def test_editor_creates_a_private_dashboard_they_can_edit(self):
        dashboard = self._create(self.editor)
        self.assertEqual(dashboard.created_by_id, self.editor.id)
        self.assertFalse(dashboard.is_public)
        self.assertFalse(dashboard.is_default)
        self.assertTrue(_can_edit_dashboard(self.editor, dashboard))
        listed = Query().resolve_analytics_dashboards(_info(self.editor))
        self.assertTrue(listed.filter(pk=dashboard.pk).exists())
        self.assertFalse(Query().resolve_analytics_dashboards(_info(self.viewer)).filter(pk=dashboard.pk).exists())

    def test_create_without_the_dashboard_right_is_refused(self):
        user = _role_user(f'an_ed_none_{self.marker}', [VIEW, QUERY])
        with self.assertRaises(PermissionDenied):
            self._create(user)

    def test_public_dashboard_requires_the_share_right(self):
        with self.assertRaises(PermissionDenied):
            self._create(self.editor, is_public=True)
        self.assertTrue(self._create(self.sharer, is_public=True).is_public)

    def test_sharing_an_existing_dashboard_requires_the_share_right(self):
        dashboard = self._create(self.editor)
        with self.assertRaises(PermissionDenied):
            UpdateAnalyticsDashboardMutation.mutate(
                None, _info(self.editor), str(dashboard.id),
                SimpleNamespace(name='Mon tableau', description=None, is_public=True),
            )
        dashboard.refresh_from_db()
        self.assertFalse(dashboard.is_public)
        shared = self._create(self.sharer)
        UpdateAnalyticsDashboardMutation.mutate(
            None, _info(self.sharer), _relay_id('AnalyticsDashboardType', shared.id),
            SimpleNamespace(name='Partagé', description='d', is_public=True),
        )
        shared.refresh_from_db()
        self.assertEqual((shared.name, shared.is_public), ('Partagé', True))

    def test_editing_another_users_dashboard_is_refused(self):
        dashboard = self._create(self.editor)
        with self.assertRaises(PermissionDenied):
            UpdateAnalyticsDashboardMutation.mutate(
                None, _info(self.other), str(dashboard.id),
                SimpleNamespace(name='X', description=None, is_public=None),
            )
        with self.assertRaises(PermissionDenied):
            self._add_widget(self.other, dashboard, self.public_query)

    @override_settings(IS_TESTING=False)
    def test_widget_from_a_public_query_is_shown_to_viewers_of_a_public_dashboard(self):
        # IS_TESTING=False makes core's pre_save model validation raise, as it does in service.
        dashboard = self._create(self.sharer, is_public=True)
        widget = self._add_widget(self.sharer, dashboard, self.public_query)
        self.assertEqual(widget.position, {'x': 0, 'y': 0, 'w': 6, 'h': 4})
        self.assertEqual(widget.config, {'display': 'default'})
        UpdateAnalyticsDashboardMutation.mutate(
            None, _info(self.sharer), str(dashboard.id),
            SimpleNamespace(name='Renommé', description='', is_public=None),
        )
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            result = Query().resolve_execute_analytics_widget(
                _info(self.viewer), _relay_id('AnalyticsWidgetType', widget.id),
            )
        self.assertEqual(result.data, [{'n': 1}])

    def test_new_widget_goes_below_the_existing_ones(self):
        dashboard = self._create(self.editor)
        self._add_widget(self.editor, dashboard, self.public_query, position='{"x": 2, "y": 1, "w": 4, "h": 3}')
        widget = self._add_widget(self.editor, dashboard, self.public_query, widget_type='TABLE')
        self.assertEqual(widget.widget_type, 'table')
        self.assertEqual(widget.position, {'x': 0, 'y': 4, 'w': 6, 'h': 4})

    def test_another_users_private_query_cannot_be_added(self):
        dashboard = self._create(self.editor)
        with self.assertRaises(PermissionDenied):
            self._add_widget(self.editor, dashboard, self.private_query)
        self.assertFalse(AnalyticsWidget.objects.filter(dashboard=dashboard).exists())

    def test_unsupported_widget_type_is_refused(self):
        dashboard = self._create(self.editor)
        with self.assertRaises(ValueError):
            self._add_widget(self.editor, dashboard, self.public_query, widget_type='map')

    @override_settings(IS_TESTING=False)
    def test_removed_widget_leaves_the_dashboard(self):
        dashboard = self._create(self.editor)
        kept = self._add_widget(self.editor, dashboard, self.public_query, title='Gardé')
        removed = self._add_widget(self.editor, dashboard, self.public_query, title='Retiré')
        with self.assertRaises(PermissionDenied):
            DeleteAnalyticsWidgetMutation.mutate(None, _info(self.other), str(removed.id))
        DeleteAnalyticsWidgetMutation.mutate(
            None, _info(self.editor), _relay_id('AnalyticsWidgetType', removed.id),
        )
        result = graphene.Schema(query=Query, mutation=Mutation).execute(
            '{ analyticsDashboard(id: "%s") { canEdit widgets { edges { node { title } } } } }' % dashboard.id,
            context_value=SimpleNamespace(user=self.editor),
        )
        self.assertIsNone(result.errors)
        titles = [edge['node']['title'] for edge in result.data['analyticsDashboard']['widgets']['edges']]
        self.assertEqual(titles, [kept.title])
        self.assertTrue(result.data['analyticsDashboard']['canEdit'])

    @override_settings(IS_TESTING=False)
    def test_deleted_dashboard_and_its_widgets_are_retired(self):
        dashboard = self._create(self.editor)
        widget = self._add_widget(self.editor, dashboard, self.public_query)
        DeleteAnalyticsDashboardMutation.mutate(None, _info(self.editor), str(dashboard.id))
        dashboard.refresh_from_db()
        widget.refresh_from_db()
        self.assertIsNotNone(dashboard.validity_to)
        self.assertIsNotNone(widget.validity_to)
        with self.assertRaises(AnalyticsDashboard.DoesNotExist):
            Query().resolve_analytics_dashboard(_info(self.editor), str(dashboard.id))

    def test_default_dashboard_cannot_be_deleted(self):
        dashboard = self._create(self.editor)
        AnalyticsDashboard.objects.filter(pk=dashboard.pk).update(is_default=True)
        with self.assertRaises(ValueError):
            DeleteAnalyticsDashboardMutation.mutate(None, _info(self.editor), str(dashboard.id))

    def test_graphql_exposes_the_dashboard_mutations(self):
        schema = graphene.Schema(query=Query, mutation=Mutation)
        names = set(schema.get_mutation_type().fields)
        self.assertTrue({
            'createAnalyticsDashboard', 'updateAnalyticsDashboard', 'deleteAnalyticsDashboard',
            'addAnalyticsWidget', 'deleteAnalyticsWidget',
        } <= names)


class SeedCommandTest(TestCase):
    def test_seeded_queries_run_and_the_activity_query_is_retired(self):
        admin = create_test_interactive_user(username='Admin')
        retired = AnalyticsQuery.objects.create(
            name='Activités par statut', entity_type='beneficiary', created_by=admin, is_public=True,
            query_config={'measures': ['count'], 'dimensions': ['status'], 'filters': []},
        )
        call_command('seed_analytics_dashboards', stdout=open(os.devnull, 'w'))
        retired.refresh_from_db()
        self.assertIsNotNone(retired.validity_to)
        for name in ('Bénéficiaires par programme', 'Bénéficiaires par province',
                     'Paiements par statut', 'Plaintes par catégorie'):
            query = AnalyticsQuery.objects.get(name=name, validity_to__isnull=True)
            QueryBuilderService._execute_orm_query(query.entity_type, query.query_config, admin)
