from datetime import date

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from orders.models import Part
from .expiry import expiry_movement_history
from .models import (
    MaterialStock, MaterialTransaction, MovementExpiryEvent,
    MovementVisibilityEvent, RawMaterialSetting, Warehouse,
)


class MovementVisibilityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='visibility-admin', is_superuser=True)
        self.client.force_login(self.user)
        self.part = Part.objects.create(part_no='HIDE-SEAL', part_name='Seal')
        self.setting = RawMaterialSetting.objects.create(part=self.part, shelf_life_days=90)
        self.source = Warehouse.objects.create(code='4200', name='Material')
        self.target = Warehouse.objects.create(code='4300', name='Production')
        self.trx = MaterialTransaction.objects.create(
            transaction_no='HIDE-TRX', transaction_type='TRF_ERP', part=self.part,
            quantity=90, warehouse_from=self.source, warehouse_to=self.target,
        )
        self.url = reverse('material:set_movement_visibility', args=[self.trx.pk])
        self.page = reverse('material:raw_material_expiry') + '?tab=used'

    def hide(self, hidden='true', revision=0):
        return self.client.post(self.url, {'hidden': hidden, 'revision': revision})

    def test_hide_and_restore_persist_without_changing_stock_or_expiry(self):
        stock = MaterialStock.objects.create(warehouse=self.target, part=self.part, quantity=90)
        expiry = MovementExpiryEvent.objects.create(
            movement=self.trx, transaction_no=self.trx.transaction_no,
            expiry_date=date(2026, 11, 20), actor=self.user,
        )
        original = MaterialTransaction.objects.filter(pk=self.trx.pk).values().get()
        response = self.hide()
        self.assertEqual(response.status_code, 200)
        revision = response.json()['revision']
        self.assertEqual(expiry_movement_history({self.part.pk: self.setting}), [])
        hidden_rows = expiry_movement_history({self.part.pk: self.setting}, include_hidden=True)
        self.assertEqual(len(hidden_rows), 1)
        self.assertTrue(hidden_rows[0]['history_hidden'])
        self.assertEqual(hidden_rows[0]['expiry_date'], expiry.expiry_date)
        self.assertEqual(hidden_rows[0]['visibility_revision'], revision)
        page = self.client.get(self.page)
        self.assertEqual(page.context['used_count'], 0)
        self.assertContains(page, 'class="expiry-history-row d-none"')
        self.assertContains(page, '다시 표시')
        self.assertEqual(self.hide('false', revision).status_code, 200)
        self.assertEqual(self.client.get(self.page).context['used_count'], 1)
        self.assertFalse(expiry_movement_history({self.part.pk: self.setting})[0]['history_hidden'])
        self.assertEqual(MaterialTransaction.objects.filter(pk=self.trx.pk).values().get(), original)
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 90)
        self.assertEqual(MovementExpiryEvent.objects.count(), 1)
        self.assertEqual(MovementVisibilityEvent.objects.count(), 2)

    def test_only_selected_movement_is_hidden_even_with_same_part_and_lot(self):
        MaterialTransaction.objects.create(
            transaction_no='KEEP-TRX', transaction_type='TRANSFER', part=self.part,
            quantity=90, warehouse_from=self.source, warehouse_to=self.target,
        )
        self.hide()
        rows = expiry_movement_history({self.part.pk: self.setting})
        self.assertEqual([row['transaction_no'] for row in rows], ['KEEP-TRX'])

    def test_staff_does_not_receive_hidden_records_even_with_query_parameter(self):
        self.hide()
        staff = User.objects.create_user(username='visibility-staff')
        staff.profile.role = 'STAFF'
        staff.profile.can_wms_storage_expiry = True
        staff.profile.save()
        self.client.force_login(staff)
        page = self.client.get(self.page + '&include_hidden=true')
        self.assertNotContains(page, 'HIDE-TRX')
        self.assertEqual(page.context['used_count'], 0)
        self.assertEqual(self.hide().status_code, 403)

    def test_stale_revision_and_retries_do_not_duplicate_history(self):
        revision = self.hide().json()['revision']
        self.assertEqual(self.hide('false').status_code, 409)
        self.assertEqual(self.hide('true', revision).json()['revision'], revision)
        self.assertEqual(MovementVisibilityEvent.objects.count(), 1)

    def test_computed_lot_is_hideable_but_other_routes_are_not(self):
        self.trx.lot_no = date(2026, 8, 22)
        self.trx.save()
        self.assertEqual(self.hide().status_code, 200)
        self.trx.warehouse_to = self.source
        self.trx.save()
        self.assertEqual(self.hide().status_code, 404)
        self.assertEqual(MovementVisibilityEvent.objects.count(), 1)

    def test_bad_input_and_missing_csrf_cannot_write(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        for value, revision in [('yes', 0), ('true', -1), ('false', 'bad')]:
            self.assertEqual(self.hide(value, revision).status_code, 400)
        self.assertEqual(self.client.post(self.url, {'revision': 0}).status_code, 400)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(self.url, {'hidden': 'true', 'revision': 0}).status_code, 403)
        self.assertFalse(MovementVisibilityEvent.objects.exists())

    def test_admin_requires_expiry_permission(self):
        admin = User.objects.create_user(username='visibility-role-admin')
        admin.profile.role = 'ADMIN'
        admin.profile.save()
        self.client.force_login(admin)
        self.assertEqual(self.hide().status_code, 403)
        admin.profile.can_wms_storage_expiry = True
        admin.profile.save()
        self.assertEqual(self.hide().status_code, 200)

    def test_audit_survives_movement_deletion_and_deleted_record_cannot_be_restored(self):
        revision = self.hide().json()['revision']
        self.trx.delete()
        event = MovementVisibilityEvent.objects.get()
        self.assertEqual(event.transaction_no, 'HIDE-TRX')
        self.assertEqual(event.actor, self.user)
        self.assertIsNone(event.movement_id)
        self.assertEqual(self.hide('false', revision).status_code, 404)
