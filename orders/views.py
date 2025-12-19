from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum, Case, When, Value, IntegerField, Max
from django.utils import timezone
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.db import transaction
import openpyxl 
from datetime import timedelta

# 모델 임포트
from .models import Order, Vendor, Part, Inventory, Incoming, LabelPrintLog, DeliveryOrder, DeliveryOrderItem, Demand

# [0. 필수 공통 로직]
def login_success(request):
    """로그인 성공 시 권한에 따른 리다이렉트"""
    # [✅ 수정] if문을 제거하고 모든 사용자가 바로 order_list로 가도록 들여쓰기를 맞춤
    return redirect('order_list')

# [1. 발주 조회 화면]
@login_required
def order_list(request):
    user = request.user
    vendor_list = Vendor.objects.all().order_by('name') if user.is_superuser else []
    sort_by = request.GET.get('sort', 'due_date') or 'due_date'

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
    if user.is_superuser and selected_vendor:
        orders = orders.filter(vendor_id=selected_vendor)
    
    status_filter = request.GET.get('status')
    if status_filter:
        if status_filter == 'unapproved': orders = orders.filter(approved_at__isnull=True, is_closed=False)
        elif status_filter == 'approved': orders = orders.filter(approved_at__isnull=False, is_closed=False)
        elif status_filter == 'closed': orders = orders.filter(is_closed=True)

    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    if start_date and end_date:
        orders = orders.filter(due_date__range=[start_date, end_date])
    
    q = request.GET.get('q', '')
    if q: orders = orders.filter(Q(part_no__icontains=q) | Q(part_name__icontains=q))

    return render(request, 'order_list.html', {
        'orders': orders, 'user_name': user.username, 'vendor_name': vendor_name,
        'q': q, 'vendor_list': vendor_list, 'selected_vendor': selected_vendor,
        'status_filter': status_filter, 'start_date': start_date, 'end_date': end_date,
        'active_menu': 'list', 'current_sort': sort_by,
    })

# [2. 발주 관련 액션]
@login_required
def order_upload(request):
    if not request.user.is_superuser: return redirect('order_list')
    return render(request, 'order_upload.html', {'active_menu': 'upload'})

@login_required
def order_upload_action(request):
    if not request.user.is_superuser: return redirect('order_list')
    if request.method == 'POST' and request.FILES.get('excel_file'):
        try:
            wb = openpyxl.load_workbook(request.FILES['excel_file'])
            ws = wb.active
            created_count = 0
            for row in ws.iter_rows(min_row=2, values_only=True):
                v_name, p_no, qty, due = row[0], row[1], row[2], row[3]
                part = Part.objects.filter(part_no=p_no, vendor__name=v_name).first()
                if part:
                    Order.objects.create(vendor=part.vendor, part_no=p_no, part_name=part.part_name, part_group=part.part_group, quantity=qty or 0, due_date=due)
                    created_count += 1
            messages.success(request, f"{created_count}건 등록 완료")
        except Exception as e: messages.error(request, str(e))
    return redirect('order_upload')

@login_required
@require_POST
def order_delete(request):
    if not request.user.is_superuser: return redirect('order_list')
    Order.objects.filter(id__in=request.POST.getlist('order_ids')).delete()
    return redirect('order_list')

@login_required
@require_POST
def order_close_action(request):
    if not request.user.is_superuser: return redirect('order_list')
    Order.objects.filter(id__in=request.POST.getlist('order_ids')).update(is_closed=True)
    return redirect('order_list')

@login_required
def order_approve_all(request):
    q = Order.objects.filter(approved_at__isnull=True, is_closed=False)
    if not request.user.is_superuser: q = q.filter(vendor=request.user.vendor)
    q.update(approved_at=timezone.now())
    return redirect('order_list')

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
        status = "발주마감" if o.is_closed else ("승인완료" if o.approved_at else "미확인")
        ws.append([status, o.created_at.date(), o.approved_at.date() if o.approved_at else "-", o.vendor.name, o.part_group, o.part_no, o.part_name, o.quantity, str(o.due_date)])
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=orders.xlsx'
    wb.save(response); return response

