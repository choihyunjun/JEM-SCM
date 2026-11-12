"""성형 가동률 종합보고서(엑셀).

가동률 조회 화면의 세 탭(차트 · 표 · 호기별 세부분석)과, 호기를 눌러야 보이던 생산 실적·유실 내역을
한 파일로 모은다. 숫자는 화면과 같은 집계(views._molding_analytics_context)를 그대로 쓴다.
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

NAVY, MUTED = '243B53', '627D98'
F_TITLE = Font(name='맑은 고딕', size=14, bold=True, color=NAVY)
F_SECTION = Font(name='맑은 고딕', size=11, bold=True, color=NAVY)
F_HEAD = Font(name='맑은 고딕', size=10, bold=True, color='FFFFFF')
F_BODY = Font(name='맑은 고딕', size=10, color='243746')
F_BOLD = Font(name='맑은 고딕', size=10, bold=True, color='243746')
F_NOTE = Font(name='맑은 고딕', size=9, color=MUTED)
FILL_HEAD = PatternFill('solid', fgColor=NAVY)
FILL_TOTAL = PatternFill('solid', fgColor='EDF3F8')
FILL_KPI = PatternFill('solid', fgColor='F8FAFC')
THIN = Side(style='hair', color='C5D0DB')
BORDER = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
CENTER = Alignment(horizontal='center', vertical='center', wrap_text=True)
LEFT = Alignment(horizontal='left', vertical='center')
RIGHT = Alignment(horizontal='right', vertical='center')
INT, PCT, DEC = '#,##0', '0.0%', '#,##0.0'


class Sheet:
    """행을 위에서부터 채워 가는 작은 도우미 (서식을 한 곳에서 맞춘다)."""

    def __init__(self, ws, widths):
        self.ws, self.row = ws, 1
        for index, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(index)].width = width

    def title(self, text, note=''):
        self.ws.cell(self.row, 1, text).font = F_TITLE
        self.ws.row_dimensions[self.row].height = 26
        self.row += 1
        if note:
            self.ws.cell(self.row, 1, note).font = F_NOTE
            self.row += 1
        self.row += 1

    def section(self, text, note=''):
        self.ws.cell(self.row, 1, text).font = F_SECTION
        if note:
            self.ws.cell(self.row, 4, note).font = F_NOTE
        self.row += 1

    def header(self, labels):
        for col, label in enumerate(labels, 1):
            cell = self.ws.cell(self.row, col, label)
            cell.font, cell.fill, cell.alignment, cell.border = F_HEAD, FILL_HEAD, CENTER, BORDER
        self.ws.row_dimensions[self.row].height = 22
        self.row += 1
        return self.row - 1

    def line(self, values, formats=None, total=False):
        """values: 셀 값 목록. formats: 열별 표시 형식(None이면 글자)."""
        formats = formats or [None] * len(values)
        for col, (value, fmt) in enumerate(zip(values, formats), 1):
            cell = self.ws.cell(self.row, col, value)
            cell.font = F_BOLD if total else F_BODY
            cell.border = BORDER
            if total:
                cell.fill = FILL_TOTAL
            if fmt:
                cell.number_format, cell.alignment = fmt, RIGHT
            else:
                cell.alignment = LEFT
        self.row += 1
        return self.row - 1

    def gap(self, rows=1):
        self.row += rows


def _pct(value):
    """화면의 92.7 같은 값을 엑셀 백분율(0.927)로. 엑셀에서 다시 계산할 수 있게 숫자로 둔다."""
    return round((value or 0) / 100, 4)


def _ratio(part, whole):
    return round(part / whole, 4) if whole else 0


def build_molding_report(ctx):
    """화면 집계(ctx)로 종합보고서 통합문서를 만든다."""
    year, month = ctx['year'], ctx['month']
    period = f'{year}년 {month}월'
    made = timezone.localtime().strftime('%Y-%m-%d %H:%M')
    loads = lambda key: json.loads(ctx[key])  # noqa: E731 — 차트용으로 직렬화된 값을 되돌린다
    modal = loads('machine_modal_data')
    wb = Workbook()

    # ───────────────────────── 1. 요약 ─────────────────────────
    s = Sheet(wb.active, [16, 14, 14, 14, 14, 14, 14, 14])
    s.ws.title = '요약'
    s.title(f'성형 가동률 종합보고서 — {period}',
            f'생성 {made} · 실적 {ctx["total_records"]:,}건 · 근무일수 설정 {ctx["setting_work_days"]}일 / 실적 {ctx["actual_work_days"]}일')

    s.section('핵심 지표')
    s.header(['지표', '값', '기준'])
    for label, value, fmt, basis in [
        ('설비가동률', _pct(ctx['kpi_utilization']), PCT, '가동시간 ÷ 부하시간'),
        ('시간가동률 (설정 기준)', _pct(ctx['kpi_time_rate']), PCT, f'가동시간 ÷ 전체 근무시간 ({ctx["setting_work_days"]}일)'),
        ('시간가동률 (실적 기준)', _pct(ctx['kpi_time_rate_actual']), PCT, f'가동시간 ÷ 실적이 있는 날 근무시간 ({ctx["actual_work_days"]}일)'),
        ('계획정지 (시간)', ctx['kpi_planned_hours'], DEC, '유실 사유 중 계획정지'),
        ('실유실 (시간)', ctx['kpi_actual_loss_hours'], DEC, '유실 합계 − 계획정지'),
        ('유실 합계 (시간)', ctx['kpi_total_loss_hours'], DEC, ''),
    ]:
        r = s.line([label, value, basis], [None, fmt, None])
        s.ws.cell(r, 1).fill = FILL_KPI
        s.ws.cell(r, 2).font = F_BOLD
    s.gap()

    s.section('톤수별 현황')
    first = s.header(['톤수', '보유대수', '가동대수', '설비가동률', '시간가동률(설정)', '시간가동률(실적)'])
    for t in ctx['tonnage_table']:
        s.line([f'{t["tonnage"]}t', t['count'], t['active_count'], _pct(t['util']), _pct(t['time_rate']),
                _pct(t['time_rate_actual'])], [None, INT, INT, PCT, PCT, PCT])
    last = s.row - 1
    if ctx['tonnage_table']:
        s.line(['전체', ctx['total_machine_count'], sum(t['active_count'] for t in ctx['tonnage_table']),
                _pct(ctx['kpi_utilization']), _pct(ctx['kpi_time_rate']), _pct(ctx['kpi_time_rate_actual'])],
               [None, INT, INT, PCT, PCT, PCT], total=True)
        chart = BarChart()
        chart.title, chart.height, chart.width = '톤수별 설비가동률', 7.5, 14
        chart.y_axis.scaling.min, chart.y_axis.scaling.max, chart.y_axis.number_format = 0, 1, '0%'
        chart.add_data(Reference(s.ws, min_col=4, min_row=first, max_row=last), titles_from_data=True)
        chart.set_categories(Reference(s.ws, min_col=1, min_row=first + 1, max_row=last))
        chart.legend = None
        s.ws.add_chart(chart, 'J4')
    s.gap()

    s.section('일별 설비가동률', '실적이 있는 날만 표시')
    first = s.header(['일자', '설비가동률'])
    for label, rate in zip(loads('daily_labels'), loads('daily_rates')):
        s.line([f'{month}월 {label}', _pct(rate)], [None, PCT])
    last = s.row - 1
    if last > first:
        chart = LineChart()
        chart.title, chart.height, chart.width = '일별 설비가동률', 7.5, 14
        chart.y_axis.scaling.min, chart.y_axis.scaling.max, chart.y_axis.number_format = 0, 1, '0%'
        chart.add_data(Reference(s.ws, min_col=2, min_row=first, max_row=last), titles_from_data=True)
        chart.set_categories(Reference(s.ws, min_col=1, min_row=first + 1, max_row=last))
        chart.legend = None
        s.ws.add_chart(chart, 'J20')
    s.gap()

    s.section('월별 추이 (최근 6개월)', '마지막 달은 집계 중일 수 있음')
    first = s.header(['월', '설비가동률', '시간가동률'])
    for label, util, time_rate in zip(loads('monthly_labels'), loads('monthly_utilization'), loads('monthly_time_rate')):
        s.line([label, _pct(util), _pct(time_rate)], [None, PCT, PCT])
    last = s.row - 1
    if last > first:
        chart = LineChart()
        chart.title, chart.height, chart.width = '월별 추이', 7.5, 14
        chart.y_axis.scaling.min, chart.y_axis.scaling.max, chart.y_axis.number_format = 0, 1, '0%'
        chart.add_data(Reference(s.ws, min_col=2, max_col=3, min_row=first, max_row=last), titles_from_data=True)
        chart.set_categories(Reference(s.ws, min_col=1, min_row=first + 1, max_row=last))
        s.ws.add_chart(chart, 'J36')

    # ───────────────────────── 2. 유실 분석 ─────────────────────────
    tonnages = [f'{t}t' for t in ctx['all_tonnages']]
    s = Sheet(wb.create_sheet('유실 분석'), [18, 12] + [11] * (len(tonnages) + 2))
    s.title(f'유실 분석 — {period}')

    labels, values = loads('loss_labels'), loads('loss_values')
    total_loss = sum(values)
    s.section('유실 사유별 합계')
    s.header(['사유', '시간(분)', '시간(h)', '비율'])
    for label, minutes in zip(labels, values):
        s.line([label, minutes, round(minutes / 60, 1), _ratio(minutes, total_loss)], [None, INT, DEC, PCT])
    if labels:
        s.line(['합계', total_loss, round(total_loss / 60, 1), 1], [None, INT, DEC, PCT], total=True)
    s.gap()

    s.section('유실 사유 × 톤수 (분)', '구분: 관리LOSS = 계획정지·생산종료·식사/휴식·청소, 그 외는 시간LOSS')
    s.header(['사유', '구분'] + tonnages + ['합계'])
    for row in ctx['loss_category_table']:
        s.line([row['label'], '관리LOSS' if row['is_mgmt'] else '시간LOSS'] + list(row['values']) + [row['total']],
               [None, None] + [INT] * (len(tonnages) + 1))
    s.gap()

    s.section('톤수별 대당 평균 시간 (분)', '호기 1대 기준 평균')
    s.header(['구분', ''] + tonnages + ['평균'])
    time_rows = ctx['tonnage_time_table']
    total_row = ctx['tonnage_time_total']
    s.line(['보유대수', ''] + [r['count'] for r in time_rows] + [''], [None, None] + [INT] * (len(tonnages) + 1))
    for label, key in [('근무시간', 'work_h'), ('가동시간', 'operating_h'), ('관리LOSS', 'mgmt_loss_h'),
                       ('시간LOSS', 'time_loss_h'), ('유실 합계', 'loss_h')]:
        s.line([label, ''] + [r[key] for r in time_rows] + [total_row[key]],
               [None, None] + [DEC] * (len(tonnages) + 1), total=(key == 'loss_h'))

    # ───────────────────────── 3. 호기별 종합 ─────────────────────────
    s = Sheet(wb.create_sheet('호기별 종합'), [8, 10, 10, 9, 12, 12, 12, 12, 12, 11, 13, 13, 12, 12, 10, 9, 22, 30])
    s.title(f'호기별 종합 — {period}',
            '호기마다 한 줄. 가동 실적과 생산 실적, 가장 큰 유실 사유, 가장 많이 만든 품번을 함께 본다. 시간 단위는 분.')
    head = s.header(['톤수', '호기', '근무구분', '가동일수', '근무시간', '가동시간', '관리LOSS', '시간LOSS', '유실합계',
                     '설비가동률', '시간가동률(설정)', '시간가동률(실적)', '총 생산', '양품', '불량', '불량률',
                     '최대 유실 사유', '주 생산 품번'])
    formats = [None, None, None, INT, INT, INT, INT, INT, INT, PCT, PCT, PCT, INT, INT, INT, PCT, None, None]
    work_min = ctx['work_per_machine_min']
    for group in ctx['machine_analysis_table']:
        sums = [0, 0, 0]
        for m in group['machines']:
            data = modal.get(m['code'], {})
            produced, good, bad = data.get('total_qty', 0), data.get('good_qty', 0), data.get('defect_qty', 0)
            sums = [sums[0] + produced, sums[1] + good, sums[2] + bad]
            losses, items = data.get('loss_detail') or [], data.get('items') or []
            top_loss = f'{losses[0]["cat"]} ({losses[0]["minutes"]:,}분)' if losses else ''
            top_item = f'{items[0]["pno"]} {items[0].get("pname") or ""}'.strip() if items else ''
            s.line([f'{group["tonnage"]}t', m['code'], m['shift_label'], m['op_days'] or 0, work_min, m['operating'],
                    m['mgmt_loss'], m['time_loss'], m['loss'], _pct(m['util']), _pct(m['time_rate']),
                    _pct(m['time_rate_actual']), produced, good, bad, _ratio(bad, produced), top_loss, top_item], formats)
        total = group['total']
        s.line([f'{group["tonnage"]}t', '소계', f'가동 {group["active_cnt"]}대', '', work_min * len(group['machines']),
                total['operating'], total['mgmt_loss'], total['time_loss'], total['loss'], _pct(total['util']),
                _pct(total['time_rate']), _pct(total['time_rate_actual']), sums[0], sums[1], sums[2],
                _ratio(sums[2], sums[0]), '', ''], formats, total=True)
    s.ws.freeze_panes = s.ws.cell(head + 1, 3)
    s.ws.auto_filter.ref = f'A{head}:{get_column_letter(len(formats))}{max(head, s.row - 1)}'

    # ───────────────────────── 4. 호기별 생산 실적 ─────────────────────────
    tonnage_of = {m['code']: group['tonnage'] for group in ctx['machine_analysis_table'] for m in group['machines']}
    order = [m['code'] for group in ctx['machine_analysis_table'] for m in group['machines']]
    order += sorted(code for code in modal if code not in tonnage_of)   # 지금은 미사용 처리된 호기의 실적
    s = Sheet(wb.create_sheet('호기별 생산 실적'), [8, 10, 18, 44, 13, 13, 11, 10, 12])
    s.title(f'호기별 생산 실적 — {period}', '화면에서 호기를 눌렀을 때 보이는 기종별 생산 실적을 모두 펼친 표.')
    head = s.header(['톤수', '호기', '품번', '품명', '생산', '양품', '불량', '불량률', '호기 내 비율'])
    formats = [None, None, None, None, INT, INT, INT, PCT, PCT]
    for code in order:
        data = modal.get(code)
        if not data or not data.get('items'):
            continue
        ton = f'{tonnage_of[code]}t' if code in tonnage_of else ''
        item_total = sum(i['qty'] for i in data['items'])
        for item in data['items']:
            s.line([ton, code, item['pno'], item.get('pname') or '', item['qty'], item['good_qty'], item['bad_qty'],
                    _ratio(item['bad_qty'], item['qty']), _ratio(item['qty'], item_total)], formats)
        s.line([ton, code, '합계', '', data.get('total_qty', 0), data.get('good_qty', 0), data.get('defect_qty', 0),
                _ratio(data.get('defect_qty', 0), data.get('total_qty', 0)), 1], formats, total=True)
    s.ws.freeze_panes = s.ws.cell(head + 1, 3)
    s.ws.auto_filter.ref = f'A{head}:I{max(head, s.row - 1)}'

    # ───────────────────────── 5. 호기별 유실 내역 ─────────────────────────
    s = Sheet(wb.create_sheet('호기별 유실 내역'), [8, 10, 16, 12, 12, 12, 10])
    s.title(f'호기별 유실 내역 — {period}', '화면에서 호기를 눌렀을 때 보이는 유실 세부항목을 모두 펼친 표.')
    head = s.header(['톤수', '호기', '사유', '구분', '시간(분)', '시간(h)', '호기 내 비율'])
    formats = [None, None, None, None, INT, DEC, PCT]
    mgmt_labels = {row['label'] for row in ctx['loss_category_table'] if row['is_mgmt']} | {
        '계획정지', '생산종료', '식사휴식', '청소'}
    for code in order:
        data = modal.get(code)
        if not data or not data.get('loss_detail'):
            continue
        ton = f'{tonnage_of[code]}t' if code in tonnage_of else ''
        for loss in data['loss_detail']:
            s.line([ton, code, loss['cat'], '관리LOSS' if loss['cat'] in mgmt_labels else '시간LOSS', loss['minutes'],
                    round(loss['minutes'] / 60, 1), _ratio(loss['minutes'], data.get('loss_total', 0))], formats)
        s.line([ton, code, '합계', '', data.get('loss_total', 0), round(data.get('loss_total', 0) / 60, 1), 1],
               formats, total=True)
    s.ws.freeze_panes = s.ws.cell(head + 1, 3)
    s.ws.auto_filter.ref = f'A{head}:G{max(head, s.row - 1)}'

    # ───────────────────────── 6. 품목군 생산 ─────────────────────────
    s = Sheet(wb.create_sheet('품목군 생산'), [22, 50, 14, 12])
    s.title(f'품목군별 생산수량 (상위 10) — {period}', '품목군별 상위 품번 20개까지 표시.')
    s.header(['품목군', '품번 (품명)', '생산수량', '품목군 내 비율'])
    details = loads('group_detail_data')
    for group, quantity in zip(loads('ranking_top_labels'), loads('ranking_top_rates')):
        s.line([group, '합계', quantity, 1], [None, None, INT, PCT], total=True)
        for item in details.get(group, []):
            s.line([group, item['name'], item['qty'], _ratio(item['qty'], quantity)], [None, None, INT, PCT])

    for ws in wb.worksheets:
        ws.sheet_view.showGridLines = False
        ws.page_setup.orientation, ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 'landscape', 1, 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
    return wb


@login_required
def molding_analytics_report(request):
    """[생산현장] 성형 가동률 종합보고서 엑셀 내려받기 (내부 직원 전용)."""
    from orders.access import vendor_identity
    from .views import _molding_analytics_context
    if not request.user.is_superuser and vendor_identity(request.user)[0]:
        messages.error(request, '종합보고서는 내부 직원만 내려받을 수 있습니다.')
        return redirect('order_list')
    now = timezone.localtime()
    try:
        year = int(request.GET.get('year', now.year))
        month = int(request.GET.get('month', now.month))
        if not (2000 <= year <= 2100 and 1 <= month <= 12):
            raise ValueError
    except (TypeError, ValueError):
        messages.error(request, '보고서 기간(연·월)이 올바르지 않습니다.')
        return redirect('material:molding_analytics')
    workbook = build_molding_report(_molding_analytics_context(year, month))
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="molding_report_{year}{month:02d}.xlsx"'
    workbook.save(response)
    return response
