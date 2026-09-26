"""Payment row security: the payments a user may read are those of the
individuals Individual.get_queryset admits, whichever SQL form is used."""
import datetime
import uuid
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.db.models import Q
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from core.test_helpers import create_test_interactive_user
from individual.models import Group, GroupIndividual, Individual
from location.models import Location, LocationManager
from location.test_helpers import assign_user_districts, create_test_village
from payroll.models import BenefitConsumption

from analytics.tests.test_query_builder import _marker, _role_user, _run

QUERY = 200002


@override_settings(ROW_SECURITY=True)
class PaymentScopeTest(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = create_test_interactive_user(username='analytics_qb_admin')
        self.marker = _marker()
        self.allowed = create_test_village({'code': _marker()[:6]})
        self.denied = create_test_village({'code': _marker()[:6]})
        self.allowed_district = self.allowed.parent.parent
        self.denied_district = self.denied.parent.parent
        cache.clear()

        allowed_group = self._group(self.allowed)
        null_group = self._group(None)
        denied_group = self._group(self.denied)
        # Each individual covers one branch of Individual.get_queryset.
        self.cases = {
            'no_location': self._individual(None),
            'allowed_location': self._individual(self.allowed),
            'denied_in_allowed_group': self._individual(self.denied, allowed_group),
            'denied_in_group_without_location': self._individual(self.denied, null_group),
            'denied_in_denied_group': self._individual(self.denied, denied_group),
            'denied_without_group': self._individual(self.denied),
            'soft_deleted_allowed': self._individual(self.allowed, deleted=True),
            'denied_with_deleted_link_to_allowed_group': self._individual(
                self.denied, allowed_group, link_deleted=True,
            ),
        }
        for individual in self.cases.values():
            BenefitConsumption(
                id=uuid.uuid4(), individual=individual, code=f'{self.marker}-{individual.id}', amount=10, type='Cash',
                date_due=datetime.date(2026, 1, 1),
                user_created=self.admin, user_updated=self.admin, version=1,
            ).save(user=self.admin)

    def _group(self, location):
        group = Group(code=self.marker, location=location, json_ext={})
        group.save(user=self.admin)
        return group

    def _individual(self, location, group=None, deleted=False, link_deleted=False):
        individual = Individual(
            first_name='P', last_name=self.marker, dob=datetime.date(1990, 1, 1), json_ext={}, location=location,
        )
        individual.save(user=self.admin)
        if deleted:
            Individual.objects.filter(id=individual.id).update(is_deleted=True)
        if group is not None:
            link = GroupIndividual(group=group, individual=individual)
            link.save(user=self.admin)
            if link_deleted:
                GroupIndividual.objects.filter(id=link.id).update(is_deleted=True)
        return individual

    def _expected(self, user):
        return set(
            BenefitConsumption.objects.filter(
                code__startswith=self.marker, individual__in=Individual.get_queryset(None, user),
            ).values_list('individual_id', flat=True)
        )

    def _scoped(self, user):
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            rows = _run('payment', {
                'filters': {'code': {'operator': 'startswith', 'value': self.marker}},
                'fields': ['individual_id'],
                'limit': 100,
            }, user).rows
        payment_sql = [q['sql'] for q in ctx.captured_queries if 'payroll_benefitconsumption' in q['sql']]
        return {uuid.UUID(str(row['individual_id'])) for row in rows}, payment_sql

    def _names(self, ids):
        return sorted(name for name, individual in self.cases.items() if individual.id in ids)

    def _user(self, district_codes):
        user = _role_user(f'an_pay_{_marker()}', [QUERY])
        assign_user_districts(user, district_codes)
        cache.clear()
        return user

    def test_user_of_few_districts_gets_the_individual_rule(self):
        user = self._user([self.allowed_district.code])
        ids, _ = self._scoped(user)
        self.assertEqual(self._names(ids), self._names(self._expected(user)))
        self.assertNotIn('denied_without_group', self._names(ids))

    def test_user_of_most_districts_gets_the_same_rows_without_the_group_scan(self):
        districts = Location.objects.filter(type='D', validity_to__isnull=True).exclude(
            id=self.denied_district.id,
        ).values_list('code', flat=True)
        user = self._user(list(districts))
        ids, payment_sql = self._scoped(user)
        expected = self._expected(user)
        self.assertEqual(self._names(ids), self._names(expected))
        self.assertEqual(self._names(ids), [
            'allowed_location', 'denied_in_allowed_group', 'denied_in_group_without_location',
            'denied_with_deleted_link_to_allowed_group', 'no_location', 'soft_deleted_allowed',
        ])
        self.assertEqual(len(payment_sql), 1)
        self.assertNotIn('LEFT OUTER JOIN "individual_groupindividual"', payment_sql[0])

    def test_no_denied_location_adds_no_individual_filter(self):
        user = self._user([self.allowed_district.code])
        every_location = Q(id__in=list(Location.objects.values_list('id', flat=True))) | Q(id__isnull=True)
        with mock.patch.object(LocationManager, 'build_user_location_filter_query', return_value=every_location):
            ids, payment_sql = self._scoped(user)
        self.assertEqual(self._names(ids), sorted(self.cases))
        self.assertEqual(len(payment_sql), 1)
        self.assertNotIn('individual_individual', payment_sql[0])
