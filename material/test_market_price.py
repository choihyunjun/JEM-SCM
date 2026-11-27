"""시세·환율 조회: 한국은행 응답 해석, 협회 표 읽기, 붙여넣기 해석, 수집 실패 처리, 화면·엑셀."""
import re
from contextlib import ExitStack
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import openpyxl
import requests
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .market_price import (
    CARDS, MarketPriceError, _card, collect, collect_metals, fetch_exchange, fetch_metal, month_average,
    parse_copper, parse_pasted, save_prices,
)
from .models import MarketPrice, MarketPriceSource

# 한국비철금속협회 'LME시세' 표의 실제 모양 (2026-11-27 확인): 머리글 두 줄, 날짜는 '2026. 11. 26'
COPPER_HTML = '''
<table summary="이 테이블은 LME시세 정보를 제공합니다."> <caption>LME시세</caption>
<tbody> <tr> <th rowspan="2" scope="col">일자</th> <th colspan="6" scope="col">품목</th> </tr>
<tr> <th>Cu</th> <th>Al</th> <th>Zn</th> <th>Pb</th> <th>Ni</th> <th>Sn</th> </tr>
<tr> <td class="sp">2026. 11. 26</td> <td>11,965.0</td> <td>2,980.0</td> <td>3,646.0</td> <td>1,844.0</td> <td>15,665.0</td> <td>38,425.0</td> </tr>
<tr> <td class="sp">2026. 11. 25</td> <td>11,703.0</td> <td>2,849.0</td> <td>3,520.5</td> <td>1,792.0</td> <td>15,035.0</td> <td>38,200.0</td> </tr>
</tbody></table>'''


class FakeResponse:
    def __init__(self, payload=None, text='', status=200):
        self._payload, self.status_code, self.content = payload, status, text.encode('utf-8')

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


def ecos(*rows):
    return {'StatisticSearch': {'list_total_count': len(rows),
                                'row': [{'STAT_CODE': '731Y001', 'TIME': day, 'DATA_VALUE': value} for day, value in rows]}}


def fake_get(url, params=None, **kwargs):
    """환율은 통화마다 같은 2일 치, 동은 협회 표."""
    if 'ecos.bok.or.kr' in url:
        return FakeResponse(ecos(('20261126', '1335.5'), ('20261127', '1337.2')))
    return FakeResponse(text=COPPER_HTML)


# 한국금거래소 '국제거래기준 시세' 화면이 받는 응답의 실제 모양 (2026-11-27 확인)
GOLD_ROWS = {'list': [
    {'date': '2026-11-26', 'exchangeRate': 1344.6200000000001, 'ask': 3575.65, 'domesticPrice': 154577.0,
     'bid': 3572.85, 'domesticPriceDon': 579663.75},
    {'date': '2026-11-25', 'exchangeRate': 1344.16, 'ask': 3536.35, 'domesticPrice': 152826.0,
     'bid': 3533.75, 'domesticPriceDon': 573097.5},
]}
SILVER_ROWS = {'list': [
    {'date': '2026-11-26', 'exchangeRate': 1344.69, 'ask': 41.82, 'domesticPrice': 1808.0, 'bid': 41.69,
     'domesticPriceDon': 6780.0},
]}


def fake_post(url, json=None, **kwargs):
    return FakeResponse(GOLD_ROWS if json['type'] == 'Au' else SILVER_ROWS)


def sources(get=fake_get, post=fake_post):
    """외부 세 곳을 가짜로 바꾼다. 금·은 두 번 사이의 쉬는 시간도 없앤다."""
    stack = ExitStack()
    stack.enter_context(patch('material.market_price.time.sleep'))
    stack.enter_context(patch('material.market_price.requests.get', side_effect=get))
    # 금거래소는 화면을 먼저 열어 받은 접속 정보(세션)로 값을 묻는다
    session = stack.enter_context(patch('material.market_price.requests.Session')).return_value
    session.get.return_value = FakeResponse(text='<html>국제거래기준 시세</html>')
    session.post.side_effect = post
    stack.page, stack.post = session.get, session.post
    return stack


