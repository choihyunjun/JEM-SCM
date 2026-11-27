"""시세·환율 조회 (생산기획팀): 환율 4종, 금·은, 동의 날짜별 값을 모아 한 화면에 보여준다.

출처
- 환율(달러·유로·위안·엔): 한국은행 경제통계 API, 통계표 731Y001 '주요국 통화의 대원화환율' 일별 값. 엔은 100엔당
- 동: 한국비철금속협회 누리집 'LME시세' 표 (현물, US$/톤). API가 없어 로그인 없이 열리는 표를 읽는다
- 금·은: 한국금거래소 '국제거래기준 시세'. 사이트가 자동 수집을 막아 두어(robots.txt) 사람이 입력하거나 표를 붙여 넣는다

화면은 저장된 값만 읽는다. 수집은 fetch_market_prices 명령(주기 실행)과 화면의 [지금 갱신]이 한다.
한 출처가 실패해도 나머지는 계속 수집하고, 출처별 마지막 결과를 MarketPriceSource에 남긴다.
"""
import logging
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from orders.decorators import wms_permission_required
from .models import MarketPrice, MarketPriceSource

logger = logging.getLogger(__name__)

ECOS_URL = 'https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/{count}/731Y001/D/{start}/{end}/{code}'
ECOS_ITEMS = {'USD': '0000001', 'JPY': '0000002', 'EUR': '0000003', 'CNY': '0000053'}  # 731Y001 항목코드
COPPER_URL = 'https://www.nonferrous.or.kr/stats/'
COPPER_ROWS_PER_PAGE = 20
MAX_COPPER_PAGES = 30
TIMEOUT = 15
USER_AGENT = 'Mozilla/5.0 (compatible; market-price-viewer)'
FOUR_PLACES = Decimal('0.0001')
STALE_DAYS = 5  # 기준일이 이만큼 지났으면 카드에 표시 (연휴에도 뜰 수 있다)

PERIODS = {'1m': ('1개월', 31), '3m': ('3개월', 92), '6m': ('6개월', 183), '1y': ('1년', 366)}
DEFAULT_PERIOD = '3m'

# 표·그래프·엑셀의 칸 순서: (키, 항목, 값이 든 칸, 이름, 단위, 소수 자리)
SERIES = [
    ('USD', 'USD', 'value', '달러', '원', 2),
    ('EUR', 'EUR', 'value', '유로', '원', 2),
    ('CNY', 'CNY', 'value', '위안', '원', 2),
    ('JPY', 'JPY', 'value', '엔 (100엔)', '원', 2),
    ('GOLD', 'GOLD', 'value', '금 국제가', 'USD/T.oz', 2),
    ('GOLD_KRW', 'GOLD', 'value_krw', '금 국내기준가', '원/g', 0),
    ('SILVER', 'SILVER', 'value', '은 국제가', 'USD/T.oz', 2),
    ('SILVER_KRW', 'SILVER', 'value_krw', '은 국내기준가', '원/g', 0),
    ('COPPER', 'COPPER', 'value', '동 LME 현물', 'USD/톤', 1),
]
# 지표 카드: (항목, 이름, 단위, 소수 자리, 출처 이름)
CARDS = [
    ('USD', '미국 달러', '원', 2, '한국은행'),
    ('EUR', '유로', '원', 2, '한국은행'),
    ('CNY', '중국 위안', '원', 2, '한국은행'),
    ('JPY', '일본 엔 (100엔)', '원', 2, '한국은행'),
    ('GOLD', '금 국제가', 'USD/T.oz', 2, '한국금거래소'),
    ('SILVER', '은 국제가', 'USD/T.oz', 2, '한국금거래소'),
    ('COPPER', '동 LME 현물', 'USD/톤', 1, '비철금속협회'),
]
MANUAL_ITEMS = {'GOLD': '금', 'SILVER': '은'}
GROUP_STARTS = {'GOLD', 'SILVER', 'COPPER'}  # 표에서 이 칸 앞에 구분선

