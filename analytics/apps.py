from django.apps import AppConfig

MODULE_NAME = "analytics"

DEFAULT_CFG = {
    "analytics_max_export_rows": 20000,
    "analytics_max_query_rows": 10000,
    "analytics_cache_ttl": 300,  # 5 minutes
    # Seconds a database statement of an analytics query may run; 0 keeps the
    # connection's statement_timeout.
    "analytics_query_timeout": 30,
    "gql_analytics_dashboards_perms": ["803001"],
    "gql_analytics_query_perms": ["803002"],
    "gql_analytics_export_perms": ["803003"],
    "gql_analytics_dashboard_create_perms": ["803004"],
    "gql_analytics_dashboard_share_perms": ["803005"],
    "gql_analytics_query_create_perms": ["803006"],
    "gql_analytics_query_update_perms": ["803007"],
}


class AnalyticsConfig(AppConfig):
    name = MODULE_NAME

    # Analytics permissions
    gql_analytics_dashboards_perms = DEFAULT_CFG["gql_analytics_dashboards_perms"]
    gql_analytics_query_perms = DEFAULT_CFG["gql_analytics_query_perms"]
    gql_analytics_export_perms = DEFAULT_CFG["gql_analytics_export_perms"]
    gql_analytics_dashboard_create_perms = DEFAULT_CFG["gql_analytics_dashboard_create_perms"]
    gql_analytics_dashboard_share_perms = DEFAULT_CFG["gql_analytics_dashboard_share_perms"]
    gql_analytics_query_create_perms = DEFAULT_CFG["gql_analytics_query_create_perms"]
    gql_analytics_query_update_perms = DEFAULT_CFG["gql_analytics_query_update_perms"]

    # Configuration
    analytics_max_export_rows = DEFAULT_CFG["analytics_max_export_rows"]
    analytics_max_query_rows = DEFAULT_CFG["analytics_max_query_rows"]
    analytics_cache_ttl = DEFAULT_CFG["analytics_cache_ttl"]
    analytics_query_timeout = DEFAULT_CFG["analytics_query_timeout"]

    def ready(self):
        from core.models import ModuleConfiguration
        cfg = ModuleConfiguration.get_or_default(MODULE_NAME, DEFAULT_CFG)
        self.__load_config(cfg)

    @classmethod
    def __load_config(cls, cfg):
        """
        Load all config fields that match current AppConfig class fields
        """
        for field in cfg:
            if hasattr(AnalyticsConfig, field):
                setattr(AnalyticsConfig, field, cfg[field])