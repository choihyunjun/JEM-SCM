"""2026-11-12 점검 후속 수정(A 항목) 회귀 테스트. 운영 DB·ERP·외부 HTTP 없이 실행한다."""
import json
from datetime import timedelta

from django.urls import reverse

from material.models import MaterialStock, MaterialTransaction, Warehouse
from orders.models import DeliveryOrder, DeliveryOrderItem, Incoming, LabelPrintLog, Part, UserProfile
from orders.remaining import order_progress
from orders.test_scm_remodel import RemodelBase, messages_of


class PartVendorLinkPermissionTests(RemodelBase):
    """A1: 품목의 협력사는 납품서 소유권 판정 기준이므로 협력사 계정이 바꿀 수 없다."""

    def link(self, user, part, vendor):
        self.client.force_login(user)
        return self.client.post(reverse('api_part_link_vendor'), json.dumps(
            {'part_no': part.part_no, 'vendor_id': vendor.pk}), content_type='application/json')

    def test_supplier_cannot_take_over_another_vendors_part(self):
        victim = Part.objects.create(part_no='RM-OTHER', part_name='Other part', vendor=self.other_vendor)
        response = self.link(self.supplier, victim, self.vendor)
        self.assertEqual(response.status_code, 403)
        victim.refresh_from_db()
        self.assertEqual(victim.vendor_id, self.other_vendor.pk)

    def test_internal_user_needs_a_screen_permission(self):
        victim = Part.objects.create(part_no='RM-OTHER', part_name='Other part', vendor=self.other_vendor)
        self.assertEqual(self.link(self.staff('no-perm'), victim, self.vendor).status_code, 403)
        ok = self.link(self.staff('order-editor', can_scm_order_edit=True), victim, self.vendor)
        self.assertTrue(ok.json()['success'])
        victim.refresh_from_db()
        self.assertEqual(victim.vendor_id, self.vendor.pk)


class ReceiveAfterCancelTests(RemodelBase):
    """A2: 입고 취소로 풀린 발주 잔량을 다른 납품서가 쓴 뒤에는 옛 납품서를 다시 입고할 수 없다."""

    def setUp(self):
        super().setUp()
        self.wh = Warehouse.objects.create(code='4200', name='Material')
        self.po = self.order(qty=100, erp='PO-A2')

    def confirm(self, delivery):
        self.client.force_login(self.admin)
        return self.client.post(reverse('receive_delivery_order_confirm'), {
            'order_id': delivery.pk, 'inspection_needed': 'no', 'direct_warehouse_code': '4200'}, follow=True)

    def cancel_all(self, delivery):
        incoming = Incoming.objects.filter(delivery_order_no=delivery.order_no).first()
        self.client.force_login(self.admin)
        return self.client.post(reverse('incoming_cancel'), {
            'incoming_id': incoming.pk, 'cancel_mode': 'all'}, follow=True)

    def stock(self):
        return sum(s.quantity for s in MaterialStock.objects.filter(warehouse=self.wh, part=self.part))

    def test_old_delivery_note_is_blocked_once_capacity_was_reused(self):
        first = self.deliver(100, order=self.po)
        self.confirm(first)
        self.assertEqual(self.stock(), 100)
        self.cancel_all(first)
        self.assertEqual(self.stock(), 0)
        first.refresh_from_db()
        self.assertFalse(first.is_received)
        self.assertEqual(order_progress([self.po])[self.po.pk].remaining, 100)  # 취소로 잔량 복구

        second = self.deliver(100, order=self.po)   # 복구된 잔량으로 새 납품서 발행
        self.confirm(second)
        self.assertEqual(self.stock(), 100)

        response = self.confirm(first)              # 옛 납품서를 다시 입고 시도
        self.assertEqual(self.stock(), 100, messages_of(response))  # 발주량 초과 입고 없음
        first.refresh_from_db()
        self.assertFalse(first.is_received)
        self.assertFalse(MaterialTransaction.objects.filter(ref_delivery_order=first.order_no).exists())
        self.assertTrue(any('잔량' in m for m in messages_of(response)), messages_of(response))

    def test_old_delivery_note_can_be_received_again_when_capacity_is_free(self):
        first = self.deliver(100, order=self.po)
        self.confirm(first)
        self.cancel_all(first)
        self.confirm(first)
        self.assertEqual(self.stock(), 100)
        # 다시 입고하면 잔량도 다시 잡힌다.
        self.assertEqual(order_progress([self.po])[self.po.pk].remaining, 0)
        self.assertEqual(LabelPrintLog.objects.filter(delivery_item__order=first).count(), 1)

    def test_cancelled_delivery_note_can_be_deleted(self):
        first = self.deliver(100, order=self.po)
        self.confirm(first)
        self.cancel_all(first)
        self.client.force_login(self.admin)
        self.client.post(reverse('delete_delivery_order', args=[first.pk]))
        self.assertFalse(DeliveryOrder.objects.filter(pk=first.pk).exists())


