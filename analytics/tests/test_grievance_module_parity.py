"""Grievance data in analytics against what the grievance module itself shows.

The fixture follows the production grievance configuration: a violence_vbg
category with a sub-category and visible_fields, SENSITIVE and SPECIAL flags,
and no delete right anywhere. The expected visibility of each ticket and field
comes from the grievance module's own `tickets` GraphQL query run as the same
user, so the tests hold for every grievance module version on the path.

The module returns None for a restricted field whose value is empty, so every
fixture ticket holds a value in each gated field; tickets without a configured
flag carry ROUTINE, a flag absent from the configuration.
"""
import copy
import csv
import datetime
import json
import os
import uuid
from unittest import mock

import graphene
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from graphql_relay import from_global_id

from core.test_helpers import create_test_interactive_user
from grievance_social_protection.apps import TicketConfig
from grievance_social_protection.models import Ticket
from grievance_social_protection.schema import Query as GrievanceQuery
from individual.models import Individual

from analytics.apps import AnalyticsConfig
from analytics.models import AnalyticsExport
from analytics.schema import ExportAnalyticsDataMutation, Query as AnalyticsQuery
from analytics.services import QueryBuilderService
from analytics.tests.test_permissions import _info
from analytics.tests.test_query_builder import _marker, _role_user, _run, _widen_allowlist

QUERY = int(AnalyticsConfig.gql_analytics_query_perms[0])
EXPORT = int(AnalyticsConfig.gql_analytics_export_perms[0])
TICKET_READ, TICKET_CREATE, TICKET_UPDATE, TICKET_DELETE = 127000, 127001, 127002, 127003

VBG = 'violence_vbg'
VBG_CHILD = 'violence_vbg > viol'

GRIEVANCE_CONFIG = {
    'default_grievance_type': 'uncategorized',
    'grievance_types': [
        {
            'name': 'violence_vbg',
            'permissions': ['restricted_read', 'read', 'create', 'update'],
            'default_flags': ['SENSITIVE'],
            'visible_fields': ['category', 'description', 'date_of_incident', 'reporter', 'status', 'attending_staff'],
            'children': ['viol'],
        },
        {'name': 'paiement', 'permissions': ['read', 'create', 'update']},
    ],
    'grievance_flags': [
        {'name': 'SENSITIVE', 'permissions': ['restricted_read', 'read']},
        {'name': 'SPECIAL', 'permissions': ['read']},
    ],
}

# GraphQL name of each ticket field the grievance module's resolvers gate, and
# the model field it shows.
GATED_FIELDS = {
    'title': 'title', 'description': 'description', 'status': 'status', 'priority': 'priority',
    'category': 'category', 'flags': 'flags', 'channel': 'channel', 'resolution': 'resolution',
    'dateOfIncident': 'date_of_incident', 'dueDate': 'due_date', 'dateCreated': 'date_created',
    'jsonExt': 'json_ext', 'reporterId': 'reporter_id', 'attendingStaff { id }': 'attending_staff',
}
RESTRICTED_VALUE = '[Restricted]'

FIELD_SETS = (
    ['status'],
    ['category'],
    ['category', 'status'],
    ['description'],
    ['title'],
    ['json_ext'],
    ['reporter_id'],
    ['attending_staff'],
    sorted(GATED_FIELDS.values()),
)


def _configure(test, anonymised=None):
    """Load GRIEVANCE_CONFIG through the grievance module's own processing and
    give each generated right a test right id. Returns {(name, permission): id}."""
    cfg = copy.deepcopy(GRIEVANCE_CONFIG)
    patcher = mock.patch.multiple(
        TicketConfig,
        processed_categories={}, processed_flags={}, grievance_types=[], grievance_flags=[],
        default_grievance_type=cfg['default_grievance_type'],
        grievance_anonymized_fields=anonymised if anonymised is not None else {},
        gql_query_tickets_perms=[str(TICKET_READ)],
        gql_mutation_create_tickets_perms=[str(TICKET_CREATE)],
        gql_mutation_update_tickets_perms=[str(TICKET_UPDATE)],
        gql_mutation_delete_tickets_perms=[str(TICKET_DELETE)],
    )
    patcher.start()
    test.addCleanup(patcher.stop)
    TicketConfig._TicketConfig__process_unified_categories(cfg)
    TicketConfig._TicketConfig__process_unified_flags(cfg)
    TicketConfig.grievance_types = cfg['grievance_types']
    TicketConfig.grievance_flags = cfg['grievance_flags']
    rights = {}
    next_id = 998000
    for processed in (TicketConfig.processed_categories, TicketConfig.processed_flags):
        for name, info in processed.items():
            info['generated_rights'] = {}
            for permission in info.get('permissions', []):
                next_id += 1
                info['generated_rights'][permission] = next_id
                rights[(name, permission)] = next_id
    return rights


