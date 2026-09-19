import uuid
from django.db import models
from django.conf import settings
from core import models as core_models


class AnalyticsQuery(core_models.VersionedModel):
    """
    Saved analytics queries
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    entity_type = models.CharField(
        max_length=50,
        choices=[
            ('individual', 'Individual'),
            ('group', 'Group'),
            ('beneficiary', 'Beneficiary'),
            ('payment', 'Payment'),
            ('grievance', 'Grievance'),
        ]
    )
    query_config = models.JSONField(default=dict)  # Stores filters, fields, aggregations
    is_public = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING, related_name='analytics_queries')
    
    class Meta:
        db_table = 'analytics_query'
        permissions = [
            ('view_analytics_query', 'Can view analytics queries'),
            ('execute_analytics_query', 'Can execute analytics queries'),
        ]


class AnalyticsDashboard(core_models.VersionedModel):
    """
    Analytics dashboards containing multiple widgets
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    layout_config = models.JSONField(default=dict)  # Dashboard layout configuration
    is_public = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING, related_name='analytics_dashboards')
    
    class Meta:
        db_table = 'analytics_dashboard'


class AnalyticsWidget(core_models.VersionedModel):
    """
    Individual widgets on a dashboard
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dashboard = models.ForeignKey(AnalyticsDashboard, on_delete=models.CASCADE, related_name='widgets')
    query = models.ForeignKey(AnalyticsQuery, on_delete=models.CASCADE)
    widget_type = models.CharField(
        max_length=50,
        choices=[
            ('bar_chart', 'Bar Chart'),
            ('line_chart', 'Line Chart'),
            ('pie_chart', 'Pie Chart'),
            ('table', 'Table'),
            ('metric', 'Single Metric'),
            ('map', 'Geographic Map'),
        ]
    )
    title = models.CharField(max_length=255)
    config = models.JSONField(default=dict)  # Widget-specific configuration
    position = models.JSONField(default=dict)  # x, y, width, height
    
    class Meta:
        db_table = 'analytics_widget'
        ordering = ['dashboard', 'id']


class AnalyticsExport(models.Model):
    """
    Track export history
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    query = models.ForeignKey(AnalyticsQuery, on_delete=models.SET_NULL, null=True)
    export_format = models.CharField(
        max_length=10,
        choices=[
            ('excel', 'Excel'),
            ('csv', 'CSV'),
        ]
    )
    filters_applied = models.JSONField(default=dict)
    row_count = models.IntegerField()
    file_path = models.CharField(max_length=500, blank=True, null=True)
    exported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING)
    exported_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'analytics_export'
        ordering = ['-exported_at']