class ItemCancelStatusTests(RemodelBase):
    """A4: 품목별 취소 뒤 납품서 상태는 남은 품목 기준으로 다시 계산된다."""

    def setUp(self):
        super().setUp()
        Warehouse.objects.create(code='4200', name='Material')
        self.po = self.order(qty=100, erp='PO-A4')
        self.part2 = Part.objects.create(part_no='RM-P2', part_name='Second part', vendor=self.vendor)
        self.po2 = self.order(qty=100, erp='PO-A4B', part=self.part2)

    def receive(self, delivery):
        self.client.force_login(self.admin)
        self.client.post(reverse('receive_delivery_order_confirm'), {
            'order_id': delivery.pk, 'inspection_needed': 'no', 'direct_warehouse_code': '4200'})

    def cancel_item(self, delivery, part):
        incoming = Incoming.objects.get(delivery_order_no=delivery.order_no, part=part)
        self.client.force_login(self.admin)
        return self.client.post(reverse('incoming_cancel'), {
            'incoming_id': incoming.pk, 'cancel_mode': 'item'}, follow=True)

    def test_two_item_note_keeps_received_status_for_the_remaining_item(self):
        delivery = self.deliver(40, order=self.po)
        item2 = DeliveryOrderItem.objects.create(
            order=delivery, part_no=self.part2.part_no, part_name=self.part2.part_name, snp=30, box_count=1,
            total_qty=30, lot_no=self.today, linked_order=self.po2, erp_order_no='PO-A4B', erp_order_seq='1')
        LabelPrintLog.objects.create(delivery_item=item2, vendor=self.vendor, part=self.part2,
                                     part_no=self.part2.part_no, printed_qty=30, snp=30, order=self.po2)
        self.receive(delivery)
        self.cancel_item(delivery, self.part)
        delivery.refresh_from_db()
        self.assertEqual(delivery.items.count(), 1)
        self.assertEqual(delivery.status, 'APPROVED')      # '검사대기'로 굳지 않는다
        self.assertEqual(order_progress([self.po])[self.po.pk].remaining, 100)
        self.assertEqual(order_progress([self.po2])[self.po2.pk].remaining, 70)

    def test_single_item_note_is_removed_with_its_last_item(self):
        delivery = self.deliver(40, order=self.po)
        self.receive(delivery)
        self.cancel_item(delivery, self.part)
        self.assertFalse(DeliveryOrder.objects.filter(pk=delivery.pk).exists())
        self.assertFalse(LabelPrintLog.objects.exists())
        self.assertEqual(order_progress([self.po])[self.po.pk].remaining, 100)

    def test_missing_cancel_mode_is_reported(self):
        delivery = self.deliver(40, order=self.po)
        self.receive(delivery)
        incoming = Incoming.objects.get(delivery_order_no=delivery.order_no)
        self.client.force_login(self.admin)
        response = self.client.post(reverse('incoming_cancel'), {'incoming_id': incoming.pk}, follow=True)
        self.assertTrue(any('취소 방식' in m for m in messages_of(response)))
        self.assertTrue(Incoming.objects.filter(pk=incoming.pk).exists())


class VendorIsolationTests(RemodelBase):
    """A6: 협력사 계정은 납기준수율·검색 API에서 자기 업체 자료만 본다."""

    def setUp(self):
        super().setUp()
        self.other_part = Part.objects.create(part_no='RM-OTHER', part_name='Other part', vendor=self.other_vendor)
        for part in (self.part, self.other_part):
            self.order(qty=10, part=part, due=self.today)
            Incoming.objects.create(part=part, quantity=10, confirmed_qty=10, in_date=self.today)

    def vendors_in_report(self, user, **params):
        self.client.force_login(user)
        response = self.client.get(reverse('vendor_delivery_report'), params)
        self.assertEqual(response.status_code, 200)
        return {row['vendor'].pk for row in response.context['vendor_stats']}

    def test_supplier_report_is_limited_to_own_vendor(self):
        self.assertEqual(self.vendors_in_report(self.admin), {self.vendor.pk, self.other_vendor.pk})
        self.assertEqual(self.vendors_in_report(self.supplier), {self.vendor.pk})
        self.assertEqual(self.vendors_in_report(self.supplier, vendor_id=self.other_vendor.pk), {self.vendor.pk})

    def test_supplier_search_apis_are_limited_to_own_data(self):
        self.client.force_login(self.supplier)
        parts = self.client.get(reverse('api_part_search'), {'q': 'RM-'}).json()['results']
        self.assertEqual({p['part_no'] for p in parts}, {self.part.part_no})
        vendors = self.client.get(reverse('api_vendor_search'), {'q': 'Remodel'}).json()['results']
        self.assertEqual(len(vendors), 1)
        self.client.force_login(self.admin)
        parts = self.client.get(reverse('api_part_search'), {'q': 'RM-'}).json()['results']
        self.assertEqual({p['part_no'] for p in parts}, {self.part.part_no, self.other_part.part_no})


