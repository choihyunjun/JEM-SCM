"""Validate all delivery lines and reserve purchase-order capacity in one transaction."""
from collections import defaultdict
from datetime import date
from uuid import uuid4

from django.db import transaction
from django.db.models import F, Max
from django.utils import timezone

from .access import scope_vendor_queryset
from .models import DeliveryOrder, DeliveryOrderItem, LabelPrintLog, LotExceptionRequest, Order, Part
from .remaining import manual_pools, order_progress


class OlderLotError(ValueError):
    """이전 납품보다 오래된 LOT가 포함되어 예외 승인이 필요하다. blocked = [(Part, lot, latest_lot)]."""

    def __init__(self, blocked):
        self.blocked = blocked
        super().__init__('이전 납품보다 오래된 LOT가 포함되어 있습니다.')


def latest_delivered_lots(part_nos):
    """{품번: 이전 납품서에 등록된 LOT(생산일) 중 가장 최근 날짜}.

    기준은 납품서뿐이다. ERP에서 들어온 입고의 LOT는 생산일이 아니라 입고일이라 비교하면 안 된다.
    """
    return dict(DeliveryOrderItem.objects.filter(part_no__in=part_nos, lot_no__isnull=False)
                .values('part_no').annotate(latest=Max('lot_no')).values_list('part_no', 'latest'))


def request_lot_exceptions(user, blocked):
    """막힌 LOT마다 승인 대기 요청을 1건씩 둔다(이미 대기 중이면 그대로). 요청 목록을 돌려준다."""
    requests = []
    for part, lot, latest in blocked:
        pending = LotExceptionRequest.objects.filter(part=part, lot_no=lot, status='PENDING').first()
        if not pending:
            pending = LotExceptionRequest.objects.create(
                vendor=part.vendor, part=part, lot_no=lot, latest_lot=latest, requested_by=user)
        requests.append(pending)
    return requests


def register_delivery(user, data):
    keys = ['part_nos[]', 'snps[]', 'box_counts[]', 'order_ids[]', 'lot_nos[]']
    arrays = [data.getlist(key) for key in keys]
    if not arrays[0] or len({len(values) for values in arrays}) != 1:
        raise ValueError('납품 품목과 수량, 발주, LOT 정보를 다시 확인해주세요.')
    lines, seen = [], set()
    for part_no, snp, boxes, order_id, lot in zip(*arrays):
        try:
            part_no = part_no.strip()
            snp, boxes = int(snp), int(boxes)
            lot = date.fromisoformat(lot.strip())
            order_id = None if order_id in ('', 'None') else int(order_id)
        except (ValueError, TypeError):
            raise ValueError('수량·포장 수·발주 번호·LOT 날짜를 올바르게 입력해주세요.')
        if min(snp, boxes) <= 0 or max(snp, boxes, snp * boxes) > 2147483647:
            raise ValueError('SNP와 박스 수는 양의 정수이며 총수량은 저장 가능한 범위여야 합니다.')
        if (part_no, lot) in seen:
            raise ValueError(f'[{part_no} / {lot}] 같은 품번·LOT는 한 납품서에 한 번만 등록할 수 있습니다.')
        seen.add((part_no, lot))
        lines.append((part_no, snp, boxes, order_id, lot))

    today = timezone.localdate()
    for part_no, snp, boxes, order_id, lot in lines:
        if lot > today:  # 오타로 미래 날짜가 들어가면 이후 납품이 전부 '이전 LOT'로 막힌다.
            raise ValueError(f'[{part_no} / {lot}] LOT(생산일)는 오늘 이후 날짜일 수 없습니다.')

    with transaction.atomic():
        # Lock the common part row before reading capacity, including manual PO aggregates.
        # Stable ordering avoids multi-part registrations taking locks in opposite order.
        part_nos = sorted({line[0] for line in lines})
        for part_no in part_nos:
            if scope_vendor_queryset(user, Part.objects.filter(part_no=part_no)).update(part_name=F('part_name')) != 1:
                raise ValueError('존재하지 않거나 권한이 없는 품번이 포함되어 있습니다.')
        parts = {part.part_no: part for part in Part.objects.filter(part_no__in=part_nos)}
        order_ids = sorted({line[3] for line in lines if line[3] is not None})
        orders = {order.pk: order for order in scope_vendor_queryset(user,
            Order.objects.select_for_update().filter(pk__in=order_ids)).order_by('pk')}
        requested = defaultdict(int)
        for part_no, snp, boxes, order_id, lot in lines:
            part = parts[part_no]
            if order_id is not None:
                order = orders.get(order_id)
                if not order or order.part_no != part_no or order.vendor_id != part.vendor_id:
                    raise ValueError('발주와 품번·협력사가 일치하지 않습니다.')
                if not order.approved_at or order.is_closed:
                    raise ValueError('미승인 또는 마감 발주는 납품서를 등록할 수 없습니다.')
                # Manual orders retain the existing part-level aggregation policy.
                if not order.erp_order_no:
                    raise ValueError('수기 발주는 납품서 등록 목록의 품번별 잔량을 선택해주세요.')
            requested[(part_no, order_id)] += snp * boxes

        # 화면(label_list)과 같은 잔량 계산을 사용한다: 발주량 - 납품서 등록량 + 반출 복구분
        progress = order_progress(orders.values())
        pools = manual_pools({(part_no, parts[part_no].vendor_id)
                              for (part_no, order_id) in requested if order_id is None})
        for (part_no, order_id), quantity in requested.items():
            if order_id is not None:
                remain = progress[order_id].remaining
            else:
                pool = pools.get((part_no, parts[part_no].vendor_id))
                remain = pool.remaining if pool else 0
            if quantity > remain:
                raise ValueError(f'[{part_no}] 납품가능 수량 {remain:,}개를 초과했습니다. 다시 조회해주세요.')

        # 선입선출: 그 품목의 이전 납품서 LOT보다 오래된 LOT는 예외 승인이 있어야 등록된다.
        latest = latest_delivered_lots(part_nos)
        approvals, blocked = {}, []
        for part_no, snp, boxes, order_id, lot in lines:
            if part_no in latest and lot < latest[part_no]:
                approval = (LotExceptionRequest.objects.select_for_update()
                            .filter(part=parts[part_no], lot_no=lot, status='APPROVED').order_by('pk').first())
                if approval:
                    approvals[(part_no, lot)] = approval
                else:
                    blocked.append((parts[part_no], lot, latest[part_no]))
        if blocked:
            raise OlderLotError(blocked)

        delivery = DeliveryOrder.objects.create(
            order_no='DO-' + timezone.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:16])
        for part_no, snp, boxes, order_id, lot in lines:
            part, order = parts[part_no], orders.get(order_id)
            # 수기 발주는 품번 풀로 검증하되, 납기 판단을 위해 가장 빠른 열린 발주에 연결해 둔다.
            linked = order
            pool = pools.get((part_no, part.vendor_id)) if order is None else None
            if pool:
                linked = pool.primary_order()
                if linked:
                    pool.allocated[linked.pk] = pool.allocated.get(linked.pk, 0) + snp * boxes
            item = DeliveryOrderItem.objects.create(order=delivery, part_no=part_no, part_name=part.part_name,
                snp=snp, box_count=boxes, total_qty=snp * boxes, lot_no=lot, linked_order=linked,
                erp_order_no=order.erp_order_no if order else '', erp_order_seq=order.erp_order_seq if order else '')
            LabelPrintLog.objects.create(delivery_item=item, vendor=part.vendor, part=part,
                part_no=part_no, printed_qty=item.total_qty, snp=snp, order=order)
            approval = approvals.get((part_no, lot))
            if approval:  # 승인은 한 번 쓰면 끝난다.
                approval.status, approval.delivery_item = 'USED', item
                approval.save(update_fields=['status', 'delivery_item'])
        return delivery


