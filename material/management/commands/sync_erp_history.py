"""
ERP 전체 수불 이력 동기화 + 재고조정 통합 커맨드

사용법:
  python manage.py sync_erp_history                    # 1월1일~오늘 전체 동기화
  python manage.py sync_erp_history --from 20260201    # 2월1일부터
  python manage.py sync_erp_history --no-adjust        # 재고조정 생략 (이력만)
  python manage.py sync_erp_history --reset            # 기존 데이터 삭제 후 처음부터

동작 순서:
  --reset 시:
    0-1) 기존 ERP 동기화 트랜잭션 전체 삭제
    0-2) 기초재고 셋팅 (ERP 실시간 현재고)
  공통:
    1) ERP 구매입고 (IN_ERP) 동기화
    2) ERP 생산출고 (ISU_ERP) 동기화
    3) ERP 생산입고 (RCV_ERP) 동기화
    4) ERP 재고이동 (TRF_ERP) 동기화
    5) ERP 재고조정 (ADJ_ERP) 동기화
    6) SCM 재고를 ERP 현재고에 맞춤 (adjust_stock_to_erp)
"""

import time
from datetime import datetime
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'ERP 전체 수불 이력을 동기화하고 재고를 ERP에 맞춥니다'

    def add_arguments(self, parser):
        parser.add_argument(
            '--from',
            type=str,
            dest='date_from',
            default='20260101',
            help='시작일 (YYYYMMDD, 기본: 20260101)',
        )
        parser.add_argument(
            '--to',
            type=str,
            dest='date_to',
            default=None,
            help='종료일 (YYYYMMDD, 기본: 오늘)',
        )
        parser.add_argument(
            '--no-adjust',
            action='store_true',
            dest='no_adjust',
            help='마지막 재고조정(adjust_stock_to_erp) 생략',
        )
        parser.add_argument(
            '--reset',
            action='store_true',
            dest='reset',
            help='기존 ERP 동기화 데이터 삭제 + 기초재고 셋팅 후 동기화',
        )

    def handle(self, *args, **options):
        from material.erp_api import (
            sync_erp_incoming,
            sync_erp_issue,
            sync_erp_receipt,
            sync_erp_stock_transfer,
            sync_erp_adjustments,
            sync_erp_outgoing,
            adjust_stock_to_erp,
            init_stock_from_erp,
        )
        from material.models import MaterialTransaction

        date_from = options['date_from']
        date_to = options['date_to'] or datetime.now().strftime('%Y%m%d')

        self.stdout.write(self.style.WARNING(
            f'\n=== ERP 전체 수불 이력 동기화 ===\n'
            f'기간: {date_from} ~ {date_to}\n'
            f'리셋: {"YES" if options["reset"] else "NO"}\n'
        ))

        total_start = time.time()
        grand_total = {'synced': 0, 'skipped': 0, 'errors': 0}

        # --reset: 기존 데이터 삭제 + 기초재고 셋팅
        if options['reset']:
            self.stdout.write(self.style.WARNING('[리셋 1/2] 기존 ERP 동기화 트랜잭션 삭제 중...'))
            erp_types = ['IN_ERP', 'ISU_ERP', 'RCV_ERP', 'TRF_ERP', 'ADJ_ERP_IN', 'ADJ_ERP_OUT', 'OUT_ERP']
            for t in erp_types:
                cnt = MaterialTransaction.objects.filter(transaction_type=t).count()
                if cnt > 0:
                    MaterialTransaction.objects.filter(transaction_type=t).delete()
                    self.stdout.write(f'  {t}: {cnt}건 삭제')
            self.stdout.write('')

            self.stdout.write(self.style.WARNING('[리셋 2/2] ERP 기초재고 셋팅 (실시간 현재고)...'))
            start = time.time()
            try:
                result = init_stock_from_erp(cutoff_date=datetime.now().strftime('%Y-%m-%d'))
                elapsed = time.time() - start
                if result.get('error'):
                    self.stderr.write(self.style.ERROR(f'  -> 실패: {result["error"]}'))
                    return
                self.stdout.write(
                    f'  -> 생성: {result["created"]}건, '
                    f'건너뜀(재고0): {result["skipped_zero"]}건, '
                    f'건너뜀(Part없음): {result["skipped_no_part"]}건, '
                    f'건너뜀(창고없음): {result["skipped_no_wh"]}건 '
                    f'({elapsed:.1f}초)'
                )
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'  -> 실패: {e}'))
                return
            self.stdout.write('')

        # --reset 시 이력만 기록 (기초재고=현재고이므로 재고 가감 불필요)
        skip = options['reset']

        # 동기화 대상 목록
        sync_tasks = [
            ('구매입고 (IN_ERP)', lambda: sync_erp_incoming(date_from, date_to, skip_stock_update=skip)),
            ('생산출고 (ISU_ERP)', lambda: sync_erp_issue(date_from, date_to, skip_stock_update=skip)),
            ('생산입고 (RCV_ERP)', lambda: sync_erp_receipt(date_from, date_to, skip_stock_update=skip)),
            ('재고이동 (TRF_ERP)', lambda: sync_erp_stock_transfer(date_from, date_to, skip_stock_update=skip)),
            ('재고조정 (ADJ_ERP)', lambda: sync_erp_adjustments(date_from, date_to, skip_stock_update=skip)),
            ('고객출고 (OUT_ERP)', lambda: sync_erp_outgoing(date_from, date_to, skip_stock_update=skip)),
        ]

        total_tasks = len(sync_tasks)
        for idx, (name, func) in enumerate(sync_tasks, 1):
            self.stdout.write(f'[{idx}/{total_tasks}] {name} 동기화 중...')
            start = time.time()

            try:
                synced, skipped, errors, error_list = func()
                elapsed = time.time() - start

                grand_total['synced'] += synced
                grand_total['skipped'] += skipped
                grand_total['errors'] += errors

                status = self.style.SUCCESS(f'{synced}건') if synced > 0 else f'{synced}건'
                self.stdout.write(
                    f'  -> 동기화: {status}, 건너뜀: {skipped}건, '
                    f'오류: {errors}건 ({elapsed:.1f}초)'
                )

                if error_list:
                    for err in error_list[:3]:
                        self.stderr.write(self.style.ERROR(f'     {err}'))
                    if len(error_list) > 3:
                        self.stderr.write(f'     ... 외 {len(error_list) - 3}건')

            except Exception as e:
                self.stderr.write(self.style.ERROR(f'  -> 실패: {e}'))
                grand_total['errors'] += 1

            self.stdout.write('')

        # 재고조정 (ERP 현재고와 SCM 재고 차이 보정 - 리셋 시에도 실행)
        if not options['no_adjust']:
            self.stdout.write(self.style.WARNING('[마무리] ERP 기준 재고조정 실행 중...'))
            start = time.time()

            try:
                result = adjust_stock_to_erp()
                elapsed = time.time() - start

                if result.get('error'):
                    self.stderr.write(self.style.ERROR(f'  -> 실패: {result["error"]}'))
                else:
                    self.stdout.write(
                        f'  -> 조정: {result["adjusted"]}건 '
                        f'(증가 {result["increased"]}, 감소 {result["decreased"]}), '
                        f'동기화 시작일: {result.get("sync_start", "")} ({elapsed:.1f}초)'
                    )
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'  -> 실패: {e}'))

        total_elapsed = time.time() - total_start

        self.stdout.write(self.style.SUCCESS(
            f'\n=== 완료 ({total_elapsed:.1f}초) ===\n'
            f'총 동기화: {grand_total["synced"]}건, '
            f'건너뜀: {grand_total["skipped"]}건, '
            f'오류: {grand_total["errors"]}건\n'
        ))