def _coerce(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def _row_key(row):
    return json.dumps({k: _coerce(v) for k, v in row.items()}, sort_keys=True, default=str)


class ModuleParityFixture(TestCase):
    anonymised = None

    def setUp(self):
        cache.clear()
        # The grievance rules also govern the ticket fields an operator adds to
        # the allowlist; these tests read them as such.
        _widen_allowlist(self, grievance=['title', 'description', 'reporter_id', 'json_ext'])
        self.rights = _configure(self, self.anonymised)
        self.admin = create_test_interactive_user(username='analytics_parity_admin')
        individual = Individual(first_name='R', last_name='P', dob=datetime.date(1990, 1, 1), json_ext={})
        individual.save(user=self.admin)
        reporter_type = ContentType.objects.get_for_model(Individual)
        self.tickets = {}
        for key, category, flags in (
            ('pay', 'paiement', 'ROUTINE'),
            ('pay_sensitive', 'paiement', 'SENSITIVE'),
            ('pay_special', 'paiement', 'SPECIAL'),
            ('vbg', VBG, 'ROUTINE'),
            ('vbg_sensitive', VBG, 'SENSITIVE'),
            ('viol_sensitive', VBG_CHILD, 'SENSITIVE'),
        ):
            ticket = Ticket(
                title=f'title {key}', description=f'description {key}', code=_marker()[:16],
                category=category, flags=flags, status='OPEN', priority='High', channel='telephone',
                resolution=f'resolution {key}', date_of_incident=datetime.date(2026, 5, 1),
                due_date=datetime.date(2026, 6, 1), json_ext={'key': key},
                reporter_type=reporter_type, reporter_id=str(individual.id), attending_staff=self.admin,
            )
            ticket.save(user=self.admin)
            self.tickets[key] = ticket
        # Tickets other tests left in a kept test database stay out of the counts.
        Ticket.objects.exclude(id__in=[t.id for t in self.tickets.values()]).update(is_deleted=True)

    def _right(self, name, permission):
        return self.rights[(name, permission)]

    def _users(self):
        """Users holding the rights of the production roles analysed in #251."""
        pay_read = self._right('paiement', 'read')
        special_read = self._right('SPECIAL', 'read')
        vbg_restricted = [self._right(VBG, 'restricted_read'), self._right(VBG_CHILD, 'restricted_read')]
        vbg_read = [self._right(VBG, 'read'), self._right(VBG_CHILD, 'read')]
        sensitive_restricted = self._right('SENSITIVE', 'restricted_read')
        sensitive_read = self._right('SENSITIVE', 'read')
        base = [QUERY, EXPORT, TICKET_READ]
        marker = _marker()
        users = {
            'no_vbg_right': _role_user(f'par_novbg_{marker}', base + [pay_read, special_read]),
            'restricted': _role_user(
                f'par_restr_{marker}', base + [pay_read, special_read, sensitive_restricted, *vbg_restricted]),
            'restricted_with_base_cud': _role_user(
                f'par_cud_{marker}',
                base + [TICKET_CREATE, TICKET_UPDATE, TICKET_DELETE,
                        pay_read, special_read, sensitive_restricted, *vbg_restricted]),
            'full': _role_user(
                f'par_full_{marker}', base + [pay_read, special_read, sensitive_read, *vbg_read]),
        }
        users['superuser'] = self.admin
        return users

    def _module_view(self, user):
        """{ticket id: set of model fields the grievance module shows} for the
        fixture tickets the module's `tickets` query lists to `user`.

        `status` is non-null in the module's schema and its resolver returns
        None when the field is restricted, which fails the whole list; it is
        read ticket by ticket instead."""
        schema = graphene.Schema(query=GrievanceQuery)
        request = RequestFactory().post('/graphql')
        request.user = user
        selection = ' '.join(name for name in GATED_FIELDS if name != 'status')
        result = schema.execute(
            '{ tickets { edges { node { id %s } } } }' % selection, context_value=request,
        )
        self.assertIsNone(result.errors, result.errors)
        fixture_ids = {str(t.id) for t in self.tickets.values()}
        view = {}
        for edge in result.data['tickets']['edges']:
            node = edge['node']
            ticket_id = from_global_id(node['id'])[1]
            if ticket_id not in fixture_ids:
                continue
            shown = set()
            for gql_name, model_field in GATED_FIELDS.items():
                if gql_name == 'status':
                    continue
                value = node[gql_name.split(' ')[0]]
                if value is not None and value != RESTRICTED_VALUE:
                    shown.add(model_field)
            status = schema.execute(
                '{ tickets(id: "%s") { edges { node { id status } } } }' % ticket_id, context_value=request,
            )
            edges = [] if status.errors else status.data['tickets']['edges']
            if len(edges) == 1 and edges[0]['node'] and edges[0]['node']['status']:
                shown.add('status')
            view[ticket_id] = shown
        return view

    def _expected_rows(self, ticket_ids, fields):
        rows = Ticket.objects.filter(id__in=ticket_ids).values(*fields)
        return sorted(_row_key(row) for row in rows)


class GrievanceModuleParityTest(ModuleParityFixture):
    def test_rows_hold_only_tickets_and_fields_the_module_shows(self):
        for label, user in self._users().items():
            view = self._module_view(user)
            for fields in FIELD_SETS:
                with self.subTest(user=label, fields=fields):
                    expected_ids = [tid for tid, shown in view.items() if set(fields) <= shown]
                    rows = _run('grievance', {'fields': fields, 'limit': 100}, user).rows
                    self.assertEqual(sorted(_row_key(row) for row in rows), self._expected_rows(expected_ids, fields))

    def test_counts_cover_only_tickets_the_module_lists(self):
        for label, user in self._users().items():
            view = self._module_view(user)
            with self.subTest(user=label):
                rows = _run('grievance', {'aggregations': {'n': {'function': 'count', 'field': 'id'}}}, user).rows
                self.assertEqual(rows, [{'n': len(view)}])

    def test_group_by_counts_only_tickets_whose_group_field_the_module_shows(self):
        for label, user in self._users().items():
            view = self._module_view(user)
            for group_field in ('category', 'flags', 'status', 'channel', 'title'):
                with self.subTest(user=label, group_by=group_field):
                    expected = {}
                    for ticket in self.tickets.values():
                        if group_field in view.get(str(ticket.id), set()):
                            value = getattr(ticket, group_field)
                            expected[value] = expected.get(value, 0) + 1
                    rows = _run('grievance', {
                        'group_by': [group_field], 'aggregations': {'n': {'function': 'count', 'field': 'id'}},
                    }, user).rows
                    self.assertEqual({row[group_field]: row['n'] for row in rows}, expected)

    def test_filters_on_hidden_fields_select_no_hidden_ticket(self):
        for label, user in self._users().items():
            view = self._module_view(user)
            with self.subTest(user=label):
                expected = [tid for tid, shown in view.items() if {'description', 'category'} <= shown]
                rows = _run('grievance', {
                    'filters': {'description': {'operator': 'contains', 'value': 'description'}},
                    'group_by': ['category'], 'aggregations': {'n': {'function': 'count', 'field': 'id'}},
                }, user).rows
                self.assertEqual(sum(row['n'] for row in rows), len(expected))

    def test_superuser_reads_every_field_of_every_ticket(self):
        rows = _run('grievance', {'fields': ['description'], 'limit': 100}, self.admin).rows
        self.assertEqual(len(rows), len(self.tickets))


class NoVbgRightTest(ModuleParityFixture):
    """A user without any violence_vbg right: the module lists no VBG ticket,
    and neither do rows, counts, group-bys, entity fields or exports."""

    def setUp(self):
        super().setUp()
        self.user = self._users()['no_vbg_right']
        self.vbg_ids = {str(self.tickets[k].id) for k in ('vbg', 'vbg_sensitive', 'viol_sensitive')}

    def test_module_lists_no_vbg_ticket(self):
        self.assertFalse(self.vbg_ids & set(self._module_view(self.user)))

    def test_no_vbg_ticket_in_rows_counts_or_group_bys(self):
        rows = _run('grievance', {'fields': ['category', 'description'], 'limit': 100}, self.user).rows
        self.assertEqual(sorted(r['category'] for r in rows), ['paiement', 'paiement'])
        total = _run('grievance', {'aggregations': {'n': {'function': 'count', 'field': 'id'}}}, self.user).rows
        self.assertEqual(total, [{'n': 2}])
        by_category = _run('grievance', {
            'group_by': ['category'], 'aggregations': {'n': {'function': 'count', 'field': 'id'}},
        }, self.user).rows
        self.assertEqual(by_category, [{'category': 'paiement', 'n': 2}])
        filtered = _run('grievance', {
            'filters': {'category': {'operator': 'startswith', 'value': 'violence'}},
            'aggregations': {'n': {'function': 'count', 'field': 'id'}},
        }, self.user)
        self.assertEqual(filtered.rows, [{'n': 0}])
        self.assertFalse(filtered.restricted_rows_withheld)

    def test_export_holds_no_vbg_ticket(self):
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            result = ExportAnalyticsDataMutation.mutate(
                None, _info(self.user), entity_type='grievance',
                query_config={'fields': ['category', 'description', 'title']}, export_format='csv',
            )
        record = AnalyticsExport.objects.get(pk=result.export_id)
        self.addCleanup(lambda: os.path.exists(record.file_path) and os.remove(record.file_path))
        with open(record.file_path, newline='') as handle:
            exported = list(csv.DictReader(handle))
        self.assertEqual(sorted(r['category'] for r in exported), ['paiement', 'paiement'])
        self.assertFalse(any('vbg' in value or 'viol' in value for r in exported for value in r.values()))


class EntityFieldsTest(ModuleParityFixture):
    def _listed(self, user):
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            fields = AnalyticsQuery().resolve_analytics_entity_fields(_info(user), 'grievance')
        return {f.name for f in fields}

    def test_user_restricted_to_vbg_is_offered_only_fields_the_module_shows_there(self):
        user = _role_user(f'par_ef_{_marker()}', [QUERY, TICKET_READ, self._right(VBG, 'restricted_read')])
        shown = self._module_view(user)[str(self.tickets['vbg'].id)]
        listed = self._listed(user)
        self.assertEqual(listed & set(GATED_FIELDS.values()), shown)
        self.assertNotIn('title', listed)

    def test_user_without_the_ticket_read_right_is_offered_no_field(self):
        user = _role_user(f'par_ef0_{_marker()}', [QUERY])
        self.assertEqual(self._listed(user), set())

    def test_superuser_is_offered_every_field(self):
        self.assertTrue(set(GATED_FIELDS.values()) <= self._listed(self.admin))


class AnonymisedFieldsTest(ModuleParityFixture):
    """Fields listed in grievance_anonymized_fields are hidden from every user
    but superusers: under the default key on every ticket, under a category on
    its tickets and on those of its sub-categories."""
    anonymised = {'Default': ['reporter_id'], VBG: ['description']}

    def test_default_entry_hides_the_field_on_every_ticket(self):
        user = self._users()['full']
        result = _run('grievance', {'fields': ['reporter_id'], 'limit': 100}, user)
        self.assertEqual(result.rows, [])
        self.assertTrue(result.restricted_rows_withheld)

    def test_category_entry_hides_the_field_on_the_category_and_its_sub_categories(self):
        user = self._users()['full']
        rows = _run('grievance', {'fields': ['category', 'description'], 'limit': 100}, user).rows
        self.assertEqual({r['category'] for r in rows}, {'paiement'})
        grouped = _run('grievance', {
            'group_by': ['description'], 'aggregations': {'n': {'function': 'count', 'field': 'id'}},
        }, user).rows
        self.assertFalse(any('vbg' in r['description'] or 'viol' in r['description'] for r in grouped))

    def test_anonymised_fields_are_not_offered(self):
        user = _role_user(f'par_an_{_marker()}', [QUERY, TICKET_READ, self._right(VBG, 'read')])
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            listed = {f.name for f in AnalyticsQuery().resolve_analytics_entity_fields(_info(user), 'grievance')}
        self.assertNotIn('description', listed)
        self.assertNotIn('reporter_id', listed)
        self.assertIn('status', listed)

    def test_superuser_still_reads_anonymised_fields(self):
        rows = _run('grievance', {'fields': ['reporter_id', 'description'], 'limit': 100}, self.admin).rows
        self.assertEqual(len(rows), len(self.tickets))