class ParseTests(TestCase):
    def test_copper_column_is_found_by_its_header(self):
        self.assertEqual(parse_copper(COPPER_HTML), {date(2026, 11, 26): (Decimal('11965.0'), None),
                                                     date(2026, 11, 25): (Decimal('11703.0'), None)})

    def test_copper_single_header_row_and_moved_column(self):
        html = ('<table><tr><th>일자</th><th>Al</th><th>Cu</th></tr>'
                '<tr><td>2026. 11. 26</td><td>2,980.0</td><td>11,965.0</td></tr></table>')
        self.assertEqual(parse_copper(html), {date(2026, 11, 26): (Decimal('11965.0'), None)})

    def test_copper_changed_page_is_an_error_not_an_empty_result(self):
        with self.assertRaisesMessage(MarketPriceError, '화면 형식'):
            parse_copper('<html><body>점검 중입니다</body></html>')

    def test_pasted_rows_copied_from_the_gold_exchange_table(self):
        # 2026-11-27 한국금거래소 표를 끌어서 복사한 그대로: 칸 사이가 붙어 있고, 소수 끝 0은 빠져 있다
        gold = ('고시날짜BidAsk국제가 (USD/T.oz)국내기준가 (₩/g)\n'
                '2026.11.263,572.853,575.653,575.65154,577\n'
                '2026.11.233,613.993,616.63,616.6156,409\n'
                '2026.11.193,736.43,739.23,739.2160,062\n')
        rows, bad = parse_pasted(gold, today=date(2026, 11, 27))
        self.assertEqual((rows, bad), ({date(2026, 11, 26): (Decimal('3575.65'), Decimal('154577')),
                                        date(2026, 11, 23): (Decimal('3616.6'), Decimal('156409')),
                                        date(2026, 11, 19): (Decimal('3739.2'), Decimal('160062'))}, []))
        silver = '2026.11.2641.6941.8241.821,808\n2026.11.1349.9550.0850.082,154\n'
        self.assertEqual(parse_pasted(silver, today=date(2026, 11, 27))[0],
                         {date(2026, 11, 26): (Decimal('41.82'), Decimal('1808')),
                          date(2026, 11, 13): (Decimal('50.08'), Decimal('2154'))})

    def test_pasted_rows_typed_or_tab_separated(self):
        text = ('2026.11.26\t3,572.85\t3,575.65\t3,575.65\t154,577\n'   # 탭으로 띄어진 표: Bid, Ask, 국제가, 국내기준가
                '2026-11-25 3,536.35 152,826\n'                          # 국제가, 국내기준가
                '2026.11.24 3,621.54\n'                                  # 국제가만
                '\n'
                '2026.11.23 1 2 3\n'                                     # 숫자 3개는 어느 칸인지 모른다
                '2030.01.01 3,000 150,000\n'                             # 앞날
                '2026.11.223,575\n')                                     # 붙은 줄인데 칸이 모자란다
        rows, bad = parse_pasted(text, today=date(2026, 11, 27))
        self.assertEqual(rows, {date(2026, 11, 26): (Decimal('3575.65'), Decimal('154577')),
                                date(2026, 11, 25): (Decimal('3536.35'), Decimal('152826')),
                                date(2026, 11, 24): (Decimal('3621.54'), None)})
        self.assertEqual(len(bad), 3)


