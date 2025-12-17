from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.utils import timezone
from django.http import HttpResponse
from django.contrib import messages
import openpyxl 
from datetime import timedelta
from .models import Order, Vendor, Part, Inventory, Incoming 

# [1. 조회 화면]
@login_required
def order_list(request):
    user = request.user
    vendor_list = Vendor.objects.all().order_by('name') if user.is_superuser else []
    
    if user.is_superuser:
        orders = Order.objects.all().order_by('-created_at')
        vendor_name = "전체 관리자"
    elif hasattr(user, 'vendor'): 
        orders = Order.objects.filter(vendor=user.vendor).order_by('-created_at')
        vendor_name = user.vendor.name
    else:
        orders = Order.objects.none()
        vendor_name = "소속 없음"

    selected_vendor = request.GET.get('vendor_id') 
    if user.is_superuser and selected_vendor:
        orders = orders.filter(vendor_id=selected_vendor)
    
    status_filter = request.GET.get('status')
    if status_filter == 'unapproved':
        orders = orders.filter(approved_at__isnull=True)
    elif status_filter == 'approved':
        orders = orders.filter(approved_at__isnull=False)

    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    if start_date and end_date:
        orders = orders.filter(due_date__range=[start_date, end_date])
    
    q = request.GET.get('q', '')
    if q:
        orders = orders.filter(Q(part_no__icontains=q) | Q(part_name__icontains=q))

    context = {
        'orders': orders, 'user_name': user.username, 'vendor_name': vendor_name,
        'q': q, 'vendor_list': vendor_list, 'selected_vendor': selected_vendor,
        'status_filter': status_filter, 'start_date': start_date, 'end_date': end_date,
        'active_menu': 'list',
    }
    return render(request, 'order_list.html', context)

# [2. 발주 등록 화면 - 관리자 전용]
@login_required
def order_upload(request):
    if not request.user.is_superuser:
        messages.error(request, "발주 등록 권한이 없습니다.")
        return redirect('order_list')
    return render(request, 'order_upload.html', {'active_menu': 'upload'})

# [3. 엑셀 업로드 처리 - 관리자 전용]
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
            
            for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                v_name, p_no, qty, due = row
                if not v_name or not p_no: continue

                vendor = Vendor.objects.filter(name=v_name).first()
                part_master = Part.objects.filter(part_no=p_no).first()

                if vendor and part_master:
                    Order.objects.create(
                        vendor=vendor,
                        part_no=p_no,
                        part_name=part_master.part_name,
                        part_group=part_master.part_group,
                        quantity=qty if qty else 0,
                        due_date=due
                    )
                    created_count += 1
            
            if created_count > 0:
                messages.success(request, f"{created_count}건의 발주가 등록되었습니다.")
            else:
                messages.warning(request, "저장된 데이터가 없습니다. 협력사명과 품번을 확인하세요.")
        except Exception as e:
            messages.error(request, f"오류 발생: {str(e)}")
            
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

# [5. 미확인 발주 일괄 승인]
@login_required
def order_approve_all(request):
    user = request.user
    if user.is_superuser:
        orders_to_approve = Order.objects.filter(approved_at__isnull=True)
    elif hasattr(user, 'vendor'):
        orders_to_approve = Order.objects.filter(vendor=user.vendor, approved_at__isnull=True)
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
        if not order.approved_at:
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
        status = "승인완료" if o.approved_at else "미확인"
        ws.append([status, o.created_at.date(), o.approved_at.date() if o.approved_at else "-", o.vendor.name, o.part_group, o.part_no, o.part_name, o.quantity, str(o.due_date)])
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=orders.xlsx'
    wb.save(response)
    return response

