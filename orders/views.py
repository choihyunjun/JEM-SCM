# orders/views.py

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q, Sum, Case, When, Value, IntegerField, Max, Count, F
from django.utils import timezone
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.db import transaction
from functools import wraps
import openpyxl
import datetime
from datetime import timedelta, date

# SCM 모델 임포트 (ReturnLog 추가됨)
from .models import Order, Vendor, Part, Inventory, Incoming, LabelPrintLog, DeliveryOrder, DeliveryOrderItem, Demand, ReturnLog

# [신규] 타 앱(WMS, QMS) 모델 임포트 (연동용)
try:
    from material.models import Warehouse, MaterialStock, MaterialTransaction
    from qms.models import ImportInspection
except ImportError:
    Warehouse = None
    MaterialStock = None
    MaterialTransaction = None
    ImportInspection = None

# ==========================================
# [0. 필수 공통 로직 및 권한 설정]
# ==========================================

def _get_profile(user):
    return getattr(user, 'profile', None)

def _get_role(user) -> str:
    profile = _get_profile(user)
    if not profile:
        return 'VENDOR'
    role = getattr(profile, 'role', None)
    if role == 'ADMIN':
        return 'ADMIN'
    if role == 'VENDOR':
        return 'VENDOR'
    return 'STAFF'

def _is_internal(user) -> bool:
    profile = _get_profile(user)
    if not profile:
        return False
    if hasattr(profile, 'account_type'):
        return profile.account_type == 'INTERNAL'
    return _get_role(user) != 'VENDOR'

def _get_user_vendor(user):
    return Vendor.objects.filter(user=user).first()

ROLE_MENU_PERMS = {
    'ADMIN': {'can_view_orders', 'can_register_orders', 'can_view_inventory', 'can_manage_incoming', 'can_access_scm_admin'},
    'STAFF': {'can_view_orders', 'can_register_orders', 'can_view_inventory', 'can_manage_incoming'},
    'VENDOR': {'can_view_orders', 'can_register_orders', 'can_view_inventory', 'can_manage_incoming'},
}

ROLE_ACTION_PERMS = {
    'ADMIN': {
        'order.upload', 'order.delete', 'order.close', 'order.approve', 'order.approve_all', 'order.export',
        'inv.upload', 'inv.adjust', 'inv.export',
        'incoming.scan', 'incoming.cancel', 'incoming.export',
        'demand.upload', 'demand.edit', 'demand.delete', 'demand.delete_all', 'demand.export',
        'label.print', 'delivery.print', 'delivery.register', 'delivery.delete',
    },
    'STAFF': {
        'order.upload', 'order.delete', 'order.approve', 'order.export',
        'inv.upload', 'inv.export',
        'incoming.scan', 'incoming.export',
        'demand.upload', 'demand.edit', 'demand.delete', 'demand.export',
        'label.print', 'delivery.print', 'delivery.register', 'delivery.delete',
    },
    'VENDOR': {
        'delivery.register',
        'label.print', 'delivery.print',
    },
}

def role_has_menu_perm(user, permission_field: str) -> bool:
    if getattr(user, 'is_superuser', False):
        return True
    role = _get_role(user)
    allowed = ROLE_MENU_PERMS.get(role, set())
    return permission_field in allowed

def has_action_perm(user, action: str) -> bool:
    if getattr(user, 'is_superuser', False):
        return True
    role = _get_role(user)
    return action in ROLE_ACTION_PERMS.get(role, set())

def require_action_perm(request, action: str):
    if has_action_perm(request.user, action):
        return
    messages.error(request, f"권한이 없습니다. (필요 권한: {action})")
    return redirect('order_list')

def scope_qs_for_user(user, qs):
    if _get_role(user) == 'VENDOR':
        v = _get_user_vendor(user)
        if not v:
            return qs.none()
        if hasattr(qs.model, 'vendor_id') or 'vendor' in [f.name for f in qs.model._meta.fields]:
            try:
                return qs.filter(vendor=v)
            except Exception:
                return qs.none()
    return qs

def menu_permission_required(permission_field):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            if role_has_menu_perm(request.user, permission_field):
                return view_func(request, *args, **kwargs)

            if request.resolver_match.url_name == 'order_list':
                messages.error(request, f"귀하의 계정은 '{permission_field}' 권한이 활성화되지 않았습니다. 관리자에게 문의하세요.")
                return render(request, 'order_list.html', {'orders': [], 'vendor_name': '권한 없음'})

            messages.error(request, "해당 메뉴에 대한 접근 권한이 없습니다.")
            return redirect('order_list')
        return _wrapped_view
    return decorator

def login_success(request):
    return redirect('order_list')

# ==========================================
# [1. 발주 조회 화면]
# ==========================================

@login_required
@menu_permission_required('can_view_orders')
def order_list(request):
    user = request.user
    user_vendor = Vendor.objects.filter(user=user).first()

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
    elif user_vendor:
        orders = order_queryset.filter(vendor=user_vendor).order_by('status_priority', sort_by, '-created_at')
        vendor_name = user_vendor.name
    else:
        orders = order_queryset.all().order_by('status_priority', sort_by, '-created_at')
        vendor_name = "시스템 운영자"

    selected_vendor = request.GET.get('vendor_id')
    if (user.is_superuser or not user_vendor) and selected_vendor:
        orders = orders.filter(vendor_id=selected_vendor)

    status_filter = request.GET.get('status')
    if status_filter:
        if status_filter == 'unapproved':
            orders = orders.filter(approved_at__isnull=True, is_closed=False)
        elif status_filter == 'approved':
            orders = orders.filter(approved_at__isnull=False, is_closed=False)
        elif status_filter == 'closed':
            orders = orders.filter(is_closed=True)

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

    if not (user.is_superuser or (_is_internal(user))):
        active_overdue = active_overdue.filter(vendor=user_vendor) if user_vendor else active_overdue.none()
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
                'vendor_name': o.vendor.name if o.vendor else "미지정",
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

