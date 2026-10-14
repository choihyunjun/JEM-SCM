"""Exercise the repair with real DDL in an isolated test database."""
from importlib import import_module
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from orders.models import Part, Vendor
from .models import PurchaseOrderRequest


class PurchaseRequestNoteRepairTests(TransactionTestCase):
    def setUp(self):
        # Load the actual historical state; do not apply the whole migration
        # chain to the model-built, isolated test database.
        with override_settings(MIGRATION_MODULES={}):
            self.state = MigrationLoader(None).project_state([
                ('material', '0058_scan_history_visibility'),
            ])
        self.migration = import_module(
            'material.migrations.0059_repair_purchaseorderrequest_note'
        ).Migration('0059_repair_purchaseorderrequest_note', 'material')
        self.operation = self.migration.operations[0]
        user = User.objects.create_user('note-repair')
        vendor = Vendor.objects.create(code='NOTE-REPAIR', name='Repair supplier')
        part = Part.objects.create(part_no='NOTE-REPAIR', part_name='Repair part', vendor=vendor)
        self.request = PurchaseOrderRequest.objects.create(
            request_no='POR-REPAIR', part=part, part_no=part.part_no,
            part_name=part.part_name, request_qty=25, status='APPROVED',
            reason='Keep the original request', requested_by=user, updated_by=user,
            note='Keep the existing note',
        )

    def apply_repair(self):
        with connection.schema_editor() as editor:
            self.operation.database_forwards('material', editor, self.state, self.state)

    def test_missing_column_is_restored_without_changing_existing_request(self):
        before = PurchaseOrderRequest.objects.values().get(pk=self.request.pk)
        before['note'] = ''
        with connection.schema_editor() as editor:
            editor.remove_field(PurchaseOrderRequest, PurchaseOrderRequest._meta.get_field('note'))
        try:
            self.apply_repair()
            self.assertEqual(PurchaseOrderRequest.objects.values().get(pk=self.request.pk), before)
            self.request.refresh_from_db()
            self.request.note = 'New note after recovery'
            self.request.save(update_fields=['note'])
            self.request.refresh_from_db()
            self.assertEqual(self.request.note, 'New note after recovery')
            output = StringIO()
            call_command('check_deployment', stdout=output)
            self.assertIn('PASS:', output.getvalue())
        finally:
            # Ensure test teardown can flush even if an assertion fails.
            self.apply_repair()

    def test_existing_column_and_notes_are_untouched_on_repeat_and_reverse(self):
        before = PurchaseOrderRequest.objects.values().get(pk=self.request.pk)
        with CaptureQueriesContext(connection) as queries:
            self.apply_repair()
            self.apply_repair()
            with connection.schema_editor() as editor:
                self.operation.database_backwards('material', editor, self.state, self.state)
        for query in queries:
            self.assertNotRegex(query['sql'].lstrip().upper(), r'^(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|REPLACE)\b')
        self.assertEqual(PurchaseOrderRequest.objects.values().get(pk=self.request.pk), before)
