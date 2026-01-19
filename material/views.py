# material/views.py

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator  # 페이징 처리
from django.db import transaction
from django.db.models import F, Sum, Q
from django.utils import timezone

# SCM(Orders) 앱 모델
from orders.models import Part, Vendor, Inventory as OldInventory

# WMS(Material) 앱 모델
from .models import Warehouse, MaterialStock, MaterialTransaction

from django.http import JsonResponse

# [신규] QMS 앱 모델 (수입검사 연동용)
try:
    from qms.models import ImportInspection
except ImportError:
    ImportInspection = None

# =============================================================================
# 1. 대시보드 및 재고 조회
# =============================================================================

@login_required
def dashboard(request):
    """자재 관리 대시보드"""
    return render(request, 'material/dashboard.html')


@login_required
def stock_list(request):
    """
    [WMS] 자재 재고 현황 조회
    """
    # 1. 파라미터 수신
    search_triggered = request.GET.get('search_triggered', '')
    q = request.GET.get('q', '')
    part_group = request.GET.get('part_group', '')
    warehouse_id = request.GET.get('warehouse_id', '')
    view_mode = request.GET.get('view_mode', 'detail')

    stock_data = []

    # 2. 검색 버튼이 눌렸을 때만 DB 조회 실행
    if search_triggered == 'yes':
        # 수량이 0보다 큰 것만 가져옴
        stocks = MaterialStock.objects.select_related('warehouse', 'part', 'part__vendor').filter(quantity__gt=0)

        # 검색 및 필터 적용
        if q:
            stocks = stocks.filter(Q(part__part_no__icontains=q) | Q(part__part_name__icontains=q))

        if part_group:
            stocks = stocks.filter(part__part_group=part_group)

        if warehouse_id:
            stocks = stocks.filter(warehouse_id=warehouse_id)

        # 3. 조회 모드에 따른 데이터 가공
        if view_mode == 'summary':
            stock_data = stocks.values(
                'part__part_no',
                'part__part_name',
                'part__part_group',
                'part__vendor__name'
            ).annotate(total_qty=Sum('quantity')).order_by('part__part_no')
        else:
            stock_data = stocks.order_by('warehouse__code', 'part__part_no')

    # 4. 필터용 데이터
    part_groups = Part.objects.values_list('part_group', flat=True).distinct().order_by('part_group')
    warehouses = Warehouse.objects.all().order_by('code')

    context = {
        'stocks': stock_data,
        'warehouses': warehouses,
        'part_groups': part_groups,
        'q': q,
        'selected_group': part_group,
        'selected_wh': warehouse_id,
        'view_mode': view_mode,
        'search_triggered': search_triggered,
    }
    return render(request, 'material/stock_list.html', context)


# =============================================================================
# 2. 입고 관리 (Inbound)
# =============================================================================

@login_required
def manual_incoming(request):
    """
    [WMS] 자재 수기 입고 처리
    """
    if request.method == 'POST':
        try:
            date_str = request.POST.get('date', timezone.now().date())
            warehouse_id = request.POST.get('warehouse_id')
            vendor_id = request.POST.get('vendor_id')
            needs_inspection = request.POST.get('needs_inspection')

            part_ids = request.POST.getlist('part_ids[]')
            quantities = request.POST.getlist('quantities[]')
            remarks = request.POST.getlist('remarks[]')

            if not part_ids:
                messages.error(request, "입고할 품목이 리스트에 없습니다.")
                return redirect('material:manual_incoming')

            success_count = 0

            with transaction.atomic():
                warehouse = Warehouse.objects.get(id=warehouse_id)
                vendor = Vendor.objects.get(id=vendor_id) if vendor_id else None

                for i in range(len(part_ids)):
                    p_id = part_ids[i]
                    qty = int(quantities[i])
                    rmk = remarks[i] if i < len(remarks) else ''

                    if qty <= 0:
                        continue

                    part = Part.objects.get(id=p_id)
                    system_remark = "[수입검사 대상] " if needs_inspection else ""
                    final_remark = f"{system_remark}{rmk}".strip()

                    # (1) 재고 증가
                    stock, _ = MaterialStock.objects.get_or_create(
                        warehouse=warehouse,
                        part=part,
                        defaults={'quantity': 0}
                    )
                    stock.quantity = F('quantity') + qty
                    stock.save()

                    # (2) 수불 이력 생성
                    trx_no = f"IN-{timezone.now().strftime('%y%m%d%H%M%S')}-{request.user.id}-{i}"
                    trx_obj = MaterialTransaction.objects.create(
                        transaction_no=trx_no,
                        transaction_type='IN_MANUAL',
                        date=date_str,
                        part=part,
                        quantity=qty,
                        warehouse_to=warehouse,
                        result_stock=stock.quantity,
                        vendor=vendor,
                        actor=request.user,
                        remark=final_remark
                    )

                    # (3) 수입검사 요청
                    if needs_inspection and ImportInspection:
                        ImportInspection.objects.create(
                            inbound_transaction=trx_obj,
                            status='PENDING'
                        )

                    success_count += 1

            if success_count > 0:
                messages.success(request, f"총 {success_count}건 입고 처리가 완료되었습니다.")
            else:
                messages.warning(request, "저장된 항목이 없습니다.")

            return redirect('material:manual_incoming')

        except Exception as e:
            messages.error(request, f"오류 발생: {str(e)}")
            return redirect('material:manual_incoming')

    context = {
        'warehouses': Warehouse.objects.filter(is_active=True).order_by('code'),
        'vendors': Vendor.objects.all().order_by('name'),
        'parts': Part.objects.select_related('vendor').all().order_by('part_no'),
        'today': timezone.now().date(),
    }
    return render(request, 'material/manual_incoming.html', context)


