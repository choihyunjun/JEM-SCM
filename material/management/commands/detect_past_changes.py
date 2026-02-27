"""
ERP 과거 수불 변경 감지 커맨드

사용법:
  python manage.py detect_past_changes              # 최근 30일 (기본)
  python manage.py detect_past_changes --days 7     # 최근 7일

동작:
  1) 해당 기간의 ERP 동기화 트랜잭션(erp_incoming_no)을 SCM에서 조회
  2) 같은 기간의 ERP 수불을 API에서 가져옴
  3) trx_key(erp_incoming_no) 기준으로 수량 비교
  4) 수량이 다르면 SCM 이력 업데이트 + 로그
  5) ERP에서 삭제된 건은 SCM에서도 삭제
  6) 마지막에 sync_stock_from_erp()로 재고 보정
"""

import time
import fcntl
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand

ERP_SYNC_LOCK_FILE = '/tmp/erp_sync.lock'


class Command(BaseCommand):
    help = '최근 N일간 ERP 수불 변경을 감지하고 SCM 이력을 보정합니다'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help='검사할 기간 (일, 기본: 30)',
        )

    def handle(self, *args, **options):
        # ── 파일 기반 lock (동시 실행 방지) ──
        fp = open(ERP_SYNC_LOCK_FILE, 'w')
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            self.stderr.write(self.style.WARNING(
                '다른 ERP 동기화가 진행 중입니다. 건너뜁니다.'
            ))
            fp.close()
            return

        try:
            self._run(options)
        finally:
            fp.close()

    def _run(self, options):
        from material.erp_api import (
            sync_erp_adjustments,
            sync_stock_from_erp,
            fetch_erp_incoming_headers, fetch_erp_incoming_detail,
            fetch_erp_issue_headers, fetch_erp_issue_details,
            fetch_erp_receipt_list,
            fetch_erp_transfer_headers, fetch_erp_transfer_details,
            fetch_erp_outgoing_headers, fetch_erp_outgoing_details,
        )
        from material.models import MaterialTransaction

        days = options['days']
        date_from = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
        date_to = datetime.now().strftime('%Y%m%d')
        date_from_parsed = (datetime.now() - timedelta(days=days)).date()

        total_start = time.time()
        total_updated = 0
        total_deleted = 0
        total_new = 0

        self.stdout.write(self.style.WARNING(
            f'\n=== ERP 과거 수불 변경 감지 ===\n'
            f'기간: {date_from} ~ {date_to} (최근 {days}일)\n'
        ))

        # ── header+detail 패턴 유형 비교 ──
        detect_tasks = [
            ('구매입고', 'IN_ERP', fetch_erp_incoming_headers, fetch_erp_incoming_detail,
             'rcvNb', 'rcvSq', 'rcvQt', False),
            ('생산출고', 'ISU_ERP', fetch_erp_issue_headers, fetch_erp_issue_details,
             'isuNb', 'isuSq', 'isuQt', True),
            ('재고이동', 'TRF_ERP', fetch_erp_transfer_headers, fetch_erp_transfer_details,
             'moveNb', 'moveSq', 'moveQt', False),
            ('고객출고', 'OUT_ERP', fetch_erp_outgoing_headers, fetch_erp_outgoing_details,
             'isuNb', 'isuSq', 'isuQt', True),
        ]

        for name, trx_type, fetch_headers, fetch_detail, nb_key, sq_key, qt_key, is_negative in detect_tasks:
            self.stdout.write(f'[{name} ({trx_type})] 비교 중...')
            start = time.time()

            try:
                updated, deleted, new = self._compare_header_detail(
                    trx_type, fetch_headers, fetch_detail,
                    nb_key, sq_key, qt_key, is_negative,
                    date_from, date_to, date_from_parsed,
                )
                elapsed = time.time() - start

                total_updated += updated
                total_deleted += deleted
                total_new += new

                self._print_result(updated, deleted, new, elapsed)

            except Exception as e:
                self.stderr.write(self.style.ERROR(f'  -> 오류: {e}'))

            self.stdout.write('')

        # ── 생산입고 (RCV_ERP): flat list 패턴 (header+detail이 아님) ──
        self.stdout.write('[생산입고 (RCV_ERP)] 비교 중...')
        start = time.time()
        try:
            updated, deleted, new = self._compare_receipt(
                fetch_erp_receipt_list, date_from, date_to, date_from_parsed,
            )
            elapsed = time.time() - start

            total_updated += updated
            total_deleted += deleted
            total_new += new

            self._print_result(updated, deleted, new, elapsed)

        except Exception as e:
            self.stderr.write(self.style.ERROR(f'  -> 오류: {e}'))
        self.stdout.write('')

        # ── 재고조정은 구조가 다르므로 별도 처리 (신규만, 비교 생략) ──
        self.stdout.write('[재고조정 (ADJ_ERP)] 신규 건만 동기화...')
        start = time.time()
        try:
            synced, skipped, errors, _ = sync_erp_adjustments(date_from, date_to)
            elapsed = time.time() - start
            if synced > 0:
                total_new += synced
                self.stdout.write(f'  -> {self.style.SUCCESS(f"신규 {synced}건")}, 건너뜀 {skipped}건 ({elapsed:.1f}초)')
            else:
                self.stdout.write(f'  -> 변경 없음 ({elapsed:.1f}초)')
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'  -> 오류: {e}'))
        self.stdout.write('')

        # ── 재고 보정 ──
        if total_updated > 0 or total_deleted > 0 or total_new > 0:
            self.stdout.write(self.style.WARNING('[마무리] ERP 현재고 기준 재고 보정...'))
            try:
                result = sync_stock_from_erp()
                adj = result.get('adjusted', 0)
                if adj > 0:
                    self.stdout.write(f'  -> 재고 조정 {adj}건')
                else:
                    self.stdout.write(f'  -> 재고 차이 없음')
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'  -> 재고 보정 오류: {e}'))
        else:
            self.stdout.write('변경 사항 없음 - 재고 보정 생략')

        total_elapsed = time.time() - total_start
        self.stdout.write(self.style.SUCCESS(
            f'\n=== 완료 ({total_elapsed:.1f}초) ===\n'
            f'수정: {total_updated}건, 삭제: {total_deleted}건, 신규: {total_new}건\n'
        ))

    def _print_result(self, updated, deleted, new, elapsed):
        parts = []
        if updated:
            parts.append(self.style.WARNING(f'수정 {updated}건'))
        if deleted:
            parts.append(self.style.ERROR(f'삭제 {deleted}건'))
        if new:
            parts.append(self.style.SUCCESS(f'신규 {new}건'))
        if not parts:
            parts.append('변경 없음')
        self.stdout.write(f'  -> {", ".join(parts)} ({elapsed:.1f}초)')

    def _compare_header_detail(self, trx_type, fetch_headers, fetch_detail,
                               nb_key, sq_key, qt_key, is_negative,
                               date_from, date_to, date_from_parsed):
        """
        header+detail 패턴 트랜잭션 비교 (구매입고, 생산출고, 재고이동, 고객출고).
        Returns: (updated, deleted, new)
        """
        from material.models import MaterialTransaction

        # 1) ERP에서 현재 데이터 가져오기 → {trx_key: qty} 맵 생성
        erp_map = {}

        ok, headers, err = fetch_headers(date_from, date_to)
        if not ok or not headers:
            return 0, 0, 0

        for header in headers:
            nb = header.get(nb_key, '')
            if not nb:
                continue

            ok2, details, err2 = fetch_detail(nb)
            if not ok2 or not details:
                continue

            for detail in details:
                sq = detail.get(sq_key, 1)
                trx_key = f'{nb}-{sq}'
                qty = int(detail.get(qt_key, 0) or 0)
                if qty <= 0:
                    continue
                erp_map[trx_key] = -qty if is_negative else qty

        # 2) SCM에서 해당 기간 + 타입의 트랜잭션 조회
        scm_records = MaterialTransaction.objects.filter(
            transaction_type=trx_type,
            date__date__gte=date_from_parsed,
            erp_incoming_no__isnull=False,
        ).values_list('pk', 'erp_incoming_no', 'quantity')

        scm_map = {}
        for pk, erp_no, qty in scm_records:
            scm_map[erp_no] = (pk, qty)

        # 3) 비교
        return self._diff_maps(erp_map, scm_map)

    def _compare_receipt(self, fetch_receipt_list, date_from, date_to, date_from_parsed):
        """
        생산입고 (RCV_ERP) 비교: flat list 패턴 (rcvNb가 직접 trx_key).
        Returns: (updated, deleted, new)
        """
        from material.models import MaterialTransaction

        # 1) ERP에서 flat list 가져오기
        erp_map = {}

        ok, items, err = fetch_receipt_list(date_from, date_to)
        if not ok or not items:
            return 0, 0, 0

        for item in items:
            rcv_nb = item.get('rcvNb', '')
            if not rcv_nb:
                continue
            qty = int(item.get('rcvQt', 0) or 0)
            if qty <= 0:
                continue
            erp_map[rcv_nb] = qty  # 생산입고는 양수

        # 2) SCM에서 해당 기간 + RCV_ERP 트랜잭션 조회
        scm_records = MaterialTransaction.objects.filter(
            transaction_type='RCV_ERP',
            date__date__gte=date_from_parsed,
            erp_incoming_no__isnull=False,
        ).values_list('pk', 'erp_incoming_no', 'quantity')

        scm_map = {}
        for pk, erp_no, qty in scm_records:
            scm_map[erp_no] = (pk, qty)

        # 3) 비교
        return self._diff_maps(erp_map, scm_map)

    def _diff_maps(self, erp_map, scm_map):
        """
        ERP 맵과 SCM 맵을 비교하여 수정/삭제/신규 건수 반환.
        erp_map: {trx_key: qty}
        scm_map: {trx_key: (pk, qty)}
        Returns: (updated, deleted, new)
        """
        from material.models import MaterialTransaction

        updated = 0
        deleted = 0
        new = 0

        # 수량 변경 + ERP 삭제 감지
        for trx_key, (pk, scm_qty) in scm_map.items():
            if trx_key in erp_map:
                erp_qty = erp_map[trx_key]
                if scm_qty != erp_qty:
                    MaterialTransaction.objects.filter(pk=pk).update(quantity=erp_qty)
                    self.stdout.write(self.style.WARNING(
                        f'    수정: {trx_key} 수량 {scm_qty} → {erp_qty}'
                    ))
                    updated += 1
            else:
                MaterialTransaction.objects.filter(pk=pk).delete()
                self.stdout.write(self.style.ERROR(
                    f'    삭제: {trx_key} (ERP에서 제거됨)'
                ))
                deleted += 1

        # 신규 건 (ERP에 있지만 SCM에 없음 → 카운트만, 실제 생성은 sync에서)
        for trx_key in erp_map:
            if trx_key not in scm_map:
                new += 1

        return updated, deleted, new
