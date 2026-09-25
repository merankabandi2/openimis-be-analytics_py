import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from core.test_helpers import create_test_interactive_user, create_test_role

from analytics.apps import AnalyticsConfig
from analytics.models import AnalyticsExport, AnalyticsQuery
from analytics.schema import CreateAnalyticsQueryMutation, UpdateAnalyticsQueryMutation
from analytics.services import ExportService, QueryBuilderService, QueryResult


def _info(user):
    """Minimal stand-in for graphene's ResolveInfo: only `.context.user` is read."""
    info = mock.Mock()
    info.context.user = user
    return info


def _query_input(**overrides):
    data = dict(
        name='Test query',
        description='A test query',
        entity_type='individual',
        query_config='{}',
        is_public=False,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _denied_user():
    """A user object whose has_perms() always returns False, without touching the DB."""
    user = mock.Mock(is_anonymous=False)
    user.has_perms = mock.MagicMock(return_value=False)
    return user


class CreateAnalyticsQueryPermissionTest(TestCase):
    def test_unauthorized_create_is_rejected(self):
        user = _denied_user()
        with self.assertRaises(PermissionDenied):
            CreateAnalyticsQueryMutation.mutate(None, _info(user), _query_input())
        user.has_perms.assert_called_with(AnalyticsConfig.gql_analytics_query_create_perms)
        self.assertEqual(AnalyticsQuery.objects.count(), 0)

    def test_authorized_create_is_accepted(self):
        user = create_test_interactive_user(username='analytics_create_admin')
        result = CreateAnalyticsQueryMutation.mutate(
            None, _info(user), _query_input(name='My saved query')
        )
        self.assertEqual(result.query.name, 'My saved query')
        self.assertEqual(result.query.created_by, user)


class UpdateAnalyticsQueryPermissionTest(TestCase):
    def setUp(self):
        self.owner = create_test_interactive_user(username='analytics_update_admin')
        self.obj = AnalyticsQuery.objects.create(
            name='Original name',
            description='',
            entity_type='individual',
            query_config={},
            is_public=False,
            created_by=self.owner,
        )

    def test_unauthorized_update_is_rejected(self):
        user = _denied_user()
        with self.assertRaises(PermissionDenied):
            UpdateAnalyticsQueryMutation.mutate(
                None, _info(user), self.obj.id, _query_input(name='Hacked')
            )
        user.has_perms.assert_called_with(AnalyticsConfig.gql_analytics_query_update_perms)
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.name, 'Original name')

    def test_authorized_update_is_accepted(self):
        result = UpdateAnalyticsQueryMutation.mutate(
            None, _info(self.owner), self.obj.id, _query_input(name='Updated name')
        )
        self.assertEqual(result.query.name, 'Updated name')


class DownloadExportPermissionTest(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        filepath = os.path.join(self.tmpdir, 'export.csv')
        with open(filepath, 'w') as fh:
            fh.write('a,b\n1,2\n')

        self.exporter = create_test_interactive_user(username='analytics_export_admin')
        self.export = AnalyticsExport.objects.create(
            export_format='csv',
            filters_applied={},
            row_count=1,
            file_path=filepath,
            exported_by=self.exporter,
        )
        self.url = reverse('analytics_download_export', kwargs={'export_id': self.export.id})

    def test_anonymous_is_redirected_to_login(self):
        resp = Client().get(self.url)
        self.assertEqual(resp.status_code, 302)

    def test_authenticated_without_export_right_is_forbidden(self):
        no_rights_role = create_test_role([], name='AnalyticsNoRights')
        no_rights_user = create_test_interactive_user(
            username='analytics_no_rights', roles=[no_rights_role.id]
        )
        client = Client()
        client.force_login(no_rights_user)
        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 403)

    def test_owner_with_export_right_can_download(self):
        client = Client()
        client.force_login(self.exporter)
        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        content = b''.join(resp.streaming_content) if resp.streaming else resp.content
        self.assertEqual(content, b'a,b\n1,2\n')


class PdfExportFormatRemovedTest(TestCase):
    def setUp(self):
        self.exporter = create_test_interactive_user(username='analytics_pdf_admin')

    def test_export_service_rejects_pdf(self):
        with self.assertRaises(ValueError):
            ExportService.export([{'a': 1}], 'somefile', 'pdf')

    def test_export_service_still_supports_csv_and_excel(self):
        for fmt in ('csv', 'excel'):
            filepath = ExportService.export([{'a': 1, 'b': 2}], f'analytics_test_{fmt}', fmt)
            self.addCleanup(lambda p=filepath: os.path.exists(p) and os.remove(p))
            self.assertTrue(os.path.exists(filepath))

    def test_model_no_longer_accepts_pdf_choice(self):
        export = AnalyticsExport(
            export_format='pdf',
            filters_applied={},
            row_count=0,
            exported_by=self.exporter,
        )
        with self.assertRaises(ValidationError):
            export.full_clean()

    def test_export_mutation_rejects_pdf_format(self):
        from analytics.schema import ExportAnalyticsDataMutation

        with mock.patch.object(QueryBuilderService, 'execute_query', return_value=QueryResult([], False)):
            with self.assertRaises(ValueError):
                ExportAnalyticsDataMutation.mutate(
                    None,
                    _info(self.exporter),
                    entity_type='individual',
                    query_config='{}',
                    export_format='pdf',
                )
        self.assertEqual(AnalyticsExport.objects.filter(export_format='pdf').count(), 0)
