from datetime import date, datetime, timedelta, timezone as datetime_timezone

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from orders.models import Part
from .expiry import expiry_movement_history, group_expiry_movements
from .models import MaterialTransaction, MovementExpiryEvent, MovementVisibilityEvent, RawMaterialSetting, Warehouse


@override_settings(TIME_ZONE='Asia/Seoul', USE_TZ=True)
class ExpiryGroupingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='group-admin', is_superuser=True)
        self.client.force_login(self.user)
        self.part = Part.objects.create(part_no='SEAL002', part_name='Seal')
        self.setting = RawMaterialSetting.objects.create(part=self.part, shelf_life_days=90)
        self.source = Warehouse.objects.create(code='4200', name='Material')
        self.target = Warehouse.objects.create(code='4300', name='Production')
        self.moved_at = timezone.make_aware(datetime(2026, 9, 11, 8, 21))
        self.page = reverse('material:raw_material_expiry') + '?tab=used'

    def movement(self, number, **changes):
        fields = dict(transaction_no=number, transaction_type='TRANSFER', part=self.part,
                      quantity=5, lot_no=date(2026, 8, 22), date=self.moved_at,
                      warehouse_from=self.source, warehouse_to=self.target, actor=self.user)
        fields.update(changes)
        return MaterialTransaction.objects.create(**fields)

    def groups(self):
        rows = expiry_movement_history({self.part.pk: self.setting}, include_hidden=True)
        return group_expiry_movements(rows)

    def test_eleven_same_day_lot_transfers_sum_to_55_and_keep_originals(self):
        for i in range(11):
            self.movement(f'MOVE-{i}', date=self.moved_at + timedelta(minutes=i))
        groups = self.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['quantity'], 55)
        self.assertEqual(groups[0]['used_d_day'], 70)
        self.assertEqual(len(groups[0]['members']), 11)
        self.assertEqual(MaterialTransaction.objects.count(), 11)
        self.assertTrue(all(q == 5 for q in MaterialTransaction.objects.values_list('quantity', flat=True)))
        response = self.client.get(self.page)
        self.assertEqual(response.context['grouped_count'], 1)
        self.assertEqual(response.context['used_count'], 11)
        self.assertContains(response, 'class="expiry-group-row"', count=1)
        self.assertContains(response, '11건 수불번호')
        self.assertContains(response, 'id="expiryDetailBody" class="d-none"')

    def test_different_day_lot_route_and_batch_stay_separate(self):
        self.movement('BASE')
        self.movement('DAY', date=self.moved_at + timedelta(days=1))
        self.movement('LOT', lot_no=date(2026, 7, 31))
        self.movement('BATCH-A', production_lot='A')
        self.movement('BATCH-B', production_lot='B')
        source = Warehouse.objects.create(code='3200', name='Third material')
        target = Warehouse.objects.create(code='3000', name='Third production')
        self.movement('ROUTE', warehouse_from=source, warehouse_to=target)
        self.assertEqual(len(self.groups()), 6)

    def test_different_parts_are_never_combined(self):
        self.movement('BASE')
        part = Part.objects.create(part_no='OTHER', part_name='Other')
        setting = RawMaterialSetting.objects.create(part=part, shelf_life_days=90)
        self.movement('OTHER', part=part)
        rows = expiry_movement_history({self.part.pk: self.setting, part.pk: setting})
        self.assertEqual(len(group_expiry_movements(rows)), 2)

    def test_hidden_member_is_excluded_and_restore_rejoins_group(self):
        first = self.movement('HIDE')
        self.movement('KEEP')
        event = MovementVisibilityEvent.objects.create(
            movement=first, transaction_no=first.transaction_no, hidden=True, actor=self.user,
        )
        self.assertEqual(self.groups()[0]['quantity'], 5)
        MovementVisibilityEvent.objects.create(
            movement=first, transaction_no=first.transaction_no, previous_hidden=True, hidden=False, actor=self.user,
        )
        self.assertEqual(self.groups()[0]['quantity'], 10)
        self.assertTrue(MovementVisibilityEvent.objects.filter(pk=event.pk).exists())

    def test_unknown_dates_are_separate_and_entered_manufacturing_dates_can_group(self):
        first = self.movement('UNKNOWN-A', lot_no=None)
        second = self.movement('UNKNOWN-B', lot_no=None, transaction_type='TRF_ERP')
        self.assertEqual(len(self.groups()), 2)
        for trx in (first, second):
            MovementExpiryEvent.objects.create(movement=trx, transaction_no=trx.transaction_no,
                                              manufacturing_date=date(2026, 8, 22), actor=self.user)
        groups = self.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['quantity'], 10)
        self.assertCountEqual(groups[0]['sources'], ['SCM', 'ERP'])

    def test_grouping_uses_korean_calendar_day_and_preserves_filters(self):
        self.movement('UTC-PREVIOUS', date=datetime(2026, 9, 10, 16, tzinfo=datetime_timezone.utc))
        self.movement('LOCAL-MORNING')
        self.assertEqual(len(self.groups()), 1)
        response = self.client.get(self.page + '&used_start=2026-09-11&used_end=2026-09-11&used_search=SEAL002')
        self.assertEqual(response.context['grouped_history'][0]['quantity'], 10)
        response = self.client.get(self.page + '&used_search=missing')
        self.assertEqual(response.context['grouped_count'], 0)

    def test_regular_view_has_only_grouped_rows_and_no_edit_controls(self):
        self.movement('ONE')
        self.movement('TWO')
        staff = User.objects.create_user(username='group-staff')
        staff.profile.role = 'STAFF'
        staff.profile.can_wms_storage_expiry = True
        staff.profile.save()
        self.client.force_login(staff)
        response = self.client.get(self.page)
        self.assertContains(response, 'class="expiry-group-row"', count=1)
        self.assertNotContains(response, 'id="expiryDetailBody"')
        self.assertNotContains(response, 'expiry-edit-form')
