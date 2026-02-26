"""
ERP 전체 수불 이력 동기화 커맨드

사용법:
  python manage.py sync_erp_history                    # 1월1일~오늘 전체 동기화
  python manage.py sync_erp_history --from 20260201    # 2월1일부터
  python manage.py sync_erp_history --reset            # ERP 데이터만 삭제 후 재시작 (LOT 보존)
  python manage.py sync_erp_history --full-reset       # 전체 초기화 (LOT 포함 모든 재고/수불 삭제)

동작 순서:
  --full-reset 시:
    0-1) 모든 수불 이력 삭제 (SCM 입고 포함)
    0-2) 모든 재고 삭제 (LOT 재고 포함)
    0-3) sync_stock_from_erp → ERP 현재고 기준 재고 생성
  --reset 시:
    0-1) 기존 ERP 동기화 트랜잭션 전체 삭제
    0-2) lot_no=NULL 재고 삭제 (LOT 재고는 보존)
    0-3) sync_stock_from_erp → ERP 현재고 기준 lot_no=NULL 재고 생성
  공통:
    1) ERP 구매입고 (IN_ERP) 동기화 (이력만)
    2) ERP 생산출고 (ISU_ERP) 동기화 (이력만)
    3) ERP 생산입고 (RCV_ERP) 동기화 (이력만)
    4) ERP 재고이동 (TRF_ERP) 동기화 (이력만)
    5) ERP 재고조정 (ADJ_ERP) 동기화 (이력만)
    6) ERP 고객출고 (OUT_ERP) 동기화 (이력만)
    7) sync_stock_from_erp → ERP 현재고와 최종 보정
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
            '--reset',
            action='store_true',
            dest='reset',
            help='기존 ERP 동기화 데이터 삭제 + ERP 현재고 기준 재시작 (LOT 보존)',
        )
        parser.add_argument(
            '--full-reset',
            action='store_true',
            dest='full_reset',
            help='전체 초기화: 모든 수불/재고 삭제 후 ERP 기준 재시작 (서버 배포용)',
        )

    def handle(self, *args, **options):
        from material.erp_api import (
            sync_erp_incoming,
            sync_erp_issue,
            sync_erp_receipt,
            sync_erp_stock_transfer,
            sync_erp_adjustments,
            sync_erp_outgoing,
            sync_stock_from_erp,
        )
        from material.models import MaterialTransaction, MaterialStock

        date_from = options['date_from']
        date_to = options['date_to'] or datetime.now().strftime('%Y%m%d')

        is_full_reset = options['full_reset']
        is_reset = options['reset'] or is_full_reset

        reset_label = 'FULL RESET' if is_full_reset else ('YES' if is_reset else 'NO')
        self.stdout.write(self.style.WARNING(
            f'\n=== ERP 전체 수불 이력 동기화 ===\n'
            f'기간: {date_from} ~ {date_to}\n'
            f'리셋: {reset_label}\n'
        ))

        total_start = time.time()
        grand_total = {'synced': 0, 'skipped': 0, 'errors': 0}

        # --full-reset: 모든 데이터 삭제 + ERP 현재고 기준 완전 재시작
        if is_full_reset:
            self.stdout.write(self.style.ERROR('[전체 초기화 1/3] 모든 수불 이력 삭제 중...'))
            tx_cnt = MaterialTransaction.objects.count()
            MaterialTransaction.objects.all().delete()
            self.stdout.write(f'  수불 이력: {tx_cnt}건 삭제')
            self.stdout.write('')

            self.stdout.write(self.style.ERROR('[전체 초기화 2/3] 모든 재고 삭제 중 (LOT 포함)...'))
            st_cnt = MaterialStock.objects.count()
            MaterialStock.objects.all().delete()
            self.stdout.write(f'  재고: {st_cnt}건 삭제 (LOT 포함)')
            self.stdout.write('')

            self.stdout.write(self.style.WARNING('[전체 초기화 3/3] ERP 현재고 기준 재고 생성 (sync_stock_from_erp)...'))
            start = time.time()
            try:
                result = sync_stock_from_erp()
                elapsed = time.time() - start
                if result.get('error'):
                    self.stderr.write(self.style.ERROR(f'  -> 실패: {result["error"]}'))
                    return
                self.stdout.write(
                    f'  -> 조정: {result["adjusted"]}건 '
                    f'(증가 {result["increased"]}, 감소 {result["decreased"]}, '
                    f'생성 {result["created"]}건) ({elapsed:.1f}초)'
                )
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'  -> 실패: {e}'))
                return
            self.stdout.write('')

        # --reset: ERP 데이터만 삭제 + ERP 현재고 기준 재고 생성 (LOT 보존)
        elif is_reset:
            self.stdout.write(self.style.WARNING('[리셋 1/3] 기존 ERP 동기화 트랜잭션 삭제 중...'))
            erp_types = ['IN_ERP', 'ISU_ERP', 'RCV_ERP', 'TRF_ERP', 'ADJ_ERP_IN', 'ADJ_ERP_OUT', 'OUT_ERP']
            for t in erp_types:
                cnt = MaterialTransaction.objects.filter(transaction_type=t).count()
                if cnt > 0:
                    MaterialTransaction.objects.filter(transaction_type=t).delete()
                    self.stdout.write(f'  {t}: {cnt}건 삭제')
            self.stdout.write('')

            self.stdout.write(self.style.WARNING('[리셋 2/3] lot_no=NULL 재고 삭제 (LOT 재고 보존)...'))
            null_cnt = MaterialStock.objects.filter(lot_no__isnull=True).count()
            MaterialStock.objects.filter(lot_no__isnull=True).delete()
            lot_cnt = MaterialStock.objects.filter(lot_no__isnull=False).count()
            self.stdout.write(f'  lot_no=NULL: {null_cnt}건 삭제, LOT 재고 보존: {lot_cnt}건')
            self.stdout.write('')

            self.stdout.write(self.style.WARNING('[리셋 3/3] ERP 현재고 기준 재고 동기화 (sync_stock_from_erp)...'))
            start = time.time()
            try:
                result = sync_stock_from_erp()
                elapsed = time.time() - start
                if result.get('error'):
                    self.stderr.write(self.style.ERROR(f'  -> 실패: {result["error"]}'))
                    return
                self.stdout.write(
                    f'  -> 조정: {result["adjusted"]}건 '
                    f'(증가 {result["increased"]}, 감소 {result["decreased"]}, '
                    f'생성 {result["created"]}건) ({elapsed:.1f}초)'
                )
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'  -> 실패: {e}'))
                return
            self.stdout.write('')

        # 동기화 대상 목록 (이력만 기록, 재고 가감 없음)
        sync_tasks = [
            ('구매입고 (IN_ERP)', lambda: sync_erp_incoming(date_from, date_to)),
            ('생산출고 (ISU_ERP)', lambda: sync_erp_issue(date_from, date_to)),
            ('생산입고 (RCV_ERP)', lambda: sync_erp_receipt(date_from, date_to)),
            ('재고이동 (TRF_ERP)', lambda: sync_erp_stock_transfer(date_from, date_to)),
            ('재고조정 (ADJ_ERP)', lambda: sync_erp_adjustments(date_from, date_to)),
            ('고객출고 (OUT_ERP)', lambda: sync_erp_outgoing(date_from, date_to)),
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

        # 최종 재고 보정 (ERP 현재고와 SCM 재고 동기화)
        self.stdout.write(self.style.WARNING('[마무리] ERP 현재고 기준 재고 동기화 (sync_stock_from_erp)...'))
        start = time.time()

        try:
            result = sync_stock_from_erp()
            elapsed = time.time() - start

            if result.get('error'):
                self.stderr.write(self.style.ERROR(f'  -> 실패: {result["error"]}'))
            else:
                self.stdout.write(
                    f'  -> 조정: {result["adjusted"]}건 '
                    f'(증가 {result["increased"]}, 감소 {result["decreased"]}) ({elapsed:.1f}초)'
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
