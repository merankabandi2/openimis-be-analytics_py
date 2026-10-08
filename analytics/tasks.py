"""
Analytics Celery tasks.
"""
import logging
import os
import uuid
from datetime import datetime

from celery import shared_task
from django.core.exceptions import PermissionDenied

logger = logging.getLogger(__name__)

# Front-end route of the export history, where a finished export is listed.
EXPORT_HISTORY_URL = '/analytics/export-history'
EXPORT_READY_EVENT = 'analytics.export_ready'
EXPORT_FAILED_EVENT = 'analytics.export_failed'


def queue_analytics_export(user, entity_type, query_config, export_format, query_id=None):
    """Queue the export of `query_config` on `entity_type` in `user`'s name.
    The caller has already refused what can be refused without running it."""
    export_analytics_data.delay(
        str(user.id), entity_type, query_config, export_format, str(query_id) if query_id else None,
    )


def build_export(user, entity_type, query_config, export_format, query_id=None):
    """Write the export file under MEDIA_ROOT and record it as an
    AnalyticsExport of `user`. Checks the export right with the user's current
    rights, runs the query under the analytics timeout and refuses more rows
    than `analytics_max_export_rows`."""
    from analytics.apps import AnalyticsConfig
    from analytics.models import AnalyticsExport
    from analytics.schema import _admitted
    from analytics.services import ExportService, QueryBuilderService

    if not (_admitted(user) and user.has_perms(AnalyticsConfig.gql_analytics_export_perms)):
        raise PermissionDenied("Unauthorized")
    max_rows = AnalyticsConfig.analytics_max_export_rows
    result = QueryBuilderService.execute_query(entity_type, query_config, user, max_rows=max_rows)
    if result.truncated:
        # Grouping cannot shorten a result that is already grouped.
        _, group_by, _, _, _ = QueryBuilderService._normalise_config(query_config)
        advice = "narrow the filters" if group_by else "add filters or grouping"
        raise ValueError(f"Export exceeds maximum rows ({max_rows}); {advice}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"analytics_{entity_type}_{timestamp}_{uuid.uuid4().hex[:8]}"
    filepath = ExportService.export(result.rows, filename, export_format)
    try:
        return AnalyticsExport.objects.create(
            query_id=query_id,
            export_format=export_format,
            filters_applied=query_config,
            row_count=len(result.rows),
            file_path=filepath,
            exported_by=user,
        )
    except Exception:
        os.remove(filepath)
        raise


@shared_task
def export_analytics_data(user_id, entity_type, query_config, export_format, query_id=None):
    """Build an analytics export for the user `user_id` and notify them: on
    success with a link to the export history, otherwise with the refusal.
    Returns the AnalyticsExport id, or None when the export is refused."""
    from core.models import User

    user = User.objects.get(id=user_id)
    try:
        record = build_export(user, entity_type, query_config, export_format, query_id)
    except (PermissionDenied, ValueError) as exc:
        logger.info("Analytics export refused for user %s: %s", user_id, exc)
        _notify(user, EXPORT_FAILED_EVENT, '', {'export_format': export_format.upper(), 'reason': str(exc)})
        return None
    except Exception:
        logger.exception("Analytics export failed for user %s", user_id)
        _notify(user, EXPORT_FAILED_EVENT, '', {'export_format': export_format.upper(), 'reason': 'internal error'})
        raise
    _notify(user, EXPORT_READY_EVENT, EXPORT_HISTORY_URL, {
        'export_format': export_format.upper(), 'row_count': record.row_count,
    }, entity=record)
    return str(record.id)


def _notify(user, event_code, entity_url, context, entity=None):
    """Notify the requester. actor=None so notify() does not skip them; a
    missing notification module or event type leaves the export unchanged."""
    try:
        from notification.services import NotificationService

        NotificationService.notify(
            event_code=event_code,
            actor=None,
            entity=entity,
            entity_url=entity_url,
            recipients=[user],
            context=context,
        )
    except Exception as exc:
        logger.warning("Failed to send %s notification: %s", event_code, exc)
