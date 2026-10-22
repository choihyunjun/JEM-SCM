"""Validate all delivery lines and reserve purchase-order capacity in one transaction."""
from collections import defaultdict
from datetime import date
from uuid import uuid4

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .access import scope_vendor_queryset
from .models import DeliveryOrder, DeliveryOrderItem, LabelPrintLog, Order, Part
from .remaining import manual_pools, order_progress


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
        return delivery
