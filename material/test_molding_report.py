"""성형 가동률 종합보고서(엑셀) 회귀 테스트. 화면과 같은 집계로 여섯 시트가 만들어지는지 확인한다."""
import io
from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from orders.models import Part, UserProfile
from .models import MoldingDailyRecord, MoldingLossDetail, MoldingMachine, MoldingProductDetail


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'])
class MoldingReportTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('mold-admin', is_superuser=True, is_staff=True)
        Part.objects.create(part_no='MP-1', part_name='Cap', part_group='GROUP-A')
        self.m1 = MoldingMachine.objects.create(code='M301', tonnage=50)
        self.m2 = MoldingMachine.objects.create(code='M401', tonnage=110)
        MoldingMachine.objects.create(code='M999', tonnage=110)            # 실적 없는 호기
        for machine, day, operating in ((self.m1, 3, 900), (self.m1, 4, 1000), (self.m2, 3, 800)):
            record = MoldingDailyRecord.objects.create(
                machine=machine, date=date(2026, 11, day), status='가동', base_minutes=1000,
                operating_minutes=operating, loss_minutes=1000 - operating, shift='주간',
                product_part_no='MP-1:5000', product_qty=5010, defect_qty=10)
            MoldingProductDetail.objects.create(record=record, item_cd='MP-1', item_nm='Cap', good_qty=5000, bad_qty=10)
            if operating < 1000:
                MoldingLossDetail.objects.create(record=record, category='계획정지', minutes=40)
                MoldingLossDetail.objects.create(record=record, category='설비고장', minutes=1000 - operating - 40)

    def workbook(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('material:molding_analytics_report'), {'year': 2026, 'month': 11})
        self.assertEqual(response.status_code, 200)
        self.assertIn('spreadsheetml', response['Content-Type'])
        return load_workbook(io.BytesIO(response.content))

    def rows(self, ws):
        return [[v for v in row] for row in ws.iter_rows(values_only=True) if any(v is not None for v in row)]

    def test_report_collects_all_three_tabs_and_the_machine_popups(self):
        wb = self.workbook()
        self.assertEqual(wb.sheetnames, ['요약', '유실 분석', '호기별 종합', '호기별 생산 실적', '호기별 유실 내역', '품목군 생산'])

        summary = {row[0]: row[1] for row in self.rows(wb['요약']) if len(row) > 1}
        self.assertEqual(summary['설비가동률'], 0.9)                       # (900+1000+800) / 3000
        self.assertEqual(len(wb['요약']._charts), 3)

        machines = {row[1]: row for row in self.rows(wb['호기별 종합']) if row[1] in ('M301', 'M401', 'M999')}
        self.assertEqual(set(machines), {'M301', 'M401', 'M999'})          # 실적 없는 호기도 한 줄
        m301 = machines['M301']
        self.assertEqual((m301[3], m301[5], m301[9]), (2, 1900, 0.95))     # 가동일수, 가동시간, 설비가동률
        self.assertEqual((m301[12], m301[13], m301[14]), (10020, 10000, 20))   # 총생산, 양품, 불량
        self.assertIn('설비고장', m301[16])                                # 가장 큰 유실 사유
        self.assertTrue(m301[17].startswith('MP-1'))

        produced = [row for row in self.rows(wb['호기별 생산 실적']) if row[1] == 'M401' and row[2] == 'MP-1']
        self.assertEqual((produced[0][4], produced[0][5], produced[0][6]), (5010, 5000, 10))

        losses = {(row[1], row[2]): row[4] for row in self.rows(wb['호기별 유실 내역']) if row[1] in ('M301', 'M401')}
        self.assertEqual(losses[('M301', '설비고장')], 60)
        self.assertEqual(losses[('M401', '계획정지')], 40)
        self.assertEqual(losses[('M401', '합계')], 200)

        groups = self.rows(wb['품목군 생산'])
        self.assertIn(['GROUP-A', '합계', 15000, 1], groups)

    def test_screen_still_renders_with_the_shared_calculation(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse('material:molding_analytics'), {'year': 2026, 'month': 11})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['kpi_utilization'], 90.0)
        self.assertContains(page, '종합보고서')

    def test_empty_month_and_bad_period(self):
        self.client.force_login(self.admin)
        empty = self.client.get(reverse('material:molding_analytics_report'), {'year': 2025, 'month': 1})
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(len(load_workbook(io.BytesIO(empty.content)).sheetnames), 6)
        bad = self.client.get(reverse('material:molding_analytics_report'), {'year': 2026, 'month': 13})
        self.assertEqual(bad.status_code, 302)

    def test_vendor_account_cannot_download(self):
        vendor = User.objects.create_user('mold-vendor')
        UserProfile.objects.filter(user=vendor).update(role='VENDOR')
        self.client.force_login(vendor)
        response = self.client.get(reverse('material:molding_analytics_report'), {'year': 2026, 'month': 11})
        self.assertEqual(response.status_code, 302)
