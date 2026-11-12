"""합격 후 불량 등록 회귀 테스트. 화면(납품서 스캔 → 판정)으로 입고를 만든 뒤 불량을 등록한다.

운영 DB·ERP·외부 HTTP 없이 실행한다.
"""
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse

from material.models import MaterialStock, MaterialTransaction, Warehouse
from orders.models import DeliveryOrder, Incoming, ReturnLog, UserProfile
from orders.remaining import order_progress
from orders.returns import acknowledge_return, ship_return
from orders.test_scm_remodel import RemodelBase, messages_of
from qms.models import ImportInspection

D = Decimal


class LateDefectBase(RemodelBase):
    def setUp(self):
        super().setUp()
        self.hold = Warehouse.objects.create(code='8100', name='Inspection hold')
        self.store = Warehouse.objects.create(code='4200', name='Material')
        self.line = Warehouse.objects.create(code='4300', name='Line')
        self.bad = Warehouse.objects.create(code='8200', name='Nonconforming')
        self.po = self.order(qty=1000, erp='PO-LD')

    def stock(self, wh):
        row = MaterialStock.objects.filter(warehouse=wh, part=self.part, lot_no=self.today).first()
        return row.quantity if row else D(0)

    def receive(self, qty=1000, inspection=True):
        delivery = self.deliver(qty, order=self.po)
        self.client.force_login(self.admin)
        data = {'order_id': delivery.pk, 'inspection_needed': 'yes' if inspection else 'no'}
        data['target_warehouse_code' if inspection else 'direct_warehouse_code'] = '4200'
        self.client.post(reverse('receive_delivery_order_confirm'), data)
        origin = MaterialTransaction.objects.get(ref_delivery_order=delivery.order_no,
                                                 transaction_type__in=['IN_SCM', 'IN_MANUAL'])
        return delivery, origin

    def judge(self, origin, good, bad=0):
        inspection = ImportInspection.objects.get(inbound_transaction=origin)
        self.client.post(reverse('qms:import_inspection_detail', args=[inspection.pk]),
                         {'decision': 'COMPLETE', 'qty_good': good, 'qty_bad': bad, 'remark': 'test'})
        inspection.refresh_from_db()
        return inspection

    def move(self, src, dst, qty):
        row = MaterialStock.objects.get(warehouse=src, part=self.part, lot_no=self.today)
        MaterialStock.objects.filter(pk=row.pk).update(quantity=row.quantity - qty)
        target, _ = MaterialStock.objects.get_or_create(warehouse=dst, part=self.part, lot_no=self.today)
        MaterialStock.objects.filter(pk=target.pk).update(quantity=target.quantity + qty)
        MaterialTransaction.objects.create(
            transaction_no=f'MV-{uuid.uuid4().hex[:20]}', transaction_type='TRANSFER', part=self.part,
            lot_no=self.today, quantity=qty, warehouse_from=src, warehouse_to=dst)

    def defect(self, origin, wh, qty, reason='치수 불량', user=None, key=None, follow=True):
        self.client.force_login(user or self.admin)
        row = MaterialStock.objects.filter(warehouse=wh, part=self.part, lot_no=self.today).first()
        return self.client.post(reverse('material:late_defect', args=[origin.pk]), {
            'stock_id': row.pk if row else 0, 'quantity': qty, 'reason': reason,
            'request_key': key or str(uuid.uuid4())}, follow=follow)


