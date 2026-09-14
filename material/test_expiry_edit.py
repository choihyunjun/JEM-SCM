from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from orders.models import Part
from .expiry import expiry_movement_history
from .models import MaterialStock, MaterialTransaction, MovementExpiryEvent, RawMaterialSetting, Warehouse


class MovementExpiryEditTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='expiry-admin', is_superuser=True)
        self.client.force_login(self.user)
        self.part = Part.objects.create(part_no='EDIT-SEAL', part_name='Seal')
        self.setting = RawMaterialSetting.objects.create(part=self.part, shelf_life_days=90)
        self.source = Warehouse.objects.create(code='4200', name='Material')
        self.target = Warehouse.objects.create(code='4300', name='Production')
        self.trx = MaterialTransaction.objects.create(
            transaction_no='EDIT-TRX', transaction_type='TRF_ERP', part=self.part,
            quantity=90, warehouse_from=self.source, warehouse_to=self.target,
        )
        self.url = reverse('material:edit_movement_expiry', args=[self.trx.pk])
        self.page = reverse('material:raw_material_expiry') + '?tab=used'

    def save(self, value='2026-11-20', revision=0, **extra):
        return self.client.post(self.url, {'expiry_date': value, 'revision': revision, **extra})

    def test_save_only_adds_manual_metadata_and_updates_display(self):
        stock = MaterialStock.objects.create(warehouse=self.target, part=self.part, quantity=90)
        original = MaterialTransaction.objects.filter(pk=self.trx.pk).values().get()
        with patch('material.erp_api.register_erp_stock_move') as erp:
            response = self.save(note='입고 자료 확인')
        self.assertEqual(response.status_code, 200)
        event = MovementExpiryEvent.objects.get()
        self.assertEqual((event.actor, event.note, event.transaction_no), (self.user, '입고 자료 확인', 'EDIT-TRX'))
        self.assertEqual(event.expiry_date, date(2026, 11, 20))
        self.assertIsNone(event.previous_date)
        self.assertEqual(MaterialTransaction.objects.filter(pk=self.trx.pk).values().get(), original)
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 90)
        erp.assert_not_called()
        row = expiry_movement_history({self.part.pk: self.setting})[0]
        self.assertTrue(row['expiry_manual'])
        self.assertEqual(row['expiry_date'], event.expiry_date)
        self.assertEqual(row['expiry_revision'], event.pk)
        page = self.client.get(self.page)
        self.assertContains(page, '2026-11-20')
        self.assertNotContains(page, 'id="expiryManual-')

    def test_edit_clear_and_duplicate_submission_keep_audit_history(self):
        first = self.save().json()['revision']
        self.assertEqual(self.save(revision=first).json()['revision'], first)
        second = self.save('2026-12-01', first).json()['revision']
        cleared = self.save('', second).json()
        self.assertEqual(cleared['expiry_date'], '')
        self.assertIsNone(cleared['used_d_day'])
        events = list(MovementExpiryEvent.objects.order_by('pk'))
        self.assertEqual(len(events), 3)
        self.assertEqual(events[1].previous_date, events[0].expiry_date)
        self.assertEqual(events[2].previous_date, events[1].expiry_date)
        self.assertIsNone(events[2].expiry_date)
        self.assertIsNone(expiry_movement_history({self.part.pk: self.setting})[0]['expiry_date'])

    def test_stale_revision_cannot_overwrite_another_edit(self):
        self.save()
        self.assertEqual(self.save('2026-12-01').status_code, 409)
        self.assertEqual(MovementExpiryEvent.objects.count(), 1)

    def test_invalid_values_do_not_write(self):
        for value, revision in [('bad', 0), ('2026-02-30', 0), ('20261120', 0), ('2026-11-20', -1), ('2026-11-20', 'bad')]:
            with self.subTest(value=value, revision=revision):
                self.assertEqual(self.save(value, revision).status_code, 400)
        self.assertEqual(self.save(note='x' * 201).status_code, 400)
        self.assertEqual(self.client.post(self.url, {'revision': 0}).status_code, 400)
        self.assertFalse(MovementExpiryEvent.objects.exists())

    def test_computed_expiry_and_out_of_scope_records_cannot_be_edited(self):
        for values in (
            {'lot_no': date(2026, 8, 22)},
            {'lot_no': None, 'warehouse_from': self.target, 'warehouse_to': self.source},
            {'warehouse_from': self.source, 'warehouse_to': self.target, 'transaction_type': 'IN_ERP'},
        ):
            MaterialTransaction.objects.filter(pk=self.trx.pk).update(**values)
            self.assertIn(self.save().status_code, (400, 404))
        MaterialTransaction.objects.filter(pk=self.trx.pk).update(transaction_type='TRANSFER')
        self.setting.delete()
        self.assertEqual(self.save().status_code, 404)
        self.assertFalse(MovementExpiryEvent.objects.exists())

    def test_admin_role_requires_expiry_permission_and_staff_cannot_edit(self):
        user = User.objects.create_user(username='ordinary')
        self.client.force_login(user)
        for role, permitted, expected in [('STAFF', True, 403), ('VENDOR', True, 403), ('ADMIN', False, 403), ('ADMIN', True, 200)]:
            user.profile.role = role
            user.profile.can_wms_storage_expiry = permitted
            user.profile.save()
            if role == 'STAFF':
                self.assertNotContains(self.client.get(self.page), 'id="expiryEditTrigger"')
            self.assertEqual(self.save().status_code, expected)

    def test_anonymous_get_and_missing_csrf_cannot_write(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.save().status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(self.url, {'expiry_date': '2026-11-20', 'revision': 0}).status_code, 403)
        page = csrf_client.get(self.page)
        self.assertContains(page, 'id="expiryEditTrigger"')
        self.assertEqual(csrf_client.post(self.url, {
            'expiry_date': '2026-11-20', 'revision': 0,
            'csrfmiddlewaretoken': csrf_client.cookies['csrftoken'].value,
        }).status_code, 200)

    def test_deleted_movement_is_not_editable_and_audit_record_survives(self):
        self.save()
        self.trx.delete()
        self.assertEqual(self.save().status_code, 404)
        event = MovementExpiryEvent.objects.get()
        self.assertIsNone(event.movement_id)
        self.assertEqual(event.transaction_no, 'EDIT-TRX')
        self.assertEqual(expiry_movement_history({self.part.pk: self.setting}), [])
