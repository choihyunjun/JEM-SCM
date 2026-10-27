"""원단위 중량 관리 화면의 숫자 표시: 큰 값은 정수, 작은 값만 소수, 0은 '–'."""
from django import template

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
