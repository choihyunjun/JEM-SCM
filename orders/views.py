from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q, Sum, Case, When, Value, IntegerField, Max, Count
from django.utils import timezone
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.db import transaction
from functools import wraps
import openpyxl 
import datetime
from datetime import timedelta, date

# 모델 임포트
from .models import Order, Vendor, Part, Inventory, Incoming, LabelPrintLog, DeliveryOrder, DeliveryOrderItem, Demand

# ==========================================
# [0. 필수 공통 로직 및 권한 설정]
# ==========================================
def menu_permission_required(permission_field):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            profile = getattr(request.user, 'profile', None)
            if profile and getattr(profile, permission_field, False):
                return view_func(request, *args, **kwargs)
            messages.error(request, "해당 메뉴에 대한 접근 권한이 없습니다.")
            return redirect('order_list')
        return _wrapped_view
    return decorator

def login_success(request):
    return redirect('order_list')

# [1. 발주 조회 화면]
@login_required
@menu_permission_required('can_view_orders') 
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
        orders = order_queryset.all().order_by('status_priority', sort_by, '-created_at')
        vendor_name = "시스템 운영자"

    selected_vendor = request.GET.get('vendor_id')
    if (user.is_superuser or not hasattr(user, 'vendor')) and selected_vendor:
        orders = orders.filter(vendor_id=selected_vendor)
    
    status_filter = request.GET.get('status')
    if status_filter:
        if status_filter == 'unapproved': orders = orders.filter(approved_at__isnull=True, is_closed=False)
        elif status_filter == 'approved': orders = orders.filter(approved_at__isnull=False, is_closed=False)
        elif status_filter == 'closed': orders = orders.filter(is_closed=True)

    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    q = request.GET.get('q', '')

    if start_date and end_date: 
        orders = orders.filter(due_date__range=[start_date, end_date])
    if q: 
        orders = orders.filter(Q(part_no__icontains=q) | Q(part_name__icontains=q))

    today = timezone.localtime().date()
    overdue_list = []
    active_overdue = Order.objects.filter(due_date__lt=today, is_closed=False, approved_at__isnull=False)
    
    if not (user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'STAFF')):
        active_overdue = active_overdue.filter(vendor=user.vendor)
    elif selected_vendor:
        active_overdue = active_overdue.filter(vendor_id=selected_vendor)

    for o in active_overdue.order_by('due_date'):
        total_p = LabelPrintLog.objects.filter(part_no=o.part_no).aggregate(Sum('printed_qty'))['printed_qty__sum'] or 0
        closed_p = Order.objects.filter(part_no=o.part_no, is_closed=True).aggregate(Sum('quantity'))['quantity__sum'] or 0
        current_p = max(0, total_p - closed_p)
        rem = o.quantity - current_p
        
        if rem > 0:
            overdue_list.append({
                'due_date': o.due_date,
                'vendor_name': o.vendor.name,
                'part_no': o.part_no,
                'remain_qty': rem
            })

    return render(request, 'order_list.html', {
        'orders': orders, 'user_name': user.username, 'vendor_name': vendor_name,
        'q': q, 'vendor_list': vendor_list, 'selected_vendor': selected_vendor,
        'status_filter': status_filter, 'start_date': start_date, 'end_date': end_date,
        'active_menu': 'list', 'current_sort': sort_by,
        'overdue_orders': overdue_list,
    })

# [2. 발주 관련 액션]
@login_required
def order_upload(request):
    if not request.user.is_superuser: return redirect('order_list')
    return render(request, 'order_upload.html', {'active_menu': 'upload'})

