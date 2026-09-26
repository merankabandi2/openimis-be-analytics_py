"""executeAnalyticsQuery as a GraphQL client calls it: rights and data scoping
are enforced for the calling user, not only inside the service."""
import datetime
import json
import os
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings

from core.test_helpers import create_test_interactive_user
from grievance_social_protection.apps import TicketConfig
from grievance_social_protection.models import Ticket
from individual.models import Individual, Group
from location.test_helpers import create_test_village, assign_user_districts

from analytics.schema import Query
from analytics.services import ExportService, QueryBuilderService
from analytics.tests.test_grievance_scope import (
    GRIEVANCE_CONFIG, SECRET_RESTRICTED_READ, TICKET_READ,
)
from analytics.tests.test_query_builder import _marker, _role_user

QUERY = 200002


def _info(user):
    info = mock.Mock()
    info.context.user = user
    return info


def _execute(user, entity_type, config):
    with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
        result = Query().resolve_execute_analytics_query(
            _info(user), entity_type=entity_type, query_config=json.dumps(config),
        )
    return result.data


class GrievanceResolverScopeTest(TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(TicketConfig, **GRIEVANCE_CONFIG)
        patcher.start()
        self.addCleanup(patcher.stop)
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        self.marker = _marker()
        for category in ('public', 'secret'):
            Ticket(
                title=f'{category} title', description='sensitive', category=category,
                status='OPEN', channel=self.marker,
            ).save(user=self.admin)
        self.config = {'filters': {'channel': {'operator': 'exact', 'value': self.marker}}, 'limit': 10}

    def test_query_right_without_ticket_read_right_returns_no_ticket(self):
        user = _role_user(f'an_rs_gn_{self.marker}', [QUERY])
        with self.assertRaises(PermissionDenied):
            _execute(user, 'grievance', self.config)

    def test_restricted_reader_gets_no_column_outside_visible_fields(self):
        user = _role_user(f'an_rs_gr_{self.marker}', [QUERY, TICKET_READ, SECRET_RESTRICTED_READ])
        rows = _execute(user, 'grievance', self.config)
        self.assertEqual(sorted(row['category'] for row in rows), ['public'])
        self.assertNotIn('secret title', [row.get('title') for row in rows])

    def test_withheld_restricted_tickets_are_reported_to_the_client(self):
        user = _role_user(f'an_rs_gw_{self.marker}', [QUERY, TICKET_READ, SECRET_RESTRICTED_READ])
        config = dict(self.config, filters=dict(self.config['filters'], category={'operator': 'exact', 'value': 'secret'}))
        with mock.patch.object(QueryBuilderService, '_use_opensearch', return_value=False):
            result = Query().resolve_execute_analytics_query(
                _info(user), entity_type='GRIEVANCE', query_config=json.dumps(config),
            )
        self.assertEqual((result.data, result.row_count), ([], 0))
        self.assertTrue(result.restricted_rows_withheld)


class LocationResolverScopeTest(TestCase):
    def setUp(self):
        cache.clear()
        admin = create_test_interactive_user(username='analytics_qb_admin')
        self.marker = _marker()
        self.village_in = create_test_village({'code': _marker()[:6]})
        village_out = create_test_village({'code': _marker()[:6]})
        for village in (self.village_in, village_out):
            Group(code=self.marker, location=village, json_ext={}).save(user=admin)
        self.user = _role_user(f'an_rs_l_{self.marker}', [QUERY])
        assign_user_districts(self.user, [f'D-{self.village_in.code}'])
        cache.clear()

    @override_settings(ROW_SECURITY=True)
    def test_single_district_user_gets_only_that_district(self):
        rows = _execute(self.user, 'group', {
            'filters': {'code': {'operator': 'exact', 'value': self.marker}},
            'group_by': ['location_id'],
            'aggregations': {'n': {'function': 'count', 'field': 'id'}},
        })
        self.assertEqual(rows, [{'location_id': self.village_in.id, 'n': 1}])


class FieldSelectionResolverTest(TestCase):
    def test_only_selected_fields_are_returned(self):
        cache.clear()
        admin = create_test_interactive_user(username='analytics_qb_admin')
        marker = _marker()
        Individual(
            first_name='Alice', last_name=marker, dob=datetime.date(1990, 1, 1),
            json_ext={'phoneNumber': '79000000'},
        ).save(user=admin)
        rows = _execute(admin, 'individual', {
            'filters': {'last_name': {'operator': 'exact', 'value': marker}},
            'fields': ['first_name'],
        })
        self.assertEqual(rows, [{'first_name': 'Alice'}])


class ExportStorageTest(TestCase):
    def test_export_file_is_written_under_media_root(self):
        filepath = ExportService.export_to_csv([{'a': 1}], f'analytics_storage_{_marker()}')
        self.addCleanup(lambda: os.path.exists(filepath) and os.remove(filepath))
        media_root = os.path.realpath(settings.MEDIA_ROOT)
        self.assertEqual(os.path.commonpath([media_root, os.path.realpath(filepath)]), media_root)
        self.assertTrue(os.path.exists(filepath))


class DateFilterResolverTest(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        self.marker = _marker()
        Individual(
            first_name='Dated', last_name=self.marker, dob=datetime.date(1990, 1, 1), json_ext={},
        ).save(user=self.admin)

    def _dob(self, operator, value):
        return _execute(self.admin, 'individual', {
            'filters': {
                'last_name': {'operator': 'exact', 'value': self.marker},
                'dob': {'operator': operator, 'value': value},
            },
            'fields': ['first_name'],
        })

    def test_calendar_day_matches_the_date_column(self):
        self.assertEqual(self._dob('exact', '1990-01-01'), [{'first_name': 'Dated'}])

    def test_range_of_calendar_days_matches(self):
        self.assertEqual(self._dob('range', ['1989-12-31', '1990-01-02']), [{'first_name': 'Dated'}])

    def test_greater_than_a_calendar_day_matches(self):
        self.assertEqual(self._dob('gt', '1989-12-31'), [{'first_name': 'Dated'}])

    def test_range_with_a_timestamp_bound_is_refused(self):
        with self.assertRaises(ValueError):
            self._dob('range', ['1989-12-31T22:00:00.000Z', '1990-01-02'])

    def test_timestamp_on_a_date_column_is_refused_instead_of_matching_nothing(self):
        with self.assertRaises(ValueError):
            self._dob('exact', '1990-01-01T11:57:00.000Z')

    def test_timestamp_on_a_datetime_column_is_accepted(self):
        rows = _execute(self.admin, 'individual', {
            'filters': {
                'last_name': {'operator': 'exact', 'value': self.marker},
                'date_created': {'operator': 'gt', 'value': '2000-01-01T00:00:00.000Z'},
            },
            'fields': ['first_name'],
        })
        self.assertEqual(rows, [{'first_name': 'Dated'}])
