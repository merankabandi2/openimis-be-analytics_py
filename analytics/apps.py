from django.apps import AppConfig

MODULE_NAME = "analytics"

DEFAULT_CFG = {
    "analytics_max_export_rows": 100000,
    "analytics_max_query_rows": 10000,
    "analytics_cache_ttl": 300,  # 5 minutes
    "analytics_enable_sql_queries": False,
    "gql_analytics_dashboards_perms": ["200001"],
    "gql_analytics_query_perms": ["200002"],
    "gql_analytics_export_perms": ["200003"],
    "gql_analytics_dashboard_create_perms": ["200004"],
    "gql_analytics_dashboard_share_perms": ["200005"],
}


class AnalyticsConfig(AppConfig):
    name = MODULE_NAME

    # Analytics permissions
    gql_analytics_dashboards_perms = DEFAULT_CFG["gql_analytics_dashboards_perms"]
    gql_analytics_query_perms = DEFAULT_CFG["gql_analytics_query_perms"]
    gql_analytics_export_perms = DEFAULT_CFG["gql_analytics_export_perms"]
    gql_analytics_dashboard_create_perms = DEFAULT_CFG["gql_analytics_dashboard_create_perms"]
    gql_analytics_dashboard_share_perms = DEFAULT_CFG["gql_analytics_dashboard_share_perms"]

    # Configuration
    analytics_max_export_rows = DEFAULT_CFG["analytics_max_export_rows"]
    analytics_max_query_rows = DEFAULT_CFG["analytics_max_query_rows"]
    analytics_cache_ttl = DEFAULT_CFG["analytics_cache_ttl"]
    analytics_enable_sql_queries = DEFAULT_CFG["analytics_enable_sql_queries"]

    def ready(self):
        from core.models import ModuleConfiguration
        cfg = ModuleConfiguration.get_or_default(MODULE_NAME, DEFAULT_CFG)
        self.__load_config(cfg)
        try:
            from analytics.services import DashboardService
            DashboardService.create_default_dashboards()
        except Exception:
            pass  # Ignore on startup if DB not ready

    @classmethod
    def __load_config(cls, cfg):
        """
        Load all config fields that match current AppConfig class fields
        """
        for field in cfg:
            if hasattr(AnalyticsConfig, field):
                setattr(AnalyticsConfig, field, cfg[field])