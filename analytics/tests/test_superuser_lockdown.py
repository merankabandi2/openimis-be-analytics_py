from unittest import mock

from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase

from analytics.apps import AnalyticsConfig, DEFAULT_CFG
from analytics.schema import Query, _check_perms, _holds


def _user(superuser):
    user = mock.Mock(id=1, is_anonymous=False, is_authenticated=True, is_superuser=superuser)
    user.has_perms.return_value = True
    return user


def _info(user):
    info = mock.Mock()
    info.context.user = user
    return info


class SuperuserLockdownTest(SimpleTestCase):
    """With analytics_superuser_only on, holding every analytics right is not enough."""

    def setUp(self):
        patcher = mock.patch.object(AnalyticsConfig, 'analytics_superuser_only', True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_lockdown_is_on_by_default(self):
        self.assertIs(DEFAULT_CFG['analytics_superuser_only'], True)

    def test_a_non_superuser_holding_the_rights_is_refused(self):
        user = _user(superuser=False)
        with self.assertRaises(PermissionDenied):
            _check_perms(user, AnalyticsConfig.gql_analytics_query_perms)
        self.assertFalse(_holds(user, AnalyticsConfig.gql_analytics_dashboards_perms))

    def test_query_resolvers_refuse_a_non_superuser(self):
        info = _info(_user(superuser=False))
        with self.assertRaises(PermissionDenied):
            Query.resolve_analytics_queries(None, info)
        with self.assertRaises(PermissionDenied):
            Query.resolve_execute_analytics_query(None, info, 'individual', '{}')

    def test_a_superuser_holding_the_rights_is_admitted(self):
        user = _user(superuser=True)
        _check_perms(user, AnalyticsConfig.gql_analytics_query_perms)
        self.assertTrue(_holds(user, AnalyticsConfig.gql_analytics_dashboards_perms))

    def test_switching_the_lockdown_off_admits_rights_holders(self):
        with mock.patch.object(AnalyticsConfig, 'analytics_superuser_only', False):
            _check_perms(_user(superuser=False), AnalyticsConfig.gql_analytics_query_perms)
