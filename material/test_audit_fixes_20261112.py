"""2026-11-12 점검 후속 수정(B 항목) 회귀 테스트. 운영 DB·ERP·외부 HTTP 없이 실행한다."""
import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from orders.models import Part
from .labels import sync_labels
from .models import (LotConsumption, MaterialStock, MaterialTransaction, ProcessTag, ProcessTagScanLog,
                     RawMaterialLabel, Warehouse)
from .stock_reconciliation import _settle_lot_managed, lot_managed_part_ids

D = Decimal


@override_settings(ERP_ENABLED=False, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('audit-admin', is_superuser=True, is_staff=True)
        self.client.force_login(self.user)
        self.part = Part.objects.create(part_no='ZR700', part_name='Resin')
        self.raw = Warehouse.objects.create(code='3200', name='Raw')
        self.feed = Warehouse.objects.create(code='3000', name='Feed')
        self.other = Warehouse.objects.create(code='4300', name='Line')
        self.lot = date(2026, 10, 1)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('External HTTP prohibited'))
        network.start()
        self.addCleanup(network.stop)

    def stock(self, wh, qty, lot='default', batch=None):
        return MaterialStock.objects.create(warehouse=wh, part=self.part, quantity=qty,
                                            lot_no=self.lot if lot == 'default' else lot, production_lot=batch)

    def qty(self, wh, lot='default'):
        lot = self.lot if lot == 'default' else lot
        row = MaterialStock.objects.filter(warehouse=wh, part=self.part, lot_no=lot).first()
        return row.quantity if row else D(0)

    def lot_change(self, stock, new_date, new_qty):
        return self.client.post(reverse('material:api_lot_change'), json.dumps(
            {'stock_id': stock.pk, 'new_lot_date': new_date.isoformat(), 'new_qty': new_qty}),
            content_type='application/json')


class LotDateChangeTests(Base):
    """B1: LOT 날짜를 바꾸면 그 LOT의 라벨도 함께 옮겨져 소진 처리되지 않는다."""

    def labels(self, n, wh):
        return [RawMaterialLabel.objects.create(
            label_id=f'RM-A-{i:04d}', part=self.part, part_no='ZR700', part_name='Resin',
            lot_no=self.lot, quantity=25, status='INSTOCK', warehouse=wh) for i in range(n)]

    def test_live_labels_follow_the_new_lot_date(self):
        stock = self.stock(self.raw, 50)
        self.labels(2, self.raw)
        new_date = date(2026, 10, 2)
        response = self.lot_change(stock, new_date, 50)
        self.assertTrue(response.json()['success'], response.content)
        self.assertEqual(set(RawMaterialLabel.objects.values_list('lot_no', flat=True)), {new_date})
        self.assertEqual(sync_labels(self.part), (0, 0))          # 다음 재고 맞추기에서도 소진되지 않는다
        self.assertEqual(set(RawMaterialLabel.objects.values_list('status', flat=True)), {'INSTOCK'})

    def test_labels_in_another_warehouse_are_left_alone(self):
        stock = self.stock(self.raw, 50)
        self.stock(self.feed, 25)
        raw_labels = self.labels(2, self.raw)
        feed_label = RawMaterialLabel.objects.create(
            label_id='RM-A-FEED', part=self.part, part_no='ZR700', part_name='Resin',
            lot_no=self.lot, quantity=25, status='INSTOCK', warehouse=self.feed)
        self.lot_change(stock, date(2026, 10, 2), 50)
        feed_label.refresh_from_db()
        self.assertEqual(feed_label.lot_no, self.lot)
        raw_labels[0].refresh_from_db()
        self.assertEqual(raw_labels[0].lot_no, date(2026, 10, 2))

    def test_batch_is_kept_when_the_date_changes(self):
        stock = self.stock(self.raw, 10, batch='26401-01')
        self.lot_change(stock, date(2026, 10, 2), 10)
        moved = MaterialStock.objects.get(warehouse=self.raw, part=self.part, lot_no=date(2026, 10, 2))
        self.assertEqual(moved.production_lot, '26401-01')


