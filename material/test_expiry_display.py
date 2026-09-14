from datetime import date

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from orders.models import Part
from .models import MaterialTransaction, RawMaterialSetting, Warehouse, WMSConfig


class ExpiryDisplayTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='display-admin', is_superuser=True)
        self.staff = User.objects.create_user(username='display-staff')
        self.staff.profile.role = 'STAFF'
        self.staff.profile.can_wms_storage_expiry = True
        self.staff.profile.save()
        part = Part.objects.create(part_no='DISPLAY-PART', part_name='Display part')
        RawMaterialSetting.objects.create(part=part, shelf_life_days=90)
        source = Warehouse.objects.create(code='4200', name='From')
        target = Warehouse.objects.create(code='4300', name='To')
        for kind in ('TRANSFER', 'TRF_ERP'):
            MaterialTransaction.objects.create(
                transaction_no=f'PRIVATE-{kind}', transaction_type=kind,
                part=part, warehouse_from=source, warehouse_to=target,
                quantity=5, lot_no=date(2026, 8, 22),
            )
        self.client.force_login(self.admin)
        self.url = reverse('material:set_expiry_display')
        self.page = reverse('material:raw_material_expiry') + '?tab=used'

    def save(self, show='false', revision=0):
        return self.client.post(self.url, {'show_references': show, 'revision': revision})

    def test_default_is_visible_and_read_does_not_create_config(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.page)
        self.assertContains(response, '구분 / 수불번호')
        self.assertContains(response, 'PRIVATE-TRANSFER')
        self.assertContains(response, '2건 수불번호')
        self.assertFalse(WMSConfig.objects.exists())

    def test_hide_omits_badges_numbers_and_expander_from_regular_html(self):
        self.assertEqual(self.save().status_code, 200)
        self.client.force_login(self.staff)
        response = self.client.get(self.page + '&show_expiry_references=true')
        self.assertNotContains(response, '구분 / 수불번호')
        self.assertNotContains(response, 'PRIVATE-TRANSFER')
        self.assertNotContains(response, 'PRIVATE-TRF_ERP')
        self.assertNotContains(response, '2건 수불번호')
        self.assertNotContains(response, 'expiry-reference-column')
        self.assertNotContains(response, 'expiryDisplayForm')
        self.assertContains(response, 'DISPLAY-PART')
        self.assertEqual(response.context['grouped_count'], 1)
        self.assertEqual(response.context['grouped_history'][0]['quantity'], 10)
        self.assertEqual(MaterialTransaction.objects.count(), 2)

    def test_admin_can_restore_after_hidden_reload(self):
        revision = self.save().json()['revision']
        response = self.client.get(self.page)
        self.assertContains(response, 'id="expiryDisplayForm"')
        self.assertContains(response, 'data-show-references="false"')
        self.assertContains(response, 'id="expiryDetailBody" class="d-none"')
        self.assertContains(response, 'expiry-reference-column d-none')
        self.assertEqual(self.save('true', revision).status_code, 200)
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(self.page), 'PRIVATE-TRANSFER')

    def test_only_permitted_admin_can_save(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.save().status_code, 403)
        self.staff.profile.role = 'ADMIN'
        self.staff.profile.can_wms_storage_expiry = False
        self.staff.profile.save()
        self.assertEqual(self.save().status_code, 403)
        self.staff.profile.can_wms_storage_expiry = True
        self.staff.profile.save()
        self.assertEqual(self.save().status_code, 200)

    def test_stale_edits_and_repeated_saves_preserve_other_settings(self):
        WMSConfig.objects.create(pk=1, audit_mode=True)
        revision = self.save().json()['revision']
        self.assertEqual(self.save('true', 0).status_code, 409)
        self.assertEqual(self.save('false', revision).json()['revision'], revision)
        self.assertTrue(WMSConfig.objects.get(pk=1).audit_mode)

    def test_get_invalid_input_and_anonymous_cannot_save(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self.save('yes').status_code, 400)
        self.assertEqual(self.save('true', -1).status_code, 400)
        self.assertEqual(self.client.post(self.url, {}).status_code, 400)
        self.assertFalse(WMSConfig.objects.exists())
        self.client.logout()
        self.assertEqual(self.save().status_code, 302)

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(self.url, {'show_references': 'false', 'revision': 0}).status_code, 403)
        client.get(self.page)
        response = client.post(self.url, {
            'show_references': 'false', 'revision': 0,
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
        })
        self.assertEqual(response.status_code, 200)
