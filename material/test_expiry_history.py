import json
from datetime import date, datetime, timezone as datetime_timezone

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from orders.models import Part
from .expiry import expiry_movement_history
from .models import (
    MaterialStock, MaterialTransaction, ProcessTag, RawMaterialLabel,
    RawMaterialSetting, Warehouse,
)
from .views import cancel_stock_move, raw_material_expiry


@override_settings(TIME_ZONE='Asia/Seoul', USE_TZ=True)
class ExpiryMovementHistoryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='expiry-test', is_superuser=True)
        self.part = Part.objects.create(part_no='SEAL002', part_name='Seal test')
        self.setting = RawMaterialSetting.objects.create(part=self.part, shelf_life_days=90)
        self.settings_map = {self.part.pk: self.setting}
        self.source = Warehouse.objects.create(code='3200', name='Material')
        self.target = Warehouse.objects.create(code='3000', name='Production', is_production=True)
        self.day = date(2026, 8, 22)
        self.moved_at = timezone.make_aware(datetime(2026, 9, 14, 17, 13))

    def movement(self, number='MOVE-1', **overrides):
        values = dict(
            transaction_no=number, transaction_type='TRANSFER', part=self.part,
            quantity=10, lot_no=self.day, date=self.moved_at,
            warehouse_from=self.source, warehouse_to=self.target, actor=self.user,
        )
        values.update(overrides)
        return MaterialTransaction.objects.create(**values)

    def label(self, number='RM-1', **overrides):
        values = dict(
            label_id=number, part=self.part, part_no=self.part.part_no,
            part_name=self.part.part_name, lot_no=self.day, quantity=5,
            status='USED', used_at=self.moved_at, used_by=self.user,
        )
        values.update(overrides)
        return RawMaterialLabel.objects.create(**values)

    def rows(self, **filters):
        return expiry_movement_history(self.settings_map, **filters)

    def test_only_requested_direction_pairs_for_scm_and_erp_without_current_stock(self):
        fourth_source = Warehouse.objects.create(code='4200', name='Fourth material')
        fourth_target = Warehouse.objects.create(code='4300', name='Fourth production')
        other = Warehouse.objects.create(code='2000', name='Other storage')
        expected = set()
        for kind in ('TRANSFER', 'TRF_ERP'):
            routes = [
                (self.source, self.target), (fourth_source, fourth_target), (other, self.target),
                (self.target, self.source), (fourth_target, fourth_source),
                (self.source, fourth_target), (fourth_source, self.target),
                (self.target, other), (other, fourth_target), (self.source, other), (None, self.target),
            ]
            for index, (source, target) in enumerate(routes):
                number = f'{kind}-{index}'
                self.movement(number, transaction_type=kind, warehouse_from=source, warehouse_to=target)
                if index < 3:
                    expected.add(number)
        self.movement('RECEIPT', transaction_type='IN_SCM')
        rows = self.rows()
        self.assertEqual({r['transaction_no'] for r in rows}, expected)
        self.assertFalse(MaterialStock.objects.exists())
        self.assertTrue(all(r['expiry_date'] == date(2026, 11, 20) for r in rows))
        self.assertTrue(all(r['used_d_day'] == 67 for r in rows))

    def test_only_expiry_configured_parts_are_included(self):
        other = Part.objects.create(part_no='OTHER', part_name='Other')
        self.movement(part=other)
        self.label(part=other, part_no=other.part_no)
        self.assertEqual(self.rows(), [])
        self.assertEqual(expiry_movement_history({}), [])

    def test_multiple_labels_and_tag_do_not_multiply_movement_quantity(self):
        trx = self.movement(quantity=20)
        self.label('RM-A', used_transaction=trx)
        self.label('RM-B', used_transaction=trx)
        ProcessTag.objects.create(
            tag_id='TAG-A', part=self.part, part_no=self.part.part_no,
            part_name=self.part.part_name, quantity=10, lot_no=self.day,
            status='USED', used_at=self.moved_at, used_transaction=trx,
        )
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['quantity'], 20)
        self.assertCountEqual(rows[0]['label_ids'], ['RM-A', 'RM-B', 'TAG-A'])

    def test_labels_without_a_route_are_excluded_without_changing_them(self):
        self.label('SAVED', expiry_date=date(2026, 10, 1))
        label = self.label('MISSING')
        self.label('CANCELLED', status='CANCELLED')
        self.assertEqual(self.rows(), [])
        label.refresh_from_db()
        self.assertEqual(label.status, 'USED')
        self.assertIsNone(label.expiry_date)

    def test_missing_lot_is_not_guessed_from_current_stock(self):
        self.movement(transaction_type='TRF_ERP', lot_no=None)
        MaterialStock.objects.create(warehouse=self.target, part=self.part, lot_no=self.day, quantity=10)
        row = self.rows()[0]
        self.assertIsNone(row['lot_no'])
        self.assertIsNone(row['expiry_date'])
        self.assertIsNone(row['used_d_day'])

    def test_filters_use_local_movement_day_and_do_not_reintroduce_linked_labels(self):
        # 9/14 UTC is already 9/15 in Korea.
        timestamp = datetime(2026, 9, 14, 16, tzinfo=datetime_timezone.utc)
        self.movement(date=timestamp)
        old = self.movement('OLD', date=self.moved_at)
        self.label(used_transaction=old, used_at=timestamp)
        self.label('LEGACY', used_at=timestamp)
        rows = self.rows(search='Seal test', start='2026-09-15', end='2026-09-15')
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(r['used_d_day'] == 66 for r in rows))
        self.assertEqual(self.rows(search='unmatched'), [])

    def test_cancellation_removes_movement_and_restores_label_without_ghost_history(self):
        trx = self.movement()
        label = self.label(used_transaction=trx)
        MaterialStock.objects.create(warehouse=self.target, part=self.part, lot_no=self.day, quantity=10)
        request = RequestFactory().post('/cancel/')
        request.user = self.user
        result = json.loads(cancel_stock_move(request, trx.pk).content)
        self.assertTrue(result['success'], result)
        self.assertEqual(self.rows(), [])
        label.refresh_from_db()
        self.assertEqual(label.status, 'INSTOCK')
        self.assertEqual(MaterialStock.objects.get(warehouse=self.source).quantity, 10)

    def test_page_renders_movement_number_direction_batch_and_unknown_expiry(self):
        self.movement(lot_no=None, production_lot='BATCH-1')
        request = RequestFactory().get('/expiry/', {'tab': 'used'})
        request.user = self.user
        response = raw_material_expiry(request)
        for text in ('MOVE-1', 'Material → Production', 'BATCH-1', '확인 불가'):
            self.assertContains(response, text)
