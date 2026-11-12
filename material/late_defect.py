"""합격(또는 무검사) 입고가 끝난 뒤 발견한 불량을 일부 수량만 부적합으로 돌린다.

입고 취소나 판정 전체 정정과 달리 입고 기록과 나머지 양품은 그대로 둔다.
  1) 불량이 있는 창고의 재고 → (다르면) 입고 창고로 회수 → 부적합창고(8200)
  2) 검사 기록·SCM 입고 집계의 양품 수량을 줄이고 불량 수량을 늘린다
  3) 반출 대기 건을 만들어(또는 늘려) 기존 반출 → 협력사 인수 확인 → 발주 잔량 복구 흐름에 태운다
  4) ERP 입고 수량을 줄인다(기존 판정 정정과 같은 방식: ERP 입고 삭제 후 줄어든 수량으로 재등록)
"""
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import MaterialStock, MaterialTransaction, Warehouse
from .stock_reconciliation import SCM_ONLY_WAREHOUSE_CODES
from .stock_safety import deduct_stock, quantity_change

RECEIPT_TYPES = ('IN_SCM', 'IN_MANUAL')
BAD_WAREHOUSE = '8200'
OK_REMARK = '[수입검사] 양품 입고'
LATE_REMARK = '[사후 불량]'


class LateDefectError(ValueError):
    pass


def _inspection(origin):
    from qms.models import ImportInspection
    return ImportInspection.objects.filter(inbound_transaction=origin).first()


def _return_case(origin):
    from orders.models import ReturnLog
    return ReturnLog.objects.filter(source_incoming=origin).first()


def erp_receipt_quantity(erp_trx):
    """ERP에 등록되어 있어야 할 이 입고의 수량 = 합격 후 불량으로 돌린 수량을 뺀 양품 수량.

    단가 재반영·입고일 수정처럼 ERP 입고를 지우고 다시 등록하는 곳은 모두 이 값을 써야 한다.
    수불 기록의 수량(처음 합격 수량)을 그대로 쓰면 불량 등록으로 줄여 둔 ERP 수량이 되돌아간다.
    """
    origin = erp_trx.source_incoming if erp_trx.source_incoming_id else erp_trx
    inspection = _inspection(origin)
    if inspection and erp_trx.pk != origin.pk:   # 검사 합격 이동
        return inspection.qty_good if (erp_trx.remark or '').startswith(OK_REMARK) else erp_trx.quantity
    case = None if inspection else _return_case(origin)
    return erp_trx.quantity - (case.quantity if case else 0)


def receipt_state(origin):
    """이 입고에서 지금 불량으로 돌릴 수 있는 범위를 계산한다 (화면과 처리 공용)."""
    if origin.transaction_type not in RECEIPT_TYPES:
        raise LateDefectError('SCM·수기 입고 건만 불량 등록을 할 수 있습니다. ERP에서 들어온 입고는 ERP에서 처리해 주세요.')
    inspection = _inspection(origin)
    if inspection and inspection.status == 'PENDING':
        raise LateDefectError('아직 검사 판정 전입니다. 수입검사 화면에서 불량 수량을 판정해 주세요.')
    case = _return_case(origin)
    if inspection:
        accepted = inspection.qty_good
        erp_trx = (MaterialTransaction.objects.filter(
            transaction_type='TRANSFER', source_incoming=origin, remark__startswith=OK_REMARK)
            .select_related('warehouse_to').order_by('pk').first())
        if accepted > 0 and erp_trx is None:
            raise LateDefectError('검사 합격 입고 기록을 찾을 수 없습니다. 과거 자료는 담당자 확인이 필요합니다.')
        target_wh = erp_trx.warehouse_to if erp_trx else origin.warehouse_to
    else:
        accepted = origin.quantity - (case.quantity if case else 0)
        erp_trx, target_wh = origin, origin.warehouse_to
    stocks = list(MaterialStock.objects.select_related('warehouse').filter(
        part_id=origin.part_id, lot_no=origin.lot_no, production_lot=origin.production_lot, quantity__gt=0)
        .exclude(warehouse__code__in=SCM_ONLY_WAREHOUSE_CODES).order_by('warehouse__code'))
    return {'inspection': inspection, 'case': case, 'accepted': accepted, 'erp_trx': erp_trx,
            'target_wh': target_wh, 'stocks': stocks}


