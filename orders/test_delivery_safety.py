from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db.models import Sum
from django.test import RequestFactory, TestCase, override_settings

from material.models import MaterialStock, Warehouse
from .models import DeliveryOrder, DeliveryOrderItem, LabelPrintLog, Order, Part, Vendor
from . import views


@override_settings(ERP_ENABLED=False)
class DeliveryRegistrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='delivery-test', is_superuser=True)
        self.vendor = Vendor.objects.create(code='TEST', name='Test vendor')
        self.part = Part.objects.create(part_no='P1', part_name='Part 1', vendor=self.vendor)
        self.factory = RequestFactory()

    def request(self, data):
        request = self.factory.post('/', data)
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def payload(self, lots, snps=None, order_ids=None, parts=None):
        count = len(lots)
        return {
            'part_nos[]': parts or [self.part.part_no] * count,
            'snps[]': snps or ['10'] * count,
            'box_counts[]': ['1'] * count,
            'order_ids[]': order_ids or [''] * count,
            'lot_nos[]': lots,
        }

    def assert_registration_rejected(self, data):
        response = views.create_delivery_order(self.request(data))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DeliveryOrder.objects.exists())
        self.assertFalse(DeliveryOrderItem.objects.exists())
        self.assertFalse(LabelPrintLog.objects.exists())

    def test_same_part_lot_with_different_snp_is_rejected_before_writes(self):
        self.assert_registration_rejected(self.payload(
            ['2026-01-01', '2026-01-01'], snps=['10', '20'],
        ))

    def test_same_part_lot_with_different_purchase_orders_is_rejected(self):
        orders = [
            Order.objects.create(
                vendor=self.vendor, part_no=self.part.part_no, part_name='Part',
                quantity=100, due_date=date(2026, 1, 1),
            ) for _ in range(2)
        ]
        self.assert_registration_rejected(self.payload(
            ['2026-01-01', '2026-01-01'], order_ids=[str(o.pk) for o in orders],
        ))

    def test_invalid_or_missing_lot_is_rejected_before_writes(self):
        for lots in [['2026-01-01', 'invalid'], ['']]:
            with self.subTest(lots=lots):
                self.assert_registration_rejected(self.payload(lots))
        data = self.payload(['2026-01-01'])
        data['snps[]'] = []
        self.assert_registration_rejected(data)

    def test_different_lots_register_and_receive_both_quantities(self):
        Warehouse.objects.create(code='2000', name='Material')
        views.create_delivery_order(self.request(self.payload(
            ['2026-01-01', '2026-01-02'], snps=['10', '20'],
        )))
        delivery = DeliveryOrder.objects.get()
        self.assertEqual(delivery.items.count(), 2)
        self.assertEqual(LabelPrintLog.objects.filter(delivery_item__order=delivery).count(), 2)
        with patch('material.erp_api.register_erp_incoming', return_value=(True, 'MOCK', None)):
            views.receive_delivery_order_confirm(self.request({
                'order_id': delivery.pk, 'inspection_needed': 'no',
                'direct_warehouse_code': '2000',
            }))
        self.assertEqual(MaterialStock.objects.aggregate(q=Sum('quantity'))['q'], 30)
        # Repeating receipt must still be ignored.
        views.receive_delivery_order_confirm(self.request({
            'order_id': delivery.pk, 'inspection_needed': 'no',
            'direct_warehouse_code': '2000',
        }))
        self.assertEqual(MaterialStock.objects.aggregate(q=Sum('quantity'))['q'], 30)

    def test_different_parts_with_same_lot_are_allowed(self):
        second = Part.objects.create(part_no='P2', part_name='Part 2', vendor=self.vendor)
        views.create_delivery_order(self.request(self.payload(
            ['2026-01-01', '2026-01-01'], parts=[self.part.part_no, second.part_no],
        )))
        self.assertEqual(DeliveryOrderItem.objects.count(), 2)

    def test_same_part_lot_in_another_delivery_is_allowed(self):
        existing = DeliveryOrder.objects.create(order_no='PREVIOUS')
        DeliveryOrderItem.objects.create(
            order=existing, part_no=self.part.part_no, part_name='Part',
            total_qty=10, lot_no=date(2026, 1, 1),
        )
        views.create_delivery_order(self.request(self.payload(['2026-01-01'])))
        self.assertEqual(DeliveryOrder.objects.count(), 2)
