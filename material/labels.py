"""라벨 규칙 한 곳 (2026-10 라벨 정리).

- QR에는 라벨 번호만 담는다. 품번·수량·LOT·위치는 번호로 시스템에서 읽는다.
- 원재료 라벨(포대 RM- / 파렛트 PLT-)의 상태는 '놓인 창고'와 '소진' 두 가지뿐이다.
  3000·4300(현장)으로 옮기는 것이 곧 투입이고, 소진은 ERP 생산출고가 정한다.
- sync_labels: 창고·LOT별 실재고와 라벨을 맞춘다. 재고를 따라 라벨을 옮기고,
  남는 라벨은 오래된 것부터 소진 처리한다. 일부만 쓴 포대는 라벨을 유지한다.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

LIVE = ('PRINTED', 'INSTOCK')
RAW_PREFIXES = ('RM-', 'PLT-')


def scan_id(raw):
    """PDA 입력에서 라벨 번호만 꺼낸다 (예전 'ID|품번|수량|LOT' 형식이 들어와도 번호만 쓴다)."""
    return (raw or '').split('|')[0].strip().upper()


def initial_warehouse(label):
    """새 라벨의 창고: 그 LOT 재고가 있는 곳, 원재료창고(3200) 우선."""
    from .models import MaterialStock, Warehouse
    from .stock_reconciliation import SCM_ONLY_WAREHOUSE_CODES
    raw = Warehouse.objects.filter(code='3200').first()
    holders = list(MaterialStock.objects.filter(part_id=label.part_id, lot_no=label.lot_no, quantity__gt=0)
                   .values_list('warehouse_id', flat=True).distinct()) if label.part_id else []
    if raw and raw.pk in holders:
        return raw
    dest = label.incoming_transaction.warehouse_to if label.incoming_transaction_id else None
    if dest and dest.code not in SCM_ONLY_WAREHOUSE_CODES:
        return dest
    if len(holders) == 1:
        return Warehouse.objects.get(pk=holders[0])
    return raw


def raw_material_part_nos(part_nos):
    """원재료로 보는 품번: 보관관리 품목 설정에 있거나 원재료·파렛트 라벨을 발행한 적이 있는 품목."""
    from .models import RawMaterialLabel, RawMaterialSetting
    part_nos = set(part_nos)
    raw = set(RawMaterialSetting.objects.filter(part__part_no__in=part_nos).values_list('part__part_no', flat=True))
    raw |= set(RawMaterialLabel.objects.filter(part_no__in=part_nos).values_list('part_no', flat=True))
    return raw


def lookup(label_id):
    """라벨 번호 → 화면에서 쓰는 정보. 없으면 None."""
    from .models import ProcessTag, RawMaterialLabel
    label_id = scan_id(label_id)
    if label_id.startswith(RAW_PREFIXES):
        label = RawMaterialLabel.objects.select_related('warehouse').filter(label_id=label_id).first()
        if not label:
            return None
        return {
            'kind': 'PLT' if label.label_type == 'PALLET' else 'RM', 'id': label.pk, 'label_id': label.label_id,
            'part_no': label.part_no, 'part_name': label.part_name, 'quantity': float(label.quantity),
            'unit': label.get_unit_display(), 'lot_no': label.lot_no.isoformat() if label.lot_no else '',
            'expiry_date': label.expiry_date.isoformat() if label.expiry_date else '',
            'warehouse': label.warehouse.code if label.warehouse_id else '',
            'status': label.status, 'status_display': label.get_status_display(),
        }
    tag = ProcessTag.objects.filter(tag_id=label_id).first() if label_id else None
    if tag:
        return {
            'kind': 'DLV' if label_id.startswith('DLV-') else 'TAG', 'id': tag.pk, 'label_id': tag.tag_id, 'part_no': tag.part_no, 'part_name': tag.part_name,
            'quantity': float(tag.quantity), 'unit': '', 'lot_no': tag.lot_no.isoformat() if tag.lot_no else '',
            'expiry_date': '', 'warehouse': tag.used_warehouse.code if tag.used_warehouse_id else '',
            'status': tag.status, 'status_display': tag.get_status_display(),
        }
    return None  # 등록되지 않은 번호


def sync_labels(part, lot_no=None):
    """한 품목(필요하면 한 LOT)의 라벨을 창고별 실재고에 맞춘다. (옮긴 수, 소진 수)를 돌려준다."""
    from .models import MaterialStock, RawMaterialLabel
    labels = RawMaterialLabel.objects.filter(part=part, status__in=LIVE, warehouse__isnull=False)
    if lot_no is not None:
        labels = labels.filter(lot_no=lot_no)
    by_lot = defaultdict(list)
    for label in labels.order_by('-printed_at', '-pk'):  # 최근 라벨부터: 오래된 포대가 먼저 쓰인다
        by_lot[label.lot_no].append(label)
    moved = consumed = 0
    now = timezone.now()
    for lot, lot_labels in by_lot.items():
        stock = {wh: q for wh, q in MaterialStock.objects.filter(part=part, lot_no=lot, quantity__gt=0)
                 .values_list('warehouse_id').annotate(t=Sum('quantity'))}
        kept_qty = defaultdict(Decimal)
        surplus = []
        for label in lot_labels:
            if kept_qty[label.warehouse_id] < stock.get(label.warehouse_id, 0):  # 일부만 쓴 포대도 유지
                kept_qty[label.warehouse_id] += label.quantity
            else:
                surplus.append(label)
        # 라벨보다 재고가 많은 창고로 남는 라벨을 옮긴다 (ERP에서 직접 이동한 경우 등)
        need = {wh: q - kept_qty[wh] for wh, q in stock.items() if q > kept_qty[wh]}
        for label in reversed(surplus[:]):  # 오래된 라벨부터 옮긴다
            target = next((wh for wh, q in need.items() if q > 0), None)
            if target is None:
                break
            if RawMaterialLabel.objects.filter(pk=label.pk, warehouse_id=label.warehouse_id, status__in=LIVE).update(
                    warehouse_id=target, status='INSTOCK') != 1:
                continue  # 그 사이 스캔으로 옮겨진 라벨
            need[target] -= label.quantity
            surplus.remove(label)
            moved += 1
        for label in surplus:
            done = RawMaterialLabel.objects.filter(pk=label.pk, warehouse_id=label.warehouse_id, status__in=LIVE)
            if done.update(status='USED') == 1:
                RawMaterialLabel.objects.filter(pk=label.pk, used_at__isnull=True).update(used_at=now)
                consumed += 1
    return moved, consumed


def sync_all():
    """재고 반영이 끝난 뒤 한 번: 살아 있는 라벨이 있는 모든 품목을 맞춘다."""
    from orders.models import Part
    from .models import RawMaterialLabel
    part_ids = RawMaterialLabel.objects.filter(status__in=LIVE, warehouse__isnull=False).values_list(
        'part_id', flat=True).distinct()
    moved = consumed = 0
    for part in Part.objects.filter(pk__in=list(part_ids)):
        with transaction.atomic():  # 품목 단위로 한 번에 커밋 (라벨 수천 장도 빠르게)
            m, c = sync_labels(part)
        moved += m
        consumed += c
    return {'moved': moved, 'consumed': consumed}
