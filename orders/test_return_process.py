from datetime import date
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import connection
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from material.models import MaterialStock, MaterialTransaction, Warehouse
from material.views import _do_cancel_incoming
from qms.models import ImportInspection
from qms.views import import_inspection_detail
from .models import (DeliveryOrder, DeliveryOrderItem, Incoming, LabelPrintLog,
                     Order, Part, ReturnLog, ReturnShipment, UserProfile, Vendor)
from .returns import acknowledge_return, refresh_delivery_status, restored_quantities, ship_return


class ReturnProcessTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create(username='staff', is_superuser=True)
        self.supplier = User.objects.create(username='supplier')
        UserProfile.objects.update_or_create(user=self.supplier, defaults={
            'role': 'VENDOR', 'can_scm_label_view': True, 'can_scm_label_edit': True})
        self.vendor = Vendor.objects.create(code='RETURN', name='Return vendor', user=self.supplier)
        self.part = Part.objects.create(part_no='P-RETURN', part_name='Part', vendor=self.vendor)
        self.waiting = Warehouse.objects.create(code='8100', name='Inspection')
        self.good = Warehouse.objects.create(code='2000', name='Good')
        self.bad = Warehouse.objects.create(code='8200', name='Bad')
        self.delivery = DeliveryOrder.objects.create(order_no='RETURN-DO', status='RECEIVED', is_received=True)
        self.order = Order.objects.create(vendor=self.vendor, part_no=self.part.part_no,
            part_name='Part', quantity=100, due_date=date(2026, 10, 20),
            erp_order_no='PO', erp_order_seq='1', approved_at=timezone.now())
        self.item, self.origin, self.inspection = self.add_receipt('A', self.order)
        self.erp = patch('material.erp_api.register_erp_incoming', return_value=(True, '', None)).start()
        self.erp_delete = patch('material.erp_api.delete_erp_incoming', return_value=(True, None)).start()
        self.addCleanup(patch.stopall)

    def add_receipt(self, batch, order=None):
        item = DeliveryOrderItem.objects.create(order=self.delivery, part_no=self.part.part_no,
            part_name='Part', lot_no=date(2026, 10, 1), total_qty=100, snp=100, box_count=1,
            linked_order=order, erp_order_no=order.erp_order_no if order else '',
            erp_order_seq=order.erp_order_seq if order else '')
        origin = MaterialTransaction.objects.create(transaction_no='IN-' + batch,
            transaction_type='IN_SCM', part=self.part, quantity=100, warehouse_to=self.waiting,
            vendor=self.vendor, lot_no=item.lot_no, production_lot=batch,
            delivery_item=item, ref_delivery_order=self.delivery.order_no)
        MaterialStock.objects.create(warehouse=self.waiting, part=self.part,
            lot_no=item.lot_no, production_lot=batch, quantity=100)
        inspection = ImportInspection.objects.create(inbound_transaction=origin, lot_no=item.lot_no)
        LabelPrintLog.objects.create(delivery_item=item, vendor=self.vendor, part=self.part,
            part_no=self.part.part_no, order=order, printed_qty=100, snp=100)
        return item, origin, inspection

    def decide(self, good=80, bad=20, inspection=None, **extra):
        request = RequestFactory().post('/', {'decision': 'COMPLETE', 'qty_good': good,
            'qty_bad': bad, 'remark': 'Dimension defect', **extra})
        request.user = self.staff
        request.session = {}
        request._messages = FallbackStorage(request)
        return import_inspection_detail(request, (inspection or self.inspection).pk)

    def case(self):
        self.decide()
        return ReturnLog.objects.get(source_incoming=self.origin)

    def ship(self, case, qty=5, key=None):
        return ship_return(case.pk, qty, self.staff, key or str(uuid4()))

    def test_cannot_confirm_before_actual_shipment(self):
        case = self.case()
        with self.assertRaises(ValueError):
            acknowledge_return(case.pk, None, self.supplier)
        case.refresh_from_db()
        self.assertFalse(case.is_confirmed)
        self.assertEqual(case.confirmed_quantity, 0)

    def test_partial_shipment_restores_only_exact_order_sequence(self):
        case = self.case()
        second_order = Order.objects.create(vendor=self.vendor, part_no=self.part.part_no,
            part_name='Part', quantity=100, due_date=self.order.due_date, erp_order_no='PO',
            erp_order_seq='2', approved_at=timezone.now())
        self.add_receipt('B', second_order)
        shipment = self.ship(case)
        self.assertEqual(acknowledge_return(case.pk, shipment.pk, self.supplier), 5)
        by_order, _ = restored_quantities(ReturnLog.objects.all())
        self.assertEqual(by_order[self.order.pk], 5)
        self.assertEqual(by_order[second_order.pk], 0)
        case.refresh_from_db()
        self.assertEqual(case.remaining_quantity, 15)
        self.assertFalse(case.is_confirmed)
        self.client.force_login(self.supplier)
        response = self.client.get(reverse('label_list'))
        self.assertEqual(response.status_code, 200)
        rows = {row['order_id']: row for row in response.context['label_data']}
        self.assertEqual(rows[self.order.pk]['remain'], 5)
        self.assertNotIn(second_order.pk, rows)

    def test_repeat_shipment_and_ack_are_idempotent(self):
        case = self.case()
        key = str(uuid4())
        first = self.ship(case, key=key)
        self.assertEqual(self.ship(case, key=key).pk, first.pk)
        self.assertEqual(ReturnShipment.objects.count(), 1)
        self.assertEqual(acknowledge_return(case.pk, first.pk, self.supplier), 5)
        self.assertEqual(acknowledge_return(case.pk, first.pk, self.supplier), 0)
        with self.assertRaises(ValueError):
            self.ship(case, qty=6, key=key)
        with self.assertRaises(ValueError):
            self.ship(case, qty=16)
        self.assertEqual(MaterialStock.objects.get(warehouse=self.bad, production_lot='A').quantity, 15)

    def test_all_partial_shipments_confirm_case_only_after_last_ack(self):
        case = self.case()
        first, second = self.ship(case, 5), self.ship(case, 15)
        acknowledge_return(case.pk, first.pk, self.supplier)
        case.refresh_from_db()
        self.assertFalse(case.is_confirmed)
        acknowledge_return(case.pk, second.pk, self.supplier)
        case.refresh_from_db()
        self.assertTrue(case.is_confirmed)
        self.assertEqual(case.confirmed_quantity, 20)

    def test_shipment_preserves_origin_and_lot_without_touching_other_batch(self):
        case = self.case()
        other = MaterialStock.objects.create(warehouse=self.bad, part=self.part,
            lot_no=self.origin.lot_no, production_lot='OTHER', quantity=200)
        movement = self.ship(case).movement
        self.assertEqual((movement.source_incoming_id, movement.delivery_item_id,
            movement.lot_no, movement.production_lot, movement.ref_delivery_order),
            (self.origin.pk, self.item.pk, self.origin.lot_no, 'A', self.delivery.order_no))
        other.refresh_from_db()
        self.assertEqual(other.quantity, 200)

    def test_other_supplier_cannot_acknowledge(self):
        case = self.case()
        shipment = self.ship(case)
        other = User.objects.create(username='other')
        UserProfile.objects.update_or_create(user=other, defaults={'role': 'VENDOR'})
        Vendor.objects.create(code='OTHER', name='Other', user=other)
        with self.assertRaises(ValueError):
            acknowledge_return(case.pk, shipment.pk, other)
        shipment.refresh_from_db()
        self.assertIsNone(shipment.confirmed_at)

    def test_pending_second_line_keeps_delivery_in_inspection(self):
        _, _, second = self.add_receipt('B')
        self.decide(100, 0)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'RECEIVED')
        self.decide(100, 0, second)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'APPROVED')

    def test_all_bad_delivery_remains_received_and_cannot_be_rescanned(self):
        self.decide(0, 100)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'REJECTED')
        self.assertTrue(self.delivery.is_received)

    def test_correction_restores_stock_summary_and_redecision_does_not_duplicate(self):
        case = self.case()
        self.decide(decision='CANCEL', correction_reason='Wrong quantity')
        self.inspection.refresh_from_db()
        self.delivery.refresh_from_db()
        self.assertEqual(self.inspection.status, 'PENDING')
        self.assertEqual(self.delivery.status, 'RECEIVED')
        self.assertFalse(ReturnLog.objects.filter(pk=case.pk).exists())
        self.assertEqual(self.inspection.correction_history[0]['qty_bad'], 20)
        self.assertFalse(Incoming.objects.exists())
        self.assertEqual(MaterialStock.objects.get(warehouse=self.waiting, production_lot='A').quantity, 100)
        self.decide(90, 10)
        self.assertEqual(Incoming.objects.get().confirmed_qty, 90)
        self.assertEqual(ReturnLog.objects.get().quantity, 10)

    def test_correction_requires_reason_and_blocks_after_shipment(self):
        case = self.case()
        self.decide(decision='CANCEL')
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.status, 'APPROVED')
        self.ship(case)
        for action in ('cancel_incoming_only', 'delete_all'):
            ok, _ = _do_cancel_incoming(self.origin, action)
            self.assertFalse(ok)
        self.assertEqual(Incoming.objects.get().confirmed_qty, 80)
        self.erp_delete.assert_not_called()

    def test_correction_blocks_used_lot_even_when_stock_was_replenished(self):
        self.case()
        MaterialTransaction.objects.create(transaction_no='USED', transaction_type='OUT_PROD',
            part=self.part, quantity=1, lot_no=self.origin.lot_no, production_lot='A',
            warehouse_from=self.good)
        ok, _ = _do_cancel_incoming(self.origin, 'cancel_incoming_only')
        self.assertFalse(ok)
        self.erp_delete.assert_not_called()

    def test_batch_shipment_rolls_back_when_one_quantity_is_invalid(self):
        first = self.case()
        _, second_origin, second_inspection = self.add_receipt('B')
        self.decide(80, 20, second_inspection)
        second = ReturnLog.objects.get(source_incoming=second_origin)
        self.client.force_login(self.staff)
        response = self.client.post(reverse('material:stock_return'), {
            'case_ids': [first.pk, second.pk], f'quantity_{first.pk}': 5,
            f'quantity_{second.pk}': 21, 'request_key': str(uuid4())})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ReturnShipment.objects.exists())
        self.assertEqual(MaterialStock.objects.get(warehouse=self.bad, production_lot='A').quantity, 20)

    def test_screens_and_reprint_render_with_new_labels(self):
        self.case()
        self.client.force_login(self.staff)
        response = self.client.get(reverse('material:stock_return'))
        self.assertContains(response, '반출 대기')
        self.assertContains(response, 'IN-A')
        self.assertContains(self.client.get(reverse('qms:import_inspection_detail', args=[self.inspection.pk])), '판정 되돌리기')
        shipment = self.ship(ReturnLog.objects.get())
        response = self.client.get(reverse('material:print_return_note', args=[shipment.movement_id]))
        self.assertContains(response, 'LOT / 제조 LOT')
        self.assertContains(response, self.delivery.order_no)
        self.assertEqual(self.client.get(reverse('material:print_return_note', args=[self.origin.pk])).status_code, 404)
        DeliveryOrder.objects.create(order_no='PENDING-TEST')
        self.assertContains(self.client.get(reverse('label_list')), '납품서 발행')

    def migrate_history(self):
        module = import_module('orders.migrations.0054_link_return_history')
        module.link_history(apps, SimpleNamespace(connection=connection))

    def test_legacy_unique_case_links_and_preserves_prior_confirmation(self):
        case = self.case()
        ReturnLog.objects.filter(pk=case.pk).update(source_incoming=None, delivery_item=None,
            is_confirmed=True, confirmed_at=timezone.now())
        self.migrate_history()
        case.refresh_from_db()
        self.assertEqual(case.source_incoming_id, self.origin.pk)
        self.assertEqual(case.delivery_item_id, self.item.pk)
        self.assertEqual(case.confirmed_quantity, 20)
        self.assertEqual(restored_quantities(ReturnLog.objects.all())[0][self.order.pk], 20)

    def test_ambiguous_legacy_cases_are_not_assigned_to_arbitrary_lots(self):
        self.case()
        _, _, second = self.add_receipt('B')
        self.decide(80, 20, second)
        ReturnLog.objects.update(source_incoming=None, delivery_item=None)
        self.migrate_history()
        self.assertFalse(ReturnLog.objects.filter(source_incoming__isnull=False).exists())

    def test_unlinked_historical_shipment_blocks_duplicate_return(self):
        case = self.case()
        MaterialTransaction.objects.create(transaction_no='LEGACY-RETURN',
            transaction_type='OUT_RETURN', part=self.part, quantity=5, warehouse_from=self.bad)
        with self.assertRaises(ValueError):
            self.ship(case)
        self.assertFalse(ReturnShipment.objects.exists())

    def test_original_order_must_be_identified_before_supplier_ack(self):
        case = self.case()
        shipment = self.ship(case)
        ReturnLog.objects.filter(pk=case.pk).update(delivery_item=None)
        with self.assertRaises(ValueError):
            acknowledge_return(case.pk, shipment.pk, self.supplier)
        shipment.refresh_from_db()
        self.assertIsNone(shipment.confirmed_at)

    def test_unlinked_bad_stock_remains_visible_for_review(self):
        self.client.force_login(self.staff)
        MaterialStock.objects.create(warehouse=self.bad, part=self.part, quantity=7,
                                     production_lot='LEGACY-UNLINKED')
        response = self.client.get(reverse('material:stock_return'))
        self.assertContains(response, 'LEGACY-UNLINKED')
        self.assertContains(response, '7개')

    def test_historical_linked_shipment_does_not_double_count_legacy_ack(self):
        case = self.case()
        movement = MaterialTransaction.objects.create(transaction_no='LINKED-LEGACY',
            transaction_type='OUT_RETURN', part=self.part, quantity=5,
            source_incoming=self.origin, warehouse_from=self.bad,
            lot_no=self.origin.lot_no, production_lot=self.origin.production_lot)
        ReturnLog.objects.filter(pk=case.pk).update(source_incoming=None, delivery_item=None,
            is_confirmed=True, confirmed_at=timezone.now())
        self.migrate_history()
        case.refresh_from_db()
        self.assertEqual(case.legacy_confirmed_quantity, 15)
        self.assertEqual(case.confirmed_quantity, 20)
        self.assertEqual(case.shipped_quantity, 20)
        self.assertEqual(ReturnShipment.objects.get().movement_id, movement.pk)
