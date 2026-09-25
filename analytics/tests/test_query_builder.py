import datetime
import random
import string
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from core.models import Role, RoleRight
from core.test_helpers import create_test_interactive_user
from individual.models import Individual, Group
from location.test_helpers import create_test_village, assign_user_districts

from analytics.apps import AnalyticsConfig
from analytics.models import AnalyticsQuery
from analytics.services import QueryBuilderService


def _marker():
    return 'AN' + ''.join(random.choices(string.ascii_uppercase, k=8))


def _role_user(username, rights):
    """Non-admin interactive user holding exactly `rights`."""
    role = Role.objects.create(
        name=f'analytics-test-{username}',
        is_system=0,
        is_blocked=False,
        audit_user_id=-1,
        validity_from=datetime.datetime.now(),
    )
    for right in rights:
        RoleRight.objects.create(
            role=role, right_id=int(right), audit_user_id=-1, validity_from=datetime.datetime.now()
        )
    cache.clear()
    return create_test_interactive_user(username=username, roles=[role.id])


def _run(entity, config, user, **kwargs):
    return QueryBuilderService._execute_orm_query(entity, config, user, **kwargs)


class OrmQueryTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        self.marker = _marker()
        for first_name in ('Alice', 'Alain', 'Bob', 'Bob'):
            Individual(
                first_name=first_name, last_name=self.marker, dob=datetime.date(1990, 1, 1), json_ext={},
            ).save(user=self.admin)

    def _config(self, **extra):
        config = {'filters': {'last_name': {'operator': 'exact', 'value': self.marker}}}
        config.update(extra)
        return config

    def _names(self, config):
        rows = _run('individual', config, self.admin, max_rows=100).rows
        return sorted(row['first_name'] for row in rows)


class FilterOperatorTest(OrmQueryTestCase):
    def _with_filter(self, operator, value):
        config = self._config(fields=['first_name'])
        config['filters']['first_name'] = {'operator': operator, 'value': value}
        return self._names(config)

    def test_ne_excludes_the_value(self):
        self.assertEqual(self._with_filter('ne', 'Bob'), ['Alain', 'Alice'])

    def test_startswith_matches_prefix(self):
        self.assertEqual(self._with_filter('startswith', 'al'), ['Alain', 'Alice'])

    def test_endswith_matches_suffix(self):
        self.assertEqual(self._with_filter('endswith', 'CE'), ['Alice'])

    def test_not_in_excludes_listed_values(self):
        self.assertEqual(self._with_filter('not_in', ['Bob', 'Alain']), ['Alice'])

    def test_unknown_operator_is_rejected(self):
        with self.assertRaises(ValueError):
            self._with_filter('matches', 'x')


class SoftDeleteTest(OrmQueryTestCase):
    def test_soft_deleted_rows_are_not_counted(self):
        deleted = Individual(
            first_name='Gone', last_name=self.marker, dob=datetime.date(1990, 1, 1), json_ext={},
        )
        deleted.save(user=self.admin)
        deleted.is_deleted = True
        deleted.save(user=self.admin)
        rows = _run('individual', self._config(
            group_by=['is_deleted'], aggregations={'n': {'function': 'count', 'field': 'id'}},
        ), self.admin).rows
        self.assertEqual(rows, [{'is_deleted': False, 'n': 4}])


class GroupingTest(OrmQueryTestCase):
    def test_aggregation_without_group_by_returns_one_total_row(self):
        rows = _run('individual', self._config(
            aggregations={'nb': {'function': 'count', 'field': 'id'}},
        ), self.admin).rows
        self.assertEqual(rows, [{'nb': 4}])

    def test_group_by_without_aggregation_returns_distinct_groups(self):
        rows = _run('individual', self._config(group_by=['first_name']), self.admin).rows
        self.assertEqual(sorted(row['first_name'] for row in rows), ['Alain', 'Alice', 'Bob'])

    def test_order_by_descending_aggregation(self):
        rows = _run('individual', self._config(
            group_by=['first_name'],
            aggregations={'nb': {'function': 'count', 'field': 'id'}},
            order_by=['-nb', 'first_name'],
        ), self.admin).rows
        self.assertEqual(rows[0], {'first_name': 'Bob', 'nb': 2})


