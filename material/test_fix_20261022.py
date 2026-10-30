"""2026-10-22 수정 회귀 테스트: ERP 이동 삭제 대기열, 이동요청 소수 승인, 안전재고 제안, 내부 보정 기록 숨김."""
import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from orders.models import Part
from .models import (ERPIncomingOperation, MaterialStock, MaterialTransaction, MaterialTransferRequest,
                     MaterialTransferRequestLine, RawMaterialSetting, Warehouse)
from .views import (api_raw_material_auto_stock, cancel_stock_move, stock_move, transfer_request_approve,
                    transfer_request_revoke, _internal_corrections)


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='fix-test', is_superuser=True)
        self.part = Part.objects.create(part_no='ZR700', part_name='Resin')
        self.src = Warehouse.objects.create(code='3200', name='Raw')
        self.dst = Warehouse.objects.create(code='3000', name='Feed')
        self.day = date(2026, 4, 1)

    def request(self, method='post', data=None, **kw):
        request = getattr(RequestFactory(), method)('/', data or {}, **kw)
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def qty(self, wh):
        return sum(MaterialStock.objects.filter(warehouse=wh, part=self.part).values_list('quantity', flat=True))


class MoveCancelOutboxTests(Base):
    def move(self, status, erp_no=''):
        MaterialStock.objects.create(warehouse=self.src, part=self.part, lot_no=self.day, quantity=25)
        MaterialStock.objects.create(warehouse=self.dst, part=self.part, lot_no=self.day, quantity=25)
        trx = MaterialTransaction.objects.create(
            transaction_no='MV-1', transaction_type='TRANSFER', date=timezone.now(), part=self.part,
            quantity=25, lot_no=self.day, warehouse_from=self.src, warehouse_to=self.dst,
            erp_incoming_no=erp_no or None)
        ERPIncomingOperation.objects.create(kind='MOVE', transaction=trx, transaction_no=trx.transaction_no,
                                            status=status, erp_no=erp_no, payload={})
        return trx

    def cancel(self, trx):
        return json.loads(cancel_stock_move(self.request(), trx.pk).content)

    def test_unsent_move_is_cancelled_in_the_queue(self):
        trx = self.move('PENDING')
        self.assertTrue(self.cancel(trx)['success'])
        self.assertEqual(ERPIncomingOperation.objects.get(kind='MOVE').status, 'CANCELLED')
        self.assertFalse(ERPIncomingOperation.objects.filter(kind='MOVE_DEL').exists())
        self.assertEqual((self.qty(self.src), self.qty(self.dst)), (50, 0))

    @override_settings(ERP_OUTBOX_AUTODISPATCH=False)
    def test_registered_move_queues_a_visible_erp_delete(self):
        trx = self.move('SUCCESS', 'MV2610220001')
        self.assertTrue(self.cancel(trx)['success'])
        job = ERPIncomingOperation.objects.get(kind='MOVE_DEL')
        self.assertEqual((job.payload['moveNb'], job.status), ('MV2610220001', 'PENDING'))

    def test_uncertain_move_blocks_cancel_and_changes_nothing(self):
        trx = self.move('REVIEW')
        result = self.cancel(trx)
        self.assertFalse(result['success'])
        self.assertIn('먼저 확인', result['error'])
        self.assertEqual((self.qty(self.src), self.qty(self.dst)), (25, 25))
        self.assertTrue(MaterialTransaction.objects.filter(pk=trx.pk).exists())