@override_settings(ECOS_API_KEY='test-key')
class ExchangeTests(TestCase):
    def test_rows_are_read_and_a_period_without_rates_is_empty(self):
        with patch('material.market_price.requests.get', return_value=FakeResponse(ecos(('20261127', '1337.2')))) as get:
            self.assertEqual(fetch_exchange('USD', date(2026, 11, 20), date(2026, 11, 27)),
                             {date(2026, 11, 27): (Decimal('1337.2'), None)})
        self.assertIn('/731Y001/D/20261120/20261127/0000001', get.call_args.args[0])
        no_data = {'RESULT': {'CODE': 'INFO-200', 'MESSAGE': '해당하는 데이터가 없습니다.'}}
        with patch('material.market_price.requests.get', return_value=FakeResponse(no_data)):
            self.assertEqual(fetch_exchange('EUR', date(2026, 9, 24), date(2026, 9, 26)), {})

    def test_error_answer_is_reported_with_its_reason(self):
        bad_key = {'RESULT': {'CODE': 'INFO-100', 'MESSAGE': '인증키가 유효하지 않습니다. 인증키를 확인하십시오!'}}
        with patch('material.market_price.requests.get', return_value=FakeResponse(bad_key)):
            with self.assertRaisesMessage(MarketPriceError, '인증키가 유효하지 않습니다'):
                fetch_exchange('USD', date(2026, 11, 20), date(2026, 11, 27))
        with patch('material.market_price.requests.get', return_value=FakeResponse(status=503)):
            with self.assertRaisesMessage(MarketPriceError, 'HTTP 503'):
                fetch_exchange('USD', date(2026, 11, 20), date(2026, 11, 27))

    @override_settings(ECOS_API_KEY='')
    def test_missing_key_is_reported_without_calling_out(self):
        with patch('material.market_price.requests.get') as get:
            with self.assertRaisesMessage(MarketPriceError, 'ECOS_API_KEY'):
                fetch_exchange('USD', date(2026, 11, 20), date(2026, 11, 27))
        get.assert_not_called()

    def test_connection_failure_does_not_leak_the_key(self):
        # requests 예외 글에는 인증키가 든 주소가 들어 있다. 화면·상태 표에 그대로 나가면 안 된다
        error = requests.exceptions.ConnectionError('Max retries exceeded with url: /api/StatisticSearch/test-key/json')
        with sources(get=error):
            results = collect()
        self.assertEqual(results[0][:2], ('BOK', False))
        self.assertNotIn('test-key', results[0][2])
        self.assertNotIn('test-key', MarketPriceSource.objects.get(source='BOK').message)


class GoldExchangeTests(TestCase):
    def test_request_looks_like_the_page_and_rows_are_read(self):
        with sources() as mocks:
            rows = fetch_metal('SILVER', days=10, today=date(2026, 11, 27))
            fetch_metal('GOLD', days=400, today=date(2026, 11, 27))
        self.assertEqual(rows, {date(2026, 11, 26): (Decimal('41.82'), Decimal('1808.0'))})
        week, year = (call.kwargs['json'] for call in mocks.post.call_args_list)
        self.assertEqual(week, {'srchDt': '1W', 'type': 'Ag', 'dataDateStart': '2026.11.20', 'dataDateEnd': '2026.11.27'})
        self.assertEqual(year, {'srchDt': '1Y', 'type': 'Au', 'dataDateStart': '2025.11.27', 'dataDateEnd': '2026.11.27'})

    def test_page_is_opened_once_before_both_metals_and_the_program_is_not_disguised(self):
        # 2026-11-27 확인: 화면을 열지 않고 값만 물으면 403. 화면을 연 접속으로 물으면 프로그램 이름 그대로도 받아 준다
        with sources() as mocks:
            self.assertEqual(collect_metals(), 3)
        self.assertEqual(mocks.page.call_count, 1)
        self.assertIn('/price/global', mocks.page.call_args.args[0])
        self.assertEqual(mocks.post.call_count, 2)
        for call in mocks.post.call_args_list:
            self.assertIn('market-price-viewer', call.kwargs['headers']['User-Agent'])

    def test_page_that_does_not_open_stops_the_collection(self):
        with sources() as mocks:
            mocks.page.return_value = FakeResponse(status=503)
            with self.assertRaisesMessage(MarketPriceError, 'HTTP 503'):
                collect_metals()
            mocks.post.assert_not_called()

    def test_changed_or_empty_answer_is_an_error(self):
        for payload in ({'list': []}, {'error': 'forbidden'}, [], {'list': [{'date': '2026-11-26', 'ask': None}]}):
            with sources(post=lambda url, payload=payload, **kwargs: FakeResponse(payload)):
                with self.assertRaisesMessage(MarketPriceError, '시세를 찾지 못했습니다'):
                    fetch_metal('GOLD')
        with sources(post=lambda url, **kwargs: FakeResponse(text='<html>차단</html>')):
            with self.assertRaisesMessage(MarketPriceError, '읽지 못했습니다'):
                fetch_metal('GOLD')


