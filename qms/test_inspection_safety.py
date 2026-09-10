from datetime import date
from unittest.mock import patch
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import TestCase, TransactionTestCase, RequestFactory
from material.models import MaterialStock, MaterialTransaction, Warehouse
from orders.models import Vendor, Part, DeliveryOrder, DeliveryOrderItem
from .models import ImportInspection
from .views import import_inspection_detail


class InspectionSafetyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='inspector-safety', is_superuser=True)
        self.vendor = Vendor.objects.create(code='TEST', name='Test')
        self.part = Part.objects.create(part_no='P', part_name='Part', vendor=self.vendor)
        self.wh = Warehouse.objects.create(code='8100', name='Inspection')
        self.good = Warehouse.objects.create(code='2000', name='Good')
        self.bad = Warehouse.objects.create(code='8200', name='Bad')
        self.lot = date(2026, 1, 1)
        self.trx = MaterialTransaction.objects.create(transaction_no='SAFE-IN', transaction_type='IN_MANUAL',
            part=self.part, vendor=self.vendor, quantity=100, lot_no=self.lot,
            production_lot='BATCH-A', warehouse_to=self.wh)
        self.stock = MaterialStock.objects.create(warehouse=self.wh, part=self.part, lot_no=self.lot,
            production_lot='BATCH-A', quantity=100)
        self.other = MaterialStock.objects.create(warehouse=self.wh, part=self.part, lot_no=self.lot,
            production_lot='BATCH-B', quantity=500)
        self.inspection = ImportInspection.objects.create(inbound_transaction=self.trx, lot_no=self.lot)

    def decide(self, good, bad):
        request = RequestFactory().post('/', {'decision': 'COMPLETE', 'qty_good': good, 'qty_bad': bad})
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        return import_inspection_detail(request, self.inspection.pk)

    def test_invalid_quantities_leave_everything_unchanged(self):
        for good, bad in [(110, -10), (-10, 110), (90, 0), ('bad', 100)]:
            with self.subTest(good=good, bad=bad), patch('material.erp_api.register_erp_incoming') as erp:
                self.decide(good, bad)
                self.inspection.refresh_from_db()
                self.stock.refresh_from_db()
                self.assertEqual(self.inspection.status, 'PENDING')
                self.assertEqual(self.stock.quantity, 100)
                self.assertEqual(MaterialTransaction.objects.count(), 1)
                erp.assert_not_called()

    def test_batch_preserved_and_repeat_decision_does_not_duplicate(self):
        with patch('material.erp_api.register_erp_incoming', return_value=(True, 'MOCK', None)) as erp:
            self.decide(80, 20)
            self.decide(80, 20)
            erp.assert_called_once()
        self.stock.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.stock.quantity, 0)
        self.assertEqual(self.other.quantity, 500)
        self.assertEqual(MaterialStock.objects.get(warehouse=self.good, production_lot='BATCH-A').quantity, 80)
        self.assertEqual(MaterialStock.objects.get(warehouse=self.bad, production_lot='BATCH-A').quantity, 20)
        self.assertEqual(set(MaterialTransaction.objects.filter(source_incoming=self.trx).values_list('production_lot', flat=True)), {'BATCH-A'})

    def test_stale_pending_object_cannot_process_already_completed_inspection(self):
        stale = ImportInspection.objects.get(pk=self.inspection.pk)
        ImportInspection.objects.filter(pk=stale.pk).update(status='APPROVED')
        with patch('qms.views.get_object_or_404', return_value=stale), patch('material.erp_api.register_erp_incoming') as erp:
            self.decide(100, 0)
            erp.assert_not_called()
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 100)

    def test_correct_lot_selects_correct_erp_order(self):
        delivery = DeliveryOrder.objects.create(order_no='DO-SAFETY')
        DeliveryOrderItem.objects.create(order=delivery, part_no='P', part_name='Part',
            lot_no=date(2025, 12, 31), total_qty=100, erp_order_no='WRONG', erp_order_seq='1')
        item = DeliveryOrderItem.objects.create(order=delivery, part_no='P', part_name='Part',
            lot_no=self.lot, total_qty=100, erp_order_no='CORRECT', erp_order_seq='2')
        self.trx.ref_delivery_order = delivery.order_no
        self.trx.save()
        with patch('material.erp_api.register_erp_incoming', return_value=(True, 'MOCK', None)) as erp:
            self.decide(100, 0)
            self.assertEqual(erp.call_args.kwargs['erp_order_no'], 'CORRECT')
            self.assertEqual(erp.call_args.kwargs['erp_order_seq'], '2')
        self.assertEqual(MaterialTransaction.objects.get(source_incoming=self.trx).delivery_item, item)

    def test_ambiguous_legacy_line_blocks_without_stock_or_erp_changes(self):
        delivery = DeliveryOrder.objects.create(order_no='DO-AMBIGUOUS')
        for po in ('PO1', 'PO2'):
            DeliveryOrderItem.objects.create(order=delivery, part_no='P', part_name='Part',
                lot_no=self.lot, total_qty=100, erp_order_no=po)
        self.trx.ref_delivery_order = delivery.order_no
        self.trx.save()
        with patch('material.erp_api.register_erp_incoming') as erp:
            self.decide(100, 0)
            erp.assert_not_called()
        self.inspection.refresh_from_db()
        self.stock.refresh_from_db()
        self.assertEqual(self.inspection.status, 'PENDING')
        self.assertEqual(self.stock.quantity, 100)

    def test_all_rejected_is_still_allowed(self):
        self.decide(0, 100)
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.status, 'REJECTED')
        self.assertEqual(MaterialStock.objects.get(warehouse=self.bad, production_lot='BATCH-A').quantity, 100)


class ConcurrentInspectionTests(TransactionTestCase):
    setUp = InspectionSafetyTests.setUp
    decide = InspectionSafetyTests.decide

    def test_two_simultaneous_decisions_only_apply_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connections
        barrier = Barrier(2)

        def stale_inspection(*args, **kwargs):
            # Both users have read PENDING before either attempts the conditional write.
            obj = ImportInspection.objects.select_related('inbound_transaction').get(pk=self.inspection.pk)
            barrier.wait(timeout=10)
            return obj

        def worker():
            try:
                return self.decide(100, 0).status_code
            finally:
                connections.close_all()

        with patch('qms.views.get_object_or_404', side_effect=stale_inspection), patch(
            'material.erp_api.register_erp_incoming', return_value=(True, 'MOCK', None)
        ) as erp, ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
            self.assertEqual(results, [302, 302])
            erp.assert_called_once()
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 0)
        self.assertEqual(MaterialStock.objects.get(warehouse=self.good, production_lot='BATCH-A').quantity, 100)
        self.assertEqual(MaterialTransaction.objects.filter(source_incoming=self.trx).count(), 1)
