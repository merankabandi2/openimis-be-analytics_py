"""The export mutation queues a Celery task; the task checks the rights again,
runs under the timeout and the export cap, writes the file and notifies."""
import datetime
import os
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import TestCase

from core.models import RoleRight
from individual.models import Individual

from analytics import tasks
from analytics.apps import AnalyticsConfig
from analytics.models import AnalyticsExport
from analytics.schema import ExportAnalyticsDataMutation
from analytics.services import QueryBuilderService
from analytics.tests.test_cost_bounds import _current_timeout
from analytics.tests.test_permissions import _info
from analytics.tests.test_query_builder import _marker, _role_user, _widen_allowlist

QUERY, EXPORT = (
    int(getattr(AnalyticsConfig, attr)[0]) for attr in ('gql_analytics_query_perms', 'gql_analytics_export_perms')
)


class ExportTaskTestCase(TestCase):
    def setUp(self):
        cache.clear()
        _widen_allowlist(self, individual=['first_name', 'last_name'])
        opensearch = mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False)
        opensearch.start()
        self.addCleanup(opensearch.stop)
        self.marker = _marker()
        self.user = _role_user(f'an_task_{self.marker}', [QUERY, EXPORT])
        for name in ('A', 'B', 'C'):
            Individual(
                first_name=name, last_name=self.marker, dob=datetime.date(1990, 1, 1), json_ext={},
            ).save(user=self.user)
        self.config = {
            'filters': {'last_name': {'operator': 'exact', 'value': self.marker}},
            'fields': ['first_name'],
            'limit': 1,
        }
        notify = mock.patch('notification.services.NotificationService.notify')
        self.notify = notify.start()
        self.addCleanup(notify.stop)

    def _queue(self):
        """Call the mutation with the task's delay replaced; return (result, delay mock)."""
        with mock.patch.object(tasks.export_analytics_data, 'delay') as delay:
            result = ExportAnalyticsDataMutation.mutate(
                None, _info(self.user), entity_type='individual', query_config=self.config, export_format='csv',
            )
        return result, delay

    def _run(self):
        export_id = tasks.export_analytics_data(str(self.user.id), 'individual', self.config, 'csv')
        if export_id:
            record = AnalyticsExport.objects.get(pk=export_id)
            self.addCleanup(lambda: os.path.exists(record.file_path) and os.remove(record.file_path))
        return export_id

    def _notified(self):
        self.assertEqual(self.notify.call_count, 1)
        kwargs = self.notify.call_args.kwargs
        self.assertIsNone(kwargs['actor'])
        self.assertEqual([u.id for u in kwargs['recipients']], [self.user.id])
        return kwargs

    def _assert_refused(self, reason):
        self.assertFalse(AnalyticsExport.objects.filter(exported_by=self.user).exists())
        kwargs = self._notified()
        self.assertEqual(kwargs['event_code'], 'analytics.export_failed')
        self.assertEqual(kwargs['context']['reason'], reason)


class ExportQueueTest(ExportTaskTestCase):
    def test_the_mutation_queues_the_export_instead_of_running_it(self):
        with mock.patch.object(QueryBuilderService, 'execute_query') as execute:
            result, delay = self._queue()
        execute.assert_not_called()
        delay.assert_called_once_with(str(self.user.id), 'individual', self.config, 'csv', None)
        self.assertTrue(result.queued)
        self.assertIsNone(result.export_id)
        self.assertFalse(AnalyticsExport.objects.filter(exported_by=self.user).exists())

    def test_without_the_export_right_nothing_is_queued(self):
        user = _role_user(f'an_task_noexp_{self.marker}', [QUERY])
        with mock.patch.object(tasks.export_analytics_data, 'delay') as delay:
            with self.assertRaises(PermissionDenied):
                ExportAnalyticsDataMutation.mutate(
                    None, _info(user), entity_type='individual', query_config=self.config, export_format='csv',
                )
        delay.assert_not_called()

    def test_superuser_only_refuses_a_rights_holder_at_queue_time(self):
        with mock.patch.object(AnalyticsConfig, 'analytics_superuser_only', True), \
                mock.patch.object(tasks.export_analytics_data, 'delay') as delay:
            with self.assertRaises(PermissionDenied):
                ExportAnalyticsDataMutation.mutate(
                    None, _info(self.user), entity_type='individual', query_config=self.config,
                    export_format='csv',
                )
        delay.assert_not_called()

    def test_a_field_outside_the_allowlist_is_refused_at_queue_time(self):
        config = {'filters': {'dob': {'operator': 'exact', 'value': '1990-01-01'}}}
        with mock.patch.object(tasks.export_analytics_data, 'delay') as delay:
            with self.assertRaises(ValueError) as refused:
                ExportAnalyticsDataMutation.mutate(
                    None, _info(self.user), entity_type='individual', query_config=config, export_format='csv',
                )
        self.assertEqual(str(refused.exception), "Field 'dob' is not allowed in filters for entity 'individual'")
        delay.assert_not_called()

    def test_an_unknown_operator_or_format_is_refused_at_queue_time(self):
        config = {'filters': {'last_name': {'operator': 'like', 'value': self.marker}}}
        with mock.patch.object(tasks.export_analytics_data, 'delay') as delay:
            with self.assertRaises(ValueError) as operator:
                ExportAnalyticsDataMutation.mutate(
                    None, _info(self.user), entity_type='individual', query_config=config, export_format='csv',
                )
            with self.assertRaises(ValueError) as export_format:
                ExportAnalyticsDataMutation.mutate(
                    None, _info(self.user), entity_type='individual', query_config=self.config,
                    export_format='pdf',
                )
        self.assertEqual(str(operator.exception), "Unsupported filter operator 'like'")
        self.assertEqual(str(export_format.exception), 'Unsupported export format: pdf')
        delay.assert_not_called()


