"""Reconcile explicit ERP balances without discarding LOT identity or audit."""
from collections import defaultdict
from decimal import Decimal

from django.core.cache import cache
from django.db import DatabaseError, transaction
from django.utils import timezone

from orders.models import Part
from .models import MaterialStock, ProcessTag, StockSyncRun, Warehouse
from .stock_safety import unassigned_stock
from .quantities import stock_quantity

# Incoming-inspection hold and nonconforming stock never reach ERP (ERP receives
# only the accepted quantity), so ERP balances must not overwrite them.
SCM_ONLY_WAREHOUSE_CODES = ('8100', '8200')


def stock_snapshot():
    snapshots = defaultdict(dict)
    for row in MaterialStock.objects.values(
        'pk', 'warehouse__code', 'part__part_no', 'quantity', 'lot_no', 'production_lot',
    ).iterator():
        snapshots[(row['warehouse__code'], row['part__part_no'])][row['pk']] = (
            row['quantity'], row['lot_no'], row['production_lot'],
        )
    return snapshots


def parse_balances(items):
    balances = defaultdict(Decimal)
    if not isinstance(items, list) or not items:
        raise ValueError('ERP 재고 응답이 비어 있거나 올바르지 않습니다. 기존 재고를 유지합니다.')
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('ERP 재고 응답 행 형식이 올바르지 않습니다.')
        wh, part = item.get('whCd'), item.get('itemCd')
        if not wh or not part or item.get('invQt1') in (None, ''):
            raise ValueError('ERP 재고 응답에 창고·품번·수량이 누락되었습니다. 기존 재고를 유지합니다.')
        qty = stock_quantity(item['invQt1'])
        key = (str(wh), str(part))
        balances[key] = stock_quantity(balances[key] + qty)
    return balances


def lot_managed_part_ids():
    """SCM이 LOT를 책임지는 품목: SCM으로 입고·라벨 발행된 품목 + 릴레이(생산 LOT) 품목.
    나머지는 ERP에 LOT가 없으므로 SCM도 ERP 수량을 그대로 보여준다."""
    from .models import MaterialTransaction, ProductionLotItem, RawMaterialLabel
    ids = set(MaterialTransaction.objects.filter(transaction_type__in=['IN_SCM', 'IN_MANUAL'])
              .values_list('part_id', flat=True).distinct())
    ids |= set(RawMaterialLabel.objects.values_list('part_id', flat=True).distinct())
    ids |= set(ProductionLotItem.objects.values_list('part_id', flat=True))
    ids.discard(None)
    return ids


def pending_part_ids(since):
    """SCM→ERP 전송이 끝나지 않았거나 비교 도중 끝난 품목: ERP에 아직 안 보일 수 있다."""
    from django.db.models import Q
    from .models import ERPIncomingOperation
    q = Q(status__in=['PENDING', 'RUNNING', 'FAILED', 'REVIEW']) | Q(updated_at__gte=since)
    return set(ERPIncomingOperation.objects.filter(q).exclude(transaction__isnull=True)
               .values_list('transaction__part_id', flat=True))


def _mark_tags_consumed(warehouse, part, decrease):
    # Only mark tags consumed from this source warehouse.
    tags = ProcessTag.objects.filter(
        part_no=part.part_no, status='USED', stock_reflected=False,
        used_transaction__warehouse_from=warehouse,
    ).order_by('used_at', 'pk')
    remaining = decrease
    for tag in tags:
        if remaining < tag.quantity:
            break
        ProcessTag.objects.filter(pk=tag.pk).update(stock_reflected=True)
        remaining -= tag.quantity


def _locked_rows(warehouse, part, expected):
    Part.objects.select_for_update().get(pk=part.pk)
    rows = list(MaterialStock.objects.select_for_update().filter(warehouse=warehouse, part=part).order_by('pk'))
    if {s.pk: (s.quantity, s.lot_no, s.production_lot) for s in rows} != expected:
        raise ValueError('ERP 조회 중 SCM 재고가 변경되어 보정을 보류했습니다.')
    return rows


def _mirror_quantity_only(warehouse, part, erp_qty, expected):
    """ERP에 LOT가 없는 품목: SCM 재고 = ERP 현재고. 보정 기록을 남기지 않는다 (가비지 방지)."""
    with transaction.atomic():
        rows = _locked_rows(warehouse, part, expected)
        before = sum((s.quantity for s in rows), Decimal(0))
        bucket = next((s for s in rows if s.lot_no is None and s.production_lot is None), None)
        if bucket is None:
            bucket = unassigned_stock(warehouse, part)
        MaterialStock.objects.filter(pk=bucket.pk).update(quantity=erp_qty)
        MaterialStock.objects.filter(warehouse=warehouse, part=part).exclude(pk=bucket.pk).delete()
        if erp_qty < before:
            _mark_tags_consumed(warehouse, part, before - erp_qty)
    return erp_qty - before


