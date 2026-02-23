"""
ERP 동기화 중복 트랜잭션 정리 + 재고 재계산

사용법:
  python manage.py cleanup_duplicates                # 중복 정리 + 재고 재계산
  python manage.py cleanup_duplicates --dry-run      # 변경 없이 중복 현황만 확인
  python manage.py cleanup_duplicates --recalc-only  # 중복 정리 생략, 재고만 재계산
"""

from django.core.management.base import BaseCommand
from django.db.models import Count


class Command(BaseCommand):
    help = 'ERP 동기화 중복 트랜잭션을 정리하고 재고를 수불 기준으로 재계산합니다'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            dest='dry_run',
            help='변경 없이 중복 현황만 확인',
        )
        parser.add_argument(
            '--recalc-only',
            action='store_true',
            dest='recalc_only',
            help='중복 정리 생략하고 재고만 재계산',
        )

    def handle(self, *args, **options):
        from material.models import MaterialTransaction, MaterialStock
        from django.db.models import Sum, Q

        dry_run = options['dry_run']
        recalc_only = options['recalc_only']

        if dry_run:
            self.stdout.write(self.style.WARNING('=== DRY RUN 모드 (변경 없음) ===\n'))

        # ─────────────────────────────────────────────
        # 1단계: ERP 동기화 중복 트랜잭션 찾기 + 삭제
        # ─────────────────────────────────────────────
        if not recalc_only:
            self.stdout.write(self.style.WARNING('[1/3] ERP 동기화 중복 트랜잭션 검색 중...\n'))

            erp_types = ['IN_ERP', 'ISU_ERP', 'RCV_ERP', 'TRF_ERP',
                         'ADJ_ERP_IN', 'ADJ_ERP_OUT', 'OUT_ERP']

            total_duplicates = 0

            for tx_type in erp_types:
                # erp_incoming_no 기준으로 중복 찾기
                dupes = (
                    MaterialTransaction.objects
                    .filter(transaction_type=tx_type, erp_incoming_no__isnull=False)
                    .values('erp_incoming_no')
                    .annotate(cnt=Count('id'))
                    .filter(cnt__gt=1)
                )

                type_dup_count = 0
                type_deleted = 0

                for dupe in dupes:
                    erp_no = dupe['erp_incoming_no']
                    cnt = dupe['cnt']

                    # 같은 erp_incoming_no의 레코드들 → 첫 번째만 남기고 삭제
                    records = (
                        MaterialTransaction.objects
                        .filter(transaction_type=tx_type, erp_incoming_no=erp_no)
                        .order_by('id')
                    )
                    ids_to_keep = [records.first().id]
                    ids_to_delete = list(records.exclude(id__in=ids_to_keep).values_list('id', flat=True))

                    type_dup_count += len(ids_to_delete)

                    if not dry_run:
                        MaterialTransaction.objects.filter(id__in=ids_to_delete).delete()
                        type_deleted += len(ids_to_delete)

                if type_dup_count > 0:
                    action = f'삭제: {type_deleted}건' if not dry_run else '(dry-run)'
                    self.stdout.write(
                        f'  {tx_type}: 중복 {type_dup_count}건 발견 → {action}'
                    )
                    total_duplicates += type_dup_count

            if total_duplicates == 0:
                self.stdout.write(self.style.SUCCESS('  중복 트랜잭션 없음'))
            else:
                self.stdout.write(self.style.WARNING(
                    f'\n  총 중복: {total_duplicates}건'
                    + (' 삭제 완료' if not dry_run else ' (dry-run, 실제 삭제 안 됨)')
                ))
            self.stdout.write('')

        # ─────────────────────────────────────────────
        # 2단계: 수불 기준 재고 재계산
        # ─────────────────────────────────────────────
        if dry_run:
            self.stdout.write(self.style.WARNING('[2/3] 재고 재계산 (dry-run이므로 생략)\n'))
            self.stdout.write('[3/3] 완료\n')
            return

        self.stdout.write(self.style.WARNING('[2/3] 수불 기준 재고 전체 재계산 중...\n'))

        # 모든 수불 내역에서 창고+품목별 합계 계산
        # 입고 계열: warehouse_to 기준 (+quantity)
        # 출고 계열: warehouse_from 기준 (-quantity, 이미 음수)
        # 이동: warehouse_from(-) + warehouse_to(+)

        # 방법: 각 (warehouse, part) 조합의 순재고를 계산
        stock_calc = {}

        # warehouse_to 방향 (입고, 이동 도착)
        to_agg = (
            MaterialTransaction.objects
            .filter(warehouse_to__isnull=False)
            .values('warehouse_to_id', 'part_id')
            .annotate(total=Sum('quantity'))
        )
        for row in to_agg:
            key = (row['warehouse_to_id'], row['part_id'])
            stock_calc[key] = stock_calc.get(key, 0) + (row['total'] or 0)

        # warehouse_from 방향 (출고, 이동 출발) - quantity는 이미 음수
        from_agg = (
            MaterialTransaction.objects
            .filter(warehouse_from__isnull=False)
            .values('warehouse_from_id', 'part_id')
            .annotate(total=Sum('quantity'))
        )
        for row in from_agg:
            key = (row['warehouse_from_id'], row['part_id'])
            stock_calc[key] = stock_calc.get(key, 0) + (row['total'] or 0)

        # 기존 MaterialStock 전체 삭제 후 재생성
        old_count = MaterialStock.objects.count()
        MaterialStock.objects.all().delete()
        self.stdout.write(f'  기존 재고 레코드 {old_count}건 삭제')

        created = 0
        skipped_zero = 0
        negative_count = 0

        for (wh_id, part_id), qty in stock_calc.items():
            if qty == 0:
                skipped_zero += 1
                continue
            if qty < 0:
                negative_count += 1
                self.stderr.write(self.style.WARNING(
                    f'  경고: 창고={wh_id}, 품목={part_id} 계산 재고={qty} (음수 재고)'
                ))

            MaterialStock.objects.create(
                warehouse_id=wh_id,
                part_id=part_id,
                lot_no=None,
                quantity=int(qty),
            )
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f'  재고 재생성: {created}건 (건너뜀: 재고0={skipped_zero}건, 음수재고={negative_count}건 포함)'
        ))

        # ─────────────────────────────────────────────
        # 3단계: ERP 현재고와 비교 (검증)
        # ─────────────────────────────────────────────
        self.stdout.write(self.style.WARNING('\n[3/3] ERP 현재고 비교 검증...\n'))

        try:
            from material.erp_api import fetch_erp_stock
            from datetime import datetime

            ok, items, err = fetch_erp_stock(
                year=str(datetime.now().year), month=None, total_fg='0'
            )
            if not ok:
                self.stderr.write(f'  ERP 조회 실패: {err}')
                return

            from orders.models import Part
            from material.models import Warehouse
            part_map = {p.part_no: p.id for p in Part.objects.all()}
            wh_map = {w.code: w.id for w in Warehouse.objects.filter(is_active=True)}

            erp_map = {}
            for item in items:
                qty = int(item.get('invQt1', 0) or 0)
                wh_id = wh_map.get(item.get('whCd', ''))
                part_id = part_map.get(item.get('itemCd', ''))
                if wh_id and part_id:
                    key = (wh_id, part_id)
                    erp_map[key] = erp_map.get(key, 0) + qty

            # SCM 재고 맵
            scm_map = {}
            for s in MaterialStock.objects.all():
                key = (s.warehouse_id, s.part_id)
                scm_map[key] = scm_map.get(key, 0) + s.quantity

            # 비교
            all_keys = set(erp_map.keys()) | set(scm_map.keys())
            diff_count = 0
            for key in all_keys:
                erp_qty = erp_map.get(key, 0)
                scm_qty = scm_map.get(key, 0)
                if erp_qty != scm_qty:
                    diff_count += 1

            if diff_count == 0:
                self.stdout.write(self.style.SUCCESS('  ERP↔SCM 재고 완전 일치!'))
            else:
                self.stdout.write(self.style.WARNING(
                    f'  ERP↔SCM 차이: {diff_count}건 (adjust_stock_to_erp로 조정 필요)'
                ))
                self.stdout.write('  → python manage.py sync_erp_history --no-adjust 실행 후')
                self.stdout.write('  → "ERP 기준 재고조정" 버튼을 사용하세요')

        except Exception as e:
            self.stderr.write(f'  ERP 검증 실패 (무시 가능): {e}')

        self.stdout.write(self.style.SUCCESS('\n=== 완료 ==='))
