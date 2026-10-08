import datetime
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings

from core.models import Role, RoleRight
from core.services.userServices import create_or_update_user_roles
from core.test_helpers import create_test_interactive_user
from grievance_social_protection.apps import TicketConfig
from individual.models import Group
from location.models import UserDistrict
from location.test_helpers import create_test_village, assign_user_districts

from analytics.apps import AnalyticsConfig, DEFAULT_FIELD_ALLOWLIST
from analytics.services import QueryBuilderService
from analytics.tests.test_grievance_scope import (
    GrievanceFixture, HIDDEN_FLAG_READ, QUERY, SECRET_READ, TICKET_READ,
)
from analytics.tests.test_query_builder import _marker, _role_user, _run, _widen_allowlist


def _role(name, rights):
    role = Role.objects.create(
        name=name, is_system=0, is_blocked=False, audit_user_id=-1, validity_from=datetime.datetime.now(),
    )
    for right in rights:
        RoleRight.objects.create(
            role=role, right_id=int(right), audit_user_id=-1, validity_from=datetime.datetime.now()
        )
    return role


def _execute(entity, config, user):
    """QueryBuilderService.execute_query on the ORM path, through the result cache."""
    with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
        return QueryBuilderService.execute_query(entity, config, user).rows


class GrievanceResultCacheTest(GrievanceFixture):
    """A cached grievance result is not served once the rights or the grievance
    configuration that scoped it have changed."""

    def _count_config(self):
        return self._config(group_by=['category'], aggregations={'n': {'function': 'count', 'field': 'id'}})

    def test_a_withdrawn_category_right_is_applied_to_the_next_run(self):
        user = _role_user(f'an_cache_full_{self.marker}', [QUERY, TICKET_READ, SECRET_READ, HIDDEN_FLAG_READ])
        # Created before the first run: saving a role clears a local-memory cache.
        narrower = _role(f'an-cache-narrow-{self.marker}', [QUERY, TICKET_READ, HIDDEN_FLAG_READ])
        before = _execute('grievance', self._count_config(), user)
        self.assertEqual({row['category']: row['n'] for row in before}, {'public': 2, 'secret': 1})

        create_or_update_user_roles(user.i_user, [narrower.id], -1)

        after = _execute('grievance', self._count_config(), user)
        self.assertEqual({row['category']: row['n'] for row in after}, {'public': 2})

    def test_a_withdrawn_ticket_read_right_refuses_the_next_run(self):
        user = _role_user(f'an_cache_read_{self.marker}', [QUERY, TICKET_READ, SECRET_READ, HIDDEN_FLAG_READ])
        query_only = _role(f'an-cache-query-{self.marker}', [QUERY])
        _execute('grievance', self._count_config(), user)

        create_or_update_user_roles(user.i_user, [query_only.id], -1)

        with self.assertRaises(PermissionDenied):
            _execute('grievance', self._count_config(), user)

    def test_a_grievance_configuration_change_is_applied_to_the_next_run(self):
        user = _role_user(f'an_cache_cfg_{self.marker}', [QUERY, TICKET_READ, HIDDEN_FLAG_READ])
        before = _execute('grievance', self._count_config(), user)
        self.assertEqual({row['category']: row['n'] for row in before}, {'public': 2})

        with mock.patch.object(TicketConfig, 'processed_categories', {}):
            after = _execute('grievance', self._count_config(), user)
        self.assertEqual({row['category']: row['n'] for row in after}, {'public': 2, 'secret': 1})


class AllowlistResultCacheTest(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        self.marker = _marker()
        Group(code=self.marker, json_ext={}).save(user=self.admin)
        self.user = _role_user(f'an_cache_allow_{self.marker}', AnalyticsConfig.gql_analytics_query_perms)

    def test_a_field_removed_from_the_allowlist_is_refused_on_the_next_run(self):
        config = {
            'filters': {'code': {'operator': 'exact', 'value': self.marker}},
            'aggregations': {'n': {'function': 'count', 'field': 'id'}},
        }
        _widen_allowlist(self, group=['code'])
        self.assertEqual(_execute('group', config, self.user), [{'n': 1}])

        with mock.patch.object(AnalyticsConfig, 'analytics_field_allowlist', DEFAULT_FIELD_ALLOWLIST):
            with self.assertRaises(ValueError):
                _execute('group', config, self.user)


class DistrictResultCacheTest(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        self.marker = _marker()
        self.code_in, self.code_out = _marker()[:6], _marker()[:6]
        self.village_in = create_test_village({'code': self.code_in})
        self.village_out = create_test_village({'code': self.code_out})
        for village in (self.village_in, self.village_out):
            Group(code=self.marker, location=village, json_ext={}).save(user=self.admin)
        self.user = _role_user(f'an_cache_loc_{self.marker}', AnalyticsConfig.gql_analytics_query_perms)
        _widen_allowlist(self, group=['code'])
        assign_user_districts(self.user, [f'D-{self.code_in}'])
        cache.clear()

    @override_settings(ROW_SECURITY=True)
    def test_a_district_reassignment_is_applied_to_the_next_run(self):
        config = {
            'filters': {'code': {'operator': 'exact', 'value': self.marker}},
            'group_by': ['location_id'],
            'aggregations': {'n': {'function': 'count', 'field': 'id'}},
        }
        self.assertEqual(_execute('group', config, self.user), [{'location_id': self.village_in.id, 'n': 1}])

        for district in UserDistrict.objects.filter(user=self.user.i_user, validity_to__isnull=True):
            district.validity_to = datetime.datetime.now()
            district.save()
        assign_user_districts(self.user, [f'D-{self.code_out}'])

        self.assertEqual(_execute('group', config, self.user), [{'location_id': self.village_out.id, 'n': 1}])

    @override_settings(ROW_SECURITY=True)
    def test_an_unchanged_user_still_gets_the_cached_result(self):
        config = {
            'filters': {'code': {'operator': 'exact', 'value': self.marker}},
            'aggregations': {'n': {'function': 'count', 'field': 'id'}},
        }
        first = _execute('group', config, self.user)
        with mock.patch.object(QueryBuilderService, '_execute_orm_query') as orm:
            second = _execute('group', config, self.user)
        orm.assert_not_called()
        self.assertEqual(first, second)