@override_settings(ECOS_API_KEY='test-key')
class CollectTests(TestCase):
    def test_saves_every_source_and_a_second_run_adds_nothing(self):
        with sources():
            self.assertEqual(collect(), [('BOK', True, '새 값 8건'), ('KNFA', True, '새 값 2건'), ('KGX', True, '새 값 3건')])
            self.assertEqual(collect(force=True),
                             [('BOK', True, '새 값 0건'), ('KNFA', True, '새 값 0건'), ('KGX', True, '한 시간 안에 이미 받음')])
        self.assertEqual(MarketPrice.objects.count(), 13)
        self.assertEqual(MarketPrice.objects.get(item='JPY', price_date=date(2026, 11, 27)).value, Decimal('1337.2'))
        self.assertEqual(MarketPrice.objects.get(item='COPPER', price_date=date(2026, 11, 26)).source, 'KNFA')
        gold = MarketPrice.objects.get(item='GOLD', price_date=date(2026, 11, 26))
        self.assertEqual((gold.value, gold.value_krw, gold.source), (Decimal('3575.65'), Decimal('154577'), 'KGX'))
        self.assertTrue(all(s.ok and s.succeeded_at for s in MarketPriceSource.objects.all()))

    def test_gold_exchange_is_asked_once_a_day_and_at_most_hourly_by_hand(self):
        # 금거래소는 자동 접근을 막아 둔 주소다. 주기 실행은 하루 한 번, 사람이 [지금 갱신]을 눌러도 한 시간에 한 번만 묻는다
        kgx = MarketPriceSource.objects.filter(source='KGX')
        with sources() as mocks:
            collect()
            self.assertEqual(mocks.post.call_count, 2)                       # 금, 은
            self.assertEqual(collect()[-1], ('KGX', True, '오늘 이미 받음 (하루 한 번)'))
            self.assertEqual(collect(force=True)[-1], ('KGX', True, '한 시간 안에 이미 받음'))
            self.assertEqual(mocks.post.call_count, 2)
            kgx.update(succeeded_at=timezone.now() - timedelta(hours=2))
            collect(force=True)                                              # 두 시간 뒤의 [지금 갱신]
            self.assertEqual(mocks.post.call_count, 4)
            kgx.update(succeeded_at=timezone.now() - timedelta(days=1))
            collect()                                                        # 다음 날 주기 실행
            self.assertEqual(mocks.post.call_count, 6)

    def test_gold_exchange_failure_is_retried_later_the_same_day(self):
        blocked = lambda url, **kwargs: FakeResponse(status=403)  # noqa: E731
        with sources(post=blocked) as mocks:
            self.assertEqual(collect()[-1], ('KGX', False, '한국금거래소 응답 오류 (HTTP 403)'))
            collect()
            self.assertEqual(mocks.post.call_count, 2)                       # 실패하면 금에서 멈추고, 다음 실행 때 다시 묻는다
        self.assertEqual(set(MarketPrice.objects.values_list('item', flat=True)), {'USD', 'EUR', 'CNY', 'JPY', 'COPPER'})

    @override_settings(ECOS_API_KEY='')
    def test_one_failing_source_does_not_stop_the_other(self):
        with sources():
            results = collect()
        self.assertEqual([(source, ok) for source, ok, _ in results], [('BOK', False), ('KNFA', True), ('KGX', True)])
        self.assertEqual(set(MarketPrice.objects.values_list('item', flat=True)), {'COPPER', 'GOLD', 'SILVER'})
        bok = MarketPriceSource.objects.get(source='BOK')
        self.assertEqual((bok.ok, bok.succeeded_at), (False, None))

    def test_a_failure_keeps_the_time_of_the_last_success(self):
        with sources():
            collect()
        succeeded = MarketPriceSource.objects.get(source='KNFA').succeeded_at
        with sources(get=lambda url, **kwargs: FakeResponse(text='<html>점검</html>')):
            collect()
        knfa = MarketPriceSource.objects.get(source='KNFA')
        self.assertEqual((knfa.ok, knfa.succeeded_at), (False, succeeded))
        self.assertEqual(MarketPrice.objects.filter(item='COPPER').count(), 2)  # 받아 둔 값은 그대로

    def test_same_date_is_overwritten_and_counted_only_when_changed(self):
        day = date(2026, 11, 26)
        self.assertEqual(save_prices('GOLD', {day: (Decimal('3575.65'), Decimal('154577'))}, 'MANUAL'), 1)
        self.assertEqual(save_prices('GOLD', {day: (Decimal('3575.65'), Decimal('154577'))}, 'MANUAL'), 0)
        self.assertEqual(save_prices('GOLD', {day: (Decimal('3580'), Decimal('154577'))}, 'MANUAL'), 1)
        self.assertEqual(list(MarketPrice.objects.values_list('value', flat=True)), [Decimal('3580')])


