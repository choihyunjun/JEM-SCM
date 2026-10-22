"""SCM 메뉴 점검(2026-10) 후속 수정 회귀 테스트. 운영 DB·ERP·외부 HTTP 없이 실행한다."""
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from material.models import MaterialStock, Warehouse
from orders.models import (DeliveryOrder, DeliveryOrderItem, Demand, Incoming, LabelPrintLog, Order, Part,
                           QnA, RoleDefaultPermission, UserProfile, Vendor)
from orders.remaining import manual_pool, order_progress
from orders.supply import AVAILABLE_WAREHOUSE_CODES


def messages_of(response):
    return [str(m) for m in response.context['messages']] if response.context else []


@override_settings(ERP_ENABLED=False, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class RemodelBase(TestCase):
    def setUp(self):
        self.today = timezone.localtime().date()
        self.admin = User.objects.create_user('remodel-admin', is_superuser=True, is_staff=True)
        self.vendor = Vendor.objects.create(code='RV-A', name='Remodel vendor A', erp_code='TR-A')
        self.other_vendor = Vendor.objects.create(code='RV-B', name='Remodel vendor B', erp_code='TR-B')
        self.part = Part.objects.create(part_no='RM-P1', part_name='Remodel part', vendor=self.vendor)
        self.supplier = User.objects.create_user('remodel-supplier')
        UserProfile.objects.filter(user=self.supplier).update(
            role='VENDOR', account_type='VENDOR', org=self.vendor.organization,
            can_scm_label_view=True, can_scm_label_edit=True, can_scm_order_view=True,
            can_scm_inventory_view=True)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('External HTTP prohibited'))
        network.start()
        self.addCleanup(network.stop)

    def staff(self, username='remodel-staff', role='STAFF', **perms):
        user = User.objects.create_user(username)
        UserProfile.objects.filter(user=user).update(role=role, account_type='INTERNAL', **perms)
        return user

    def order(self, qty=100, due=None, erp=None, closed=False, approved=True, vendor=None, part=None):
        part = part or self.part
        return Order.objects.create(
            vendor=vendor or part.vendor, part_no=part.part_no, part_name=part.part_name, quantity=qty,
            due_date=due or self.today + timedelta(days=10), erp_order_no=erp or '',
            erp_order_seq='1' if erp else '', is_closed=closed,
            approved_at=timezone.now() if approved else None)

    def deliver(self, qty, order=None, linked=None, created=None):
        """LabelPrintLog까지 포함한 납품서 1건 (화면 등록과 같은 구조)."""
        delivery = DeliveryOrder.objects.create(order_no=f'DO-T-{DeliveryOrder.objects.count() + 1}')
        if created:
            DeliveryOrder.objects.filter(pk=delivery.pk).update(created_at=created)
            delivery.refresh_from_db()
        item = DeliveryOrderItem.objects.create(
            order=delivery, part_no=self.part.part_no, part_name=self.part.part_name, snp=qty, box_count=1,
            total_qty=qty, lot_no=self.today, linked_order=order or linked,
            erp_order_no=order.erp_order_no if order else '', erp_order_seq=order.erp_order_seq if order else '')
        LabelPrintLog.objects.create(delivery_item=item, vendor=self.part.vendor, part=self.part,
                                     part_no=self.part.part_no, printed_qty=qty, snp=qty, order=order)
        return delivery

    def register(self, user, qty, order=None):
        self.client.force_login(user)
        return self.client.post(reverse('create_delivery_order'), {
            'part_nos[]': [self.part.part_no], 'snps[]': ['1'], 'box_counts[]': [str(qty)],
            'order_ids[]': [str(order.pk) if order else ''], 'lot_nos[]': [self.today.isoformat()]})


