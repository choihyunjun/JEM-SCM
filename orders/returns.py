"""검사 불량 건, 실제 반출, 협력사 인수 확인을 연결한다."""
import uuid
from collections import defaultdict

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import ReturnLog, ReturnShipment


@transaction.atomic
def ship_return(case_id, quantity, user, request_key):
    from material.models import MaterialStock, MaterialTransaction
    from material.stock_safety import deduct_stock
    if quantity <= 0:
        raise ValueError('반출 수량은 1개 이상이어야 합니다.')
    uuid.UUID(request_key)  # 재전송 방지 키 검증
    key = f'{request_key}:{case_id}'
    origin_id = ReturnLog.objects.filter(pk=case_id).values_list('source_incoming_id', flat=True).first()
    if origin_id:
        MaterialTransaction.objects.filter(pk=origin_id).update(quantity=F('quantity'))
    # PostgreSQL 행 잠금과 SQLite 쓰기 잠금을 모두 확보한 뒤 잔량을 읽는다.
    if not ReturnLog.objects.filter(pk=case_id).update(quantity=F('quantity')):
        raise ValueError('반출 대상이 없습니다.')
    case = ReturnLog.objects.select_related('source_incoming', 'delivery_item', 'part').get(pk=case_id)
    existing = ReturnShipment.objects.filter(request_key=key).first()
    if existing:
        if existing.return_log_id != case.pk or existing.movement.quantity != quantity:
            raise ValueError('이미 처리한 요청과 수량이 다릅니다. 목록을 새로고침하세요.')
        return existing
    if not case.source_incoming_id:
        raise ValueError('과거 내역의 원본 입고·LOT 연결을 먼저 확인해야 합니다.')
    origin = case.source_incoming
    if MaterialTransaction.objects.filter(transaction_type='OUT_RETURN', part=case.part,
        date__gte=case.created_at, return_shipment__isnull=True).exists():
        raise ValueError('연결되지 않은 과거 반출 이력이 있습니다. 중복 반출 방지를 위해 원본 내역을 먼저 확인해 주세요.')
    if quantity > case.remaining_quantity:
        raise ValueError(f'{case.part.part_no}: 반출대기 {case.remaining_quantity}개를 초과했습니다.')
    stock = MaterialStock.objects.filter(warehouse__code='8200', part=case.part,
        lot_no=origin.lot_no, production_lot=origin.production_lot).first()
    deduct_stock(stock, quantity)
    stock.refresh_from_db()
    movement = MaterialTransaction.objects.create(
        transaction_no='RET-' + uuid.uuid4().hex[:24], transaction_type='OUT_RETURN',
        part=case.part, quantity=quantity, lot_no=origin.lot_no, production_lot=origin.production_lot,
        warehouse_from=stock.warehouse, vendor=origin.vendor or case.part.vendor, actor=user,
        source_incoming=origin, delivery_item=case.delivery_item,
        ref_delivery_order=origin.ref_delivery_order, result_stock=stock.quantity,
        remark=f'[반품] {case.reason}'[:200],
    )
    return ReturnShipment.objects.create(return_log=case, movement=movement, request_key=key)


@transaction.atomic
def acknowledge_return(case_id, shipment_id, user):
    from .access import vendor_identity
    ReturnLog.objects.filter(pk=case_id).update(quantity=F('quantity'))
    case = ReturnLog.objects.select_related('part').get(pk=case_id)
    restricted, vendor, _ = vendor_identity(user)
    if not user.is_superuser and (not restricted or not vendor or vendor.pk != case.part.vendor_id):
        raise ValueError('자사 반출 내역만 확인할 수 있습니다.')
    item = case.delivery_item
    if case.delivery_order_id and (not item or item.order_id != case.delivery_order_id or
            item.part_no != case.part.part_no or (item.erp_order_no and not item.linked_order_id)):
        raise ValueError('원본 납품 품목·발주 연결을 먼저 확인해야 납품 가능 수량을 복구할 수 있습니다.')
    shipment = ReturnShipment.objects.select_related('movement').filter(pk=shipment_id, return_log=case).first()
    if not shipment:
        raise ValueError('실제로 반출된 건을 선택해 주세요. 반출 전에는 확인할 수 없습니다.')
    if shipment.confirmed_at:
        return 0
    now = timezone.now()
    ReturnShipment.objects.filter(pk=shipment.pk, confirmed_at__isnull=True).update(
        confirmed_at=now, confirmed_by=user)
    case.is_confirmed = case.confirmed_quantity >= case.quantity
    case.confirmed_at = now if case.is_confirmed else None
    case.save(update_fields=['is_confirmed', 'confirmed_at'])
    return shipment.movement.quantity


