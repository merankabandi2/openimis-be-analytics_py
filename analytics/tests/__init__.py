# The tests of this package exercise the analytics rights model, which applies
# when analytics_superuser_only is off; test_superuser_lockdown turns it back on.
from analytics.apps import AnalyticsConfig

AnalyticsConfig.analytics_superuser_only = False