@override_settings(ERP_OUTBOX_AUTODISPATCH=False)
class TransferRequestTests(Base):
    def setUp(self):
        super().setUp()
        self.req = MaterialTransferRequest.objects.create(request_no='REQ-1', requested_by=self.user)
        self.line = MaterialTransferRequestLine.objects.create(request=self.req, part=self.part, requested_qty=13)
        self.lot = MaterialStock.objects.create(warehouse=self.src, part=self.part, lot_no=self.day,
                                                quantity=Decimal('12.5'))

    def approve(self, qty):
        return transfer_request_approve(self.request(data={
            'warehouse_from': self.src.pk, 'warehouse_to': self.dst.pk,
            f'process_{self.line.pk}': 'on', f'lot_{self.line.pk}': f'stock:{self.lot.pk}',
            f'qty_{self.line.pk}': qty}), self.req.pk)

    def test_fraction_only_as_whole_remaining_lot(self):
        self.approve('0.3')
        self.assertEqual(self.qty(self.dst), 0)  # 전량이 아닌 소수는 거절
        self.approve('12.5')
        self.line.refresh_from_db()
        self.assertEqual((self.qty(self.dst), self.line.approved_qty), (Decimal('12.5'), Decimal('12.5')))

    def test_revoke_of_registered_move_queues_erp_delete(self):
        with patch('material.erp_api.register_erp_stock_move', return_value=(True, None, None)):
            self.approve('12')
        trx = MaterialTransaction.objects.get()
        ERPIncomingOperation.objects.create(kind='MOVE', transaction=trx, status='SUCCESS', erp_no='MV-9', payload={})
        transfer_request_revoke(self.request(), self.req.pk)
        self.assertEqual(ERPIncomingOperation.objects.get(kind='MOVE_DEL').payload['moveNb'], 'MV-9')
        self.assertEqual(self.qty(self.src), Decimal('12.5'))


class SafetyStockSuggestionTests(Base):
    def test_decimal_usage_over_1000_per_month_is_not_capped(self):
        RawMaterialSetting.objects.create(part=self.part)
        MaterialTransaction.objects.create(transaction_no='ISU', transaction_type='ISU_ERP', date=timezone.now(),
                                           part=self.part, quantity=Decimal('-9000.5'), warehouse_from=self.dst)
        response = api_raw_material_auto_stock(self.request('get'))
        row = json.loads(response.content)['results'][0]
        self.assertEqual((row['suggested_safety'], row['suggested_warning']), (1200, 900))


class InternalCorrectionFilterTests(Base):
    def test_old_auto_corrections_hidden_real_erp_adjustments_kept(self):
        common = dict(date=timezone.now(), part=self.part, quantity=1, warehouse_to=self.dst)
        MaterialTransaction.objects.create(transaction_no='A', transaction_type='ADJ_ERP_IN',
                                           remark='ERP 재고동기화 (ERP=1)', **common)
        MaterialTransaction.objects.create(transaction_no='B', transaction_type='LOT_CORRECT',
                                           remark='ERP정합 LOT축소 (ERP=1)', **common)
        MaterialTransaction.objects.create(transaction_no='C', transaction_type='ADJ_ERP_IN',
                                           erp_incoming_no='IA2610000001-1', remark='ERP 입고조정', **common)
        MaterialTransaction.objects.create(transaction_no='D', transaction_type='LOT_CORRECT',
                                           remark='LOT 수정: 2026-04-01', **common)
        kept = MaterialTransaction.objects.exclude(_internal_corrections()).values_list('transaction_no', flat=True)
        self.assertEqual(sorted(kept), ['C', 'D'])


@override_settings(ERP_ENABLED=True, ERP_OUTBOX_AUTODISPATCH=False)
class ScmOnlyWarehouseTests(Base):
    def test_moves_to_or_from_8100_8200_never_reach_erp(self):
        from .erp_api import register_erp_stock_move
        trx = MaterialTransaction.objects.create(transaction_no='NG', transaction_type='TRANSFER',
                                                 date=timezone.now(), part=self.part, quantity=5)
        for pair in (('3200', '8200'), ('8100', '3200')):
            self.assertEqual(register_erp_stock_move(trx, 5, *pair), (True, None, None))
        self.assertFalse(ERPIncomingOperation.objects.exists())
        register_erp_stock_move(trx, 5, '3200', '3000')
        self.assertEqual(ERPIncomingOperation.objects.get().kind, 'MOVE')