class UnifiedRemainingTests(RemodelBase):
    def test_every_screen_uses_registered_delivery_quantity(self):
        overdue = self.order(qty=100, due=self.today - timedelta(days=2), erp='PO-1')
        self.deliver(30, order=overdue)          # 납품서만 등록, 아직 입고 전
        self.client.force_login(self.admin)

        label = self.client.get(reverse('label_list')).context['label_data']
        self.assertEqual(next(r for r in label if r['order_id'] == overdue.pk)['remain'], 70)
        order_list = self.client.get(reverse('order_list')).context['overdue_orders']
        self.assertEqual([r['remain_qty'] for r in order_list], [70])
        dashboard = self.client.get(reverse('scm_alert_dashboard')).context['overdue_list']
        self.assertEqual([r['remain_qty'] for r in dashboard], [70])
        admin_main = self.client.get(reverse('scm_admin_main')).context['overdue_orders']
        self.assertEqual([r['remain_qty'] for r in admin_main], [70])

        # 실제 등록 검증도 같은 잔량(70)을 쓴다.
        self.register(self.supplier, 71, overdue)
        self.assertEqual(DeliveryOrderItem.objects.count(), 1)
        self.register(self.supplier, 70, overdue)
        self.assertEqual(DeliveryOrderItem.objects.count(), 2)
        self.assertEqual(order_progress([overdue])[overdue.pk].remaining, 0)

    def test_closing_delivered_manual_order_does_not_shrink_other_manual_order(self):
        first = self.order(qty=100, due=self.today + timedelta(days=1))
        self.register(self.supplier, 100)               # 품번 풀로 등록 → 가장 빠른 발주에 연결
        self.assertEqual(DeliveryOrderItem.objects.get().linked_order, first)
        first.is_closed = True
        first.save()
        self.order(qty=50, due=self.today + timedelta(days=5))
        self.assertEqual(manual_pool(self.part.part_no, self.vendor.pk).remaining, 50)
        self.client.force_login(self.supplier)
        rows = self.client.get(reverse('label_list')).context['label_data']
        self.assertEqual([r['remain'] for r in rows if not r['is_erp']], [50])

    def test_legacy_unlinked_manual_delivery_is_absorbed_by_closed_order_first(self):
        self.order(qty=100, due=self.today - timedelta(days=5), closed=True)
        open_order = self.order(qty=40)
        legacy = self.deliver(100)  # 연결 없는 과거 수기 납품
        self.assertIsNone(legacy.items.get().linked_order)
        self.assertEqual(manual_pool(self.part.part_no, self.vendor.pk).remaining, 40)
        self.assertEqual(order_progress([open_order])[open_order.pk].remaining, 40)

    def test_manual_pool_is_per_vendor(self):
        self.order(qty=100)
        other_part_order = Order.objects.create(vendor=self.other_vendor, part_no=self.part.part_no,
            part_name='x', quantity=10, due_date=self.today, approved_at=timezone.now())
        self.deliver(100)
        self.assertEqual(manual_pool(self.part.part_no, self.vendor.pk).remaining, 0)
        self.assertEqual(order_progress([other_part_order])[other_part_order.pk].remaining, 10)


class OrderActionPermissionTests(RemodelBase):
    def test_staff_with_order_permission_can_delete_and_close(self):
        staff = self.staff(can_scm_order_view=True, can_scm_order_edit=True)
        a, b = self.order(), self.order()
        self.client.force_login(staff)
        page = self.client.get(reverse('order_list'))
        self.assertTrue(page.context['can_manage_orders'])
        self.client.post(reverse('order_close_action'), {'order_ids': [a.pk]})
        a.refresh_from_db()
        self.assertTrue(a.is_closed)
        self.client.post(reverse('order_delete'), {'order_ids': [b.pk]})
        self.assertFalse(Order.objects.filter(pk=b.pk).exists())

    def test_staff_without_permission_sees_no_buttons_and_gets_message(self):
        staff = self.staff(can_scm_order_view=True)
        target = self.order()
        self.client.force_login(staff)
        page = self.client.get(reverse('order_list'))
        self.assertFalse(page.context['can_manage_orders'])
        self.assertNotContains(page, 'deleteSelected();')
        response = self.client.post(reverse('order_delete'), {'order_ids': [target.pk]}, follow=True)
        self.assertTrue(Order.objects.filter(pk=target.pk).exists())
        self.assertTrue(any('권한이 없습니다' in m for m in messages_of(response)))

    def test_vendor_flags_never_allow_order_management(self):
        UserProfile.objects.filter(user=self.supplier).update(role='STAFF', can_scm_order_edit=True)
        target = self.order()
        self.client.force_login(self.supplier)
        self.client.post(reverse('order_delete'), {'order_ids': [target.pk]})
        self.assertTrue(Order.objects.filter(pk=target.pk).exists())

    def test_order_with_delivery_cannot_be_deleted(self):
        erp_order = self.order(erp='PO-DEL')
        manual = self.order()
        self.deliver(10, order=erp_order)
        self.register(self.supplier, 5)
        self.client.force_login(self.admin)
        for target in (erp_order, manual):
            response = self.client.post(reverse('order_delete'), {'order_ids': [target.pk]}, follow=True)
            self.assertTrue(Order.objects.filter(pk=target.pk).exists())
            self.assertTrue(any('삭제할 수 없습니다' in m for m in messages_of(response)))

    def test_admin_role_with_permission_can_open_order_upload(self):
        admin_role = self.staff('remodel-admin-role', role='ADMIN', can_scm_order_edit=True)
        self.client.force_login(admin_role)
        self.assertEqual(self.client.get(reverse('order_upload')).status_code, 200)
        no_perm = self.staff('remodel-staff-noperm', can_scm_order_view=True)
        self.client.force_login(no_perm)
        response = self.client.post(reverse('order_create_confirm'), {
            'part_no_list[]': [self.part.part_no], 'quantity_list[]': ['5'],
            'due_date_list[]': [self.today.isoformat()]})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Order.objects.exists())


