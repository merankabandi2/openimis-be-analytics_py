import os
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse, Http404
from django.contrib.auth.decorators import login_required, permission_required
from django.views.decorators.http import require_http_methods
from .apps import AnalyticsConfig
from .models import AnalyticsExport
from .services import QueryBuilderService


@login_required
def download_export(request, export_id):
    """
    Download an exported file
    """
    # The module's numeric right code (RoleRight), same check as resolve_analytics_exports
    # in schema.py — Django's declared-permission `permission_required` has no matching
    # permission for AnalyticsExport, so it can never be satisfied.
    if not request.user.has_perms(AnalyticsConfig.gql_analytics_export_perms):
        raise PermissionDenied("Unauthorized")
    try:
        export_record = AnalyticsExport.objects.get(pk=export_id)

        # Check permissions
        if export_record.exported_by != request.user and not request.user.is_superuser:
            raise Http404("Export not found")
        
        if not os.path.exists(export_record.file_path):
            raise Http404("Export file not found")
        
        # Determine content type
        content_types = {
            'excel': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'csv': 'text/csv',
            'pdf': 'application/pdf',
        }
        content_type = content_types.get(export_record.export_format, 'application/octet-stream')
        
        # Read file and return response
        with open(export_record.file_path, 'rb') as f:
            response = HttpResponse(f.read(), content_type=content_type)
            filename = os.path.basename(export_record.file_path)
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
            
    except AnalyticsExport.DoesNotExist:
        raise Http404("Export not found")


@login_required
@permission_required('analytics.view_analytics_query')
@require_http_methods(["GET"])
def get_entity_schema(request, entity_type):
    """
    Get the schema/fields for an entity type
    """
    try:
        fields = QueryBuilderService.get_entity_fields(entity_type)
        return JsonResponse({
            'entity_type': entity_type,
            'fields': fields
        })
    except Exception as e:
        return JsonResponse({
            'error': str(e)
        }, status=400)