"""Stock mutations shared by ERP reconciliation and WMS actions."""
from django.db.models import F
from django.db.models.functions import Round

from .models import MaterialStock
from .quantities import stock_quantity


def quantity_change(delta, field='quantity'):
    """F() update for a 4-place decimal quantity.

    SQLite evaluates decimal arithmetic as REAL (0.3 - 0.1 = 0.19999...);
    rounding keeps the stored value equal to the decimal the code reads back.
    """
    return Round(F(field) + stock_quantity(delta), 4)


def deduct_stock(stock, quantity):
    """Conditional debit: a stale read must never overdraw a LOT.

    Call inside the transaction that also writes the counterpart and history.
    """
    if not stock or quantity <= 0:
        raise ValueError('차감할 재고 또는 수량이 올바르지 않습니다.')
    if MaterialStock.objects.filter(pk=stock.pk, quantity__gte=quantity).update(
        quantity=quantity_change(-stock_quantity(quantity)),
    ) != 1:
        raise ValueError('LOT 재고가 부족하거나 다른 작업에서 변경되었습니다. 재고를 다시 조회해 주세요.')


def unassigned_stock(warehouse, part):
    # An undated production batch is not the unassigned balancing bucket.
    return MaterialStock.objects.get_or_create(
        warehouse=warehouse, part=part, lot_no=None, production_lot=None,
        defaults={'quantity': 0},
    )[0]