class DeliveryReportTests(RemodelBase):
    def test_manual_delivery_after_due_date_counts_as_delayed(self):
        from orders.views import _calculate_vendor_monthly_stats
        month_start = self.today.replace(day=1)
        manual = self.order(qty=10, due=month_start)
        Order.objects.filter(pk=manual.pk).update(created_at=timezone.now() - timedelta(days=40))
        delivery = self.deliver(10, linked=manual)
        DeliveryOrder.objects.filter(pk=delivery.pk).update(status='APPROVED')
        if self.today == month_start:
            self.skipTest('납기 이후 납품을 만들 수 없는 날짜')
        stats = _calculate_vendor_monthly_stats(self.vendor, self.today.strftime('%Y-%m'))
        self.assertEqual(stats['delayed_qty'], 10)
        self.assertEqual(stats['on_time_qty'], 0)


class ErpPoSyncTests(RemodelBase):
    def details(self, po_qt=100, rcv_qt=40, due='20261130', seq='1', item=None):
        return [{'poSq': seq, 'itemCd': item or self.part.part_no, 'itemNm': 'n', 'poQt': po_qt,
                 'rcvQt': rcv_qt, 'dueDt': due}]

    def apply(self, rows, seq='1', tr='TR-B'):
        self.client.force_login(self.admin)
        with patch('material.erp_api.fetch_erp_po_details', return_value=rows):
            return self.client.post(reverse('erp_po_sync'), {
                'action': 'apply', 'selected_items': [f'PO-9|{seq}|{self.part.part_no}|999|20990101|{tr}']},
                follow=True)

    def test_apply_uses_erp_remaining_and_erp_vendor(self):
        self.apply(self.details())
        order = Order.objects.get()
        self.assertEqual(order.quantity, 60)            # 100 - 40, 화면 값(999) 무시
        self.assertEqual(order.vendor, self.other_vendor)  # trCd 우선
        self.assertEqual(order.due_date, date(2026, 11, 30))
        self.assertEqual(order.erp_po_qty, 100)

    def test_fully_received_line_is_not_applied(self):
        self.apply(self.details(rcv_qt=100))
        self.assertFalse(Order.objects.exists())

    def test_reapply_updates_quantity_and_due_date(self):
        self.apply(self.details(), tr='TR-A')
        self.apply(self.details(po_qt=120, due='20261215'), tr='TR-A')
        order = Order.objects.get()
        self.assertEqual(order.quantity, 80)
        self.assertEqual(order.due_date, date(2026, 12, 15))
        self.assertEqual(order.erp_po_qty, 120)

    def test_removed_erp_line_marks_scm_order_cancelled(self):
        self.apply(self.details(), tr='TR-A')
        self.apply(self.details(seq='2'))
        order = Order.objects.get(erp_order_seq='1')
        self.assertEqual(order.erp_status, 'CANCELLED')
        self.assertTrue(order.is_closed)

    def test_failed_erp_lookup_never_cancels(self):
        self.apply(self.details(), tr='TR-A')
        self.apply([])
        order = Order.objects.get()
        self.assertEqual(order.erp_status, '')
        self.assertFalse(order.is_closed)

    def test_search_marks_zero_remaining_and_changes(self):
        self.apply(self.details(), tr='TR-A')
        self.client.force_login(self.admin)
        header = [{'poNb': 'PO-9', 'poDt': '20261001', 'trCd': 'TR-A', 'attrNm': 'A'}]
        rows = self.details(po_qt=150) + self.details(seq='2', rcv_qt=100)
        with patch('material.erp_api.fetch_erp_po_headers', return_value=header), \
                patch('material.erp_api.fetch_erp_po_details', return_value=rows):
            items = self.client.post(reverse('erp_po_sync'), {
                'action': 'search', 'date_from': '2026-10-01', 'date_to': '2026-10-31'}).context['po_items']
        by_seq = {i['po_sq']: i for i in items}
        self.assertEqual(by_seq['1']['sync_state'], 'changed')
        self.assertTrue(by_seq['1']['selectable'])
        self.assertFalse(by_seq['2']['selectable'])