# ==========================================
# [2. 발주 관련 액션]
# ==========================================

@login_required
def order_upload(request):
    resp = require_action_perm(request, 'order.upload')
    if resp:
        return resp
    if not request.user.is_superuser and _get_role(request.user) != 'STAFF':
        return redirect('order_list')
    return render(request, 'order_upload.html', {'active_menu': 'upload'})

@login_required
@require_POST
def order_upload_preview(request):
    resp = require_action_perm(request, 'order.upload')
    if resp:
        return resp

    if not request.FILES.get('excel_file'):
        messages.error(request, "파일을 선택해주세요.")
        return redirect('order_upload')

    preview_data = []

    try:
        wb = openpyxl.load_workbook(request.FILES['excel_file'], data_only=True)
        ws = wb.active

        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row[0] or not row[1] or not row[2] or not row[3]:
                continue

            raw_date = row[3]
            if isinstance(raw_date, datetime.datetime):
                fmt_date = raw_date.strftime("%Y-%m-%d")
            elif isinstance(raw_date, str):
                fmt_date = raw_date[:10]
            else:
                fmt_date = str(raw_date)

            vendor_name = str(row[0]).strip()
            part_no = str(row[1]).strip()
            quantity = int(row[2]) if row[2] else 0

            part_obj = Part.objects.filter(part_no=part_no).first()

            part_name = part_obj.part_name if part_obj else "품번 없음"
            part_group = part_obj.part_group if part_obj else ""
            part_found = True if part_obj else False

            item = {
                'vendor': vendor_name,
                'part_no': part_no,
                'part_name': part_name,
                'part_group': part_group,
                'part_found': part_found,
                'quantity': quantity,
                'due_date': fmt_date,
                'erp_order_no': str(row[4]).strip() if len(row) > 4 and row[4] else '',
                'erp_order_seq': str(row[5]).strip() if len(row) > 5 and row[5] else ''
            }
            preview_data.append(item)

        if not preview_data:
            messages.warning(request, "유효한 데이터가 없습니다. 엑셀 양식을 확인해주세요.")
            return redirect('order_upload')

        valid_count = sum(1 for item in preview_data if item['part_found'])
        error_count = len(preview_data) - valid_count

        if valid_count == 0:
            messages.warning(request, "등록 가능한 정상 품목이 없습니다. 품번을 확인해주세요.")
        else:
            messages.info(request, f"총 {len(preview_data)}건 중 정상 {valid_count}건, 오류 {error_count}건이 확인되었습니다.")

    except Exception as e:
        messages.error(request, f"엑셀 처리 중 오류: {str(e)}")
        return redirect('order_upload')

    return render(request, 'order_upload.html', {
        'active_menu': 'upload',
        'preview_data': preview_data,
        'valid_count': valid_count,
        'error_count': error_count
    })

@login_required
@require_POST
def order_create_confirm(request):
    resp = require_action_perm(request, 'order.upload')
    if resp:
        return resp

    vendors = request.POST.getlist('vendor_list[]')
    part_groups = request.POST.getlist('part_group_list[]')
    part_nos = request.POST.getlist('part_no_list[]')
    part_names = request.POST.getlist('part_name_list[]')
    quantities = request.POST.getlist('quantity_list[]')
    due_dates = request.POST.getlist('due_date_list[]')
    erp_orders = request.POST.getlist('erp_order_no_list[]')
    erp_seqs = request.POST.getlist('erp_order_seq_list[]')

    success_count = 0

    try:
        with transaction.atomic():
            for i in range(len(part_nos)):
                vendor_obj = Vendor.objects.filter(name=vendors[i]).first()
                part_obj = Part.objects.filter(part_no=part_nos[i]).first()
                if not vendor_obj and part_obj:
                    vendor_obj = part_obj.vendor

                if vendor_obj:
                    Order.objects.create(
                        vendor=vendor_obj,
                        part_group=part_groups[i],
                        part_no=part_nos[i],
                        part_name=part_names[i],
                        quantity=int(quantities[i]),
                        due_date=due_dates[i],
                        erp_order_no=erp_orders[i] if erp_orders[i] != 'None' else '',
                        erp_order_seq=erp_seqs[i] if erp_seqs[i] != 'None' else ''
                    )
                    success_count += 1

        messages.success(request, f"총 {success_count}건의 발주가 정상적으로 등록되었습니다.")
        return redirect('order_list')

    except Exception as e:
        messages.error(request, f"저장 중 오류 발생: {str(e)}")
        return redirect('order_upload')

@login_required
@require_POST
def order_delete(request):
    if not request.user.is_superuser:
        return redirect('order_list')
    Order.objects.filter(id__in=request.POST.getlist('order_ids')).delete()
    return redirect('order_list')

@login_required
@require_POST
def order_close_action(request):
    if not request.user.is_superuser:
        return redirect('order_list')
    Order.objects.filter(id__in=request.POST.getlist('order_ids')).update(is_closed=True)
    return redirect('order_list')

@login_required
def order_approve_all(request):
    q = Order.objects.filter(approved_at__isnull=True, is_closed=False)
    user_vendor = Vendor.objects.filter(user=request.user).first()
    if not request.user.is_superuser and user_vendor:
        q = q.filter(vendor=user_vendor)
    q.update(approved_at=timezone.now())
    return redirect('order_list')