class InspectedReceiptTests(LateDefectBase):
    def test_defect_in_the_receiving_warehouse(self):
        delivery, origin = self.receive()
        self.judge(origin, 1000)
        response = self.defect(origin, self.store, 5)
        self.assertTrue(any('불량 5개' in m for m in messages_of(response)), messages_of(response))
        self.assertEqual((self.stock(self.store), self.stock(self.bad)), (995, 5))
        inspection = ImportInspection.objects.get(inbound_transaction=origin)
        self.assertEqual((inspection.qty_good, inspection.qty_bad, inspection.status), (995, 5, 'APPROVED'))
        self.assertEqual(inspection.correction_history[-1]['type'], 'late_defect')
        case = ReturnLog.objects.get(source_incoming=origin)
        self.assertEqual((case.quantity, case.delivery_order_id), (5, delivery.pk))
        self.assertEqual(Incoming.objects.get(delivery_order_no=delivery.order_no).confirmed_qty, 995)
        self.assertTrue(MaterialTransaction.objects.filter(pk=origin.pk).exists())   # 입고 기록은 그대로
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'APPROVED')

    def test_defect_found_on_the_line_after_a_move(self):
        """시뮬레이션에서 세 경로가 모두 막혔던 상황: 300개를 현장으로 옮긴 뒤 불량 5개."""
        _, origin = self.receive()
        self.judge(origin, 1000)
        self.move(self.store, self.line, 300)
        self.defect(origin, self.line, 5)
        self.assertEqual((self.stock(self.store), self.stock(self.line), self.stock(self.bad)), (700, 295, 5))
        recall = MaterialTransaction.objects.get(transaction_no__startswith='LDM-')
        self.assertEqual((recall.warehouse_from, recall.warehouse_to, recall.quantity), (self.line, self.store, 5))
        isolate = MaterialTransaction.objects.get(transaction_no__startswith='LDF-')
        self.assertEqual((isolate.warehouse_from, isolate.warehouse_to), (self.store, self.bad))
        self.assertEqual(ReturnLog.objects.get(source_incoming=origin).quantity, 5)
        page = self.client.get(reverse('material:late_defect', args=[origin.pk]))
        self.assertEqual(page.context['history'][0].found_warehouse, self.line)   # 이력에는 발견한 창고가 보인다

    def test_return_flow_continues_to_po_remaining(self):
        _, origin = self.receive()
        self.judge(origin, 1000)
        self.defect(origin, self.store, 5)
        case = ReturnLog.objects.get(source_incoming=origin)
        shipment = ship_return(case.pk, 5, self.admin, str(uuid.uuid4()))
        self.assertEqual(self.stock(self.bad), 0)
        self.assertEqual(order_progress([self.po])[self.po.pk].remaining, 0)
        supplier = type(self.supplier).objects.get(pk=self.supplier.pk)   # 프로필 캐시 없이 다시 읽는다
        acknowledge_return(case.pk, shipment.pk, supplier)
        self.assertEqual(order_progress([self.po])[self.po.pk].remaining, 5)

    def test_second_defect_adds_to_the_same_return_case(self):
        _, origin = self.receive()
        self.judge(origin, 990, 10)                     # 검사 때 이미 불량 10
        self.defect(origin, self.store, 5)
        case = ReturnLog.objects.get(source_incoming=origin)
        self.assertEqual(case.quantity, 15)
        inspection = ImportInspection.objects.get(inbound_transaction=origin)
        self.assertEqual((inspection.qty_good, inspection.qty_bad), (985, 15))
        self.assertEqual(self.stock(self.bad), 15)

    def test_limits_and_duplicates(self):
        _, origin = self.receive()
        self.judge(origin, 1000)
        self.move(self.store, self.line, 300)
        response = self.defect(origin, self.line, 301)            # 그 창고 재고보다 많음
        self.assertTrue(any('부족' in m for m in messages_of(response)), messages_of(response))
        key = str(uuid.uuid4())
        self.defect(origin, self.store, 5, key=key)
        again = self.defect(origin, self.store, 5, key=key)        # 같은 요청 재전송
        self.assertTrue(any('이미 처리된' in m for m in messages_of(again)))
        self.assertEqual(self.stock(self.bad), 5)
        self.assertEqual(ReturnLog.objects.get(source_incoming=origin).quantity, 5)
        self.defect(origin, self.store, 5, reason='   ')           # 사유 없음
        self.assertEqual(self.stock(self.bad), 5)

    def test_pending_inspection_is_sent_to_the_inspection_screen(self):
        _, origin = self.receive()
        self.client.force_login(self.admin)
        page = self.client.get(reverse('material:late_defect', args=[origin.pk]))
        self.assertContains(page, '아직 검사 판정 전입니다')
        self.defect(origin, self.hold, 5)
        self.assertEqual(self.stock(self.bad), 0)

    def test_cancel_and_correction_stay_blocked_afterwards(self):
        _, origin = self.receive()
        inspection = self.judge(origin, 1000)
        self.defect(origin, self.store, 5)
        self.client.post(reverse('qms:import_inspection_detail', args=[inspection.pk]),
                         {'decision': 'CANCEL', 'correction_reason': '되돌리기 시도'})
        inspection.refresh_from_db()
        self.assertEqual((inspection.status, inspection.qty_good), ('APPROVED', 995))
        self.assertEqual(self.stock(self.bad), 5)


class UninspectedReceiptTests(LateDefectBase):
    def test_direct_receipt_gets_a_defect_record_without_cancelling(self):
        delivery, origin = self.receive(inspection=False)
        self.assertFalse(ImportInspection.objects.exists())
        self.defect(origin, self.store, 7)
        self.assertEqual((self.stock(self.store), self.stock(self.bad)), (993, 7))
        self.assertEqual(ReturnLog.objects.get(source_incoming=origin).quantity, 7)
        self.assertEqual(Incoming.objects.get(delivery_order_no=delivery.order_no).confirmed_qty, 993)
        delivery.refresh_from_db()
        self.assertTrue(delivery.is_received)                      # 납품서는 입고완료로 남는다
        self.defect(origin, self.store, 3)
        self.assertEqual(ReturnLog.objects.get(source_incoming=origin).quantity, 10)
        response = self.defect(origin, self.store, 991)            # 남은 양품 990개 초과
        self.assertTrue(any('남은 양품' in m for m in messages_of(response)), messages_of(response))


