"""가용 재고·입고 예정·부족 품목 공통 계산.

재고/소요 조회, 알림 대시보드, 발주 조회의 부족 알림이 모두 이 모듈을 쓴다.
- 가용 재고 창고: 자재창고(2000) + 4공장 자재창고(4200) + 제조현장(4300)  (사장님 결정)
- 입고 예정: 아직 입고 스캔 전(PENDING)인 납품서 수량. 예정일은 연결 발주의 납기,
  납기가 지났거나 연결 발주가 없으면 오늘.
"""
from collections import defaultdict
from datetime import timedelta

from django.db.models import Sum

from .models import DeliveryOrderItem, Demand

AVAILABLE_WAREHOUSE_CODES = ('2000', '4200', '4300')


def available_stock_map(part_ids):
    """{part_id: 가용 재고 합계} (AVAILABLE_WAREHOUSE_CODES 창고만)."""
    from material.models import MaterialStock
    part_ids = list(part_ids)
    if not part_ids:
        return {}
    rows = (MaterialStock.objects.filter(part_id__in=part_ids, warehouse__code__in=AVAILABLE_WAREHOUSE_CODES)
            .values('part_id').annotate(total=Sum('quantity')))
    return {row['part_id']: row['total'] or 0 for row in rows}


def expected_incoming(part_nos, today, until=None):
    """{part_no: {예정일: 수량}} — until이 있으면 그 날짜까지만."""
    result = defaultdict(lambda: defaultdict(int))
    part_nos = list(part_nos)
    if not part_nos:
        return result
    items = DeliveryOrderItem.objects.filter(part_no__in=part_nos, order__status='PENDING').select_related('linked_order')
    for item in items:
        expected = item.linked_order.due_date if item.linked_order else today
        expected = max(expected, today)
        if until and expected > until:
            continue
        result[item.part_no][expected] += item.total_qty
    return result


def shortage_items(parts, today, days=7):
    """오늘부터 days일 안에 소요량을 못 맞추는 품목 (부족량 큰 순). parts는 Part 쿼리셋."""
    until = today + timedelta(days=days)
    demand = {row['part_id']: row['total'] or 0 for row in Demand.objects.filter(
        part__in=parts, due_date__gte=today, due_date__lte=until,
    ).values('part_id').annotate(total=Sum('quantity'))}
    parts = [p for p in parts.filter(id__in=[pid for pid, qty in demand.items() if qty]).select_related('vendor')]
    stock = available_stock_map(p.id for p in parts)
    incoming = expected_incoming((p.part_no for p in parts), today, until)
    shortages = []
    for part in parts:
        pending = sum(incoming[part.part_no].values()) if part.part_no in incoming else 0
        current = stock.get(part.id, 0)
        expected = current + pending - demand[part.id]
        if expected < 0:
            shortages.append({
                'part': part,
                'current_stock': current,
                'future_demand': demand[part.id],
                'pending_incoming': pending,
                'expected_shortage': abs(expected),
            })
    shortages.sort(key=lambda x: x['expected_shortage'], reverse=True)
    return shortages
