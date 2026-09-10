from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase, override_settings

from material.models import MaterialStock, MaterialTransaction, Warehouse
from orders.models import DeliveryOrder, DeliveryOrderItem, Part, Vendor
from .models import ImportInspection
from .views import import_inspection_detail


@override_settings(ERP_ENABLED=False)
class InspectionSourceLinkTests(TestCase):
    def test_standalone_good_and_bad_movements_keep_source(self):
        self.check_source_links(None)

    def test_scm_good_and_bad_movements_keep_source_and_delivery(self):
        self.check_source_links('TEST-DO')

    def check_source_links(self, delivery_no):
        user = User.objects.create(username='inspector', is_superuser=True)
        vendor = Vendor.objects.create(code='TEST', name='Test vendor')
        part = Part.objects.create(part_no='P1', part_name='Part', vendor=vendor)
        wh = Warehouse.objects.create(code='8100', name='Inspection')
        Warehouse.objects.create(code='2000', name='Material')
        Warehouse.objects.create(code='8200', name='Rejected')
        lot = date(2026, 1, 1)
        if delivery_no:
            delivery = DeliveryOrder.objects.create(order_no=delivery_no)
            DeliveryOrderItem.objects.create(
                order=delivery, part_no=part.part_no, part_name=part.part_name,
                lot_no=lot, total_qty=10, snp=10, box_count=1,
            )
        trx = MaterialTransaction.objects.create(
            transaction_no='TEST-IN', transaction_type='IN_MANUAL', part=part,
            quantity=10, lot_no=lot, warehouse_to=wh, ref_delivery_order=delivery_no,
        )
        MaterialStock.objects.create(warehouse=wh, part=part, lot_no=lot, quantity=10)
        inspection = ImportInspection.objects.create(inbound_transaction=trx, lot_no=lot)
        request = RequestFactory().post('/', {'decision': 'COMPLETE', 'qty_good': '8', 'qty_bad': '2'})
        request.user = user
        request.session = {}
        request._messages = FallbackStorage(request)
        with patch('material.erp_api.register_erp_incoming', return_value=(True, 'MOCK', None)):
            import_inspection_detail(request, inspection.pk)
        inspection.refresh_from_db()
        self.assertEqual(inspection.status, 'APPROVED')
        transfers = MaterialTransaction.objects.filter(source_incoming=trx)
        self.assertEqual(transfers.count(), 2)
        self.assertEqual(sorted(transfers.values_list('quantity', flat=True)), [2, 8])
        self.assertTrue(all(t.ref_delivery_order == trx.ref_delivery_order for t in transfers))