class IncomingAndStockTests(RemodelBase):
    def test_incoming_check_lists_real_warehouses_and_rejects_unknown_code(self):
        Warehouse.objects.create(code='4200', name='4공장 자재창고')
        Warehouse.objects.create(code='8100', name='검사대기')
        Warehouse.objects.create(code='9999', name='미사용', is_active=False)
        delivery = self.deliver(5, order=self.order(erp='PO-W'))
        self.client.force_login(self.admin)
        page = self.client.post(reverse('receive_delivery_order_scan'), {'qr_code': delivery.order_no})
        self.assertContains(page, '[4200] 4공장 자재창고')
        self.assertNotContains(page, 'value="8100"')
        self.assertNotContains(page, 'value="9999"')
        self.assertNotContains(page, '제2공장')
        self.client.post(reverse('receive_delivery_order_confirm'), {
            'order_id': delivery.pk, 'inspection_needed': 'no', 'direct_warehouse_code': '3000'})
        delivery.refresh_from_db()
        self.assertFalse(delivery.is_received)

    def test_admin_today_incoming_counts_accepted_quantity_including_inspected(self):
        Incoming.objects.create(part=self.part, in_date=self.today, quantity=100, confirmed_qty=100)
        Incoming.objects.create(part=self.part, in_date=self.today, quantity=50, confirmed_qty=30)  # 수입검사분
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('scm_admin_main')).context['summary']['today_incoming'], 130)
        rows = self.client.get(reverse('incoming_list')).context['incomings']
        self.assertEqual(sorted(r.confirmed_qty for r in rows), [30, 100])

    def test_dashboard_and_inventory_share_available_warehouses(self):
        self.assertEqual(AVAILABLE_WAREHOUSE_CODES, ('2000', '4200', '4300'))
        line = Warehouse.objects.create(code='4300', name='제조현장')
        MaterialStock.objects.create(warehouse=line, part=self.part, quantity=100, lot_no=self.today)
        Demand.objects.create(part=self.part, due_date=self.today + timedelta(days=2), quantity=100)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('scm_alert_dashboard')).context['shortage_count'], 0)
        self.assertEqual(self.client.get(reverse('order_list')).context['shortage_count'], 0)
        inv = self.client.get(reverse('inventory_list'), {'search': '1'}).context['inventory_data']
        self.assertEqual(inv[0]['base_stock'], 100)

    def test_dashboard_expected_incoming_is_limited_to_seven_days(self):
        Demand.objects.create(part=self.part, due_date=self.today + timedelta(days=1), quantity=50)
        late = self.order(qty=50, due=self.today + timedelta(days=20), erp='PO-LATE')
        self.deliver(50, order=late)
        self.client.force_login(self.admin)
        context = self.client.get(reverse('scm_alert_dashboard')).context
        self.assertEqual(context['shortage_count'], 1)
        self.assertEqual(context['shortage_items'][0]['pending_incoming'], 0)

    def test_demand_and_bulk_order_follow_specific_permissions(self):
        viewer = self.staff('remodel-inv-viewer', can_scm_inventory_view=True)
        editor = self.staff('remodel-inv-editor', can_scm_inventory_view=True, can_scm_inventory_edit=True)
        self.client.force_login(viewer)
        page = self.client.get(reverse('inventory_list'))
        self.assertFalse(page.context['can_edit_demand'])
        self.assertFalse(page.context['can_create_order'])
        self.assertEqual(self.client.get(reverse('demand_manage')).status_code, 302)
        bulk = self.client.post(reverse('bulk_shortage_order'), {'orders': '[{"vendor": "%s", "part_no": "%s", '
            '"due_date": "%s", "quantity": 5}]' % (self.vendor.name, self.part.part_no, self.today)})
        self.assertEqual(bulk.status_code, 403)
        self.assertFalse(Order.objects.exists())
        self.client.force_login(editor)
        self.assertEqual(self.client.get(reverse('demand_manage')).status_code, 200)

    def test_unused_routes_are_removed(self):
        for name in ('demand_upload_action', 'delete_all_demands', 'label_print_action',
                     'vendor_create', 'vendor_delete'):
            with self.subTest(name=name), self.assertRaises(NoReverseMatch):
                reverse(name)