def register_late_defect(origin_id, stock_id, quantity, reason, user, request_key):
    """불량 수량을 부적합으로 돌린다. 돌려주는 값: (반출 건, ERP로 보낼 회수 이동 또는 None, 이미 처리된 요청 여부)."""
    from orders.models import DeliveryOrder, Incoming, ReturnLog
    from orders.returns import refresh_delivery_status
    from .erp_api import delete_erp_incoming, register_erp_incoming
    from .erp_outbox import guard_receipt_change
    from .incoming import delivery_item_for_incoming

    try:
        key = uuid.UUID(str(request_key)).hex[:24]
    except (ValueError, TypeError):
        raise LateDefectError('요청을 확인할 수 없습니다. 화면을 새로 고친 뒤 다시 시도해 주세요.')
    reason = (reason or '').strip()
    if not reason:
        raise LateDefectError('불량 사유를 입력해 주세요.')
    if quantity <= 0:
        raise LateDefectError('불량 수량은 1개 이상이어야 합니다.')

    with transaction.atomic():
        # 같은 입고에 대한 정정·취소·불량 등록이 겹치지 않도록 원본 입고 행을 먼저 잠근다.
        if MaterialTransaction.objects.filter(pk=origin_id).update(quantity=F('quantity')) != 1:
            raise LateDefectError('입고 건을 찾을 수 없습니다.')
        origin = MaterialTransaction.objects.select_related('part', 'warehouse_to', 'vendor').get(pk=origin_id)
        ng_no = f'LDF-{key}'
        if MaterialTransaction.objects.filter(transaction_no=ng_no).exists():
            return _return_case(origin), None, True  # 같은 요청의 재전송

        state = receipt_state(origin)
        inspection, case, target_wh, erp_trx = state['inspection'], state['case'], state['target_wh'], state['erp_trx']
        if quantity > state['accepted']:
            raise LateDefectError(f"이 입고의 남은 양품 수량({int(state['accepted']):,}개)보다 많이 등록할 수 없습니다.")
        source = MaterialStock.objects.select_for_update().select_related('warehouse').filter(
            pk=stock_id, part_id=origin.part_id, lot_no=origin.lot_no, production_lot=origin.production_lot).first()
        if source is None or source.warehouse.code in SCM_ONLY_WAREHOUSE_CODES:
            raise LateDefectError('불량이 있는 창고를 다시 선택해 주세요.')
        if source.quantity < quantity:
            raise LateDefectError(f'{source.warehouse.name}의 이 LOT 재고({source.quantity.normalize():f}개)가 부족합니다.')
        bad_wh = Warehouse.objects.filter(code=BAD_WAREHOUSE).first()
        if bad_wh is None:
            raise LateDefectError('부적합창고(8200)가 등록되어 있지 않습니다.')
        try:
            delivery_item = delivery_item_for_incoming(origin)
        except ValueError:
            delivery_item = None  # 과거 자료: 납품 품목 연결 없이 진행
        now = timezone.now()
        common = dict(date=now, part=origin.part, lot_no=origin.lot_no, production_lot=origin.production_lot,
                      quantity=quantity, vendor=origin.vendor, actor=user, source_incoming=origin,
                      delivery_item=delivery_item, ref_delivery_order=origin.ref_delivery_order)

        # 1) 불량이 현장 등 다른 창고에 있으면 먼저 입고 창고로 회수한다 (ERP에도 같은 이동을 보낸다).
        erp_move = None
        target_stock, _ = MaterialStock.objects.select_for_update().get_or_create(
            warehouse=target_wh, part=origin.part, lot_no=origin.lot_no, production_lot=origin.production_lot,
            defaults={'quantity': 0})
        if source.warehouse_id != target_wh.pk:
            deduct_stock(source, quantity)
            MaterialStock.objects.filter(pk=target_stock.pk).update(quantity=quantity_change(quantity))
            target_stock.refresh_from_db()
            erp_move = MaterialTransaction.objects.create(
                transaction_no=f'LDM-{key}', transaction_type='TRANSFER', warehouse_from=source.warehouse,
                warehouse_to=target_wh, result_stock=target_stock.quantity,
                remark=f'{LATE_REMARK} 입고 창고로 회수'[:200], **common)

        # 2) 입고 창고 → 부적합창고
        deduct_stock(target_stock, quantity)
        bad_stock, _ = MaterialStock.objects.get_or_create(
            warehouse=bad_wh, part=origin.part, lot_no=origin.lot_no, production_lot=origin.production_lot,
            defaults={'quantity': 0})
        MaterialStock.objects.filter(pk=bad_stock.pk).update(quantity=quantity_change(quantity))
        bad_stock.refresh_from_db()
        MaterialTransaction.objects.create(
            transaction_no=ng_no, transaction_type='TRANSFER', warehouse_from=target_wh, warehouse_to=bad_wh,
            result_stock=bad_stock.quantity, remark=f'{LATE_REMARK} 격리 (사유: {reason})'[:200], **common)

        # 3) 검사 기록과 SCM 입고 집계
        if inspection:
            inspection.correction_history.append({
                'at': now.isoformat(), 'actor': user.get_username(), 'type': 'late_defect',
                'reason': f'합격 후 불량 {quantity}개 등록: {reason}',
                'qty_good': inspection.qty_good, 'qty_bad': inspection.qty_bad, 'remark': inspection.remark or '',
            })
            inspection.qty_good -= quantity
            inspection.qty_bad += quantity
            inspection.status = 'APPROVED' if inspection.qty_good > 0 else 'REJECTED'
            inspection.save(update_fields=['qty_good', 'qty_bad', 'status', 'correction_history'])
        if origin.ref_delivery_order:
            summary = Incoming.objects.select_for_update().filter(
                delivery_order_no=origin.ref_delivery_order, part_id=origin.part_id).first()
            if summary and summary.confirmed_qty is not None:
                summary.confirmed_qty = max(0, summary.confirmed_qty - quantity)
                summary.save(update_fields=['confirmed_qty'])

        # 4) 반출 대기 건 (입고 1건당 1건이므로 이미 있으면 수량을 늘린다)
        if case:
            case.quantity += quantity
            case.is_confirmed, case.confirmed_at = False, None
            case.reason = f'{case.reason} / {LATE_REMARK} {reason}'.strip(' /')[:255]
            case.save(update_fields=['quantity', 'is_confirmed', 'confirmed_at', 'reason'])
        else:
            delivery = (DeliveryOrder.objects.filter(order_no=origin.ref_delivery_order).first()
                        if origin.ref_delivery_order else None)
            case = ReturnLog.objects.create(
                delivery_order=delivery, delivery_item=delivery_item, source_incoming=origin, part=origin.part,
                quantity=quantity, reason=f'{LATE_REMARK} {reason}'[:255])

        # 5) ERP 입고 수량 줄이기 (ERP에 등록된 적이 있는 입고만)
        if erp_trx is not None:
            had_pending = guard_receipt_change(erp_trx)
            if erp_trx.erp_incoming_no or had_pending:
                if erp_trx.erp_incoming_no:
                    ok, err = delete_erp_incoming(erp_trx.erp_incoming_no)
                    if not ok:
                        raise LateDefectError(f'ERP 입고 수량을 줄이지 못했습니다: {err}')
                    erp_trx.erp_incoming_no, erp_trx.erp_sync_status = None, 'PENDING'
                    erp_trx.save(update_fields=['erp_incoming_no', 'erp_sync_status'])
                remaining = state['accepted'] - quantity
                if remaining > 0:
                    ok, _, err = register_erp_incoming(
                        erp_trx, remaining, target_wh.code,
                        erp_order_no=(delivery_item.erp_order_no or '') if delivery_item else '',
                        erp_order_seq=(delivery_item.erp_order_seq or '') if delivery_item else '')
                    if not ok and not erp_trx.erp_operations.filter(kind='REGISTER', status='PENDING').exists():
                        raise LateDefectError(f'ERP 입고 재등록 요청을 저장하지 못했습니다: {err}')

        if origin.ref_delivery_order:
            delivery = DeliveryOrder.objects.filter(order_no=origin.ref_delivery_order).first()
            if delivery:
                refresh_delivery_status(delivery)
    return case, erp_move, False


