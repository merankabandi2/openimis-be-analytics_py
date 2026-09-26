from unittest import mock

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import TestCase

from core.test_helpers import create_test_interactive_user
from grievance_social_protection.apps import TicketConfig
from grievance_social_protection.models import Ticket

from analytics.services import QueryBuilderService
from analytics.tests.test_query_builder import _marker, _role_user, _run

TICKET_READ = 127000
SECRET_RESTRICTED_READ = 999101
SECRET_READ = 999102
HIDDEN_FLAG_RESTRICTED_READ = 999111
HIDDEN_FLAG_READ = 999112

GRIEVANCE_CONFIG = {
    'grievance_types': ['Default', 'public', 'secret'],
    'default_grievance_type': 'Default',
    'processed_categories': {
        'secret': {
            'generated_rights': {'restricted_read': SECRET_RESTRICTED_READ, 'read': SECRET_READ},
            'visible_fields': ['category', 'status', 'reporter', 'channel'],
        },
    },
    'processed_flags': {
        'hidden': {
            'generated_rights': {'restricted_read': HIDDEN_FLAG_RESTRICTED_READ, 'read': HIDDEN_FLAG_READ},
        },
    },
    'gql_query_tickets_perms': [str(TICKET_READ)],
}


class GrievanceFixture(TestCase):
    """Three tickets: a public one, a secret one, and a public one flagged hidden."""

    def setUp(self):
        patcher = mock.patch.multiple(TicketConfig, **GRIEVANCE_CONFIG)
        patcher.start()
        self.addCleanup(patcher.stop)
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        self.marker = _marker()
        for category, flags in (('public', None), ('secret', None), ('public', 'hidden')):
            Ticket(
                title=f'{category} title', description='sensitive', category=category, flags=flags,
                status='OPEN', channel=self.marker,
            ).save(user=self.admin)

    def _config(self, **extra):
        config = {'filters': {'channel': {'operator': 'exact', 'value': self.marker}}}
        config.update(extra)
        return config

    def _count_by_category(self, user):
        rows = _run('grievance', self._config(
            group_by=['category'], aggregations={'n': {'function': 'count', 'field': 'id'}},
        ), user).rows
        return {row['category']: row['n'] for row in rows}


class GrievanceScopeTest(GrievanceFixture):
    def test_user_without_ticket_read_right_is_refused(self):
        user = _role_user(f'an_grv_none_{self.marker}', [200002])
        with self.assertRaises(PermissionDenied):
            self._count_by_category(user)

    def test_full_reader_sees_every_ticket(self):
        user = _role_user(
            f'an_grv_full_{self.marker}', [200002, TICKET_READ, SECRET_READ, HIDDEN_FLAG_READ]
        )
        self.assertEqual(self._count_by_category(user), {'public': 2, 'secret': 1})

    def test_restricted_reader_counts_on_visible_fields(self):
        user = _role_user(
            f'an_grv_restr_{self.marker}', [200002, TICKET_READ, SECRET_RESTRICTED_READ, HIDDEN_FLAG_RESTRICTED_READ]
        )
        # The restricted ticket of the unrestricted category exposes no field.
        self.assertEqual(self._count_by_category(user), {'public': 1, 'secret': 1})

    def test_restricted_reader_gets_no_hidden_columns(self):
        user = _role_user(
            f'an_grv_rows_{self.marker}', [200002, TICKET_READ, SECRET_RESTRICTED_READ, HIDDEN_FLAG_RESTRICTED_READ]
        )
        rows = _run('grievance', self._config(), user).rows
        self.assertEqual([row['category'] for row in rows], ['public'])
        grouped_by_title = _run('grievance', self._config(group_by=['title']), user).rows
        self.assertEqual(grouped_by_title, [{'title': 'public title'}])

    def test_user_without_category_access_does_not_see_it(self):
        user = _role_user(f'an_grv_nocat_{self.marker}', [200002, TICKET_READ])
        self.assertEqual(self._count_by_category(user), {'public': 1})

    def test_export_path_applies_the_same_scope(self):
        user = _role_user(f'an_grv_exp_{self.marker}', [200002, TICKET_READ])
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            result = QueryBuilderService.execute_query('grievance', self._config(fields=['title']), user, max_rows=10)
        self.assertEqual(result.rows, [{'title': 'public title'}])


class WithheldRestrictedTicketsTest(GrievanceFixture):
    """A query that reads fields hidden on restricted tickets drops those tickets;
    the result says so instead of looking like an empty match."""

    def _restricted_user(self, rights=(SECRET_RESTRICTED_READ, HIDDEN_FLAG_RESTRICTED_READ)):
        return _role_user(f'an_grv_wh_{_marker()}', [200002, TICKET_READ, *rights])

    def test_ungrouped_query_on_a_restricted_category_reports_the_withheld_tickets(self):
        result = _run('grievance', self._config(
            filters={
                'channel': {'operator': 'exact', 'value': self.marker},
                'category': {'operator': 'exact', 'value': 'secret'},
            },
            limit=1,
        ), self._restricted_user())
        self.assertEqual(result.rows, [])
        self.assertTrue(result.restricted_rows_withheld)

    def test_query_on_visible_fields_withholds_nothing(self):
        result = _run('grievance', self._config(
            filters={
                'channel': {'operator': 'exact', 'value': self.marker},
                'category': {'operator': 'exact', 'value': 'secret'},
            },
            fields=['category', 'status'],
        ), self._restricted_user())
        self.assertEqual(result.rows, [{'category': 'secret', 'status': 'OPEN'}])
        self.assertFalse(result.restricted_rows_withheld)

    def test_filters_that_match_no_withheld_ticket_report_nothing(self):
        result = _run('grievance', self._config(
            filters={
                'channel': {'operator': 'exact', 'value': self.marker},
                'title': {'operator': 'exact', 'value': 'no such title'},
            },
        ), self._restricted_user())
        self.assertEqual(result.rows, [])
        self.assertFalse(result.restricted_rows_withheld)

    def test_tickets_the_user_cannot_see_are_not_reported(self):
        user = _role_user(f'an_grv_wh_none_{_marker()}', [200002, TICKET_READ])
        result = _run('grievance', self._config(
            filters={
                'channel': {'operator': 'exact', 'value': self.marker},
                'category': {'operator': 'exact', 'value': 'secret'},
            },
        ), user)
        self.assertEqual(result.rows, [])
        self.assertFalse(result.restricted_rows_withheld)

    def test_full_reader_has_nothing_withheld(self):
        user = _role_user(f'an_grv_wh_full_{_marker()}', [200002, TICKET_READ, SECRET_READ, HIDDEN_FLAG_READ])
        result = _run('grievance', self._config(), user)
        self.assertEqual(len(result.rows), 3)
        self.assertFalse(result.restricted_rows_withheld)