@login_required
def order_approve(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    if not order.approved_at and not order.is_closed:
        order.approved_at = timezone.now()
        order.save()
    return redirect('order_list')

@login_required
def order_export(request):
    user_vendor = Vendor.objects.filter(user=request.user).first()
    orders = Order.objects.all().order_by('-created_at') if request.user.is_superuser else Order.objects.filter(vendor=user_vendor).order_by('-created_at')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['상태', '등록일', '승인일', '협력사', '품번', '품명', '수량', '납기일', 'ERP번호'])

    for o in orders:
        status = "마감" if o.is_closed else ("승인" if o.approved_at else "미확인")
        ws.append([status, o.created_at.date(), o.approved_at.date() if o.approved_at else "-", o.vendor.name, o.part_no, o.part_name, o.quantity, str(o.due_date), o.erp_order_no])

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=orders.xlsx'
    wb.save(response)
    return response

# ==========================================
# [3. 과부족/소요량 로직]
# ==========================================

@login_required
@menu_permission_required('can_view_inventory')
def inventory_list(request):
    user = request.user
    today = timezone.localtime().date()
    user_vendor = Vendor.objects.filter(user=user).first()

    if MaterialStock is None:
        messages.error(request, "WMS(MaterialStock) 연동 모델을 불러올 수 없습니다. material 앱/모델 연결을 확인해주세요.")
        return redirect('order_list')

    if user.is_superuser or not user_vendor:
        max_due = Demand.objects.aggregate(Max('due_date'))['due_date__max']
        standard_end = today + datetime.timedelta(days=31)
        end_date = max_due if max_due and max_due > standard_end else standard_end
    else:
        end_date = today + datetime.timedelta(days=14)

    date_range = [today + datetime.timedelta(days=i) for i in range((end_date - today).days + 1)]

    show_all = request.GET.get('show_all') == 'true'
    selected_v = request.GET.get('vendor_id')
    q = request.GET.get('q', '')

    part_qs = Part.objects.select_related('vendor').all().order_by('vendor__name', 'part_name')

    if user.is_superuser or not user_vendor:
        vendor_list = Vendor.objects.all().order_by('name')
        if selected_v:
            part_qs = part_qs.filter(vendor_id=selected_v)
    elif user_vendor:
        part_qs = part_qs.filter(vendor=user_vendor)
        vendor_list = []
    else:
        return redirect('order_list')

    if q:
        part_qs = part_qs.filter(Q(part_no__icontains=q) | Q(part_name__icontains=q))

    if not show_all:
        act_pnos = Demand.objects.filter(due_date__range=[today, end_date]).values_list('part__part_no', flat=True).distinct()
        wms_pnos = MaterialStock.objects.filter(quantity__gt=0).values_list('part__part_no', flat=True).distinct()
        combined_pnos = set(list(act_pnos) + list(wms_pnos))
        part_qs = part_qs.filter(part_no__in=combined_pnos)

    inventory_data = []

    for part in part_qs:
        daily_status = []

        wms_stock_agg = MaterialStock.objects.filter(part=part).aggregate(Sum('quantity'))
        current_wms_stock = wms_stock_agg['quantity__sum'] or 0

        temp_stock = current_wms_stock
        opening_stock = current_wms_stock

        for dt in date_range:
            dq = Demand.objects.filter(part=part, due_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0
            iq = Incoming.objects.filter(part=part, in_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0

            effective_iq = iq if dt > today else 0

            temp_stock = temp_stock - dq + effective_iq

            daily_status.append({
                'date': dt,
                'demand_qty': dq,
                'in_qty': iq,
                'stock': temp_stock,
                'is_danger': temp_stock < 0
            })

        inventory_data.append({
            'vendor_name': part.vendor.name,
            'part_no': part.part_no,
            'part_name': part.part_name,
            'base_stock': opening_stock,
            'daily_status': daily_status
        })

    latest_inv_date = None
    last_inv_obj = Inventory.objects.exclude(last_inventory_date__isnull=True).order_by('-last_inventory_date').first()
    if last_inv_obj:
        latest_inv_date = last_inv_obj.last_inventory_date

    return render(request, 'inventory_list.html', {
        'date_range': date_range,
        'inventory_data': inventory_data,
        'vendor_list': vendor_list,
        'active_menu': 'inventory',
        'show_all': show_all,
        'selected_vendor_id': selected_v,
        'user_name': user.username,
        'vendor_name': user_vendor.name if user_vendor else "관리자",
        'q': q,
        'inventory_ref_date': latest_inv_date
    })

@login_required
@menu_permission_required('can_view_inventory')
def inventory_export(request):
    user = request.user
    user_vendor = Vendor.objects.filter(user=user).first()

    # ✅ 직원/관리자(벤더가 아닌 계정)만 inv.export 권한 체크
    if (not user.is_superuser) and (not user_vendor):
        resp = require_action_perm(request, 'inv.export')
        if resp:
            return resp

    today = timezone.localtime().date()

    max_due = Demand.objects.aggregate(Max('due_date'))['due_date__max']
    end_date = max_due if max_due and max_due > (today + datetime.timedelta(days=31)) else (today + datetime.timedelta(days=31))
    dr = [today + datetime.timedelta(days=i) for i in range((end_date - today).days + 1)]

    items = Inventory.objects.select_related('part', 'part__vendor').all()
    if (not user.is_superuser) and user_vendor:
        items = items.filter(part__vendor=user_vendor)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['협력사', '품번', '품명', '구분'] + [d.strftime('%m/%d') for d in dr])

    for item in items:
        ref = item.last_inventory_date or date(2000, 1, 1)

        hist_dem = Demand.objects.filter(part=item.part, due_date__gt=ref, due_date__lt=today).aggregate(Sum('quantity'))['quantity__sum'] or 0
        hist_in = Incoming.objects.filter(part=item.part, in_date__gt=ref, in_date__lt=today).aggregate(Sum('quantity'))['quantity__sum'] or 0

        stock = item.base_stock - hist_dem + hist_in

        r1 = [item.part.vendor.name, item.part.part_no, item.part.part_name, '소요량']
        r2 = ['', '', '', '입고량']
        r3 = ['', '', '', '재고']

        for dt in dr:
            dq = Demand.objects.filter(part=item.part, due_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0
            iq = Incoming.objects.filter(part=item.part, in_date=dt).aggregate(Sum('quantity'))['quantity__sum'] or 0

            stock = stock - dq + iq

            r1.append(dq)
            r2.append(iq)
            r3.append(stock)

        ws.append(r1)
        ws.append(r2)
        ws.append(r3)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename=Inventory_{today}.xlsx'
    wb.save(response)
    return response


@login_required
@require_POST
def quick_order_action(request):
    user = request.user
    if not (user.is_superuser or (_is_internal(user))):
        messages.error(request, "발주 등록 권한이 없습니다.")
        return redirect('inventory_list')

    v_name = request.POST.get('vendor_name')
    p_no = request.POST.get('part_no')
    qty = request.POST.get('quantity')
    due = request.POST.get('due_date')

    try:
        part = Part.objects.filter(part_no=p_no, vendor__name=v_name).first()
        if part:
            Order.objects.create(
                vendor=part.vendor,
                part_no=p_no,
                part_name=part.part_name,
                part_group=part.part_group,
                quantity=int(qty),
                due_date=due
            )
            messages.success(request, f"발주 완료: {p_no}")
    except Exception as e:
        messages.error(request, str(e))

    return redirect('inventory_list')

@login_required
@menu_permission_required('can_view_inventory')
def demand_manage(request):
    if not request.user.is_superuser:
        return redirect('inventory_list')

    v_id, p_no, sd, ed = request.GET.get('vendor_id'), request.GET.get('part_no'), request.GET.get('start_date'), request.GET.get('end_date')
    demands = Demand.objects.select_related('part', 'part__vendor').all().order_by('-due_date')

    if v_id:
        demands = demands.filter(part__vendor_id=v_id)
    if p_no:
        demands = demands.filter(part__part_no__icontains=p_no)
    if sd and ed:
        demands = demands.filter(due_date__range=[sd, ed])

    return render(
        request,
        'demand_manage.html',
        {'demands': demands[:500], 'vendor_list': Vendor.objects.all().order_by('name'), 'active_menu': 'inventory'}
    )

@login_required
@require_POST
def demand_delete_action(request):
    resp = require_action_perm(request, 'demand.delete')
    if resp:
        return resp

    if not request.user.is_superuser:
        return redirect('inventory_list')
    Demand.objects.filter(id__in=request.POST.getlist('demand_ids')).delete()
    messages.success(request, "삭제 완료.")
    return redirect(request.META.get('HTTP_REFERER', 'demand_manage'))

@login_required
@require_POST
def delete_all_demands(request):
    resp = require_action_perm(request, 'demand.delete_all')
    if resp:
        return resp

    if not request.user.is_superuser:
        return redirect('inventory_list')
    Demand.objects.all().delete()
    messages.success(request, "전체 삭제 완료.")
    return redirect('inventory_list')

@login_required
@require_POST
def demand_upload_action(request):
    resp = require_action_perm(request, 'demand.upload')
    if resp:
        return resp

    if not request.user.is_superuser:
        return redirect('inventory_list')
    if request.FILES.get('demand_file'):
        try:
            wb = openpyxl.load_workbook(request.FILES['demand_file'], read_only=True, data_only=True)
            ws = wb.active
            c_count = 0
            all_parts = {p.part_no: p for p in Part.objects.select_related('vendor').all()}

            with transaction.atomic():
                for row in ws.iter_rows(min_row=2, values_only=True):
                    p_no = str(row[0]).strip() if row[0] else None
                    if not p_no or p_no not in all_parts:
                        continue
                    Demand.objects.update_or_create(part=all_parts[p_no], due_date=row[2], defaults={'quantity': row[1] or 0})
                    c_count += 1

            messages.success(request, f"소요량 {c_count}건 반영 완료")

        except Exception as e:
            messages.error(request, f"업로드 중 오류: {str(e)}")

    return redirect('inventory_list')

@login_required
@require_POST
def demand_update_ajax(request):
    resp = require_action_perm(request, 'demand.edit')
    if resp:
        return resp

    if not request.user.is_superuser:
        return JsonResponse({'status': 'error'}, status=403)

    p_no, d_date, qty = request.POST.get('part_no'), request.POST.get('due_date'), request.POST.get('quantity')

    try:
        part = Part.objects.get(part_no=p_no)

        if int(qty) <= 0:
            Demand.objects.filter(part=part, due_date=d_date).delete()
        else:
            Demand.objects.update_or_create(part=part, due_date=d_date, defaults={'quantity': int(qty)})

        return JsonResponse({'status': 'success'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

# ==========================================
# [4. 라벨/입고 관리]
# ==========================================

@login_required
@menu_permission_required('can_register_orders')
def label_list(request):
    user = request.user
    selected_v = request.GET.get('vendor_id')
    status_filter = request.GET.get('status')
    q = request.GET.get('q', '')

    user_vendor = Vendor.objects.filter(user=user).first()

    pnos_with_delivery = DeliveryOrderItem.objects.values_list('part_no', flat=True).distinct()
    vendor_ids = Part.objects.filter(part_no__in=pnos_with_delivery).values_list('vendor_id', flat=True).distinct()

    profile = getattr(user, 'profile', None)
    is_staff_or_admin = user.is_superuser or (profile and _is_internal(user))

    vendor_list = Vendor.objects.filter(id__in=vendor_ids).order_by('name') if is_staff_or_admin else []
    
    # [수정] 기본 쿼리셋: vendor 필드 없이 items__part__vendor 등을 통해 역추적해야 함
    # 하지만 items는 ManyToMany or ReverseFK 이므로 .distinct() 주의
    # 만약 DeliveryOrder 모델에 'vendor' 필드가 없다면 select_related('vendor')는 제거해야 함.
    # 안전하게 제거하고 진행합니다.
    do_qs = DeliveryOrder.objects.prefetch_related('items').order_by('-created_at')

    if not is_staff_or_admin:
        if user_vendor:
            # Part(vendor=user_vendor) → part_no 목록
            vendor_part_nos = Part.objects.filter(
                vendor=user_vendor
            ).values_list('part_no', flat=True)

            do_ids = DeliveryOrderItem.objects.filter(
                part_no__in=vendor_part_nos
            ).values_list('order_id', flat=True)

            do_qs = do_qs.filter(id__in=do_ids).distinct()
        else:
            do_qs = do_qs.none()

    elif selected_v:
        selected_part_nos = Part.objects.filter(
            vendor_id=selected_v
        ).values_list('part_no', flat=True)

        do_ids = DeliveryOrderItem.objects.filter(
            part_no__in=selected_part_nos
        ).values_list('order_id', flat=True)

        do_qs = do_qs.filter(id__in=do_ids).distinct()
    # 2. 상태별 리스트 분리
    recent_orders = do_qs.exclude(status='REJECTED')

    if status_filter == 'registered':
        recent_orders = recent_orders.filter(status='PENDING')
    elif status_filter == 'received':
        recent_orders = recent_orders.filter(status__in=['RECEIVED', 'APPROVED'])

    recent_orders = recent_orders[:20]

    # [수정] 부적합/반출 관리 탭 데이터 (ReturnLog 기준)
    if user_vendor:
        return_logs_qs = ReturnLog.objects.filter(
            part__vendor=user_vendor
        ).select_related(
            'delivery_order',
            'part'
        ).order_by(
            'is_confirmed',
            '-created_at'
        )
    else:
        return_logs_qs = ReturnLog.objects.all().select_related(
            'delivery_order',
            'part'
        ).order_by(
            'is_confirmed',
            '-created_at'
        )

    # 🔴 미확인 건수 (뱃지용)
    return_pending_count = return_logs_qs.filter(
        is_confirmed=False
    ).count()

    return_logs = return_logs_qs
        
    # 3. 라벨 발행 데이터 (잔량 계산 로직)
    label_data = []
    
    order_q = Order.objects.filter(is_closed=False, approved_at__isnull=False)
    if not is_staff_or_admin and user_vendor:
        order_q = order_q.filter(vendor=user_vendor)
    elif selected_v:
        order_q = order_q.filter(vendor_id=selected_v)

    if q:
        order_q = order_q.filter(Q(part_no__icontains=q) | Q(part_name__icontains=q))

    # [A] ERP 발주 건
    erp_orders = order_q.exclude(erp_order_no__isnull=True).exclude(erp_order_no='')
    for o in erp_orders:
        printed = LabelPrintLog.objects.filter(order=o).aggregate(Sum('printed_qty'))['printed_qty__sum'] or 0
        
        # 반출 확인 수량 (ERP 번호 매칭)
        returned = ReturnLog.objects.filter(
            part=o.part, 
            is_confirmed=True,
            delivery_order__items__erp_order_no=o.erp_order_no
        ).distinct().aggregate(Sum('quantity'))['quantity__sum'] or 0

        valid_printed = printed - returned
        remain = o.quantity - valid_printed

        if remain > 0:
            label_data.append({
                'is_erp': True,
                'order_id': o.id,
                'erp_no': o.erp_order_no,
                'erp_seq': o.erp_order_seq,
                'part_no': o.part_no,
                'part_name': o.part_name,
                'total_order': o.quantity,
                'remain': remain,
                'due_date': o.due_date
            })

    # [B] 수기 발주 건
    manual_orders = order_q.filter(Q(erp_order_no__isnull=True) | Q(erp_order_no=''))
    manual_pnos = manual_orders.values_list('part_no', flat=True).distinct()

    for p_no in manual_pnos:
        sub_orders = manual_orders.filter(part_no=p_no)
        total_qty = sub_orders.aggregate(Sum('quantity'))['quantity__sum'] or 0
        part_first = sub_orders.first()
        part_name = part_first.part_name
        due_date = sub_orders.order_by('due_date').first().due_date
        
        printed = LabelPrintLog.objects.filter(part_no=p_no, order__isnull=True).aggregate(Sum('printed_qty'))['printed_qty__sum'] or 0
        
        returned = ReturnLog.objects.filter(
            part__part_no=p_no,
            is_confirmed=True,
            delivery_order__items__erp_order_no=''
        ).distinct().aggregate(Sum('quantity'))['quantity__sum'] or 0

        remain = total_qty - (printed - returned)

        if remain > 0:
            label_data.append({
                'is_erp': False,
                'order_id': None,
                'erp_no': '-',
                'erp_seq': '-',
                'part_no': p_no,
                'part_name': part_name,
                'total_order': total_qty,
                'remain': remain,
                'due_date': due_date
            })

    label_data.sort(key=lambda x: x['due_date'])

    # ✅✅✅ [요청 반영 1] 템플릿 경로만 orders/로 변경 ✅✅✅
    return render(request, 'label_list.html', {
        'label_data': label_data,
        'orders': recent_orders,
        'return_logs': return_logs,
        'return_pending_count': return_pending_count,
        'vendor_list': vendor_list,
        'selected_vendor_id': selected_v,
        'status_filter': status_filter,
        'active_menu': 'label',
        'q': q
    })

@login_required
@require_POST
def delete_delivery_order(request, order_id):
    resp = require_action_perm(request, 'delivery.delete')
    if resp:
        return resp

    order = get_object_or_404(DeliveryOrder, pk=order_id)
    
    # 권한 체크 (items를 통해 vendor 확인)
    first_item = order.items.first()
    vendor = first_item.part.vendor if first_item else None

    if not request.user.is_superuser and request.user.profile.vendor != vendor:
        messages.error(request, "삭제 권한이 없습니다.")
        return redirect('label_list')

    if order.status != 'PENDING' and order.status != 'REJECTED':
        messages.error(request, "이미 처리된 납품서는 삭제할 수 없습니다.")
        return redirect('label_list')

    with transaction.atomic():
        for item in order.items.all():
            LabelPrintLog.objects.filter(
                part_no=item.part_no,
                printed_qty=item.total_qty,
                printed_at__date=order.created_at.date()
            ).delete()
        order.delete()
        messages.success(request, "납품서가 삭제되었습니다.")

    return redirect('label_list')

@login_required
def label_print_action(request):
    return redirect('label_list')

@login_required
@require_POST
def create_delivery_order(request):
    resp = require_action_perm(request, 'delivery.register')
    if resp:
        return resp

    p_nos = request.POST.getlist('part_nos[]')
    snps = request.POST.getlist('snps[]')
    b_counts = request.POST.getlist('box_counts[]')
    order_ids = request.POST.getlist('order_ids[]')
    lot_nos = request.POST.getlist('lot_nos[]')

    if _get_role(request.user) == 'VENDOR':
        user_vendor = _get_user_vendor(request.user)
        if not user_vendor:
            messages.error(request, "협력사 정보가 연결되어 있지 않습니다.")
            return redirect('label_list')

        allowed_pnos = set(Part.objects.filter(vendor=user_vendor).values_list('part_no', flat=True))
        bad = [p for p in p_nos if p not in allowed_pnos]
        if bad:
            messages.error(request, f"권한이 없는 품번이 포함되어 있습니다.")
            return redirect('label_list')

    with transaction.atomic():
        # [수정] vendor 필드 제거 (DeliveryOrder에 vendor가 없다면)
        do = DeliveryOrder.objects.create(order_no="DO-"+timezone.now().strftime("%Y%m%d-%H%M%S"))

        for i in range(len(p_nos)):
            part = Part.objects.filter(part_no=p_nos[i]).first()
            if not part:
                continue

            qty = int(snps[i]) * int(b_counts[i])
            if qty <= 0:
                continue

            linked_order = None
            erp_no = ''
            erp_seq = ''

            if len(order_ids) > i and order_ids[i] and order_ids[i] != 'None':
                try:
                    linked_order = Order.objects.get(id=order_ids[i])
                    erp_no = linked_order.erp_order_no
                    erp_seq = linked_order.erp_order_seq
                except Order.DoesNotExist:
                    linked_order = None

            # LOT 정보 추출 (날짜 형식으로 통일)
            lot_no = lot_nos[i] if len(lot_nos) > i else None

            DeliveryOrderItem.objects.create(
                order=do,
                part_no=p_nos[i],
                part_name=part.part_name,
                snp=int(snps[i]),
                box_count=int(b_counts[i]),
                total_qty=qty,
                linked_order=linked_order,
                erp_order_no=erp_no,
                erp_order_seq=erp_seq,
                lot_no=lot_no
            )

            LabelPrintLog.objects.create(
                vendor=part.vendor,
                part=part,
                part_no=p_nos[i],
                printed_qty=qty,
                snp=int(snps[i]),
                order=linked_order
            )

    return redirect('label_list')

@login_required
def label_print(request, order_id):
    resp = require_action_perm(request, 'label.print')
    if resp:
        return resp

    order = get_object_or_404(DeliveryOrder, pk=order_id)
    queue = []
    first_item = order.items.first()
    part = Part.objects.filter(part_no=first_item.part_no).first() if first_item else None
    v_name = part.vendor.name if part else "알수없음"

    for item in order.items.all():
        for _ in range(item.box_count):
            queue.append({
                'vendor_name': v_name,
                'part_name': item.part_name,
                'part_no': item.part_no,
                'snp': item.snp,
                'print_date': timezone.now()
            })

    return render(request, 'print_label.html', {'box_count': queue, 'vendor_name': v_name})

@login_required
def delivery_note_print(request, order_id):
    resp = require_action_perm(request, 'delivery.print')
    if resp:
        return resp

    do = get_object_or_404(DeliveryOrder, pk=order_id)
    items = do.items.all()
    first_item = items.first()
    part = Part.objects.filter(part_no=first_item.part_no).first() if first_item else None
    vendor = part.vendor if part else None

    return render(request, 'print_delivery_note.html', {
        'order': do,
        'items': items,
        'total_qty': items.aggregate(Sum('total_qty'))['total_qty__sum'] or 0,
        'total_box': items.aggregate(Sum('box_count'))['box_count__sum'] or 0,
        'print_date': timezone.localtime().date(),
        'vendor': vendor
    })

# ==========================================
# [5. 입고 및 반출 관리]
# ==========================================

@login_required
@require_POST
@menu_permission_required('can_manage_incoming')
def receive_delivery_order_scan(request):
    qr_code = request.POST.get('qr_code', '').strip()
    do = DeliveryOrder.objects.filter(order_no=qr_code).first()

    if not do:
        messages.error(request, f"납품서 번호 [{qr_code}]를 찾을 수 없습니다.")
        return redirect('incoming_list')

    if do.is_received:
        messages.warning(request, f"이미 입고 처리된 납품서입니다. ({do.order_no})")
        return redirect('incoming_list')

    if Warehouse is None:
        warehouses = []
    else:
        warehouses = Warehouse.objects.exclude(code__in=['8100', '8200']).order_by('code')

    return render(request, 'incoming_check.html', {'order': do, 'warehouses': warehouses})

@login_required
@require_POST
def incoming_cancel(request):
    resp = require_action_perm(request, 'incoming.cancel')
    if resp:
        return resp

    inc_id = request.POST.get('incoming_id')
    mode = request.POST.get('cancel_mode')
    target_inc = get_object_or_404(Incoming, id=inc_id)
    do_no = target_inc.delivery_order_no
    do = DeliveryOrder.objects.filter(order_no=do_no).first()

    with transaction.atomic():
        if mode == 'item':
            if do:
                LabelPrintLog.objects.filter(part_no=target_inc.part.part_no, printed_qty=target_inc.quantity).delete()
                DeliveryOrderItem.objects.filter(order=do, part_no=target_inc.part.part_no, total_qty=target_inc.quantity).delete()

            target_inc.delete()
            messages.success(request, f"품목 {target_inc.part.part_no} 입고 취소 및 잔량이 복구되었습니다.")

        elif mode == 'all':
            Incoming.objects.filter(delivery_order_no=do_no).delete()
            if do:
                do.is_received = False
                do.save()

            messages.success(request, f"납품서 {do_no} 입고 취소 완료. (품목 데이터는 보존됩니다)")

    return redirect('incoming_list')

@login_required
@menu_permission_required('can_manage_incoming')
def incoming_list(request):
    user = request.user
    selected_v = request.GET.get('vendor_id')
    sd, ed, q = request.GET.get('start_date'), request.GET.get('end_date'), request.GET.get('q', '')

    user_vendor = Vendor.objects.filter(user=user).first()

    incomings = Incoming.objects.select_related('part', 'part__vendor').all().order_by('-in_date', '-created_at')

    profile = getattr(user, 'profile', None)
    vendor_ids = Incoming.objects.values_list('part__vendor_id', flat=True).distinct()
    vendor_list = Vendor.objects.filter(id__in=vendor_ids).order_by('name') if (user.is_superuser or (profile and _is_internal(user))) else []

    if not user.is_superuser and (profile and profile.role != 'STAFF'):
        incomings = incomings.filter(part__vendor=user_vendor) if user_vendor else incomings.none()
    elif selected_v:
        incomings = incomings.filter(part__vendor_id=selected_v)

    if sd and ed:
        incomings = incomings.filter(in_date__range=[sd, ed])
    if q:
        incomings = incomings.filter(Q(part__part_no__icontains=q) | Q(part__part_name__icontains=q))

    return render(request, 'incoming_list.html', {
        'incomings': incomings,
        'active_menu': 'incoming',
        'start_date': sd,
        'end_date': ed,
        'q': q,
        'vendor_list': vendor_list,
        'selected_vendor_id': selected_v
    })

@login_required
@menu_permission_required('can_manage_incoming')
def incoming_export(request):
    # 1. 권한 및 사용자 확인
    user_vendor = Vendor.objects.filter(user=request.user).first()

    # 슈퍼유저가 아니고, 협력사도 아닌 경우에만 권한 체크
    if (not request.user.is_superuser) and (not user_vendor):
        resp = require_action_perm(request, 'incoming.export')
        if resp:
            return resp

    # 2. 기본 쿼리셋 생성
    incomings = Incoming.objects.select_related('part', 'part__vendor').all().order_by('-in_date', '-created_at')

    # 3. 필터링 적용 (화면 조회와 동일한 로직)
    # 3-1. 협력사 계정인 경우 본인 데이터만 필터링
    if (not request.user.is_superuser) and user_vendor:
        incomings = incomings.filter(part__vendor=user_vendor)
    # (관리자 페이지 등에서 특정 업체만 선택해서 조회했을 경우 대응이 필요하다면 아래 주석 해제)
    # elif request.GET.get('vendor_id'):
    #     incomings = incomings.filter(part__vendor_id=request.GET.get('vendor_id'))

    # 3-2. 날짜 필터 (시작일~종료일)
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    
    if start_date and end_date:
        incomings = incomings.filter(in_date__range=[start_date, end_date])

    # 3-3. 검색어 필터 (품번/품명)
    q = request.GET.get('q', '')
    if q:
        incomings = incomings.filter(Q(part__part_no__icontains=q) | Q(part__part_name__icontains=q))

    # 4. 엑셀 파일 생성
    wb = openpyxl.Workbook()
    ws = wb.active
    # 헤더 작성
    ws.append(['입고일자', '협력사', '품번', '품명', '입고수량(확정)', '처리일시'])

    # 데이터 작성
    for i in incomings:
        ws.append([
            i.in_date,
            i.part.vendor.name,
            i.part.part_no,
            i.part.part_name,
            i.confirmed_qty,  # [수정] 납품수량(quantity) 대신 확정수량(confirmed_qty) 사용
            i.created_at.strftime("%Y-%m-%d %H:%M")
        ])

    # 5. 응답 반환
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename=Incomings.xlsx'
    wb.save(response)
    return response


@staff_member_required
@menu_permission_required('can_access_scm_admin')
def scm_admin_main(request):
    today = timezone.localtime().date()
    overdue_list = []
    active_overdue_orders = Order.objects.filter(due_date__lt=today, is_closed=False, approved_at__isnull=False).order_by('due_date')

    for order in active_overdue_orders:
        total_printed = LabelPrintLog.objects.filter(part_no=order.part_no).aggregate(Sum('printed_qty'))['printed_qty__sum'] or 0
        closed_qty = Order.objects.filter(part_no=order.part_no, is_closed=True).aggregate(Sum('quantity'))['quantity__sum'] or 0
        current_printed = max(0, total_printed - closed_qty)
        remain = order.quantity - current_printed

        if remain > 0:
            overdue_list.append({
                'due_date': order.due_date,
                'vendor_name': order.vendor.name,
                'part_no': order.part_no,
                'part_name': order.part_name,
                'remain_qty': remain,
                'days_diff': (today - order.due_date).days
            })

    summary = {
        'total_vendors': Vendor.objects.count(),
        'total_parts': Part.objects.count(),
        'unapproved_orders': Order.objects.filter(approved_at__isnull=True, is_closed=False).count(),
        'today_incoming': Incoming.objects.filter(in_date=today).aggregate(Sum('quantity'))['quantity__sum'] or 0,
        'overdue_count': len(overdue_list)
    }

    recent_incomings = Incoming.objects.select_related('part', 'part__vendor').all().order_by('-created_at')[:10]

    return render(request, 'scm_admin_main.html', {
        'summary': summary,
        'recent_incomings': recent_incomings,
        'overdue_orders': overdue_list[:10],
        'active_menu': 'admin_main',
        'user_name': request.user.username,
        'vendor_name': "시스템 관리자"
    })

# ==========================================
# [맨 마지막] 납품서 입고 확정 (교체 반영)
# ==========================================

@login_required
@require_POST
def receive_delivery_order_confirm(request):
    order_id = request.POST.get('order_id')
    inspection_needed = request.POST.get('inspection_needed')
    direct_warehouse_code = request.POST.get('direct_warehouse_code')

    do = get_object_or_404(DeliveryOrder, pk=order_id)
    if do.is_received:
        return redirect('incoming_list')

    if Warehouse is None or MaterialStock is None or MaterialTransaction is None:
        messages.error(request, "WMS 연동 모델을 불러올 수 없습니다.")
        return redirect('incoming_list')

    if inspection_needed == 'yes' and ImportInspection is None:
        messages.error(request, "QMS 연동 모델을 불러올 수 없습니다.")
        return redirect('incoming_list')

    try:
        with transaction.atomic():
            do.is_received = True

            if inspection_needed == 'yes':
                do.status = 'RECEIVED'
                target_wh = Warehouse.objects.filter(code='8100').first()
                if not target_wh:
                    target_wh = Warehouse.objects.filter(name__contains='검사').first()
                remark_msg = "[SCM연동] 수입검사 대기 입고 (8100)"
            else:
                do.status = 'APPROVED'
                if direct_warehouse_code:
                    target_wh = Warehouse.objects.filter(code=direct_warehouse_code).first()
                else:
                    target_wh = Warehouse.objects.filter(code='4200').first()
                
                remark_msg = f"[SCM연동] 무검사 직납 입고 ({target_wh.name if target_wh else '미지정'})"

            if not target_wh:
                raise Exception("입고할 창고 정보를 찾을 수 없습니다.")

            do.save()

            for item in do.items.all():
                part = Part.objects.filter(part_no=item.part_no).first()
                if not part:
                    continue

                stock, _ = MaterialStock.objects.get_or_create(warehouse=target_wh, part=part)
                stock.quantity = F('quantity') + item.total_qty
                stock.save()

                trx_no = f"IN-SCM-{timezone.now().strftime('%y%m%d%H%M%S')}-{item.id}"
                trx = MaterialTransaction.objects.create(
                    transaction_no=trx_no,
                    transaction_type='IN_SCM',
                    part=part,
                    quantity=item.total_qty,
                    warehouse_to=target_wh,
                    vendor=part.vendor,
                    actor=request.user,
                    ref_delivery_order=do.order_no,
                    remark=remark_msg
                )

                if inspection_needed == 'yes':
                    ImportInspection.objects.create(inbound_transaction=trx, status='PENDING')
                else:
                    Incoming.objects.create(
                        part=part,
                        quantity=item.total_qty,
                        in_date=timezone.localtime().date(),
                        delivery_order_no=do.order_no,
                        erp_order_no=item.erp_order_no,
                        erp_order_seq=item.erp_order_seq
                    )

            msg = f"{'수입검사 요청' if inspection_needed == 'yes' else '직납 입고'} 완료 (입고창고: {target_wh.name})"
            messages.success(request, f"납품서 처리 완료: {msg}")

    except Exception as e:
        messages.error(request, f"처리 중 오류 발생: {str(e)}")

    return redirect('incoming_list')


# orders/views.py 의 confirm_return 함수 교체

@login_required
@require_POST
def confirm_return(request, pk):
    """
    [협력사 액션] 부적합 반출 확인 (단순 확인용)
    - WMS 재고 차감은 관리자가 이미 수행했다고 가정함.
    - 여기서는 협력사가 '확인' 버튼을 누르면 납품 가능 수량(Remain)만 복구해줌.
    """
    return_log = get_object_or_404(ReturnLog, pk=pk)
    
    # 1. 권한 체크 (본인 회사 물건인지)
    if not request.user.is_superuser:
        user_vendor = _get_user_vendor(request.user)
        if (not user_vendor) or (user_vendor != return_log.part.vendor):
            messages.error(request, "권한이 없습니다.")
            return redirect('label_list')

    # 2. 중복 체크
    if return_log.is_confirmed:
        messages.warning(request, "이미 확인 처리된 건입니다.")
        return redirect('label_list')

    try:
        # 3. 상태 업데이트 (단순 마킹)
        # 재고 로직(WMS)은 일절 개입하지 않음
        return_log.is_confirmed = True
        return_log.confirmed_at = timezone.now()
        return_log.save()

        messages.success(request, f"반출 확인 완료. ({return_log.quantity}ea 만큼 납품 가능 수량이 복구되었습니다.)")

    except Exception as e:
        messages.error(request, f"처리 중 오류 발생: {str(e)}")

    return redirect('label_list')