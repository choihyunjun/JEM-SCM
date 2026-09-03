"""
NULL LOT 재고 정리 커맨드

ERP 현재고를 단일 진실로 보고, SCM 재고를 두 방향으로 정합시킨다.

  (A) NULL 버킷이 과다(양수 초과)      → 초과분을 NULL에서 제거
  (B) LOT 재고 합계가 ERP 현재고 초과   → 오래된 LOT부터 FIFO로 축소하고
                                          NULL 버킷은 0 이상으로 복구
                                          (과거 "ERP 재고 -380,466" 같은 garbage 해소)

대부분의 (B) 케이스는 "현재NULL = -(LOT합 - ERP)" 라서 총재고는 이미 ERP와 일치한다.
→ 축소는 표시만 정리(총재고 불변). 총재고가 실제로 바뀌는 건은 별도로 집계/표시한다.

사용법:
  python manage.py cleanup_null_lot                     # 드라이런 (변경 없음)
  python manage.py cleanup_null_lot --part RJBE2        # 특정 품번만 (부분일치)
  python manage.py cleanup_null_lot --warehouse 4200    # 특정 창고만
  python manage.py cleanup_null_lot --part RJBE2 --execute
  python manage.py cleanup_null_lot --execute --limit 100   # 100건씩 나눠 실행 (재실행 시 이어서)
  python manage.py cleanup_null_lot --execute --only cosmetic   # 총재고 불변인 건만
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
        parser.add_argument('--limit', type=int, default=0, help='이번 실행에서 처리할 최대 건수 (0=전체)')
        parser.add_argument('--only', type=str, default='', choices=['', 'cosmetic', 'netchange'],
                            help='cosmetic=총재고 불변 건만 / netchange=총재고 변동 건만')

    def handle(self, *args, **options):
        from material.models import MaterialStock, Warehouse
        from material.erp_api import fetch_erp_stock
        from orders.models import Part

        execute = options['execute']
        wh_filter = options['warehouse']
        part_filter = options['part']
        limit = options['limit']
        only = options['only']

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
            for row in qs.values('warehouse__code', 'part__part_no').annotate(total=Sum('quantity')):
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
        all_part_nos = {k[1] for k in set(null_map) | set(lot_map)}
        part_objs = {p.part_no: p for p in Part.objects.filter(part_no__in=all_part_nos)}

        # 3) 분석
        keys = sorted(set(null_map) | set(lot_map))
        issues_over = []     # (B) LOT > ERP
        issues_excess = []   # (A) NULL 과다
        for key in keys:
            wh_code, part_no = key
            erp_total = erp_map.get(key, 0)
            lot_total = lot_map.get(key, 0)
            current_null = null_map.get(key, 0)

            if lot_total > erp_total:
                trim = lot_total - erp_total
                # 축소 후: lot=erp, null=max(0, 0)=0  → net_after = erp_total
                net_before = lot_total + current_null
                net_after = erp_total
                issues_over.append({
                    'key': key, 'wh_code': wh_code, 'wh_name': wh_names.get(wh_code, wh_code),
                    'part_no': part_no,
                    'erp_total': erp_total, 'lot_total': lot_total, 'current_null': current_null,
                    'trim': trim, 'net_delta': net_after - net_before,
                })
                continue

            expected_null = max(0, erp_total - lot_total)
            excess = current_null - expected_null
            if excess > 0:
                issues_excess.append({
                    'key': key, 'wh_code': wh_code, 'wh_name': wh_names.get(wh_code, wh_code),
                    'part_no': part_no,
                    'erp_total': erp_total, 'lot_total': lot_total,
                    'current_null': current_null, 'expected_null': expected_null,
                    'excess': excess, 'net_delta': -excess,
                })

        # --only 필터
        def _match_only(it):
            if only == 'cosmetic':
                return it['net_delta'] == 0
            if only == 'netchange':
                return it['net_delta'] != 0
            return True
        issues_over = [it for it in issues_over if _match_only(it)]
        issues_excess = [it for it in issues_excess if _match_only(it)]

        # 4) 요약
        over_cosmetic = [it for it in issues_over if it['net_delta'] == 0]
        over_netchg = [it for it in issues_over if it['net_delta'] != 0]
        self.stdout.write(
            f'\n분석: 총 {len(keys)}건 확인\n'
            f'  (B) LOT>ERP : {len(issues_over)}건 '
            f'(총재고 불변 {len(over_cosmetic)} / 총재고 변동 {len(over_netchg)}), '
            f'축소량 합계 {sum(it["trim"] for it in issues_over):,}\n'
            f'  (A) NULL 과다: {len(issues_excess)}건, '
            f'제거량 합계 {sum(it["excess"] for it in issues_excess):,}\n'
        )

        if over_netchg:
            self.stdout.write(self.style.ERROR('── 총재고가 실제로 바뀌는 건 (ERP 기준으로 축소) ──'))
            self.stdout.write(f'{"창고":12} {"품번":20} {"ERP":>12} {"LOT합":>12} {"현재NULL":>12} {"총재고Δ":>12}')
            self.stdout.write('-' * 86)
            for it in sorted(over_netchg, key=lambda x: x['net_delta'])[:60]:
                self.stdout.write(
                    f'{it["wh_code"]:5} {it["wh_name"][:6]:6} {it["part_no"]:20} '
                    f'{it["erp_total"]:>12,} {it["lot_total"]:>12,} {it["current_null"]:>12,} {it["net_delta"]:>12,}'
                )
            if len(over_netchg) > 60:
                self.stdout.write(f'  ... 외 {len(over_netchg) - 60}건')
            self.stdout.write(f'  총재고 변동 합계: {sum(it["net_delta"] for it in over_netchg):,}\n')

        if not issues_over and not issues_excess:
            self.stdout.write(self.style.SUCCESS('정리할 NULL LOT 가비지 없음'))
            return

        if not execute:
            self.stdout.write(self.style.WARNING(
                '\n[드라이런] 실제 정리하려면 --execute. '
                '먼저 --part 로 좁혀서 확인하거나 --only cosmetic 으로 표시정리만 먼저 하는 것을 권장.'
            ))
            return

        # 5) 실행 — (창고,품번) 단위로 개별 커밋 (Ctrl+C 해도 완료분 유지, 재실행 시 이어서)
        from django.db import transaction as db_transaction
        from django.db.models import F
        from django.utils import timezone
        from material.erp_api import _create_trx, _trim_lot_stock_fifo

        work = [('over', it) for it in issues_over] + [('excess', it) for it in issues_excess]
        if limit and limit > 0:
            work = work[:limit]
        total_work = len(work)
        self.stdout.write(self.style.WARNING(f'\n{total_work}건 처리 시작 (품목별 개별 커밋)...'))

        done_over = done_excess = failed = 0
        for i, (kind, it) in enumerate(work, 1):
            wh = wh_objs.get(it['wh_code'])
            part = part_objs.get(it['part_no'])
            if not wh or not part:
                continue
            try:
                with db_transaction.atomic():
                    now = timezone.now()
                    if kind == 'over':
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
                    else:
                        ns = MaterialStock.objects.filter(warehouse=wh, part=part, lot_no=None).first()
                        if ns:
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
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING(
                    f'\n중단됨. 여기까지 커밋 완료: LOT축소 {done_over} / NULL 과다 {done_excess}. '
                    f'재실행하면 남은 건부터 이어서 처리합니다.'
                ))
                return
            except Exception as e:
                failed += 1
                self.stderr.write(self.style.ERROR(f'  실패 {it["wh_code"]}/{it["part_no"]}: {e}'))
                continue

            if i % 50 == 0 or i == total_work:
                self.stdout.write(f'  {i}/{total_work} ...')

        self.stdout.write(self.style.SUCCESS(
            f'\n정리 완료: LOT축소 {done_over}건 / NULL 과다 {done_excess}건 / 실패 {failed}건'
        ))
