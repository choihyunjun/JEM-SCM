"""원단위 중량 관리 화면의 숫자 표시: 큰 값은 정수, 작은 값만 소수, 0은 '–'."""
from django import template
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def kg(value):
    if value is None or abs(value) < 0.005:
        return '–'
    if abs(value) >= 100:
        return f'{value:,.0f}'
    return f'{value:,.1f}' if abs(value) >= 1 else f'{value:.2f}'


@register.filter
def grams(value):
    if value is None:
        return '–'
    return f'{value:,.0f}' if abs(value) >= 100 else f'{value:.2f}'


@register.filter
def ea(value):
    return '–' if not value else f'{value:,.0f}'


@register.filter
def pct(value, digits=1):
    return '–' if value is None else f'{value:.{int(digits)}f}'


DASH = mark_safe('<span class="dash">–</span>')


@register.filter
def txt(value):
    """성형 마스터 표: 빈 글자는 흐린 '–'."""
    return conditional_escape(value) if value else DASH


@register.filter
def num(value):
    """성형 마스터 표: 소수 뒤 0 제거, 천 단위 쉼표, 0·빈값은 '–'."""
    from decimal import Decimal, InvalidOperation
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return DASH
    if d == 0:
        return DASH
    text = format(d.normalize(), 'f')
    whole, _, frac = text.partition('.')
    sign = '-' if whole.startswith('-') else ''
    whole = f'{int(whole.lstrip("-")):,}'
    return f'{sign}{whole}.{frac}' if frac else f'{sign}{whole}'