class ExportTaskRunTest(ExportTaskTestCase):
    def test_the_task_writes_the_file_records_it_and_notifies_the_requester(self):
        export_id = self._run()
        record = AnalyticsExport.objects.get(pk=export_id)
        self.assertEqual(record.exported_by_id, self.user.id)
        self.assertEqual(record.row_count, 3)
        self.assertEqual(record.filters_applied, self.config)
        with open(record.file_path) as handle:
            self.assertEqual(sorted(handle.read().split()), ['A', 'B', 'C', 'first_name'])
        kwargs = self._notified()
        self.assertEqual(kwargs['event_code'], 'analytics.export_ready')
        self.assertEqual(kwargs['entity_url'], '/analytics/export-history')
        self.assertEqual(kwargs['entity'], record)
        self.assertEqual(kwargs['context'], {'export_format': 'CSV', 'row_count': 3})

    def test_a_right_withdrawn_after_queueing_is_refused_when_the_task_runs(self):
        _, delay = self._queue()
        delay.assert_called_once()
        RoleRight.objects.filter(role__name=f'analytics-test-an_task_{self.marker}', right_id=EXPORT).delete()
        cache.clear()
        self.assertIsNone(self._run())
        self._assert_refused('Unauthorized')

    def test_superuser_only_switched_on_after_queueing_refuses_the_task(self):
        self._queue()
        with mock.patch.object(AnalyticsConfig, 'analytics_superuser_only', True):
            self.assertIsNone(self._run())
        self._assert_refused('Unauthorized')

    def test_the_export_cap_applies_in_the_task(self):
        with mock.patch.object(AnalyticsConfig, 'analytics_max_export_rows', 2):
            self.assertIsNone(self._run())
        self._assert_refused('Export exceeds maximum rows (2); add filters or grouping')

    def test_the_task_query_runs_under_the_timeout(self):
        seen = []
        original = QueryBuilderService._scoped_queryset

        def spy(entity_type, model, user, referenced_fields):
            seen.append(_current_timeout())
            return original(entity_type, model, user, referenced_fields)
        with mock.patch.object(AnalyticsConfig, 'analytics_query_timeout', 9), \
                mock.patch.object(QueryBuilderService, '_scoped_queryset', side_effect=spy):
            self.assertIsNotNone(self._run())
        self.assertEqual(seen, ['9s'])

    def test_a_task_query_past_the_timeout_is_stopped_and_reported(self):
        from django.db import connection
        original = QueryBuilderService._scoped_queryset

        def slow(entity_type, model, user, referenced_fields):
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_sleep(2)')
            return original(entity_type, model, user, referenced_fields)
        with mock.patch.object(AnalyticsConfig, 'analytics_query_timeout', 0.2), \
                mock.patch.object(QueryBuilderService, '_scoped_queryset', side_effect=slow):
            self.assertIsNone(self._run())
        self._assert_refused('The query ran longer than 0.2 seconds and was stopped; narrow the filters')
