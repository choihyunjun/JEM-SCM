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
from .views import (api_raw_material_auto_stock, cancel_stock_move, transfer_request_approve,
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