ROW_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.S | re.I)
CELL_RE = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.S | re.I)
TAG_RE = re.compile(r'<[^>]+>')
COPPER_DATE_RE = re.compile(r'(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})')
PASTE_LINE_RE = re.compile(r'^(\d{4})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})\.?\s+(.+)$')
# 한국금거래소 표는 칸이 붙은 채 복사된다: '2026.11.2641.6941.8241.821,808' = 날짜·Bid·Ask·국제가·국내기준가.
# 국제가 칸이 Ask와 같은 값이라, 같은 숫자가 두 번 이어지는 자리로 칸을 나눈다
_NUM = r'\d{1,3}(?:,\d{3})*(?:\.\d{1,3})?'
GLUED_LINE_RE = re.compile(rf'^(\d{{4}})\.(\d{{2}})\.(\d{{2}})(?P<bid>{_NUM})(?P<ask>{_NUM})(?P=ask)(?P<krw>{_NUM})$')


class MarketPriceError(Exception):
    """화면에 그대로 보여줄 수 있는 실패 사유. 주소나 인증키를 담지 않는다."""


def _decimal(text):
    """'11,965.0' → Decimal. 숫자가 아니면 None."""
    cleaned = re.sub(r'[,\s$₩원]', '', str(text if text is not None else ''))
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _get(url, what, params=None):
    """외부 조회. requests 예외 글에는 인증키가 든 주소가 섞이므로 그대로 내보내지 않는다."""
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT, headers={'User-Agent': USER_AGENT})
    except requests.exceptions.Timeout:
        raise MarketPriceError(f'{what} 응답 시간 초과') from None
    except requests.exceptions.RequestException:
        raise MarketPriceError(f'{what} 접속 실패') from None
    if resp.status_code != 200:
        raise MarketPriceError(f'{what} 응답 오류 (HTTP {resp.status_code})')
    return resp


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------
def fetch_exchange(item, start, end, key=None, count=1000):
    """한국은행 일별 환율 {날짜: (값, None)}. 그 기간에 고시가 없으면 빈 dict."""
    key = key or getattr(settings, 'ECOS_API_KEY', '')
    if not key:
        raise MarketPriceError('한국은행 인증키가 없습니다 (서버 .env의 ECOS_API_KEY)')
    url = ECOS_URL.format(key=quote(key, safe=''), count=count, start=f'{start:%Y%m%d}', end=f'{end:%Y%m%d}',
                          code=ECOS_ITEMS[item])
    try:
        data = _get(url, '한국은행').json()
    except ValueError:
        raise MarketPriceError('한국은행 응답을 읽지 못했습니다') from None
    if not isinstance(data, dict) or 'StatisticSearch' not in data:
        result = (data.get('RESULT') if isinstance(data, dict) else None) or {}
        if result.get('CODE') == 'INFO-200':  # 해당하는 데이터가 없습니다 (연휴 등)
            return {}
        reason = str(result.get('MESSAGE') or result.get('CODE') or '알 수 없는 응답').split('\n')[0].strip()
        raise MarketPriceError(f'한국은행 오류: {reason[:120]}')
    rows = {}
    for row in data['StatisticSearch'].get('row') or []:
        value = _decimal(row.get('DATA_VALUE'))
        try:
            day = datetime.strptime(str(row.get('TIME')), '%Y%m%d').date()
        except ValueError:
            continue
        if value is not None and value > 0:
            rows[day] = (value, None)
    return rows


def parse_copper(html):
    """협회 'LME시세' 표에서 구리(Cu) 현물가 {날짜: (값, None)}."""
    column, rows = None, {}
    for tr in ROW_RE.findall(html):
        cells = [TAG_RE.sub('', c).replace('&nbsp;', ' ').strip() for c in CELL_RE.findall(tr)]
        if 'Cu' in cells:
            # 품목 이름 줄에는 '일자' 칸이 없다(위 줄에서 두 줄을 차지) → 값 줄에서는 한 칸 뒤
            column = cells.index('Cu') + (0 if '일자' in cells else 1)
            continue
        found = COPPER_DATE_RE.search(cells[0]) if cells else None
        if not found or column is None or len(cells) <= column:
            continue
        value = _decimal(cells[column])
        if value is None or value <= 0:
            continue
        try:
            rows[date(*map(int, found.groups()))] = (value, None)
        except ValueError:
            continue
    if not rows:
        raise MarketPriceError('한국비철금속협회 화면 형식이 바뀌어 표를 읽지 못했습니다')
    return rows


def fetch_copper(pages=1):
    """협회 표를 최근 쪽부터 pages쪽 읽는다 (한 쪽에 20일)."""
    rows = {}
    for page in range(1, min(max(1, pages), MAX_COPPER_PAGES) + 1):
        if page > 1:
            time.sleep(0.5)  # 남의 누리집이다. 여러 쪽을 읽을 때는 천천히
        body = _get(COPPER_URL, '한국비철금속협회', params={'act': 'sub3', 'page': page}).content
        try:
            html = body.decode('utf-8')
        except UnicodeDecodeError:
            html = body.decode('cp949', 'replace')
        try:
            rows.update(parse_copper(html))
        except MarketPriceError:
            if page == 1:
                raise
            break  # 마지막 쪽을 넘어갔다
    return rows


