"""성형 원단위 중량 관리: 파지 비고 해석, 재료 행별 중량 계산, BOM 보완, 화면·엑셀."""
from datetime import datetime
from io import BytesIO
from unittest.mock import patch

import openpyxl
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.utils import timezone

from orders.models import Part
from .models import BOMItem, MaterialTransaction, MoldingMaster, Product, ResinType, Warehouse
from .resin_yield import build_report, parse_scrap, resin_yield, resin_yield_excel


def erp_rows(*rows):
    return True, {'resultData': [dict(zip(('equipNm', 'itemCd', 'goodQt', 'badQt', 'remarkDc'), r),
                                      wrDt='20261005', itemNm='', wrCd='WR1') for r in rows]}, None


class ParseScrapTests(TestCase):
    def test_five_digits_with_leading_zeros_and_m7_four_digits(self):
        self.assertEqual(parse_scrap('01475', 'M311'), (1475, None))
        self.assertEqual(parse_scrap('00125', 'M204'), (125, None))
        self.assertEqual(parse_scrap('6100', 'M702'), (6100, None))
        self.assertEqual(parse_scrap('A670 01036', 'M105'), (1036, None))

    def test_empty_is_zero_and_other_text_is_flagged(self):
        self.assertEqual(parse_scrap(None, 'M105'), (0, None))
        self.assertEqual(parse_scrap('A', 'M105'), (0, '숫자 형식 아님'))
        self.assertEqual(parse_scrap('6500', 'M311')[0], 6500)
        self.assertIn('4자리', parse_scrap('6500', 'M311')[1])


class ReportTests(TestCase):
    def setUp(self):
        cache.clear()
        # 재료 2행(재료1 + M/B): 파지는 총소요량 비율 9:1로 나뉜다
        MoldingMaster.objects.create(part_no='P-1', part_name='CASE', material_type='재료1', material_part_no='ZR1',
                                     material_name='PC TEST', cavity=2, product_weight=10, runner_weight=4,
                                     total_material=9)
        MoldingMaster.objects.create(part_no='P-1', part_name='CASE', material_type='M/B', material_part_no='ZR9',
                                     material_name='MB BLACK', cavity=2, product_weight=1, runner_weight=0,
                                     total_material=1)
        ResinType.objects.create(material_part_no='ZR1', resin_type='PC')
        part = Part.objects.create(part_no='P-1', part_name='CASE')
        molding = Warehouse.objects.create(code='3000', name='3공장 창고')
        assembly = Warehouse.objects.create(code='4300', name='제조현장')
        bad = Warehouse.objects.create(code='8000', name='불량창고')
        common = dict(part=part, warehouse_from=molding, warehouse_to=bad,
                      date=timezone.make_aware(datetime(2026, 10, 10)))
        MaterialTransaction.objects.create(transaction_no='MV1', transaction_type='TRF_ERP', quantity=20, **common)
        MaterialTransaction.objects.create(transaction_no='ADJ', transaction_type='ADJ_ERP_IN', quantity=999, **common)
        # 조립 현장에서 나온 불량은 성형 원단위에 넣지 않는다
        MaterialTransaction.objects.create(transaction_no='MV3', transaction_type='TRF_ERP', quantity=300,
                                           **{**common, 'warehouse_from': assembly})
        MaterialTransaction.objects.create(transaction_no='MV2', transaction_type='TRF_ERP', quantity=5,
                                           **{**common, 'part': Part.objects.create(part_no='ASSY-1')})
        # 이 달 생산이 없는 성형품의 검사불량 (이전 생산분)
        MoldingMaster.objects.create(part_no='OLD-1', part_name='COVER', material_type='재료1', material_part_no='ZR1',
                                     material_name='PC TEST', cavity=1, product_weight=50, runner_weight=5,
                                     total_material=55)
        MaterialTransaction.objects.create(transaction_no='MV4', transaction_type='TRF_ERP', quantity=4,
                                           **{**common, 'part': Part.objects.create(part_no='OLD-1')})

    def report(self):
        with patch('material.resin_yield.call_erp_api', return_value=erp_rows(
                ('M311', 'P-1', 1000, 10, '02000'), ('M311', 'P-1', 500, 0, None),
                ('M311', 'X-9', 100, 0, '00500'), ('A01', 'P-1', 9999, 0, '99999'))):
            return build_report(2026, 10)

    def test_weights_per_material_line(self):
        rep = self.report()
        main, mb = rep['lines']
        self.assertEqual((main['material_type'], main['resin'], mb['resin']), ('재료1', 'PC', 'M/B'))
        self.assertEqual(main['produced'], 1510)  # 성형 설비(M*) 실적만
        self.assertAlmostEqual(main['good_kg'], (1500 - 20) * 10 / 1000)  # 검사불량은 양품에서 빼서 스크랩으로
        self.assertAlmostEqual(main['bad_kg'], 0.1)
        self.assertAlmostEqual(main['inspect_kg'], 0.2)  # 3000 출발만, 재고 맞춤 기록 제외
        self.assertAlmostEqual(main['runner_kg'], 1510 * 4 / 2 / 1000)
        self.assertAlmostEqual(main['scrap_kg'], 1.8)
        self.assertAlmostEqual(mb['scrap_kg'], 0.2)
        self.assertAlmostEqual(main['unit_standard'], 12)
        self.assertAlmostEqual(main['total_kg'], 15 + 0.1 + 3.02 + 1.8)  # 총 사용량 = 실제 투입량

    def test_summaries_count_production_once_and_list_unregistered_parts(self):
        rep = self.report()
        self.assertEqual(rep['totals']['produced'], 1510)
        self.assertEqual([u['part_no'] for u in rep['unregistered']], ['X-9'])
        self.assertEqual(rep['unregistered_scrap_kg'], 0.5)
        self.assertEqual({g['key'] for g in rep['by_resin']}, {'PC', 'M/B'})

    def test_prior_production_defects_are_kept_out_of_the_table(self):
        rep = self.report()
        self.assertNotIn('OLD-1', {r['part_no'] for r in rep['lines']})
        self.assertEqual([(d['part_no'], d['defect_qty']) for d in rep['prior_defects']], [('OLD-1', 4)])
        self.assertAlmostEqual(rep['prior_defect_kg'], 0.2)

    def test_page_and_excel(self):
        request = RequestFactory().get('/', {'month': '2026-10'})
        request.user = User.objects.create(username='ry', is_superuser=True)
        with patch('material.resin_yield.call_erp_api', return_value=erp_rows(('M311', 'P-1', 10, 0, '00100'))):
            self.assertEqual(resin_yield(request).status_code, 200)
            response = resin_yield_excel(request)
        wb = openpyxl.load_workbook(BytesIO(response.content))
        self.assertEqual(wb.sheetnames, ['원단위 중량 관리기준', '원재료별 사용량', '확인 필요'])
        ws = wb['원단위 중량 관리기준']
        self.assertEqual((ws['A1'].value, ws['B4'].value), ('생산품목별 원단위 중량 관리기준 자료 (2026년 10월)', 'P-1'))
        self.assertIn('[ 수지별 소계 ]', [ws.cell(r, 1).value for r in range(1, ws.max_row + 1)])

    def test_erp_failure_is_reported(self):
        with patch('material.resin_yield.call_erp_api', return_value=(False, None, 'timeout')):
            self.assertEqual(build_report(2026, 9), {'error': 'timeout'})