@login_required
def incoming_history(request):
    """
    [입고 내역 조회]
    - 구분은 'SCM입고' / '수기입고'만 표시
    - 수입검사 대기(8100) 입고는 제외
    - 부적합(8200)으로 간 건 제외
    - 수기입고 비고: 입력 비고
    - SCM입고 비고: 납품서번호(ref_delivery_order) (※ TRANSFER에는 ref가 없을 수 있음)
    """
    waiting_wh = Warehouse.objects.filter(code='8100').first()
    if not waiting_wh:
        waiting_wh = Warehouse.objects.filter(name__contains='수입검사').first()

    ng_wh = Warehouse.objects.filter(code='8200').first()
    if not ng_wh:
        ng_wh = Warehouse.objects.filter(name__contains='부적합').first()

    # ✅ "입고 확정" 기준
    # - 수기입고(IN_MANUAL): 그대로
    # - SCM입고(수입검사 대상): 검사대기(8100)로 들어간 IN_SCM은 제외되고,
    #   검사 완료 후 정상 창고로 이동된 TRANSFER(8100 -> 정상창고)는 'SCM입고'로 표시
    qs = MaterialTransaction.objects.filter(
        Q(transaction_type='IN_MANUAL')
        |
        (
            Q(transaction_type='IN_SCM')
            & Q(quantity__gt=0)
            & Q(warehouse_to__isnull=False)
        )
        |
        (
            Q(transaction_type='TRANSFER')
            & Q(quantity__gt=0)
            & Q(warehouse_from__isnull=False)
            & Q(warehouse_to__isnull=False)
        )
    ).select_related('part', 'warehouse_to', 'warehouse_from', 'actor', 'vendor').order_by('-date', '-id')

    # ✅ 검사대기/부적합 제외
    if waiting_wh:
        # (A) IN_SCM이 검사대기창고로 들어간 건 제외
        qs = qs.exclude(transaction_type='IN_SCM', warehouse_to=waiting_wh)

        # (B) TRANSFER는 "검사대기창고 -> 다른창고"로 이동된 것만 '입고확정'으로 취급
        qs = qs.filter(
            Q(transaction_type='IN_MANUAL') |
            Q(transaction_type='IN_SCM') |
            (Q(transaction_type='TRANSFER') & Q(warehouse_from=waiting_wh))
        )
    else:
        # 확실하지 않음: 8100 식별이 안되면 TRANSFER를 입고확정으로 판정하기 어려움
        # => 이 경우 TRANSFER를 제외하고 수기/SCM(비검사)만 보여줌
        qs = qs.exclude(transaction_type='TRANSFER')

    if ng_wh:
        qs = qs.exclude(warehouse_to=ng_wh)

    # 2) 검색 필터
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(part__part_no__icontains=q) |
            Q(part__part_name__icontains=q)
        )

    # 3) 날짜 필터 ('None' 방어)
    start_date = (request.GET.get('start_date') or '').strip()
    end_date = (request.GET.get('end_date') or '').strip()

    if start_date in ('None', 'null', 'NULL'):
        start_date = ''
    if end_date in ('None', 'null', 'NULL'):
        end_date = ''

    if start_date:
        qs = qs.filter(date__date__gte=start_date)
    if end_date:
        qs = qs.filter(date__date__lte=end_date)

    # 4) 페이징
    paginator = Paginator(qs, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # ✅ 템플릿에서 "구분/비고"를 요구사항대로 표시할 수 있도록 표시용 값만 붙임
    for item in page_obj:
        if item.transaction_type == 'IN_MANUAL':
            item.display_type = "수기입고"
            item.display_remark = item.remark or ""
        else:
            # IN_SCM 또는 (검사완료 후) TRANSFER를 모두 "SCM입고"로 표시
            item.display_type = "SCM입고"

            # 비고는 "납품서 번호"가 원칙
            # (중요) 현재 QMS에서 생성하는 TRANSFER에는 ref_delivery_order가 저장되지 않아
            #        여기서는 ref_delivery_order가 있는 경우만 표시 가능 (없으면 빈값)
            item.display_remark = item.ref_delivery_order or ""

    context = {
        'page_obj': page_obj,
        'q': q,
        'start_date': start_date,
        'end_date': end_date,
    }
    return render(request, 'material/incoming_history.html', context)


# =============================================================================
# 3. 현장 지원 (태그 발행)
# =============================================================================

@login_required
def process_tag_form(request):
    """현품표 발행 입력 폼"""
    parts = Part.objects.all().order_by('part_no')
    context = {'parts': parts}
    return render(request, 'material/process_tag_form.html', context)


@login_required
def process_tag_print(request):
    """현품표 실제 출력 뷰"""
    if request.method == 'POST':
        part_no = request.POST.get('part_no')
        quantity = request.POST.get('quantity')
        lot_no = request.POST.get('lot_no')
        print_count = int(request.POST.get('print_count', 1))
        print_mode = request.POST.get('print_mode', 'roll')
        size_type = request.POST.get('size_type', 'medium')

        context = {
            'part_no': part_no,
            'quantity': quantity,
            'lot_no': lot_no,
            'print_mode': print_mode,
            'size_type': size_type,
            'print_range': range(print_count),
            'custom_width': request.POST.get('custom_width'),
            'custom_height': request.POST.get('custom_height'),
        }
        return render(request, 'material/process_tag_print.html', context)

    return redirect('material:process_tag_form')


# =============================================================================
# 4. 기타 메뉴
# =============================================================================

@login_required
def transaction_history(request):
    """기간별 수불 대장"""
    return render(request, 'material/transaction_history.html')


@login_required
def stock_adjustment(request):
    """재고 실사 및 조정"""
    return render(request, 'material/stock_adjustment.html')


@login_required
def outbound_create(request):
    """생산 자재 불출"""
    return render(request, 'material/outbound_create.html')


@login_required
def stock_transfer(request):
    """
    [WMS] 재고 이동 처리
    - 기존 메뉴/URL(material:stock_transfer)을 유지하면서
      실제 처리는 stock_move로 위임
    """
    return stock_move(request)



@login_required
def transfer_history(request):
    """재고 이동 현황"""
    return render(request, 'material/transfer_history.html')


@login_required
def stock_check(request):
    """[현장] 재고 실사 QR 스캔"""
    return render(request, 'material/stock_check.html')


@login_required
def stock_check_result(request):
    """[관리] 재고 실사 결과"""
    return render(request, 'material/stock_check_result.html')


# =============================================================================
# 5. 불량 반품 및 반출증
# =============================================================================

@login_required
def stock_return(request):
    """
    [WMS] 불량 반품 처리
    """
    ng_wh = Warehouse.objects.filter(code='8200').first()
    if not ng_wh:
        ng_wh = Warehouse.objects.filter(name__contains='부적합').first()

    if request.method == 'POST':
        try:
            stock_id = request.POST.get('stock_id')
            return_qty = int(request.POST.get('quantity', 0))
            remark = request.POST.get('remark', '')

            with transaction.atomic():
                stock = get_object_or_404(MaterialStock, id=stock_id)

                if return_qty <= 0:
                    messages.error(request, "반품 수량은 1개 이상이어야 합니다.")
                    return redirect('material:stock_return')
                if stock.quantity < return_qty:
                    messages.error(request, "재고 수량보다 많이 반품할 수 없습니다.")
                    return redirect('material:stock_return')

                # (1) 재고 차감
                stock.quantity = F('quantity') - return_qty
                stock.save()

                # (2) 반품 이력 남기기
                trx_no = f"RET-{timezone.now().strftime('%y%m%d%H%M%S')}-{request.user.id}"
                trx = MaterialTransaction.objects.create(
                    transaction_no=trx_no,
                    transaction_type='OUT_RETURN',
                    date=timezone.now(),
                    part=stock.part,
                    quantity=return_qty,
                    warehouse_from=stock.warehouse,
                    vendor=stock.part.vendor,
                    actor=request.user,
                    remark=f"[반품] {remark}"
                )

            messages.success(request, f"[{stock.part.part_name}] {return_qty}개 반품 처리 완료.")
            return redirect('material:print_return_note', trx_id=trx.id)

        except Exception as e:
            messages.error(request, f"오류 발생: {str(e)}")
            return redirect('material:stock_return')

    ng_stocks = []
    if ng_wh:
        ng_stocks = MaterialStock.objects.filter(
            warehouse=ng_wh, quantity__gt=0
        ).select_related('part', 'part__vendor').order_by('part__part_no')

    return render(request, 'material/stock_return.html', {
        'ng_stocks': ng_stocks
    })


@login_required
def print_return_note(request, trx_id):
    """
    [WMS] 반출증(Gate Pass) 인쇄 화면
    """
    trx = get_object_or_404(MaterialTransaction, pk=trx_id)
    return render(request, 'material/print_return_note.html', {
        'trx': trx,
        'today': timezone.now()
    })

# material/views.py 에 아래 함수를 추가/덮어쓰기 하세요.

# 기존 import 아래에 추가하거나, 필요한 import가 없다면 함께 추가하세요.
from django.db import transaction
from django.db.models import F
from .models import MaterialTransaction  # MaterialStock, Warehouse 등은 이미 import 되어 있을 것으로 예상됨

# material/views.py 맨 아래에 작성

@login_required
def stock_move(request):
    """
    [WMS] 재고 이동 처리 (다중 품목 일괄 처리)
    """

    # 1. 화면 보여주기 (GET)
    if request.method == 'GET':
        # 전체 창고 조회 (필요시 .filter(is_active=True)로 변경)
        warehouses = Warehouse.objects.all().order_by('code')

        context = {
            'warehouses': warehouses,
        }
        return render(request, 'material/stock_move.html', context)

    # 2. 이동 처리 (POST)
    elif request.method == 'POST':
        part_nos = request.POST.getlist('part_no[]')
        from_locs = request.POST.getlist('from_loc[]')
        to_locs = request.POST.getlist('to_loc[]')
        move_qtys = request.POST.getlist('move_qty[]')

        success_count = 0

        try:
            with transaction.atomic():
                for i in range(len(part_nos)):
                    p_no = (part_nos[i] or "").strip()
                    f_loc_val = (from_locs[i] or "").strip()
                    t_loc_val = (to_locs[i] or "").strip()

                    if not p_no or not f_loc_val or not t_loc_val:
                        continue

                    try:
                        qty = int(move_qtys[i])
                    except (ValueError, TypeError):
                        continue

                    if qty <= 0:
                        continue

                    if f_loc_val == t_loc_val:
                        continue

                    # 모델 조회
                    try:
                        part_obj = Part.objects.get(part_no=p_no)
                    except Part.DoesNotExist:
                        raise ValueError(f"존재하지 않는 품번입니다: [{p_no}]")

                    # ✅ 템플릿 value가 code인 경우(보통 이 케이스)
                    from_wh = Warehouse.objects.get(code=f_loc_val)
                    to_wh = Warehouse.objects.get(code=t_loc_val)

                    # 만약 템플릿 value가 id라면 아래처럼 바꿔야 함 (확실하지 않음)
                    # from_wh = Warehouse.objects.get(id=f_loc_val)
                    # to_wh   = Warehouse.objects.get(id=t_loc_val)

                    # 2-1. 보내는 창고 재고 차감
                    source_stock = MaterialStock.objects.select_for_update().filter(
                        warehouse=from_wh,
                        part=part_obj
                    ).first()

                    if not source_stock:
                        raise ValueError(f"출고 창고[{from_wh.name}]에 해당 품목[{p_no}] 재고가 없습니다.")

                    # source_stock.quantity가 F()로 변한 상태를 피하기 위해 현재 값을 먼저 사용
                    if int(source_stock.quantity) < qty:
                        raise ValueError(f"[{p_no}] 재고 부족 (보유: {source_stock.quantity}, 요청: {qty})")

                    MaterialStock.objects.filter(pk=source_stock.pk).update(
                        quantity=F('quantity') - qty
                    )

                    # 2-2. 받는 창고 재고 증가
                    target_stock, _ = MaterialStock.objects.select_for_update().get_or_create(
                        warehouse=to_wh,
                        part=part_obj,
                        defaults={'quantity': 0}
                    )

                    MaterialStock.objects.filter(pk=target_stock.pk).update(
                        quantity=F('quantity') + qty
                    )

                    # ✅ update(F()) 이후 실제 수량을 다시 읽어서 result_stock 숫자 저장
                    target_stock.refresh_from_db()

                    # 2-3. 이력(Transaction) 생성
                    trx_no = f"TRX-{timezone.now().strftime('%y%m%d%H%M%S%f')}-{request.user.id}-{i}"

                    MaterialTransaction.objects.create(
                        transaction_no=trx_no,
                        transaction_type='TRANSFER',
                        date=timezone.now(),
                        part=part_obj,
                        quantity=qty,
                        warehouse_from=from_wh,
                        warehouse_to=to_wh,
                        result_stock=target_stock.quantity,   # ✅ 실제 숫자
                        actor=request.user,
                        remark=f"재고이동 ({from_wh.name} -> {to_wh.name})"
                    )

                    success_count += 1

            if success_count > 0:
                messages.success(request, f"총 {success_count}건 이동 완료되었습니다.")
            else:
                messages.warning(request, "처리된 내역이 없습니다.")

            return redirect('material:stock_move')

        except Exception as e:
            messages.error(request, f"오류 발생: {str(e)}")
            return redirect('material:stock_move')

@login_required
def api_part_exists(request):
    part_no = (request.GET.get('part_no') or '').strip()
    exists = Part.objects.filter(part_no=part_no).exists()
    return JsonResponse({'exists': exists})


# =============================================================================
# 6. LOT 관리 - LOT별 재고 상세 조회 API
# =============================================================================
@login_required
def get_lot_details(request, part_no):
    """
    특정 품목의 LOT별 재고 상세 정보를 JSON으로 반환 (WMS용)
    """
    try:
        part = Part.objects.filter(part_no=part_no).first()
        if not part:
            return JsonResponse({'error': '품목을 찾을 수 없습니다.'}, status=404)

        # MaterialStock에서 해당 품목의 LOT별 재고 조회
        lot_stocks = MaterialStock.objects.filter(part=part, quantity__gt=0).select_related('warehouse').order_by('lot_no')

        lot_data = []
        total_qty = 0
        oldest_lot = None

        for stock in lot_stocks:
            lot_info = {
                'warehouse': stock.warehouse.name,
                'warehouse_code': stock.warehouse.code,
                'lot_no': stock.lot_no.strftime('%Y-%m-%d') if stock.lot_no else '-',
                'quantity': stock.quantity,
                'days_old': (timezone.now().date() - stock.lot_no).days if stock.lot_no else 0
            }
            lot_data.append(lot_info)
            total_qty += stock.quantity

            # 가장 오래된 LOT 추적 (FIFO 경고용)
            if stock.lot_no and (oldest_lot is None or stock.lot_no < oldest_lot):
                oldest_lot = stock.lot_no

        # FIFO 경고 판정 (60일 이상 된 LOT가 있으면 경고)
        fifo_warning = False
        if oldest_lot:
            days_old = (timezone.now().date() - oldest_lot).days
            if days_old >= 60:
                fifo_warning = True

        return JsonResponse({
            'part_no': part.part_no,
            'part_name': part.part_name,
            'vendor_name': part.vendor.name,
            'total_quantity': total_qty,
            'lot_details': lot_data,
            'fifo_warning': fifo_warning,
            'oldest_lot': oldest_lot.strftime('%Y-%m-%d') if oldest_lot else None,
            'oldest_days': (timezone.now().date() - oldest_lot).days if oldest_lot else 0
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)