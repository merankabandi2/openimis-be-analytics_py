import base64
import json
import time
import uuid
from datetime import datetime as py_datetime

import graphene
from django.db import transaction
from django.db.models import Q
from graphene_django import DjangoObjectType
from django.core.exceptions import PermissionDenied

from core import ExtendedConnection
from core.schema import OrderedDjangoFilterConnectionField

from .models import AnalyticsQuery, AnalyticsDashboard, AnalyticsWidget, AnalyticsExport
from .services import QueryBuilderService, ExportService


def _check_perms(user, perms):
    if not user or not getattr(user, 'id', None) or not user.has_perms(perms):
        raise PermissionDenied("Unauthorized")


def _visible_to(qs, user):
    """Scope saved queries/dashboards to public records or the caller's own."""
    if user.is_superuser:
        return qs
    return qs.filter(Q(is_public=True) | Q(created_by=user))


def _can_edit_dashboard(user, dashboard):
    from analytics.apps import AnalyticsConfig
    if not user or not getattr(user, 'id', None):
        return False
    if not user.has_perms(AnalyticsConfig.gql_analytics_dashboard_create_perms):
        return False
    return user.is_superuser or dashboard.created_by_id == user.id


def _owns_query(user, query):
    return user.is_superuser or query.created_by_id == user.id


def _can_edit_query(user, query):
    """Whether update/delete of this saved query would be accepted for `user`."""
    from analytics.apps import AnalyticsConfig
    if not user or not getattr(user, 'id', None):
        return False
    if not user.has_perms(AnalyticsConfig.gql_analytics_query_update_perms):
        return False
    return _owns_query(user, query)


def _check_share_perms(user):
    from analytics.apps import AnalyticsConfig
    if not user.has_perms(AnalyticsConfig.gql_analytics_dashboard_share_perms):
        raise PermissionDenied("Making a query public requires the analytics share right")


def _normalise_entity_type(entity_type):
    """Graphene exposes Django choice fields as uppercase enums; the service layer
    uses the lowercase choice keys."""
    normalised = (entity_type or '').lower()
    if normalised not in QueryBuilderService.ENTITY_TYPES:
        raise ValueError(f"Unknown entity type: {entity_type}")
    return normalised


def _load_config(query_config):
    return json.loads(query_config) if isinstance(query_config, str) else query_config


def _query_result(entity_type, config, user):
    start = time.time()
    result = QueryBuilderService.execute_query(entity_type, config, user)
    return QueryResultType(
        data=result.rows,
        row_count=len(result.rows),
        truncated=result.truncated,
        execution_time=time.time() - start,
    )


def _resolve_pk(raw_id):
    """Accept either a plain UUID or a Relay global ID (base64 of `Type:UUID`).

    Graphene-Django's Relay node wrapper exposes the `id` field on objects as a base64
    string like `QW5hbHl0aWNzRGFzaGJvYXJkVHlwZTox...`; the UI sometimes forwards that
    straight into `analytics_dashboard(id)` lookups whose `.get(pk=...)` call expects the
    raw primary key. Normalise here so both shapes resolve.
    """
    if not isinstance(raw_id, str):
        return raw_id
    # If it already looks like a UUID, return it untouched.
    if len(raw_id) >= 32 and '-' in raw_id and not raw_id.endswith('='):
        return raw_id
    try:
        decoded = base64.b64decode(raw_id).decode('utf-8', errors='replace')
        if ':' in decoded:
            return decoded.rsplit(':', 1)[-1]
    except Exception:  # noqa: BLE001 — malformed ID will fail the subsequent .get()
        return raw_id
    return raw_id


class AnalyticsQueryType(DjangoObjectType):
    can_edit = graphene.Boolean()

    class Meta:
        model = AnalyticsQuery
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            'name': ['exact', 'icontains'],
            'entity_type': ['exact'],
            'is_public': ['exact'],
        }
        connection_class = ExtendedConnection

    def resolve_can_edit(self, info):
        return _can_edit_query(info.context.user, self)