class InspectionWaitingWarehouseTests(Base):
    """1000 수입검사 대기장: SCM 전용 창고로 명시, 합계 0인 상쇄 행은 목록·정리 대상."""

    def test_1000_is_scm_only_and_never_sent_to_erp(self):
        from .stock_reconciliation import SCM_ONLY_WAREHOUSE_CODES
        from .erp_api import register_erp_stock_move
        self.assertIn('1000', SCM_ONLY_WAREHOUSE_CODES)
        trx = MaterialTransaction.objects.create(transaction_no='W1', transaction_type='TRANSFER',
                                                 date=timezone.now(), part=self.part, quantity=5)
        with override_settings(ERP_ENABLED=True, ERP_OUTBOX_AUTODISPATCH=False):
            self.assertEqual(register_erp_stock_move(trx, 5, '1000', '3200'), (True, None, None))
        self.assertFalse(ERPIncomingOperation.objects.exists())

    def test_stock_list_hides_items_whose_rows_cancel_out(self):
        from django.test import Client
        from django.urls import reverse
        waiting = Warehouse.objects.create(code='1000', name='Waiting')
        MaterialStock.objects.create(warehouse=waiting, part=self.part, quantity=850)
        MaterialStock.objects.create(warehouse=waiting, part=self.part, lot_no=date(2026, 5, 17), quantity=-850)
        MaterialStock.objects.create(warehouse=self.src, part=self.part, lot_no=self.day, quantity=575)
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse('material:stock_list'), {'q': 'ZR700', 'search_triggered': 'yes'})
        shown = [(row['warehouse__code'], row['total_qty']) for row in response.context['stocks']]
        self.assertEqual(shown, [('3200', 575)])  # 1000의 +850/-850은 합계 0이라 표시 안 함

    def test_cleanup_migration_removes_only_zero_sum_rows(self):
        from importlib import import_module
        from django.apps import apps
        waiting = Warehouse.objects.create(code='1000', name='Waiting')
        other = Part.objects.create(part_no='ZR711', part_name='PP')
        MaterialStock.objects.create(warehouse=waiting, part=self.part, quantity=850)
        MaterialStock.objects.create(warehouse=waiting, part=self.part, lot_no=date(2026, 5, 17), quantity=-850)
        MaterialStock.objects.create(warehouse=waiting, part=other, lot_no=date(2026, 10, 22), quantity=100)
        MaterialStock.objects.create(warehouse=waiting, part=other, lot_no=date(2026, 5, 4), quantity=0)
        import_module('material.migrations.0066_clean_inspection_waiting_rows').forwards(apps, None)
        self.assertEqual(list(MaterialStock.objects.filter(warehouse=waiting).values_list('part__part_no', 'quantity')),
                         [('ZR711', 100)])  # 실제 대기 재고는 그대로


class StockMovePerRowDateTests(Base):
    def test_each_row_keeps_the_date_it_was_added_with(self):
        lot = MaterialStock.objects.create(warehouse=self.src, part=self.part, lot_no=self.day, quantity=30)
        row = {'part_no[]': 'ZR700', 'stock_id[]': lot.pk, 'from_loc[]': '3200', 'to_loc[]': '3000',
               'label_ids[]': '[]', 'fifo_reason[]': ''}
        data = {k: [v, v] for k, v in row.items()}
        data.update({'move_qty[]': ['10', '5'], 'move_date[]': ['2026-10-21', '2026-10-25'],
                     'transfer_date': '2026-10-25'})
        with patch('material.erp_api.register_erp_stock_move', return_value=(True, None, None)):
            stock_move(self.request(data=data))
        dates = sorted((timezone.localtime(t.date).date().isoformat(), t.quantity)
                       for t in MaterialTransaction.objects.filter(transaction_type='TRANSFER'))
        self.assertEqual(dates, [('2026-10-21', 10), ('2026-10-25', 5)])
        self.assertEqual((self.qty(self.src), self.qty(self.dst)), (15, 15))


class InspectionHoldWarehouseTests(Base):
    """2026-10-29: 검사 대상 입고를 3000에 받아 ERP 현재고 반영 때 500→50으로 깎인 건."""
    def setUp(self):
        super().setUp()
        self.hold = Warehouse.objects.create(code='1000', name='수입검사 대기장')

    def test_inspection_receipt_goes_to_1000_and_chosen_warehouse_becomes_target(self):
        from qms.models import ImportInspection
        from .views import manual_incoming
        data = {'date': '2026-10-29', 'warehouse_id': self.dst.pk, 'needs_inspection': 'on',
                'part_ids[]': [self.part.pk], 'lot_nos[]': ['2026-10-29'], 'quantities[]': ['500']}
        manual_incoming(self.request(data=data))
        trx = MaterialTransaction.objects.get(transaction_type='IN_MANUAL')
        self.assertEqual(trx.warehouse_to, self.hold)
        self.assertEqual((self.qty(self.hold), self.qty(self.dst)), (500, 0))
        self.assertEqual(ImportInspection.objects.get(inbound_transaction=trx).target_warehouse_code, '3000')

    def test_reconcile_skips_parts_with_pending_inspection_in_erp_warehouse(self):
        from qms.models import ImportInspection
        from .stock_reconciliation import pending_part_ids
        trx = MaterialTransaction.objects.create(transaction_no='IN-MAN-T', transaction_type='IN_MANUAL',
                                                 date=timezone.now(), part=self.part, quantity=500,
                                                 warehouse_to=self.dst)
        inspection = ImportInspection.objects.create(inbound_transaction=trx, status='PENDING',
                                                     target_warehouse_code='3000')
        self.assertIn(self.part.pk, pending_part_ids(timezone.now()))
        inspection.status = 'APPROVED'
        inspection.save()
        self.assertNotIn(self.part.pk, pending_part_ids(timezone.now()))