def released_items(delivery):
    """입고 취소로 발주 잔량 예약(LabelPrintLog)이 풀린 납품서 품목.

    연결 없는 과거 발행 이력이 남아 있는 품번은 예약 여부를 단정할 수 없으므로 제외한다
    (과거 납품서를 이중으로 잡지 않기 위함).
    """
    items = list(delivery.items.filter(label_logs__isnull=True))
    if not items:
        return []
    legacy = set(LabelPrintLog.objects.filter(
        delivery_item__isnull=True, part_no__in={i.part_no for i in items}).values_list('part_no', flat=True))
    return [i for i in items if i.part_no not in legacy]


def reserve_released_items(delivery):
    """다시 입고하려는 납품서의 풀린 품목에 대해 발주 잔량을 재검증하고 다시 예약한다.

    호출자는 납품서 행을 잠근 트랜잭션 안에 있어야 한다. 잔량이 부족하면 ValueError.
    """
    items = released_items(delivery)
    if not items:
        return 0
    part_nos = sorted({i.part_no for i in items})
    for part_no in part_nos:  # register_delivery와 같은 순서로 잠근다.
        Part.objects.filter(part_no=part_no).update(part_name=F('part_name'))
    parts = {p.part_no: p for p in Part.objects.filter(part_no__in=part_nos)}
    order_ids = sorted({i.linked_order_id for i in items if (i.erp_order_no or '').strip() and i.linked_order_id})
    orders = {o.pk: o for o in Order.objects.select_for_update().filter(pk__in=order_ids).order_by('pk')}

    requested = defaultdict(int)
    for item in items:
        part = parts.get(item.part_no)
        if not part or not part.vendor_id:
            raise ValueError(f'[{item.part_no}] 품목 또는 협력사 정보를 확인할 수 없습니다.')
        if (item.erp_order_no or '').strip():
            order = orders.get(item.linked_order_id)
            if not order:
                raise ValueError(f'[{item.part_no}] 원본 발주 연결을 확인할 수 없습니다. 납품서를 취소하고 다시 발행해주세요.')
            if not order.approved_at or order.is_closed:
                raise ValueError(f'[{item.part_no}] 발주가 마감되었거나 미승인 상태입니다. 납품서를 취소하고 다시 발행해주세요.')
            requested[(item.part_no, order.pk)] += item.total_qty
        else:
            requested[(item.part_no, None)] += item.total_qty

    progress = order_progress(orders.values())
    pools = manual_pools({(part_no, parts[part_no].vendor_id)
                          for (part_no, order_id) in requested if order_id is None})
    for (part_no, order_id), quantity in requested.items():
        if order_id is not None:
            remain = progress[order_id].remaining
        else:
            pool = pools.get((part_no, parts[part_no].vendor_id))
            remain = pool.remaining if pool else 0
        if quantity > remain:
            raise ValueError(
                f'[{part_no}] 입고 취소 후 발주 잔량이 다른 납품서에 사용되어 {remain:,}개만 남았습니다 '
                f'(이 납품서 {quantity:,}개). 납품서를 취소하고 잔량에 맞게 다시 발행해주세요.')

    for item in items:
        part = parts[item.part_no]
        order = orders.get(item.linked_order_id) if (item.erp_order_no or '').strip() else None
        LabelPrintLog.objects.create(delivery_item=item, vendor=part.vendor, part=part, part_no=item.part_no,
                                     printed_qty=item.total_qty, snp=item.snp, order=order)
    return len(items)