def save_prices(item, rows, source, user=None):
    """rows {날짜: (값, 국내기준가)}를 저장하고, 새로 들어오거나 바뀐 건수를 돌려준다. 같은 날짜는 덮어쓴다."""
    if not rows:
        return 0
    changed = 0
    with transaction.atomic():
        existing = {p.price_date: p for p in
                    MarketPrice.objects.filter(item=item, price_date__range=(min(rows), max(rows)))}
        for day, (value, value_krw) in rows.items():
            value = value.quantize(FOUR_PLACES)
            value_krw = value_krw.quantize(FOUR_PLACES) if value_krw is not None else None
            current = existing.get(day)
            if current is None:
                MarketPrice.objects.create(item=item, price_date=day, value=value, value_krw=value_krw,
                                           source=source, updated_by=user)
            elif (current.value, current.value_krw, current.source) != (value, value_krw, source):
                current.value, current.value_krw, current.source, current.updated_by = value, value_krw, source, user
                current.save()
            else:
                continue
            changed += 1
    return changed


def collect(days=10, pages=1):
    """자동 출처(환율·동)를 수집한다. 한 출처가 실패해도 다음 출처는 계속한다. [(출처, 성공 여부, 결과 글)]"""
    end = timezone.localdate()
    start = end - timedelta(days=days)
    jobs = (
        ('BOK', lambda: sum(save_prices(item, fetch_exchange(item, start, end), 'BOK') for item in ECOS_ITEMS)),
        ('KNFA', lambda: save_prices('COPPER', fetch_copper(pages), 'KNFA')),
    )
    results = []
    for source, job in jobs:
        try:
            ok, message = True, f'새 값 {job()}건'
        except MarketPriceError as exc:
            ok, message = False, str(exc)
        except Exception:
            logger.exception('시세 수집 실패: %s', source)
            ok, message = False, '수집 중 오류가 발생했습니다'
        now = timezone.now()
        MarketPriceSource.objects.update_or_create(source=source, defaults={
            'ok': ok, 'message': message[:200], 'checked_at': now, **({'succeeded_at': now} if ok else {})})
        results.append((source, ok, message))
    return results


# ---------------------------------------------------------------------------
# 금·은 직접 입력
# ---------------------------------------------------------------------------
def _pasted_line(line):
    """한 줄 → (날짜 숫자 3개, 국제가, 국내기준가). 읽지 못하면 None."""
    found = PASTE_LINE_RE.match(line)
    if found:  # 칸이 띄어진 줄
        numbers = [_decimal(token) for token in found.group(4).split()]
        if None in numbers or len(numbers) == 3 or not numbers:  # 숫자 3개는 어느 칸인지 알 수 없다
            return None
        if len(numbers) >= 4:
            return found.groups()[:3], numbers[2], numbers[3]
        return found.groups()[:3], numbers[0], numbers[1] if len(numbers) == 2 else None
    found = GLUED_LINE_RE.match(line)
    if found:  # 칸이 붙은 줄. 잘못 나뉜 줄을 거르려고 Bid가 Ask 바로 아래인지 본다
        bid, ask, krw = (_decimal(found.group(name)) for name in ('bid', 'ask', 'krw'))
        if ask * Decimal('0.9') <= bid <= ask:
            return found.groups()[:3], ask, krw
    return None


def parse_pasted(text, today=None):
    """한국금거래소 '국제거래기준 시세' 표에서 복사한 줄을 읽는다.

    한 줄 형식: 표에서 복사한 그대로(칸이 붙어 있거나 탭으로 띄어진 '날짜 Bid Ask 국제가 국내기준가'),
    또는 직접 쓴 '날짜 국제가 국내기준가' / '날짜 국제가'. 숫자로 시작하지 않는 줄(표 머리글)은 건너뛴다.
    ({날짜: (국제가, 국내기준가)}, 읽지 못한 줄 목록)
    """
    today = today or timezone.localdate()
    rows, bad = {}, []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or not line[0].isdigit():
            continue
        parsed = _pasted_line(line)
        try:
            day = date(*map(int, parsed[0])) if parsed else None
        except ValueError:
            day = None
        if (day is None or day > today or parsed[1] <= 0 or (parsed[2] is not None and parsed[2] <= 0)):
            bad.append(line[:60])
            continue
        rows[day] = (parsed[1], parsed[2])
    return rows, bad


