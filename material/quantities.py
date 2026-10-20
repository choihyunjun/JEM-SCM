"""Shared ERP/WMS quantity precision; never silently truncate ERP balances."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

STEP = Decimal('0.0001')


def stock_quantity(value):
    try:
        quantity = Decimal(str(value))
        if not quantity.is_finite() or abs(quantity) >= Decimal('10000000000000000'):
            raise ValueError('수량이 유효 범위를 벗어났습니다.')
        rounded = quantity.quantize(STEP)
        if rounded != quantity:
            raise ValueError('수량은 소수점 4자리까지 지원합니다.')
        return rounded
    except InvalidOperation as exc:
        raise ValueError('수량 형식이 올바르지 않습니다.') from exc


def stock_total(value):
    """SQLite sums decimal columns as REAL; drop binary noise below 4 places."""
    return Decimal(str(value or 0)).quantize(STEP, rounding=ROUND_HALF_UP)


def input_quantity(value, positive=True):
    """Form/JSON input: up to 4 decimal places, rejected rather than truncated."""
    quantity = stock_quantity(str(value).strip().replace(',', ''))
    if positive and quantity <= 0:
        raise ValueError('수량은 0보다 커야 합니다.')
    return quantity


def erp_quantity(value):
    """ERP payloads keep the integer form for whole quantities."""
    quantity = stock_quantity(value)
    return int(quantity) if quantity == quantity.to_integral_value() else float(quantity)
