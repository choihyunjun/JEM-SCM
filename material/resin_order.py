"""원재료 재고·발주 점검: 원단위 중량 관리의 월 사용량과 현재 재고를 견줘 발주할 원재료를 가린다.

- 사용량: 원단위 중량 관리의 원재료별 총 사용량 (resin_yield.build_report)
- 3개월 평균: 선택한 달까지의 '끝난 달' 3개. 진행 중인 달은 덜 채워져 평균을 낮추므로 뺀다
- 재고: 원재료창고(3200)·3공장 창고(3000)·자재창고(2000)의 현재고 합계 (ERP 보정이 반영된 SCM 재고)
- 판정: 재고가 0 이하면 '재고 없음', 평균 한 달 사용량보다 적으면 '부족'.
  3개월 동안 쓴 적이 없는 원재료는 이 달 사용량을 기준으로 삼는다
"""
from urllib.parse import quote

from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from orders.decorators import wms_permission_required
from orders.models import Part
from .models import MaterialStock, Warehouse
from .quantities import stock_total
from .resin_yield import _month_param, build_report

STOCK_WAREHOUSES = ('3200', '3000', '2000')
AVERAGE_MONTHS = 3
EMPTY, SHORT, ENOUGH = '재고 없음', '부족', '충분'
STATUS_ORDER = {EMPTY: 0, SHORT: 1, ENOUGH: 2}


def _previous(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def average_months(year, month, today=None):
    """평균을 낼 달 (오래된 달부터): 선택한 달까지의 끝난 달 3개."""
    today = today or timezone.localdate()
    current = (year, month)
    while current >= (today.year, today.month):
        current = _previous(*current)
    months = [current]
    while len(months) < AVERAGE_MONTHS:
        months.append(_previous(*months[-1]))
    return months[::-1]


def build_order_report(year, month, refresh=False, today=None):
    months = average_months(year, month, today)
    usage, info = {}, {}  # usage[(년, 월)][재료품번] = kg
    for ym in dict.fromkeys(months + [(year, month)]):
        report = build_report(*ym, refresh=refresh)
        if report.get('error'):
            return {'error': f"{ym[0]}년 {ym[1]}월 ERP 생산실적 조회 실패: {report['error']}"}
        usage[ym] = {}
        for g in report['by_material']:
            if g['key']:
                usage[ym][g['key']] = g['total_kg']
                info[g['key']] = {'material_name': g['material_name'], 'resin': g['resin']}

    codes = sorted(info)
    parts = {p.part_no: p for p in Part.objects.filter(part_no__in=codes).select_related('vendor')}
    stocks = {}
    for row in (MaterialStock.objects.filter(part__part_no__in=codes, warehouse__code__in=STOCK_WAREHOUSES)
                .values('part__part_no', 'warehouse__code').annotate(qty=Sum('quantity'))):
        stocks[row['part__part_no'], row['warehouse__code']] = float(stock_total(row['qty']))

    rows = []
    for code in codes:
        monthly = [usage[ym].get(code, 0.0) for ym in months]
        month_kg = usage[(year, month)].get(code, 0.0)
        average = sum(monthly) / AVERAGE_MONTHS
        basis = average or month_kg
        by_warehouse = [stocks.get((code, wh), 0.0) for wh in STOCK_WAREHOUSES]
        stock = sum(by_warehouse)
        status = EMPTY if stock <= 0 else SHORT if stock < basis else ENOUGH
        part = parts.get(code)
        rows.append({
            'material_part_no': code, **info[code],
            'vendor': part.vendor.name if part and part.vendor else '',
            'registered': part is not None,
            'month_kg': month_kg, 'monthly': monthly, 'average_kg': average,
            'monthly_text': ' · '.join(f'{m}월 {kg:,.1f}kg' for (_, m), kg in zip(months, monthly)),
            'by_warehouse': by_warehouse, 'stock_kg': stock,
            'cover_months': stock / basis if basis else None,
            'short_kg': max(basis - stock, 0.0),
            'status': status, 'need': status != ENOUGH,
        })
    rows.sort(key=lambda r: (STATUS_ORDER[r['status']],
                             r['cover_months'] if r['cover_months'] is not None else 0, -r['average_kg']))
    names = dict(Warehouse.objects.filter(code__in=STOCK_WAREHOUSES).values_list('code', 'name'))
    return {
        'rows': rows,
        'months': [{'year': y, 'month': m, 'label': f'{m}월'} for y, m in months],
        'warehouses': [{'code': wh, 'name': names.get(wh, '')} for wh in STOCK_WAREHOUSES],
        'empty_count': sum(r['status'] == EMPTY for r in rows),
        'short_count': sum(r['status'] == SHORT for r in rows),
        'need_count': sum(r['need'] for r in rows),
        'short_kg': sum(r['short_kg'] for r in rows),
        'checked_at': timezone.localtime().strftime('%Y-%m-%d %H:%M'),
    }


@wms_permission_required('can_wms_stock_view')
def resin_order(request):
    """원단위 중량 관리의 [재고·발주] 탭 내용. 탭을 처음 열 때 불러온다 (ERP 조회가 달마다 한 번씩 더 든다)."""
    year, month = _month_param(request)
    return render(request, 'material/resin_yield_order.html', {
        'order': build_order_report(year, month), 'month': f'{year}-{month:02d}', 'mon': month,
    })


@wms_permission_required('can_wms_stock_view')
def resin_order_excel(request):
    year, month = _month_param(request)
    order = build_order_report(year, month)
    if order.get('error'):
        return HttpResponse(order['error'], status=502, content_type='text/plain; charset=utf-8')
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    filename = f'원재료 재고·발주 점검 ({year}년 {month}월).xlsx'
    response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(filename)}"
    build_workbook(order, year, month).save(response)
    return response


