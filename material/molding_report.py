"""성형 가동률 종합보고서(엑셀).

가동률 조회 화면의 세 탭(차트 · 표 · 호기별 세부분석)과, 호기를 눌러야 보이던 생산 실적·유실 내역을
한 파일로 모은다. 숫자는 화면과 같은 집계(views._molding_analytics_context)를 그대로 쓴다.

시트 구성
  종합            한 장짜리 대시보드 (지표 카드, 그래프 4개, 톤수별 현황, 눈여겨볼 호기)
  호기별 종합     호기마다 한 줄. 호기 번호를 누르면 상세로 이동
  호기별 상세     호기마다 카드 한 장 (생산 실적 + 유실 내역)
  유실 분석       사유별 합계, 사유 × 톤수, 톤수별 대당 평균
  품목군 생산     품목군 상위 10개와 품번
  원자료-생산/유실  필터·피벗용 목록
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.chart.text import RichText
from openpyxl.drawing.line import LineProperties
from openpyxl.drawing.text import CharacterProperties, Font as DrawingFont, Paragraph, ParagraphProperties
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .models import MOLDING_LOSS_CATEGORIES, MOLDING_MGMT_LOSS

FACE = '맑은 고딕'
INK, MUTED, LINE = '1A1A1A', '666666', 'B5B5B5'
ACCENT, SOFT, RED = '1F4E79', 'A6A6A6', 'C00000'      # 강조색은 하나만. 빨강은 나쁜 값에만 쓴다


def _font(size=10, bold=False, color=INK, underline=None):
    return Font(name=FACE, size=size, bold=bold, color=color, underline=underline)


def _fill(color):
    return PatternFill('solid', fgColor=color)


F_BODY, F_BOLD, F_NOTE = _font(), _font(bold=True), _font(9, color=MUTED)
F_HEAD = _font(bold=True)
F_LINK = _font(color=ACCENT, underline='single')
F_OFF = _font(color='A6A6A6')
F_BAD = _font(bold=True, color=RED)
FILL_HEAD, FILL_GROUP = _fill('E7E7E7'), _fill('F2F2F2')
FILL_TOTAL, FILL_GRAND = _fill('F2F2F2'), _fill('E0E0E0')
RULE = Side(style='medium', color=INK)
THIN = Side(style='thin', color=LINE)
BORDER = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
CENTER = Alignment(horizontal='center', vertical='center', wrap_text=True)
LEFT = Alignment(horizontal='left', vertical='center', indent=1)
RIGHT = Alignment(horizontal='right', vertical='center', indent=1)
INT, PCT, PCT2, DEC = '#,##0', '0.0%', '0.00%', '#,##0.0'
MGMT, TIME = '관리LOSS', '시간LOSS'
LOSS_LABEL = dict(MOLDING_LOSS_CATEGORIES)


def _pct(value):
    """화면의 92.7 같은 값을 엑셀 백분율(0.927)로. 엑셀에서 다시 계산할 수 있게 숫자로 둔다."""
    return round((value or 0) / 100, 4)


def _ratio(part, whole):
    return round(part / whole, 4) if whole else 0


def _put(ws, row, col, value=None, font=F_BODY, fill=None, align=LEFT, border=BORDER, fmt=None, span=1, rows=1):
    """셀 하나(또는 병합 영역)에 값과 서식을 넣는다. 병합하면 테두리가 가장자리까지 이어진다."""
    cell = ws.cell(row, col, value)
    cell.font, cell.alignment = font, align
    if fill:
        cell.fill = fill
    if border:
        cell.border = border
    if fmt:
        cell.number_format = fmt
    if span > 1 or rows > 1:
        ws.merge_cells(start_row=row, start_column=col, end_row=row + rows - 1, end_column=col + span - 1)
    return cell


def _link(cell, location):
    """같은 파일 안의 다른 셀로 가는 링크 (예: "'호기별 상세'!B12")."""
    cell.hyperlink = f'#{location}'
    cell.hyperlink.location, cell.hyperlink.target = location, None


def _widths(ws, widths):
    for index, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(index)].width = width


def _banner(ws, first_col, last_col, title, right='', note='', row=1):
    """시트 맨 위의 제목 줄 (큰 글씨 + 굵은 밑줄). 다음에 쓸 행 번호를 돌려준다."""
    split = last_col - 2 if right else last_col
    under = Border(bottom=RULE)
    _put(ws, row, first_col, title, _font(16, True), None, Alignment(horizontal='left', vertical='center'), under,
         span=split - first_col + 1)
    if right:
        _put(ws, row, split + 1, right, _font(11, color=MUTED), None, Alignment(horizontal='right', vertical='bottom'),
             under, span=last_col - split)
    ws.row_dimensions[row].height = 34
    if note:
        _put(ws, row + 1, first_col, note, F_NOTE, None, Alignment(horizontal='left', vertical='center'), None,
             span=last_col - first_col + 1)
        ws.row_dimensions[row + 1].height = 20
    return row + 3


def _section(ws, row, first_col, last_col, text, note=''):
    """구역 제목 (굵은 글씨 + 가는 밑줄)."""
    plain = Alignment(horizontal='left', vertical='bottom')
    under = Border(bottom=Side(style='thin', color=INK))
    width = last_col - first_col + 1
    if note and width >= 6:
        _put(ws, row, first_col, text, _font(11, True), None, plain, under, span=width // 2)
        _put(ws, row, first_col + width // 2, note, F_NOTE, None, Alignment(horizontal='right', vertical='bottom'),
             under, span=width - width // 2)
    else:
        _put(ws, row, first_col, text, _font(11, True), None, plain, under, span=width)
    ws.row_dimensions[row].height = 22
    return row + 1


def _header(ws, row, col, labels, spans=None, fill=FILL_HEAD, height=24):
    spans = spans or [1] * len(labels)
    for label, span in zip(labels, spans):
        _put(ws, row, col, label, F_HEAD, fill, CENTER, BORDER, span=span)
        col += span
    ws.row_dimensions[row].height = height
    return row + 1


def _row(ws, row, col, values, formats, kind='body', spans=None, fonts=None):
    """표의 한 줄. kind: body · total(소계) · grand(총계) · off(실적 없음)."""
    spans = spans or [1] * len(values)
    fill = {'total': FILL_TOTAL, 'grand': FILL_GRAND}.get(kind)
    base = F_BOLD if kind in ('total', 'grand') else F_OFF if kind == 'off' else F_BODY
    for index, (value, fmt, span) in enumerate(zip(values, formats, spans)):
        font = (fonts or {}).get(index, base)
        _put(ws, row, col, value, font, fill, RIGHT if fmt else LEFT, BORDER, fmt, span=span)
        col += span
    ws.row_dimensions[row].height = 19
    return row + 1


def _finish(ws, landscape=True, title_rows=None):
    ws.sheet_view.showGridLines = False
    ws.page_setup.orientation = 'landscape' if landscape else 'portrait'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top, ws.page_margins.bottom, ws.page_margins.footer = 0.5, 0.7, 0.3
    ws.print_options.horizontalCentered = True
    ws.oddFooter.center.text = '&P / &N'
    ws.oddFooter.center.size = 8
    if title_rows:
        ws.print_title_rows = title_rows


# ───────────────────────── 그래프 ─────────────────────────

def _text(size=900, bold=False, color=MUTED):
    props = CharacterProperties(sz=size, b=bold, solidFill=color,
                                latin=DrawingFont(typeface=FACE), ea=DrawingFont(typeface=FACE))
    return RichText(p=[Paragraph(pPr=ParagraphProperties(defRPr=props), endParaRPr=props)])


def _labels(fmt, position, size=800):
    labels = DataLabelList()
    labels.showVal, labels.showSerName, labels.showCatName = True, False, False
    labels.showLegendKey, labels.showPercent = False, False
    labels.numFmt, labels.position, labels.txPr = fmt, position, _text(size, color=INK)
    return labels


def _chart(kind, width, height, value_format, horizontal=False):
    chart = BarChart() if kind == 'bar' else LineChart()
    chart.width, chart.height, chart.legend = width, height, None
    chart.roundedCorners = False
    chart.graphical_properties = GraphicalProperties(ln=LineProperties(solidFill=LINE))
    chart.x_axis.delete = chart.y_axis.delete = False
    chart.x_axis.txPr = chart.y_axis.txPr = _text()
    chart.x_axis.graphicalProperties = GraphicalProperties(ln=LineProperties(solidFill='8C8C8C'))
    chart.y_axis.graphicalProperties = GraphicalProperties(ln=LineProperties(noFill=True))
    chart.y_axis.majorGridlines.spPr = GraphicalProperties(ln=LineProperties(solidFill='E4E4E4'))
    chart.y_axis.number_format = value_format
    chart.x_axis.tickLblPos = 'low'
    if kind == 'bar':
        chart.type = 'bar' if horizontal else 'col'
        chart.gapWidth = 60
    return chart


def _paint(series, color, kind, label_format=None, label_pos=None):
    if kind == 'bar':
        series.graphicalProperties.solidFill = color
        series.graphicalProperties.line.noFill = True
    else:
        series.smooth = False
        series.graphicalProperties.line.solidFill = color
        series.graphicalProperties.line.width = 22000
        series.marker.symbol, series.marker.size = 'circle', 5
        series.marker.graphicalProperties.solidFill = 'FFFFFF'
        series.marker.graphicalProperties.line.solidFill = color
    if label_format:
        series.dLbls = _labels(label_format, label_pos or ('outEnd' if kind == 'bar' else 't'))


# ───────────────────────── 자료 준비 ─────────────────────────

def _machines(ctx, modal):
    """호기별로 화면 표의 값과 눌렀을 때 보이는 값을 한데 묶는다."""
    rows = []
    for group in ctx['machine_analysis_table']:
        for m in group['machines']:
            data = modal.get(m['code'], {})
            produced, bad = data.get('total_qty', 0), data.get('defect_qty', 0)
            rows.append({
                **m, 'tonnage': group['tonnage'], 'produced': produced, 'good': data.get('good_qty', 0), 'bad': bad,
                'defect_rate': _ratio(bad, produced), 'items': data.get('items') or [],
                'losses': data.get('loss_detail') or [], 'loss_total': data.get('loss_total', 0),
                'active': m['shift_label'] != '-' or bool(data),
            })
    known = {row['code'] for row in rows}
    for code in sorted(code for code in modal if code not in known):   # 지금은 미사용 처리된 호기의 실적
        data = modal[code]
        rows.append({
            'code': code, 'tonnage': None, 'shift_label': '-', 'op_days': 0, 'operating': 0, 'base': 0,
            'mgmt_loss': 0, 'time_loss': 0, 'loss': data.get('loss_total', 0), 'util': 0, 'time_rate': 0,
            'time_rate_actual': 0, 'produced': data.get('total_qty', 0), 'good': data.get('good_qty', 0),
            'bad': data.get('defect_qty', 0), 'defect_rate': _ratio(data.get('defect_qty', 0), data.get('total_qty', 0)),
            'items': data.get('items') or [], 'losses': data.get('loss_detail') or [],
            'loss_total': data.get('loss_total', 0), 'active': True, 'retired': True,
        })
    return rows


def _ton(value):
    return f'{value}t' if value is not None else '미사용'


def _loss_kind(category):
    return MGMT if category in MOLDING_MGMT_LOSS else TIME


# ───────────────────────── 시트: 종합 ─────────────────────────

def _dashboard(wb, ws, ctx, machines, loads, period, made):
    ws.title = '종합'
    _widths(ws, [1.5] + [11] * 12 + [1.5])
    first, last = 2, 13
    ws.row_dimensions[1].height = 8
    row = _banner(ws, first, last, '성형 가동률 종합보고서', period, row=2)
    _put(ws, 3, first, f'생성 {made}   ·   실적 {ctx["total_records"]:,}건   ·   근무일수 설정 {ctx["setting_work_days"]}일 / '
                       f'실적 {ctx["actual_work_days"]}일   ·   보유 {ctx["total_machine_count"]}대',
         F_NOTE, None, LEFT, None, span=12)
    ws.row_dimensions[3].height = 20
    ws.row_dimensions[4].height = 8

    # 지표 카드
    monthly_util, monthly_time = loads('monthly_utilization'), loads('monthly_time_rate')

    def versus(now, series):
        if len(series) < 2 or not series[-2]:
            return '전월 실적 없음'
        gap = round(now - series[-2], 1)
        return f'전월 {series[-2]:.1f}%  ({"▲" if gap > 0 else "▼" if gap < 0 else "–"} {abs(gap):.1f}%p)'

    produced = sum(m['produced'] for m in machines)
    bad = sum(m['bad'] for m in machines)
    cards = [
        ('설비가동률', _pct(ctx['kpi_utilization']), PCT, versus(ctx['kpi_utilization'], monthly_util)),
        ('시간가동률 (설정 기준)', _pct(ctx['kpi_time_rate']), PCT, versus(ctx['kpi_time_rate'], monthly_time)),
        ('시간가동률 (실적 기준)', _pct(ctx['kpi_time_rate_actual']), PCT, f'실적이 있는 {ctx["actual_work_days"]}일 기준'),
        ('총 생산', produced, INT, f'불량 {bad:,}  ·  불량률 {_ratio(bad, produced):.2%}'),
        ('실유실 시간', ctx['kpi_actual_loss_hours'], '#,##0.0"h"', '유실 합계 − 계획정지'),
        ('계획정지 시간', ctx['kpi_planned_hours'], '#,##0.0"h"', f'유실 합계 {ctx["kpi_total_loss_hours"]:,.1f}h'),
    ]
    row = 5
    middle = Alignment(horizontal='center', vertical='center')
    for index, (label, value, fmt, note) in enumerate(cards):
        col = first + index * 2
        _put(ws, row, col, label, F_HEAD, FILL_HEAD, middle, BORDER, span=2)
        _put(ws, row + 1, col, value, _font(17, True), None, middle, Border(left=THIN, right=THIN), fmt, span=2)
        _put(ws, row + 2, col, note, _font(8.5, color=MUTED), None, middle,
             Border(left=THIN, right=THIN, bottom=THIN), span=2)
    ws.row_dimensions[row].height, ws.row_dimensions[row + 1].height, ws.row_dimensions[row + 2].height = 22, 36, 20
    ws.row_dimensions[row + 3].height = 10

    # 그래프용 값은 숨긴 시트에 둔다
    data = wb.create_sheet('_그래프자료')
    data.sheet_state = 'hidden'
    daily = list(zip(loads('daily_labels'), loads('daily_rates')))
    data.append(['일자', '설비가동률'])
    for label, rate in daily:
        data.append([label, _pct(rate)])
        data.cell(data.max_row, 2).number_format = PCT
    ton_col, month_col, loss_col = 4, 8, 12
    tons = [t for t in ctx['tonnage_table']]
    for offset, head in enumerate(['톤수', '설비가동률', '시간가동률(설정)']):
        data.cell(1, ton_col + offset, head)
    for index, t in enumerate(tons, 2):
        data.cell(index, ton_col, f'{t["tonnage"]}t')
        data.cell(index, ton_col + 1, _pct(t['util'])).number_format = PCT
        data.cell(index, ton_col + 2, _pct(t['time_rate'])).number_format = PCT
    months = list(zip(loads('monthly_labels'), monthly_util, monthly_time))
    for offset, head in enumerate(['월', '설비가동률', '시간가동률(설정)']):
        data.cell(1, month_col + offset, head)
    for index, (label, util, time_rate) in enumerate(months, 2):
        data.cell(index, month_col, label[2:])                          # 2026.05 → 26.05
        data.cell(index, month_col + 1, _pct(util)).number_format = PCT
        data.cell(index, month_col + 2, _pct(time_rate)).number_format = PCT
    losses = list(zip(loads('loss_labels'), loads('loss_values')))[:8]
    data.cell(1, loss_col, '사유')
    data.cell(1, loss_col + 1, '시간(h)')
    for index, (label, minutes) in enumerate(reversed(losses), 2):     # 가로 막대는 아래에서 위로 그려진다
        data.cell(index, loss_col, LOSS_LABEL.get(label, label))
        data.cell(index, loss_col + 1, round(minutes / 60, 1)).number_format = DEC

    def ref(col, count, with_head=True):
        return Reference(data, min_col=col, min_row=1 if with_head else 2, max_row=count + 1)

    # 일별 설비가동률 (가로 전체)
    row = _section(ws, 9, first, last, '일별 설비가동률', '실적이 있는 날만 표시')
    chart_rows = 12
    for r in range(row, row + chart_rows):
        ws.row_dimensions[r].height = 18
    if daily:
        chart = _chart('line', 28.5, 7.3, '0%')
        chart.add_data(ref(2, len(daily)), titles_from_data=True)
        chart.set_categories(ref(1, len(daily), False))
        chart.y_axis.scaling.max = 1
        _paint(chart.series[0], ACCENT, 'line', '0.0%')
        ws.add_chart(chart, f'B{row}')
    else:
        _put(ws, row, first, '이 달에는 실적이 없습니다.', F_NOTE, None, LEFT, None, span=12)
    row += chart_rows + 1

    # 작은 그래프 세 개
    keys = [('■ 설비가동률', ACCENT), ('■ 시간가동률 (설정)', '8C8C8C')]
    for index, (text, legend) in enumerate([('톤수별 가동률', keys), ('월별 추이 (최근 6개월)', keys),
                                            ('유실 사유별 시간 (상위 8개)', [('■ 유실 시간 (h)', ACCENT)])]):
        col = first + index * 4
        _section(ws, row, col, col + 3, text)
        for n, (label, color) in enumerate(legend):                     # 범례는 그래프 위에 글자로 둔다
            _put(ws, row + 1, col + n * 2, label, _font(8.5, True, color), None, LEFT, None, span=2)
    row += 2
    for r in range(row, row + chart_rows):
        ws.row_dimensions[r].height = 18
    if tons:
        chart = _chart('bar', 9.35, 7.3, '0%')
        chart.add_data(Reference(data, min_col=ton_col + 1, max_col=ton_col + 2, min_row=1, max_row=len(tons) + 1),
                       titles_from_data=True)
        chart.set_categories(ref(ton_col, len(tons), False))
        chart.y_axis.scaling.min, chart.y_axis.scaling.max = 0, 1
        few = len(tons) <= 6                                             # 톤수가 많으면 값은 아래 표에서 본다
        _paint(chart.series[0], ACCENT, 'bar', '0.0%' if few else None)
        _paint(chart.series[1], SOFT, 'bar', '0.0%' if few else None)
        ws.add_chart(chart, f'B{row}')
    if months:
        chart = _chart('line', 9.35, 7.3, '0%')
        chart.add_data(Reference(data, min_col=month_col + 1, max_col=month_col + 2, min_row=1, max_row=len(months) + 1),
                       titles_from_data=True)
        chart.set_categories(ref(month_col, len(months), False))
        chart.y_axis.scaling.max = 1
        _paint(chart.series[0], ACCENT, 'line', '0.0%')
        _paint(chart.series[1], '8C8C8C', 'line', '0.0%', 'b')
        ws.add_chart(chart, f'F{row}')
    if losses:
        chart = _chart('bar', 9.35, 7.3, '#,##0', horizontal=True)
        chart.add_data(ref(loss_col + 1, len(losses)), titles_from_data=True)
        chart.set_categories(ref(loss_col, len(losses), False))
        chart.y_axis.scaling.min = 0
        _paint(chart.series[0], ACCENT, 'bar', '#,##0.0')
        ws.add_chart(chart, f'J{row}')
    row += chart_rows + 1

    # 톤수별 현황
    row = _section(ws, row, first, last, '톤수별 현황')
    spans = [2] * 6
    row = _header(ws, row, first, ['톤수', '보유 대수', '가동 대수', '설비가동률', '시간가동률 (설정)', '시간가동률 (실적)'], spans)
    formats = [None, INT, INT, PCT, PCT, PCT]
    start = row
    for index, t in enumerate(tons):
        row = _row(ws, row, first, [f'{t["tonnage"]}t', t['count'], t['active_count'], _pct(t['util']),
                                    _pct(t['time_rate']), _pct(t['time_rate_actual'])], formats,
                   'body', spans)
    if tons:
        row = _row(ws, row, first, ['전체', ctx['total_machine_count'], sum(t['active_count'] for t in tons),
                                    _pct(ctx['kpi_utilization']), _pct(ctx['kpi_time_rate']),
                                    _pct(ctx['kpi_time_rate_actual'])], formats, 'grand', spans)
    row += 1

    # 눈여겨볼 호기
    ran = [m for m in machines if m.get('base')]
    blocks = [
        ('설비가동률이 낮은 호기', ['호기', '톤수', '가동률', '주 사유'], [None, None, PCT, None],
         [[m['code'], _ton(m['tonnage']), _pct(m['util']), _top_loss(m, short=True)]
          for m in sorted(ran, key=lambda m: m['util'])[:5]]),
        ('유실 시간이 큰 호기', ['호기', '톤수', '유실(분)', '주 사유'], [None, None, INT, None],
         [[m['code'], _ton(m['tonnage']), m['loss'], _top_loss(m, short=True)]
          for m in sorted(machines, key=lambda m: -m['loss'])[:5] if m['loss']]),
        ('불량률이 높은 호기', ['호기', '톤수', '불량률', '불량 수량'], [None, None, PCT2, INT],
         [[m['code'], _ton(m['tonnage']), m['defect_rate'], m['bad']]
          for m in sorted(machines, key=lambda m: -m['defect_rate'])[:5] if m['bad']]),
    ]
    top = _section(ws, row, first, last, '눈여겨볼 호기', '항목별 5대 · 자세한 내용은 [호기별 상세] 시트')
    for index, (text, heads, fmts, lines) in enumerate(blocks):
        col, r = first + index * 4, top
        _put(ws, r, col, text, _font(10, True), None, LEFT, None, span=4)
        ws.row_dimensions[r].height = 22
        r = _header(ws, r + 1, col, heads, fill=FILL_GROUP, height=22)
        for n in range(5):
            if n < len(lines):
                r = _row(ws, r, col, lines[n], fmts, 'body')
            else:
                r = _row(ws, r, col, ['–' if n == 0 and not lines else '', '', '', ''], [None] * 4, 'body')
    ws.print_area = f'A1:N{top + 7}'
    _finish(ws, landscape=False)
    ws.page_setup.fitToHeight = 1


def _top_loss(machine, short=False):
    if not machine['losses']:
        return ''
    loss = machine['losses'][0]
    label = LOSS_LABEL.get(loss['cat'], loss['cat'])
    return label if short else f'{label} ({loss["minutes"]:,}분)'


# ───────────────────────── 시트: 호기별 상세 ─────────────────────────

def _details(ws, machines, period):
    """호기마다 카드 한 장. 왼쪽은 생산 실적, 오른쪽은 유실 내역. {호기: 카드 첫 행}을 돌려준다."""
    _widths(ws, [1.5, 17, 34, 11, 11, 9, 9, 10, 2, 15, 11, 11, 10, 1.5])
    row = _banner(ws, 2, 13, '호기별 상세', period,
                  '화면에서 호기를 눌러야 보이던 생산 실적과 유실 내역을 호기마다 한 장씩 펼쳤습니다. 실적이 없는 호기는 빠져 있습니다.')
    anchors = {}
    for m in machines:
        if not m['items'] and not m['losses']:
            continue
        anchors[m['code']] = row
        title = f'{m["code"]}    {_ton(m["tonnage"])}  ·  {m["shift_label"] if m["shift_label"] != "-" else "근무 기록 없음"}  ·  가동 {m["op_days"] or 0}일'
        under = Border(top=RULE, bottom=THIN)
        _put(ws, row, 2, title, _font(12, True), FILL_GROUP, LEFT, under, span=10)
        back = _put(ws, row, 12, '↑ 호기별 종합', _font(9, color=ACCENT, underline='single'), FILL_GROUP, RIGHT, under, span=2)
        _link(back, "'호기별 종합'!A1")
        ws.cell(row, 9).border = under
        ws.cell(row, 9).fill = FILL_GROUP
        ws.row_dimensions[row].height = 26
        row += 1
        stats = [
            (2, 1, f'설비가동률  {m["util"]:.1f}%'),
            (3, 1, f'시간가동률  설정 {m["time_rate"]:.1f}%  ·  실적 {m["time_rate_actual"]:.1f}%'),
            (4, 2, f'총 생산  {m["produced"]:,}'),
            (6, 3, f'불량  {m["bad"]:,}  ({m["defect_rate"]:.2%})'),
            (9, 3, f'유실 합계  {m["loss"]:,}분'),
            (12, 2, f'관리 {m["mgmt_loss"]:,} · 시간 {m["time_loss"]:,}'),
        ]
        for col, span, text in stats:
            _put(ws, row, col, text, _font(9.5, color=MUTED), None, LEFT, None, span=span)
        ws.row_dimensions[row].height = 22
        row += 1

        _header(ws, row, 2, ['품번', '품명', '생산', '양품', '불량', '불량률', '비율'], fill=FILL_GROUP, height=21)
        _header(ws, row, 10, ['유실 사유', '구분', '시간(분)', '비율'], fill=FILL_GROUP, height=21)
        row += 1
        left, right = row, row
        for n, item in enumerate(m['items']):
            left = _row(ws, left, 2, [item['pno'], item.get('pname') or '', item['qty'], item['good_qty'], item['bad_qty'],
                                      _ratio(item['bad_qty'], item['qty']), _ratio(item['qty'], m['produced'])],
                        [None, None, INT, INT, INT, PCT2, PCT], 'body')
        if m['items']:
            left = _row(ws, left, 2, ['합계', f'{len(m["items"])}개 품번', m['produced'], m['good'], m['bad'],
                                      m['defect_rate'], 1], [None, None, INT, INT, INT, PCT2, PCT], 'total')
        else:
            left = _row(ws, left, 2, ['생산 실적 없음', '', '', '', '', '', ''], [None] * 7, 'off')
        for n, loss in enumerate(m['losses']):
            right = _row(ws, right, 10, [LOSS_LABEL.get(loss['cat'], loss['cat']), _loss_kind(loss['cat']), loss['minutes'],
                                         _ratio(loss['minutes'], m['loss_total'])], [None, None, INT, PCT],
                         'body')
        if m['losses']:
            right = _row(ws, right, 10, ['합계', '', m['loss_total'], 1], [None, None, INT, PCT], 'total')
        else:
            right = _row(ws, right, 10, ['유실 내역 없음', '', '', ''], [None] * 4, 'off')
        row = max(left, right) + 1
    if not anchors:
        _put(ws, row, 2, '이 달에는 호기별 실적이 없습니다.', F_NOTE, None, LEFT, None, span=12)
    ws.freeze_panes = 'A3'
    _finish(ws)
    return anchors


# ───────────────────────── 시트: 호기별 종합 ─────────────────────────

def _overview(ws, ctx, machines, anchors, period):
    _widths(ws, [8, 10, 10, 9, 11, 11, 11, 11, 11, 12, 12, 12, 12, 12, 10, 10, 22, 34])
    _banner(ws, 1, 18, '호기별 종합', period,
            '호기마다 한 줄입니다. 밑줄 친 호기 번호를 누르면 그 호기의 생산 실적·유실 내역으로 이동합니다. 빨간 글씨는 설비가동률이 가장 낮은 5대와 불량률이 가장 높은 5대. 시간 단위는 분.')
    row = 3
    col = 1
    for text, span in [('기본', 4), ('시간 (분)', 5), ('가동률', 3), ('생산', 4), ('주요 내용', 2)]:
        _put(ws, row, col, text, F_HEAD, FILL_GROUP, CENTER, BORDER, span=span)
        col += span
    ws.row_dimensions[row].height = 20
    head = row + 1
    row = _header(ws, head, 1, ['톤수', '호기', '근무구분', '가동일수', '근무시간', '가동시간', '관리LOSS', '시간LOSS', '유실합계',
                                '설비가동률', '시간가동률\n(설정)', '시간가동률\n(실적)', '총 생산', '양품', '불량', '불량률',
                                '최대 유실 사유', '주 생산 품번'], height=32)
    formats = [None, None, None, INT, INT, INT, INT, INT, INT, PCT, PCT, PCT, INT, INT, INT, PCT2, None, None]
    work_min = ctx['work_per_machine_min']
    by_code = {m['code']: m for m in machines}
    low_util = {m['code'] for m in sorted((m for m in machines if m.get('base')), key=lambda m: m['util'])[:5]}
    high_defect = {m['code'] for m in sorted((m for m in machines if m['bad']), key=lambda m: -m['defect_rate'])[:5]}
    start = row
    grand = [0] * 3
    for group in ctx['machine_analysis_table']:
        sums = [0, 0, 0]
        for n, raw in enumerate(group['machines']):
            m = by_code[raw['code']]
            sums = [sums[0] + m['produced'], sums[1] + m['good'], sums[2] + m['bad']]
            item = m['items'][0] if m['items'] else None
            top_item = f'{item["pno"]} {item.get("pname") or ""}'.strip() if item else ''
            kind = 'body' if m['active'] else 'off'
            linked = m['code'] in anchors
            row = _row(ws, row, 1, [_ton(m['tonnage']), m['code'], m['shift_label'], m['op_days'] or 0, work_min,
                                    m['operating'], m['mgmt_loss'], m['time_loss'], m['loss'], _pct(m['util']),
                                    _pct(m['time_rate']), _pct(m['time_rate_actual']), m['produced'], m['good'], m['bad'],
                                    m['defect_rate'], _top_loss(m), top_item], formats, kind,
                       fonts={**({1: F_LINK} if linked else {}), **({9: F_BAD} if m['code'] in low_util else {}),
                              **({15: F_BAD} if m['code'] in high_defect else {})})
            if linked:
                _link(ws.cell(row - 1, 2), f"'호기별 상세'!B{anchors[m['code']]}")
        total = group['total']
        grand = [grand[0] + sums[0], grand[1] + sums[1], grand[2] + sums[2]]
        row = _row(ws, row, 1, [_ton(group['tonnage']), '소계', f'가동 {group["active_cnt"]}대', '',
                                work_min * len(group['machines']), total['operating'], total['mgmt_loss'],
                                total['time_loss'], total['loss'], _pct(total['util']), _pct(total['time_rate']),
                                _pct(total['time_rate_actual']), sums[0], sums[1], sums[2], _ratio(sums[2], sums[0]),
                                '', ''], formats, 'total')
    if row > start:
        totals = [g['total'] for g in ctx['machine_analysis_table']]
        row = _row(ws, row, 1, ['전체', '합계', f'가동 {sum(g["active_cnt"] for g in ctx["machine_analysis_table"])}대', '',
                                work_min * ctx['total_machine_count'], sum(t['operating'] for t in totals),
                                sum(t['mgmt_loss'] for t in totals), sum(t['time_loss'] for t in totals),
                                sum(t['loss'] for t in totals), _pct(ctx['kpi_utilization']), _pct(ctx['kpi_time_rate']),
                                _pct(ctx['kpi_time_rate_actual']), grand[0], grand[1], grand[2],
                                _ratio(grand[2], grand[0]), '', ''], formats, 'grand')
    else:
        _put(ws, row, 1, '등록된 호기가 없습니다.', F_NOTE, None, LEFT, None, span=18)
    ws.freeze_panes = ws.cell(head + 1, 3)
    ws.auto_filter.ref = f'A{head}:R{max(head, row - 1)}'
    _finish(ws, title_rows=f'{head - 1}:{head}')


# ───────────────────────── 시트: 유실 분석 ─────────────────────────

def _loss_sheet(ws, ctx, loads, period):
    tonnages = [f'{t}t' for t in ctx['all_tonnages']]
    last = max(6, len(tonnages) + 3)
    _widths(ws, [18, 12] + [12] * (last - 2))
    row = _banner(ws, 1, last, '유실 분석', period,
                  f'{MGMT} = 계획정지 · 생산종료 · 식사/휴식 · 청소.  그 밖의 사유는 {TIME}이며 설비가동률을 낮춥니다.')

    labels, values = loads('loss_labels'), loads('loss_values')
    total = sum(values)
    row = _section(ws, row, 1, last, '유실 사유별 합계', '큰 순서')
    row = _header(ws, row, 1, ['사유', '구분', '시간(분)', '시간(h)', '비율', '누적 비율'])
    start, running = row, 0
    for n, (label, minutes) in enumerate(zip(labels, values)):
        running += minutes
        row = _row(ws, row, 1, [LOSS_LABEL.get(label, label), _loss_kind(label), minutes, round(minutes / 60, 1),
                                _ratio(minutes, total), _ratio(running, total)], [None, None, INT, DEC, PCT, PCT],
                   'body')
    if labels:
        row = _row(ws, row, 1, ['합계', '', total, round(total / 60, 1), 1, ''], [None, None, INT, DEC, PCT, None], 'grand')
    else:
        row = _row(ws, row, 1, ['유실 내역 없음', '', '', '', '', ''], [None] * 6, 'off')
    row += 1

    row = _section(ws, row, 1, last, '유실 사유 × 톤수 (분)', '색이 진할수록 큰 값')
    row = _header(ws, row, 1, ['사유', '구분'] + tonnages + ['합계'])
    start = row
    for n, line in enumerate(ctx['loss_category_table']):
        row = _row(ws, row, 1, [line['label'], MGMT if line['is_mgmt'] else TIME] + list(line['values']) + [line['total']],
                   [None, None] + [INT] * (len(tonnages) + 1), 'body')
    if tonnages and row > start:
        area = f'C{start}:{get_column_letter(len(tonnages) + 2)}{row - 1}'
        ws.conditional_formatting.add(area, ColorScaleRule(start_type='num', start_value=0, start_color='FFFFFF',
                                                           end_type='max', end_color='A9BDD3'))
        sums = [sum(line['values'][i] for line in ctx['loss_category_table']) for i in range(len(tonnages))]
        row = _row(ws, row, 1, ['합계', ''] + sums + [sum(sums)], [None, None] + [INT] * (len(tonnages) + 1), 'grand')
    row += 1

    row = _section(ws, row, 1, last, '톤수별 대당 평균 시간 (분)', '호기 1대 기준')
    row = _header(ws, row, 1, ['구분', ''] + tonnages + ['전체 평균'])
    lines, overall = ctx['tonnage_time_table'], ctx['tonnage_time_total']
    formats = [None, None] + [DEC] * (len(tonnages) + 1)
    row = _row(ws, row, 1, ['보유 대수', ''] + [r['count'] for r in lines] + [ctx['total_machine_count']],
               [None, None] + [INT] * (len(tonnages) + 1), 'body')
    for label, key in [('근무시간', 'work_h'), ('가동시간', 'operating_h'), (MGMT, 'mgmt_loss_h'), (TIME, 'time_loss_h'),
                       ('유실 합계', 'loss_h')]:
        row = _row(ws, row, 1, [label, ''] + [r[key] for r in lines] + [overall[key]], formats,
                   'total' if key == 'loss_h' else 'body')
    _finish(ws)


# ───────────────────────── 시트: 품목군 생산 ─────────────────────────

def _group_sheet(ws, loads, period):
    _widths(ws, [7, 24, 56, 15, 13])
    row = _banner(ws, 1, 5, '품목군별 생산', period, '생산수량 상위 10개 품목군과 품목군마다 많이 만든 품번 20개까지.  비율: 품목군 줄은 상위 10개 합계 대비, 품번 줄은 그 품목군 안에서의 비율.')
    row = _header(ws, row, 1, ['순위', '품목군', '품번 (품명)', '생산수량', '비율'])
    groups = list(zip(loads('ranking_top_labels'), loads('ranking_top_rates')))
    details = loads('group_detail_data')
    whole = sum(quantity for _, quantity in groups)
    start = row
    for rank, (group, quantity) in enumerate(groups, 1):
        items = details.get(group, [])
        row = _row(ws, row, 1, [rank, group, f'품번 {len(items)}개', quantity, _ratio(quantity, whole)],
                   [INT, None, None, INT, PCT], 'total')
        ws.cell(row - 1, 1).alignment = CENTER
        ws.cell(row - 1, 3).font = F_NOTE
        for n, item in enumerate(items):
            row = _row(ws, row, 1, ['', '', item['name'], item['qty'], _ratio(item['qty'], quantity)],
                       [None, None, None, INT, PCT], 'body')
    if not groups:
        _row(ws, row, 1, ['', '생산 실적 없음', '', '', ''], [None] * 5, 'off')
    ws.freeze_panes = ws.cell(start, 1)
    _finish(ws, landscape=False, title_rows=f'{start - 1}:{start - 1}')


# ───────────────────────── 시트: 원자료 ─────────────────────────

def _raw_sheets(wb, machines, period):
    ws = wb.create_sheet('원자료-생산')
    _widths(ws, [8, 10, 18, 44, 13, 13, 11, 10, 12])
    row = _banner(ws, 1, 9, '원자료 · 호기별 생산 실적', period, '필터와 피벗에 쓰기 좋게 한 줄에 한 건씩 늘어놓은 목록입니다.')
    head = row
    row = _header(ws, row, 1, ['톤수', '호기', '품번', '품명', '생산', '양품', '불량', '불량률', '호기 내 비율'])
    formats = [None, None, None, None, INT, INT, INT, PCT2, PCT]
    for m in machines:
        for n, item in enumerate(m['items']):
            row = _row(ws, row, 1, [_ton(m['tonnage']), m['code'], item['pno'], item.get('pname') or '', item['qty'],
                                    item['good_qty'], item['bad_qty'], _ratio(item['bad_qty'], item['qty']),
                                    _ratio(item['qty'], m['produced'])], formats, 'body')
        if m['items']:
            row = _row(ws, row, 1, [_ton(m['tonnage']), m['code'], '합계', '', m['produced'], m['good'], m['bad'],
                                    m['defect_rate'], 1], formats, 'total')
    ws.freeze_panes = ws.cell(head + 1, 3)
    ws.auto_filter.ref = f'A{head}:I{max(head, row - 1)}'
    _finish(ws, title_rows=f'{head}:{head}')

    ws = wb.create_sheet('원자료-유실')
    _widths(ws, [8, 10, 16, 12, 12, 12, 12])
    row = _banner(ws, 1, 7, '원자료 · 호기별 유실 내역', period, '필터와 피벗에 쓰기 좋게 한 줄에 한 건씩 늘어놓은 목록입니다.')
    head = row
    row = _header(ws, row, 1, ['톤수', '호기', '사유', '구분', '시간(분)', '시간(h)', '호기 내 비율'])
    formats = [None, None, None, None, INT, DEC, PCT]
    for m in machines:
        for n, loss in enumerate(m['losses']):
            row = _row(ws, row, 1, [_ton(m['tonnage']), m['code'], LOSS_LABEL.get(loss['cat'], loss['cat']),
                                    _loss_kind(loss['cat']), loss['minutes'], round(loss['minutes'] / 60, 1),
                                    _ratio(loss['minutes'], m['loss_total'])], formats, 'body')
        if m['losses']:
            row = _row(ws, row, 1, [_ton(m['tonnage']), m['code'], '합계', '', m['loss_total'],
                                    round(m['loss_total'] / 60, 1), 1], formats, 'total')
    ws.freeze_panes = ws.cell(head + 1, 3)
    ws.auto_filter.ref = f'A{head}:G{max(head, row - 1)}'
    _finish(ws, landscape=False, title_rows=f'{head}:{head}')


def build_molding_report(ctx):
    """화면 집계(ctx)로 종합보고서 통합문서를 만든다."""
    period = f'{ctx["year"]}년 {ctx["month"]}월'
    made = timezone.localtime().strftime('%Y-%m-%d %H:%M')
    loads = lambda key: json.loads(ctx[key])  # noqa: E731 — 차트용으로 직렬화된 값을 되돌린다
    machines = _machines(ctx, loads('machine_modal_data'))

    wb = Workbook()
    dashboard = wb.active
    overview = wb.create_sheet('호기별 종합')
    details = wb.create_sheet('호기별 상세')
    losses = wb.create_sheet('유실 분석')
    groups = wb.create_sheet('품목군 생산')

    anchors = _details(details, machines, period)
    _overview(overview, ctx, machines, anchors, period)
    _loss_sheet(losses, ctx, loads, period)
    _group_sheet(groups, loads, period)
    _raw_sheets(wb, machines, period)
    _dashboard(wb, dashboard, ctx, machines, loads, period, made)

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
