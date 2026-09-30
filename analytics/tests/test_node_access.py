"""node(id:) and connections on the analytics types apply the rights and the
owner rules of the analytics list queries."""
import base64
import datetime

import graphene
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, TestCase

from analytics.apps import AnalyticsConfig
from analytics.models import AnalyticsDashboard, AnalyticsExport, AnalyticsQuery, AnalyticsWidget
from analytics.schema import Query
from analytics.tests.test_query_builder import _marker, _role_user

VIEW, QUERY, EXPORT = (
    int(getattr(AnalyticsConfig, attr)[0])
    for attr in ('gql_analytics_dashboards_perms', 'gql_analytics_query_perms', 'gql_analytics_export_perms')
)


class NodeQuery(Query, graphene.ObjectType):
    node = graphene.relay.Node.Field()


SCHEMA = graphene.Schema(query=NodeQuery)


def _gid(type_name, pk):
    return base64.b64encode(f'{type_name}:{pk}'.encode()).decode()


class NodeAccessTest(TestCase):
    def setUp(self):
        cache.clear()
        marker = _marker()
        self.owner = _role_user(f'an_node_owner_{marker}', [VIEW, QUERY, EXPORT])
        self.other = _role_user(f'an_node_other_{marker}', [VIEW, QUERY, EXPORT])
        self.viewer = _role_user(f'an_node_viewer_{marker}', [VIEW])
        self.no_rights = _role_user(f'an_node_none_{marker}', [])
        self.private_query = AnalyticsQuery.objects.create(
            name='private', entity_type='grievance', created_by=self.owner, is_public=False,
            query_config={'filters': {'category': 'violence_vbg'}, 'aggregations': {'n': {'function': 'count'}}},
        )
        self.export = AnalyticsExport.objects.create(
            query=self.private_query, export_format='csv', row_count=7, file_path='/nonexistent/export.csv',
            filters_applied={'filters': {'category': 'violence_vbg'}}, exported_by=self.owner,
        )
        self.private_dashboard = AnalyticsDashboard.objects.create(name='private', created_by=self.owner)
        self.public_dashboard = AnalyticsDashboard.objects.create(name='public', created_by=self.owner, is_public=True)
        self.private_widget = AnalyticsWidget.objects.create(
            dashboard=self.private_dashboard, query=self.private_query, widget_type='metric', title='W',
            config={'a': 1}, position={'x': 0, 'y': 0, 'w': 4, 'h': 4},
        )
        self.public_widget = AnalyticsWidget.objects.create(
            dashboard=self.public_dashboard, query=self.private_query, widget_type='metric', title='P',
            config={'a': 1}, position={'x': 0, 'y': 0, 'w': 4, 'h': 4},
        )
        self.public_query = AnalyticsQuery.objects.create(
            name='public', entity_type='individual', created_by=self.owner, is_public=True,
            query_config={'aggregations': {'n': {'function': 'count'}}},
        )

    def _node(self, user, type_name, pk, selection='id'):
        request = RequestFactory().post('/graphql')
        request.user = user
        result = SCHEMA.execute(
            '{ node(id: "%s") { ... on %s { %s } } }' % (_gid(type_name, pk), type_name, selection),
            context_value=request,
        )
        self.assertIsNone(result.errors, result.errors)
        return result.data['node']

    def _objects(self):
        return (
            ('AnalyticsQueryType', self.private_query.id, 'name queryConfig'),
            ('AnalyticsExportType', self.export.id, 'rowCount filtersApplied filePath'),
            ('AnalyticsDashboardType', self.private_dashboard.id, 'name'),
            ('AnalyticsWidgetType', self.private_widget.id, 'title'),
        )

    def test_anonymous_caller_gets_nothing(self):
        for type_name, pk, selection in self._objects():
            with self.subTest(type=type_name):
                self.assertIsNone(self._node(AnonymousUser(), type_name, pk, selection))

    def test_another_user_gets_nothing_of_the_owners_private_records(self):
        for type_name, pk, selection in self._objects():
            with self.subTest(type=type_name):
                self.assertIsNone(self._node(self.other, type_name, pk, selection))

    def test_owner_gets_their_records(self):
        for type_name, pk, selection in self._objects():
            with self.subTest(type=type_name):
                self.assertIsNotNone(self._node(self.owner, type_name, pk, selection))
        self.assertEqual(self._node(self.owner, 'AnalyticsExportType', self.export.id, 'rowCount')['rowCount'], 7)

    def test_dashboard_viewer_gets_the_widgets_of_a_public_dashboard(self):
        self.assertIsNotNone(self._node(self.viewer, 'AnalyticsWidgetType', self.public_widget.id, 'title'))
        request = RequestFactory().post('/graphql')
        request.user = self.viewer
        result = SCHEMA.execute(
            '{ analyticsDashboard(id: "%s") { widgets { edges { node { title query { name } } } } } }'
            % self.public_dashboard.id,
            context_value=request,
        )
        self.assertIsNone(result.errors, result.errors)
        edges = result.data['analyticsDashboard']['widgets']['edges']
        self.assertEqual([(e['node']['title'], e['node']['query']['name']) for e in edges], [('P', 'private')])

    def _public_objects(self):
        return (
            ('AnalyticsQueryType', self.public_query.id, 'name'),
            ('AnalyticsDashboardType', self.public_dashboard.id, 'name'),
            ('AnalyticsWidgetType', self.public_widget.id, 'title'),
        )

    def test_another_rights_holder_gets_public_records(self):
        for type_name, pk, selection in self._public_objects():
            with self.subTest(type=type_name):
                self.assertIsNotNone(self._node(self.other, type_name, pk, selection))

    def test_user_without_the_analytics_rights_gets_no_public_record(self):
        for type_name, pk, selection in self._public_objects():
            with self.subTest(type=type_name):
                self.assertIsNone(self._node(self.no_rights, type_name, pk, selection))

    def test_dashboard_viewer_without_the_query_right_gets_no_export(self):
        self.assertIsNone(self._node(self.viewer, 'AnalyticsExportType', self.export.id, 'rowCount'))

    def _retire(self, *records):
        for record in records:
            type(record).objects.filter(pk=record.pk).update(validity_to=datetime.datetime.now())

    def test_retired_public_query_and_dashboard_are_not_returned(self):
        self._retire(self.public_query, self.public_dashboard)
        for user in (self.owner, self.other):
            for type_name, pk, selection in (
                ('AnalyticsQueryType', self.public_query.id, 'name'),
                ('AnalyticsDashboardType', self.public_dashboard.id, 'name'),
            ):
                with self.subTest(user=user.username, type=type_name):
                    self.assertIsNone(self._node(user, type_name, pk, selection))

    def test_retired_widget_is_not_returned(self):
        self._retire(self.public_widget)
        for user in (self.owner, self.other):
            with self.subTest(user=user.username):
                self.assertIsNone(self._node(user, 'AnalyticsWidgetType', self.public_widget.id, 'title'))

    def test_widget_of_a_retired_dashboard_is_not_returned(self):
        self._retire(self.public_dashboard)
        for user in (self.owner, self.other):
            with self.subTest(user=user.username):
                self.assertIsNone(self._node(user, 'AnalyticsWidgetType', self.public_widget.id, 'title'))
