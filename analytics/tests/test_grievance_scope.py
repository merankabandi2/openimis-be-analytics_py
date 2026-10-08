from unittest import mock

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import TestCase

from core.test_helpers import create_test_interactive_user
from location.test_helpers import assign_user_districts, create_test_village
from grievance_social_protection.apps import TicketConfig
from grievance_social_protection.models import Ticket

from analytics.apps import AnalyticsConfig
from analytics.services import QueryBuilderService
from analytics.tests.test_query_builder import _marker, _role_user, _run, _widen_allowlist

QUERY = int(AnalyticsConfig.gql_analytics_query_perms[0])
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
    """Three tickets: a public one, a secret one, and a public one flagged hidden.

    The tickets are located on a test colline whose province is assigned to the
    test users (see _user): an assembly may limit tickets to the user's
    provinces."""

    def setUp(self):
        patcher = mock.patch.multiple(TicketConfig, **GRIEVANCE_CONFIG)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The grievance rules also govern the ticket fields an operator adds to
        # the allowlist; these tests read them as such.
        _widen_allowlist(self, grievance=['title', 'description', 'reporter_id', 'json_ext'])
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        self.marker = _marker()
        colline_code = _marker()[:8]
        self.colline = create_test_village({'code': colline_code})
        self.province_code = f'D-{colline_code}'
        for category, flags in (('public', None), ('secret', None), ('public', 'hidden')):
            Ticket(
                title=f'{category} title', description='sensitive', category=category, flags=flags,
                status='OPEN', channel=self.marker, json_ext={'location': {'colline_code': self.colline.code}},
            ).save(user=self.admin)

    def _user(self, username, rights):
        """Non-admin user holding exactly `rights`, assigned the province of
        the fixture tickets."""
        user = _role_user(username, rights)
        assign_user_districts(user, [self.province_code])
        cache.clear()
        return user

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
        user = self._user(f'an_grv_none_{self.marker}', [QUERY])
        with self.assertRaises(PermissionDenied):
            self._count_by_category(user)

    def test_full_reader_sees_every_ticket(self):
        user = self._user(
            f'an_grv_full_{self.marker}', [QUERY, TICKET_READ, SECRET_READ, HIDDEN_FLAG_READ]
        )
        self.assertEqual(self._count_by_category(user), {'public': 2, 'secret': 1})

    def test_restricted_reader_counts_on_visible_fields(self):
        user = self._user(
            f'an_grv_restr_{self.marker}', [QUERY, TICKET_READ, SECRET_RESTRICTED_READ, HIDDEN_FLAG_RESTRICTED_READ]
        )
        # The flagged public ticket shows only the basic fields; the channel
        # filter reads a field hidden on it.
        self.assertEqual(self._count_by_category(user), {'public': 1, 'secret': 1})

    def test_restricted_reader_gets_no_hidden_columns(self):
        user = self._user(
            f'an_grv_rows_{self.marker}', [QUERY, TICKET_READ, SECRET_RESTRICTED_READ, HIDDEN_FLAG_RESTRICTED_READ]
        )
        rows = _run('grievance', self._config(), user).rows
        self.assertEqual([row['category'] for row in rows], ['public'])
        grouped_by_title = _run('grievance', self._config(group_by=['title']), user).rows
        self.assertEqual(grouped_by_title, [{'title': 'public title'}])

    def test_user_without_category_access_does_not_see_it(self):
        user = self._user(f'an_grv_nocat_{self.marker}', [QUERY, TICKET_READ])
        self.assertEqual(self._count_by_category(user), {'public': 1})

    def test_export_path_applies_the_same_scope(self):
        user = self._user(f'an_grv_exp_{self.marker}', [QUERY, TICKET_READ])
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            result = QueryBuilderService.execute_query('grievance', self._config(fields=['title']), user, max_rows=10)
        self.assertEqual(result.rows, [{'title': 'public title'}])


