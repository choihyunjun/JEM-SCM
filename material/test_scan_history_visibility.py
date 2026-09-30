from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from orders.models import Part
from .models import (
    MaterialStock, MaterialTransaction, ProcessTag, RawMaterialLabel,
    Warehouse, WMSConfig,
)


class ScanHistoryVisibilityTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='history-admin', is_superuser=True)
        self.client.force_login(self.admin)
        self.part = Part.objects.create(part_no='HISTORY-PART', part_name='Test material')
        self.warehouse = Warehouse.objects.create(code='3000', name='Production')
        self.settings_url = reverse('material:api_scan_history_visibility')
        self.history_url = reverse('material:api_scan_history_by_part')
        self.layout_url = reverse('material:raw_material_layout')
        self.start = datetime(2026, 5, 1, tzinfo=ZoneInfo('Asia/Seoul'))
        self.end = datetime(2026, 5, 31, 23, 59, 59, 999999, tzinfo=ZoneInfo('Asia/Seoul'))

    def save_setting(self, enabled=True, **dates):
        return self.client.post(self.settings_url, {
            'enabled': enabled,
            'date_from': '2026-05-01', 'date_to': '2026-05-31', **dates,
        }, content_type='application/json')

    def create_history(self, kind, suffix, timestamp):
        identifier = f'{kind}-{suffix}'
        fields = dict(part=self.part, quantity=25, lot_no=date(2026, 1, 1))
        if kind == 'ERP':
            MaterialTransaction.objects.create(
                **fields, transaction_no=identifier, transaction_type='TRF_ERP',
                date=timestamp, remark='ERP 수기반영', warehouse_to=self.warehouse,
            )
            return f'ERP-{identifier}'
        fields.update(part_no=self.part.part_no, part_name=self.part.part_name,
                      status='USED', used_at=timestamp)
        if kind == 'TAG':
            ProcessTag.objects.create(**fields, tag_id=identifier, used_warehouse=self.warehouse)
        else:
            RawMaterialLabel.objects.create(
                **fields, label_id=identifier,
                label_type='PALLET' if kind == 'PLT' else 'PACKAGE',
            )
        return identifier

    def history_ids(self, part_no=''):
        response = self.client.get(self.history_url, {'part_no': part_no})
        self.assertEqual(response.status_code, 200)
        return [row['tag_id'] for row in response.json()['items']]

    def test_default_keeps_history_without_creating_settings(self):
        expected = self.create_history('RM', 'DEFAULT', self.start)
        self.assertEqual(self.history_ids(), [expected])
        self.assertFalse(WMSConfig.objects.exists())

    def test_inclusive_korean_dates_filter_every_source_and_both_views(self):
        visible = set()
        hidden = set()
        for kind in ('TAG', 'RM', 'PLT', 'ERP'):
            visible.add(self.create_history(kind, 'BEFORE', self.start - timedelta(microseconds=1)))
            hidden.add(self.create_history(kind, 'START', self.start))
            hidden.add(self.create_history(kind, 'END', self.end))
            visible.add(self.create_history(kind, 'AFTER', self.end + timedelta(microseconds=1)))
        visible.add(self.create_history('RM', 'UNKNOWN', None))
        self.assertEqual(self.save_setting().status_code, 200)
        for part_no in ('', self.part.part_no):
            with self.subTest(part_no=part_no):
                self.assertEqual(set(self.history_ids(part_no)), visible)
        self.assertFalse(WMSConfig.objects.get().audit_mode)
        # OFF restores the normal history and keeps the dates for later reuse.
        self.assertEqual(self.client.post(self.settings_url, {'enabled': False},
                                         content_type='application/json').status_code, 200)
        self.assertEqual(set(self.history_ids()), visible | hidden)
        config = WMSConfig.objects.get()
        self.assertEqual(config.scan_history_hide_from, date(2026, 5, 1))

    def test_filter_runs_before_each_source_limit(self):
        visible = set()
        for kind in ('TAG', 'RM', 'PLT', 'ERP'):
            for index in range(55):
                self.create_history(kind, f'HIDDEN-{index}', self.start)
            visible.add(self.create_history(kind, 'OLDER', self.start - timedelta(days=1)))
        self.save_setting()
        self.assertEqual(set(self.history_ids()), visible)
        self.assertEqual(set(self.history_ids(self.part.part_no)), visible)

    def test_same_day_range_and_normal_result_limits(self):
        for index in range(60):
            self.create_history('RM', str(index), self.start + timedelta(minutes=index))
        self.assertEqual(len(self.history_ids()), 50)
        self.assertEqual(len(self.history_ids(self.part.part_no)), 30)
        self.assertEqual(self.save_setting(date_to='2026-05-01').status_code, 200)
        self.assertEqual(self.history_ids(), [])
        self.assertEqual(self.history_ids(self.part.part_no), [])

    def test_hiding_does_not_change_records_stock_or_audit_mode(self):
        for kind in ('TAG', 'RM', 'PLT', 'ERP'):
            self.create_history(kind, 'IMMUTABLE', self.start)
        MaterialStock.objects.create(part=self.part, warehouse=self.warehouse, quantity=100)
        WMSConfig.objects.create(audit_mode=True, show_expiry_references=False)
        models = (ProcessTag, RawMaterialLabel, MaterialTransaction, MaterialStock)
        before = {model: list(model.objects.order_by('pk').values()) for model in models}
        self.save_setting()
        self.history_ids()
        self.save_setting(False)
        for model in models:
            self.assertEqual(list(model.objects.order_by('pk').values()), before[model])
        config = WMSConfig.objects.get()
        self.assertTrue(config.audit_mode)
        self.assertFalse(config.show_expiry_references)

    def test_invalid_input_cannot_change_saved_settings(self):
        self.save_setting()
        before = WMSConfig.objects.values().get()
        invalid = [
            {}, [], {'enabled': 'true'}, {'enabled': 1}, {'enabled': True},
            {'enabled': True, 'date_from': '2026-05-32', 'date_to': '2026-06-01'},
            {'enabled': True, 'date_from': '2026-06-01', 'date_to': '2026-05-01'},
            {'enabled': True, 'date_from': None, 'date_to': '2026-05-01'},
            {'enabled': True, 'date_from': ['2026-05-01'], 'date_to': '2026-05-01'},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post(self.settings_url, payload,
                                                 content_type='application/json').status_code, 400)
                self.assertEqual(WMSConfig.objects.values().get(), before)
        self.assertEqual(self.client.post(self.settings_url, '{bad',
                                         content_type='application/json').status_code, 400)

    def test_permission_csrf_and_post_required(self):
        self.assertEqual(self.client.get(self.settings_url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.post(self.settings_url, {'enabled': False},
                                         content_type='application/json').status_code, 403)
        viewer = User.objects.create_user(username='history-viewer')
        viewer.profile.can_wms_stock_view = True
        viewer.profile.can_wms_storage_layout = True
        viewer.profile.save()
        self.client.force_login(viewer)
        self.assertEqual(self.save_setting().status_code, 403)
        self.assertNotContains(self.client.get(self.layout_url), 'id="historyVisibilityForm"')
        self.assertFalse(WMSConfig.objects.get().hide_scan_history)
        # Explicit edit permission is used by both the form and the API.
        viewer.profile.can_wms_stock_edit = True
        viewer.profile.save()
        self.assertContains(self.client.get(self.layout_url), 'id="historyVisibilityForm"')
        self.assertEqual(self.save_setting().status_code, 200)

    def test_settings_persist_across_users_and_layout_reload(self):
        self.create_history('RM', 'SHARED', self.start)
        self.save_setting()
        viewer = User.objects.create_user(username='another-history-viewer')
        viewer.profile.can_wms_stock_view = True
        viewer.profile.save()
        other_client = Client()
        other_client.force_login(viewer)
        response = other_client.get(self.history_url, {'part_no': self.part.part_no})
        self.assertEqual(response.json()['items'], [])
        page = self.client.get(self.layout_url)
        self.assertTrue(page.context['hide_scan_history'])
        self.assertContains(page, 'value="2026-05-01"')
        self.assertContains(page, 'value="2026-05-31"')