def _save_manual(request):
    """날짜 하나에 금·은 값을 직접 입력."""
    try:
        day = datetime.strptime(request.POST.get('price_date', ''), '%Y-%m-%d').date()
    except ValueError:
        messages.error(request, '날짜를 확인해 주세요.')
        return
    if day > timezone.localdate():
        messages.error(request, '오늘 이후 날짜는 입력할 수 없습니다.')
        return
    saved, rejected = [], False
    for item, name in MANUAL_ITEMS.items():
        raw, raw_krw = (request.POST.get(f'{item.lower()}{suffix}', '').strip() for suffix in ('', '_krw'))
        if not raw and not raw_krw:
            continue
        value = _decimal(raw)
        value_krw = _decimal(raw_krw) if raw_krw else None
        if value is None or value <= 0 or (raw_krw and (value_krw is None or value_krw <= 0)):
            messages.error(request, f'{name}: 국제가는 꼭 넣고, 값은 0보다 큰 숫자로 입력해 주세요.')
            rejected = True
            continue
        save_prices(item, {day: (value, value_krw)}, 'MANUAL', request.user)
        saved.append(name)
    if saved:
        messages.success(request, f"{day:%Y-%m-%d} {'·'.join(saved)} 값을 저장했습니다.")
    elif not rejected:
        messages.error(request, '입력한 값이 없습니다.')


def _save_pasted(request):
    item = request.POST.get('item')
    if item not in MANUAL_ITEMS:
        messages.error(request, '금·은 중 하나를 골라 주세요.')
        return
    rows, bad = parse_pasted(request.POST.get('rows', ''))
    if rows:
        changed = save_prices(item, rows, 'MANUAL', request.user)
        messages.success(request, f'{MANUAL_ITEMS[item]} {len(rows)}일 치를 읽어 {changed}건을 저장했습니다.')
    if bad:
        messages.error(request, f"읽지 못한 줄 {len(bad)}개: {' / '.join(bad[:3])}{' …' if len(bad) > 3 else ''}")
    if not rows and not bad:
        messages.error(request, '붙여 넣은 내용이 없습니다.')


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
def _fmt(value, digits):
    return '–' if value is None else f'{value:,.{digits}f}'


def _period(request):
    period = request.GET.get('period') or request.POST.get('period')
    return period if period in PERIODS else DEFAULT_PERIOD


def _card(item, label, unit, digits, source_name, today):
    latest = list(MarketPrice.objects.filter(item=item).order_by('-price_date')[:2])
    card = {'item': item, 'label': label, 'unit': unit, 'source': source_name, 'value': '–', 'empty': not latest}
    if not latest:
        return card
    current = latest[0]
    card.update(value=_fmt(current.value, digits), date=current.price_date,
                age=(today - current.price_date).days, manual=current.source == 'MANUAL')
    card['stale'] = card['age'] >= STALE_DAYS
    if current.value_krw is not None:
        card['krw'] = _fmt(current.value_krw, 0)
    if len(latest) == 2 and latest[1].value:
        diff = current.value - latest[1].value
        card['delta'] = {
            'direction': 'up' if diff > 0 else 'down' if diff < 0 else 'flat',
            'amount': _fmt(abs(diff), digits),
            'percent': f'{diff / latest[1].value * 100:+.2f}%',
            'since': latest[1].price_date,
        }
    return card


def _source_status():
    """화면 아래 수집 상태 줄. 자동 출처 2곳 + 직접 입력."""
    saved = {s.source: s for s in MarketPriceSource.objects.all()}
    lines = []
    for source, name in (('BOK', '환율 · 한국은행'), ('KNFA', '동 · 한국비철금속협회')):
        status = saved.get(source)
        failed = bool(status and not status.ok)
        lines.append({'name': name, 'status': status, 'failed': failed, 'when': status and status.succeeded_at,
                      'done': '수집', 'never': '수집 실패' if failed else '수집 전'})
    manual = MarketPrice.objects.filter(source='MANUAL').order_by('-updated_at').first()
    lines.append({'name': '금·은 · 직접 입력', 'failed': False, 'when': manual and manual.updated_at,
                  'done': '저장', 'never': '입력 전'})
    return lines


