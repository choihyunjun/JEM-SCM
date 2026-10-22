"""발주 잔량(납품 가능 수량) 단일 계산 모듈.

모든 화면(납품서 등록/조회, 발주 조회 미납 알림, 관리자 페이지, 대시보드,
납기준수율)과 실제 납품서 등록 검증이 이 모듈 하나만 사용한다.

기준 (사장님 결정, 2026-10)
  납품 가능 수량 = 발주량 − 납품서로 등록한 수량 + 반출 인수확인으로 복구된 수량
  (입고 수량이 아니다. 입고 기준이면 입고 전까지 납품서를 무한정 발행할 수 있다.)

- ERP 발주: 발주 건별로 관리한다. 납품서 라벨(LabelPrintLog.order)이 발주를 가리킨다.
- 수기 발주: 같은 협력사·품번의 승인된 수기 발주를 하나의 '풀'로 관리한다(기존 정책).
  풀 사용량은 각 발주에 배분한다.
    1) 새 납품서는 등록 시점에 납기가 가장 빠른 열린 발주에 연결된다
       (DeliveryOrderItem.linked_order). 연결된 수량은 그 발주가 먼저 소화한다.
    2) 연결 없는 과거 납품분·초과분은 마감된 발주(=납품 완료로 처리된 발주)가 먼저,
       그다음 열린 발주가 납기순으로 소화한다.
  그래서 납품을 마친 발주를 마감해도 그 수량이 다른 수기 발주로 넘어가지 않는다.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from django.db.models import Q, Sum

from .models import LabelPrintLog, Order, ReturnLog
from .returns import restorable_items

MANUAL_Q = Q(erp_order_no__isnull=True) | Q(erp_order_no='')


def is_manual(order):
    return not (order.erp_order_no or '').strip()


@dataclass
class OrderProgress:
    ordered: int
    delivered: int

    @property
    def remaining(self):
        return max(0, self.ordered - self.delivered)


@dataclass
class ManualPool:
    part_no: str
    vendor_id: int
    orders: list = field(default_factory=list)   # 승인된 수기 발주 (마감 포함), 납기순
    allocated: dict = field(default_factory=dict)
    overflow: int = 0
    credit: int = 0

    @property
    def open_orders(self):
        return [o for o in self.orders if not o.is_closed]

    @property
    def remaining(self):
        open_orders = self.open_orders
        capacity = sum(o.quantity for o in open_orders)
        left = sum(o.quantity - self.allocated.get(o.pk, 0) for o in open_orders)
        return max(0, min(capacity, left - self.overflow + self.credit))

    def progress(self, order):
        return OrderProgress(order.quantity, self.allocated.get(order.pk, 0))

    def primary_order(self):
        """새 납품을 연결할 발주: 잔량이 남은 열린 발주 중 납기가 가장 빠른 것."""
        open_orders = self.open_orders
        for order in open_orders:
            if order.quantity > self.allocated.get(order.pk, 0):
                return order
        return open_orders[0] if open_orders else None


def _fifo(orders):
    return sorted(orders, key=lambda o: (o.due_date, o.created_at, o.pk))


def manual_pools(keys):
    """{(품번, 협력사ID): ManualPool}. keys는 (part_no, vendor_id) 묶음."""
    keys = {(p, v) for p, v in keys if p and v}
    if not keys:
        return {}
    part_nos = {p for p, _ in keys}
    pools = {key: ManualPool(*key) for key in keys}

    orders = Order.objects.filter(MANUAL_Q, part_no__in=part_nos, approved_at__isnull=False)
    for order in orders:
        pool = pools.get((order.part_no, order.vendor_id))
        if pool:
            pool.orders.append(order)
    manual_ids = {o.pk for pool in pools.values() for o in pool.orders}

    linked = defaultdict(int)   # 수기 발주에 직접 연결된 납품 수량
    pooled = defaultdict(int)   # 연결 없는(과거) 수기 납품 수량
    labels = LabelPrintLog.objects.filter(part_no__in=part_nos, order__isnull=True).filter(
        Q(delivery_item__isnull=True) | Q(delivery_item__erp_order_no__isnull=True) |
        Q(delivery_item__erp_order_no='')
    ).values('part_no', 'vendor_id', 'delivery_item__linked_order_id').annotate(q=Sum('printed_qty'))
    for row in labels:
        order_id = row['delivery_item__linked_order_id']
        if order_id in manual_ids:
            linked[order_id] += row['q'] or 0
        else:
            pooled[(row['part_no'], row['vendor_id'])] += row['q'] or 0

    for item, quantity in restorable_items(ReturnLog.objects.filter(part__part_no__in=part_nos)):
        if (item.erp_order_no or '').strip():
            continue
        if item.linked_order_id in manual_ids:
            linked[item.linked_order_id] -= quantity
        else:
            vendor_id = item.linked_order.vendor_id if item.linked_order_id else None
            matches = [k for k in pools if k[0] == item.part_no and (vendor_id is None or k[1] == vendor_id)]
            if matches:
                pooled[matches[0]] -= quantity

    for key, pool in pools.items():
        pool.orders = _fifo(pool.orders)
        spill = pooled[key]
        if spill < 0:
            pool.credit, spill = -spill, 0
        for order in pool.orders:
            own = max(0, linked[order.pk])
            pool.allocated[order.pk] = min(order.quantity, own)
            spill += own - pool.allocated[order.pk]
        closed_first = [o for o in pool.orders if o.is_closed] + pool.open_orders
        for order in closed_first:
            if spill <= 0:
                break
            take = min(order.quantity - pool.allocated[order.pk], spill)
            pool.allocated[order.pk] += take
            spill -= take
        pool.overflow = spill
    return pools


def manual_pool(part_no, vendor_id):
    return manual_pools({(part_no, vendor_id)}).get((part_no, vendor_id))


def order_progress(orders):
    """{발주ID: OrderProgress} — ERP 발주는 건별, 수기 발주는 풀 배분 결과."""
    orders = list(orders)
    result = {}
    erp = [o for o in orders if not is_manual(o)]
    if erp:
        ids = [o.pk for o in erp]
        printed = dict(LabelPrintLog.objects.filter(order_id__in=ids).values('order_id')
                       .annotate(q=Sum('printed_qty')).values_list('order_id', 'q'))
        returned = defaultdict(int)
        for item, quantity in restorable_items(ReturnLog.objects.filter(delivery_item__linked_order_id__in=ids)):
            if item.linked_order_id and item.erp_order_no:
                returned[item.linked_order_id] += quantity
        for o in erp:
            result[o.pk] = OrderProgress(o.quantity, max(0, (printed.get(o.pk) or 0) - returned[o.pk]))
    manual = [o for o in orders if is_manual(o)]
    if manual:
        pools = manual_pools({(o.part_no, o.vendor_id) for o in manual})
        for o in manual:
            pool = pools.get((o.part_no, o.vendor_id))
            result[o.pk] = pool.progress(o) if pool and o.approved_at else OrderProgress(o.quantity, 0)
    return result


def has_delivery_history(order):
    """납품서 이력이 이 발주에 걸려 있는지. 삭제하면 그 수량이 다른 발주로 옮겨 계산된다."""
    if order.labels.exists() or order.delivery_items.exists():
        return True
    return is_manual(order) and order_progress([order])[order.pk].delivered > 0
