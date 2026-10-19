"""Shared ERP/WMS quantity precision; never silently truncate ERP balances."""
from decimal import Decimal, InvalidOperation


def stock_quantity(value):
    try:
        quantity = Decimal(str(value))
        if not quantity.is_finite() or abs(quantity) >= Decimal('10000000000000000'):
            raise ValueError('수량이 유효 범위를 벗어났습니다.')
        rounded = quantity.quantize(Decimal('0.0001'))
        if rounded != quantity:
            raise ValueError('수량은 소수점 4자리까지 지원합니다.')
        return rounded
    except InvalidOperation as exc:
        raise ValueError('수량 형식이 올바르지 않습니다.') from exc