def build_table(start, end):
    """기간 안의 날짜별 값. (날짜 내림차순 행 [{date, cells}], 그래프용 자료)"""
    by_date = {}
    for price in MarketPrice.objects.filter(price_date__range=(start, end)):
        by_date.setdefault(price.price_date, {})[price.item] = price
    rows, chart = [], {key: [] for key, *_ in SERIES}
    for day in sorted(by_date):
        cells = []
        for key, item, field, _label, _unit, digits in SERIES:
            price = by_date[day].get(item)
            value = getattr(price, field) if price else None
            cells.append({'text': _fmt(value, digits), 'value': value, 'sep': key in GROUP_STARTS})
            if value is not None:
                chart[key].append([day.isoformat(), float(value)])
        rows.append({'date': day, 'weekday': '월화수목금토일'[day.weekday()], 'cells': cells})
    rows.reverse()
    return rows, chart


@wms_permission_required('can_wms_stock_view')
@require_http_methods(['GET', 'POST'])
def market_price(request):
    period = _period(request)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'refresh':
            names = {'BOK': '환율', 'KNFA': '동'}
            for source, ok, message in collect():
                (messages.success if ok else messages.error)(request, f'{names[source]}: {message}')
        elif action == 'manual':
            _save_manual(request)
        elif action == 'paste':
            _save_pasted(request)
        return redirect(f"{reverse('material:market_price')}?period={period}")

    today = timezone.localdate()
    rows, chart = build_table(today - timedelta(days=PERIODS[period][1]), today)
    sources = _source_status()
    return render(request, 'material/market_price.html', {
        'cards': [_card(*card, today) for card in CARDS],
        'series': [{'key': key, 'label': label, 'unit': unit, 'digits': digits, 'sep': key in GROUP_STARTS}
                   for key, _item, _field, label, unit, digits in SERIES],
        'rows': rows, 'chart': chart,
        'period': period, 'periods': [(key, label) for key, (label, _days) in PERIODS.items()],
        'period_label': PERIODS[period][0],
        'sources': sources, 'has_failure': any(s['failed'] for s in sources),
        'today': today, 'manual_items': MANUAL_ITEMS,
    })


@wms_permission_required('can_wms_stock_view')
def market_price_excel(request):
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    period = _period(request)
    today = timezone.localdate()
    start = today - timedelta(days=PERIODS[period][1])
    rows, _chart = build_table(start, today)

    font, thin = '맑은 고딕', Side(style='thin', color='D9D9D9')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '시세·환율'
    last = len(SERIES) + 1
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last)
    ws['A1'] = f'시세·환율 ({start:%Y-%m-%d} ~ {today:%Y-%m-%d})'
    ws['A1'].font = Font(name=font, size=14, bold=True)
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    ws['A2'] = '환율: 한국은행 매매기준율(엔은 100엔당) · 금·은: 한국금거래소 국제거래기준 시세 · 동: 한국비철금속협회 LME 현물가'
    ws['A2'].font = Font(name=font, size=9, color='808080')
    ws['A2'].alignment = Alignment(horizontal='center')
    ws.row_dimensions[1].height = 26
    headers = ['날짜'] + [f'{label}\n({unit})' for _key, _item, _field, label, unit, _digits in SERIES]
    for col, text in enumerate(headers, 1):
        cell = ws.cell(3, col, text)
        cell.fill = PatternFill('solid', start_color='C00000', end_color='C00000')
        cell.font = Font(name=font, size=10, bold=True, color='FFFFFF')
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border
        ws.column_dimensions[get_column_letter(col)].width = 13 if col == 1 else 15
    ws.row_dimensions[3].height = 34
    for r, row in enumerate(rows, 4):
        cell = ws.cell(r, 1, row['date'])
        cell.number_format, cell.font, cell.border = 'yyyy-mm-dd', Font(name=font, size=10), border
        cell.alignment = Alignment(horizontal='center')
        for col, (data, (*_meta, digits)) in enumerate(zip(row['cells'], SERIES), 2):
            cell = ws.cell(r, col, float(data['value']) if data['value'] is not None else None)
            cell.number_format = '#,##0' + ('.' + '0' * digits if digits else '')
            cell.font, cell.border = Font(name=font, size=10), border
    ws.freeze_panes = 'B4'

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    filename = f'시세·환율 ({start:%Y%m%d}~{today:%Y%m%d}).xlsx'
    response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(filename)}"
    wb.save(response)
    return response