class LotCorrectionInputTests(Base):
    """B7: LOT 수정은 소수를 자르지 않고, 다른 창고의 LOT를 건드리지 않는다."""

    def test_decimal_quantity_is_not_truncated(self):
        stock = self.stock(self.raw, D('20'))
        response = self.lot_change(stock, self.lot, '12.5')
        self.assertTrue(response.json()['success'], response.content)
        self.assertEqual(self.qty(self.raw), D('12.5'))
        self.assertEqual(self.qty(self.raw, lot=None), D('7.5'))    # 차이는 그대로 LOT 미지정으로

    def test_unassigned_bucket_ignores_undated_batches(self):
        stock = self.stock(self.raw, 20)
        batch_only = self.stock(self.raw, 5, lot=None, batch='26401-01')   # 날짜 없는 배치 행은 버킷이 아니다
        self.lot_change(stock, self.lot, 15)
        batch_only.refresh_from_db()
        self.assertEqual(batch_only.quantity, 5)
        bucket = MaterialStock.objects.get(warehouse=self.raw, part=self.part,
                                           lot_no__isnull=True, production_lot__isnull=True)
        self.assertEqual(bucket.quantity, 5)

    def test_correction_rejects_a_lot_from_another_warehouse(self):
        foreign = self.stock(self.feed, 30)
        self.client.post(reverse('material:lot_allocation'), {
            'action': 'lot_correct', 'warehouse_id': self.raw.pk,
            'stock_ids[]': [foreign.pk], 'new_qtys[]': ['10']})
        foreign.refresh_from_db()
        self.assertEqual(foreign.quantity, 30)
        self.assertFalse(MaterialStock.objects.filter(warehouse=self.raw, part=self.part).exists())
        self.assertFalse(MaterialTransaction.objects.filter(transaction_type='LOT_CORRECT').exists())

    def test_correction_keeps_decimal_quantity(self):
        stock = self.stock(self.raw, 30)
        self.client.post(reverse('material:lot_allocation'), {
            'action': 'lot_correct', 'warehouse_id': self.raw.pk,
            'stock_ids[]': [stock.pk], 'new_qtys[]': ['10.25']})
        self.assertEqual(self.qty(self.raw), D('10.25'))
        self.assertEqual(self.qty(self.raw, lot=None), D('19.75'))


class ProcessTagScanTests(Base):
    """B2: 같은 현품표를 연달아 읽어도 한 번만 처리된다."""

    def setUp(self):
        super().setUp()
        self.stock(self.raw, 100)
        self.tag = ProcessTag.objects.create(tag_id='TAG-A-0001', part=self.part, part_no='ZR700',
                                             part_name='Resin', quantity=25, lot_no=self.lot)

    def scan(self, target='3000'):
        return self.client.post(reverse('material:api_process_tag_scan'), json.dumps(
            {'tag_id': self.tag.tag_id, 'target_warehouse_code': target}),
            content_type='application/json').json()

    def test_double_scan_to_feed_moves_once_and_does_not_cancel(self):
        self.assertTrue(self.scan()['success'])
        second = self.scan()
        self.assertFalse(second['success'], second)
        self.assertEqual(self.qty(self.raw), 75)
        self.assertEqual(self.qty(self.feed), 25)
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.status, 'USED')       # 곧바로 투입취소되지 않는다

    def test_double_scan_to_other_warehouse_moves_once(self):
        self.assertTrue(self.scan('4300')['success'])
        self.assertFalse(self.scan('4300')['success'])
        self.assertEqual(self.qty(self.raw), 75)
        self.assertEqual(self.qty(self.other), 25)

    def test_cancel_scan_still_works_after_the_block_window(self):
        self.assertTrue(self.scan()['success'])
        old = timezone.now() - timezone.timedelta(minutes=5)
        ProcessTag.objects.filter(pk=self.tag.pk).update(used_at=old)
        ProcessTagScanLog.objects.filter(tag=self.tag).update(scanned_at=old)
        cancelled = self.scan()
        self.assertTrue(cancelled['success'], cancelled)
        self.assertEqual(self.qty(self.raw), 100)
        self.assertEqual(self.qty(self.feed), 0)
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.status, 'PRINTED')

    def test_moved_quantity_is_stored_as_exact_decimal(self):
        self.scan()
        trx = MaterialTransaction.objects.get(transaction_type='TRANSFER')
        self.assertEqual(trx.quantity, D('25'))


