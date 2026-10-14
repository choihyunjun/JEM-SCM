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
from orders.models import Part, Vendor
from qms.models import ImportInspection


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
