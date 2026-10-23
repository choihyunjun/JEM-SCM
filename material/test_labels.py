"""라벨 규칙: 위치 기반 이동, 재고를 따라 옮기기, 다 쓴 포대 소진, QR은 번호만."""
import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from orders.models import Part
from .labels import lookup, scan_id, sync_all, sync_labels
from .models import ERPIncomingOperation, MaterialStock, MaterialTransaction, RawMaterialLabel, Warehouse

D = Decimal


class Base(TestCase):
    def setUp(self):
        self.part = Part.objects.create(part_no='ZR700', part_name='Resin')
        self.raw = Warehouse.objects.create(code='3200', name='Raw')
        self.feed = Warehouse.objects.create(code='3000', name='Feed')
        self.lot = date(2026, 4, 1)

    def stock(self, wh, qty, lot=None):
        return MaterialStock.objects.create(warehouse=wh, part=self.part, lot_no=lot or self.lot, quantity=qty)

    def labels(self, n, wh=None, qty=25, lot=None):
        return [RawMaterialLabel.objects.create(
            label_id=f'RM-T-{RawMaterialLabel.objects.count() + 1:04d}', part=self.part, part_no='ZR700',
            part_name='Resin', lot_no=lot or self.lot, quantity=qty, status='INSTOCK', warehouse=wh)
            for _ in range(n)]

    def states(self):
        return sorted((l.warehouse.code if l.warehouse_id else '', l.status)
                      for l in RawMaterialLabel.objects.all())


class SyncRuleTests(Base):
    def test_used_up_bags_are_consumed_oldest_first_and_partial_bag_kept(self):
        self.stock(self.feed, 30)            # 4포대(100) 중 70 사용, 30 남음
        first, *_ = self.labels(4, self.feed)
        self.assertEqual(sync_labels(self.part), (0, 2))
        self.assertEqual(self.states(), [('3000', 'INSTOCK')] * 2 + [('3000', 'USED')] * 2)
        first.refresh_from_db()
        self.assertEqual(first.status, 'USED')  # 먼저 발행된(오래된) 포대가 먼저 쓰인 것으로 본다

    def test_labels_follow_stock_moved_outside_scanning(self):
        self.stock(self.raw, 50)
        self.stock(self.feed, 50)            # ERP에서 직접 3200→3000으로 50 이동
        self.labels(4, self.raw)
        self.assertEqual(sync_labels(self.part), (2, 0))
        self.assertEqual(self.states(), [('3000', 'INSTOCK')] * 2 + [('3200', 'INSTOCK')] * 2)

    def test_sync_all_is_idempotent(self):
        self.stock(self.feed, 25)
        self.labels(3, self.feed)
        self.assertEqual(sync_all(), {'moved': 0, 'consumed': 2})
        self.assertEqual(sync_all(), {'moved': 0, 'consumed': 0})

    def test_new_label_is_placed_where_its_lot_is(self):
        self.stock(self.raw, 50)
        label = RawMaterialLabel.objects.create(label_id='RM-NEW-1', part=self.part, part_no='ZR700',
                                                part_name='R', lot_no=self.lot, quantity=25, status='INSTOCK')
        self.assertEqual(label.warehouse, self.raw)


class LookupTests(Base):
    def test_qr_is_read_by_number_only(self):
        self.stock(self.raw, 25)
        self.labels(1, self.raw)
        self.assertEqual(scan_id(' rm-t-0001|ZR700|25|2026-04-01 '), 'RM-T-0001')
        info = lookup('RM-T-0001')
        self.assertEqual((info['kind'], info['part_no'], info['warehouse'], info['lot_no']),
                         ('RM', 'ZR700', '3200', '2026-04-01'))
        self.assertIsNone(lookup('RM-NOPE'))

    def test_lookup_api(self):
        self.stock(self.raw, 25)
        self.labels(1, self.raw)
        self.client.force_login(User.objects.create(username='pda', is_superuser=True))
        data = self.client.get(reverse('material:api_label_lookup', args=['RM-T-0001'])).json()
        self.assertTrue(data['success'])
        self.assertEqual(data['label']['quantity'], 25)
        self.assertFalse(self.client.get(reverse('material:api_label_lookup', args=['X-1'])).json()['success'])


