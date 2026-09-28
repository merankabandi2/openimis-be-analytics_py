"""The analytics right codes are distinct from the rights of every other
installed module.

A role right is a bare integer, so a code shared with another module grants
both modules' features: the role screen moves the other module's right along
with the analytics one, and ``has_perms`` accepts either.
"""
import importlib

from django.apps import apps
from django.test import SimpleTestCase

from analytics.apps import AnalyticsConfig, DEFAULT_CFG

ANALYTICS_PERMS = sorted(key for key in DEFAULT_CFG if key.endswith('_perms'))


def _codes(value):
    if isinstance(value, (list, tuple, set)):
        return {int(code) for code in value if str(code).isdigit()}
    if isinstance(value, (int, str)) and str(value).isdigit():
        return {int(value)}
    return set()


def _declared_rights(app_config):
    """Codes an app declares on its AppConfig (``*_perms``) and in its
    ``gql_config`` module (``*_PERMS``)."""
    codes = set()
    for name in dir(type(app_config)):
        if name.endswith('_perms'):
            codes |= _codes(getattr(type(app_config), name, None))
    try:
        gql_config = importlib.import_module(f'{app_config.name}.gql_config')
    except ImportError:
        gql_config = None
    if gql_config is not None:
        for name in dir(gql_config):
            if name.endswith('_PERMS'):
                codes |= _codes(getattr(gql_config, name))
    return codes


def _analytics_codes():
    return {attr: _codes(getattr(AnalyticsConfig, attr)) for attr in ANALYTICS_PERMS}


class AnalyticsRightCodesTest(SimpleTestCase):

    def test_each_analytics_right_has_its_own_code(self):
        codes = [code for attr_codes in _analytics_codes().values() for code in attr_codes]
        self.assertEqual(len(codes), len(ANALYTICS_PERMS))
        self.assertEqual(len(set(codes)), len(codes))

    def test_payment_cycle_rights_are_not_analytics_rights(self):
        payment_cycle = _declared_rights(apps.get_app_config('payment_cycle'))
        self.assertTrue(payment_cycle)
        shared = {attr: sorted(codes & payment_cycle)
                  for attr, codes in _analytics_codes().items() if codes & payment_cycle}
        self.assertEqual(shared, {})

    def test_no_installed_module_declares_an_analytics_code(self):
        analytics = set().union(*_analytics_codes().values())
        shared = {}
        for app_config in apps.get_app_configs():
            if app_config.name == 'analytics':
                continue
            overlap = _declared_rights(app_config) & analytics
            if overlap:
                shared[app_config.name] = sorted(overlap)
        self.assertEqual(shared, {})