def restored_quantities(cases):
    """발주번호가 아닌 원본 납품 품목 FK로 단 한 번씩 복구한다."""
    by_order, by_manual_part = defaultdict(int), defaultdict(int)
    for case in cases.select_related('delivery_item__linked_order', 'part').prefetch_related('shipments__movement'):
        item = case.delivery_item
        if not item or item.part_no != case.part.part_no or item.order_id != case.delivery_order_id:
            continue  # 연결 불명확한 과거 건을 다른 발주에 추정 반영하지 않는다.
        quantity = min(case.quantity, case.confirmed_quantity)
        if item.linked_order_id and item.erp_order_no:
            by_order[item.linked_order_id] += quantity
        elif not item.erp_order_no:
            by_manual_part[case.part.part_no] += quantity
    return by_order, by_manual_part


@transaction.atomic
def refresh_delivery_status(delivery):
    from .models import DeliveryOrder
    DeliveryOrder.objects.filter(pk=delivery.pk).update(is_received=F('is_received'))
    from material.models import MaterialTransaction
    from qms.models import ImportInspection
    receipts = MaterialTransaction.objects.filter(ref_delivery_order=delivery.order_no,
        transaction_type__in=['IN_SCM', 'IN_MANUAL', 'IN_ERP'])
    if not receipts.exists():
        # 미입고 납품서만 발행 상태로 되돌린다.
        status, received = 'PENDING', False
    else:
        inspections = ImportInspection.objects.filter(inbound_transaction__in=receipts)
        received = True
        if inspections.filter(status='PENDING').exists() or receipts.count() < delivery.items.count():
            status = 'RECEIVED'
        elif inspections.filter(qty_good__gt=0).exists() or receipts.filter(inspection__isnull=True).exists():
            status = 'APPROVED'
        else:
            status = 'REJECTED'
    delivery.status, delivery.is_received = status, received
    delivery.save(update_fields=['status', 'is_received'])


def guard_return_cancellation(origin):
    """실물 반출 또는 과거 인수 확인이 있으면 검사/입고를 초기화하지 않는다."""
    from django.db.models import Q
    cases = ReturnLog.objects.filter(Q(source_incoming=origin) | Q(
        source_incoming__isnull=True, delivery_order__order_no=origin.ref_delivery_order,
        part_id=origin.part_id)) if origin.ref_delivery_order else ReturnLog.objects.filter(source_incoming=origin)
    cases.update(quantity=F('quantity'))
    if cases.filter(Q(shipments__isnull=False) | Q(is_confirmed=True) |
                    Q(legacy_confirmed_quantity__gt=0)).exists():
        raise ValueError('이미 반출 또는 인수 확인된 수량이 있어 판정·입고를 취소할 수 없습니다.')
    if cases.filter(source_incoming__isnull=True).exists():
        raise ValueError('과거 반출 내역의 원본 입고 연결을 확인한 후 정정해 주세요.')
    return cases


def guard_inspection_stock_usage(origin, transfers):
    """이미 이동/사용한 LOT은 다른 입고 재고로 채워졌더라도 정정하지 않는다."""
    from material.models import MaterialTransaction
    for transfer in transfers:
        if MaterialTransaction.objects.filter(
            part_id=origin.part_id, lot_no=origin.lot_no, production_lot=origin.production_lot,
            warehouse_from_id=transfer.warehouse_to_id, pk__gt=transfer.pk,
        ).exclude(pk__in=transfers.values_list('pk', flat=True)).exists():
            raise ValueError('판정 후 이동·사용된 LOT이므로 자동 정정할 수 없습니다. 재고 이력을 확인해 주세요.')


def reverse_inspection_summary(origin, inspection):
    from .models import Incoming, Inventory
    if not origin.ref_delivery_order:
        return
    # 과거 자료에 집계가 없는 경우 새로 만들지 않는다. 존재하는 집계만 원복한다.
    incoming = Incoming.objects.select_for_update().filter(
        delivery_order_no=origin.ref_delivery_order, part_id=origin.part_id).first()
    if incoming:
        if incoming.quantity < origin.quantity or incoming.confirmed_qty is None or incoming.confirmed_qty < inspection.qty_good:
            raise ValueError('SCM 입고 집계 수량이 맞지 않아 정정할 수 없습니다. 입고 내역을 확인해 주세요.')
        incoming.quantity -= origin.quantity
        incoming.confirmed_qty -= inspection.qty_good
        if incoming.quantity == 0:
            incoming.delete()
        else:
            incoming.save(update_fields=['quantity', 'confirmed_qty'])
        Inventory.objects.filter(part_id=origin.part_id).update(base_stock=F('base_stock') - inspection.qty_good)
