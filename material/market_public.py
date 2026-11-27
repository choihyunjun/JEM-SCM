"""로그인 없이 보는 시세·환율 화면 (/market/<열쇠>/).

SCM에 들어오지 않고도 휴대폰·PC에서 바로 볼 수 있게 한 보기 전용 화면이다. 값과 수집은 market_price.py 것을 그대로 쓴다.
- 주소에 든 열쇠(MARKET_PUBLIC_KEY)가 맞아야 열린다. 틀리거나 열쇠가 설정되지 않았으면 없는 화면(404)으로 답한다.
  금거래소·비철금속협회 값을 누구나 보는 곳에 다시 내보내지 않으려는 것이다 (링크를 받은 사람만 본다)
- 할 수 있는 일은 조회와 [지금 갱신]뿐이다. 값 입력·엑셀은 SCM 화면에 있다
- 검색 엔진에 잡히지 않게 하고, 바깥 사이트로 이 주소가 새어 나가지 않게 한다
"""
import secrets

from django.conf import settings
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .market_price import _period, page_context, refresh_now


@require_http_methods(['GET', 'POST'])
def market_public(request, key):
    expected = getattr(settings, 'MARKET_PUBLIC_KEY', '')
    if not expected or not secrets.compare_digest(key.encode(), expected.encode()):
        raise Http404
    period = _period(request)
    if request.method == 'POST':
        refresh_now(request)
        response = redirect(f"{reverse('market_public', args=[key])}?period={period}")
    else:
        response = render(request, 'material/market_public.html', {**page_context(period), 'key': key})
    response['X-Robots-Tag'] = 'noindex, nofollow'
    response['Referrer-Policy'] = 'same-origin'
    response['Cache-Control'] = 'no-store'
    return response