class OlderLotExceptionTests(RemodelBase):
    """A3: 이전 납품보다 오래된 LOT는 막히고, 내부 담당자가 승인하면 한 번 등록할 수 있다."""

    def setUp(self):
        super().setUp()
        from orders.models import LotExceptionRequest
        self.Request = LotExceptionRequest
        self.po = self.order(qty=1000, erp='PO-A3')
        self.older = self.today - timedelta(days=3)

    def register_lot(self, lot, qty=10, user=None):
        self.client.force_login(user or self.supplier)
        return self.client.post(reverse('create_delivery_order'), {
            'part_nos[]': [self.part.part_no], 'snps[]': ['1'], 'box_counts[]': [str(qty)],
            'order_ids[]': [str(self.po.pk)], 'lot_nos[]': [lot.isoformat()]}, follow=True)

    def decide(self, user, request_obj, action):
        self.client.force_login(user)
        return self.client.post(reverse('lot_exception_decide', args=[request_obj.pk]), {'action': action})

    def test_older_lot_is_blocked_and_a_request_is_filed_once(self):
        self.register_lot(self.today)
        self.assertEqual(DeliveryOrder.objects.count(), 1)
        response = self.register_lot(self.older)
        self.assertEqual(DeliveryOrder.objects.count(), 1, messages_of(response))
        self.assertTrue(any('예외 승인 요청' in m for m in messages_of(response)))
        self.register_lot(self.older)                       # 다시 시도해도 요청은 1건
        request_obj = self.Request.objects.get()
        self.assertEqual((request_obj.status, request_obj.lot_no, request_obj.latest_lot, request_obj.vendor_id),
                         ('PENDING', self.older, self.today, self.vendor.pk))

    def test_same_or_newer_lot_and_first_delivery_are_not_blocked(self):
        self.register_lot(self.older)                       # 첫 납품은 비교 대상이 없다
        self.register_lot(self.older)                       # 같은 날짜
        self.register_lot(self.today)                       # 더 최근
        self.assertEqual(DeliveryOrder.objects.count(), 3)
        self.assertFalse(self.Request.objects.exists())

    def test_future_lot_is_rejected_without_a_request(self):
        response = self.register_lot(self.today + timedelta(days=1))
        self.assertFalse(DeliveryOrder.objects.exists())
        self.assertFalse(self.Request.objects.exists())
        self.assertTrue(any('오늘 이후' in m for m in messages_of(response)))

    def test_approval_allows_exactly_one_registration(self):
        self.register_lot(self.today)
        self.register_lot(self.older)
        request_obj = self.Request.objects.get()

        self.decide(self.supplier, request_obj, 'approve')  # 협력사는 스스로 승인할 수 없다
        request_obj.refresh_from_db()
        self.assertEqual(request_obj.status, 'PENDING')

        self.decide(self.staff('receiver', can_scm_incoming_edit=True), request_obj, 'approve')
        request_obj.refresh_from_db()
        self.assertEqual(request_obj.status, 'APPROVED')

        self.register_lot(self.older)
        self.assertEqual(DeliveryOrder.objects.count(), 2)
        request_obj.refresh_from_db()
        self.assertEqual(request_obj.status, 'USED')
        self.assertEqual(request_obj.delivery_item.lot_no, self.older)

        self.register_lot(self.older)                       # 승인은 한 번만 유효
        self.assertEqual(DeliveryOrder.objects.count(), 2)
        self.assertEqual(self.Request.objects.filter(status='PENDING').count(), 1)

    def test_rejected_request_does_not_allow_registration(self):
        self.register_lot(self.today)
        self.register_lot(self.older)
        request_obj = self.Request.objects.get()
        self.decide(self.admin, request_obj, 'reject')
        request_obj.refresh_from_db()
        self.assertEqual(request_obj.status, 'REJECTED')
        self.decide(self.admin, request_obj, 'approve')     # 이미 처리된 요청은 바뀌지 않는다
        request_obj.refresh_from_db()
        self.assertEqual(request_obj.status, 'REJECTED')

    def test_requests_are_listed_per_vendor(self):
        self.register_lot(self.today)
        self.register_lot(self.older)
        self.client.force_login(self.supplier)
        page = self.client.get(reverse('label_list'))
        self.assertContains(page, '이전 LOT 예외 요청')
        self.assertEqual(len(page.context['lot_exceptions']), 1)
        self.assertFalse(page.context['can_decide_lot_exception'])
        other = __import__('django.contrib.auth.models', fromlist=['User']).User.objects.create_user('other-supplier')
        UserProfile.objects.filter(user=other).update(
            role='VENDOR', account_type='VENDOR', org=self.other_vendor.organization, can_scm_label_view=True)
        self.client.force_login(other)
        self.assertEqual(len(self.client.get(reverse('label_list')).context['lot_exceptions']), 0)
        self.client.force_login(self.admin)
        admin_page = self.client.get(reverse('label_list'))
        self.assertTrue(admin_page.context['can_decide_lot_exception'])
        self.assertEqual(admin_page.context['lot_exception_pending'], 1)