# [7. 과부족 조회 현황 - D+14 필터링 강화 버전]
@login_required
def inventory_list(request):
    user = request.user
    today = timezone.now().date()
    end_date = today + timedelta(days=14)
    date_range = [today + timedelta(days=i) for i in range(15)]
    
    show_all = request.GET.get('show_all') == 'true'
    selected_vendor_id = request.GET.get('vendor_id')
    
    vendor_list = []
    if user.is_superuser:
        inventory_items = Inventory.objects.select_related('part', 'part__vendor').all()
        vendor_list = Vendor.objects.all().order_by('name')
        vendor_name = "전체 관리자"
    elif hasattr(user, 'vendor'):
        inventory_items = Inventory.objects.select_related('part', 'part__vendor').filter(part__vendor=user.vendor)
        vendor_name = user.vendor.name
    else:
        inventory_items = Inventory.objects.none()
        vendor_name = "소속 없음"

    if user.is_superuser and selected_vendor_id:
        inventory_items = inventory_items.filter(part__vendor_id=selected_vendor_id)

    if not show_all:
        active_part_nos = Order.objects.filter(due_date__range=[today, end_date]).values_list('part_no', flat=True).distinct()
        inventory_items = inventory_items.filter(part__part_no__in=active_part_nos)
    
    inventory_data = []
    for item in inventory_items:
        daily_status = []
        running_stock = item.base_stock  
        for dt in date_range:
            daily_order = Order.objects.filter(part_no=item.part.part_no, due_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0
            daily_in = Incoming.objects.filter(part=item.part, in_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0
            running_stock = running_stock - daily_order + daily_in
            daily_status.append({'date': dt, 'order_qty': daily_order, 'in_qty': daily_in, 'stock': running_stock, 'is_danger': running_stock < 0})
            
        inventory_data.append({'vendor_name': item.part.vendor.name, 'part_no': item.part.part_no, 'part_name': item.part.part_name, 'base_stock': item.base_stock, 'daily_status': daily_status})

    context = {
        'date_range': date_range, 'inventory_data': inventory_data, 'user_name': user.username, 'vendor_name': vendor_name,
        'active_menu': 'inventory', 'show_all': show_all, 'vendor_list': vendor_list, 'selected_vendor_id': selected_vendor_id,
    }
    return render(request, 'inventory_list.html', context)

# [8. 과부족 현황 엑셀 내보내기 - 3단계 추가]
@login_required
def inventory_export(request):
    user = request.user
    today = timezone.now().date()
    date_range = [today + timedelta(days=i) for i in range(15)]
    
    show_all = request.GET.get('show_all') == 'true'
    selected_vendor_id = request.GET.get('vendor_id')
    
    # 리스트 조회와 동일한 필터링 적용
    if user.is_superuser:
        items = Inventory.objects.select_related('part', 'part__vendor').all()
        if selected_vendor_id:
            items = items.filter(part__vendor_id=selected_vendor_id)
    elif hasattr(user, 'vendor'):
        items = Inventory.objects.select_related('part', 'part__vendor').filter(part__vendor=user.vendor)
    else:
        return redirect('inventory_list')

    if not show_all:
        active_nos = Order.objects.filter(due_date__range=[today, today + timedelta(days=14)]).values_list('part_no', flat=True).distinct()
        items = items.filter(part__part_no__in=active_nos)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "D+14_수급현황"

    header = ['협력사', '품번', '품명', '구분', '기초재고'] + [dt.strftime('%m/%d') for dt in date_range]
    ws.append(header)

    for item in items:
        running_stock = item.base_stock
        row_order = [item.part.vendor.name, item.part.part_no, item.part.part_name, '소요량', item.base_stock]
        row_in = ['', '', '', '입고량', '']
        row_stock = ['', '', '', '과부족', '']

        for dt in date_range:
            d_order = Order.objects.filter(part_no=item.part.part_no, due_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0
            d_in = Incoming.objects.filter(part=item.part, in_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0
            running_stock = running_stock - d_order + d_in
            row_order.append(d_order)
            row_in.append(d_in)
            row_stock.append(running_stock)

        ws.append(row_order)
        ws.append(row_in)
        ws.append(row_stock)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename=Inventory_Status_{today}.xlsx'
    wb.save(response)
    return response