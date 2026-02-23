"""
ERP 전체 수불 이력 동기화 + 재고조정 통합 커맨드

사용법:
  python manage.py sync_erp_history                    # 1월1일~오늘 전체 동기화
  python manage.py sync_erp_history --from 20260201    # 2월1일부터
  python manage.py sync_erp_history --no-adjust        # 재고조정 생략 (이력만)

동작 순서:
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

    def handle(self, *args, **options):
        from material.erp_api import (
            sync_erp_incoming,
            sync_erp_issue,
            sync_erp_receipt,
            sync_erp_stock_transfer,
            sync_erp_adjustments,
            adjust_stock_to_erp,
        )

        date_from = options['date_from']
        date_to = options['date_to'] or datetime.now().strftime('%Y%m%d')

        self.stdout.write(self.style.WARNING(
            f'\n=== ERP 전체 수불 이력 동기화 ===\n'
            f'기간: {date_from} ~ {date_to}\n'
        ))

        total_start = time.time()
        grand_total = {'synced': 0, 'skipped': 0, 'errors': 0}

        # 동기화 대상 목록
        sync_tasks = [
            ('구매입고 (IN_ERP)', lambda: sync_erp_incoming(date_from, date_to)),
            ('생산출고 (ISU_ERP)', lambda: sync_erp_issue(date_from, date_to)),
            ('생산입고 (RCV_ERP)', lambda: sync_erp_receipt(date_from, date_to)),
            ('재고이동 (TRF_ERP)', lambda: sync_erp_stock_transfer(date_from, date_to)),
            ('재고조정 (ADJ_ERP)', lambda: sync_erp_adjustments(date_from, date_to)),
        ]

        for name, func in sync_tasks:
            self.stdout.write(f'[{len(grand_total)+1}/5] {name} 동기화 중...')
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

        # 재고조정
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
