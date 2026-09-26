import base64
import datetime
import os
import shutil
import tempfile
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from graphql_jwt.shortcuts import get_token

from core.test_helpers import create_test_interactive_user
from individual.models import Individual

from analytics.apps import AnalyticsConfig
from analytics.models import AnalyticsDashboard, AnalyticsExport, AnalyticsQuery, AnalyticsWidget
from analytics.schema import (
    AnalyticsQueryType,
    CreateAnalyticsQueryMutation,
    DeleteAnalyticsQueryMutation,
    ExportAnalyticsDataMutation,
    Query,
    UpdateAnalyticsDashboardLayoutMutation,
    UpdateAnalyticsQueryMutation,
    _can_edit_dashboard,
)
from analytics.services import DashboardService, ExportService, QueryBuilderService
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


class DefaultDashboardsTest(TestCase):
    def test_built_in_dashboards_are_created_without_a_second_default(self):
        create_test_interactive_user(username='Admin')
        DashboardService.create_default_dashboards()
        names = set(AnalyticsDashboard.objects.values_list('name', flat=True))
        self.assertTrue({'System Overview', 'Beneficiary Analytics', 'Payment Analytics'} <= names)
        self.assertFalse(AnalyticsDashboard.objects.filter(
            name__in=['System Overview', 'Beneficiary Analytics', 'Payment Analytics'], is_default=True,
        ).exists())


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
