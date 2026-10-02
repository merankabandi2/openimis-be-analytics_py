"""Per-entity field allowlist: the columns a non-superuser may reference in
fields, filters, group_by, order_by and aggregations, and the columns the
field list offers them. Superusers are not restricted."""
import datetime
import os
import uuid
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings

from core.test_helpers import create_test_interactive_user
from grievance_social_protection.apps import TicketConfig
from individual.models import Individual
from payroll.models import BenefitConsumption

from analytics.apps import AnalyticsConfig
from analytics.models import AnalyticsDashboard, AnalyticsExport, AnalyticsQuery, AnalyticsWidget
from analytics.schema import ExportAnalyticsDataMutation, Query
from analytics.services import QueryBuilderService
from analytics.tests.test_permissions import _info
from analytics.tests.test_query_builder import _marker, _role_user, _run

VIEW, QUERY, EXPORT = (
    int(getattr(AnalyticsConfig, attr)[0]) for attr in (
        'gql_analytics_dashboards_perms', 'gql_analytics_query_perms', 'gql_analytics_export_perms',
    )
)
TICKET_READ = int(TicketConfig.gql_query_tickets_perms[0])

EXCLUDED = {
    'individual': ('first_name', 'last_name', 'dob', 'json_ext'),
    'group': ('code', 'json_ext'),
    'beneficiary': ('json_ext',),
    'group_beneficiary': ('json_ext',),
    'payment': ('photo', 'receipt', 'code', 'json_ext'),
    'grievance': ('description', 'title', 'reporter_id', 'json_ext'),
}

CLAUSES = {
    'fields': lambda field: {'fields': [field]},
    'filters': lambda field: {'filters': {field: {'operator': 'isnull', 'value': False}}},
    'group_by': lambda field: {'group_by': [field]},
    'order_by': lambda field: {'order_by': [field]},
    'aggregations': lambda field: {'aggregations': {'n': {'function': 'count', 'field': field}}},
}

# Clauses refused on a JSON field to every caller (cost bound, test_cost_bounds).
JSON_REFUSED_CLAUSES = ('group_by', 'order_by', 'aggregations')