def _settle_lot_managed(warehouse, part, erp_qty, expected):
    """SCM이 LOT를 책임지는 품목: LOT는 깎지 않는다. 설명 안 되는 차이만 미지정(LOT 미확인)에
    넣고 기록 1건을 남긴다. 총량은 ERP와 같아진다."""
    from .erp_api import _create_trx
    with transaction.atomic():
        rows = _locked_rows(warehouse, part, expected)
        lots = [s for s in rows if s.lot_no is not None or s.production_lot is not None]
        bucket = next((s for s in rows if s.lot_no is None and s.production_lot is None), None)
        if bucket is None:
            bucket = unassigned_stock(warehouse, part)
        target = erp_qty - sum((s.quantity for s in lots), Decimal(0))
        diff = target - bucket.quantity
        if not diff:
            return diff
        MaterialStock.objects.filter(pk=bucket.pk).update(quantity=target)
        _create_trx(
            transaction_type='ADJ_ERP_IN' if diff > 0 else 'ADJ_ERP_OUT', date=timezone.now(), part=part,
            warehouse_to=warehouse if diff > 0 else None, warehouse_from=warehouse if diff < 0 else None,
            quantity=abs(diff), result_stock=target,
            remark=f'ERP 차이 → LOT 미확인 (ERP={erp_qty.normalize():f}, 차이={diff.normalize():+f})',
        )
        if diff < 0:
            _mark_tags_consumed(warehouse, part, -diff)
    return diff


def reconcile_stock():
    """ERP 현재고 반영. 수량만 품목은 ERP 그대로(기록 없음), LOT 관리 품목은 LOT를 건드리지 않고
    차이만 'LOT 미확인'으로 남긴다. ERP로 보내는 중인 품목은 다음 회차로 미룬다."""
    from .erp_api import fetch_erp_stock
    result = dict(checked=0, adjusted=0, mirrored=0, lot_diffs=[], pending=[], changed=[],
                  missing=[], no_master=[], error=None, status='running')
    run = StockSyncRun.objects.create()
    try:
        snapshot_at = timezone.now()
        # Compare under row locks after the network request; never apply a total
        # to a balance that changed while the ERP response was in flight.
        snapshot = stock_snapshot()
        lot_managed = lot_managed_part_ids()
        cache.set('erp_sync_progress', {'stage': 'ERP 현재고 조회 중...', 'percent': 5}, 600)
        ok, items, error = fetch_erp_stock(year=str(timezone.localdate().year), month=None, total_fg='0')
        if not ok:
            raise ValueError(error or 'ERP 현재고 조회 실패')
        balances = parse_balances(items)
        pending = pending_part_ids(snapshot_at)
        parts = {p.part_no: p for p in Part.objects.all()}
        warehouses = {w.code: w for w in Warehouse.objects.all()}
        # Missing is not zero. Only explicit balances (including explicit 0)
        # authorize a correction; unknown API completeness cannot erase stock.
        result['missing'] = sorted(f'{wh}/{code}' for (wh, code), rows in snapshot.items()
                                   if wh not in SCM_ONLY_WAREHOUSE_CODES and (wh, code) not in balances
                                   and sum((value[0] for value in rows.values()), Decimal(0)) != 0)
        for index, ((wh, code), qty) in enumerate(sorted(balances.items())):
            if wh in SCM_ONLY_WAREHOUSE_CODES:
                continue
            if wh not in warehouses or code not in parts:
                result['no_master'].append(f'{wh}/{code}')
                continue
            result['checked'] += 1
            expected = snapshot.get((wh, code), {})
            part = parts[code]
            rows = expected.values()
            # 일치하면 쓰지 않는다. 수량만 품목은 음수가 미지정 칸에만 있을 때 일치로 본다 (ERP 음수 재고).
            if sum((value[0] for value in rows), Decimal(0)) == qty and (part.pk in lot_managed or all(
                    value[0] >= 0 or (value[1] is None and value[2] is None) for value in rows)):
                continue
            if part.pk in pending:
                result['pending'].append(f'{wh}/{code}')
                continue
            try:
                if part.pk in lot_managed:
                    diff = _settle_lot_managed(warehouses[wh], part, qty, expected)
                    if diff:
                        result['lot_diffs'].append({'wh': wh, 'part_no': code, 'name': part.part_name,
                                                    'erp': float(qty), 'diff': float(diff)})
                else:
                    _mirror_quantity_only(warehouses[wh], part, qty, expected)
                    result['mirrored'] += 1
                result['adjusted'] += 1
            except (ValueError, DatabaseError) as exc:
                # One locked or changed key must not stop the remaining ERP balances.
                result['changed'].append(f'{wh}/{code}: {exc}')
            if index % 100 == 0:
                cache.set('erp_sync_progress', {
                    'stage': f'ERP 현재고 반영 중 ({index + 1}/{len(balances)})',
                    'percent': 10 + int(85 * (index + 1) / len(balances)),
                }, 600)
        # 응답 누락·마스터 미등록은 매번 같은 고정 항목이라 실패로 보지 않고 목록으로만 남긴다.
        result['error'] = '; '.join(result['changed'][:10]) or None
        run.status = 'partial' if result['changed'] else 'success'
    except Exception as exc:
        result['error'] = str(exc)
        run.status = 'failed'
    finally:
        # partial = applied with items left for review; failed = nothing verified to apply.
        result['status'] = run.status
        run.finished_at = timezone.now()
        run.summary = result
        run.error = result['error'] or ''
        run.save(update_fields=['finished_at', 'summary', 'error', 'status'])
        cache.set('erp_sync_progress', {
            'stage': '확인 필요' if result['error'] else 'ERP 현재고 반영 완료',
            'percent': 100, 'error': result['error'],
            'detail': result['error'] or f'반영 {result["adjusted"]}건',
        }, 600)
    return result
