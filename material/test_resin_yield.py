"""성형 원단위 중량 관리: 파지 비고 해석, 재료 행별 중량 계산, 화면·엑셀."""
from datetime import datetime
from io import BytesIO
from unittest.mock import patch

import openpyxl
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.utils import timezone

from orders.models import Part
from .models import MaterialTransaction, MoldingMaster, ResinType, Warehouse
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
        site = Warehouse.objects.create(code='4300', name='제조현장')
        bad = Warehouse.objects.create(code='8000', name='불량창고')
        common = dict(part=part, warehouse_from=site, warehouse_to=bad,
                      date=timezone.make_aware(datetime(2026, 10, 10)))
        MaterialTransaction.objects.create(transaction_no='MV1', transaction_type='TRF_ERP', quantity=20, **common)
        MaterialTransaction.objects.create(transaction_no='ADJ', transaction_type='ADJ_ERP_IN', quantity=999, **common)
        MaterialTransaction.objects.create(transaction_no='MV2', transaction_type='TRF_ERP', quantity=5,
                                           **{**common, 'part': Part.objects.create(part_no='ASSY-1')})

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
        self.assertAlmostEqual(main['good_kg'], 15.0)
        self.assertAlmostEqual(main['bad_kg'], 0.1)
        self.assertAlmostEqual(main['inspect_kg'], 0.2)  # 재고 맞춤 기록은 제외
        self.assertAlmostEqual(main['runner_kg'], 1510 * 4 / 2 / 1000)
        self.assertAlmostEqual(main['scrap_kg'], 1.8)
        self.assertAlmostEqual(mb['scrap_kg'], 0.2)
        self.assertAlmostEqual(main['unit_standard'], 12)
        self.assertAlmostEqual(main['total_kg'], 15 + 0.1 + 0.2 + 3.02 + 1.8)

    def test_summaries_count_production_once_and_list_unregistered_parts(self):
        rep = self.report()
        self.assertEqual(rep['totals']['produced'], 1510)
        self.assertEqual([u['part_no'] for u in rep['unregistered']], ['X-9'])
        self.assertEqual(rep['unregistered_scrap_kg'], 0.5)
        self.assertEqual({g['key'] for g in rep['by_resin']}, {'PC', 'M/B'})

    def test_page_and_excel(self):
        request = RequestFactory().get('/', {'month': '2026-10'})
        request.user = User.objects.create(username='ry', is_superuser=True)
        with patch('material.resin_yield.call_erp_api', return_value=erp_rows(('M311', 'P-1', 10, 0, '00100'))):
            self.assertEqual(resin_yield(request).status_code, 200)
            response = resin_yield_excel(request)
        wb = openpyxl.load_workbook(BytesIO(response.content))
        self.assertEqual(wb.sheetnames, ['품번별 원단위', '재질별 집계', '원재료별 사용량', '확인 필요'])
        self.assertEqual(wb['품번별 원단위']['B3'].value, 'P-1')

    def test_erp_failure_is_reported(self):
        with patch('material.resin_yield.call_erp_api', return_value=(False, None, 'timeout')):
            self.assertEqual(build_report(2026, 9), {'error': 'timeout'})
