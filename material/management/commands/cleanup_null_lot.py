"""
NULL LOT 재고 정리 커맨드

ERP총량 vs SCM LOT합 기준으로 초과된 NULL LOT 레코드를 정리합니다.

사용법:
  python manage.py cleanup_null_lot           # 드라이런 (실제 변경 없음)
  python manage.py cleanup_null_lot --execute  # 실제 정리 실행
  python manage.py cleanup_null_lot --warehouse 2000  # 특정 창고만
  python manage.py cleanup_null_lot --part ZR702      # 특정 품번만
"""

from django.core.management.base import BaseCommand
from django.db.models import Sum


class Command(BaseCommand):
    help = 'NULL LOT 가비지 재고 정리 (ERP 총량 기준 초과분 제거)'

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
        from datetime import datetime
        ok, erp_items, err = fetch_erp_stock(year=str(datetime.now().year), total_fg='0')
        if not ok:
            self.stderr.write(self.style.ERROR(f'ERP 조회 실패: {err}'))
            return

        # (whCd, itemCd) → erp_qty
        erp_map = {}
        for item in (erp_items or []):
            qty = int(item.get('invQt1', 0) or 0)
            if qty > 0:
                key = (item.get('whCd', ''), item.get('itemCd', ''))
                erp_map[key] = erp_map.get(key, 0) + qty

        # 2) SCM NULL LOT 재고 조회
        null_stocks = MaterialStock.objects.filter(lot_no__isnull=True).select_related('warehouse', 'part')
        if wh_filter:
            null_stocks = null_stocks.filter(warehouse__code=wh_filter)
        if part_filter:
            null_stocks = null_stocks.filter(part__part_no__icontains=part_filter)

        # 3) SCM LOT 재고 집계 (lot_no IS NOT NULL)
        lot_agg = MaterialStock.objects.filter(lot_no__isnull=False).values(
            'warehouse__code', 'part__part_no'
        ).annotate(total=Sum('quantity'))
        lot_map = {}
        for row in lot_agg:
            key = (row['warehouse__code'], row['part__part_no'])
            lot_map[key] = int(row['total'] or 0)

        # 4) 품목별 분석
        total_checked = 0
        total_excess = 0
        total_cleaned = 0
        issues = []

        wh_names = {w.code: w.name for w in Warehouse.objects.all()}
        part_names = {p.part_no: p.part_name for p in Part.objects.all()}

        for null_stock in null_stocks:
            wh_code = null_stock.warehouse.code
            part_no = null_stock.part.part_no
            key = (wh_code, part_no)

            erp_total = erp_map.get(key, 0)
            lot_total = lot_map.get(key, 0)
            current_null = null_stock.quantity
            expected_null = max(0, erp_total - lot_total)
            excess = current_null - expected_null

            total_checked += 1

            if excess <= 0:
                continue

            total_excess += excess
            wh_name = wh_names.get(wh_code, wh_code)
            part_name = part_names.get(part_no, '')

            issues.append({
                'null_stock': null_stock,
                'wh_code': wh_code, 'wh_name': wh_name,
                'part_no': part_no, 'part_name': part_name,
                'erp_total': erp_total, 'lot_total': lot_total,
                'current_null': current_null, 'expected_null': expected_null,
                'excess': excess,
            })

        # 5) 결과 출력
        self.stdout.write(f'\n분석 결과: 총 {total_checked}건 확인, 초과 {len(issues)}건 발견')
        self.stdout.write('')

        if not issues:
            self.stdout.write(self.style.SUCCESS('정리할 NULL LOT 가비지 없음'))
            return

        self.stdout.write(f'{"창고":15} {"품번":20} {"ERP총량":>10} {"LOT합":>10} {"현재NULL":>10} {"예상NULL":>10} {"초과":>10}')
        self.stdout.write('-' * 90)

        for issue in issues:
            self.stdout.write(
                f'{issue["wh_code"]:6} {issue["wh_name"][:8]:8} '
                f'{issue["part_no"]:20} '
                f'{issue["erp_total"]:>10,} '
                f'{issue["lot_total"]:>10,} '
                f'{issue["current_null"]:>10,} '
                f'{issue["expected_null"]:>10,} '
                f'{issue["excess"]:>10,}'
            )

        self.stdout.write('')
        self.stdout.write(f'초과 총계: {total_excess:,}')

        # 6) 실행 모드에서 정리
        if execute:
            from django.db import transaction as db_transaction
            from django.db.models import F
            from material.models import MaterialTransaction
            from material.erp_api import _create_trx
            from django.utils import timezone

            now = timezone.now()

            with db_transaction.atomic():
                for issue in issues:
                    null_stock = issue['null_stock']
                    excess = issue['excess']
                    new_qty = issue['expected_null']

                    MaterialStock.objects.filter(pk=null_stock.pk).update(quantity=new_qty)

                    _create_trx(
                        transaction_type='ADJ_ERP_OUT',
                        part=null_stock.part,
                        warehouse_from=null_stock.warehouse,
                        quantity=excess,
                        lot_no=None,
                        date=now,
                        remark=f'NULL LOT 가비지 정리 (ERP={issue["erp_total"]}, LOT합={issue["lot_total"]}, 초과={excess})',
                    )
                    total_cleaned += 1

            self.stdout.write(self.style.SUCCESS(
                f'\n정리 완료: {total_cleaned}건 조정 (총 {total_excess:,} 제거)'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f'\n[드라이런] 실제 정리하려면 --execute 옵션을 추가하세요.'
            ))