class PermissionAndGuardTests(LateDefectBase):
    def test_vendor_and_unprivileged_staff_cannot_register(self):
        _, origin = self.receive()
        self.judge(origin, 1000)
        UserProfile.objects.filter(user=self.supplier).update(can_wms_stock_return=True)
        self.defect(origin, self.store, 5, user=self.supplier)
        self.defect(origin, self.store, 5, user=self.staff('no-perm-staff'))
        self.assertEqual(self.stock(self.bad), 0)
        self.defect(origin, self.store, 5, user=self.staff('qc', can_qms_inspection_edit=True))
        self.assertEqual(self.stock(self.bad), 5)

    def test_manual_move_into_nonconforming_warehouse_is_refused(self):
        _, origin = self.receive()
        self.judge(origin, 1000)
        row = MaterialStock.objects.get(warehouse=self.store, part=self.part, lot_no=self.today)
        self.client.force_login(self.admin)
        response = self.client.post(reverse('material:stock_move'), {
            'part_no[]': [self.part.part_no], 'stock_id[]': [row.pk], 'from_loc[]': ['4200'], 'to_loc[]': ['8200'],
            'move_qty[]': ['5'], 'label_ids[]': ['[]'], 'fifo_reason[]': [''], 'move_date[]': ['']}, follow=True)
        self.assertTrue(any('불량 등록' in m for m in messages_of(response)), messages_of(response))
        self.assertEqual((self.stock(self.store), self.stock(self.bad)), (1000, 0))


@override_settings(ERP_ENABLED=True)
class ErpQuantityTests(LateDefectBase):
    """ERP에 등록된 입고는 삭제 후 줄어든 수량으로 다시 등록 요청한다 (판정 정정과 같은 방식)."""

    def setUp(self):
        super().setUp()
        self.register = patch('material.erp_api.register_erp_incoming', return_value=(True, 'RV-NEW', None)).start()
        self.delete = patch('material.erp_api.delete_erp_incoming', return_value=(True, None)).start()
        self.erp_move = patch('material.erp_api.register_erp_stock_move', return_value=(True, 'MV-1', None)).start()
        self.addCleanup(patch.stopall)

    def test_receipt_quantity_is_reduced_and_recall_move_is_sent(self):
        _, origin = self.receive()
        self.judge(origin, 1000)
        ok_move = MaterialTransaction.objects.get(source_incoming=origin, remark__startswith='[수입검사] 양품')
        MaterialTransaction.objects.filter(pk=ok_move.pk).update(erp_incoming_no='RV-OLD', erp_sync_status='SUCCESS')
        self.register.reset_mock()
        self.move(self.store, self.line, 300)
        self.defect(origin, self.line, 5)
        self.delete.assert_called_once_with('RV-OLD')
        args, kwargs = self.register.call_args
        self.assertEqual((args[0].pk, args[1], args[2]), (ok_move.pk, 995, '4200'))
        self.assertEqual(kwargs['erp_order_no'], 'PO-LD')
        move_args = self.erp_move.call_args[0]
        self.assertEqual((move_args[1], move_args[2], move_args[3]), (5, '4300', '4200'))

    def test_erp_delete_failure_rolls_everything_back(self):
        _, origin = self.receive()
        self.judge(origin, 1000)
        ok_move = MaterialTransaction.objects.get(source_incoming=origin, remark__startswith='[수입검사] 양품')
        MaterialTransaction.objects.filter(pk=ok_move.pk).update(erp_incoming_no='RV-OLD', erp_sync_status='SUCCESS')
        self.delete.return_value = (False, 'closed')
        response = self.defect(origin, self.store, 5)
        self.assertTrue(any('ERP 입고 수량을 줄이지 못했습니다' in m for m in messages_of(response)))
        self.assertEqual((self.stock(self.store), self.stock(self.bad)), (1000, 0))
        self.assertFalse(ReturnLog.objects.exists())
        self.assertEqual(ImportInspection.objects.get(inbound_transaction=origin).qty_good, 1000)


