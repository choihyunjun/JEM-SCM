from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase, override_settings

from orders.models import DeliveryOrder, DeliveryOrderItem, Incoming, LabelPrintLog, Part, Vendor
from orders.views import incoming_cancel
from qms.models import ImportInspection
from .models import MaterialStock, MaterialTransaction, Warehouse
from .views import _do_cancel_incoming


@override_settings(ERP_ENABLED=False)
class IncomingCancellationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='cancel-test', is_superuser=True)
        self.vendor = Vendor.objects.create(code='TEST', name='Test vendor')
        self.part = Part.objects.create(part_no='P1', part_name='Part 1', vendor=self.vendor)
        self.good_wh = Warehouse.objects.create(code='2000', name='Material')
        self.inspect_wh = Warehouse.objects.create(code='8100', name='Inspection')
        self.bad_wh = Warehouse.objects.create(code='8200', name='Rejected')
        self.lot = date(2026, 1, 1)
        self.erp_delete = patch('material.erp_api.delete_erp_incoming', return_value=(True, None)).start()
        self.addCleanup(patch.stopall)

    def stock(self, wh, part=None):
        return MaterialStock.objects.get(
            warehouse=wh, part=part or self.part, lot_no=self.lot,
        ).quantity

    def add_stock(self, wh, part, quantity):
        stock, _ = MaterialStock.objects.get_or_create(
            warehouse=wh, part=part, lot_no=self.lot,
        )
        stock.quantity += quantity
        stock.save()

    def receipt(self, number, good=8, bad=2, part=None, legacy=False, judged=True):
        part = part or self.part
        delivery, _ = DeliveryOrder.objects.get_or_create(
            order_no=number, defaults={'is_received': True, 'status': 'APPROVED'},
        )
        item = DeliveryOrderItem.objects.create(
            order=delivery, part_no=part.part_no, part_name=part.part_name,
            snp=good + bad, box_count=1, total_qty=good + bad, lot_no=self.lot,
        )
        label = LabelPrintLog.objects.create(
            delivery_item=None if legacy else item,
            vendor=self.vendor, part=part, part_no=part.part_no,
            printed_qty=good + bad, snp=good + bad,
        )
        trx = MaterialTransaction.objects.create(
            transaction_no=uuid4().hex[:25], transaction_type='IN_SCM',
            part=part, lot_no=self.lot, quantity=good + bad,
            warehouse_to=self.inspect_wh, ref_delivery_order=number,
        )
        inspection = ImportInspection.objects.create(
            inbound_transaction=trx, lot_no=self.lot,
            status=('APPROVED' if good else 'REJECTED') if judged else 'PENDING',
            qty_good=good if judged else 0, qty_bad=bad if judged else 0,
        )
        transfers = []
        if judged:
            for qty, wh, kind in [(good, self.good_wh, '양품'), (bad, self.bad_wh, '불량')]:
                if qty:
                    self.add_stock(wh, part, qty)
                    transfers.append(MaterialTransaction.objects.create(
                        transaction_no=uuid4().hex[:25], transaction_type='TRANSFER',
                        part=part, lot_no=self.lot, quantity=qty,
                        warehouse_from=self.inspect_wh, warehouse_to=wh,
                        source_incoming=None if legacy else trx,
                        ref_delivery_order=None if legacy and kind == '불량' else number,
                        remark='[수입검사] ' + kind,
                        erp_incoming_no='ERP-' + str(trx.pk) if kind == '양품' else None,
                    ))
            incoming = Incoming.objects.create(
                part=part, in_date=self.lot, quantity=good + bad,
                confirmed_qty=good, delivery_order_no=number,
            )
        else:
            self.add_stock(self.inspect_wh, part, good + bad)
            incoming = None
        return {'do': delivery, 'item': item, 'label': label, 'trx': trx,
                'inspection': inspection, 'transfers': transfers, 'incoming': incoming}

    def assert_preserved(self, receipt):
        for obj in [receipt['do'], receipt['item'], receipt['label'], receipt['trx'],
                    receipt['inspection'], receipt['incoming'], *receipt['transfers']]:
            if obj is not None:
                self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists())
        receipt['inspection'].refresh_from_db()
        self.assertEqual(receipt['inspection'].status, 'APPROVED')
        receipt['do'].refresh_from_db()
        self.assertTrue(receipt['do'].is_received)

    def test_reset_only_selected_receipt_preserves_other_delivery(self):
        other = self.receipt('B')
        target = self.receipt('A')
        ok, message = _do_cancel_incoming(target['trx'], 'cancel_incoming_only')
        self.assertTrue(ok, message)
        self.assert_preserved(other)
        target['inspection'].refresh_from_db()
        self.assertEqual(target['inspection'].status, 'PENDING')
        self.assertEqual(self.stock(self.good_wh), 8)
        self.assertEqual(self.stock(self.bad_wh), 2)
        self.assertEqual(self.stock(self.inspect_wh), 10)
        self.erp_delete.assert_called_once_with('ERP-' + str(target['trx'].pk))
        self.assertFalse(MaterialTransaction.objects.filter(
            pk__in=[t.pk for t in target['transfers']],
        ).exists())

    def test_delete_only_selected_receipt_preserves_other_delivery_and_label(self):
        other = self.receipt('B')
        target = self.receipt('A')
        target_pk = target['trx'].pk
        ok, message = _do_cancel_incoming(target['trx'], 'delete_all')
        self.assertTrue(ok, message)
        self.assert_preserved(other)
        self.assertFalse(MaterialTransaction.objects.filter(pk=target_pk).exists())
        self.assertFalse(LabelPrintLog.objects.filter(pk=target['label'].pk).exists())
        self.assertEqual(self.stock(self.good_wh), 8)
        self.assertEqual(self.stock(self.bad_wh), 2)
        self.erp_delete.assert_called_once_with('ERP-' + str(target_pk))

    def test_legacy_scoped_good_transfer_still_cancels(self):
        other = self.receipt('B', good=20, bad=0)
        target = self.receipt('A', good=10, bad=0, legacy=True)
        ok, message = _do_cancel_incoming(target['trx'], 'delete_all')
        self.assertTrue(ok, message)
        self.assert_preserved(other)
        self.assertEqual(self.stock(self.good_wh), 20)

    def test_legacy_unreferenced_bad_transfer_is_not_guessed(self):
        other = self.receipt('B', legacy=True)
        target = self.receipt('A', legacy=True)
        for action in ['cancel_incoming_only', 'delete_all']:
            with self.subTest(action=action):
                ok, _ = _do_cancel_incoming(target['trx'], action)
                self.assertFalse(ok)
                self.assert_preserved(target)
                self.assert_preserved(other)
                self.erp_delete.assert_not_called()
                self.assertEqual(self.stock(self.good_wh), 16)
                self.assertEqual(self.stock(self.bad_wh), 4)

    def test_single_legacy_receipt_with_unreferenced_bad_transfer_still_cancels(self):
        target = self.receipt('A', legacy=True)
        ok, message = _do_cancel_incoming(target['trx'], 'delete_all')
        self.assertTrue(ok, message)
        self.assertEqual(self.stock(self.good_wh), 0)
        self.assertEqual(self.stock(self.bad_wh), 0)

    def test_new_source_links_protect_receipts_without_delivery_numbers(self):
        other = self.receipt('B')
        target = self.receipt('A')
        MaterialTransaction.objects.filter(
            pk__in=[other['trx'].pk, target['trx'].pk,
                    *[t.pk for t in other['transfers']], *[t.pk for t in target['transfers']]],
        ).update(ref_delivery_order=None)
        target['trx'].refresh_from_db()
        ok, message = _do_cancel_incoming(target['trx'], 'cancel_incoming_only')
        self.assertTrue(ok, message)
        self.assert_preserved(other)
        self.assertEqual(self.stock(self.good_wh), 8)

    def test_erp_failure_does_not_delete_local_records(self):
        target = self.receipt('A')
        self.erp_delete.return_value = (False, 'mock error')
        for action in ['cancel_incoming_only', 'delete_all']:
            with self.subTest(action=action):
                ok, _ = _do_cancel_incoming(target['trx'], action)
                self.assertFalse(ok)
                self.assert_preserved(target)
                self.assertEqual(self.stock(self.good_wh), 8)
                self.assertEqual(self.stock(self.bad_wh), 2)

    def cancel_from_scm(self, incoming, mode):
        request = RequestFactory().post('/', {'incoming_id': incoming.pk, 'cancel_mode': mode})
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        response = incoming_cancel(request)
        self.assertEqual(response.status_code, 302)

    def test_scm_item_cancel_keeps_other_part_and_other_delivery(self):
        other = self.receipt('B')
        target = self.receipt('A')
        second_part = Part.objects.create(part_no='P2', part_name='Part 2', vendor=self.vendor)
        sibling = self.receipt('A', part=second_part)
        self.cancel_from_scm(target['incoming'], 'item')
        self.assert_preserved(other)
        self.assert_preserved(sibling)
        self.assertFalse(DeliveryOrderItem.objects.filter(pk=target['item'].pk).exists())
        self.assertFalse(Incoming.objects.filter(pk=target['incoming'].pk).exists())

    def test_scm_all_checks_ambiguous_legacy_records_before_any_erp_call(self):
        other = self.receipt('B', legacy=True)
        ambiguous = self.receipt('A', legacy=True)
        second_part = Part.objects.create(part_no='P2', part_name='Part 2', vendor=self.vendor)
        clear = self.receipt('A', part=second_part)
        self.cancel_from_scm(clear['incoming'], 'all')
        self.erp_delete.assert_not_called()
        self.assert_preserved(other)
        self.assert_preserved(ambiguous)
        self.assert_preserved(clear)

    def test_scm_cancel_without_delivery_does_not_select_unlinked_receipts(self):
        other = self.receipt('B')
        unlinked = Incoming.objects.create(
            part=self.part, in_date=self.lot, quantity=10, delivery_order_no=None,
        )
        self.cancel_from_scm(unlinked, 'all')
        self.erp_delete.assert_not_called()
        self.assert_preserved(other)
        self.assertTrue(Incoming.objects.filter(pk=unlinked.pk).exists())

    def test_scm_all_cancel_includes_pending_items_but_keeps_other_delivery(self):
        other = self.receipt('B')
        target = self.receipt('A')
        second_part = Part.objects.create(part_no='P2', part_name='Part 2', vendor=self.vendor)
        pending = self.receipt('A', part=second_part, judged=False)
        self.cancel_from_scm(target['incoming'], 'all')
        self.assert_preserved(other)
        self.assertFalse(MaterialTransaction.objects.filter(ref_delivery_order='A').exists())
        self.assertFalse(Incoming.objects.filter(delivery_order_no='A').exists())
        target['do'].refresh_from_db()
        self.assertFalse(target['do'].is_received)
        self.assertEqual(self.stock(self.inspect_wh, second_part), 0)
        self.assertTrue(DeliveryOrderItem.objects.filter(pk=pending['item'].pk).exists())