class DeliveryListTests(RemodelBase):
    def test_delivery_note_list_paginates_and_filters_by_period(self):
        old = timezone.now() - timedelta(days=60)
        for i in range(25):
            self.deliver(1, order=self.order(erp=f'PO-L{i}'), created=old if i < 3 else None)
        self.client.force_login(self.admin)
        first = self.client.get(reverse('label_list')).context['orders']
        self.assertEqual(len(first), 20)
        self.assertEqual(first.paginator.count, 25)
        self.assertEqual(len(self.client.get(reverse('label_list'), {'page': 2}).context['orders']), 5)
        start = (self.today - timedelta(days=70)).isoformat()
        end = (self.today - timedelta(days=50)).isoformat()
        period = self.client.get(reverse('label_list'), {'do_start': start, 'do_end': end}).context['orders']
        self.assertEqual(period.paginator.count, 3)


class VendorAccountTests(RemodelBase):
    def test_qna_from_org_linked_vendor_is_saved_with_company_and_visible(self):
        self.client.force_login(self.supplier)
        self.client.post(reverse('qna_create'), {'title': 'q', 'content': 'c'})
        self.assertEqual(QnA.objects.get().vendor, self.vendor)
        self.assertEqual(list(self.client.get(reverse('scm_alert_dashboard')).context['qna_list']), [QnA.objects.get()])

    def test_unlinked_vendor_account_sees_no_company_data_on_dashboard(self):
        loose = User.objects.create_user('remodel-loose')
        UserProfile.objects.filter(user=loose).update(role='VENDOR', account_type='VENDOR')
        self.order()
        self.client.force_login(loose)
        context = self.client.get(reverse('scm_alert_dashboard')).context
        self.assertEqual(context['open_orders'], 0)
        self.assertEqual(context['total_parts'], 0)

    def test_deleting_old_style_linked_user_keeps_vendor_parts_and_orders(self):
        legacy = User.objects.create_user('remodel-legacy')
        self.vendor.user = legacy
        self.vendor.save()
        order = self.order()
        legacy.delete()
        self.vendor.refresh_from_db()
        self.assertIsNone(self.vendor.user)
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())

    def test_org_link_wins_over_conflicting_old_link(self):
        from orders.access import vendor_identity
        self.other_vendor.user = self.supplier
        self.other_vendor.save()
        self.assertEqual(vendor_identity(User.objects.get(pk=self.supplier.pk))[1], self.vendor)

    def test_new_user_gets_defaults_of_chosen_role(self):
        RoleDefaultPermission.objects.create(role='VENDOR', permission_field='can_scm_label_edit')
        RoleDefaultPermission.objects.create(role='STAFF', permission_field='can_scm_order_view')
        self.client.force_login(self.admin)
        self.client.post(reverse('user_create'), {'username': 'remodel-new', 'password': 'x-Strong-1',
                                                  'role': 'STAFF', 'account_type': 'INTERNAL'})
        profile = UserProfile.objects.get(user__username='remodel-new')
        self.assertTrue(profile.can_scm_order_view)
        self.assertFalse(profile.can_scm_label_edit)