@override_settings(ERP_ENABLED=True, ERP_OUTBOX_AUTODISPATCH=False)
class ScanTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create(username='pda', is_superuser=True))
        self.src = self.stock(self.raw, 50)
        self.label = self.labels(2, self.raw)[0]

    def scan(self, target='3000'):
        return self.client.post(reverse('material:api_process_tag_scan'),
                                json.dumps({'tag_id': self.label.label_id + '|ZR700|25|2026-04-01',
                                            'target_warehouse_code': target}),
                                content_type='application/json').json()

    def cancel(self):
        return self.client.post(reverse('material:api_process_tag_cancel_scan'),
                                json.dumps({'tag_id': self.label.label_id}), content_type='application/json').json()

    def test_scan_to_feed_moves_label_and_stock_without_using_it_up(self):
        self.assertTrue(self.scan()['success'])
        self.label.refresh_from_db()
        self.assertEqual((self.label.warehouse.code, self.label.status), ('3000', 'INSTOCK'))
        self.assertEqual(ERPIncomingOperation.objects.filter(kind='MOVE').count(), 1)  # ERP 이동은 한 번만
        again = self.scan()
        self.assertFalse(again['success'])
        self.assertIn('이미 3000 창고', again['error'])
        self.assertTrue(self.scan('3200')['success'])  # 현장에서 원재료창고로 되돌리기도 스캔으로

    def test_cancel_returns_label_stock_and_erp_move(self):
        self.scan()
        self.assertTrue(self.cancel()['success'])
        self.label.refresh_from_db()
        self.src.refresh_from_db()
        self.assertEqual((self.label.warehouse.code, self.src.quantity), ('3200', 50))
        self.assertEqual(ERPIncomingOperation.objects.get(kind='MOVE').status, 'CANCELLED')
        self.assertFalse(self.cancel()['success'])

    def test_cancel_blocked_once_production_used_the_material(self):
        self.scan()
        MaterialStock.objects.filter(warehouse=self.feed).update(quantity=10)  # 생산출고로 15 사용
        result = self.cancel()
        self.assertFalse(result['success'])
        self.assertIn('이미 생산에 사용된', result['error'])

    def test_used_up_label_cannot_be_scanned(self):
        RawMaterialLabel.objects.filter(pk=self.label.pk).update(status='USED')
        result = self.scan()
        self.assertFalse(result['success'])
        self.assertIn('소진', result['error'])


class VendorBoxLabelTests(Base):
    def test_vendor_box_label_number_is_looked_up_like_any_label(self):
        from .models import ProcessTag
        ProcessTag.objects.create(tag_id='DLV-00001-00001-001', part=self.part, part_no='ZR700',
                                  part_name='Resin', quantity=100, lot_no=self.lot)
        info = lookup('dlv-00001-00001-001')
        self.assertEqual((info['kind'], info['quantity'], info['lot_no']), ('DLV', 100, '2026-04-01'))


class PalletRuleTests(Base):
    """파렛트 품목(A안): 파렛트 라벨만 발행하고 포대 라벨은 발행하지 않는다."""

    def setUp(self):
        super().setUp()
        from django.utils import timezone
        from qms.models import ImportInspection
        from .models import RawMaterialSetting
        self.client.force_login(User.objects.create(username='label-admin', is_superuser=True))
        self.setting = RawMaterialSetting.objects.create(part=self.part, label_unit='PALLET')
        hold = Warehouse.objects.create(code='8100', name='Inspection')
        trx = MaterialTransaction.objects.create(transaction_no='IN-1', transaction_type='IN_MANUAL', part=self.part,
                                                 warehouse_to=hold, quantity=1000, lot_no=timezone.localdate())
        self.inspection = ImportInspection.objects.create(inbound_transaction=trx, status='APPROVED', qty_good=1000)

    def test_bag_labels_refused_pallet_labels_issued(self):
        url = reverse('material:raw_material_incoming')
        self.client.post(url, {'action': 'print_labels', 'inspection_id': self.inspection.pk,
                               'unit': 'KG', 'pkg_qty': '25', 'pkg_count': '40', 'expiry_date': ''})
        self.assertFalse(RawMaterialLabel.objects.exists())
        data = self.client.post(reverse('material:pallet_label_create'),
                                {'inspection_id': self.inspection.pk}).json()
        self.assertTrue(data['success'], data)
        self.assertEqual(list(RawMaterialLabel.objects.values_list('label_type', 'quantity')), [('PALLET', 1000)])
        page = self.client.get(url).content.decode()
        self.assertNotIn("openLabelModal('%d'" % self.inspection.pk, page)  # 포대 라벨 버튼 없음

    def test_label_unit_is_saved_from_item_setting(self):
        self.client.post(reverse('material:raw_material_setting'), {
            'part_id': self.part.pk, 'safety_stock': 0, 'warning_stock': 0, 'shelf_life_days': 365,
            'unit_weight': 25, 'label_unit': 'PACKAGE'})
        self.setting.refresh_from_db()
        self.assertEqual(self.setting.label_unit, 'PACKAGE')


class ProductTagOnlyTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create(username='tag-admin', is_superuser=True))
        self.product = Part.objects.create(part_no='PRD-1', part_name='Product')
        from .models import RawMaterialSetting
        RawMaterialSetting.objects.create(part=self.part)

    def test_product_tag_refused_for_raw_material(self):
        from .models import ProcessTag
        url = reverse('material:process_tag_print')
        self.client.post(url, {'part_no': 'ZR700', 'quantity': '25', 'lot_no': '2026-04-01', 'print_count': 1})
        self.assertFalse(ProcessTag.objects.exists())
        self.client.post(url, {'part_no': 'PRD-1', 'quantity': '100', 'lot_no': '2026-04-01', 'print_count': 2})
        self.assertEqual(ProcessTag.objects.filter(part_no='PRD-1').count(), 2)

    def test_lot_allocation_tags_skip_raw_materials(self):
        from .models import ProcessTag
        items = [{'part_no': 'ZR700', 'quantity': 25, 'lot_no': '2026-04-01'},
                 {'part_no': 'PRD-1', 'quantity': 100, 'lot_no': '2026-04-01'}]
        self.client.post(reverse('material:lot_allocation_print'), {'items': json.dumps(items)})
        self.assertEqual(list(ProcessTag.objects.values_list('part_no', flat=True)), ['PRD-1'])


class ReissueTests(Base):
    """전환용 일괄 재발행: 실재고 LOT대로 새 라벨, 그 창고 기존 라벨은 폐기."""

    def setUp(self):
        super().setUp()
        from .models import RawMaterialSetting
        self.client.force_login(User.objects.create(username='reissue', is_superuser=True))
        RawMaterialSetting.objects.create(part=self.part, unit_weight=25, shelf_life_days=180)
        self.stock(self.raw, 60)
        self.stock(self.raw, 25, date(2026, 5, 1))
        self.old = self.labels(9, self.raw)   # 실물과 안 맞는 예전 라벨

    def test_new_labels_follow_lot_stock_and_old_ones_are_disposed(self):
        url = reverse('material:label_reissue')
        page = self.client.get(url, {'warehouse': '3200'})
        self.assertContains(page, 'ZR700')
        response = self.client.post(url, {'warehouse': '3200', 'part_ids': [self.part.pk]})
        self.assertEqual(response.status_code, 302)
        live = RawMaterialLabel.objects.filter(status='INSTOCK').order_by('lot_no', 'quantity')
        self.assertEqual([(l.lot_no.month, l.quantity) for l in live],
                         [(4, D(10)), (4, D(25)), (4, D(25)), (5, D(25))])  # 60 = 25+25+10, 25
        self.assertEqual(live[0].expiry_date, date(2026, 9, 28))     # LOT + 보관기간 180일
        self.assertEqual(RawMaterialLabel.objects.filter(status='DISPOSED').count(), 9)
        result = self.client.get(response.url)
        self.assertContains(result, '포대 라벨 출력 (4장)')
        disposed = self.client.post(reverse('material:api_process_tag_scan'),
                                    json.dumps({'tag_id': self.old[0].label_id}), content_type='application/json').json()
        self.assertIn('폐기', disposed['error'])

    def test_pallet_items_get_one_pallet_label_per_lot(self):
        from .models import RawMaterialSetting
        RawMaterialSetting.objects.filter(part=self.part).update(label_unit='PALLET')
        self.client.post(reverse('material:label_reissue'), {'warehouse': '3200', 'part_ids': [self.part.pk]})
        self.assertEqual(sorted(RawMaterialLabel.objects.filter(status='INSTOCK').values_list('label_type', 'quantity')),
                         [('PALLET', D(25)), ('PALLET', D(60))])
