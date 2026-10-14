"""Explicit audit cases, excluded from normal test*.py discovery.

Assertions describe the expected safety contract. Failures are findings, not
production fixes. Run through run_workflow_audit.py focused only.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.db.models import Sum
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from material.models import MaterialStock, MaterialTransaction, ProcessTag, Warehouse
from orders.models import DeliveryOrder, DeliveryOrderItem, LabelPrintLog, Order, Part, ReturnLog, UserProfile, Vendor
from orders.returns import acknowledge_return, restored_quantities, ship_return
from qms.models import ImportInspection


@override_settings(ERP_ENABLED=False, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class WorkflowAudit(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('audit-staff', is_superuser=True, is_staff=True)
        self.supplier = User.objects.create_user('audit-supplier')
        self.vendor = Vendor.objects.create(code='AUDIT-A', name='Synthetic supplier A', user=self.supplier)
        self.other_vendor = Vendor.objects.create(code='AUDIT-B', name='Synthetic supplier B')
        UserProfile.objects.update_or_create(user=self.supplier, defaults={
            'role': 'VENDOR', 'account_type': 'VENDOR', 'org': self.vendor.organization,
            'can_scm_label_view': True, 'can_scm_label_edit': True,
            'can_scm_order_view': True, 'can_scm_order_edit': True,
            'can_scm_inventory_view': True,
        })
        self.part = Part.objects.create(part_no='AUDIT-A-PART', part_name='Synthetic part A', vendor=self.vendor)
        self.other_part = Part.objects.create(part_no='AUDIT-B-PART', part_name='Synthetic part B', vendor=self.other_vendor)
        self.lot = date(2026, 10, 1)
        self.order = self.make_order(self.part)
        self.other_order = self.make_order(self.other_part)
        self.waiting = Warehouse.objects.create(code='8100', name='Inspection waiting')
        self.good = Warehouse.objects.create(code='2000', name='Accepted stock')
        self.bad = Warehouse.objects.create(code='8200', name='Rejected stock')
        self.client.force_login(self.supplier)
        self.client.raise_request_exception = False
        self.sequence = 0

    def make_order(self, part):
        return Order.objects.create(vendor=part.vendor, part_no=part.part_no, part_name=part.part_name,
            quantity=100, due_date=date(2026, 10, 20), approved_at=timezone.now(),
            erp_order_no='AUDIT-PO-' + part.part_no, erp_order_seq='1')

    def payload(self, quantity=10, order=None, part=None):
        return {'part_nos[]': [(part or self.part).part_no], 'snps[]': ['1'],
                'box_counts[]': [str(quantity)], 'order_ids[]': [str((order or self.order).pk)],
                'lot_nos[]': [self.lot.isoformat()]}

    def register(self, data=None, instant=None):
        self.sequence += 1
        instant = instant or datetime(2026, 10, 14, 8, 0, tzinfo=dt_timezone.utc) + timedelta(seconds=self.sequence)
        with patch('orders.views.timezone.now', return_value=instant):
            return self.client.post(reverse('create_delivery_order'), data or self.payload())

    def fixture_delivery(self, number, part=None, order=None, qty=10):
        part, order = part or self.part, order or self.order
        delivery = DeliveryOrder.objects.create(order_no=number)
        item = DeliveryOrderItem.objects.create(order=delivery, part_no=part.part_no,
            part_name=part.part_name, snp=qty, box_count=1, total_qty=qty, lot_no=self.lot,
            linked_order=order, erp_order_no=order.erp_order_no, erp_order_seq=order.erp_order_seq)
        log = LabelPrintLog.objects.create(delivery_item=item, part=part, vendor=part.vendor,
            part_no=part.part_no, order=order, printed_qty=qty, snp=qty)
        return delivery, item, log

    def test_supplier_can_approve_own_order(self):
        self.order.approved_at = None
        self.order.save(update_fields=['approved_at'])
        response = self.client.get(reverse('order_approve', args=[self.order.pk]))
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.approved_at, f'Supplier approval blocked; HTTP {response.status_code}')

    def test_unapproved_order_cannot_register_delivery(self):
        self.order.approved_at = None
        self.order.save(update_fields=['approved_at'])
        self.register()
        self.assertEqual(DeliveryOrderItem.objects.count(), 0, 'Unapproved order was registered')

    def test_closed_order_cannot_register_delivery(self):
        self.order.is_closed = True
        self.order.save(update_fields=['is_closed'])
        self.register()
        self.assertEqual(DeliveryOrderItem.objects.count(), 0, 'Closed order was registered')

    def test_delivery_must_not_exceed_remaining_order_quantity(self):
        self.register(self.payload(quantity=101))
        self.assertEqual(DeliveryOrderItem.objects.count(), 0, 'Order 100 accepted delivery 101')

    def test_multiple_lots_must_not_exceed_combined_remaining_quantity(self):
        data = self.payload(quantity=60)
        for key in data:
            data[key] = data[key] * 2
        data['lot_nos[]'][1] = '2026-10-02'
        self.register(data)
        total = DeliveryOrderItem.objects.aggregate(q=Sum('total_qty'))['q'] or 0
        self.assertLessEqual(total, 100, 'Two LOT lines of 60 each exceeded the order total of 100')

    def test_partial_deliveries_reduce_remaining_quantity(self):
        self.register(self.payload(quantity=40))
        response = self.client.get(reverse('label_list'))
        row = next(row for row in response.context['label_data'] if row['order_id'] == self.order.pk)
        self.assertEqual(row['remain'], 60)
        self.register(self.payload(quantity=60))
        response = self.client.get(reverse('label_list'))
        self.assertFalse(any(row['order_id'] == self.order.pk for row in response.context['label_data']))
        self.assertEqual(DeliveryOrderItem.objects.aggregate(q=Sum('total_qty'))['q'], 100)

    def test_other_supplier_order_cannot_be_linked_to_own_part(self):
        self.register(self.payload(order=self.other_order))
        self.assertFalse(DeliveryOrderItem.objects.filter(linked_order=self.other_order).exists(),
                         'Own part was linked to another supplier purchase order')

    def test_other_supplier_part_is_rejected(self):
        self.register(self.payload(part=self.other_part, order=self.other_order))
        self.assertEqual(DeliveryOrderItem.objects.count(), 0)

    def test_other_supplier_delivery_note_is_not_readable(self):
        delivery, _, _ = self.fixture_delivery('AUDIT-FOREIGN-NOTE', self.other_part, self.other_order)
        response = self.client.get(reverse('delivery_note_print', args=[delivery.pk]))
        self.assertIn(response.status_code, [302, 403, 404], 'Other supplier note returned HTTP 200')

    def test_other_supplier_labels_are_not_printable(self):
        delivery, _, _ = self.fixture_delivery('AUDIT-FOREIGN-LABEL', self.other_part, self.other_order)
        response = self.client.get(reverse('label_print', args=[delivery.pk]))
        self.assertEqual(ProcessTag.objects.count(), 0, 'Other supplier print request created process tags')
        self.assertIn(response.status_code, [302, 403, 404])

    def test_other_supplier_lot_stock_is_not_readable(self):
        MaterialStock.objects.create(part=self.other_part, warehouse=self.good, quantity=77, lot_no=self.lot)
        response = self.client.get(reverse('get_lot_details', args=[self.other_part.part_no]))
        self.assertIn(response.status_code, [302, 403, 404], 'Other supplier LOT API returned HTTP 200')

    def test_other_supplier_delivery_cannot_be_deleted(self):
        delivery, _, _ = self.fixture_delivery('AUDIT-FOREIGN-DELETE', self.other_part, self.other_order)
        self.client.post(reverse('delete_delivery_order', args=[delivery.pk]))
        self.assertTrue(DeliveryOrder.objects.filter(pk=delivery.pk).exists())

    def test_cancel_delivery_keeps_other_delivery_label_history(self):
        first, _, _ = self.fixture_delivery('AUDIT-CANCEL-1')
        second, _, second_log = self.fixture_delivery('AUDIT-CANCEL-2')
        self.client.post(reverse('delete_delivery_order', args=[first.pk]))
        self.assertFalse(DeliveryOrder.objects.filter(pk=first.pk).exists())
        self.assertTrue(DeliveryOrder.objects.filter(pk=second.pk).exists())
        self.assertTrue(LabelPrintLog.objects.filter(pk=second_log.pk).exists(),
                        'Cancelling one delivery deleted the second delivery label history')

    def test_non_numeric_packaging_is_rejected_without_server_error(self):
        data = self.payload()
        data['snps[]'] = ['not-a-number']
        response = self.register(data)
        self.assertLess(response.status_code, 500, 'Invalid numeric input returned HTTP 500')
        self.assertEqual(DeliveryOrder.objects.count(), 0)

    def test_negative_packaging_factors_are_rejected(self):
        data = self.payload()
        data['snps[]'], data['box_counts[]'] = ['-10'], ['-1']
        self.register(data)
        self.assertEqual(DeliveryOrderItem.objects.count(), 0, 'Two negative packaging factors created a positive delivery')

    def test_two_valid_deliveries_in_same_second_both_succeed(self):
        instant = datetime(2026, 10, 14, 8, 0, tzinfo=dt_timezone.utc)
        first = self.register(instant=instant)
        second = self.register(instant=instant)
        self.assertLess(first.status_code, 500)
        self.assertLess(second.status_code, 500, 'Second valid request in same second returned HTTP 500')
        self.assertEqual(DeliveryOrder.objects.count(), 2)

    def test_receipt_inspection_partial_return_and_acknowledgement_chain(self):
        self.register(self.payload(quantity=100))
        delivery = DeliveryOrder.objects.get()
        self.client.force_login(self.staff)
        payload = {'order_id': delivery.pk, 'inspection_needed': 'yes', 'target_warehouse_code': '2000'}
        self.client.post(reverse('receive_delivery_order_confirm'), payload)
        self.client.post(reverse('receive_delivery_order_confirm'), payload)
        self.assertEqual(MaterialStock.objects.get(part=self.part, warehouse=self.waiting).quantity, 100)
        self.assertFalse(MaterialStock.objects.filter(part=self.part, warehouse=self.good, quantity__gt=0).exists())
        self.assertEqual(MaterialTransaction.objects.filter(delivery_item__order=delivery).count(), 1)
        inspection = ImportInspection.objects.get()
        with patch('material.erp_api.register_erp_incoming', return_value=(True, 'MOCK', None)):
            response = self.client.post(reverse('qms:import_inspection_detail', args=[inspection.pk]),
                {'decision': 'COMPLETE', 'qty_good': '80', 'qty_bad': '20', 'remark': 'Synthetic defect'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MaterialStock.objects.get(part=self.part, warehouse=self.waiting).quantity, 0)
        self.assertEqual(MaterialStock.objects.get(part=self.part, warehouse=self.good).quantity, 80)
        self.assertEqual(MaterialStock.objects.get(part=self.part, warehouse=self.bad).quantity, 20)
        case = ReturnLog.objects.get()
        shipment = ship_return(case.pk, 5, self.staff, str(uuid4()))
        self.assertEqual(MaterialStock.objects.get(part=self.part, warehouse=self.bad).quantity, 15)
        self.assertEqual(restored_quantities(ReturnLog.objects.all())[0][self.order.pk], 0)
        self.assertEqual(acknowledge_return(case.pk, shipment.pk, self.supplier), 5)
        self.assertEqual(acknowledge_return(case.pk, shipment.pk, self.supplier), 0)
        self.assertEqual(restored_quantities(ReturnLog.objects.all())[0][self.order.pk], 5)
        self.client.force_login(self.supplier)
        self.client.post(reverse('delete_delivery_order', args=[delivery.pk]))
        self.assertTrue(DeliveryOrder.objects.filter(pk=delivery.pk).exists(), 'Received delivery must not be deleted')
        self.assertEqual(MaterialStock.objects.get(part=self.part, warehouse=self.good).quantity, 80)


@override_settings(ERP_ENABLED=False, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class ScanAndERPWriterAudit(TestCase):
    def setUp(self):
        from material.models import RawMaterialLabel
        self.user = User.objects.create_user('scan-audit', is_superuser=True)
        self.client.force_login(self.user)
        self.part = Part.objects.create(part_no='AUDIT-SCAN', part_name='Synthetic scan part')
        self.source = Warehouse.objects.create(code='3200', name='Raw material')
        self.target = Warehouse.objects.create(code='3000', name='Production')
        self.other = Warehouse.objects.create(code='2000', name='Other warehouse')
        self.lot = date(2026, 10, 1)
        self.stock = MaterialStock.objects.create(part=self.part, warehouse=self.source, lot_no=self.lot, quantity=100)
        self.label = RawMaterialLabel.objects.create(label_id='RM-AUDIT-001', part=self.part,
            part_no=self.part.part_no, part_name=self.part.part_name, lot_no=self.lot, quantity=25, status='INSTOCK')

    def scan(self, label_id=None, target='3000'):
        return self.client.post(reverse('material:api_process_tag_scan'),
            {'tag_id': label_id or self.label.label_id, 'target_warehouse_code': target},
            content_type='application/json')

    def test_raw_label_sequential_duplicate_scan_is_blocked(self):
        self.assertTrue(self.scan().json()['success'])
        self.assertFalse(self.scan().json()['success'])
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 75)
        self.assertEqual(MaterialStock.objects.get(part=self.part, warehouse=self.target).quantity, 25)

    def test_raw_label_older_lot_blocks_newer_lot(self):
        MaterialStock.objects.create(part=self.part, warehouse=self.source, lot_no=date(2026, 9, 1), quantity=10)
        self.assertFalse(self.scan().json()['success'])
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 100)
        self.assertEqual(MaterialTransaction.objects.count(), 0)

    def test_process_tag_repeat_scan_should_not_silently_cancel(self):
        tag = ProcessTag.objects.create(tag_id='TAG-AUDIT-001', part=self.part, part_no=self.part.part_no,
            part_name=self.part.part_name, lot_no=self.lot, quantity=25, status='PRINTED')
        self.assertTrue(self.scan(tag.tag_id).json()['success'])
        self.scan(tag.tag_id)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 75, 'Second scan reversed the first move without a separate cancel action')

    def test_raw_label_nonproduction_repeat_move_should_be_blocked(self):
        self.assertTrue(self.scan(target='2000').json()['success'])
        self.scan(target='2000')
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 75, 'Same label moved 25 twice to a nonproduction warehouse')

    def test_stale_label_read_should_not_allow_second_stock_move(self):
        from material.models import RawMaterialLabel
        stale = RawMaterialLabel.objects.get(pk=self.label.pk)
        self.assertTrue(self.scan().json()['success'])
        # Simulate a second worker that read INSTOCK before the first committed.
        # This is deterministic stale-read coverage, not a production DB load test.
        with patch.object(RawMaterialLabel.objects, 'filter') as query:
            query.return_value.first.return_value = stale
            self.scan()
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 75, 'Stale label state allowed a second debit')

    def test_same_erp_stock_move_is_not_sent_twice(self):
        from material.erp_api import register_erp_stock_move
        trx = MaterialTransaction.objects.create(transaction_no='AUDIT-ERP-MOVE', transaction_type='TRANSFER',
            part=self.part, quantity=25, lot_no=self.lot, warehouse_from=self.source, warehouse_to=self.target)
        with override_settings(ERP_ENABLED=True), patch('material.erp_api.call_erp_api',
                return_value=(True, {'resultData': 'MOCK-MOVE-1'}, None)) as remote:
            register_erp_stock_move(trx, 25, '3200', '3000')
            register_erp_stock_move(trx, 25, '3200', '3000')
        self.assertEqual(remote.call_count, 1, 'Same local movement produced two ERP write requests')