# [3. 과부족/소요량 핵심 로직]
@login_required
@require_POST
def quick_order_action(request):
    """과부족 화면 전용 빠른 발주 생성"""
    vendor_name = request.POST.get('vendor_name')
    part_no = request.POST.get('part_no')
    quantity = request.POST.get('quantity')
    due_date = request.POST.get('due_date')
    try:
        part = Part.objects.filter(part_no=part_no, vendor__name=vendor_name).first()
        if part:
            Order.objects.create(vendor=part.vendor, part_no=part_no, part_name=part.part_name, part_group=part.part_group, quantity=int(quantity), due_date=due_date)
            messages.success(request, f"발주 등록 완료: {part_no}")
    except Exception as e: messages.error(request, str(e))
    return redirect('inventory_list')

@login_required
def inventory_list(request):
    user = request.user
    if hasattr(user, 'vendor') and not user.vendor.can_view_inventory: return redirect('order_list')
    
    today = timezone.now().date()
    
    if user.is_superuser:
        max_due = Demand.objects.aggregate(Max('due_date'))['due_date__max']
        standard_end = today + timedelta(days=31)
        end_date = max_due if max_due and max_due > standard_end else standard_end
    else:
        end_date = today + timedelta(days=14)
    
    date_range = [today + timedelta(days=i) for i in range((end_date - today).days + 1)]
    show_all = request.GET.get('show_all') == 'true'
    selected_vendor_id = request.GET.get('vendor_id')
    
    if user.is_superuser:
        inventory_items = Inventory.objects.select_related('part', 'part__vendor').all()
        vendor_list = Vendor.objects.all().order_by('name')
        vendor_name = "전체 관리자"
        if selected_vendor_id: inventory_items = inventory_items.filter(part__vendor_id=selected_vendor_id)
    elif hasattr(user, 'vendor'):
        inventory_items = Inventory.objects.select_related('part', 'part__vendor').filter(part__vendor=user.vendor)
        vendor_list = []; vendor_name = user.vendor.name
    else: return redirect('order_list')

    if not show_all:
        active_part_nos = Demand.objects.filter(due_date__range=[today, end_date]).values_list('part__part_no', flat=True).distinct()
        inventory_items = inventory_items.filter(part__part_no__in=active_part_nos)
    
    inventory_data = []
    for item in inventory_items:
        daily_status = []; ref_date = item.last_inventory_date or timezone.datetime(2000, 1, 1).date(); running_stock = item.base_stock
        hist_dem = Demand.objects.filter(part=item.part, due_date__gt=ref_date, due_date__lt=today).aggregate(Sum('quantity'))['quantity__sum'] or 0
        hist_in = Incoming.objects.filter(part=item.part, in_date__gt=ref_date, in_date__lt=today).aggregate(Sum('quantity'))['quantity__sum'] or 0
        running_stock = running_stock - hist_dem + hist_in

        for dt in date_range:
            d_qty = Demand.objects.filter(part=item.part, due_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0 if dt >= ref_date else 0
            i_qty = Incoming.objects.filter(part=item.part, in_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0 if dt >= ref_date else 0
            running_stock = running_stock - d_qty + i_qty
            daily_status.append({'date': dt, 'demand_qty': d_qty, 'in_qty': i_qty, 'stock': running_stock, 'is_danger': running_stock < 0})
        inventory_data.append({'vendor_name': item.part.vendor.name, 'part_no': item.part.part_no, 'part_name': item.part.part_name, 'base_stock': item.base_stock, 'daily_status': daily_status, 'ref_date': item.last_inventory_date})
    return render(request, 'inventory_list.html', {'date_range': date_range, 'inventory_data': inventory_data, 'vendor_list': vendor_list, 'active_menu': 'inventory', 'show_all': show_all, 'selected_vendor_id': selected_vendor_id, 'user_name': user.username, 'vendor_name': vendor_name})

@login_required
@require_POST
def demand_upload_action(request):
    """소요량 엑셀 업로드 처리"""
    if not request.user.is_superuser: return redirect('inventory_list')
    if request.FILES.get('demand_file'):
        try:
            wb = openpyxl.load_workbook(request.FILES['demand_file']); ws = wb.active
            created_count = 0
            with transaction.atomic():
                for row in ws.iter_rows(min_row=2, values_only=True):
                    v_name, p_no, qty, d_date = row[0], row[1], row[2], row[3]
                    part = Part.objects.filter(part_no=p_no, vendor__name=v_name).first()
                    if part:
                        Demand.objects.update_or_create(part=part, due_date=d_date, defaults={'quantity': qty})
                        created_count += 1
            messages.success(request, f"소요량 {created_count}건 반영 완료")
        except Exception as e: messages.error(request, str(e))
    return redirect('inventory_list')

# [✅ 신규 기능] 소요량 필터 조회 및 관리 (수정/삭제용)
@login_required
def demand_manage(request):
    if not request.user.is_superuser: return redirect('inventory_list')
    
    vendor_id = request.GET.get('vendor_id')
    part_no = request.GET.get('part_no')
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')

    demands = Demand.objects.select_related('part', 'part__vendor').all().order_by('-due_date')

    if vendor_id: demands = demands.filter(part__vendor_id=vendor_id)
    if part_no: demands = demands.filter(part__part_no__icontains=part_no)
    if start_date and end_date: demands = demands.filter(due_date__range=[start_date, end_date])

    vendor_list = Vendor.objects.all().order_by('name')
    
    return render(request, 'demand_manage.html', {
        'demands': demands[:500], # 과도한 데이터 방지용 상위 500건
        'vendor_list': vendor_list,
        'selected_vendor': vendor_id,
        'part_no': part_no,
        'start_date': start_date,
        'end_date': end_date,
        'active_menu': 'inventory'
    })

# [✅ 신규 기능] 소요량 개별/선택 삭제
@login_required
@require_POST
def demand_delete_action(request):
    if not request.user.is_superuser: return redirect('inventory_list')
    demand_ids = request.POST.getlist('demand_ids')
    Demand.objects.filter(id__in=demand_ids).delete()
    messages.success(request, "선택한 소요량 데이터가 삭제되었습니다.")
    return redirect(request.META.get('HTTP_REFERER', 'demand_manage'))

# [✅ 신규 기능] 과부족 시트 실시간 수정 (AJAX)
@login_required
@require_POST
def demand_update_ajax(request):
    if not request.user.is_superuser: return JsonResponse({'status': 'error'}, status=403)
    part_no = request.POST.get('part_no')
    due_date = request.POST.get('due_date')
    quantity = request.POST.get('quantity')
    
    try:
        part = Part.objects.get(part_no=part_no)
        # 소요량이 0이면 삭제, 아니면 생성/업데이트
        if int(quantity) <= 0:
            Demand.objects.filter(part=part, due_date=due_date).delete()
        else:
            Demand.objects.update_or_create(
                part=part, due_date=due_date,
                defaults={'quantity': int(quantity)}
            )
        return JsonResponse({'status': 'success'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@login_required
@require_POST
def delete_all_demands(request):
    if not request.user.is_superuser: return redirect('inventory_list')
    count = Demand.objects.all().count()
    Demand.objects.all().delete()
    messages.success(request, f"총 {count}건의 소요량 데이터가 삭제되었습니다.")
    return redirect('inventory_list')

@login_required
def inventory_export(request):
    user = request.user; today = timezone.now().date()
    if user.is_superuser:
        max_due = Demand.objects.aggregate(Max('due_date'))['due_date__max']
        standard_end = today + timedelta(days=31)
        end_date = max_due if max_due and max_due > standard_end else standard_end
    else: end_date = today + timedelta(days=14)
    dr = [today + timedelta(days=i) for i in range((end_date - today).days + 1)]
    items = Inventory.objects.select_related('part', 'part__vendor').all()
    if not user.is_superuser: items = items.filter(part__vendor=user.vendor)
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(['협력사', '품번', '품명', '구분', '기초재고'] + [d.strftime('%m/%d') for d in dr])
    for item in items:
        ref = item.last_inventory_date or timezone.datetime(2000, 1, 1).date(); stock = item.base_stock
        h_dem = Demand.objects.filter(part=item.part, due_date__gt=ref, due_date__lt=today).aggregate(Sum('quantity'))['quantity__sum'] or 0
        h_in = Incoming.objects.filter(part=item.part, in_date__gt=ref, in_date__lt=today).aggregate(Sum('quantity'))['quantity__sum'] or 0
        stock = stock - h_dem + h_in
        r1, r2, r3 = [item.part.vendor.name, item.part.part_no, item.part.part_name, '소요량', item.base_stock], ['', '', '', '입고량', ''], ['', '', '', '과부족', '']
        for dt in dr:
            dq = Demand.objects.filter(part=item.part, due_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0 if dt >= ref else 0
            iq = Incoming.objects.filter(part=item.part, in_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0 if dt >= ref else 0
            stock = stock - dq + iq
            r1.append(dq); r2.append(iq); r3.append(stock)
        ws.append(r1); ws.append(r2); ws.append(r3)
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename=Inventory_{today}.xlsx'
    wb.save(response); return response

@login_required
def inventory_upload(request):
    if not request.user.is_superuser: return redirect('inventory_list')
    return render(request, 'inventory_upload.html', {'active_menu': 'inventory', 'today': timezone.now().date()})

@login_required
@require_POST
def inventory_upload_action(request):
    if not request.user.is_superuser: return redirect('inventory_list')
    selected_date = request.POST.get('inventory_date')
    if request.FILES.get('excel_file'):
        try:
            wb = openpyxl.load_workbook(request.FILES['excel_file']); ws = wb.active
            updated_count = 0
            with transaction.atomic():
                for row in ws.iter_rows(min_row=2, values_only=True):
                    v_name, p_no, qty = row[0], row[1], row[2]
                    part = Part.objects.filter(part_no=p_no, vendor__name=v_name).first()
                    if part:
                        inv, _ = Inventory.objects.get_or_create(part=part)
                        inv.base_stock = int(qty)
                        inv.last_inventory_date = selected_date
                        inv.save()
                        updated_count += 1
            messages.success(request, f"재고 초기화 완료 ({selected_date} 기준, {updated_count}건)")
        except Exception as e: messages.error(request, str(e))
    return redirect('inventory_list')

# [4. 라벨/입고 관리]
@login_required
def label_list(request):
    user = request.user
    q = Order.objects.filter(is_closed=False, approved_at__isnull=False)
    if not user.is_superuser: q = q.filter(vendor=user.vendor)
    label_data = []
    for p_no in q.values_list('part_no', flat=True).distinct():
        order = q.filter(part_no=p_no).first()
        total = q.filter(part_no=p_no).aggregate(Sum('quantity'))['quantity__sum'] or 0
        printed = LabelPrintLog.objects.filter(part_no=p_no).aggregate(Sum('printed_qty'))['printed_qty__sum'] or 0
        if total - printed > 0: label_data.append({'part_no': p_no, 'part_name': order.part_name, 'total_order': total, 'printed': printed, 'remain': total - printed})
    return render(request, 'label_list.html', {'label_data': label_data, 'recent_orders': DeliveryOrder.objects.all().order_by('-created_at')[:5], 'active_menu': 'label'})

@login_required
@require_POST
def create_delivery_order(request):
    part_nos = request.POST.getlist('part_nos[]'); snps = request.POST.getlist('snps[]'); box_counts = request.POST.getlist('box_counts[]')
    with transaction.atomic():
        do = DeliveryOrder.objects.create(order_no="DO-"+timezone.now().strftime("%Y%m%d-%H%M%S"))
        for i in range(len(part_nos)):
            part = Part.objects.filter(part_no=part_nos[i]).first()
            qty = int(snps[i]) * int(box_counts[i])
            DeliveryOrderItem.objects.create(order=do, part_no=part_nos[i], part_name=part.part_name, snp=int(snps[i]), box_count=int(box_counts[i]), total_qty=qty)
            LabelPrintLog.objects.create(vendor=part.vendor, part=part, part_no=part_nos[i], printed_qty=qty, snp=int(snps[i]))
    return redirect('label_list')

@login_required
def label_print(request, order_id):
    order = get_object_or_404(DeliveryOrder, pk=order_id); items = order.items.all(); queue = []
    vendor = request.user.vendor.name if hasattr(request.user, 'vendor') else "관리자"
    for item in items:
        for _ in range(item.box_count): queue.append({'vendor_name': vendor, 'part_name': item.part_name, 'part_no': item.part_no, 'snp': item.snp, 'print_date': timezone.now()})
    return render(request, 'print_label.html', {'box_count': queue, 'vendor_name': vendor})

@login_required
def delivery_note_print(request, order_id):
    do = get_object_or_404(DeliveryOrder, pk=order_id); items = do.items.all()
    return render(request, 'print_delivery_note.html', {'order': do, 'items': items, 'total_qty': items.aggregate(Sum('total_qty'))['total_qty__sum'] or 0, 'total_box': items.aggregate(Sum('box_count'))['box_count__sum'] or 0, 'print_date': timezone.now().date(), 'vendor_name': request.user.vendor.name if hasattr(request.user, 'vendor') else "관리자"})

@login_required
@require_POST
def receive_delivery_order_scan(request):
    do = DeliveryOrder.objects.filter(order_no=request.POST.get('qr_code', '').strip()).first()
    if do and not do.is_received:
        with transaction.atomic():
            for item in do.items.all():
                part = Part.objects.filter(part_no=item.part_no).first()
                if part:
                    Incoming.objects.create(part=part, quantity=item.total_qty, in_date=timezone.now().date())
                    inv, _ = Inventory.objects.get_or_create(part=part, defaults={'base_stock': 0})
                    inv.base_stock += item.total_qty; inv.save()
            do.is_received = True; do.save()
    return redirect('incoming_list')

@login_required
def incoming_list(request):
    user = request.user
    incomings = Incoming.objects.select_related('part', 'part__vendor').all().order_by('-in_date', '-created_at')
    if hasattr(user, 'vendor'): incomings = incomings.filter(part__vendor=user.vendor)
    sd = request.GET.get('start_date'); ed = request.GET.get('end_date'); q = request.GET.get('q', '')
    if sd and ed: incomings = incomings.filter(in_date__range=[sd, ed])
    if q: incomings = incomings.filter(Q(part__part_no__icontains=q) | Q(part__part_name__icontains=q))
    return render(request, 'incoming_list.html', {'incomings': incomings, 'active_menu': 'incoming', 'start_date': sd, 'end_date': ed, 'q': q})

@login_required
def incoming_export(request):
    user = request.user
    incomings = Incoming.objects.select_related('part', 'part__vendor').all()
    if hasattr(user, 'vendor'): incomings = incomings.filter(part__vendor=user.vendor)
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(['입고일자', '협력사', '품번', '품명', '입고수량', '처리일시'])
    for item in incomings: ws.append([item.in_date, item.part.vendor.name, item.part.part_no, item.part.part_name, item.quantity, item.created_at.strftime("%Y-%m-%d %H:%M")])
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename=Incomings.xlsx'
    wb.save(response); return response

@login_required
def label_print_action(request): return redirect('label_list')