"""시세·환율 조회: 한국은행 응답 해석, 협회 표 읽기, 붙여넣기 해석, 수집 실패 처리, 화면·엑셀."""
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

from .market_price import MarketPriceError, collect, fetch_exchange, parse_copper, parse_pasted, save_prices
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
        with patch('material.market_price.requests.get', side_effect=error):
            results = collect()
        self.assertEqual(results[0][:2], ('BOK', False))
        self.assertNotIn('test-key', results[0][2])
        self.assertNotIn('test-key', MarketPriceSource.objects.get(source='BOK').message)


@override_settings(ECOS_API_KEY='test-key')
class CollectTests(TestCase):
    def test_saves_every_source_and_a_second_run_adds_nothing(self):
        with patch('material.market_price.requests.get', side_effect=fake_get):
            self.assertEqual(collect(), [('BOK', True, '새 값 8건'), ('KNFA', True, '새 값 2건')])
            self.assertEqual(collect(), [('BOK', True, '새 값 0건'), ('KNFA', True, '새 값 0건')])
        self.assertEqual(MarketPrice.objects.count(), 10)
        self.assertEqual(MarketPrice.objects.get(item='JPY', price_date=date(2026, 11, 27)).value, Decimal('1337.2'))
        self.assertEqual(MarketPrice.objects.get(item='COPPER', price_date=date(2026, 11, 26)).source, 'KNFA')
        self.assertTrue(all(s.ok and s.succeeded_at for s in MarketPriceSource.objects.all()))

    @override_settings(ECOS_API_KEY='')
    def test_one_failing_source_does_not_stop_the_other(self):
        with patch('material.market_price.requests.get', side_effect=fake_get):
            results = collect()
        self.assertEqual([(source, ok) for source, ok, _ in results], [('BOK', False), ('KNFA', True)])
        self.assertEqual(set(MarketPrice.objects.values_list('item', flat=True)), {'COPPER'})
        bok = MarketPriceSource.objects.get(source='BOK')
        self.assertEqual((bok.ok, bok.succeeded_at), (False, None))

    def test_a_failure_keeps_the_time_of_the_last_success(self):
        with patch('material.market_price.requests.get', side_effect=fake_get):
            collect()
        succeeded = MarketPriceSource.objects.get(source='KNFA').succeeded_at
        with patch('material.market_price.requests.get', return_value=FakeResponse(text='<html>점검</html>')):
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


class ViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='planner', is_superuser=True)
        self.client.force_login(self.user)
        self.url = reverse('material:market_price')
        self.today = timezone.localdate()

    def test_page_shows_latest_values_with_change_and_both_gold_prices(self):
        yesterday = self.today - timedelta(days=1)
        save_prices('USD', {yesterday: (Decimal('1325'), None), self.today: (Decimal('1337.2'), None)}, 'BOK')
        save_prices('GOLD', {yesterday: (Decimal('3575.65'), Decimal('154577'))}, 'MANUAL')
        save_prices('COPPER', {self.today - timedelta(days=9): (Decimal('11965'), None)}, 'KNFA')
        html = self.client.get(self.url).content.decode()
        self.assertIn('1,337.20', html)
        self.assertIn('▲ 12.20', html)
        self.assertIn('(+0.92%)', html)
        self.assertIn('국내기준가 <b>154,577</b> 원/g', html)
        self.assertIn('(9일 전)', html)                      # 오래된 값은 카드에 표시
        self.assertIn(f'<td class="first">{self.today:%Y-%m-%d}', html)
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
                                               'gold': '3,575.65', 'gold_krw': '154,577', 'silver': '41.20'})
        self.assertRedirects(response, f'{self.url}?period=3m')
        gold, silver = MarketPrice.objects.get(item='GOLD'), MarketPrice.objects.get(item='SILVER')
        self.assertEqual((gold.value, gold.value_krw, gold.source, gold.updated_by), (Decimal('3575.65'), 154577, 'MANUAL', self.user))
        self.assertEqual((silver.value, silver.value_krw), (Decimal('41.2'), None))

    def test_manual_input_rejects_future_dates_and_a_domestic_price_alone(self):
        tomorrow = self.today + timedelta(days=1)
        self.client.post(self.url, {'action': 'manual', 'price_date': f'{tomorrow:%Y-%m-%d}', 'gold': '3575'})
        self.client.post(self.url, {'action': 'manual', 'price_date': f'{self.today:%Y-%m-%d}', 'gold_krw': '154577'})
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
        with patch('material.market_price.collect', return_value=[('BOK', True, '새 값 4건'), ('KNFA', False, '접속 실패')]) as run:
            response = self.client.post(self.url, {'action': 'refresh'}, follow=True)
        run.assert_called_once_with()
        self.assertEqual([str(m) for m in response.context['messages']], ['환율: 새 값 4건', '동: 접속 실패'])

    def test_excel_has_one_row_per_date(self):
        save_prices('USD', {self.today: (Decimal('1337.2'), None)}, 'BOK')
        save_prices('GOLD', {self.today: (Decimal('3575.65'), Decimal('154577'))}, 'MANUAL')
        response = self.client.get(reverse('material:market_price_excel'), {'period': '1m'})
        ws = openpyxl.load_workbook(BytesIO(response.content))['시세·환율']
        self.assertEqual([c.value.split('\n')[0] for c in ws[3]][:6], ['날짜', '달러', '유로', '위안', '엔 (100엔)', '금 국제가'])
        self.assertEqual((ws['A4'].value.date(), ws['B4'].value, ws['C4'].value, ws['F4'].value, ws['G4'].value),
                         (self.today, 1337.2, None, 3575.65, 154577))

    def test_login_and_permission_are_required(self):
        self.client.logout()
        self.assertRedirects(self.client.get(self.url), reverse('login'), fetch_redirect_response=False)
        self.client.force_login(User.objects.create(username='vendor'))  # 권한 없는 계정 (협력사 등)
        response = self.client.post(self.url, {'action': 'manual', 'price_date': f'{self.today:%Y-%m-%d}', 'gold': '3575'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(MarketPrice.objects.exists())
