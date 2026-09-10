"""입고 취소 대상 조회. 연결이 불명확한 과거 기록은 추정해서 삭제하지 않는다."""

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q

from orders.models import DeliveryOrderItem, LabelPrintLog
from .models import MaterialTransaction


class CancellationScopeError(ValueError):
    pass


def inspection_transfers_for_cancel(trx, inspection):
    """원본 입고 연결 우선, 과거 기록은 납품서/검사 대상이 유일할 때만 선택."""
    from qms.models import ImportInspection

    base = MaterialTransaction.objects.filter(
        transaction_type='TRANSFER', part_id=trx.part_id, lot_no=trx.lot_no,
        production_lot=trx.production_lot,
        warehouse_from_id=trx.warehouse_to_id,
        remark__startswith='[수입검사]',
    )
    linked = base.filter(source_incoming=trx)
    legacy = base.filter(source_incoming__isnull=True)
    peers = ImportInspection.objects.filter(
        inbound_transaction__part_id=trx.part_id,
        inbound_transaction__lot_no=trx.lot_no,
        inbound_transaction__warehouse_to_id=trx.warehouse_to_id,
        status__in=['APPROVED', 'REJECTED'],
    ).exclude(pk=inspection.pk)

    selected_ids = []
    expected = [
        (inspection.qty_good, inspection.target_warehouse_code or '2000'),
        (inspection.qty_bad, '8200'),
    ]
    for quantity, warehouse_code in expected:
        if quantity <= 0:
            continue
        rows = list(linked.filter(warehouse_to__code=warehouse_code))
        if not rows and trx.ref_delivery_order:
            rows = list(legacy.filter(
                ref_delivery_order=trx.ref_delivery_order,
                warehouse_to__code=warehouse_code,
            ))
            if rows and peers.filter(
                inbound_transaction__ref_delivery_order=trx.ref_delivery_order,
            ).exists():
                raise CancellationScopeError("같은 납품서의 검사 이동 원본이 중복되어 취소 대상을 확인해야 합니다.")

        if not rows:
            # 예전 불량 이동 및 수기입고는 납품서 연결이 없을 수 있다.
            # 같은 품번/LOT/창고에 다른 판정 건이 있으면 임의로 연결하지 않는다.
            unreferenced = legacy.filter(
                Q(ref_delivery_order__isnull=True) | Q(ref_delivery_order=''),
                warehouse_to__code=warehouse_code,
            )
            if peers.exists():
                raise CancellationScopeError(
                    "과거 검사 이동 이력의 원본 입고 연결이 불명확합니다. 다른 납품서 보호를 위해 취소를 중단했습니다."
                )
            rows = list(unreferenced)

        if len(rows) != 1 or rows[0].quantity != quantity:
            raise CancellationScopeError("검사 이동 이력과 판정 수량이 일치하지 않아 취소 대상을 확인해야 합니다.")
        selected_ids.append(rows[0].pk)

    # 선택된 PK만 재사용하여 ERP 삭제와 이력 삭제의 범위를 일치시킨다.
    return MaterialTransaction.objects.filter(pk__in=selected_ids)


def validate_cancel_scope(trx):
    """납품서 일괄 취소에서도 첫 ERP 호출 전에 모든 기록의 연결을 확인한다."""
    label_for_cancel(trx)
    try:
        inspection = trx.inspection
    except ObjectDoesNotExist:
        return
    if inspection.status in ('APPROVED', 'REJECTED'):
        inspection_transfers_for_cancel(trx, inspection)


def label_for_cancel(trx):
    """선택한 납품서 품목의 라벨만 복구한다. 과거 라벨의 모호한 매칭은 차단."""
    if not trx.ref_delivery_order:
        return None
    items = DeliveryOrderItem.objects.filter(
        order__order_no=trx.ref_delivery_order,
        part_no=trx.part.part_no, lot_no=trx.lot_no,
    )
    labels = LabelPrintLog.objects.filter(
        delivery_item__in=items, part_id=trx.part_id, printed_qty=trx.quantity,
    )
    matches = list(labels[:2])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CancellationScopeError("원본 납품서의 라벨이 중복되어 취소 대상을 확인해야 합니다.")

    # 기존 데이터: 발주/SNP/수량으로 좁혀도 다른 납품서와 겹치면 취소하지 않는다.
    items = list(items.filter(total_qty=trx.quantity)[:2])
    if not items:
        return None
    if len(items) != 1:
        raise CancellationScopeError("과거 납품서 품목이 중복되어 라벨 복구 대상을 확인해야 합니다.")
    item = items[0]
    legacy = list(LabelPrintLog.objects.filter(
        delivery_item__isnull=True, part_id=trx.part_id,
        order_id=item.linked_order_id, snp=item.snp, printed_qty=trx.quantity,
    )[:2])
    if not legacy:
        return None
    other_items = DeliveryOrderItem.objects.filter(
        part_no=item.part_no, linked_order_id=item.linked_order_id,
        snp=item.snp, total_qty=item.total_qty,
    ).exclude(pk=item.pk)
    if len(legacy) != 1 or other_items.exists():
        raise CancellationScopeError(
            "과거 라벨의 원본 납품서 연결이 불명확합니다. 다른 납품서 보호를 위해 취소를 중단했습니다."
        )
    return legacy[0]
