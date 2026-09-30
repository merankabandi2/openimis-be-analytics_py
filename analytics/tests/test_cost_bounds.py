"""Cost bounds of analytics queries: text filters on JSON fields are refused,
database statements run under the analytics timeout, exports have a lower cap."""
import datetime
import os
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase

from core.test_helpers import create_test_interactive_user
from individual.models import Individual

from analytics.apps import DEFAULT_CFG, AnalyticsConfig
from analytics.models import AnalyticsExport
from analytics.schema import ExportAnalyticsDataMutation
from analytics.services import QueryBuilderService, QueryTimeout
from analytics.tests.test_permissions import _info
from analytics.tests.test_query_builder import _marker


def _current_timeout():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('statement_timeout')")
        return cursor.fetchone()[0]


class CostBoundsTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_cost_admin')
        self.marker = _marker()
        for first_name in ('Alice', 'Alain', 'Bob'):
            Individual(
                first_name=first_name, last_name=self.marker, dob=datetime.date(1990, 1, 1),
                json_ext={'note': 'alpha'} if first_name == 'Alice' else {},
            ).save(user=self.admin)

    def _config(self, **filters):
        config = {'filters': {'last_name': {'operator': 'exact', 'value': self.marker}}, 'fields': ['first_name']}
        config['filters'].update(filters)
        return config

    def _names(self, config):
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            rows = QueryBuilderService.execute_query('individual', config, self.admin).rows
        return sorted(row['first_name'] for row in rows)

    def _spy_timeout(self, seen, sleep=None):
        """Patch _scoped_queryset to record the statement_timeout in force when
        the query runs, and optionally sleep in the database first."""
        original = QueryBuilderService._scoped_queryset

        def spy(entity_type, model, user, referenced_fields):
            seen.append(_current_timeout())
            if sleep:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_sleep(%s)', [sleep])
            return original(entity_type, model, user, referenced_fields)
        return mock.patch.object(QueryBuilderService, '_scoped_queryset', side_effect=spy)


class JsonTextFilterTest(CostBoundsTestCase):
    def test_text_filters_on_json_ext_are_refused_for_every_entity(self):
        for entity_type in QueryBuilderService.ENTITY_TYPES:
            for operator in ('contains', 'startswith', 'endswith'):
                with self.subTest(entity=entity_type, operator=operator):
                    config = {'filters': {'json_ext': {'operator': operator, 'value': 'alpha'}}}
                    with self.assertRaises(ValueError) as refused:
                        QueryBuilderService._execute_orm_query(entity_type, config, self.admin)
                    self.assertEqual(
                        str(refused.exception),
                        f"Text filter '{operator}' is not allowed on JSON field 'json_ext' for entity '{entity_type}'",
                    )

    def test_refusal_applies_to_the_list_form_of_filters(self):
        config = {'filters': [{'field': 'json_ext', 'operator': 'contains', 'value': 'alpha'}]}
        with self.assertRaises(ValueError):
            self._names(config)

    def test_other_filters_on_json_ext_still_run(self):
        self.assertEqual(self._names(self._config(json_ext={'operator': 'isnull', 'value': True})), [])
        self.assertEqual(self._names(self._config(json_ext={'operator': 'is_not_null'})), ['Alain', 'Alice', 'Bob'])
        self.assertEqual(self._names(self._config(json_ext={'operator': 'exact', 'value': {'note': 'alpha'}})), ['Alice'])

    def test_text_filters_on_text_columns_still_run(self):
        self.assertEqual(self._names(self._config(first_name={'operator': 'contains', 'value': 'LI'})), ['Alice'])
        self.assertEqual(self._names(self._config(first_name={'operator': 'startswith', 'value': 'al'})), ['Alain', 'Alice'])


class StatementTimeoutTest(CostBoundsTestCase):
    def test_default_timeout_is_30_seconds(self):
        self.assertEqual(DEFAULT_CFG['analytics_query_timeout'], 30)
        self.assertEqual(AnalyticsConfig.analytics_query_timeout, 30)

    def test_query_runs_under_the_configured_timeout_and_the_previous_one_is_restored(self):
        before = _current_timeout()
        seen = []
        with mock.patch.object(AnalyticsConfig, 'analytics_query_timeout', 7), self._spy_timeout(seen):
            self.assertEqual(self._names(self._config()), ['Alain', 'Alice', 'Bob'])
        self.assertEqual(seen, ['7s'])
        self.assertEqual(_current_timeout(), before)

    def test_query_past_the_timeout_is_stopped_with_a_clear_error(self):
        before = _current_timeout()
        with mock.patch.object(AnalyticsConfig, 'analytics_query_timeout', 0.2), self._spy_timeout([], sleep=2):
            with self.assertRaises(QueryTimeout) as stopped:
                self._names(self._config())
        self.assertIsInstance(stopped.exception, ValueError)
        self.assertEqual(
            str(stopped.exception), 'The query ran longer than 0.2 seconds and was stopped; narrow the filters',
        )
        # The connection is usable afterwards and the stopped query was not cached.
        self.assertEqual(_current_timeout(), before)
        self.assertEqual(self._names(self._config()), ['Alain', 'Alice', 'Bob'])

    def test_zero_timeout_leaves_the_connection_setting(self):
        before = _current_timeout()
        seen = []
        with mock.patch.object(AnalyticsConfig, 'analytics_query_timeout', 0), self._spy_timeout(seen):
            self._names(self._config())
        self.assertEqual(seen, [before])

    def test_other_databases_run_without_the_setting(self):
        before = _current_timeout()
        seen = []
        with mock.patch.object(connection, 'vendor', 'microsoft'), self._spy_timeout(seen):
            self._names(self._config())
        self.assertEqual(seen, [before])

    def test_export_runs_under_the_timeout(self):
        seen = []
        with mock.patch.object(AnalyticsConfig, 'analytics_query_timeout', 9), self._spy_timeout(seen), \
                mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            result = ExportAnalyticsDataMutation.mutate(
                None, _info(self.admin), entity_type='individual', query_config=self._config(), export_format='csv',
            )
        record = AnalyticsExport.objects.get(pk=result.export_id)
        self.addCleanup(lambda: os.path.exists(record.file_path) and os.remove(record.file_path))
        self.assertEqual(seen, ['9s'])
        self.assertEqual(result.row_count, 3)


class ExportCapTest(TestCase):
    def test_default_export_cap_is_20000_rows(self):
        self.assertEqual(DEFAULT_CFG['analytics_max_export_rows'], 20000)
        self.assertEqual(AnalyticsConfig.analytics_max_export_rows, 20000)