class FieldSelectionTest(OrmQueryTestCase):
    def test_selected_fields_are_the_only_columns(self):
        rows = _run('individual', self._config(fields=['first_name', 'last_name']), self.admin).rows
        self.assertEqual(len(rows), 4)
        self.assertEqual({tuple(sorted(row)) for row in rows}, {('first_name', 'last_name')})

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(ValueError):
            _run('individual', self._config(fields=['groupindividuals__group__code']), self.admin)


class LimitTest(OrmQueryTestCase):
    def test_truncated_is_reported(self):
        result = _run('individual', self._config(fields=['first_name'], limit=3), self.admin)
        self.assertEqual(len(result.rows), 3)
        self.assertTrue(result.truncated)

    def test_not_truncated_when_all_rows_fit(self):
        result = _run('individual', self._config(fields=['first_name'], limit=4), self.admin)
        self.assertEqual(len(result.rows), 4)
        self.assertFalse(result.truncated)


class RelationPathTest(TestCase):
    def test_programme_and_province_paths_are_allowed_for_group_beneficiaries(self):
        admin = create_test_interactive_user(username='analytics_qb_admin')
        for dimension in ('benefit_plan__name', 'group__location__parent__parent__name'):
            _run('group_beneficiary', {'measures': ['count'], 'dimensions': [dimension]}, admin)

    def test_other_relation_paths_stay_rejected(self):
        admin = create_test_interactive_user(username='analytics_qb_admin')
        with self.assertRaises(ValueError):
            _run('group_beneficiary', {'group_by': ['group__groupindividuals__individual__first_name']}, admin)


class LocationScopeTest(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        self.marker = _marker()
        code_in, code_out = _marker()[:6], _marker()[:6]
        self.village_in = create_test_village({'code': code_in})
        self.village_out = create_test_village({'code': code_out})
        for village in (self.village_in, self.village_out):
            Group(code=self.marker, location=village, json_ext={}).save(user=self.admin)
        self.user = _role_user(f'an_loc_{self.marker}', AnalyticsConfig.gql_analytics_query_perms)
        assign_user_districts(self.user, [f'D-{code_in}'])
        cache.clear()

    @override_settings(ROW_SECURITY=True)
    def test_rows_outside_the_users_districts_are_excluded(self):
        rows = _run('group', {
            'filters': {'code': {'operator': 'exact', 'value': self.marker}},
            'group_by': ['location_id'],
            'aggregations': {'n': {'function': 'count', 'field': 'id'}},
        }, self.user).rows
        self.assertEqual(rows, [{'location_id': self.village_in.id, 'n': 1}])

    @override_settings(ROW_SECURITY=True)
    def test_cached_result_is_not_shared_between_users(self):
        config = {
            'filters': {'code': {'operator': 'exact', 'value': self.marker}},
            'aggregations': {'n': {'function': 'count', 'field': 'id'}},
        }
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            admin_rows = QueryBuilderService.execute_query('group', config, self.admin).rows
            user_rows = QueryBuilderService.execute_query('group', config, self.user).rows
        self.assertEqual(admin_rows, [{'n': 2}])
        self.assertEqual(user_rows, [{'n': 1}])


class EntityTypeChoiceTest(TestCase):
    def test_group_beneficiary_is_a_valid_saved_query_entity(self):
        owner = create_test_interactive_user(username='analytics_qb_admin')
        query = AnalyticsQuery(
            name='gb', entity_type='group_beneficiary', query_config={'limit': 1}, created_by=owner,
        )
        try:
            query.full_clean()
        except ValidationError as exc:
            self.fail(f'group_beneficiary rejected: {exc}')