@login_required
@require_POST
def order_upload_action(request):
    if not request.user.is_superuser: return redirect('order_list')
    if request.FILES.get('excel_file'):
        try:
            wb = openpyxl.load_workbook(request.FILES['excel_file']); ws = wb.active; c_count = 0
            for row in ws.iter_rows(min_row=2, values_only=True):
                part = Part.objects.filter(part_no=row[1], vendor__name=row[0]).first()
                if part:
                    Order.objects.create(vendor=part.vendor, part_no=row[1], part_name=part.part_name, part_group=part.part_group, quantity=row[2] or 0, due_date=row[3])
                    c_count += 1
            messages.success(request, f"{c_count}건 등록 완료")
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
    if not request.user.is_superuser and hasattr(request.user, 'vendor'): 
        q = q.filter(vendor=request.user.vendor)
    q.update(approved_at=timezone.now())
    return redirect('order_list')

@login_required
def order_approve(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    can_approve = request.user.is_superuser or not hasattr(request.user, 'vendor') or (hasattr(request.user, 'vendor') and order.vendor == request.user.vendor)
    if can_approve:
        if not order.approved_at and not order.is_closed:
            order.approved_at = timezone.now(); order.save()
    return redirect('order_list')

@login_required
def order_export(request):
    user = request.user
    if user.is_superuser or not hasattr(user, 'vendor'):
        orders = Order.objects.all().order_by('-created_at')
    else:
        orders = Order.objects.filter(vendor=user.vendor).order_by('-created_at')
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(['상태', '등록일', '승인일', '협력사', '품목군', '품번', '품명', '수량', '납기일'])
    for o in orders:
        status = "발주마감" if o.is_closed else ("승인완료" if o.approved_at else "미확인")
        ws.append([status, o.created_at.date(), o.approved_at.date() if o.approved_at else "-", o.vendor.name, o.part_group, o.part_no, o.part_name, o.quantity, str(o.due_date)])
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=orders.xlsx'; wb.save(response); return response

# [3. 과부족/소요량 로직]
@login_required
@menu_permission_required('can_view_inventory') 
def inventory_list(request):
    user = request.user; today = timezone.localtime().date()
    if user.is_superuser or not hasattr(user, 'vendor'):
        max_due = Demand.objects.aggregate(Max('due_date'))['due_date__max']
        standard_end = today + datetime.timedelta(days=31)
        end_date = max_due if max_due and max_due > standard_end else standard_end
    else: end_date = today + datetime.timedelta(days=14)
    date_range = [today + datetime.timedelta(days=i) for i in range((end_date - today).days + 1)]
    show_all = request.GET.get('show_all') == 'true'; selected_v = request.GET.get('vendor_id'); q = request.GET.get('q', '')
    if user.is_superuser or not hasattr(user, 'vendor'):
        inventory_items = Inventory.objects.select_related('part', 'part__vendor').all()
        vendor_list = Vendor.objects.all().order_by('name')
        if selected_v: inventory_items = inventory_items.filter(part__vendor_id=selected_v)
    elif hasattr(user, 'vendor'):
        inventory_items = Inventory.objects.select_related('part', 'part__vendor').filter(part__vendor=user.vendor); vendor_list = []
    else: return redirect('order_list')
    if q: inventory_items = inventory_items.filter(Q(part__part_no__icontains=q) | Q(part__part_name__icontains=q))
    if not show_all:
        act_pnos = Demand.objects.filter(due_date__range=[today, end_date]).values_list('part__part_no', flat=True).distinct()
        inventory_items = inventory_items.filter(part__part_no__in=act_pnos)
    inventory_data = []
    for item in inventory_items:
        daily_status = []
        ref = item.last_inventory_date or date(2000, 1, 1)
        hist_dem = Demand.objects.filter(part=item.part, due_date__gt=ref, due_date__lt=today).aggregate(Sum('quantity'))['quantity__sum'] or 0
        hist_in = Incoming.objects.filter(part=item.part, in_date__gt=ref, in_date__lt=today).aggregate(Sum('quantity'))['quantity__sum'] or 0
        opening_stock = item.base_stock - hist_dem + hist_in
        temp_stock = opening_stock
        for dt in date_range:
            dq = Demand.objects.filter(part=item.part, due_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0
            iq = Incoming.objects.filter(part=item.part, in_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0
            temp_stock = temp_stock - dq + iq
            daily_status.append({'date': dt, 'demand_qty': dq, 'in_qty': iq, 'stock': temp_stock, 'is_danger': temp_stock < 0})
        inventory_data.append({'vendor_name': item.part.vendor.name, 'part_no': item.part.part_no, 'part_name': item.part.part_name, 'base_stock': opening_stock, 'daily_status': daily_status})
    latest_inv = Inventory.objects.exclude(last_inventory_date__isnull=True).order_by('-last_inventory_date').first()
    return render(request, 'inventory_list.html', {'date_range': date_range, 'inventory_data': inventory_data, 'vendor_list': vendor_list, 'active_menu': 'inventory', 'show_all': show_all, 'selected_vendor_id': selected_v, 'user_name': user.username, 'vendor_name': user.vendor.name if hasattr(user, 'vendor') else "관리자", 'q': q, 'inventory_ref_date': latest_inv.last_inventory_date if latest_inv else None})

@login_required
@menu_permission_required('can_view_inventory')
def inventory_export(request):
    user = request.user; today = timezone.localtime().date()
    max_due = Demand.objects.aggregate(Max('due_date'))['due_date__max']
    end_date = max_due if max_due and max_due > (today + datetime.timedelta(days=31)) else (today + datetime.timedelta(days=31))
    dr = [today + datetime.timedelta(days=i) for i in range((end_date - today).days + 1)]
    items = Inventory.objects.select_related('part', 'part__vendor').all()
    if not user.is_superuser and hasattr(user, 'vendor'): items = items.filter(part__vendor=user.vendor)
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(['협력사', '품번', '품명', '구분'] + [d.strftime('%m/%d') for d in dr])
    for item in items:
        ref = item.last_inventory_date or date(2000, 1, 1); stock = item.base_stock - (Demand.objects.filter(part=item.part, due_date__gt=ref, due_date__lt=today).aggregate(Sum('quantity'))['quantity__sum'] or 0) + (Incoming.objects.filter(part=item.part, in_date__gt=ref, in_date__lt=today).aggregate(Sum('quantity'))['quantity__sum'] or 0)
        r1, r2, r3 = [item.part.vendor.name, item.part.part_no, item.part.part_name, '소요량'], ['', '', '', '입고량'], ['', '', '', '재고']
        for dt in dr:
            dq = Demand.objects.filter(part=item.part, due_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0 if dt >= ref else 0
            iq = Incoming.objects.filter(part=item.part, in_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0 if dt >= ref else 0
            stock = stock - dq + iq
            r1.append(dq); r2.append(iq); r3.append(stock)
        ws.append(r1); ws.append(r2); ws.append(r3)
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'); response['Content-Disposition'] = f'attachment; filename=Inventory_{today}.xlsx'; wb.save(response); return response

@login_required
@require_POST
def quick_order_action(request):
    user = request.user
    if not (user.is_superuser or (hasattr(user, 'profile') and user.profile.role == 'STAFF')):
        messages.error(request, "발주 등록 권한이 없습니다."); return redirect('inventory_list')
    v_name, p_no, qty, due = request.POST.get('vendor_name'), request.POST.get('part_no'), request.POST.get('quantity'), request.POST.get('due_date')
    try:
        part = Part.objects.filter(part_no=p_no, vendor__name=v_name).first()
        if part: Order.objects.create(vendor=part.vendor, part_no=p_no, part_name=part.part_name, part_group=part.part_group, quantity=int(qty), due_date=due); messages.success(request, f"발주 완료: {p_no}")
    except Exception as e: messages.error(request, str(e))
    return redirect('inventory_list')

@login_required
@menu_permission_required('can_view_inventory')
def demand_manage(request):
    if not request.user.is_superuser: return redirect('inventory_list')
    v_id, p_no, sd, ed = request.GET.get('vendor_id'), request.GET.get('part_no'), request.GET.get('start_date'), request.GET.get('end_date')
    demands = Demand.objects.select_related('part', 'part__vendor').all().order_by('-due_date')
    if v_id: demands = demands.filter(part__vendor_id=v_id)
    if p_no: demands = demands.filter(part__part_no__icontains=p_no)
    if sd and ed: demands = demands.filter(due_date__range=[sd, ed])
    return render(request, 'demand_manage.html', {'demands': demands[:500], 'vendor_list': Vendor.objects.all().order_by('name'), 'active_menu': 'inventory'})

@login_required
@require_POST
def demand_delete_action(request):
    if not request.user.is_superuser: return redirect('inventory_list')
    Demand.objects.filter(id__in=request.POST.getlist('demand_ids')).delete()
    messages.success(request, "삭제 완료."); return redirect(request.META.get('HTTP_REFERER', 'demand_manage'))

@login_required
@require_POST
def delete_all_demands(request):
    if not request.user.is_superuser: return redirect('inventory_list')
    Demand.objects.all().delete(); messages.success(request, "전체 삭제 완료."); return redirect('inventory_list')

@login_required
@require_POST
def demand_upload_action(request):
    if not request.user.is_superuser: return redirect('inventory_list')
    if request.FILES.get('demand_file'):
        try:
            wb = openpyxl.load_workbook(request.FILES['demand_file'], read_only=True, data_only=True); ws = wb.active; c_count = 0
            all_parts = {p.part_no: p for p in Part.objects.select_related('vendor').all()}
            with transaction.atomic():
                for row in ws.iter_rows(min_row=2, values_only=True):
                    p_no = str(row[0]).strip() if row[0] else None
                    if not p_no or p_no not in all_parts: continue
                    Demand.objects.update_or_create(part=all_parts[p_no], due_date=row[2], defaults={'quantity': row[1] or 0}); c_count += 1
            messages.success(request, f"소요량 {c_count}건 반영 완료")
        except Exception as e: messages.error(request, f"업로드 중 오류 발생: {str(e)}")
    return redirect('inventory_list')

@login_required
@require_POST
def demand_update_ajax(request):
    if not request.user.is_superuser: return JsonResponse({'status': 'error'}, status=403)
    p_no, d_date, qty = request.POST.get('part_no'), request.POST.get('due_date'), request.POST.get('quantity')
    try:
        part = Part.objects.get(part_no=p_no)
        if int(qty) <= 0: Demand.objects.filter(part=part, due_date=d_date).delete()
        else: Demand.objects.update_or_create(part=part, due_date=d_date, defaults={'quantity': int(qty)})
        return JsonResponse({'status': 'success'})
    except Exception as e: return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@login_required
def inventory_upload(request):
    if not request.user.is_superuser: return redirect('inventory_list')
    return render(request, 'inventory_upload.html', {'active_menu': 'inventory', 'today': timezone.localtime().date()})

@login_required
@require_POST
def inventory_upload_action(request):
    if not request.user.is_superuser: return redirect('inventory_list')
    s_date = request.POST.get('inventory_date')
    if request.FILES.get('excel_file'):
        try:
            wb = openpyxl.load_workbook(request.FILES['excel_file'], read_only=True, data_only=True); ws = wb.active; u_count = 0
            all_parts = {p.part_no: p for p in Part.objects.all()}
            with transaction.atomic():
                for row in ws.iter_rows(min_row=2, values_only=True):
                    p_no = str(row[0]).strip() if row[0] else None
                    if not p_no or p_no not in all_parts: continue
                    part = all_parts[p_no]
                    inv, _ = Inventory.objects.get_or_create(part=part)
                    inv.base_stock = int(row[1]) if row[1] is not None else 0
                    inv.last_inventory_date = s_date; inv.save(); u_count += 1
            messages.success(request, f"재고 초기화 완료: {u_count}건 반영됨")
        except Exception as e: messages.error(request, str(e))
    return redirect('inventory_list')

# [4. 라벨/입고 관리]
@login_required
@menu_permission_required('can_register_orders') 
def label_list(request):
    user = request.user; selected_v = request.GET.get('vendor_id'); status_filter = request.GET.get('status'); q = request.GET.get('q', '')
    pnos_with_delivery = DeliveryOrderItem.objects.values_list('part_no', flat=True).distinct()
    vendor_ids = Part.objects.filter(part_no__in=pnos_with_delivery).values_list('vendor_id', flat=True).distinct()
    vendor_list = Vendor.objects.filter(id__in=vendor_ids).order_by('name') if (user.is_superuser or user.profile.role == 'STAFF') else []
    recent_orders = DeliveryOrder.objects.all().prefetch_related('items').order_by('-created_at')[:15]
    if not (user.is_superuser or user.profile.role == 'STAFF'): recent_orders = recent_orders.filter(items__part_no__in=Part.objects.filter(vendor=user.vendor).values_list('part_no', flat=True)).distinct()
    elif selected_v: recent_orders = recent_orders.filter(items__part_no__in=Part.objects.filter(vendor_id=selected_v).values_list('part_no', flat=True)).distinct()
    if status_filter == 'registered': recent_orders = recent_orders.filter(is_received=False)
    elif status_filter == 'received': recent_orders = recent_orders.filter(is_received=True)
    order_q = Order.objects.filter(is_closed=False, approved_at__isnull=False)
    if not user.is_superuser: order_q = order_q.filter(vendor=user.vendor)
    elif selected_v: order_q = order_q.filter(vendor_id=selected_v)
    if q: order_q = order_q.filter(Q(part_no__icontains=q) | Q(part_name__icontains=q))

    label_data = []
    for p_no in order_q.values_list('part_no', flat=True).distinct():
        order_row = order_q.filter(part_no=p_no).first()
        active_t = order_q.filter(part_no=p_no).aggregate(Sum('quantity'))['quantity__sum'] or 0
        total_p = LabelPrintLog.objects.filter(part_no=p_no).aggregate(Sum('printed_qty'))['printed_qty__sum'] or 0
        closed_t = Order.objects.filter(part_no=p_no, is_closed=True, approved_at__isnull=False).aggregate(Sum('quantity'))['quantity__sum'] or 0
        
        # [✅ 최종 오타 수정] 정의되지 않은 'used_for_closed' 변수를 'closed_t'로 교정
        current_printed = max(0, total_p - closed_t)
        remain = active_t - current_printed
        
        if remain > 0: label_data.append({'part_no': p_no, 'part_name': order_row.part_name, 'total_order': active_t, 'remain': remain})
    return render(request, 'label_list.html', {'label_data': label_data, 'recent_orders': recent_orders, 'vendor_list': vendor_list, 'selected_vendor_id': selected_v, 'status_filter': status_filter, 'active_menu': 'label', 'q': q})

@login_required
@require_POST
def delete_delivery_order(request, order_id):
    order = get_object_or_404(DeliveryOrder, pk=order_id)
    if order.is_received: messages.error(request, "이미 입고된 납품서는 삭제 불가"); return redirect('label_list')
    with transaction.atomic():
        for item in order.items.all(): LabelPrintLog.objects.filter(part_no=item.part_no, printed_qty=item.total_qty, printed_at__date=order.created_at.date()).delete()
        order.delete()
    return redirect('label_list')

@login_required
def label_print_action(request): return redirect('label_list')

@login_required
@require_POST
def create_delivery_order(request):
    p_nos, snps, b_counts = request.POST.getlist('part_nos[]'), request.POST.getlist('snps[]'), request.POST.getlist('box_counts[]')
    with transaction.atomic():
        do = DeliveryOrder.objects.create(order_no="DO-"+timezone.now().strftime("%Y%m%d-%H%M%S"))
        for i in range(len(p_nos)):
            part = Part.objects.filter(part_no=p_nos[i]).first(); qty = int(snps[i]) * int(b_counts[i])
            DeliveryOrderItem.objects.create(order=do, part_no=p_nos[i], part_name=part.part_name, snp=int(snps[i]), box_count=int(b_counts[i]), total_qty=qty)
            LabelPrintLog.objects.create(vendor=part.vendor, part=part, part_no=p_nos[i], printed_qty=qty, snp=int(snps[i]))
    return redirect('label_list')

@login_required
def label_print(request, order_id):
    order = get_object_or_404(DeliveryOrder, pk=order_id); queue = []
    first_item = order.items.first(); part = Part.objects.filter(part_no=first_item.part_no).first() if first_item else None; v_name = part.vendor.name if part else "알수없음"
    for item in order.items.all():
        for _ in range(item.box_count): queue.append({'vendor_name': v_name, 'part_name': item.part_name, 'part_no': item.part_no, 'snp': item.snp, 'print_date': timezone.now()})
    return render(request, 'print_label.html', {'box_count': queue, 'vendor_name': v_name})

@login_required
def delivery_note_print(request, order_id):
    do = get_object_or_404(DeliveryOrder, pk=order_id); items = do.items.all(); first_item = items.first(); part = Part.objects.filter(part_no=first_item.part_no).first() if first_item else None; vendor = part.vendor if part else None
    return render(request, 'print_delivery_note.html', {'order': do, 'items': items, 'total_qty': items.aggregate(Sum('total_qty'))['total_qty__sum'] or 0, 'total_box': items.aggregate(Sum('box_count'))['box_count__sum'] or 0, 'print_date': timezone.localtime().date(), 'vendor': vendor})

@login_required
@require_POST
@menu_permission_required('can_manage_incoming') 
def receive_delivery_order_scan(request):
    qr_code = request.POST.get('qr_code', '').strip(); do = DeliveryOrder.objects.filter(order_no=qr_code).first()
    if not do or do.is_received: return redirect('incoming_list')
    with transaction.atomic():
        for item in do.items.all():
            part = Part.objects.filter(part_no=item.part_no).first()
            if part: 
                Incoming.objects.create(part=part, quantity=item.total_qty, in_date=timezone.localtime().date(), delivery_order_no=do.order_no)
                inv, _ = Inventory.objects.get_or_create(part=part); inv.base_stock += item.total_qty; inv.save()
        do.is_received = True; do.save(); messages.success(request, f"납품서 {do.order_no} 입고 처리가 완료되었습니다.")
    return redirect('incoming_list')

@login_required
@require_POST
@menu_permission_required('can_manage_incoming')
def incoming_cancel(request):
    inc_id = request.POST.get('incoming_id'); mode = request.POST.get('cancel_mode'); target_inc = get_object_or_404(Incoming, id=inc_id); do_no = target_inc.delivery_order_no; do = DeliveryOrder.objects.filter(order_no=do_no).first()
    with transaction.atomic():
        if mode == 'item':
            inv = Inventory.objects.get(part=target_inc.part); inv.base_stock -= target_inc.quantity; inv.save()
            if do:
                LabelPrintLog.objects.filter(part_no=target_inc.part.part_no, printed_qty=target_inc.quantity).delete()
                DeliveryOrderItem.objects.filter(order=do, part_no=target_inc.part.part_no, total_qty=target_inc.quantity).delete()
            target_inc.delete(); messages.success(request, f"품목 {target_inc.part.part_no} 입고 취소 및 잔량이 복구되었습니다.")
        elif mode == 'all':
            all_incs = Incoming.objects.filter(delivery_order_no=do_no)
            for inc in all_incs:
                inv = Inventory.objects.get(part=inc.part); inv.base_stock -= inc.quantity; inv.save(); inc.delete()
            if do: do.is_received = False; do.save()
            messages.success(request, f"납품서 {do_no} 입고 취소 완료. (품목 데이터는 보존됩니다)")
    return redirect('incoming_list')

@login_required
@menu_permission_required('can_manage_incoming') 
def incoming_list(request):
    user = request.user; selected_v = request.GET.get('vendor_id'); sd, ed, q = request.GET.get('start_date'), request.GET.get('end_date'), request.GET.get('q', '')
    incomings = Incoming.objects.select_related('part', 'part__vendor').all().order_by('-in_date', '-created_at')
    vendor_ids = Incoming.objects.values_list('part__vendor_id', flat=True).distinct(); vendor_list = Vendor.objects.filter(id__in=vendor_ids).order_by('name') if (user.is_superuser or user.profile.role == 'STAFF') else []
    if not user.is_superuser and user.profile.role != 'STAFF': incomings = incomings.filter(part__vendor=user.vendor)
    elif selected_v: incomings = incomings.filter(part__vendor_id=selected_v)
    if sd and ed: incomings = incomings.filter(in_date__range=[sd, ed])
    if q: incomings = incomings.filter(Q(part__part_no__icontains=q) | Q(part__part_name__icontains=q))
    return render(request, 'incoming_list.html', {'incomings': incomings, 'active_menu': 'incoming', 'start_date': sd, 'end_date': ed, 'q': q, 'vendor_list': vendor_list, 'selected_vendor_id': selected_v})

@login_required
@menu_permission_required('can_manage_incoming')
def incoming_export(request):
    incomings = Incoming.objects.select_related('part', 'part__vendor').all()
    if not request.user.is_superuser and hasattr(request.user, 'vendor'): incomings = incomings.filter(part__vendor=request.user.vendor)
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(['입고일자', '협력사', '품번', '품명', '입고수량', '처리일시'])
    for i in incomings: ws.append([i.in_date, i.part.vendor.name, i.part.part_no, i.part.part_name, i.quantity, i.created_at.strftime("%Y-%m-%d %H:%M")])
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'); response['Content-Disposition'] = 'attachment; filename=Incomings.xlsx'; wb.save(response); return response

# [5. 관리자 대시보드]
@staff_member_required
@menu_permission_required('can_access_scm_admin') 
def scm_admin_main(request):
    today = timezone.localtime().date(); overdue_list = []; active_overdue_orders = Order.objects.filter(due_date__lt=today, is_closed=False, approved_at__isnull=False).order_by('due_date')
    for order in active_overdue_orders:
        total_printed = LabelPrintLog.objects.filter(part_no=order.part_no).aggregate(Sum('printed_qty'))['printed_qty__sum'] or 0
        closed_qty = Order.objects.filter(part_no=order.part_no, is_closed=True).aggregate(Sum('quantity'))['quantity__sum'] or 0
        current_printed = max(0, total_printed - closed_qty)
        remain = order.quantity - current_printed
        if remain > 0: overdue_list.append({'due_date': order.due_date, 'vendor_name': order.vendor.name, 'part_no': order.part_no, 'part_name': order.part_name, 'remain_qty': remain, 'days_diff': (today - order.due_date).days})
    summary = {'total_vendors': Vendor.objects.count(), 'total_parts': Part.objects.count(), 'unapproved_orders': Order.objects.filter(approved_at__isnull=True, is_closed=False).count(), 'today_incoming': Incoming.objects.filter(in_date=today).aggregate(Sum('quantity'))['quantity__sum'] or 0, 'overdue_count': len(overdue_list)}
    recent_incomings = Incoming.objects.select_related('part', 'part__vendor').all().order_by('-created_at')[:10]
    return render(request, 'scm_admin_main.html', {'summary': summary, 'recent_incomings': recent_incomings, 'overdue_orders': overdue_list[:10], 'active_menu': 'admin_main', 'user_name': request.user.username, 'vendor_name': "시스템 관리자"})