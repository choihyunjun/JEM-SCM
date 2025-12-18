from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum, Case, When, Value, IntegerField
from django.utils import timezone
from django.http import HttpResponse
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.db import transaction
import openpyxl 
from datetime import timedelta

# 모델 임포트
from .models import Order, Vendor, Part, Inventory, Incoming, LabelPrintLog, DeliveryOrder, DeliveryOrderItem

# [1. 조회 화면]
@login_required
def order_list(request):
    user = request.user
    vendor_list = Vendor.objects.all().order_by('name') if user.is_superuser else []
    
    sort_by = request.GET.get('sort', 'due_date')
    if not sort_by or sort_by == 'None':
        sort_by = 'due_date'

    order_queryset = Order.objects.annotate(
        status_priority=Case(
            When(is_closed=True, then=Value(2)),
            When(approved_at__isnull=True, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )
    )

    if user.is_superuser:
        orders = order_queryset.all().order_by('status_priority', sort_by, '-created_at')
        vendor_name = "전체 관리자"
    elif hasattr(user, 'vendor'): 
        orders = order_queryset.filter(vendor=user.vendor).order_by('status_priority', sort_by, '-created_at')
        vendor_name = user.vendor.name
    else:
        orders = order_queryset.none()
        vendor_name = "소속 없음"

    selected_vendor = request.GET.get('vendor_id') 
    if user.is_superuser and selected_vendor and selected_vendor != 'None':
        orders = orders.filter(vendor_id=selected_vendor)
    
    status_filter = request.GET.get('status')
    if status_filter and status_filter != 'None':
        if status_filter == 'unapproved':
            orders = orders.filter(approved_at__isnull=True, is_closed=False)
        elif status_filter == 'approved':
            orders = orders.filter(approved_at__isnull=False, is_closed=False)
        elif status_filter == 'closed': 
            orders = orders.filter(is_closed=True)

    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    if start_date and start_date != 'None' and end_date and end_date != 'None':
        orders = orders.filter(due_date__range=[start_date, end_date])
    
    q = request.GET.get('q', '')
    if q and q != 'None':
        orders = orders.filter(Q(part_no__icontains=q) | Q(part_name__icontains=q))

    context = {
        'orders': orders, 'user_name': user.username, 'vendor_name': vendor_name,
        'q': q if q != 'None' else '', 'vendor_list': vendor_list, 
        'selected_vendor': selected_vendor if selected_vendor != 'None' else '',
        'status_filter': status_filter if status_filter != 'None' else '', 
        'start_date': start_date if start_date != 'None' else '', 
        'end_date': end_date if end_date != 'None' else '',
        'active_menu': 'list', 'current_sort': sort_by,
    }
    return render(request, 'order_list.html', context)

# [2. 발주 등록 화면]
@login_required
def order_upload(request):
    if not request.user.is_superuser:
        messages.error(request, "발주 등록 권한이 없습니다.")
        return redirect('order_list')
    return render(request, 'order_upload.html', {'active_menu': 'upload'})

# [3. 엑셀 업로드 처리]
@login_required
def order_upload_action(request):
    if not request.user.is_superuser:
        messages.error(request, "발주 등록 권한이 없습니다.")
        return redirect('order_list')

    if request.method == 'POST' and request.FILES.get('excel_file'):
        excel_file = request.FILES['excel_file']
        try:
            wb = openpyxl.load_workbook(excel_file)
            ws = wb.active
            created_count = 0
            skipped_count = 0 
            
            for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                v_name, p_no, qty, due = row
                if not v_name or not p_no: 
                    skipped_count += 1
                    continue

                part_master = Part.objects.filter(
                    part_no=p_no, 
                    vendor__name=v_name
                ).select_related('vendor').first()

                if part_master:
                    Order.objects.create(
                        vendor=part_master.vendor,
                        part_no=p_no,
                        part_name=part_master.part_name,
                        part_group=part_master.part_group,
                        quantity=qty if qty else 0,
                        due_date=due
                    )
                    created_count += 1
                else:
                    skipped_count += 1
            
            if created_count > 0:
                messages.success(request, f"{created_count}건의 발주가 등록되었습니다. (마스터 불일치 {skipped_count}건 제외)")
            else:
                messages.warning(request, f"등록된 데이터가 없습니다. (총 {skipped_count}건이 마스터 정보와 불일치합니다.)")
        except Exception as e:
            messages.error(request, f"파일 처리 중 오류 발생: {str(e)}")
            
    return redirect('order_upload')

# [4. 선택 발주 삭제]
@login_required
def order_delete(request):
    if request.method == 'POST':
        if not request.user.is_superuser:
            messages.error(request, "삭제 권한이 없습니다.")
            return redirect('order_list')

        order_ids = request.POST.getlist('order_ids')
        if order_ids:
            deleted_count = Order.objects.filter(id__in=order_ids).delete()[0]
            messages.success(request, f"총 {deleted_count}건의 발주가 삭제되었습니다.")
        else:
            messages.warning(request, "삭제할 항목을 선택해주세요.")
            
    return redirect('order_list')

# [4-1. 선택 발주 마감 처리]
@login_required
def order_close_action(request):
    if request.method == 'POST':
        if not request.user.is_superuser:
            messages.error(request, "마감 권한이 없습니다.")
            return redirect('order_list')

        order_ids = request.POST.getlist('order_ids')
        if order_ids:
            updated_count = Order.objects.filter(id__in=order_ids).update(is_closed=True)
            messages.success(request, f"총 {updated_count}건의 발주가 마감 처리되었습니다.")
        else:
            messages.warning(request, "마감할 항목을 선택해주세요.")
            
    return redirect('order_list')

# [5. 미확인 발주 일괄 승인]
@login_required
def order_approve_all(request):
    user = request.user
    if user.is_superuser:
        orders_to_approve = Order.objects.filter(approved_at__isnull=True, is_closed=False)
    elif hasattr(user, 'vendor'):
        orders_to_approve = Order.objects.filter(vendor=user.vendor, approved_at__isnull=True, is_closed=False)
    else:
        return redirect('order_list')

    count = orders_to_approve.count()
    if count > 0:
        orders_to_approve.update(approved_at=timezone.now())
        messages.success(request, f"총 {count}건의 발주가 일괄 승인되었습니다.")
    else:
        messages.warning(request, "승인할 미확인 발주가 없습니다.")
        
    return redirect('order_list')

# [6. 기타 기능]
def login_success(request):
    return redirect('/admin/') if request.user.is_superuser else redirect('order_list')

@login_required
def order_approve(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    if request.user.is_superuser or (hasattr(request.user, 'vendor') and order.vendor == request.user.vendor):
        if not order.approved_at and not order.is_closed:
            order.approved_at = timezone.now()
            order.save()
    return redirect('order_list')

@login_required
def order_export(request):
    user = request.user
    orders = Order.objects.all().order_by('-created_at') if user.is_superuser else Order.objects.filter(vendor=user.vendor).order_by('-created_at')
    
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "발주현황"
    ws.append(['상태', '등록일', '승인일', '협력사', '품목군', '품번', '품명', '수량', '납기일'])
    
    for o in orders:
        if o.is_closed:
            status = "발주마감"
        else:
            status = "승인완료" if o.approved_at else "미확인"
        ws.append([status, o.created_at.date(), o.approved_at.date() if o.approved_at else "-", o.vendor.name, o.part_group, o.part_no, o.part_name, o.quantity, str(o.due_date)])
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=orders.xlsx'
    wb.save(response)
    return response

# [7. 과부족 조회 현황]
@login_required
def inventory_list(request):
    user = request.user
    today = timezone.now().date()
    end_date = today + timedelta(days=14)
    date_range = [today + timedelta(days=i) for i in range(15)]
    
    show_all = request.GET.get('show_all') == 'true'
    selected_vendor_id = request.GET.get('vendor_id')
    
    if user.is_superuser:
        inventory_items = Inventory.objects.select_related('part', 'part__vendor').all()
        vendor_list = Vendor.objects.all().order_by('name')
        vendor_name = "전체 관리자"
    elif hasattr(user, 'vendor'):
        inventory_items = Inventory.objects.select_related('part', 'part__vendor').filter(part__vendor=user.vendor)
        vendor_list = []
        vendor_name = user.vendor.name
    else:
        inventory_items = Inventory.objects.none()
        vendor_list = []
        vendor_name = "소속 없음"

    if user.is_superuser and selected_vendor_id and selected_vendor_id != 'None':
        inventory_items = inventory_items.filter(part__vendor_id=selected_vendor_id)

    if not show_all:
        active_part_nos = Order.objects.filter(due_date__range=[today, end_date], is_closed=False).values_list('part_no', flat=True).distinct()
        inventory_items = inventory_items.filter(part__part_no__in=active_part_nos)
    
    inventory_data = []
    for item in inventory_items:
        daily_status = []
        ref_date = item.last_inventory_date if hasattr(item, 'last_inventory_date') and item.last_inventory_date else timezone.datetime(2000, 1, 1).date()
        
        running_stock = item.base_stock
        
        historical_orders = Order.objects.filter(part_no=item.part.part_no, due_date__gt=ref_date, due_date__lt=today, is_closed=False).aggregate(Sum('quantity'))['quantity__sum'] or 0
        historical_in = Incoming.objects.filter(part=item.part, in_date__gt=ref_date, in_date__lt=today).aggregate(Sum('quantity'))['quantity__sum'] or 0
        running_stock = running_stock - historical_orders + historical_in

        for dt in date_range:
            if dt < ref_date:
                daily_order = 0; daily_in = 0
            else:
                daily_order = Order.objects.filter(part_no=item.part.part_no, due_date=dt, is_closed=False).aggregate(Sum('quantity'))['quantity__sum'] or 0
                daily_in = Incoming.objects.filter(part=item.part, in_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0
            
            running_stock = running_stock - daily_order + daily_in
            daily_status.append({'date': dt, 'order_qty': daily_order, 'in_qty': daily_in, 'stock': running_stock, 'is_danger': running_stock < 0})
            
        inventory_data.append({'vendor_name': item.part.vendor.name, 'part_no': item.part.part_no, 'part_name': item.part.part_name, 'base_stock': item.base_stock, 'daily_status': daily_status, 'ref_date': ref_date})

    context = {
        'date_range': date_range, 'inventory_data': inventory_data, 'user_name': user.username, 'vendor_name': vendor_name,
        'active_menu': 'inventory', 'show_all': show_all, 'vendor_list': vendor_list, 'selected_vendor_id': selected_vendor_id,
    }
    return render(request, 'inventory_list.html', context)

# [8. 과부족 현황 엑셀 내보내기]
@login_required
def inventory_export(request):
    user = request.user
    today = timezone.now().date()
    date_range = [today + timedelta(days=i) for i in range(15)]
    
    show_all = request.GET.get('show_all') == 'true'
    selected_vendor_id = request.GET.get('vendor_id')
    
    if user.is_superuser:
        items = Inventory.objects.select_related('part', 'part__vendor').all()
        if selected_vendor_id and selected_vendor_id != 'None':
            items = items.filter(part__vendor_id=selected_vendor_id)
    elif hasattr(user, 'vendor'):
        items = Inventory.objects.select_related('part', 'part__vendor').filter(part__vendor=user.vendor)
    else:
        return redirect('inventory_list')

    if not show_all:
        active_nos = Order.objects.filter(due_date__range=[today, today + timedelta(days=14)], is_closed=False).values_list('part_no', flat=True).distinct()
        items = items.filter(part__part_no__in=active_nos)

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "D+14_수급현황"
    header = ['협력사', '품번', '품명', '구분', '기초재고'] + [dt.strftime('%m/%d') for dt in date_range]
    ws.append(header)

    for item in items:
        ref_date = item.last_inventory_date if hasattr(item, 'last_inventory_date') and item.last_inventory_date else timezone.datetime(2000, 1, 1).date()
        running_stock = item.base_stock
        
        hist_orders = Order.objects.filter(part_no=item.part.part_no, due_date__gt=ref_date, due_date__lt=today, is_closed=False).aggregate(Sum('quantity'))['quantity__sum'] or 0
        hist_in = Incoming.objects.filter(part=item.part, in_date__gt=ref_date, in_date__lt=today).aggregate(Sum('quantity'))['quantity__sum'] or 0
        running_stock = running_stock - hist_orders + hist_in

        row_order = [item.part.vendor.name, item.part.part_no, item.part.part_name, '소요량', item.base_stock]
        row_in = ['', '', '', '입고량', '']
        row_stock = ['', '', '', '과부족', '']

        for dt in date_range:
            if dt < ref_date: d_order = 0; d_in = 0
            else:
                d_order = Order.objects.filter(part_no=item.part.part_no, due_date=dt, is_closed=False).aggregate(Sum('quantity'))['quantity__sum'] or 0
                d_in = Incoming.objects.filter(part=item.part, in_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0
            
            running_stock = running_stock - d_order + d_in
            row_order.append(d_order); row_in.append(d_in); row_stock.append(running_stock)

        ws.append(row_order); ws.append(row_in); ws.append(row_stock)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename=Inventory_Status_{today}.xlsx'
    wb.save(response)
    return response

# ==============================================================================
# [9. 라벨 발행 화면]
# ==============================================================================
@login_required
def label_list(request):
    user = request.user
    if not hasattr(user, 'vendor') and not user.is_superuser:
         messages.error(request, "접근 권한이 없습니다.")
         return redirect('order_list')

    if user.is_superuser:
        orders = Order.objects.filter(is_closed=False, approved_at__isnull=False)
    else:
        orders = Order.objects.filter(vendor=user.vendor, is_closed=False, approved_at__isnull=False)

    label_data = []
    part_nos = orders.values_list('part_no', flat=True).distinct()

    for p_no in part_nos:
        example_order = orders.filter(part_no=p_no).first()
        if not example_order: continue
        
        part_name = example_order.part_name
        total_order = orders.filter(part_no=p_no).aggregate(Sum('quantity'))['quantity__sum'] or 0
        printed = LabelPrintLog.objects.filter(part_no=p_no).aggregate(Sum('printed_qty'))['printed_qty__sum'] or 0
        remain_qty = total_order - printed
        
        if remain_qty > 0:
            label_data.append({
                'part_no': p_no,
                'part_name': part_name,
                'total_order': total_order,
                'printed': printed,
                'remain': remain_qty
            })

    # [2] 최근 생성된 납품서 5개 가져오기
    recent_orders = DeliveryOrder.objects.all().order_by('-created_at')[:5]

    context = {
        'label_data': label_data,   
        'recent_orders': recent_orders, 
        'active_menu': 'label',
    }
    return render(request, 'label_list.html', context)


# [10. 납품서 생성 (DB 저장)]
@login_required
@require_POST
def create_delivery_order(request):
    part_nos = request.POST.getlist('part_nos[]')
    part_names = request.POST.getlist('part_names[]')
    snps = request.POST.getlist('snps[]')
    box_counts = request.POST.getlist('box_counts[]')

    if not part_nos:
        messages.warning(request, "등록할 품목이 없습니다.")
        return redirect('label_list')

    with transaction.atomic():
        order_no = "DO-" + timezone.now().strftime("%Y%m%d-%H%M%S")
        order = DeliveryOrder.objects.create(order_no=order_no)

        for i in range(len(part_nos)):
            part_no = part_nos[i]
            part_name = part_names[i]
            snp = int(snps[i])
            box_count = int(box_counts[i])
            total_qty = snp * box_count
            
            DeliveryOrderItem.objects.create(
                order=order,
                part_no=part_no,
                part_name=part_name,
                snp=snp,
                box_count=box_count,
                total_qty=total_qty
            )

            if hasattr(request.user, 'vendor'):
                part_obj = Part.objects.filter(part_no=part_no, vendor=request.user.vendor).first()
            else:
                part_obj = Part.objects.filter(part_no=part_no).first()
            
            if part_obj:
                LabelPrintLog.objects.create(
                    vendor=part_obj.vendor,
                    part=part_obj,
                    part_no=part_no,
                    printed_qty=total_qty,
                    snp=snp
                )

    messages.success(request, f"납품서 [{order_no}]가 생성되었습니다.")
    return redirect('label_list')


# [11. 저장된 납품서 인쇄 (라벨)]
@login_required
def label_print(request, order_id):
    order = get_object_or_404(DeliveryOrder, pk=order_id)
    items = order.items.all()
    
    print_queue = []
    for item in items:
        vendor_name = "세영산업" 
        if hasattr(request.user, 'vendor'):
            vendor_name = request.user.vendor.name
            
        for _ in range(item.box_count):
            print_queue.append({
                'vendor_name': vendor_name,
                'part_name': item.part_name,
                'part_no': item.part_no,
                'snp': item.snp,
                'print_date': timezone.now()
            })

    context = {
        'box_count': print_queue, 
        'vendor_name': vendor_name, 
        'print_date': timezone.now(),
    }
    return render(request, 'print_label.html', context)


# [12. (필수) 에러 방지용: 이전 인쇄 액션 처리]
@login_required
def label_print_action(request):
    return redirect('label_list')


# [13. 납품서(거래명세서) 출력 화면]
@login_required
def delivery_note_print(request, order_id):
    order = get_object_or_404(DeliveryOrder, pk=order_id)
    items = order.items.all()
    
    # 합계 계산
    total_qty = items.aggregate(Sum('total_qty'))['total_qty__sum'] or 0
    total_box = items.aggregate(Sum('box_count'))['box_count__sum'] or 0

    context = {
        'order': order,
        'items': items,
        'total_qty': total_qty,
        'total_box': total_box,
        'print_date': timezone.now().date(),
        'vendor_name': request.user.vendor.name if hasattr(request.user, 'vendor') else "관리자",
    }
    return render(request, 'print_delivery_note.html', context)


# [14. QR 스캔 입고 처리 (수정됨: 리다이렉트 주소 변경)]
@login_required
@require_POST
def receive_delivery_order_scan(request):
    qr_code = request.POST.get('qr_code', '').strip()
    
    if not qr_code:
        messages.error(request, "QR 코드가 입력되지 않았습니다.")
        return redirect('incoming_list') 

    try:
        with transaction.atomic():
            order = DeliveryOrder.objects.filter(order_no=qr_code).first()
            
            if not order:
                messages.error(request, f"존재하지 않는 납품서 번호입니다: {qr_code}")
                return redirect('incoming_list')
            
            if order.is_received:
                messages.warning(request, f"이미 입고 완료된 납품서입니다: {qr_code}")
                return redirect('incoming_list')

            items = order.items.all()
            for item in items:
                part_obj = Part.objects.filter(part_no=item.part_no).first()
                if part_obj:
                    # 입고 이력 생성 (Incoming)
                    Incoming.objects.create(
                        part=part_obj,
                        quantity=item.total_qty,
                        in_date=timezone.now().date()
                    )
                    # 재고 증가 (Inventory)
                    inventory, created = Inventory.objects.get_or_create(
                        part=part_obj, defaults={'base_stock': 0}
                    )
                    inventory.base_stock += item.total_qty
                    inventory.save()
            
            order.is_received = True
            order.save()
            messages.success(request, f"[{order.order_no}] 입고 처리 완료! (재고 반영됨)")

    except Exception as e:
        messages.error(request, f"오류 발생: {str(e)}")

    return redirect('incoming_list')


# [15. 입고 현황 조회 (검색/필터 추가됨)]
@login_required
def incoming_list(request):
    user = request.user
    
    # 1. 기본 쿼리셋 설정 (권한별)
    if user.is_superuser:
        incomings = Incoming.objects.select_related('part', 'part__vendor').all().order_by('-in_date', '-created_at')
    elif hasattr(user, 'vendor'):
        incomings = Incoming.objects.select_related('part', 'part__vendor').filter(part__vendor=user.vendor).order_by('-in_date', '-created_at')
    else:
        incomings = Incoming.objects.none()

    # 2. 검색 파라미터 받기
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    q = request.GET.get('q', '')

    # 3. 날짜 필터 적용
    if start_date and end_date:
        incomings = incomings.filter(in_date__range=[start_date, end_date])
    
    # 4. 키워드 검색 적용
    if q:
        incomings = incomings.filter(
            Q(part__part_no__icontains=q) | 
            Q(part__part_name__icontains=q)
        )

    # 5. 컨텍스트 전달
    context = {
        'incomings': incomings,
        'active_menu': 'incoming',
        'start_date': start_date if start_date else '',
        'end_date': end_date if end_date else '',
        'q': q,
    }
    return render(request, 'incoming_list.html', context)


# [16. 입고 내역 엑셀 다운로드 (필터 적용)]
@login_required
def incoming_export(request):
    user = request.user
    
    # 1. 기본 쿼리셋 설정 (권한별) - incoming_list와 동일
    if user.is_superuser:
        incomings = Incoming.objects.select_related('part', 'part__vendor').all().order_by('-in_date', '-created_at')
    elif hasattr(user, 'vendor'):
        incomings = Incoming.objects.select_related('part', 'part__vendor').filter(part__vendor=user.vendor).order_by('-in_date', '-created_at')
    else:
        return redirect('incoming_list')

    # 2. 검색 파라미터 받기
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    q = request.GET.get('q', '')

    # 3. 날짜 필터 적용
    if start_date and end_date:
        incomings = incomings.filter(in_date__range=[start_date, end_date])
    
    # 4. 키워드 검색 적용
    if q:
        incomings = incomings.filter(
            Q(part__part_no__icontains=q) | 
            Q(part__part_name__icontains=q)
        )

    # 5. 엑셀 생성
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "입고내역"

    # 헤더
    ws.append(['입고일자', '협력사', '품번', '품명', '입고수량', '처리일시'])

    # 데이터
    for item in incomings:
        ws.append([
            item.in_date,
            item.part.vendor.name,
            item.part.part_no,
            item.part.part_name,
            item.quantity,
            item.created_at.strftime("%Y-%m-%d %H:%M")
        ])

    # 6. 응답
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    filename = f"Incoming_List_{timezone.now().strftime('%Y%m%d')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename={filename}'
    
    wb.save(response)
    return response