@override_settings(ERP_ENABLED=True)
class PriceReregistrationTests(LateDefectBase):
    """단가 재반영: 불량 등록으로 줄어든 수량 유지, 단가 1회 조회, 직접 입력 확인·이력, 입고 건만 대상."""

    def setUp(self):
        super().setUp()
        from orders.models import Vendor
        Vendor.objects.filter(pk=self.vendor.pk).update(erp_code='TR-A')
        self.api = patch('material.erp_api.call_erp_api', side_effect=AssertionError('ERP call inside request')).start()
        self.price = patch('material.erp_api.fetch_erp_item_price', return_value=(100, 110)).start()
        self.addCleanup(patch.stopall)

    def reprice(self, trx, **body):
        import json
        self.client.force_login(self.admin)
        return self.client.post(reverse('material:reregister_erp_price', args=[trx.pk]),
                                json.dumps(body), content_type='application/json').json()

    def jobs(self, trx):
        from material.models import ERPIncomingOperation
        return ERPIncomingOperation.objects.filter(transaction=trx, kind='REGISTER').order_by('created_at')

    def accepted_receipt(self):
        _, origin = self.receive()
        self.judge(origin, 1000)
        ok_move = MaterialTransaction.objects.get(source_incoming=origin, remark__startswith='[수입검사] 양품')
        return origin, ok_move

    def test_quantity_reduced_by_a_late_defect_is_kept(self):
        origin, ok_move = self.accepted_receipt()
        self.defect(origin, self.store, 5)
        self.price.reset_mock()
        result = self.reprice(ok_move, use_integrated_only=True)
        self.assertTrue(result['success'], result)
        detail = self.jobs(ok_move).last().payload['detail'][0]
        self.assertEqual((detail['rcvQt'], detail['rcvUm'], detail['rcvgAm']), (995, 100, 99500))
        self.assertEqual(self.price.call_count, 1)             # 확인용·등록용으로 두 번 조회하지 않는다

    def test_manual_price_is_logged_and_large_changes_need_confirmation(self):
        from material.models import ERPIncomingEvent
        _, ok_move = self.accepted_receipt()
        self.price.reset_mock()                                  # 검사 판정 때의 최초 등록 조회는 세지 않는다
        first = self.reprice(ok_move, unit_price='120')
        self.assertTrue(first['success'], first)
        self.assertEqual(self.jobs(ok_move).last().payload['detail'][0]['rcvUm'], 120)
        event = ERPIncomingEvent.objects.filter(action='PRICE').latest('pk')
        self.assertEqual(event.actor, self.admin)
        self.assertIn('120', event.message)

        typo = self.reprice(ok_move, unit_price='12000')        # 0을 두 개 더 붙임
        self.assertFalse(typo['success'])
        self.assertTrue(typo['need_confirm'])
        self.assertEqual(self.jobs(ok_move).last().payload['detail'][0]['rcvUm'], 120)   # 바뀌지 않음
        confirmed = self.reprice(ok_move, unit_price='12000', confirmed=True)
        self.assertTrue(confirmed['success'], confirmed)
        self.assertEqual(self.jobs(ok_move).last().payload['detail'][0]['rcvUm'], 12000)
        self.price.assert_not_called()                           # 직접 입력은 ERP 단가를 조회하지 않는다

    def test_invalid_prices_are_refused(self):
        _, ok_move = self.accepted_receipt()
        for bad in ('abc', '0', '-5', '1.23456'):
            with self.subTest(price=bad):
                self.assertFalse(self.reprice(ok_move, unit_price=bad)['success'])

    def test_only_receipts_can_be_repriced(self):
        _, origin = self.receive()
        self.judge(origin, 1000)
        self.move(self.store, self.line, 300)
        plain_move = MaterialTransaction.objects.get(transaction_no__startswith='MV-')
        result = self.reprice(plain_move, unit_price='120')
        self.assertFalse(result['success'])
        self.assertIn('입고 건만', result['error'])
        self.assertFalse(self.jobs(plain_move).exists())

    def test_erp_delete_refusal_keeps_the_existing_request(self):
        _, ok_move = self.accepted_receipt()
        self.reprice(ok_move, unit_price='120')
        pending = self.jobs(ok_move).last()
        MaterialTransaction.objects.filter(pk=ok_move.pk).update(erp_incoming_no='RV-OLD')
        type(pending).objects.filter(pk=pending.pk).update(status='FAILED')
        with patch('material.erp_api.delete_erp_incoming', return_value=(False, 'closed')):
            result = self.reprice(ok_move, unit_price='130')
        self.assertFalse(result['success'])
        pending.refresh_from_db()
        self.assertEqual(pending.status, 'FAILED')              # 취소로 바뀐 채 남지 않는다
        ok_move.refresh_from_db()
        self.assertEqual(ok_move.erp_incoming_no, 'RV-OLD')
