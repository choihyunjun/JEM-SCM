"""
NULL LOT 재고 정리 커맨드

ERP 현재고를 단일 진실로 보고, SCM 재고를 두 방향으로 정합시킨다.

  (A) NULL 버킷이 과다(양수 초과)      → 초과분을 NULL에서 제거
  (B) LOT 재고 합계가 ERP 현재고 초과   → 오래된 LOT부터 FIFO로 축소하고
                                          NULL 버킷은 0 이상으로 복구
                                          (과거 "ERP 재고 -380,466" 같은 garbage 해소)

사용법:
  python manage.py cleanup_null_lot                    # 드라이런 (실제 변경 없음)
  python manage.py cleanup_null_lot --execute          # 실제 정리 실행
  python manage.py cleanup_null_lot --warehouse 4200   # 특정 창고만
  python manage.py cleanup_null_lot --part RJBE2       # 특정 품번만 (부분일치)
"""

from datetime import datetime

from django.core.management.base import BaseCommand
from django.db.models import Sum


class Command(BaseCommand):
    help = 'NULL LOT 가비지 재고 정리 (ERP 현재고 기준 양방향 정합)'

    def add_arguments(self, parser):
        parser.add_argument('--execute', action='store_true', help='실제 정리 실행 (기본: 드라이런)')
        parser.add_argument('--warehouse', type=str, default='', help='특정 창고코드 필터')
        parser.add_argument('--part', type=str, default='', help='특정 품번 필터 (부분일치)')

    def handle(self, *args, **options):
        from material.models import MaterialStock, Warehouse
        from material.erp_api import fetch_erp_stock
        from orders.models import Part

        execute = options['execute']
        wh_filter = options['warehouse']
        part_filter = options['part']

        self.stdout.write(self.style.WARNING(
            f'{"[실행모드]" if execute else "[드라이런]"} NULL LOT 정리 시작'
        ))

        # 1) ERP 현재고 조회 (품목별 합계)
        self.stdout.write('ERP 현재고 조회 중...')
        ok, erp_items, err = fetch_erp_stock(year=str(datetime.now().year), total_fg='0')
        if not ok:
            self.stderr.write(self.style.ERROR(f'ERP 조회 실패: {err}'))
            return

        erp_map = {}
        for item in (erp_items or []):
            qty = int(item.get('invQt1', 0) or 0)
            key = (item.get('whCd', ''), item.get('itemCd', ''))
            erp_map[key] = erp_map.get(key, 0) + qty

        # 2) SCM 재고 집계 (NULL / LOT 분리)
        def _agg(qs):
            out = {}
            rows = qs.values('warehouse__code', 'part__part_no').annotate(total=Sum('quantity'))
            for row in rows:
                out[(row['warehouse__code'], row['part__part_no'])] = int(row['total'] or 0)
            return out

        null_qs = MaterialStock.objects.filter(lot_no__isnull=True)
        lot_qs = MaterialStock.objects.filter(lot_no__isnull=False)
        if wh_filter:
            null_qs = null_qs.filter(warehouse__code=wh_filter)
            lot_qs = lot_qs.filter(warehouse__code=wh_filter)
        if part_filter:
            null_qs = null_qs.filter(part__part_no__icontains=part_filter)
            lot_qs = lot_qs.filter(part__part_no__icontains=part_filter)

        null_map = _agg(null_qs)
        lot_map = _agg(lot_qs)

        wh_names = {w.code: w.name for w in Warehouse.objects.all()}
        part_names = {p.part_no: p.part_name for p in Part.objects.all()}
        wh_objs = {w.code: w for w in Warehouse.objects.all()}
        part_objs = {p.part_no: p for p in Part.objects.filter(
            part_no__in={k[1] for k in set(null_map) | set(lot_map)}
        )}

        # 3) 분석 — (wh, part) 전체 키 순회
        keys = sorted(set(null_map) | set(lot_map))
        issues_excess = []   # (A) NULL 과다
        issues_over = []     # (B) LOT > ERP
        for key in keys:
            wh_code, part_no = key
            erp_total = erp_map.get(key, 0)
            lot_total = lot_map.get(key, 0)
            current_null = null_map.get(key, 0)

            if lot_total > erp_total:
                issues_over.append({
                    'key': key, 'wh_code': wh_code, 'wh_name': wh_names.get(wh_code, wh_code),
                    'part_no': part_no, 'part_name': part_names.get(part_no, ''),
                    'erp_total': erp_total, 'lot_total': lot_total, 'current_null': current_null,
                    'trim': lot_total - erp_total,
                })
                continue

            expected_null = max(0, erp_total - lot_total)
            excess = current_null - expected_null
            if excess > 0:
                issues_excess.append({
                    'key': key, 'wh_code': wh_code, 'wh_name': wh_names.get(wh_code, wh_code),
                    'part_no': part_no, 'part_name': part_names.get(part_no, ''),
                    'erp_total': erp_total, 'lot_total': lot_total,
                    'current_null': current_null, 'expected_null': expected_null, 'excess': excess,
                })

        # 4) 결과 출력
        self.stdout.write(
            f'\n분석: 총 {len(keys)}건 확인 / '
            f'NULL 과다 {len(issues_excess)}건 / LOT>ERP {len(issues_over)}건\n'
        )

        if issues_over:
            self.stdout.write(self.style.WARNING('── (B) LOT 재고 합계가 ERP 현재고 초과 → 오래된 LOT FIFO 축소 ──'))
            self.stdout.write(f'{"창고":14} {"품번":20} {"ERP":>12} {"LOT합":>12} {"현재NULL":>12} {"축소량":>12}')
            self.stdout.write('-' * 88)
            for it in issues_over:
                self.stdout.write(
                    f'{it["wh_code"]:5} {it["wh_name"][:7]:7} {it["part_no"]:20} '
                    f'{it["erp_total"]:>12,} {it["lot_total"]:>12,} {it["current_null"]:>12,} {it["trim"]:>12,}'
                )
            self.stdout.write(f'축소 총계: {sum(it["trim"] for it in issues_over):,}\n')

        if issues_excess:
            self.stdout.write(self.style.WARNING('── (A) NULL 버킷 과다 → 초과분 제거 ──'))
            self.stdout.write(f'{"창고":14} {"품번":20} {"ERP":>12} {"LOT합":>12} {"현재NULL":>12} {"예상NULL":>12} {"초과":>10}')
            self.stdout.write('-' * 98)
            for it in issues_excess:
                self.stdout.write(
                    f'{it["wh_code"]:5} {it["wh_name"][:7]:7} {it["part_no"]:20} '
                    f'{it["erp_total"]:>12,} {it["lot_total"]:>12,} {it["current_null"]:>12,} '
                    f'{it["expected_null"]:>12,} {it["excess"]:>10,}'
                )
            self.stdout.write(f'초과 총계: {sum(it["excess"] for it in issues_excess):,}\n')

        if not issues_over and not issues_excess:
            self.stdout.write(self.style.SUCCESS('정리할 NULL LOT 가비지 없음'))
            return

        if not execute:
            self.stdout.write(self.style.WARNING('\n[드라이런] 실제 정리하려면 --execute 옵션을 추가하세요.'))
            return

        # 5) 실행
        from django.db import transaction as db_transaction
        from django.db.models import F
        from django.utils import timezone
        from material.erp_api import _create_trx, _trim_lot_stock_fifo

        now = timezone.now()
        done_over = done_excess = 0

        with db_transaction.atomic():
            # (B) LOT > ERP: 오래된 LOT FIFO 축소 후 NULL 을 0 이상으로 복구
            for it in issues_over:
                wh = wh_objs.get(it['wh_code'])
                part = part_objs.get(it['part_no'])
                if not wh or not part:
                    continue
                trimmed = _trim_lot_stock_fifo(
                    wh, part, it['trim'], now,
                    reason=f'NULL LOT 정리: ERP정합 LOT축소 (ERP={it["erp_total"]} < LOT합={it["lot_total"]})',
                )
                lot_after = it['lot_total'] - trimmed
                target_null = max(0, it['erp_total'] - lot_after)
                diff = target_null - it['current_null']
                if diff != 0:
                    ns = MaterialStock.objects.filter(warehouse=wh, part=part, lot_no=None).first()
                    if ns:
                        MaterialStock.objects.filter(pk=ns.pk).update(quantity=F('quantity') + diff)
                    elif target_null != 0:
                        MaterialStock.objects.create(warehouse=wh, part=part, lot_no=None, quantity=target_null)
                    _create_trx(
                        transaction_type='ADJ_ERP_IN' if diff > 0 else 'ADJ_ERP_OUT',
                        part=part,
                        warehouse_to=wh if diff > 0 else None,
                        warehouse_from=None if diff > 0 else wh,
                        quantity=abs(diff),
                        lot_no=None,
                        date=now,
                        remark=f'NULL LOT 정리: NULL 버킷 {it["current_null"]:+,}→{target_null:+,}',
                    )
                done_over += 1

            # (A) NULL 과다
            for it in issues_excess:
                wh = wh_objs.get(it['wh_code'])
                part = part_objs.get(it['part_no'])
                if not wh or not part:
                    continue
                ns = MaterialStock.objects.filter(warehouse=wh, part=part, lot_no=None).first()
                if not ns:
                    continue
                MaterialStock.objects.filter(pk=ns.pk).update(quantity=it['expected_null'])
                _create_trx(
                    transaction_type='ADJ_ERP_OUT',
                    part=part,
                    warehouse_from=wh,
                    quantity=it['excess'],
                    lot_no=None,
                    date=now,
                    remark=f'NULL LOT 가비지 정리 (ERP={it["erp_total"]}, LOT합={it["lot_total"]}, 초과={it["excess"]})',
                )
                done_excess += 1

        self.stdout.write(self.style.SUCCESS(
            f'\n정리 완료: LOT축소 {done_over}건 / NULL 과다 {done_excess}건'
        ))
