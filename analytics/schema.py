import base64
import json
import time
import graphene
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
    class Meta:
        model = AnalyticsQuery
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            'name': ['exact', 'icontains'],
            'entity_type': ['exact'],
            'is_public': ['exact'],
        }
        connection_class = ExtendedConnection


class AnalyticsDashboardType(DjangoObjectType):
    class Meta:
        model = AnalyticsDashboard
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            'name': ['exact', 'icontains'],
            'is_public': ['exact'],
            'is_default': ['exact'],
        }
        connection_class = ExtendedConnection


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
        qs = _visible_to(AnalyticsQuery.objects.all(), info.context.user)
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
        start = time.time()
        config = json.loads(query_config) if isinstance(query_config, str) else query_config
        # Graphene auto-uppercases Django choice fields when exposing them as enums; the
        # service layer stores and expects lowercase keys ("beneficiary", "payment", ...).
        # Normalise here so both UI (lowercase form state) and saved-query execution
        # (enum value from GraphQL) hit the same code path.
        normalised_entity = (entity_type or '').lower()
        results = QueryBuilderService.execute_query(normalised_entity, config)
        return QueryResultType(
            data=results,
            row_count=len(results),
            execution_time=time.time() - start,
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
        _check_perms(info.context.user, AnalyticsConfig.gql_analytics_query_create_perms)
        obj = AnalyticsQuery.objects.create(
            name=input.name,
            description=input.description,
            entity_type=input.entity_type,
            query_config=json.loads(input.query_config) if isinstance(input.query_config, str) else input.query_config,
            is_public=input.is_public or False,
            created_by=info.context.user,
        )
        return CreateAnalyticsQueryMutation(query=obj)


class UpdateAnalyticsQueryMutation(graphene.Mutation):
    class Arguments:
        id = graphene.ID(required=True)
        input = AnalyticsQueryInput(required=True)

    query = graphene.Field(AnalyticsQueryType)

    def mutate(self, info, id, input):
        from analytics.apps import AnalyticsConfig
        _check_perms(info.context.user, AnalyticsConfig.gql_analytics_query_update_perms)
        obj = AnalyticsQuery.objects.get(pk=id)
        if obj.created_by != info.context.user and not info.context.user.is_superuser:
            raise PermissionDenied("You can only edit your own queries")
        obj.name = input.name
        obj.description = input.description
        obj.entity_type = input.entity_type
        obj.query_config = json.loads(input.query_config) if isinstance(input.query_config, str) else input.query_config
        if input.is_public is not None:
            obj.is_public = input.is_public
        obj.save()
        return UpdateAnalyticsQueryMutation(query=obj)


class ExportAnalyticsDataMutation(graphene.Mutation):
    class Arguments:
        entity_type = graphene.String(required=True)
        query_config = graphene.JSONString(required=True)
        export_format = graphene.String(required=True)
        query_id = graphene.ID()

    export_url = graphene.String()
    export_id = graphene.ID()

    def mutate(self, info, entity_type, query_config, export_format, query_id=None):
        from datetime import datetime
        from analytics.apps import AnalyticsConfig

        _check_perms(info.context.user, AnalyticsConfig.gql_analytics_export_perms)
        config = json.loads(query_config) if isinstance(query_config, str) else query_config
        results = QueryBuilderService.execute_query(entity_type, config)

        if len(results) > AnalyticsConfig.analytics_max_export_rows:
            raise Exception(f"Export exceeds maximum rows ({AnalyticsConfig.analytics_max_export_rows})")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"analytics_{entity_type}_{timestamp}"

        filepath = ExportService.export(results, filename, export_format)

        record = AnalyticsExport.objects.create(
            query_id=query_id,
            export_format=export_format,
            filters_applied=config,
            row_count=len(results),
            file_path=filepath,
            exported_by=info.context.user,
        )
        return ExportAnalyticsDataMutation(
            export_url=f"/api/analytics/download/{record.id}/",
            export_id=record.id,
        )


class Mutation(graphene.ObjectType):
    create_analytics_query = CreateAnalyticsQueryMutation.Field()
    update_analytics_query = UpdateAnalyticsQueryMutation.Field()
    export_analytics_data = ExportAnalyticsDataMutation.Field()