class WithheldRestrictedTicketsTest(GrievanceFixture):
    """A query that reads fields hidden on restricted tickets drops those tickets;
    the result says so instead of looking like an empty match."""

    def _restricted_user(self, rights=(SECRET_RESTRICTED_READ, HIDDEN_FLAG_RESTRICTED_READ)):
        return self._user(f'an_grv_wh_{_marker()}', [QUERY, TICKET_READ, *rights])

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
        ), self._restricted_user(rights=(SECRET_RESTRICTED_READ,)))
        self.assertEqual(result.rows, [{'category': 'secret', 'status': 'OPEN'}])
        self.assertFalse(result.restricted_rows_withheld)

    def test_visible_filters_that_match_no_withheld_ticket_report_nothing(self):
        result = _run('grievance', self._config(
            filters={
                'channel': {'operator': 'exact', 'value': self.marker},
                'status': {'operator': 'exact', 'value': 'CLOSED'},
            },
        ), self._restricted_user(rights=(SECRET_RESTRICTED_READ,)))
        self.assertEqual(result.rows, [])
        self.assertFalse(result.restricted_rows_withheld)

    def _flag_for(self, user, **hidden_filters):
        filters = {'channel': {'operator': 'exact', 'value': self.marker}, **hidden_filters}
        result = _run('grievance', self._config(
            filters=filters,
            group_by=['category'], aggregations={'n': {'function': 'count', 'field': 'id'}},
        ), user)
        return result.rows, result.restricted_rows_withheld

    def test_filter_on_a_field_hidden_in_the_category_does_not_change_the_flag(self):
        user = self._restricted_user(rights=(SECRET_RESTRICTED_READ,))
        category = {'category': {'operator': 'exact', 'value': 'secret'}}
        for field, hit, miss in (('description', 'sensit', 'zzzz'), ('title', 'secret title', 'no such title')):
            with self.subTest(field=field):
                matching = self._flag_for(user, **category, **{field: {'operator': 'contains', 'value': hit}})
                other = self._flag_for(user, **category, **{field: {'operator': 'contains', 'value': miss}})
                self.assertEqual(matching, other)
                self.assertEqual(matching, ([], True))

    def test_filter_on_the_ticket_id_does_not_change_the_flag(self):
        user = self._restricted_user(rights=(SECRET_RESTRICTED_READ,))
        secret = Ticket.objects.get(channel=self.marker, category='secret')
        pinned = {'id': {'operator': 'exact', 'value': str(secret.id)}}
        matching = self._flag_for(user, **pinned, description={'operator': 'startswith', 'value': 'sens'})
        other = self._flag_for(user, **pinned, description={'operator': 'startswith', 'value': 'zzzz'})
        self.assertEqual(matching, other)

    def test_filter_on_a_ticket_withheld_for_a_flag_does_not_change_the_flag(self):
        user = self._restricted_user(rights=(HIDDEN_FLAG_RESTRICTED_READ,))
        category = {'category': {'operator': 'exact', 'value': 'public'}}
        matching = self._flag_for(user, **category, description={'operator': 'contains', 'value': 'sensit'})
        other = self._flag_for(user, **category, description={'operator': 'contains', 'value': 'zzzz'})
        self.assertEqual(matching[1], other[1])
        self.assertTrue(matching[1])

    def test_tickets_the_user_cannot_see_are_not_reported(self):
        user = self._user(f'an_grv_wh_none_{_marker()}', [QUERY, TICKET_READ])
        result = _run('grievance', self._config(
            filters={
                'channel': {'operator': 'exact', 'value': self.marker},
                'category': {'operator': 'exact', 'value': 'secret'},
            },
        ), user)
        self.assertEqual(result.rows, [])
        self.assertFalse(result.restricted_rows_withheld)

    def test_full_reader_has_nothing_withheld(self):
        user = self._user(f'an_grv_wh_full_{_marker()}', [QUERY, TICKET_READ, SECRET_READ, HIDDEN_FLAG_READ])
        result = _run('grievance', self._config(), user)
        self.assertEqual(len(result.rows), 3)
        self.assertFalse(result.restricted_rows_withheld)


class FlagRestrictedTicketFieldsTest(GrievanceFixture):
    """A ticket restricted by a flag is left out of a query that reads a field
    the grievance module hides on it. Which fields stay visible on such a
    ticket depends on the grievance module version; test_grievance_module_parity
    checks them against the module itself.

    The fixture tickets carry the marker as status."""

    def setUp(self):
        super().setUp()
        Ticket.objects.filter(channel=self.marker).update(status=self.marker)
        self.restricted_user = self._user(
            f'an_grv_flag_{self.marker}', [QUERY, TICKET_READ, SECRET_RESTRICTED_READ, HIDDEN_FLAG_RESTRICTED_READ]
        )

    def _grouped(self, user, group_by, **filters):
        result = _run('grievance', {
            'filters': {'status': {'operator': 'exact', 'value': self.marker}, **filters},
            'group_by': [group_by],
            'aggregations': {'n': {'function': 'count', 'field': 'id'}},
        }, user)
        return {row[group_by]: row['n'] for row in result.rows}, result.restricted_rows_withheld

    def test_field_hidden_on_a_flag_restricted_ticket_withholds_it(self):
        self.assertEqual(self._grouped(self.restricted_user, 'title'), ({'public title': 1}, True))