class AnalyticsDashboardType(DjangoObjectType):
    can_edit = graphene.Boolean()

    class Meta:
        model = AnalyticsDashboard
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            'name': ['exact', 'icontains'],
            'is_public': ['exact'],
            'is_default': ['exact'],
        }
        connection_class = ExtendedConnection

    def resolve_can_edit(self, info):
        return _can_edit_dashboard(info.context.user, self)


class AnalyticsWidgetType(DjangoObjectType):
    class Meta:
        model = AnalyticsWidget
        interfaces = (graphene.relay.Node,)
        filter_fields = {}
        connection_class = ExtendedConnection


class AnalyticsExportType(DjangoObjectType):
    class Meta:
        model = AnalyticsExport
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            'export_format': ['exact'],
        }
        connection_class = ExtendedConnection


class EntityFieldType(graphene.ObjectType):
    name = graphene.String()
    type = graphene.String()
    label = graphene.String()
    filterable = graphene.Boolean()
    aggregatable = graphene.Boolean()


class QueryResultType(graphene.ObjectType):
    data = graphene.JSONString()
    row_count = graphene.Int()
    # True when more rows matched than the row limit returned.
    truncated = graphene.Boolean()
    execution_time = graphene.Float()


class Query(graphene.ObjectType):
    analytics_queries = OrderedDjangoFilterConnectionField(
        AnalyticsQueryType,
        orderBy=graphene.List(of_type=graphene.String),
    )
    analytics_query = graphene.Field(AnalyticsQueryType, id=graphene.ID())

    analytics_dashboards = OrderedDjangoFilterConnectionField(
        AnalyticsDashboardType,
        orderBy=graphene.List(of_type=graphene.String),
    )
    analytics_dashboard = graphene.Field(AnalyticsDashboardType, id=graphene.ID())

    execute_analytics_query = graphene.Field(
        QueryResultType,
        entity_type=graphene.String(required=True),
        query_config=graphene.JSONString(required=True),
    )

    execute_analytics_widget = graphene.Field(
        QueryResultType,
        widget_id=graphene.ID(required=True),
    )

    analytics_entity_fields = graphene.List(
        EntityFieldType,
        entity_type=graphene.String(required=True),
    )

    analytics_exports = OrderedDjangoFilterConnectionField(
        AnalyticsExportType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    def resolve_analytics_queries(self, info, **kwargs):
        from analytics.apps import AnalyticsConfig
        _check_perms(info.context.user, AnalyticsConfig.gql_analytics_query_perms)
        qs = AnalyticsQuery.objects.filter(validity_to__isnull=True)
        return _visible_to(qs, info.context.user)

    def resolve_analytics_query(self, info, id):
        from analytics.apps import AnalyticsConfig
        _check_perms(info.context.user, AnalyticsConfig.gql_analytics_query_perms)
        qs = _visible_to(AnalyticsQuery.objects.filter(validity_to__isnull=True), info.context.user)
        return qs.get(pk=_resolve_pk(id))

    def resolve_analytics_dashboards(self, info, **kwargs):
        from analytics.apps import AnalyticsConfig
        _check_perms(info.context.user, AnalyticsConfig.gql_analytics_dashboards_perms)
        qs = AnalyticsDashboard.objects.filter(validity_to__isnull=True)
        return _visible_to(qs, info.context.user)

    def resolve_analytics_dashboard(self, info, id):
        from analytics.apps import AnalyticsConfig
        _check_perms(info.context.user, AnalyticsConfig.gql_analytics_dashboards_perms)
        qs = _visible_to(AnalyticsDashboard.objects.all(), info.context.user)
        return qs.get(pk=_resolve_pk(id))

    def resolve_execute_analytics_query(self, info, entity_type, query_config):
        from analytics.apps import AnalyticsConfig
        _check_perms(info.context.user, AnalyticsConfig.gql_analytics_query_perms)
        return _query_result(
            _normalise_entity_type(entity_type), _load_config(query_config), info.context.user
        )

    def resolve_execute_analytics_widget(self, info, widget_id):
        """Run a dashboard widget's saved query for a dashboard viewer.

        Viewing a dashboard needs only the dashboards right; the widget's query
        still runs with the viewer's data scoping.
        """
        from analytics.apps import AnalyticsConfig
        user = info.context.user
        _check_perms(user, AnalyticsConfig.gql_analytics_dashboards_perms)
        widget = AnalyticsWidget.objects.select_related('query').get(
            pk=_resolve_pk(widget_id), validity_to__isnull=True
        )
        dashboards = _visible_to(AnalyticsDashboard.objects.filter(validity_to__isnull=True), user)
        if not dashboards.filter(pk=widget.dashboard_id).exists():
            raise PermissionDenied("This dashboard is not visible to you")
        if widget.query.validity_to is not None:
            raise ValueError("The query of this widget has been deleted")
        return _query_result(
            _normalise_entity_type(widget.query.entity_type), widget.query.query_config, user
        )

    def resolve_analytics_entity_fields(self, info, entity_type):
        from analytics.apps import AnalyticsConfig
        _check_perms(info.context.user, AnalyticsConfig.gql_analytics_query_perms)
        normalised_entity = (entity_type or '').lower()
        fields = QueryBuilderService.get_entity_fields(normalised_entity)
        return [EntityFieldType(**f) for f in fields]

    def resolve_analytics_exports(self, info, **kwargs):
        from analytics.apps import AnalyticsConfig
        _check_perms(info.context.user, AnalyticsConfig.gql_analytics_export_perms)
        qs = AnalyticsExport.objects.all()
        if not info.context.user.is_superuser:
            qs = qs.filter(exported_by=info.context.user)
        return qs


# ── Mutations ─────────────────────────────────────────────────────

class AnalyticsQueryInput(graphene.InputObjectType):
    name = graphene.String(required=True)
    description = graphene.String()
    entity_type = graphene.String(required=True)
    query_config = graphene.JSONString(required=True)
    is_public = graphene.Boolean()


class CreateAnalyticsQueryMutation(graphene.Mutation):
    class Arguments:
        input = AnalyticsQueryInput(required=True)

    query = graphene.Field(AnalyticsQueryType)

    def mutate(self, info, input):
        from analytics.apps import AnalyticsConfig
        user = info.context.user
        _check_perms(user, AnalyticsConfig.gql_analytics_query_create_perms)
        if input.is_public:
            _check_share_perms(user)
        obj = AnalyticsQuery.objects.create(
            name=input.name,
            description=input.description,
            entity_type=_normalise_entity_type(input.entity_type),
            query_config=_load_config(input.query_config),
            is_public=input.is_public or False,
            created_by=user,
        )
        return CreateAnalyticsQueryMutation(query=obj)


class UpdateAnalyticsQueryMutation(graphene.Mutation):
    class Arguments:
        id = graphene.ID(required=True)
        input = AnalyticsQueryInput(required=True)

    query = graphene.Field(AnalyticsQueryType)

    def mutate(self, info, id, input):
        from analytics.apps import AnalyticsConfig
        user = info.context.user
        _check_perms(user, AnalyticsConfig.gql_analytics_query_update_perms)
        obj = AnalyticsQuery.objects.get(pk=_resolve_pk(id), validity_to__isnull=True)
        if not _owns_query(user, obj):
            raise PermissionDenied("You can only edit your own queries")
        if input.is_public and not obj.is_public:
            _check_share_perms(user)
        obj.name = input.name
        obj.description = input.description
        obj.entity_type = _normalise_entity_type(input.entity_type)
        obj.query_config = _load_config(input.query_config)
        if input.is_public is not None:
            obj.is_public = input.is_public
        obj.save()
        return UpdateAnalyticsQueryMutation(query=obj)


class DeleteAnalyticsQueryMutation(graphene.Mutation):
    """Retire a saved query (sets validity_to). Uses the query update right and
    the same ownership rule as UpdateAnalyticsQueryMutation."""

    class Arguments:
        id = graphene.ID(required=True)

    success = graphene.Boolean()

    def mutate(self, info, id):
        from analytics.apps import AnalyticsConfig
        user = info.context.user
        _check_perms(user, AnalyticsConfig.gql_analytics_query_update_perms)
        obj = AnalyticsQuery.objects.get(pk=_resolve_pk(id), validity_to__isnull=True)
        if not _owns_query(user, obj):
            raise PermissionDenied("You can only delete your own queries")
        if AnalyticsWidget.objects.filter(query=obj, validity_to__isnull=True).exists():
            raise ValueError("This query is used by a dashboard widget and cannot be deleted")
        obj.validity_to = py_datetime.now()
        obj.save()
        return DeleteAnalyticsQueryMutation(success=True)


class UpdateAnalyticsDashboardLayoutMutation(graphene.Mutation):
    """Store widget positions of a dashboard. `positions` maps widget ids to
    {x, y, w, h} grid coordinates."""

    class Arguments:
        dashboard_id = graphene.ID(required=True)
        positions = graphene.JSONString(required=True)

    dashboard = graphene.Field(AnalyticsDashboardType)

    def mutate(self, info, dashboard_id, positions):
        from analytics.apps import AnalyticsConfig
        user = info.context.user
        _check_perms(user, AnalyticsConfig.gql_analytics_dashboard_create_perms)
        dashboard = AnalyticsDashboard.objects.get(pk=_resolve_pk(dashboard_id), validity_to__isnull=True)
        if not _can_edit_dashboard(user, dashboard):
            raise PermissionDenied("You can only change the layout of your own dashboards")
        positions = _load_config(positions)
        if not isinstance(positions, dict):
            raise ValueError("positions must map widget ids to {x, y, w, h}")
        with transaction.atomic():
            for raw_id, position in positions.items():
                try:
                    clean = {key: int(position[key]) for key in ('x', 'y', 'w', 'h')}
                except (KeyError, TypeError, ValueError):
                    raise ValueError(f"Invalid position for widget {raw_id}")
                if clean['x'] < 0 or clean['y'] < 0 or clean['w'] < 1 or clean['h'] < 1:
                    raise ValueError(f"Invalid position for widget {raw_id}")
                updated = AnalyticsWidget.objects.filter(
                    pk=_resolve_pk(raw_id), dashboard=dashboard, validity_to__isnull=True
                ).update(position=clean)
                if not updated:
                    raise ValueError(f"Widget {raw_id} is not on this dashboard")
        return UpdateAnalyticsDashboardLayoutMutation(dashboard=dashboard)


class ExportAnalyticsDataMutation(graphene.Mutation):
    class Arguments:
        entity_type = graphene.String(required=True)
        query_config = graphene.JSONString(required=True)
        export_format = graphene.String(required=True)
        query_id = graphene.ID()

    export_url = graphene.String()
    export_id = graphene.ID()
    row_count = graphene.Int()

    def mutate(self, info, entity_type, query_config, export_format, query_id=None):
        from analytics.apps import AnalyticsConfig

        user = info.context.user
        _check_perms(user, AnalyticsConfig.gql_analytics_export_perms)
        entity_type = _normalise_entity_type(entity_type)
        config = _load_config(query_config)
        # The export covers every matching row up to the export cap, not the
        # on-screen row limit.
        max_rows = AnalyticsConfig.analytics_max_export_rows
        result = QueryBuilderService.execute_query(entity_type, config, user, max_rows=max_rows)
        if result.truncated:
            raise ValueError(
                f"Export exceeds maximum rows ({max_rows}); add filters or grouping"
            )
        results = result.rows

        timestamp = py_datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"analytics_{entity_type}_{timestamp}_{uuid.uuid4().hex[:8]}"

        filepath = ExportService.export(results, filename, export_format)

        record = AnalyticsExport.objects.create(
            query_id=_resolve_pk(query_id) if query_id else None,
            export_format=export_format,
            filters_applied=config,
            row_count=len(results),
            file_path=filepath,
            exported_by=user,
        )
        return ExportAnalyticsDataMutation(
            export_url=f"/api/analytics/download/{record.id}/",
            export_id=record.id,
            row_count=len(results),
        )


class Mutation(graphene.ObjectType):
    create_analytics_query = CreateAnalyticsQueryMutation.Field()
    update_analytics_query = UpdateAnalyticsQueryMutation.Field()
    delete_analytics_query = DeleteAnalyticsQueryMutation.Field()
    update_analytics_dashboard_layout = UpdateAnalyticsDashboardLayoutMutation.Field()
    export_analytics_data = ExportAnalyticsDataMutation.Field()
