"""Render menu destinations using real views and an isolated test database.

Run with --settings=config.test_settings. External HTTP is blocked.
"""
import re
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from material.models import MaterialTransaction, Warehouse
from orders.models import DeliveryOrder, LabelPrintLog, Order, Part, UserProfile, Vendor
from qms.models import ImportInspection, NonConformance, OutgoingInspection, VendorClaim


@override_settings(ERP_ENABLED=False, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class MenuPageRenderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('ui-review', is_superuser=True, is_staff=True)

    def setUp(self):
        self.client.force_login(self.user)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('External HTTP prohibited during page checks'))
        network.start()
        self.addCleanup(network.stop)

    def test_menu_destinations_render(self):
        templates = [
            'orders/templates/base.html', 'qms/templates/qms/qms_base.html',
            'material/templates/material/material_base.html',
            'material/templates/material/production_base.html',
        ]
        routes = set()
        for name in templates:
            source = (Path(settings.BASE_DIR) / name).read_text(encoding='utf-8')
            routes.update(re.findall(r"{%\s*url\s+'([^']+)'\s*%}", source))
        for route in sorted(routes - {'logout'}):
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200, route)
                self.assertIn(b'</html>', response.content.lower(), route)

    def test_inspection_pages_with_real_rows(self):
        vendor = Vendor.objects.create(code='UI-TEST', name='Inspection supplier')
        part = Part.objects.create(part_no='UI-PART', part_name='Inspection part', vendor=vendor)
        warehouse = Warehouse.objects.create(code='8100', name='Inspection warehouse')
        for index, status in enumerate(['PENDING', 'APPROVED', 'REJECTED']):
            movement = MaterialTransaction.objects.create(
                transaction_no=f'UI-IN-{index}', transaction_type='IN_MANUAL',
                part=part, vendor=vendor, warehouse_to=warehouse, quantity=10,
            )
            ImportInspection.objects.create(
                inbound_transaction=movement, status=status,
                attachment=f'qms/inspection/sample-{index}.pdf',
                qty_good=8 if status == 'APPROVED' else 0,
                qty_bad=2 if status == 'APPROVED' else 0,
            )
        today = timezone.localdate().isoformat()
        for route in ['qms:import_inspection_list', 'qms:inspection_attachment_list']:
            for query in [{}, {'q': part.part_no, 'start_date': today, 'end_date': today}]:
                with self.subTest(route=route, query=query):
                    response = self.client.get(reverse(route), query)
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, part.part_no)
                    self.assertEqual(response.context['page_obj'].paginator.count, 3)

    def test_supplier_delivery_registration_redirect_and_print_pages(self):
        supplier = User.objects.create_user('ui-supplier')
        vendor = Vendor.objects.create(code='UI-SUPPLIER', name='UI supplier', user=supplier)
        UserProfile.objects.filter(user=supplier).update(
            role='VENDOR', account_type='VENDOR', org=vendor.organization,
            can_scm_label_view=True, can_scm_label_edit=True,
        )
        part = Part.objects.create(part_no='UI-DELIVERY', part_name='Delivery part', vendor=vendor)
        order = Order.objects.create(
            vendor=vendor, part_no=part.part_no, part_name=part.part_name,
            quantity=100, due_date=timezone.localdate(), approved_at=timezone.now(),
            erp_order_no='UI-PO', erp_order_seq='1',
        )
        self.client.force_login(supplier)
        self.assertContains(self.client.get(reverse('label_list')), part.part_no)
        response = self.client.post(reverse('create_delivery_order'), {
            'part_nos[]': [part.part_no], 'snps[]': ['10'], 'box_counts[]': ['2'],
            'order_ids[]': [str(order.pk)], 'lot_nos[]': [timezone.localdate().isoformat()],
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        delivery = DeliveryOrder.objects.get()
        item = delivery.items.get()
        self.assertEqual(item.total_qty, 20)
        self.assertEqual(item.linked_order, order)
        self.assertEqual(LabelPrintLog.objects.get(delivery_item=item).printed_qty, 20)
        self.assertContains(response, delivery.order_no)
        for route in ['label_print', 'delivery_note_print']:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route, args=[delivery.pk])).status_code, 200)

    def test_quality_registration_forms_render(self):
        for route in ['m4_create', 'change_request_create', 'outgoing_create',
                      'nc_create', 'capa_create', 'claim_create', 'rating_create',
                      'isir_create', 'voc_create', 'gauge_create', 'qdoc_create']:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse('qms:' + route)).status_code, 200)

    def test_quality_detail_and_edit_pages_render(self):
        vendor = Vendor.objects.create(code='UI-DETAIL', name='Detail supplier')
        nc = NonConformance.objects.create(
            nc_no='UI-NC', vendor=vendor.organization, source='INCOMING',
            occurred_date=timezone.localdate(), part_no='UI-PART', part_name='Part', defect_qty=1,
        )
        claim = VendorClaim.objects.create(
            claim_no='UI-CLAIM', vendor=vendor.organization, issue_date=timezone.localdate(),
            part_no='UI-PART', part_name='Part', claim_qty=1, issued_by=self.user,
        )
        outgoing = OutgoingInspection.objects.create(
            inspection_no='UI-OUT', inspection_date=timezone.localdate(),
            part_no='UI-PART', part_name='Part', total_qty=10,
        )
        for prefix, instance in [('nc', nc), ('claim', claim), ('outgoing', outgoing)]:
            for suffix in ['detail', 'edit']:
                route = f'qms:{prefix}_{suffix}'
                with self.subTest(route=route):
                    self.assertEqual(self.client.get(reverse(route, args=[instance.pk])).status_code, 200)
