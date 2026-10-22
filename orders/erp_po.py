"""ERP 발주 → SCM 발주 적용 규칙 (조회 화면과 적용 처리 공통).

- SCM 발주수량 = ERP 잔량(remain 값이 있으면 그 값, 없으면 발주수량 − 입고수량). 잔량 0인 줄은 적용 불가.
- 협력사 = ERP 발주 거래처(trCd → Vendor.erp_code) 우선, 매칭되지 않을 때만 품목 담당 협력사.
- 이미 적용한 줄을 다시 적용하면:
    * ERP 발주수량이 바뀌었으면 그 증감만큼 SCM 발주수량을 조정한다
      (SCM에서 이미 납품서로 등록한 수량 아래로는 줄이지 않는다).
    * 납기가 바뀌었으면 납기를 갱신한다.
    * ERP 발주 상세에서 그 순번이 사라졌거나 발주수량이 0이면 'ERP 취소'로 표시하고 마감한다.
  ERP 조회가 실패(빈 응답)한 발주는 취소로 단정하지 않고 건너뛴다.
"""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction

from .models import Order, Part, Vendor

REMAIN_KEYS = ('remainQt', 'remQt', 'rmnQt', 'poRemainQt')


def erp_qty(value):
    """ERP 수량(문자열·소수 가능)을 SCM 발주 정수 수량으로."""
    try:
        return int(Decimal(str(value if value not in (None, '') else 0)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 0


def parse_erp_date(raw, default=None):
    raw = (raw or '').replace('-', '')
    if len(raw) == 8 and raw.isdigit():
        try:
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError:
            pass
    return default


def line_values(detail):
    po_qt = erp_qty(detail.get('poQt'))
    rcv_qt = erp_qty(detail.get('rcvQt'))
    explicit = next((detail.get(k) for k in REMAIN_KEYS if detail.get(k) not in (None, '')), None)
    remain = erp_qty(explicit) if explicit is not None else po_qt - rcv_qt
    return {
        'po_sq': str(detail.get('poSq', '')),
        'item_cd': detail.get('itemCd', '') or '',
        'item_nm': detail.get('itemNm', '') or '',
        'po_qt': po_qt,
        'rcv_qt': rcv_qt,
        'remain_qt': max(0, remain),
        'due_raw': detail.get('dueDt', '') or '',
    }


def resolve_vendor(tr_cd, part, vendor_map=None):
    """ERP 거래처 우선, 없으면 품목 담당 협력사."""
    if tr_cd:
        vendor = (vendor_map or {}).get(tr_cd) if vendor_map is not None else \
            Vendor.objects.filter(erp_code=tr_cd).first()
        if vendor:
            return vendor
    return part.vendor if part else None


def sync_state(order, values):
    """이미 적용된 SCM 발주와 ERP 줄 비교: '', 'changed', 'cancelled'."""
    if order is None:
        return ''
    if values is None or values['po_qt'] <= 0:
        return '' if order.erp_status == 'CANCELLED' else 'cancelled'
    due = parse_erp_date(values['due_raw'])
    if order.erp_po_qty is not None and order.erp_po_qty != values['po_qt']:
        return 'changed'
    if due and due != order.due_date:
        return 'changed'
    return ''


def apply_lines(selected, fetch_details, today):
    """selected: [(po_nb, po_sq, tr_cd)]. 결과 dict(created, updated, cancelled, skipped, errors, warnings)."""
    from .remaining import order_progress
    result = {'created': 0, 'updated': 0, 'cancelled': 0, 'skipped': 0, 'errors': [], 'warnings': []}
    details_cache = {}
    vendor_map = {v.erp_code: v for v in Vendor.objects.exclude(erp_code__isnull=True).exclude(erp_code='')}

    for po_nb, po_sq, tr_cd in selected:
        if po_nb not in details_cache:
            rows = fetch_details(po_nb) or []
            details_cache[po_nb] = {str(r.get('poSq', '')): line_values(r) for r in rows}
        lines = details_cache[po_nb]
        if not lines:
            result['errors'].append(f'{po_nb}: ERP 발주 상세를 불러오지 못해 적용하지 않았습니다.')
            continue
        values = lines.get(str(po_sq))

        with transaction.atomic():
            existing = Order.objects.select_for_update().filter(
                erp_order_no=po_nb, erp_order_seq=str(po_sq)).first()

            if existing is None:
                if values is None:
                    result['errors'].append(f'{po_nb}-{po_sq}: ERP 발주에 없는 순번입니다.')
                    continue
                if values['remain_qt'] <= 0:
                    result['skipped'] += 1
                    result['warnings'].append(f'{po_nb}-{po_sq}: ERP 잔량이 0이라 적용하지 않았습니다.')
                    continue
                part = Part.objects.select_related('vendor').filter(part_no=values['item_cd']).first()
                if not part:
                    result['errors'].append(f'품번 미매칭: {values["item_cd"]}')
                    continue
                vendor = resolve_vendor(tr_cd, part, vendor_map)
                if not vendor:
                    result['errors'].append(f'거래처 미매칭: {values["item_cd"]} (trCd={tr_cd})')
                    continue
                if part.vendor_id and part.vendor_id != vendor.pk:
                    result['warnings'].append(
                        f'{values["item_cd"]}: ERP 거래처({vendor.name})와 품목 담당 협력사({part.vendor.name})가 다릅니다. '
                        '품목 관리에서 담당 협력사를 맞춰야 협력사가 납품서를 등록할 수 있습니다.')
                Order.objects.create(
                    vendor=vendor, part_group=part.part_group or '', part_no=part.part_no,
                    part_name=part.part_name or '', quantity=values['remain_qt'],
                    due_date=parse_erp_date(values['due_raw'], today),
                    erp_order_no=po_nb, erp_order_seq=str(po_sq), erp_po_qty=values['po_qt'],
                )
                result['created'] += 1
                continue

            state = sync_state(existing, values)
            if not state:
                result['skipped'] += 1
                continue
            if state == 'cancelled':
                existing.erp_status = 'CANCELLED'
                existing.is_closed = True
                existing.save(update_fields=['erp_status', 'is_closed'])
                result['cancelled'] += 1
                result['warnings'].append(f'{po_nb}-{po_sq}: ERP에서 취소된 발주라 SCM 발주를 마감(ERP 취소)했습니다.')
                continue

            fields = []
            due = parse_erp_date(values['due_raw'])
            if due and due != existing.due_date:
                existing.due_date = due
                fields.append('due_date')
            if existing.erp_po_qty is not None and existing.erp_po_qty != values['po_qt']:
                delivered = order_progress([existing])[existing.pk].delivered
                target = existing.quantity + (values['po_qt'] - existing.erp_po_qty)
                if target < delivered:
                    result['warnings'].append(
                        f'{po_nb}-{po_sq}: ERP 수량이 이미 납품서로 등록된 {delivered:,}개보다 적어 {delivered:,}개로 맞췄습니다.')
                    target = delivered
                existing.quantity = max(0, target)
                existing.erp_po_qty = values['po_qt']
                fields += ['quantity', 'erp_po_qty']
            if fields:
                existing.save(update_fields=fields)
                result['updated'] += 1
            else:
                result['skipped'] += 1
    return result
