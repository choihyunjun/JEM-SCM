"""ERP 단일 BOM 동기화: 하위 반제품까지 등록해야 소요량이 ERP와 같게 풀린다."""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from .bom_jobs import build_snapshot
from .erp_api import sync_single_bom
from .models import BOMItem, Product


def row(parent, child, name, qty, seq):
    return {'itemparentCd': parent, 'itemparentNm': parent + ' ASSY', 'itemchildCd': child, 'itemchildNm': name,
            'realQt': qty, 'justQt': qty, 'lossRt': 0, 'bomSq': seq, 'useYn': '1'}


# 2026-11-04 운영: F-446939-71-1800만 등록되고 반제품 1811이 빠져 ZR720이 풀리지 않았다
ERP = {
    '1800': [row('1800', 'SPRING', 'SPRING', 1, 2), row('1800', '1811', 'CHAIN BLADE', 1, 3)],
    '1811': [row('1811', 'ZR720', 'PA66/H TA217HS BK', 0.02302, 1)],
}


def fake_erp(part_no):
    return True, ERP.get(part_no, []), None


class SingleBomSyncTests(TestCase):
    def test_sub_assembly_is_registered_and_explodes_like_erp(self):
        with patch('material.erp_api.fetch_erp_bom', side_effect=fake_erp):
            ok, count, err = sync_single_bom('1800')
        self.assertEqual((ok, count, err), (True, 2, None))
        self.assertEqual(set(Product.objects.values_list('part_no', flat=True)), {'1800', '1811'})

        templates, _, _ = build_snapshot([{'part_no': '1800', 'qty': 80000, 'need_date': ''}])
        flat = {i['child_part_no']: i['required_qty'] for i in templates['1800']['items']}
        self.assertEqual(set(flat), {'SPRING', 'ZR720'})
        self.assertAlmostEqual(flat['ZR720'], 0.02302)
        semi = [i['child_part_no'] for i in templates['1800']['structured_items'] if i['is_semi']]
        self.assertEqual(semi, ['1811'])

    def test_existing_sub_assembly_is_left_alone(self):
        product = Product.objects.create(part_no='1811', part_name='CHAIN BLADE', is_active=True)
        BOMItem.objects.create(product=product, seq=1, child_part_no='ZR720', child_part_name='OLD', required_qty=1)
        with patch('material.erp_api.fetch_erp_bom', side_effect=fake_erp) as fetch:
            sync_single_bom('1800')
        self.assertNotIn('1811', [c.args[0] for c in fetch.call_args_list])
        self.assertEqual(product.bom_items.get().child_part_name, 'OLD')

    def test_sub_assembly_lookup_failure_is_reported_not_silently_skipped(self):
        def flaky(part_no):
            return (False, None, 'timeout') if part_no == '1811' else fake_erp(part_no)
        with patch('material.erp_api.fetch_erp_bom', side_effect=flaky):
            ok, count, err = sync_single_bom('1800')
        self.assertTrue(ok)
        self.assertIn('1811', err)
        self.assertFalse(Product.objects.filter(part_no='1811').exists())

    def test_missing_sub_command_reports_then_applies(self):
        parent = Product.objects.create(part_no='1800', part_name='TENSIONER', is_active=True)
        for seq, (child, name) in enumerate([('SPRING', 'SPRING'), ('1811', 'CHAIN BLADE')], 1):
            BOMItem.objects.create(product=parent, seq=seq, child_part_no=child, child_part_name=name, required_qty=1)
        out = StringIO()
        with patch('material.erp_api.fetch_erp_bom', side_effect=fake_erp):
            call_command('sync_bom', '--missing-sub', stdout=out)
            self.assertIn('반제품 누락: 1811', out.getvalue())
            self.assertFalse(Product.objects.filter(part_no='1811').exists())
            call_command('sync_bom', '--missing-sub', '--apply', stdout=out)
        self.assertEqual(Product.objects.get(part_no='1811').bom_items.get().child_part_no, 'ZR720')