class BomFallbackTests(TestCase):
    def setUp(self):
        cache.clear()
        # -D는 기본 코드와 다른 재료다 (ZR253 PA66 / ZR253-D POM): 접미어까지 정확히 일치할 때만 재질 표 사용
        ResinType.objects.create(material_part_no='ZR276', resin_type='PA66')
        ResinType.objects.create(material_part_no='ZR276-D', resin_type='PC+ABS')
        for part_no in ('B-1', 'B-2'):
            product = Product.objects.create(part_no=part_no, part_name='DECO COVER')
            BOMItem.objects.create(product=product, child_part_no='ZR276-D', child_part_name='PC+ABS HP-1000XL',
                                   required_qty='0.348')
            BOMItem.objects.create(product=product, child_part_no='SCREW-1', child_part_name='SCREW', required_qty=2)

    def report(self):
        with patch('material.resin_yield.call_erp_api', return_value=erp_rows(
                ('M702', 'B-1', 100, 2, '6100'), ('M311', 'B-2', 100, 2, '01000'))):
            return build_report(2026, 10)

    def test_m7_part_from_bom_is_runnerless_single_cavity(self):
        row = next(r for r in self.report()['lines'] if r['part_no'] == 'B-1')
        self.assertEqual((row['source'], row['material_part_no'], row['resin']), ('BOM', 'ZR276-D', 'PC+ABS'))
        self.assertEqual((row['runner_included'], row['cavity'], row['product_weight'], row['runner_weight']),
                         (False, 1, 348.0, 0.0))
        self.assertAlmostEqual(row['good_kg'], 34.8)
        self.assertAlmostEqual(row['bad_kg'], 0.696)
        self.assertAlmostEqual(row['scrap_kg'], 6.1)

    def test_other_machines_bom_quantity_includes_runner(self):
        rep = self.report()
        row = next(r for r in rep['lines'] if r['part_no'] == 'B-2')
        self.assertTrue(row['runner_included'])
        self.assertEqual(row['runner_kg'], 0)
        self.assertEqual((rep['bom_parts'], rep['unregistered']), (['B-2'], []))