def can_register_late_defect(user):
    """내부 직원 중 입고 처리·불량 반출·수입검사 판정 권한자."""
    from orders.access import has_permission, vendor_identity
    if not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    if vendor_identity(user)[0]:
        return False
    return any(has_permission(user, perm) for perm in
               ('can_wms_incoming_process', 'can_wms_stock_return', 'can_qms_inspection_edit'))


@login_required
def late_defect(request, trx_id):
    """[WMS] 합격 후 불량 등록 화면."""
    if not can_register_late_defect(request.user):
        messages.error(request, '불량 등록 권한이 없습니다.')
        return redirect('material:incoming_history')
    origin = get_object_or_404(MaterialTransaction.objects.select_related('part', 'warehouse_to', 'vendor'), pk=trx_id)
    if origin.source_incoming_id:  # 검사 이동 행에서 들어온 경우 원본 입고로
        return redirect('material:late_defect', trx_id=origin.source_incoming_id)

    if request.method == 'POST':
        try:
            quantity = int(str(request.POST.get('quantity', '')).strip() or 0)
            stock_id = int(request.POST.get('stock_id') or 0)
        except (ValueError, TypeError):
            messages.error(request, '불량 수량은 정수로 입력해 주세요.')
            return redirect('material:late_defect', trx_id=origin.pk)
        try:
            case, erp_move, duplicate = register_late_defect(
                origin.pk, stock_id, quantity, request.POST.get('reason', ''), request.user,
                request.POST.get('request_key', ''))
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('material:late_defect', trx_id=origin.pk)
        if duplicate:
            messages.info(request, '이미 처리된 요청입니다.')
        else:
            notice = (f'불량 {quantity:,}개를 부적합창고로 옮기고 반출 대기 건에 등록했습니다. '
                      f'[불량 반품 처리]에서 반출하면 협력사 인수 확인 후 발주 잔량이 복구됩니다.')
            if erp_move is not None:  # 회수 이동은 커밋 뒤에 ERP로 보낸다 (재고이동 화면과 같은 방식)
                try:
                    from .erp_api import register_erp_stock_move
                    ok, _, err = register_erp_stock_move(
                        erp_move, quantity, erp_move.warehouse_from.code, erp_move.warehouse_to.code)
                    if not ok and err:
                        notice += f' (ERP 이동 연동: {err})'
                except Exception as exc:  # ERP 장애가 등록 자체를 되돌리지는 않는다
                    notice += f' (ERP 이동 연동 예외: {exc})'
            messages.success(request, notice)
        return redirect('material:late_defect', trx_id=origin.pk)

    context = {'origin': origin, 'request_key': str(uuid.uuid4()), 'error': None}
    try:
        context.update(receipt_state(origin))
    except ValueError as exc:
        context['error'] = str(exc)
    history = list(MaterialTransaction.objects.select_related('warehouse_from', 'actor').filter(
        source_incoming=origin, remark__startswith=LATE_REMARK, warehouse_to__code=BAD_WAREHOUSE).order_by('-date'))
    # 불량을 발견한 창고: 회수 이동(LDM-)이 있으면 그 출발 창고, 없으면 입고 창고에서 바로 격리한 것
    recalls = {t.transaction_no[4:]: t for t in MaterialTransaction.objects.select_related('warehouse_from').filter(
        source_incoming=origin, transaction_no__startswith='LDM-')}
    for trx in history:
        recall = recalls.get(trx.transaction_no[4:])
        trx.found_warehouse = recall.warehouse_from if recall else trx.warehouse_from
    context['history'] = history
    return render(request, 'material/late_defect.html', context)