class JudgedReceiptDateEditTests(Base):
    """2026-10-30: 판정 완료 입고도 이월 때문에 입고일자는 바꿀 수 있어야 한다 (수량·LOT은 불가)."""
    def setUp(self):
        super().setUp()
        from qms.models import ImportInspection
        from orders.models import Vendor
        self.hold = Warehouse.objects.create(code='1000', name='수입검사 대기장')
        self.good = Warehouse.objects.create(code='2000', name='자재창고')
        vendor = Vendor.objects.create(code='V1', name='Vendor', erp_code='V1')
        old = timezone.make_aware(timezone.datetime(2026, 10, 30))
        self.lot = date(2026, 10, 30)
        self.trx = MaterialTransaction.objects.create(
            transaction_no='IN-MAN-J', transaction_type='IN_MANUAL', date=old, part=self.part, quantity=1000,
            lot_no=self.lot, warehouse_to=self.hold, vendor=vendor, remark='[수입검사 대상]')
        self.move = MaterialTransaction.objects.create(
            transaction_no='TRX-OK-J', transaction_type='TRANSFER', date=old, part=self.part, quantity=1000,
            lot_no=self.lot, warehouse_from=self.hold, warehouse_to=self.good, vendor=vendor,
            remark='[수입검사] 양품 입고', source_incoming=self.trx, erp_incoming_no='RV-OLD')
        ImportInspection.objects.create(inbound_transaction=self.trx, lot_no=self.lot, status='APPROVED',
                                        qty_good=1000, target_warehouse_code='2000')
        MaterialStock.objects.create(warehouse=self.good, part=self.part, lot_no=self.lot, quantity=1000)

    def edit(self, **changes):
        from .views import edit_manual_incoming
        body = {'quantity': 1000, 'date': '2026-10-30', 'lot_no': '2026-10-30', 'remark': '[수입검사 대상]'}
        body.update(changes)
        request = RequestFactory().post('/', json.dumps(body), content_type='application/json')
        request.user = self.user
        return json.loads(edit_manual_incoming(request, self.trx.pk).content)

    def test_date_moves_receipt_and_inspection_transfer_and_reregisters_erp(self):
        with patch('material.erp_api.delete_erp_incoming', return_value=(True, None)) as delete, \
                patch('material.erp_api.register_erp_incoming', return_value=(True, 'RV-NEW', None)) as register:
            result = self.edit(date='2026-11-01')
        self.assertTrue(result['success'], result)
        delete.assert_called_once_with('RV-OLD')
        self.assertEqual(register.call_args.args[1:], (1000, '2000'))
        for trx in (self.trx, self.move):
            trx.refresh_from_db()
            self.assertEqual(timezone.localtime(trx.date).date(), date(2026, 11, 1))
        self.assertEqual(MaterialStock.objects.get(warehouse=self.good).quantity, 1000)  # 재고는 그대로

    def test_quantity_or_lot_change_is_still_blocked(self):
        with patch('material.erp_api.delete_erp_incoming') as delete:
            self.assertFalse(self.edit(quantity=900)['success'])
            self.assertFalse(self.edit(lot_no='2026-11-01')['success'])
            delete.assert_not_called()

    def test_failed_erp_delete_rolls_back_dates(self):
        with patch('material.erp_api.delete_erp_incoming', return_value=(False, 'closed')):
            self.assertFalse(self.edit(date='2026-11-01')['success'])
        self.move.refresh_from_db()
        self.assertEqual(timezone.localtime(self.move.date).date(), date(2026, 10, 30))