class ExcludedFieldTest(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        self.user = _role_user(f'an_allow_{_marker()}', [QUERY])

    def test_non_superuser_is_refused_each_excluded_field_in_each_clause(self):
        for entity_type, fields in EXCLUDED.items():
            for field in fields:
                for clause, build in CLAUSES.items():
                    with self.subTest(entity=entity_type, field=field, clause=clause):
                        with self.assertRaises(ValueError) as refused:
                            _run(entity_type, build(field), self.user)
                        self.assertEqual(
                            str(refused.exception),
                            f"Field '{field}' is not allowed in {clause} for entity '{entity_type}'",
                        )

    def test_descending_order_on_an_excluded_field_is_refused(self):
        with self.assertRaises(ValueError) as refused:
            _run('individual', {'order_by': ['-dob']}, self.user)
        self.assertEqual(str(refused.exception), "Field 'dob' is not allowed in order_by for entity 'individual'")

    def test_list_form_of_filters_is_checked(self):
        with self.assertRaises(ValueError):
            _run('group', {'filters': [{'field': 'code', 'operator': 'exact', 'value': 'x'}]}, self.user)

    def test_superuser_references_every_excluded_field(self):
        for entity_type, fields in EXCLUDED.items():
            for field in fields:
                for clause, build in CLAUSES.items():
                    with self.subTest(entity=entity_type, field=field, clause=clause):
                        if field == 'json_ext' and clause in JSON_REFUSED_CLAUSES:
                            continue
                        _run(entity_type, build(field), self.admin)

    def test_superuser_is_refused_json_ext_where_it_reads_every_document(self):
        for entity_type in EXCLUDED:
            for clause in JSON_REFUSED_CLAUSES:
                with self.subTest(entity=entity_type, clause=clause):
                    with self.assertRaises(ValueError) as refused:
                        _run(entity_type, CLAUSES[clause]('json_ext'), self.admin)
                    self.assertEqual(
                        str(refused.exception),
                        f"JSON field 'json_ext' is not allowed in {clause} for entity '{entity_type}'",
                    )


@override_settings(ROW_SECURITY=False)
class AllowedQueryTest(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        self.user = _role_user(f'an_allowed_{_marker()}', [QUERY])
        self.individuals = []
        for first_name in ('Ana', 'Bea'):
            individual = Individual(
                first_name=first_name, last_name=_marker(), dob=datetime.date(1990, 1, 1),
                json_ext={'telephone': '79000000'},
            )
            individual.save(user=self.admin)
            self.individuals.append(individual)
            BenefitConsumption(
                id=uuid.uuid4(), individual=individual, code=_marker(), amount=72000, type='Cash',
                photo='photo-data', receipt='R-1', status='ACCEPTED', date_due=datetime.date(2026, 1, 1),
                json_ext={'phone': '79000000'}, user_created=self.admin, user_updated=self.admin, version=1,
            ).save(user=self.admin)
        self.ids = [str(individual.id) for individual in self.individuals]

    def _both(self, entity_type, config):
        return _run(entity_type, config, self.user).rows, _run(entity_type, config, self.admin).rows

    def test_allowed_columns_return_what_a_superuser_gets(self):
        cases = (
            ('individual', {
                'filters': {'id': {'operator': 'in', 'value': self.ids}},
                'group_by': ['location_id'], 'aggregations': {'n': {'function': 'count', 'field': 'id'}},
            }),
            ('payment', {
                'filters': {'individual_id': {'operator': 'in', 'value': self.ids}},
                'fields': ['amount', 'status', 'date_due', 'type'], 'order_by': ['date_due'],
            }),
            ('payment', {
                'filters': {'individual': {'operator': 'in', 'value': self.ids}},
                'aggregations': {'total': {'function': 'sum', 'field': 'amount'}},
            }),
        )
        for entity_type, config in cases:
            with self.subTest(entity=entity_type, config=config):
                user_rows, admin_rows = self._both(entity_type, config)
                self.assertTrue(user_rows)
                self.assertEqual(user_rows, admin_rows)

    def test_query_without_fields_returns_only_allowed_columns_to_a_non_superuser(self):
        config = {'filters': {'individual_id': {'operator': 'in', 'value': self.ids}}}
        user_rows, admin_rows = self._both('payment', config)
        self.assertEqual(len(user_rows), 2)
        self.assertEqual(
            set(user_rows[0]),
            {'id', 'is_deleted', 'date_created', 'date_updated', 'user_created_id', 'user_updated_id', 'version',
             'date_valid_from', 'date_valid_to', 'replacement_uuid', 'individual_id', 'date_due', 'amount',
             'type', 'status'},
        )
        self.assertTrue({'photo', 'receipt', 'code', 'json_ext'} <= set(admin_rows[0]))

    def test_individual_without_fields_leaves_out_names_birth_date_and_json_ext(self):
        config = {'filters': {'id': {'operator': 'in', 'value': self.ids}}}
        user_rows, admin_rows = self._both('individual', config)
        self.assertEqual(len(user_rows), 2)
        for row in user_rows:
            self.assertFalse({'first_name', 'last_name', 'dob', 'json_ext'} & set(row))
            self.assertIn('location_id', row)
        self.assertTrue({'first_name', 'last_name', 'dob', 'json_ext'} <= set(admin_rows[0]))

    def test_foreign_key_is_allowed_by_name_and_by_column(self):
        for field in ('location', 'location_id'):
            with self.subTest(field=field):
                _run('group', {'group_by': [field]}, self.user)


class EntityFieldListTest(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        self.user = _role_user(f'an_fields_{_marker()}', [QUERY, TICKET_READ])

    def _names(self, entity_type, user):
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            return {field['name'] for field in QueryBuilderService.get_entity_fields(entity_type, user)}

    def test_non_superuser_is_not_offered_excluded_fields(self):
        for entity_type, fields in EXCLUDED.items():
            with self.subTest(entity=entity_type):
                self.assertFalse(set(fields) & self._names(entity_type, self.user))

    def test_non_superuser_is_offered_the_allowed_columns(self):
        self.assertEqual(self._names('payment', self.user), {
            'id', 'is_deleted', 'date_created', 'date_updated', 'user_created', 'user_updated', 'version',
            'date_valid_from', 'date_valid_to', 'replacement_uuid', 'individual', 'date_due', 'amount',
            'type', 'status',
        })

    def test_superuser_is_offered_every_column(self):
        for entity_type, fields in EXCLUDED.items():
            with self.subTest(entity=entity_type):
                self.assertTrue(set(fields) <= self._names(entity_type, self.admin))

    def test_opensearch_field_list_is_filtered_too(self):
        listed = [{'name': name, 'type': 'keyword', 'label': name, 'filterable': True, 'aggregatable': False}
                  for name in ('status', 'photo', 'amount')]
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=True), \
                mock.patch('analytics.opensearch_service.OpenSearchQueryService.get_entity_fields',
                           return_value=listed):
            names = {field['name'] for field in QueryBuilderService.get_entity_fields('payment', self.user)}
        self.assertEqual(names, {'status', 'amount'})


class ConfiguredAllowlistTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _role_user(f'an_cfg_{_marker()}', [QUERY])

    def _with(self, allowlist):
        return mock.patch.object(AnalyticsConfig, 'analytics_field_allowlist', allowlist)

    def test_configured_entity_replaces_its_default_and_others_keep_theirs(self):
        with self._with({'individual': ['id', 'first_name']}):
            _run('individual', {'fields': ['first_name']}, self.user)
            with self.assertRaises(ValueError):
                _run('individual', {'group_by': ['location_id']}, self.user)
            with self.assertRaises(ValueError):
                _run('payment', {'fields': ['photo']}, self.user)
            _run('payment', {'fields': ['amount']}, self.user)

    def test_configuration_cannot_open_other_relation_paths(self):
        with self._with({'group': ['id', 'groupindividuals__individual__first_name', 'location__code']}):
            for field in ('groupindividuals__individual__first_name', 'location__code'):
                with self.subTest(field=field):
                    with self.assertRaises(ValueError):
                        _run('group', {'group_by': [field]}, self.user)

    def test_entity_value_that_is_not_a_list_allows_nothing(self):
        with self._with({'individual': 'first_name'}):
            with self.assertRaises(ValueError) as refused:
                _run('individual', {}, self.user)
            self.assertEqual(str(refused.exception), "No field of entity 'individual' is allowed")
            for field in ('first_name', 'id'):
                with self.subTest(field=field):
                    with self.assertRaises(ValueError):
                        _run('individual', {'aggregations': {'n': {'function': 'count', 'field': field}}},
                             self.user)

    def test_configuration_that_is_not_a_mapping_keeps_the_defaults(self):
        with self._with(['first_name']):
            with self.assertRaises(ValueError):
                _run('individual', {'fields': ['first_name']}, self.user)
            _run('individual', {'group_by': ['location_id']}, self.user)

    def test_module_configuration_key_is_loaded(self):
        previous = AnalyticsConfig.analytics_field_allowlist
        self.addCleanup(setattr, AnalyticsConfig, 'analytics_field_allowlist', previous)
        AnalyticsConfig._AnalyticsConfig__load_config({'analytics_field_allowlist': {'individual': ['id', 'dob']}})
        _run('individual', {'group_by': ['dob']}, self.user)


class BypassPathTest(TestCase):
    def setUp(self):
        cache.clear()
        self.marker = _marker()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        Individual(
            first_name='W', last_name=self.marker, dob=datetime.date(1990, 1, 1), json_ext={},
        ).save(user=self.admin)

    def test_public_widget_on_an_excluded_field_is_refused_to_a_viewer(self):
        viewer = _role_user(f'an_allow_view_{self.marker}', [VIEW])
        query = AnalyticsQuery.objects.create(
            name='names', entity_type='individual', is_public=True, created_by=self.admin,
            query_config={'fields': ['first_name', 'dob'], 'limit': 5},
        )
        dashboard = AnalyticsDashboard.objects.create(name='D', created_by=self.admin, is_public=True)
        widget = AnalyticsWidget.objects.create(
            dashboard=dashboard, query=query, widget_type='table', title='T',
            config={}, position={'x': 0, 'y': 0, 'w': 4, 'h': 4},
        )
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            with self.assertRaises(ValueError) as refused:
                Query().resolve_execute_analytics_widget(_info(viewer), str(widget.id))
            self.assertEqual(
                str(refused.exception), "Field 'first_name' is not allowed in fields for entity 'individual'",
            )
            rows = Query().resolve_execute_analytics_widget(_info(self.admin), str(widget.id)).data
        self.assertTrue(rows)

    def test_export_of_an_excluded_field_is_refused_and_writes_nothing(self):
        user = _role_user(f'an_allow_exp_{self.marker}', [QUERY, EXPORT])
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            with self.assertRaises(ValueError) as refused:
                ExportAnalyticsDataMutation.mutate(
                    None, _info(user), entity_type='individual',
                    query_config={'filters': {'last_name': {'operator': 'exact', 'value': self.marker}}},
                    export_format='csv',
                )
        self.assertEqual(str(refused.exception), "Field 'last_name' is not allowed in filters for entity 'individual'")
        self.assertFalse(AnalyticsExport.objects.filter(exported_by=user).exists())


class SeededQueriesTest(TestCase):
    def test_seeded_queries_run_for_a_non_superuser(self):
        create_test_interactive_user(username='Admin')
        call_command('seed_analytics_dashboards', stdout=open(os.devnull, 'w'))
        user = _role_user(f'an_allow_seed_{_marker()}', [VIEW, QUERY, TICKET_READ])
        seeded = AnalyticsQuery.objects.filter(
            name__in=('Bénéficiaires par programme', 'Bénéficiaires par province',
                      'Paiements par statut', 'Plaintes par catégorie'),
            validity_to__isnull=True,
        )
        self.assertEqual(seeded.count(), 4)
        for query in seeded:
            with self.subTest(query=query.name):
                _run(query.entity_type, query.query_config, user)
