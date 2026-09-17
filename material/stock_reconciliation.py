"""Reconcile explicit ERP balances without discarding LOT identity or audit."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from orders.models import Part
from .models import MaterialStock, ProcessTag, StockSyncRun, Warehouse
from .stock_safety import deduct_stock, unassigned_stock


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
    balances = defaultdict(int)
    if not isinstance(items, list) or not items:
        raise ValueError('ERP 재고 응답이 비어 있거나 올바르지 않습니다. 기존 재고를 유지합니다.')
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('ERP 재고 응답 행 형식이 올바르지 않습니다.')
        wh, part = item.get('whCd'), item.get('itemCd')
        if not wh or not part or item.get('invQt1') in (None, ''):
            raise ValueError('ERP 재고 응답에 창고·품번·수량이 누락되었습니다. 기존 재고를 유지합니다.')
        try:
            qty = Decimal(str(item['invQt1']))
            if not qty.is_finite():
                raise ValueError('ERP 재고 수량을 확인할 수 없습니다.')
        except InvalidOperation as exc:
            raise ValueError('ERP 재고 수량 형식이 올바르지 않습니다.') from exc
        # MaterialStock.quantity is an IntegerField; retain the existing ERP
        # import conversion instead of changing quantity units in this repair.
        balances[(str(wh), str(part))] += int(qty)
    return balances


def _reconcile_key(warehouse, part, erp_qty, expected):
    from .erp_api import _create_trx, fifo_sort_key
    changes = dict(adjusted=0, increased=0, decreased=0, created=0,
                   lot_trimmed=0, negative_normalized=0)
    with transaction.atomic():
        Part.objects.select_for_update().get(pk=part.pk)
        rows = list(MaterialStock.objects.select_for_update().filter(
            warehouse=warehouse, part=part,
        ).order_by('pk'))
        actual = {s.pk: (s.quantity, s.lot_no, s.production_lot) for s in rows}
        if actual != expected:
            raise ValueError('ERP 조회 중 SCM 재고가 변경되어 보정을 보류했습니다.')
        now = timezone.now()

        def audit(row, delta, reason, kind='LOT_CORRECT'):
            _create_trx(
                transaction_type=kind, date=now, part=part,
                warehouse_to=warehouse if delta > 0 else None,
                warehouse_from=warehouse if delta < 0 else None,
                lot_no=row.lot_no, production_lot=row.production_lot,
                quantity=abs(delta) if kind.startswith('ADJ_ERP') else delta,
                result_stock=row.quantity, remark=reason,
            )

        # Move historical LOT deficits into the unassigned bucket as a balanced
        # pair. This step alone never changes the warehouse/part total.
        assigned = [s for s in rows if s.lot_no is not None or s.production_lot is not None]
        bucket = next((s for s in rows if s.lot_no is None and s.production_lot is None), None)
        negatives = [s for s in assigned if s.quantity < 0]
        positive_total = sum(max(s.quantity, 0) for s in assigned)
        if not negatives and positive_total <= erp_qty and (bucket.quantity if bucket else 0) == erp_qty - positive_total:
            return changes
        if bucket is None:
            bucket = unassigned_stock(warehouse, part)
            changes['created'] += 1
        for row in negatives:
            deficit = -row.quantity
            old_qty = row.quantity
            row.quantity = 0
            row.save(update_fields=['quantity'])
            bucket.quantity -= deficit
            bucket.save(update_fields=['quantity'])
            reason = f'음수 LOT 정리 (재고행={row.pk}, {old_qty}→0, 미지정 역보정, 총량 유지)'
            audit(row, deficit, reason)
            audit(bucket, -deficit, reason)
            changes['negative_normalized'] += 1

        # Retain actual production batches even when their date is unknown.
        excess = max(positive_total - erp_qty, 0)
        for row in sorted((s for s in assigned if s.quantity > 0), key=fifo_sort_key):
            if excess <= 0:
                break
            take = min(row.quantity, excess)
            deduct_stock(row, take)
            row.quantity -= take
            excess -= take
            audit(row, -take, f'ERP정합 LOT축소 (ERP={erp_qty}, FIFO 차감={take})')
            changes['lot_trimmed'] = 1

        target = erp_qty - sum(s.quantity for s in assigned)
        diff = target - bucket.quantity
        if diff:
            before = bucket.quantity
            bucket.quantity = target
            bucket.save(update_fields=['quantity'])
            direction = 'IN' if diff > 0 else 'OUT'
            audit(bucket, diff, f'ERP 재고동기화 (ERP={erp_qty}, 미지정:{before}→{target}, diff={diff:+d})',
                  kind=f'ADJ_ERP_{direction}')
            changes['increased' if diff > 0 else 'decreased'] += 1
            if diff < 0:
                # Only mark tags consumed from this source warehouse.
                tags = ProcessTag.objects.filter(
                    part_no=part.part_no, status='USED', stock_reflected=False,
                    used_transaction__warehouse_from=warehouse,
                ).order_by('used_at', 'pk')
                remaining = -diff
                for tag in tags:
                    if remaining < tag.quantity:
                        break
                    ProcessTag.objects.filter(pk=tag.pk).update(stock_reflected=True)
                    remaining -= tag.quantity
        changes['adjusted'] = 1
    return changes


def reconcile_stock():
    from .erp_api import fetch_erp_stock
    result = dict(adjusted=0, increased=0, decreased=0, created=0, lot_trimmed=0,
                  negative_normalized=0, skipped_no_part=0, skipped_no_wh=0,
                  skipped_missing=0, skipped_changed=0, skipped_negative=0, error=None)
    run = StockSyncRun.objects.create()
    try:
        # Compare under row locks after the network request; never apply a total
        # to a balance that changed while the ERP response was in flight.
        snapshot = stock_snapshot()
        cache.set('erp_sync_progress', {'stage': 'ERP 현재고 조회 중...', 'percent': 5}, 600)
        ok, items, error = fetch_erp_stock(year=str(timezone.localdate().year), month=None, total_fg='0')
        if not ok:
            raise ValueError(error or 'ERP 현재고 조회 실패')
        balances = parse_balances(items)
        parts = {p.part_no: p for p in Part.objects.all()}
        warehouses = {w.code: w for w in Warehouse.objects.all()}
        # Missing is not zero. Only explicit balances (including explicit 0)
        # authorize a correction; unknown API completeness cannot erase stock.
        missing = [key for key, rows in snapshot.items()
                   if key not in balances and any(value[0] != 0 for value in rows.values())]
        result['skipped_missing'] = len(missing)
        issues = [f'ERP 응답 누락 {len(missing)}건: 기존 재고 유지'] if missing else []
        for index, ((wh, code), qty) in enumerate(sorted(balances.items())):
            if wh not in warehouses:
                result['skipped_no_wh'] += 1
                continue
            if code not in parts:
                result['skipped_no_part'] += 1
                continue
            if qty < 0:
                result['skipped_negative'] += 1
                issues.append(f'{wh}/{code}: ERP 음수 재고 보정 보류')
                continue
            expected = snapshot.get((wh, code), {})
            if all(value[0] >= 0 for value in expected.values()) and sum(value[0] for value in expected.values()) == qty:
                continue  # no write needed; avoid a transaction for every unchanged ERP row
            try:
                changes = _reconcile_key(warehouses[wh], parts[code], qty, expected)
            except ValueError as exc:
                result['skipped_changed'] += 1
                issues.append(f'{wh}/{code}: {exc}')
                continue
            for key, count in changes.items():
                result[key] += count
            if index % 100 == 0:
                cache.set('erp_sync_progress', {
                    'stage': f'총량 보정 중 ({index + 1}/{len(balances)})',
                    'percent': 10 + int(85 * (index + 1) / len(balances)),
                }, 600)
        if result['skipped_no_part'] or result['skipped_no_wh']:
            issues.append(f'마스터 미등록: 품목 {result["skipped_no_part"]}건, 창고 {result["skipped_no_wh"]}건')
        result['error'] = '; '.join(issues[:10]) or None
        run.status = 'partial' if issues else 'success'
    except Exception as exc:
        result['error'] = str(exc)
        run.status = 'failed'
    finally:
        run.finished_at = timezone.now()
        run.summary = result
        run.error = result['error'] or ''
        run.save(update_fields=['finished_at', 'summary', 'error', 'status'])
        cache.set('erp_sync_progress', {
            'stage': '확인 필요' if result['error'] else '총량 보정 완료',
            'percent': 100, 'error': result['error'],
            'detail': result['error'] or f'조정 {result["adjusted"]}건',
        }, 600)
    return result
