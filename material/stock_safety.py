"""Stock mutations shared by ERP reconciliation and WMS actions."""
from django.db.models import F

from .models import MaterialStock


def deduct_stock(stock, quantity):
    """Conditional debit: a stale read must never overdraw a LOT.

    Call inside the transaction that also writes the counterpart and history.
    """
    if not stock or quantity <= 0:
        raise ValueError('차감할 재고 또는 수량이 올바르지 않습니다.')
    if MaterialStock.objects.filter(pk=stock.pk, quantity__gte=quantity).update(
        quantity=F('quantity') - quantity,
    ) != 1:
        raise ValueError('LOT 재고가 부족하거나 다른 작업에서 변경되었습니다. 재고를 다시 조회해 주세요.')


def unassigned_stock(warehouse, part):
    # An undated production batch is not the unassigned balancing bucket.
    return MaterialStock.objects.get_or_create(
        warehouse=warehouse, part=part, lot_no=None, production_lot=None,
        defaults={'quantity': 0},
    )[0]
