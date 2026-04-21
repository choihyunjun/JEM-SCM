"""
매일 밤 11시 자동 실행 — ERP 생산입고 동기화
1) 성형 가동률 (당월)
2) 금형 MT 숏트수 (당월)

crontab: 0 23 * * * cd /var/www/scm_project && python manage.py sync_erp_daily
"""
import logging
import calendar
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'ERP 생산입고 데이터 일일 자동 동기화 (성형 가동률 + 금형 MT)'

    def handle(self, *args, **options):
        now = timezone.localtime()
        year = now.year
        month = now.month
        self.stdout.write(f'[{now:%Y-%m-%d %H:%M}] ERP 일일 동기화 시작 (대상: {year}년 {month}월)')

        # 1) 성형 가동률 동기화
        try:
            result1 = self.sync_molding(year, month)
            self.stdout.write(self.style.SUCCESS(f'  성형 가동률: {result1}'))
        except Exception as e:
            logger.exception('성형 가동률 동기화 오류')
            self.stdout.write(self.style.ERROR(f'  성형 가동률 오류: {e}'))

        # 2) 금형 MT 숏트수 동기화
        try:
            result2 = self.sync_mold_mt(year, month)
            self.stdout.write(self.style.SUCCESS(f'  금형 MT: {result2}'))
        except Exception as e:
            logger.exception('금형 MT 동기화 오류')
            self.stdout.write(self.style.ERROR(f'  금형 MT 오류: {e}'))

        self.stdout.write(self.style.SUCCESS('ERP 일일 동기화 완료'))

    def sync_molding(self, year, month):
        """성형 가동률 ERP 동기화"""
        from material.models import MoldingMachine, MoldingDailyRecord, MoldingERPSyncLog, MoldingWorkSetting
        from material.erp_api import fetch_erp_receipt_list, call_erp_api
        from datetime import date as dt_date
        from django.conf import settings as django_settings
        import re

        _, days_in_month = calendar.monthrange(year, month)
        date_from = f"{year}{month:02d}01"
        date_to = f"{year}{month:02d}{days_in_month:02d}"

        ok, data, err = fetch_erp_receipt_list(date_from, date_to)
        if not ok:
            return f'ERP 조회 실패: {err}'
        if not data:
            return '해당 기간 데이터 없음'

        setting = MoldingWorkSetting.get_setting(year, month)

        # 생산실적 API (실가동시간)
        wr_body = {
            'coCd': django_settings.ERP_COMPANY_CODE,
            'wrDtFrom': date_from,
            'wrDtTo': date_to,
        }
        wr_ok, wr_data, wr_err = call_erp_api('/apiproxy/api20A03S00901', wr_body)
        work_time_agg = {}
        bad_qty_agg = {}
        if wr_ok and wr_data:
            for r in wr_data.get('resultData', []) or []:
                en = (r.get('equipNm') or '').strip()
                if not re.match(r'^M\d', en):
                    continue
                wr_dt = r.get('wrDt', '')
                if len(wr_dt) != 8:
                    continue
                shift_nm = (r.get('wshftNm') or '').strip()
                shift = '야간' if shift_nm == '야간' else '주간'
                key = (en, wr_dt, shift)
                if key not in work_time_agg:
                    work_time_agg[key] = 0
                    bad_qty_agg[key] = 0
                work_time_agg[key] += int(float(r.get('workTm', 0) or 0))
                bad_qty_agg[key] += int(float(r.get('badQt', 0) or 0))

        with transaction.atomic():
            molding_data = [r for r in data if re.match(r'^M\d', (r.get('equipNm') or ''))]

            daily_agg = {}
            for r in molding_data:
                machine_code = r['equipNm'].strip()
                rcv_dt = r.get('rcvDt', '')
                if len(rcv_dt) != 8:
                    continue
                shift_nm = (r.get('wshftNm') or '').strip()
                shift = '야간' if shift_nm == '야간' else '주간'
                key = (machine_code, rcv_dt, shift)
                if key not in daily_agg:
                    daily_agg[key] = {'part_qty': {}, 'tonnage': 0}
                item_cd = r.get('itemCd', '')
                qty = int(r.get('rcvQt', 0) or 0)
                daily_agg[key]['part_qty'][item_cd] = daily_agg[key]['part_qty'].get(item_cd, 0) + qty

            record_count = 0
            machine_codes = set()
            for (mc, dt_str, shift), agg in daily_agg.items():
                machine, _ = MoldingMachine.objects.get_or_create(
                    code=mc, defaults={'tonnage': 0}
                )
                machine_codes.add(mc)
                rec_date = dt_date(int(dt_str[:4]), int(dt_str[4:6]), int(dt_str[6:8]))
                base_min = setting.night_shift_minutes if shift == '야간' else setting.day_shift_minutes

                record, created = MoldingDailyRecord.objects.get_or_create(
                    machine=machine, date=rec_date, shift=shift,
                    defaults={'status': '가동', 'base_minutes': base_min, 'erp_synced': True}
                )
                record.status = '가동'
                part_qty = agg['part_qty']
                record.product_part_no = ' | '.join(
                    f"{p}: {q:,}" for p, q in sorted(part_qty.items())
                )[:500]
                record.product_qty = sum(part_qty.values())
                record.defect_qty = bad_qty_agg.get((mc, dt_str, shift), 0)
                record.erp_synced = True
                if not record.input_completed:
                    record.base_minutes = base_min
                wt_key = (mc, dt_str, shift)
                if wt_key in work_time_agg and not record.input_completed:
                    record.work_minutes = min(work_time_agg[wt_key], base_min)
                    record.operating_minutes = record.work_minutes
                    record.loss_minutes = max(base_min - record.work_minutes, 0)
                    if base_min > 0:
                        rate = round(record.operating_minutes / base_min * 100, 1)
                        record.utilization_rate = rate
                        record.time_rate = rate
                record.save()
                record_count += 1

            MoldingERPSyncLog.objects.create(
                year=year, month=month,
                record_count=record_count,
                machine_count=len(machine_codes),
                message=f'[자동] ERP {len(molding_data)}건 → {record_count}개 가동일 (호기 {len(machine_codes)}대)',
            )

        return f'{record_count}개 가동일 동기화 (호기 {len(machine_codes)}대)'

    def sync_mold_mt(self, year, month):
        """금형 MT 숏트수 ERP 동기화 (생산실적 API - 양품+불량 포함)"""
        from material.models import MoldMaster, MoldShotRecord
        from material.erp_api import call_erp_api
        from django.conf import settings as django_settings

        _, days_in_month = calendar.monthrange(year, month)
        date_from = f"{year}{month:02d}01"
        date_to = f"{year}{month:02d}{days_in_month:02d}"

        # 생산실적 API 사용 (badQt 포함)
        body = {
            'coCd': django_settings.ERP_COMPANY_CODE,
            'wrDtFrom': date_from,
            'wrDtTo': date_to,
        }
        ok, raw, err = call_erp_api('/apiproxy/api20A03S00901', body)
        if not ok:
            return f'ERP 조회 실패: {err}'
        data = (raw.get('resultData', []) or []) if raw else []
        if not data:
            return '해당 기간 데이터 없음'

        mold_map = {m.part_no: m for m in MoldMaster.objects.filter(is_active=True)}

        part_qty_agg = {}
        for r in data:
            item_cd = (r.get('itemCd') or '').strip()
            good_qt = int(float(r.get('goodQt', 0) or 0))
            bad_qt = int(float(r.get('badQt', 0) or 0))
            total_qt = good_qt + bad_qt
            if item_cd and total_qt > 0:
                part_qty_agg[item_cd] = part_qty_agg.get(item_cd, 0) + total_qt

        synced = 0
        with transaction.atomic():
            for part_no, total_qty in part_qty_agg.items():
                mold = mold_map.get(part_no)
                if not mold:
                    continue
                cv = mold.cv_count if mold.cv_count > 0 else 1
                shots = total_qty // cv
                if shots > 0:
                    MoldShotRecord.objects.update_or_create(
                        mold=mold, year=year, month=month,
                        defaults={'shots': shots, 'source': 'ERP'}
                    )
                    synced += 1

        return f'{synced}건 동기화'
