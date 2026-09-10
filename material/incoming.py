"""Resolve a receipt's original delivery line without guessing between orders."""
from orders.models import DeliveryOrderItem


def delivery_item_for_incoming(trx):
    if trx.delivery_item_id:
        item = trx.delivery_item
        if (item.order.order_no != trx.ref_delivery_order or
                item.part_no != trx.part.part_no or item.lot_no != trx.lot_no):
            raise ValueError('원본 납품서 품목과 입고 정보가 일치하지 않습니다.')
        return item
    if not trx.ref_delivery_order:
        return None
    items = list(DeliveryOrderItem.objects.filter(
        order__order_no=trx.ref_delivery_order,
        part_no=trx.part.part_no, lot_no=trx.lot_no,
    )[:2])
    if len(items) != 1:
        raise ValueError('원본 납품서 품목을 특정할 수 없습니다. LOT와 발주 연결을 확인해 주세요.')
    return items[0]