class MonthlyAverageTests(TestCase):
    """카드 오른쪽 칸: 이번 달 평균과 전월 평균 대비."""

    def card(self, item, today):
        return _card(*next(c for c in CARDS if c[0] == item), today)

    def test_this_month_average_and_change_from_last_month(self):
        save_prices('USD', {date(2026, 10, 29): (Decimal('1290'), None), date(2026, 10, 30): (Decimal('1310'), None),
                            date(2026, 11, 2): (Decimal('1320'), None), date(2026, 11, 26): (Decimal('1335.5'), None),
                            date(2026, 11, 27): (Decimal('1342.7'), None)}, 'BOK')
        card = self.card('USD', date(2026, 11, 27))
        self.assertEqual((card['month'], card['average'], card['last_month'], card['last_average']),
                         (11, '1,332.73', 10, '1,300.00'))                      # (1320 + 1335.5 + 1342.7) / 3
        self.assertEqual(card['average_delta'], {'direction': 'up', 'amount': '32.73', 'percent': '+2.52%'})
        self.assertEqual((card['value'], card['delta']['amount']), ('1,342.70', '7.20'))   # 왼쪽 칸은 그대로

    def test_weekend_copies_from_the_gold_exchange_are_not_counted(self):
        # 금거래소는 토·일에도 금요일 값을 그대로 올린다. 넣으면 금요일 값이 세 번 들어간다
        save_prices('GOLD', {date(2026, 11, 19): (Decimal('3700'), None), date(2026, 11, 20): (Decimal('3600'), None),
                             date(2026, 11, 21): (Decimal('3600'), None), date(2026, 11, 22): (Decimal('3600'), None)},
                    'KGX')
        self.assertEqual(month_average('GOLD', 2026, 11), Decimal('3650'))
        self.assertIsNone(month_average('GOLD', 2026, 12))

    def test_months_without_values_and_the_year_boundary(self):
        save_prices('COPPER', {date(2026, 12, 30): (Decimal('12000'), None)}, 'KNFA')
        january = self.card('COPPER', date(2027, 1, 4))                          # 새 달 값이 아직 없다
        self.assertEqual((january['month'], january['average'], january['average_delta']), (1, '–', None))
        self.assertEqual((january['last_month'], january['last_average']), (12, '12,000.0'))
        empty = self.card('SILVER', date(2026, 11, 27))                          # 아무 값도 없는 항목
        self.assertEqual((empty['empty'], empty['average'], empty['last_average'], empty['average_delta']),
                         (True, '–', None, None))

    def test_page_shows_both_halves(self):
        today = timezone.localdate()
        save_prices('USD', {today: (Decimal('1342.7'), None)}, 'BOK')
        self.client.force_login(User.objects.create(username='avg', is_superuser=True))
        html = self.client.get(reverse('material:market_price')).content.decode()
        self.assertEqual(html.count(f'{today.month}월 평균<span class="u">'), 7)
        self.assertEqual(html.count('<div class="avg">'), 7)


class ViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='planner', is_superuser=True)
        self.client.force_login(self.user)
        self.url = reverse('material:market_price')
        self.today = timezone.localdate()

    def test_page_shows_latest_values_with_change_in_the_agreed_order(self):
        yesterday = self.today - timedelta(days=1)
        save_prices('USD', {yesterday: (Decimal('1325'), None), self.today: (Decimal('1337.2'), None)}, 'BOK')
        save_prices('GOLD', {yesterday: (Decimal('3575.65'), Decimal('154577'))}, 'MANUAL')
        save_prices('COPPER', {self.today - timedelta(days=9): (Decimal('11965'), None)}, 'KNFA')
        html = self.client.get(self.url).content.decode()
        self.assertIn('1,337.20', html)
        self.assertIn('▲ 12.20', html)
        self.assertIn('(+0.92%)', html)
        self.assertNotIn('국내기준가', html)                # 금·은 국내기준가는 받아 두기만 하고 화면에는 내지 않는다
        self.assertEqual(re.findall(r'data-key="(\w+)"', html), ['USD', 'JPY', 'CNY', 'EUR', 'GOLD', 'SILVER', 'COPPER'])
        self.assertEqual(re.findall(r'<div class="l name">([^<]+)<', html),
                         ['미국 달러', '일본 엔 (100엔)', '중국 위안', '유로', '금 국제가', '은 국제가', '동 LME 현물'])
        self.assertIn('(9일 전)</span></span>', html)                      # 오래된 값은 카드에 표시
        self.assertIn(f'<td class="first"><span class="dl">{self.today:%Y-%m-%d}</span>', html)
        self.assertNotIn('자동 수집 확인', html)

    def test_empty_page_and_failed_source_are_shown(self):
        MarketPriceSource.objects.create(source='BOK', ok=False, message='한국은행 접속 실패', checked_at=timezone.now())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('자동 수집 확인', html)
        self.assertIn('한국은행 접속 실패', html)
        self.assertIn('이 기간에 저장된 값이 없습니다', html)

    def test_manual_input_saves_gold_and_silver(self):
        response = self.client.post(self.url, {'action': 'manual', 'price_date': f'{self.today:%Y-%m-%d}',
                                               'gold': '3,575.65', 'silver': '41.20'})
        self.assertRedirects(response, f'{self.url}?period=3m')
        gold, silver = MarketPrice.objects.get(item='GOLD'), MarketPrice.objects.get(item='SILVER')
        self.assertEqual((gold.value, gold.value_krw, gold.source, gold.updated_by), (Decimal('3575.65'), None, 'MANUAL', self.user))
        self.assertEqual((silver.value, silver.value_krw), (Decimal('41.2'), None))

    def test_manual_input_rejects_future_dates_and_bad_numbers(self):
        tomorrow = self.today + timedelta(days=1)
        self.client.post(self.url, {'action': 'manual', 'price_date': f'{tomorrow:%Y-%m-%d}', 'gold': '3575'})
        self.client.post(self.url, {'action': 'manual', 'price_date': f'{self.today:%Y-%m-%d}', 'gold': '삼천'})
        self.client.post(self.url, {'action': 'manual', 'price_date': f'{self.today:%Y-%m-%d}', 'gold': '-5'})
        self.assertFalse(MarketPrice.objects.exists())

    def test_pasted_rows_are_saved_for_the_chosen_metal(self):
        rows = '2026.11.26\t40.10\t40.30\t40.30\t1,745\n2026.11.2539.840401,730\n1,730 엉뚱한 줄'
        response = self.client.post(f'{self.url}?period=1m', {'action': 'paste', 'item': 'SILVER', 'rows': rows})
        self.assertRedirects(response, f'{self.url}?period=1m')
        self.assertEqual(list(MarketPrice.objects.filter(item='SILVER').values_list('value', 'value_krw')),
                         [(Decimal('40.3'), Decimal('1745')), (Decimal('40'), Decimal('1730'))])
        self.client.post(self.url, {'action': 'paste', 'item': 'COPPER', 'rows': rows})  # 자동 수집 항목은 받지 않는다
        self.assertFalse(MarketPrice.objects.filter(item='COPPER').exists())

    def test_refresh_button_collects_and_reports_each_source(self):
        result = [('BOK', True, '새 값 4건'), ('KNFA', False, '접속 실패'), ('KGX', True, '새 값 2건')]
        with patch('material.market_price.collect', return_value=result) as run:
            response = self.client.post(self.url, {'action': 'refresh'}, follow=True)
        run.assert_called_once_with(force=True)
        self.assertEqual([str(m) for m in response.context['messages']],
                         ['환율: 새 값 4건', '동: 접속 실패', '금·은: 새 값 2건'])

    def test_refresh_right_after_a_collection_does_not_ask_the_sources_again(self):
        MarketPriceSource.objects.create(source='BOK', ok=True, message='새 값 4건', checked_at=timezone.now(),
                                         succeeded_at=timezone.now())
        with patch('material.market_price.collect') as run:
            response = self.client.post(self.url, {'action': 'refresh'}, follow=True)
        run.assert_not_called()
        self.assertIn('5분 뒤에 다시 받을 수 있습니다', [str(m) for m in response.context['messages']][0])

    def test_excel_has_one_row_per_date(self):
        save_prices('USD', {self.today: (Decimal('1337.2'), None)}, 'BOK')
        save_prices('GOLD', {self.today: (Decimal('3575.65'), Decimal('154577'))}, 'MANUAL')
        response = self.client.get(reverse('material:market_price_excel'), {'period': '1m'})
        ws = openpyxl.load_workbook(BytesIO(response.content))['시세·환율']
        self.assertEqual([c.value.split('\n')[0] for c in ws[3]],
                         ['날짜', '달러', '엔 (100엔)', '위안', '유로', '금 국제가', '은 국제가', '동 LME 현물'])
        self.assertEqual((ws['A4'].value.date(), ws['B4'].value, ws['C4'].value, ws['F4'].value, ws['G4'].value),
                         (self.today, 1337.2, None, 3575.65, None))

    def test_login_and_permission_are_required(self):
        self.client.logout()
        self.assertRedirects(self.client.get(self.url), reverse('login'), fetch_redirect_response=False)
        self.client.force_login(User.objects.create(username='vendor'))  # 권한 없는 계정 (협력사 등)
        response = self.client.post(self.url, {'action': 'manual', 'price_date': f'{self.today:%Y-%m-%d}', 'gold': '3575'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(MarketPrice.objects.exists())


@override_settings(MARKET_PUBLIC_KEY='k3y-for-test')
class PublicPageTests(TestCase):
    """로그인 없이 보는 화면 (/market/<열쇠>/): 링크를 받은 사람만, 보기와 갱신만."""

    def setUp(self):
        self.url = reverse('market_public', args=['k3y-for-test'])
        self.today = timezone.localdate()
        save_prices('USD', {self.today: (Decimal('1337.2'), None)}, 'BOK')
        save_prices('GOLD', {self.today: (Decimal('3575.65'), Decimal('154577'))}, 'KGX')

    def test_opens_without_login_and_only_shows(self):
        response = self.client.get(self.url, {'period': '1m'})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('1,337.20', html)
        self.assertIn('3,575.65', html)
        self.assertNotIn('국내기준가', html)
        self.assertEqual(html.count('class="mk-stat"'), 7)
        self.assertIn('지금 갱신', html)
        for scm_only in ('mkManual', '엑셀 다운로드', 'sb-sidenav', '/wms/'):   # 값 입력·엑셀·SCM 메뉴는 없다
            self.assertNotIn(scm_only, html)
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')         # 검색에 잡히지 않게
        self.assertEqual(response['Referrer-Policy'], 'same-origin')            # 주소 열쇠가 바깥으로 새지 않게

    def test_wrong_or_unset_key_looks_like_a_missing_page(self):
        self.assertEqual(self.client.get(reverse('market_public', args=['wrong'])).status_code, 404)
        self.assertEqual(self.client.post(reverse('market_public', args=['wrong'])).status_code, 404)
        with override_settings(MARKET_PUBLIC_KEY=''):
            self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_refresh_works_without_login_and_keeps_the_period(self):
        result = [('BOK', True, '새 값 4건'), ('KNFA', True, '새 값 1건'), ('KGX', True, '한 시간 안에 이미 받음')]
        with patch('material.market_price.collect', return_value=result) as run:
            response = self.client.post(self.url, {'period': '1m'}, follow=True)
        self.assertEqual(response.redirect_chain, [(f'{self.url}?period=1m', 302)])
        run.assert_called_once_with(force=True)
        html = response.content.decode()
        self.assertIn('환율: 새 값 4건', html)
        self.assertIn('금·은: 한 시간 안에 이미 받음', html)

    def test_scm_page_is_unchanged_and_still_needs_login(self):
        scm = reverse('material:market_price')
        self.assertRedirects(self.client.get(scm), reverse('login'), fetch_redirect_response=False)
        self.client.force_login(User.objects.create(username='planner2', is_superuser=True))
        html = self.client.get(scm).content.decode()
        for part in ('mkManual', '엑셀 다운로드', '1,337.20', 'id="mkChart"', "getElementById('mkChartData')"):
            self.assertIn(part, html)