class ManualOutgoingCancelTests(Base):
    """B3: 수기출고 취소는 재고를 한 번만 복원한다."""

    def outgoing(self, kind='OUT_MANUAL'):
        self.stock(self.raw, 70)
        return MaterialTransaction.objects.create(
            transaction_no=f'OUT-T-{kind}', transaction_type=kind, part=self.part, quantity=-30,
            lot_no=self.lot, warehouse_from=self.raw, date=timezone.now(), actor=self.user)

    def test_cancel_restores_stock_once(self):
        trx = self.outgoing()
        url = reverse('material:cancel_manual_outgoing', args=[trx.pk])
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(self.qty(self.raw), 100)
        self.assertFalse(MaterialTransaction.objects.filter(pk=trx.pk).exists())

    def test_stale_request_does_not_restore_again(self):
        """삭제된 출고 건을 들고 있던 요청이 뒤늦게 실행돼도 재고를 더하지 않는다."""
        from . import views
        trx = self.outgoing()
        with patch.object(views, 'get_object_or_404', return_value=trx):
            MaterialTransaction.objects.filter(pk=trx.pk).delete()   # 다른 요청이 먼저 취소
            self.client.post(reverse('material:cancel_manual_outgoing', args=[trx.pk]))
        self.assertEqual(self.qty(self.raw), 70)


class LotManagedFlagTests(Base):
    """B6: 한 번 LOT 관리로 판정된 품목은 이력이 지워져도 LOT 관리로 남는다."""

    def test_flag_survives_deleting_the_only_receipt(self):
        receipt = MaterialTransaction.objects.create(
            transaction_no='IN-T-1', transaction_type='IN_MANUAL', part=self.part, quantity=10,
            lot_no=self.lot, warehouse_to=self.raw, date=timezone.now())
        self.assertIn(self.part.pk, lot_managed_part_ids(remember=True))
        self.part.refresh_from_db()
        self.assertTrue(self.part.lot_managed)
        receipt.delete()
        self.assertIn(self.part.pk, lot_managed_part_ids())

    def test_reading_does_not_set_the_flag(self):
        MaterialTransaction.objects.create(
            transaction_no='IN-T-2', transaction_type='IN_MANUAL', part=self.part, quantity=10,
            lot_no=self.lot, warehouse_to=self.raw, date=timezone.now())
        lot_managed_part_ids()
        self.part.refresh_from_db()
        self.assertFalse(self.part.lot_managed)


class LotConsumptionRecordTests(Base):
    """B4: 재고 맞추기의 선입선출 소진이 LOT별로 기록된다."""

    def test_fifo_consumption_is_recorded_per_lot(self):
        old = self.stock(self.feed, 30, lot=date(2026, 9, 1))
        new = self.stock(self.feed, 50, lot=date(2026, 10, 1))
        expected = {s.pk: (s.quantity, s.lot_no, s.production_lot) for s in (old, new)}
        _settle_lot_managed(self.feed, self.part, D('40'), expected)   # ERP 40 → 40개 소진
        rows = list(LotConsumption.objects.order_by('lot_no').values_list('lot_no', 'quantity', 'remaining'))
        self.assertEqual(rows, [(date(2026, 9, 1), D('30'), D('0')), (date(2026, 10, 1), D('10'), D('40'))])
        self.assertEqual(self.qty(self.feed, lot=date(2026, 10, 1)), 40)

    def test_no_record_without_consumption(self):
        only = self.stock(self.feed, 30)
        _settle_lot_managed(self.feed, self.part, D('35'), {only.pk: (only.quantity, only.lot_no, None)})
        self.assertFalse(LotConsumption.objects.exists())