def build_workbook(order, year, month):
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    from .resin_yield_excel import FONT, KG, RED, _header, _page, _title, _widths, _write_row

    fill_empty = PatternFill('solid', start_color='FCE4E4', end_color='FCE4E4')
    fill_short = PatternFill('solid', start_color='FFF2CC', end_color='FFF2CC')
    month_labels = [m['label'] for m in order['months']]
    headers = (['발주', '상태', '재료품번', '재료명', '재질', '업체', f'{month}월 사용량(kg)']
               + [f'{label} 사용량(kg)' for label in month_labels]
               + ['3개월 평균(kg)'] + [f"{w['code']} {w['name']}".strip() for w in order['warehouses']]
               + ['재고 합계(kg)', '재고 개월수', '부족량(kg)', '비고'])
    number_cols = range(7, len(headers))
    formats = {col: KG for col in number_cols}
    months_col = len(headers) - 2
    formats[months_col] = '0.0;-0.0;"-"'

    wb = openpyxl.Workbook()
    for title, rows in (('발주 필요', [r for r in order['rows'] if r['need']]), ('전체 원재료', order['rows'])):
        ws = wb.active if title == '발주 필요' else wb.create_sheet()
        ws.title = title
        _title(ws, len(headers), f'원재료 재고·발주 점검 - {title} ({year}년 {month}월)',
               f"사용량: 원단위 중량 관리 · 평균: {'·'.join(month_labels)} · 재고: {order['checked_at']} 현재 "
               f"({', '.join(w['code'] for w in order['warehouses'])} 창고 합계) · 부족량 = 평균 한 달 사용량 − 재고")
        _header(ws, 3, headers)
        for i, r in enumerate(rows):
            row = 4 + i
            values = (['●' if r['need'] else '', r['status'], r['material_part_no'], r['material_name'], r['resin'],
                       r['vendor'], r['month_kg']] + r['monthly'] + [r['average_kg']] + r['by_warehouse']
                      + [r['stock_kg'], r['cover_months'] if r['cover_months'] is not None else '-', r['short_kg'],
                         '' if r['registered'] else 'SCM 품목 마스터에 없음'])
            fill = fill_empty if r['status'] == EMPTY else fill_short if r['status'] == SHORT else None
            values = [round(v, 2) if isinstance(v, float) else v for v in values]
            _write_row(ws, row, values, formats, fills={1: fill, 2: fill} if fill else None, stripe=i % 2 == 1)
            if r['need']:
                for col in (1, 2):
                    ws.cell(row, col).font = Font(name=FONT, size=10, bold=True,
                                                  color=RED if r['status'] == EMPTY else '9C5700')
        if not rows:
            ws.cell(4, 1, '해당 원재료 없음')
        _widths(ws, [6, 10, 14, 34, 9, 20] + [13] * (len(headers) - 7) + [22])
        ws.freeze_panes = 'D4'
        _page(ws)
    return wb
