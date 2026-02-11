# material/views.py

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator  # 페이징 처리
from django.db import transaction
from django.db.models import F, Sum, Q
from django.utils import timezone
from functools import wraps

# SCM(Orders) 앱 모델
from orders.models import Part, Vendor, Inventory as OldInventory, Demand, InventoryUploadLog


# =============================================================================
# WMS 권한 체크 데코레이터
# =============================================================================

def _get_profile(user):
    """UserProfile을 안전하게 가져온다"""
    try:
        return getattr(user, 'profile', None)
    except Exception:
        return None

def wms_permission_required(permission_field):
    """
    WMS 메뉴 권한 체크 데코레이터
    - superuser는 모든 권한
    - 일반 사용자는 UserProfile의 해당 필드가 True여야 접근 가능
    """
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def _wrapped_view(request, *args, **kwargs):
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)

            profile = _get_profile(request.user)
            if profile:
                # 새 권한 필드 체크
                if getattr(profile, permission_field, False):
                    return view_func(request, *args, **kwargs)
                # 레거시 필드 폴백 (하위 호환)
                legacy_map = {
                    'can_wms_stock_view': ['can_wms_inout', 'can_wms_adjustment'],
                    'can_wms_stock_edit': ['can_wms_adjustment'],
                    'can_wms_inout_view': ['can_wms_inout'],
                    'can_wms_inout_edit': ['can_wms_inout'],
                    'can_wms_bom_view': ['can_wms_bom'],
                    'can_wms_bom_edit': ['can_wms_bom'],
                }
                for legacy_field in legacy_map.get(permission_field, []):
                    if getattr(profile, legacy_field, False):
                        return view_func(request, *args, **kwargs)

            messages.error(request, "해당 메뉴에 대한 접근 권한이 없습니다.")
            return redirect('material:dashboard')
        return _wrapped_view
    return decorator

# WMS(Material) 앱 모델
from .models import Warehouse, MaterialStock, MaterialTransaction

from django.http import JsonResponse, HttpResponse
import openpyxl

# [신규] QMS 앱 모델 (수입검사 연동용)
try:
    from qms.models import ImportInspection
except ImportError:
    ImportInspection = None

# =============================================================================
# 마감일 검증 헬퍼 함수
# =============================================================================

def check_closing_date(target_date):
    """
    수불 날짜가 마감 기간에 속하는지 확인
    Returns: (is_closed, warning_message, closing_month)
    - is_closed: True면 마감된 기간
    - warning_message: 경고 메시지
    - closing_month: 마감월 (표시용)
    """
    from .models import InventoryClosing
    from datetime import date
    from calendar import monthrange

    latest = InventoryClosing.get_latest_closing()
    if not latest:
        return False, None, None

    # 마감월의 마지막 날 계산
    closing_year = latest.closing_month.year
    closing_month = latest.closing_month.month
    _, last_day = monthrange(closing_year, closing_month)
    closing_end_date = date(closing_year, closing_month, last_day)

    # target_date를 date 객체로 변환
    if hasattr(target_date, 'date'):
        check_date = target_date.date()
    else:
        check_date = target_date

    if check_date <= closing_end_date:
        month_str = latest.closing_month.strftime('%Y년 %m월')
        warning_msg = f"[마감 경고] {month_str}은 이미 마감되었습니다. 마감 기간({closing_end_date.strftime('%Y-%m-%d')} 이전) 날짜로 수불을 입력하면 재고 정합성에 영향을 줄 수 있습니다."
        return True, warning_msg, month_str

    return False, None, None


# =============================================================================
# 1. 대시보드 및 재고 조회
# =============================================================================

@wms_permission_required('can_wms_stock_view')
def dashboard(request):
    """자재 관리 대시보드 - 종합 현황판"""
    from datetime import timedelta
    from django.db.models import Count, Min
    from .models import Product, BOMItem, ProcessTag, InventoryClosing

    today = timezone.now().date()
    this_month_start = today.replace(day=1)

    # ========== 1. 재고 현황 통계 ==========
    # 총 재고 품목 수 (재고가 있는 품목)
    total_parts = MaterialStock.objects.filter(quantity__gt=0).values('part').distinct().count()

    # 총 재고 수량
    total_qty = MaterialStock.objects.filter(quantity__gt=0).aggregate(total=Sum('quantity'))['total'] or 0

    # 창고 수
    warehouse_count = Warehouse.objects.filter(is_active=True).count()

    # ========== 2. 입출고 통계 ==========
    # 금일 입고
    today_in = MaterialTransaction.objects.filter(
        date__date=today,
        transaction_type__in=['IN_SCM', 'IN_MANUAL']
    ).aggregate(
        count=Count('id'),
        qty=Sum('quantity')
    )

    # 금일 출고
    today_out = MaterialTransaction.objects.filter(
        date__date=today,
        transaction_type__in=['OUT_PROD', 'OUT_RETURN']
    ).aggregate(
        count=Count('id'),
        qty=Sum('quantity')
    )

    # 이번달 입고
    month_in = MaterialTransaction.objects.filter(
        date__date__gte=this_month_start,
        transaction_type__in=['IN_SCM', 'IN_MANUAL']
    ).aggregate(
        count=Count('id'),
        qty=Sum('quantity')
    )

    # 이번달 출고
    month_out = MaterialTransaction.objects.filter(
        date__date__gte=this_month_start,
        transaction_type__in=['OUT_PROD', 'OUT_RETURN']
    ).aggregate(
        count=Count('id'),
        qty=Sum('quantity')
    )

    # ========== 3. 창고별 재고 현황 ==========
    warehouse_stats = MaterialStock.objects.filter(
        quantity__gt=0
    ).values(
        'warehouse__id', 'warehouse__name', 'warehouse__code'
    ).annotate(
        part_count=Count('part', distinct=True),
        total_qty=Sum('quantity')
    ).order_by('warehouse__code')

    # ========== 4. 품목군별 재고 현황 ==========
    part_group_stats = MaterialStock.objects.filter(
        quantity__gt=0
    ).values(
        'part__part_group'
    ).annotate(
        part_count=Count('part', distinct=True),
        total_qty=Sum('quantity')
    ).order_by('-total_qty')[:10]

    # ========== 5. FIFO 경고 (30일 이상 된 LOT) ==========
    fifo_warning_date = today - timedelta(days=30)
    fifo_warnings = MaterialStock.objects.filter(
        quantity__gt=0,
        lot_no__isnull=False,
        lot_no__lt=fifo_warning_date
    ).select_related('warehouse', 'part').order_by('lot_no')[:10]

    fifo_warning_count = MaterialStock.objects.filter(
        quantity__gt=0,
        lot_no__isnull=False,
        lot_no__lt=fifo_warning_date
    ).count()

    # ========== 6. 최근 입출고 이력 ==========
    recent_transactions = MaterialTransaction.objects.select_related(
        'part', 'warehouse_from', 'warehouse_to', 'actor'
    ).order_by('-date')[:15]

    # ========== 7. BOM 현황 ==========
    bom_stats = {
        'product_count': Product.objects.filter(is_active=True).count(),
        'bom_item_count': BOMItem.objects.filter(is_active=True).count(),
    }

    # ========== 8. 공정 현품표 현황 ==========
    tag_stats = {
        'today_printed': ProcessTag.objects.filter(printed_at__date=today).count(),
        'pending': ProcessTag.objects.filter(status='PRINTED').count(),
        'used_today': ProcessTag.objects.filter(used_at__date=today).count(),
    }

    # ========== 9. 마감 현황 ==========
    latest_closing = InventoryClosing.get_latest_closing()
    closing_info = None
    if latest_closing:
        closing_info = {
            'month': latest_closing.closing_month.strftime('%Y년 %m월'),
            'closed_at': latest_closing.closed_at,
            'closed_by': latest_closing.closed_by.username if latest_closing.closed_by else '-',
        }

    # ========== 10. 재고 부족 경고 (재고 0인 품목 중 최근 출고 이력 있는 것) ==========
    # 최근 7일 내 출고된 품목 중 현재 재고가 0인 것
    recent_out_parts = MaterialTransaction.objects.filter(
        date__date__gte=today - timedelta(days=7),
        transaction_type__in=['OUT_PROD', 'OUT_RETURN']
    ).values_list('part_id', flat=True).distinct()

    zero_stock_parts = []
    for part_id in recent_out_parts:
        total = MaterialStock.objects.filter(part_id=part_id).aggregate(total=Sum('quantity'))['total'] or 0
        if total <= 0:
            part = Part.objects.filter(id=part_id).first()
            if part:
                zero_stock_parts.append(part)
    zero_stock_count = len(zero_stock_parts)

    context = {
        # 재고 현황
        'total_parts': total_parts,
        'total_qty': total_qty,
        'warehouse_count': warehouse_count,
        'zero_stock_count': zero_stock_count,

        # 입출고 통계
        'today_in_count': today_in['count'] or 0,
        'today_in_qty': abs(today_in['qty'] or 0),
        'today_out_count': today_out['count'] or 0,
        'today_out_qty': abs(today_out['qty'] or 0),
        'month_in_count': month_in['count'] or 0,
        'month_in_qty': abs(month_in['qty'] or 0),
        'month_out_count': month_out['count'] or 0,
        'month_out_qty': abs(month_out['qty'] or 0),

        # 창고별 현황
        'warehouse_stats': warehouse_stats,

        # 품목군별 현황
        'part_group_stats': part_group_stats,

        # FIFO 경고
        'fifo_warning_count': fifo_warning_count,
        'fifo_warnings': fifo_warnings,

        # 최근 이력
        'recent_transactions': recent_transactions,

        # BOM 현황
        'bom_stats': bom_stats,

        # 현품표 현황
        'tag_stats': tag_stats,

        # 마감 현황
        'closing_info': closing_info,

        # 기타
        'today': today,
    }

    return render(request, 'material/dashboard.html', context)


@wms_permission_required('can_wms_stock_view')
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
            # 전체 합계 모드: 품목별 전체 재고 합계
            stock_data = stocks.values(
                'part__part_no',
                'part__part_name',
                'part__part_group',
                'part__vendor__name'
            ).annotate(total_qty=Sum('quantity')).order_by('part__part_no')
        else:
            # 창고별 상세 모드: 창고별 품목별 합계 (LOT 합산)
            stock_data = stocks.values(
                'warehouse__id',
                'warehouse__code',
                'warehouse__name',
                'part__id',
                'part__part_no',
                'part__part_name',
                'part__part_group',
                'part__vendor__name'
            ).annotate(total_qty=Sum('quantity')).order_by('warehouse__code', 'part__part_no')

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

@wms_permission_required('can_wms_inout_edit')
def manual_incoming(request):
    """
    [WMS] 자재 수기 입고 처리 (LOT 포함)
    """
    if request.method == 'POST':
        try:
            date_str = request.POST.get('date', timezone.now().date())
            warehouse_id = request.POST.get('warehouse_id')
            vendor_id = request.POST.get('vendor_id')
            needs_inspection = request.POST.get('needs_inspection')

            target_warehouse_id = request.POST.get('target_warehouse_id')

            part_ids = request.POST.getlist('part_ids[]')
            lot_nos = request.POST.getlist('lot_nos[]')
            quantities = request.POST.getlist('quantities[]')
            remarks = request.POST.getlist('remarks[]')

            if not part_ids:
                messages.error(request, "입고할 품목이 리스트에 없습니다.")
                return redirect('material:manual_incoming')

            # 마감 기간 검증 (경고만 표시, 진행은 허용)
            from datetime import datetime
            if isinstance(date_str, str):
                check_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            else:
                check_date = date_str
            is_closed, warning_msg, _ = check_closing_date(check_date)
            if is_closed:
                messages.warning(request, warning_msg)

            success_count = 0

            with transaction.atomic():
                warehouse = Warehouse.objects.get(id=warehouse_id)
                vendor = Vendor.objects.get(id=vendor_id) if vendor_id else None

                for i in range(len(part_ids)):
                    p_id = part_ids[i]
                    qty = int(quantities[i])
                    rmk = remarks[i] if i < len(remarks) else ''
                    lot_no_str = lot_nos[i] if i < len(lot_nos) and lot_nos[i] else None

                    if qty <= 0:
                        continue

                    part = Part.objects.get(id=p_id)
                    system_remark = "[수입검사 대상] " if needs_inspection else ""
                    final_remark = f"{system_remark}{rmk}".strip()

                    # LOT 번호 처리 (날짜 형식)
                    from datetime import datetime
                    lot_date = None
                    if lot_no_str:
                        try:
                            lot_date = datetime.strptime(lot_no_str, '%Y-%m-%d').date()
                        except (ValueError, TypeError):
                            pass

                    # (1) 재고 증가 (LOT별로 분리)
                    stock, _ = MaterialStock.objects.get_or_create(
                        warehouse=warehouse,
                        part=part,
                        lot_no=lot_date,
                        defaults={'quantity': 0}
                    )
                    MaterialStock.objects.filter(pk=stock.pk).update(
                        quantity=F('quantity') + qty
                    )
                    stock.refresh_from_db()

                    # (2) 수불 이력 생성
                    trx_no = f"IN-{timezone.now().strftime('%y%m%d%H%M%S')}-{request.user.id}-{i}"
                    trx_obj = MaterialTransaction.objects.create(
                        transaction_no=trx_no,
                        transaction_type='IN_MANUAL',
                        date=date_str,
                        part=part,
                        quantity=qty,
                        lot_no=lot_date,
                        warehouse_to=warehouse,
                        result_stock=stock.quantity,
                        vendor=vendor,
                        actor=request.user,
                        remark=final_remark
                    )

                    # (3) 수입검사 요청
                    if needs_inspection and ImportInspection:
                        # 합격 후 입고 창고 코드 조회
                        target_wh_code = '2000'
                        if target_warehouse_id:
                            try:
                                target_wh = Warehouse.objects.get(id=target_warehouse_id)
                                target_wh_code = target_wh.code
                            except Warehouse.DoesNotExist:
                                pass
                        ImportInspection.objects.create(
                            inbound_transaction=trx_obj,
                            lot_no=lot_date,
                            target_warehouse_code=target_wh_code,
                            status='PENDING'
                        )
                    elif ImportInspection:
                        # 무검사 입고도 라벨 발행 대기 목록에 표시되도록
                        ImportInspection.objects.create(
                            inbound_transaction=trx_obj,
                            lot_no=lot_date,
                            target_warehouse_code=warehouse.code,
                            status='APPROVED',
                            inspected_at=timezone.now(),
                            qty_good=qty,
                            remark='무검사 수기입고 (수입검사 생략)',
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

    # === 입고처리 내역 (History) ===
    history_qs = MaterialTransaction.objects.filter(
        transaction_type='IN_MANUAL'
    ).select_related(
        'part', 'warehouse_to', 'vendor', 'actor'
    ).order_by('-date', '-id')

    history_q = (request.GET.get('hq') or '').strip()
    if history_q:
        history_qs = history_qs.filter(
            Q(part__part_no__icontains=history_q) |
            Q(part__part_name__icontains=history_q) |
            Q(transaction_no__icontains=history_q)
        )

    history_wh = (request.GET.get('hwh') or '').strip()
    if history_wh:
        history_qs = history_qs.filter(warehouse_to_id=history_wh)

    history_start = (request.GET.get('hstart') or '').strip()
    history_end = (request.GET.get('hend') or '').strip()
    if history_start and history_start not in ('None', 'null'):
        history_qs = history_qs.filter(date__date__gte=history_start)
    if history_end and history_end not in ('None', 'null'):
        history_qs = history_qs.filter(date__date__lte=history_end)

    history_paginator = Paginator(history_qs, 15)
    history_page = history_paginator.get_page(request.GET.get('hpage'))

    from .models import RawMaterialLabel
    for item in history_page:
        item.label_count = RawMaterialLabel.objects.filter(
            incoming_transaction=item
        ).count()
        try:
            item.inspection_status = item.inspection.status
        except Exception:
            item.inspection_status = None
        item.can_cancel = (item.label_count == 0)

    warehouses_qs = Warehouse.objects.filter(is_active=True).order_by('code')

    context = {
        'warehouses': warehouses_qs,
        'vendors': Vendor.objects.all().order_by('name'),
        'parts': Part.objects.select_related('vendor').all().order_by('part_no'),
        'today': timezone.now().date(),
        'history_page': history_page,
        'history_q': history_q,
        'history_wh': history_wh,
        'history_start': history_start,
        'history_end': history_end,
    }
    return render(request, 'material/manual_incoming.html', context)


@wms_permission_required('can_wms_inout_edit')
def cancel_manual_incoming(request, trx_id):
    """[WMS] 수기 입고 삭제 - 재고 차감 + 트랜잭션 삭제"""
    if request.method != 'POST':
        return redirect('material:manual_incoming')

    trx = get_object_or_404(MaterialTransaction, pk=trx_id, transaction_type='IN_MANUAL')

    from .models import RawMaterialLabel
    label_count = RawMaterialLabel.objects.filter(incoming_transaction=trx).count()
    if label_count > 0:
        messages.error(request, f"라벨이 {label_count}장 발행된 입고 건은 삭제할 수 없습니다. 라벨을 먼저 취소하세요.")
        return redirect('material:manual_incoming')

    is_closed, warning_msg, _ = check_closing_date(
        trx.date.date() if hasattr(trx.date, 'date') and callable(trx.date.date) else trx.date
    )
    if is_closed:
        messages.error(request, f"마감된 기간의 입고 건은 삭제할 수 없습니다. ({warning_msg})")
        return redirect('material:manual_incoming')

    trx_no = trx.transaction_no
    trx_qty = trx.quantity

    try:
        with transaction.atomic():
            stock = MaterialStock.objects.filter(
                warehouse=trx.warehouse_to,
                part=trx.part,
                lot_no=trx.lot_no
            ).first()

            if not stock:
                messages.error(request, "해당 재고를 찾을 수 없습니다.")
                return redirect('material:manual_incoming')

            if stock.quantity < trx.quantity:
                messages.error(request, f"현재 재고({stock.quantity})가 입고 수량({trx.quantity})보다 적어 삭제할 수 없습니다.")
                return redirect('material:manual_incoming')

            MaterialStock.objects.filter(pk=stock.pk).update(
                quantity=F('quantity') - trx.quantity
            )

            # ImportInspection 삭제
            if ImportInspection:
                try:
                    trx.inspection.delete()
                except Exception:
                    pass

            # 트랜잭션 삭제
            trx.delete()

        messages.success(request, f"입고 건 [{trx_no}] 삭제 완료 (재고 {trx_qty}개 차감)")

    except Exception as e:
        messages.error(request, f"취소 처리 중 오류 발생: {str(e)}")

    return redirect('material:manual_incoming')


@wms_permission_required('can_wms_inout_view')
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

@wms_permission_required('can_wms_stock_view')
def process_tag_form(request):
    """현품표 발행 입력 폼"""
    parts = Part.objects.all().order_by('part_no')
    context = {'parts': parts}
    return render(request, 'material/process_tag_form.html', context)


@wms_permission_required('can_wms_stock_view')
def process_tag_print(request):
    """현품표 실제 출력 뷰"""
    from .models import ProcessTag
    from datetime import datetime

    if request.method == 'POST':
        part_no = request.POST.get('part_no')
        part_name = request.POST.get('part_name', '')
        quantity = request.POST.get('quantity')
        lot_no = request.POST.get('lot_no')
        print_count = int(request.POST.get('print_count', 1))
        print_mode = request.POST.get('print_mode', 'roll')
        size_type = request.POST.get('size_type', 'medium')
        custom_width = request.POST.get('custom_width', 100)
        custom_height = request.POST.get('custom_height', 60)

        # A4 모아찍기 모드용 레이아웃 계산
        if print_mode == 'sheet':
            # 라벨 사이즈에 따른 mm 단위 설정
            if size_type == 'small':
                label_w_mm, label_h_mm = 60, 70
            elif size_type == 'medium':
                label_w_mm, label_h_mm = 95, 45  # A4 최적화: 2×6 = 12개
            elif size_type == 'large':
                label_w_mm, label_h_mm = 210, 148
            elif size_type == 'custom':
                label_w_mm = int(custom_width)
                label_h_mm = int(custom_height)
            else:
                label_w_mm, label_h_mm = 95, 45

            # A4 용지 크기 (210mm x 297mm)
            # 여백 고려: 좌우 5mm, 상하 5mm
            usable_width = 210 - 10  # 200mm
            usable_height = 297 - 10  # 287mm

            # 한 줄에 들어갈 라벨 개수 (gap 3mm 고려)
            sheet_cols = max(1, int((usable_width + 3) / (label_w_mm + 3)))
            sheet_rows = max(1, int((usable_height + 3) / (label_h_mm + 3)))
            per_page = sheet_cols * sheet_rows
        else:
            label_w_mm, label_h_mm = 0, 0
            sheet_cols, per_page = 0, 0

        # ========================================
        # ProcessTag 레코드 생성 (중복 스캔 방지용)
        # ========================================
        # Part 조회 (없으면 None)
        part = Part.objects.filter(part_no=part_no).first()

        # LOT 날짜 파싱
        lot_date = None
        if lot_no:
            try:
                lot_date = datetime.strptime(lot_no, '%Y-%m-%d').date()
            except ValueError:
                pass  # LOT 형식이 날짜가 아닌 경우 None 유지

        # 태그 생성
        tag_list = []
        for _ in range(print_count):
            tag_id = ProcessTag.generate_tag_id()
            tag = ProcessTag.objects.create(
                tag_id=tag_id,
                part=part,
                part_no=part_no,
                part_name=part_name,
                quantity=int(quantity) if quantity else 0,
                lot_no=lot_date,
                status='PRINTED',
                printed_by=request.user if request.user.is_authenticated else None
            )
            tag_list.append({
                'index': len(tag_list),
                'tag_id': tag_id,
            })

        context = {
            'part_no': part_no,
            'part_name': part_name,
            'part_group': part.part_group if part else '',  # 품목군 추가
            'quantity': quantity,
            'lot_no': lot_no,
            'print_mode': print_mode,
            'size_type': size_type,
            'print_range': range(print_count),
            'tag_list': tag_list,  # 태그 ID 목록 전달
            'custom_width': custom_width,
            'custom_height': custom_height,
            'print_date': timezone.now().strftime('%Y-%m-%d'),
            'worker': request.user.username,
            # A4 모아찍기용 변수
            'label_w_mm': label_w_mm,
            'label_h_mm': label_h_mm,
            'sheet_cols': sheet_cols,
            'per_page': per_page,
        }
        return render(request, 'material/process_tag_print.html', context)

    return redirect('material:process_tag_form')


# =============================================================================
# 3-2. 현품표 스캔 API (중복 스캔 확인)
# =============================================================================

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
import json

@require_http_methods(["POST"])
@login_required
def api_process_tag_scan(request):
    """
    현품표 QR 스캔 시 호출되는 API
    - 태그 ID로 ProcessTag 조회
    - 스캔 기록 저장
    - 중복 스캔 시 차단 (success: false)

    요청: POST { "tag_id": "TAG-20260124-0001", "warehouse_id": 1 (optional) }
    응답: {
        "success": true/false,
        "is_first_scan": true/false,
        "error": "중복 스캔 시 에러 메시지",
        "tag_info": { ... }
    }
    """
    from .models import ProcessTag, ProcessTagScanLog

    try:
        data = json.loads(request.body)
        tag_id = data.get('tag_id', '').strip()
        warehouse_id = data.get('warehouse_id')

        if not tag_id:
            return JsonResponse({
                'success': False,
                'error': '태그 ID가 누락되었습니다.'
            }, status=400)

        # 태그 조회
        tag = ProcessTag.objects.filter(tag_id=tag_id).first()

        if not tag:
            return JsonResponse({
                'success': False,
                'error': f'등록되지 않은 태그입니다: {tag_id}',
                'is_registered': False
            })

        # 창고 조회
        warehouse = None
        if warehouse_id:
            warehouse = Warehouse.objects.filter(id=warehouse_id).first()

        # 스캔 기록 (record_scan 메서드 호출)
        success, is_first_scan, error = tag.record_scan(user=request.user, warehouse=warehouse)

        # 스캔 로그 생성 (성공/실패 모두 기록)
        client_ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', ''))
        if client_ip and ',' in client_ip:
            client_ip = client_ip.split(',')[0].strip()

        ProcessTagScanLog.objects.create(
            tag=tag,
            scanned_by=request.user,
            warehouse=warehouse,
            is_first_scan=is_first_scan,
            ip_address=client_ip if client_ip else None,
            remark='중복 스캔 시도 (차단됨)' if not success else ''
        )

        # 중복 스캔 차단
        if not success:
            return JsonResponse({
                'success': False,
                'is_first_scan': False,
                'error': error,
                'tag_info': {
                    'tag_id': tag.tag_id,
                    'part_no': tag.part_no,
                    'part_name': tag.part_name,
                    'quantity': tag.quantity,
                    'status': tag.get_status_display(),
                    'scan_count': tag.scan_count,
                    'used_at': tag.used_at.strftime('%Y-%m-%d %H:%M') if tag.used_at else None,
                    'used_by': tag.used_by.username if tag.used_by else None,
                }
            })

        return JsonResponse({
            'success': True,
            'is_first_scan': is_first_scan,
            'message': '스캔 성공',
            'tag_info': {
                'tag_id': tag.tag_id,
                'part_no': tag.part_no,
                'part_name': tag.part_name,
                'quantity': tag.quantity,
                'lot_no': str(tag.lot_no) if tag.lot_no else '',
                'status': tag.get_status_display(),
                'scan_count': tag.scan_count,
                'printed_at': tag.printed_at.strftime('%Y-%m-%d %H:%M'),
                'used_at': tag.used_at.strftime('%Y-%m-%d %H:%M') if tag.used_at else None,
            }
        })

    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': '잘못된 요청 형식입니다.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': f'처리 중 오류 발생: {str(e)}'
        }, status=500)


@require_http_methods(["GET"])
@login_required
def api_process_tag_info(request, tag_id):
    """
    현품표 정보 조회 API (GET)
    - 태그 상태 및 스캔 이력 조회
    """
    from .models import ProcessTag, ProcessTagScanLog

    tag = ProcessTag.objects.filter(tag_id=tag_id).first()

    if not tag:
        return JsonResponse({
            'success': False,
            'error': f'등록되지 않은 태그입니다: {tag_id}'
        }, status=404)

    # 스캔 이력 조회 (최근 10건)
    scan_logs = tag.scan_logs.select_related('scanned_by', 'warehouse')[:10]
    logs_data = [{
        'scanned_at': log.scanned_at.strftime('%Y-%m-%d %H:%M:%S'),
        'scanned_by': log.scanned_by.username if log.scanned_by else '-',
        'warehouse': log.warehouse.name if log.warehouse else '-',
        'is_first_scan': log.is_first_scan,
    } for log in scan_logs]

    return JsonResponse({
        'success': True,
        'tag_info': {
            'tag_id': tag.tag_id,
            'part_no': tag.part_no,
            'part_name': tag.part_name,
            'quantity': tag.quantity,
            'lot_no': str(tag.lot_no) if tag.lot_no else '',
            'status': tag.get_status_display(),
            'scan_count': tag.scan_count,
            'printed_at': tag.printed_at.strftime('%Y-%m-%d %H:%M'),
            'printed_by': tag.printed_by.username if tag.printed_by else '-',
            'used_at': tag.used_at.strftime('%Y-%m-%d %H:%M') if tag.used_at else None,
            'used_by': tag.used_by.username if tag.used_by else '-',
        },
        'scan_logs': logs_data
    })


# =============================================================================
# 4. 기타 메뉴
# =============================================================================

@wms_permission_required('can_wms_stock_view')
def transaction_history(request):
    """
    품번별 수불 이력 - 품번 선택 시 해당 품번의 모든 이동 이력을 시간순으로 표시
    입고 -> 창고이동 -> 출고까지 전체 흐름 추적
    """
    from django.core.paginator import Paginator
    from orders.models import Part
    from datetime import datetime

    # ===== 필터 파라미터 =====
    start_date = request.GET.get('start_date', '')
    end_date = request.GET.get('end_date', '')
    part_no = request.GET.get('part_no', '').strip()
    part_group = request.GET.get('part_group', '')
    lot_no = request.GET.get('lot_no', '').strip()
    q = request.GET.get('q', '').strip()

    # 기본값: 이번 달
    if not start_date:
        today = timezone.localtime().date()
        start_date = today.replace(day=1).strftime('%Y-%m-%d')
    if not end_date:
        end_date = timezone.localtime().date().strftime('%Y-%m-%d')

    start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
    end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()

    # ===== 품목군 목록 =====
    part_groups = Part.objects.values_list('part_group', flat=True).distinct().order_by('part_group')

    # ===== 품번 목록 (트랜잭션이 있는 품번만) =====
    part_ids_with_trx = MaterialTransaction.objects.values_list('part_id', flat=True).distinct()
    parts_list = Part.objects.filter(id__in=part_ids_with_trx).order_by('part_no')

    if part_group:
        parts_list = parts_list.filter(part_group=part_group)
    if q:
        parts_list = parts_list.filter(Q(part_no__icontains=q) | Q(part_name__icontains=q))

    # ===== 선택된 품번의 수불 이력 조회 =====
    transactions = []
    selected_part = None
    lot_list = []  # 해당 품번의 LOT 목록

    if part_no:
        selected_part = Part.objects.filter(part_no=part_no).first()
        if selected_part:
            # 해당 품번의 LOT 목록 조회
            lot_list = MaterialTransaction.objects.filter(
                part=selected_part,
                lot_no__isnull=False
            ).values_list('lot_no', flat=True).distinct().order_by('-lot_no')

            # 트랜잭션 조회
            trx_qs = MaterialTransaction.objects.filter(
                part=selected_part,
                date__date__gte=start_dt,
                date__date__lte=end_dt
            )

            # LOT 필터 적용
            if lot_no:
                try:
                    lot_date = datetime.strptime(lot_no, '%Y-%m-%d').date()
                    trx_qs = trx_qs.filter(lot_no=lot_date)
                except ValueError:
                    pass

            transactions = trx_qs.select_related(
                'warehouse_from', 'warehouse_to', 'actor'
            ).order_by('date')

    # ===== 페이징 =====
    paginator = Paginator(transactions, 50)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # ===== 합계 계산 =====
    total_in = sum(t.quantity for t in transactions if t.warehouse_to and not t.warehouse_from)
    total_out = sum(t.quantity for t in transactions if t.warehouse_from and not t.warehouse_to)
    total_move = sum(t.quantity for t in transactions if t.warehouse_from and t.warehouse_to)

    return render(request, 'material/transaction_history.html', {
        'page_obj': page_obj,
        'parts_list': parts_list,
        'part_groups': part_groups,
        'lot_list': lot_list,
        'start_date': start_date,
        'end_date': end_date,
        'part_no': part_no,
        'selected_part': selected_part,
        'part_group': part_group,
        'lot_no': lot_no,
        'q': q,
        'total_in': total_in,
        'total_out': total_out,
        'total_move': total_move,
        'result_count': len(transactions),
    })


@wms_permission_required('can_wms_stock_view')
def transaction_history_excel(request):
    """
    품번별 수불 이력 - Excel 다운로드
    """
    from datetime import datetime
    from orders.models import Part

    # ===== 필터 파라미터 =====
    start_date = request.GET.get('start_date', '')
    end_date = request.GET.get('end_date', '')
    part_no = request.GET.get('part_no', '').strip()
    lot_no = request.GET.get('lot_no', '').strip()

    # 기본값: 이번 달
    if not start_date:
        today = timezone.localtime().date()
        start_date = today.replace(day=1).strftime('%Y-%m-%d')
    if not end_date:
        end_date = timezone.localtime().date().strftime('%Y-%m-%d')

    start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
    end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()

    # ===== 선택된 품번의 수불 이력 조회 =====
    selected_part = Part.objects.filter(part_no=part_no).first()
    transactions = []

    if selected_part:
        trx_qs = MaterialTransaction.objects.filter(
            part=selected_part,
            date__date__gte=start_dt,
            date__date__lte=end_dt
        )

        # LOT 필터 적용
        if lot_no:
            try:
                lot_date = datetime.strptime(lot_no, '%Y-%m-%d').date()
                trx_qs = trx_qs.filter(lot_no=lot_date)
            except ValueError:
                pass

        transactions = list(trx_qs.select_related(
            'warehouse_from', 'warehouse_to', 'actor'
        ).order_by('date'))

    # ===== Excel 파일 생성 =====
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "수불이력"

    # 헤더 스타일
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    header_font = Font(bold=True)
    header_fill = PatternFill(start_color='E0E0E0', end_color='E0E0E0', fill_type='solid')
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )

    # 제목 행
    ws.append([f"품번별 수불 이력 ({start_date} ~ {end_date})"])
    ws.merge_cells('A1:H1')
    ws['A1'].font = Font(bold=True, size=14)
    ws['A1'].alignment = Alignment(horizontal='center')

    # 품번 정보
    if selected_part:
        ws.append([f"품번: {selected_part.part_no} / 품명: {selected_part.part_name}"])
    else:
        ws.append(["품번 미선택"])
    ws.merge_cells('A2:H2')
    ws.append([])  # 빈 행

    # 헤더
    headers = ['No', '일시', '유형', 'LOT', '출발 창고', '도착 창고', '수량', '처리자']
    ws.append(headers)
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=4, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = thin_border
        cell.alignment = Alignment(horizontal='center')

    # 데이터
    for idx, trx in enumerate(transactions, 1):
        # 유형 결정
        if trx.warehouse_to and not trx.warehouse_from:
            trx_type = "입고"
        elif trx.warehouse_from and not trx.warehouse_to:
            trx_type = "출고"
        elif trx.warehouse_from and trx.warehouse_to:
            trx_type = "이동"
        else:
            trx_type = "기타"

        row = [
            idx,
            trx.date.strftime("%Y-%m-%d %H:%M"),
            trx_type,
            trx.lot_no.strftime("%Y-%m-%d") if trx.lot_no else "-",
            f"({trx.warehouse_from.code}) {trx.warehouse_from.name}" if trx.warehouse_from else "- 외부 입고 -",
            f"({trx.warehouse_to.code}) {trx.warehouse_to.name}" if trx.warehouse_to else "- 외부 출고 -",
            trx.quantity,
            trx.actor.username if trx.actor else "-"
        ]
        ws.append(row)
        for col_num in range(1, 9):
            cell = ws.cell(row=4 + idx, column=col_num)
            cell.border = thin_border
            if col_num == 7:
                cell.alignment = Alignment(horizontal='right')

    # 합계 행
    total_in = sum(t.quantity for t in transactions if t.warehouse_to and not t.warehouse_from)
    total_out = sum(t.quantity for t in transactions if t.warehouse_from and not t.warehouse_to)
    total_move = sum(t.quantity for t in transactions if t.warehouse_from and t.warehouse_to)

    total_row = 5 + len(transactions)
    ws.append([])  # 빈 행
    ws.append(['', '', '', '', '', '입고 합계:', total_in, ''])
    ws.append(['', '', '', '', '', '출고 합계:', total_out, ''])
    ws.append(['', '', '', '', '', '이동 합계:', total_move, ''])

    # 열 너비 조정
    ws.column_dimensions['A'].width = 6
    ws.column_dimensions['B'].width = 18
    ws.column_dimensions['C'].width = 10
    ws.column_dimensions['D'].width = 12
    ws.column_dimensions['E'].width = 25
    ws.column_dimensions['F'].width = 25
    ws.column_dimensions['G'].width = 12
    ws.column_dimensions['H'].width = 12

    # 응답 반환
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f"수불이력_{part_no}_{start_date}_{end_date}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


@wms_permission_required('can_wms_stock_edit')
def stock_adjustment(request):
    """
    [WMS] 재고 조정(관리) - 월별 마감 및 재고 실사/조정
    - 마감 현황 조회
    - 월 마감 처리
    - 마감 해제
    """
    from .models import InventoryClosing, InventoryCheck
    from datetime import date
    from calendar import monthrange

    # 현재 마감 상태
    latest_closing = InventoryClosing.get_latest_closing()

    # 마감 가능한 월 계산 (이번 달 또는 지난 달)
    today = date.today()
    current_month_start = date(today.year, today.month, 1)

    # 지난 달 계산
    if today.month == 1:
        prev_month_start = date(today.year - 1, 12, 1)
    else:
        prev_month_start = date(today.year, today.month - 1, 1)

    # 마감할 수 있는 다음 월 (latest_closing 다음 달)
    if latest_closing:
        closing_year = latest_closing.closing_month.year
        closing_month = latest_closing.closing_month.month
        if closing_month == 12:
            next_closable = date(closing_year + 1, 1, 1)
        else:
            next_closable = date(closing_year, closing_month + 1, 1)
    else:
        next_closable = prev_month_start

    # 마감 이력 조회
    closing_history = InventoryClosing.objects.all().order_by('-closing_month')[:12]

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'close_month':
            # 월 마감 처리
            close_year = int(request.POST.get('close_year'))
            close_month = int(request.POST.get('close_month'))
            remark = request.POST.get('remark', '')

            closing_date = date(close_year, close_month, 1)

            # 이미 마감된 월인지 확인
            if InventoryClosing.objects.filter(closing_month=closing_date, is_active=True).exists():
                messages.warning(request, f"{close_year}년 {close_month}월은 이미 마감되었습니다.")
            else:
                # 마감 처리 + 재고 스냅샷 저장
                from .models import InventorySnapshot

                with transaction.atomic():
                    # 1) 마감 레코드 생성
                    closing = InventoryClosing.objects.create(
                        closing_month=closing_date,
                        closed_by=request.user,
                        remark=remark
                    )

                    # 2) 현재 재고를 스냅샷으로 저장
                    current_stocks = MaterialStock.objects.filter(quantity__gt=0).select_related('warehouse', 'part')
                    snapshot_count = 0

                    for stock in current_stocks:
                        InventorySnapshot.objects.create(
                            closing=closing,
                            warehouse=stock.warehouse,
                            part=stock.part,
                            lot_no=stock.lot_no,
                            quantity=stock.quantity
                        )
                        snapshot_count += 1

                messages.success(request, f"{close_year}년 {close_month}월 재고 마감 완료! (재고 스냅샷 {snapshot_count}건 저장)")

            return redirect('material:stock_adjustment')

        elif action == 'cancel_closing':
            # 마감 해제
            closing_id = request.POST.get('closing_id')
            try:
                closing = InventoryClosing.objects.get(id=closing_id)
                closing.is_active = False
                closing.save()
                messages.success(request, f"{closing.closing_month.strftime('%Y년 %m월')} 마감이 해제되었습니다.")
            except InventoryClosing.DoesNotExist:
                messages.error(request, "마감 정보를 찾을 수 없습니다.")

            return redirect('material:stock_adjustment')

        elif action == 'adjust_stock':
            # 개별 재고 조정
            stock_id = request.POST.get('stock_id')
            new_qty = int(request.POST.get('new_qty', 0))
            remark = request.POST.get('remark', '')

            try:
                with transaction.atomic():
                    stock = MaterialStock.objects.get(id=stock_id)
                    old_qty = stock.quantity
                    diff = new_qty - old_qty

                    if diff == 0:
                        messages.info(request, "수량 변동이 없습니다.")
                    else:
                        # 재고 수정
                        stock.quantity = new_qty
                        stock.save()

                        # 조정 이력 생성
                        trx_no = f"ADJ-{timezone.now().strftime('%y%m%d%H%M%S')}-{request.user.id}"
                        MaterialTransaction.objects.create(
                            transaction_no=trx_no,
                            transaction_type='ADJUST',
                            date=timezone.now(),
                            part=stock.part,
                            lot_no=stock.lot_no,
                            quantity=diff,
                            warehouse_to=stock.warehouse if diff > 0 else None,
                            warehouse_from=stock.warehouse if diff < 0 else None,
                            result_stock=new_qty,
                            actor=request.user,
                            remark=f"[재고조정] {old_qty} → {new_qty} ({'+' if diff > 0 else ''}{diff}) / {remark}"
                        )

                        messages.success(request, f"재고 조정 완료: {stock.part.part_no} ({old_qty} → {new_qty})")

            except MaterialStock.DoesNotExist:
                messages.error(request, "재고 정보를 찾을 수 없습니다.")
            except Exception as e:
                messages.error(request, f"조정 중 오류: {str(e)}")

            return redirect('material:stock_adjustment')

        elif action == 'upload_excel':
            # 엑셀 일괄 업로드 조정
            excel_file = request.FILES.get('excel_file')
            if not excel_file:
                messages.error(request, "엑셀 파일을 선택해주세요.")
                return redirect('material:stock_adjustment')

            try:
                wb = openpyxl.load_workbook(excel_file)
                ws = wb.active

                success_count = 0
                error_count = 0
                error_messages = []

                with transaction.atomic():
                    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                        # 컬럼: 창고코드, 품목군, 품번, 수량, LOT, 비고
                        if not row or not row[0]:
                            continue

                        warehouse_code = str(row[0]).strip() if row[0] else ''
                        part_group = str(row[1]).strip() if len(row) > 1 and row[1] else ''
                        part_no = str(row[2]).strip() if len(row) > 2 and row[2] else ''
                        new_qty = row[3] if len(row) > 3 and row[3] is not None else 0
                        lot_str = str(row[4]).strip() if len(row) > 4 and row[4] else ''
                        remark = str(row[5]).strip() if len(row) > 5 and row[5] else ''

                        # 기본 검증
                        if not warehouse_code or not part_no:
                            error_count += 1
                            error_messages.append(f"행 {row_idx}: 창고코드 또는 품번 누락")
                            continue

                        # 창고 확인 (코드 또는 이름으로 매칭)
                        warehouse = Warehouse.objects.filter(code=warehouse_code).first()
                        if not warehouse:
                            warehouse = Warehouse.objects.filter(name=warehouse_code).first()
                        if not warehouse:
                            error_count += 1
                            error_messages.append(f"행 {row_idx}: 창고 '{warehouse_code}' 없음")
                            continue

                        # 품번 확인 (품목마스터 기반 - 자동 생성하지 않음)
                        try:
                            part = Part.objects.get(part_no=part_no)
                            # 기존 품목이라도 품목군이 입력되면 업데이트
                            if part_group and part.part_group != part_group:
                                part.part_group = part_group
                                part.save()
                        except Part.DoesNotExist:
                            # Part에 없으면 실패 로그 기록 후 스킵
                            error_count += 1
                            error_messages.append(f"행 {row_idx}: 품번 '{part_no}' 품목마스터에 없음")
                            # 실패 로그 기록
                            InventoryUploadLog.objects.create(
                                upload_type='INVENTORY',
                                uploaded_by=request.user,
                                part_no=part_no,
                                part_name='',
                                row_data=f"창고:{warehouse_code}, 품목군:{part_group}, 수량:{new_qty}, LOT:{lot_str}",
                                error_reason=f"품목마스터에 등록되지 않은 품번"
                            )
                            continue

                        # LOT 파싱 (yy.mm.dd 또는 yyyy-mm-dd 형식)
                        lot_no = None
                        if lot_str and lot_str != '-':
                            from datetime import datetime
                            for fmt in ['%y.%m.%d', '%Y-%m-%d', '%Y.%m.%d', '%y-%m-%d']:
                                try:
                                    lot_no = datetime.strptime(lot_str, fmt).date()
                                    break
                                except ValueError:
                                    continue

                        # 수량 변환
                        try:
                            new_qty = int(float(new_qty))
                        except (ValueError, TypeError):
                            error_count += 1
                            error_messages.append(f"행 {row_idx}: 수량 '{new_qty}' 유효하지 않음")
                            continue

                        # 기존 재고 확인 (창고 + 품번 + LOT)
                        stock, created = MaterialStock.objects.get_or_create(
                            warehouse=warehouse,
                            part=part,
                            lot_no=lot_no,
                            defaults={'quantity': 0}
                        )

                        old_qty = stock.quantity
                        diff = new_qty - old_qty

                        if diff != 0:
                            # 재고 수정
                            stock.quantity = new_qty
                            stock.save()

                            # 조정 이력 생성
                            trx_no = f"ADJX-{timezone.now().strftime('%y%m%d%H%M%S')}-{row_idx}"
                            MaterialTransaction.objects.create(
                                transaction_no=trx_no,
                                transaction_type='ADJUST',
                                date=timezone.now(),
                                part=part,
                                lot_no=lot_no,
                                quantity=diff,
                                warehouse_to=warehouse if diff > 0 else None,
                                warehouse_from=warehouse if diff < 0 else None,
                                result_stock=new_qty,
                                actor=request.user,
                                remark=f"[엑셀조정] {old_qty} → {new_qty} / {remark}"
                            )

                        action_type = "신규" if created else "수정"
                        success_count += 1

                if success_count > 0:
                    messages.success(request, f"엑셀 업로드 완료: {success_count}건 처리")
                if error_count > 0:
                    messages.warning(request, f"오류 {error_count}건: " + "; ".join(error_messages[:5]))
                    if error_count > 5:
                        messages.warning(request, f"... 외 {error_count - 5}건 추가 오류")

            except Exception as e:
                messages.error(request, f"엑셀 파일 처리 오류: {str(e)}")

            return redirect('material:stock_adjustment')

    # 조정 대상 재고 목록 (검색 가능)
    search_q = request.GET.get('q', '').strip()
    warehouse_filter = request.GET.get('warehouse', '')

    stocks = MaterialStock.objects.filter(quantity__gt=0).select_related('warehouse', 'part')

    if search_q:
        stocks = stocks.filter(
            Q(part__part_no__icontains=search_q) |
            Q(part__part_name__icontains=search_q)
        )

    if warehouse_filter:
        stocks = stocks.filter(warehouse__code=warehouse_filter)

    stocks = stocks.order_by('warehouse__code', 'part__part_no')[:100]  # 최대 100건

    warehouses = Warehouse.objects.filter(is_active=True).order_by('code')

    # 최근 실패 로그 조회 (기초재고 업로드)
    recent_error_logs = InventoryUploadLog.objects.filter(
        upload_type='INVENTORY'
    ).order_by('-uploaded_at')[:20]

    context = {
        'latest_closing': latest_closing,
        'next_closable': next_closable,
        'closing_history': closing_history,
        'stocks': stocks,
        'warehouses': warehouses,
        'search_q': search_q,
        'warehouse_filter': warehouse_filter,
        'today': today,
        'recent_error_logs': recent_error_logs,
    }
    return render(request, 'material/stock_adjustment.html', context)


@wms_permission_required('can_wms_stock_edit')
def stock_adjustment_template(request):
    """
    [WMS] 재고 조정 엑셀 양식 다운로드
    - 현재 재고를 포함한 양식 또는 빈 양식 다운로드
    """
    include_data = request.GET.get('include_data', 'false') == 'true'

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "재고조정"

    # 헤더: 창고코드, 품목군, 품번, 수량, LOT, 비고
    headers = ['창고코드', '품목군', '품번', '수량', 'LOT(yy.mm.dd)', '비고']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = openpyxl.styles.Font(bold=True)
        cell.fill = openpyxl.styles.PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')

    # 열 너비 설정
    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 12
    ws.column_dimensions['C'].width = 20
    ws.column_dimensions['D'].width = 12
    ws.column_dimensions['E'].width = 15
    ws.column_dimensions['F'].width = 30

    if include_data:
        # 현재 재고 데이터 포함
        stocks = MaterialStock.objects.filter(quantity__gt=0).select_related('warehouse', 'part').order_by('warehouse__code', 'part__part_no')
        for row_idx, stock in enumerate(stocks, 2):
            ws.cell(row=row_idx, column=1, value=stock.warehouse.code)
            ws.cell(row=row_idx, column=2, value=stock.part.part_group or '')
            ws.cell(row=row_idx, column=3, value=stock.part.part_no)
            ws.cell(row=row_idx, column=4, value=stock.quantity)
            ws.cell(row=row_idx, column=5, value=stock.lot_no.strftime('%y.%m.%d') if stock.lot_no else '')
            ws.cell(row=row_idx, column=6, value='')

    # 응답
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f"stock_adjustment_template_{timezone.now().strftime('%Y%m%d')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)

    return response


@wms_permission_required('can_wms_stock_view')
def closing_report(request):
    """
    [WMS] 월별 마감 재고 보고서
    - 마감 시점의 재고 스냅샷 조회
    - 월별 비교 가능
    """
    from .models import InventoryClosing, InventorySnapshot

    # 마감 목록
    closings = InventoryClosing.objects.filter(is_active=True).order_by('-closing_month')

    # 선택된 마감월
    closing_id = request.GET.get('closing_id', '')
    selected_closing = None
    snapshots = []
    summary = {'total_items': 0, 'total_qty': 0, 'warehouses': {}}

    if closing_id:
        try:
            selected_closing = InventoryClosing.objects.get(id=closing_id)
            snapshots = InventorySnapshot.objects.filter(
                closing=selected_closing
            ).select_related('warehouse', 'part').order_by('warehouse__code', 'part__part_no')

            # 요약 통계
            summary['total_items'] = snapshots.count()
            summary['total_qty'] = sum(s.quantity for s in snapshots)

            # 창고별 통계
            for snapshot in snapshots:
                wh_code = snapshot.warehouse.code
                if wh_code not in summary['warehouses']:
                    summary['warehouses'][wh_code] = {
                        'name': snapshot.warehouse.name,
                        'count': 0,
                        'qty': 0
                    }
                summary['warehouses'][wh_code]['count'] += 1
                summary['warehouses'][wh_code]['qty'] += snapshot.quantity

        except InventoryClosing.DoesNotExist:
            messages.error(request, "해당 마감 정보를 찾을 수 없습니다.")

    # 검색 필터
    search_q = request.GET.get('q', '').strip()
    warehouse_filter = request.GET.get('warehouse', '')

    if search_q and snapshots:
        snapshots = [s for s in snapshots if search_q.lower() in s.part.part_no.lower() or search_q.lower() in s.part.part_name.lower()]

    if warehouse_filter and snapshots:
        snapshots = [s for s in snapshots if s.warehouse.code == warehouse_filter]

    warehouses = Warehouse.objects.filter(is_active=True).order_by('code')

    context = {
        'closings': closings,
        'selected_closing': selected_closing,
        'snapshots': snapshots[:500],  # 최대 500건
        'summary': summary,
        'warehouses': warehouses,
        'search_q': search_q,
        'warehouse_filter': warehouse_filter,
    }
    return render(request, 'material/closing_report.html', context)


@wms_permission_required('can_wms_stock_view')
def closing_report_excel(request):
    """
    [WMS] 마감 재고 엑셀 다운로드
    """
    from .models import InventoryClosing, InventorySnapshot

    closing_id = request.GET.get('closing_id', '')
    if not closing_id:
        messages.error(request, "마감월을 선택해주세요.")
        return redirect('material:closing_report')

    try:
        closing = InventoryClosing.objects.get(id=closing_id)
    except InventoryClosing.DoesNotExist:
        messages.error(request, "마감 정보를 찾을 수 없습니다.")
        return redirect('material:closing_report')

    snapshots = InventorySnapshot.objects.filter(
        closing=closing
    ).select_related('warehouse', 'part').order_by('warehouse__code', 'part__part_no')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{closing.closing_month.strftime('%Y-%m')} 마감재고"

    # 제목
    ws.merge_cells('A1:F1')
    ws['A1'] = f"{closing.closing_month.strftime('%Y년 %m월')} 마감 재고 현황"
    ws['A1'].font = openpyxl.styles.Font(bold=True, size=14)

    # 헤더
    headers = ['창고코드', '창고명', '품번', '품명', 'LOT', '마감재고']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=3, column=col, value=header)
        cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
        cell.fill = openpyxl.styles.PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")

    # 데이터
    for row_idx, snapshot in enumerate(snapshots, 4):
        ws.cell(row=row_idx, column=1, value=snapshot.warehouse.code)
        ws.cell(row=row_idx, column=2, value=snapshot.warehouse.name)
        ws.cell(row=row_idx, column=3, value=snapshot.part.part_no)
        ws.cell(row=row_idx, column=4, value=snapshot.part.part_name)
        ws.cell(row=row_idx, column=5, value=snapshot.lot_no.strftime('%Y-%m-%d') if snapshot.lot_no else '-')
        ws.cell(row=row_idx, column=6, value=snapshot.quantity)

    # 컬럼 너비
    widths = [12, 15, 18, 25, 12, 12]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = width

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    filename = f"closing_stock_{closing.closing_month.strftime('%Y%m')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


@wms_permission_required('can_wms_inout_edit')
def outbound_create(request):
    """생산 자재 불출"""
    return render(request, 'material/outbound_create.html')


@wms_permission_required('can_wms_stock_edit')
def stock_transfer(request):
    """
    [WMS] 재고 이동 처리
    - 기존 메뉴/URL(material:stock_transfer)을 유지하면서
      실제 처리는 stock_move로 위임
    """
    return stock_move(request)



@wms_permission_required('can_wms_stock_view')
def transfer_history(request):
    """재고 이동 현황 - 창고 간 이동 이력 조회"""
    from django.core.paginator import Paginator
    from django.db.models import Q
    from orders.models import Part

    # 이동(TRANSFER) 타입만 조회
    qs = MaterialTransaction.objects.filter(
        transaction_type='TRANSFER'
    ).select_related(
        'part', 'warehouse_from', 'warehouse_to', 'actor'
    ).order_by('-date', '-id')

    # ===== 필터 파라미터 =====
    start_date = request.GET.get('start_date', '')
    end_date = request.GET.get('end_date', '')
    from_wh = request.GET.get('from_wh', '')
    to_wh = request.GET.get('to_wh', '')
    part_group = request.GET.get('part_group', '')
    q = request.GET.get('q', '').strip()

    # ===== 기간 필터 =====
    if start_date:
        qs = qs.filter(date__date__gte=start_date)
    if end_date:
        qs = qs.filter(date__date__lte=end_date)

    # ===== 창고 필터 =====
    if from_wh:
        qs = qs.filter(warehouse_from__code=from_wh)
    if to_wh:
        qs = qs.filter(warehouse_to__code=to_wh)

    # ===== 품목군 필터 =====
    if part_group:
        qs = qs.filter(part__part_group=part_group)

    # ===== 검색 (품번/품명) =====
    if q:
        qs = qs.filter(
            Q(part__part_no__icontains=q) |
            Q(part__part_name__icontains=q)
        )

    # ===== 페이징 =====
    paginator = Paginator(qs, 30)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # ===== 창고 목록 (필터용) =====
    warehouses = Warehouse.objects.all().order_by('code')

    # ===== 품목군 목록 (필터용) =====
    part_groups = Part.objects.values_list('part_group', flat=True).distinct().order_by('part_group')

    return render(request, 'material/transfer_history.html', {
        'page_obj': page_obj,
        'warehouses': warehouses,
        'part_groups': part_groups,
        'start_date': start_date,
        'end_date': end_date,
        'from_wh': from_wh,
        'to_wh': to_wh,
        'part_group': part_group,
        'q': q,
    })


@wms_permission_required('can_wms_stock_view')
def stock_check(request):
    """[현장] 재고 실사 (수동)"""
    warehouses = Warehouse.objects.filter(is_active=True)
    return render(request, 'material/stock_check.html', {
        'warehouses': warehouses,
    })


@wms_permission_required('can_wms_stock_view')
def stock_check_result(request):
    """[관리] 재고 실사 결과"""
    warehouse_id = request.GET.get('warehouse')
    part_no = request.GET.get('part_no', '').strip()
    part_name = request.GET.get('part_name', '').strip()

    stocks = MaterialStock.objects.select_related('warehouse', 'part').filter(quantity__gt=0)

    if warehouse_id:
        stocks = stocks.filter(warehouse_id=warehouse_id)
    if part_no:
        stocks = stocks.filter(part__part_no__icontains=part_no)
    if part_name:
        stocks = stocks.filter(part__part_name__icontains=part_name)

    stocks = stocks.order_by('warehouse__code', 'part__part_no', 'lot_no')[:200]

    return render(request, 'material/stock_check_result.html', {
        'stocks': stocks,
    })


# =============================================================================
# 5. 불량 반품 및 반출증
# =============================================================================

@wms_permission_required('can_wms_stock_edit')
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


@wms_permission_required('can_wms_stock_view')
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

@wms_permission_required('can_wms_stock_edit')
def stock_move(request):
    """
    [WMS] 재고 이동 처리 (다중 품목 일괄 처리 + LOT 기반)
    """

    # 1. 화면 보여주기 (GET)
    if request.method == 'GET':
        from django.core.paginator import Paginator
        from django.db.models import Q, Count

        # 전체 창고 조회
        warehouses = Warehouse.objects.all().order_by('code')

        # 이동 내역 조회
        hstart = request.GET.get('hstart', '')
        hend = request.GET.get('hend', '')
        hwh = request.GET.get('hwh', '')
        hq = request.GET.get('hq', '')

        history_qs = MaterialTransaction.objects.filter(
            transaction_type='TRANSFER'
        ).select_related('part', 'warehouse_from', 'warehouse_to', 'actor').annotate(
            label_count=Count('used_labels')
        ).order_by('-date')

        if hstart:
            history_qs = history_qs.filter(date__date__gte=hstart)
        if hend:
            history_qs = history_qs.filter(date__date__lte=hend)
        if hwh:
            history_qs = history_qs.filter(
                Q(warehouse_from__code=hwh) | Q(warehouse_to__code=hwh)
            )
        if hq:
            history_qs = history_qs.filter(
                Q(part__part_no__icontains=hq) | Q(part__part_name__icontains=hq) | Q(transaction_no__icontains=hq)
            )

        paginator = Paginator(history_qs, 20)
        page_num = request.GET.get('page', 1)
        history_page = paginator.get_page(page_num)

        production_codes = list(Warehouse.objects.filter(is_production=True).values_list('code', flat=True))

        context = {
            'warehouses': warehouses,
            'production_codes': production_codes,
            'today': timezone.now().strftime('%Y-%m-%d'),
            'history': history_page,
            'history_count': paginator.count,
            'hstart': hstart,
            'hend': hend,
            'hwh': hwh,
            'hq': hq,
        }
        return render(request, 'material/stock_move.html', context)

    # 2. 이동 처리 (POST) - LOT 기반 처리
    elif request.method == 'POST':
        part_nos = request.POST.getlist('part_no[]')
        stock_ids = request.POST.getlist('stock_id[]')  # LOT별 재고 ID
        from_locs = request.POST.getlist('from_loc[]')
        to_locs = request.POST.getlist('to_loc[]')
        move_qtys = request.POST.getlist('move_qty[]')
        label_ids_list = request.POST.getlist('label_ids[]')  # 라벨 선택 (JSON 문자열 리스트)

        # 이동 처리일 (사용자 선택, 미선택 시 오늘)
        from datetime import datetime as dt_cls
        transfer_date_str = request.POST.get('transfer_date', '').strip()
        if transfer_date_str:
            try:
                transfer_date = dt_cls.strptime(transfer_date_str, '%Y-%m-%d').date()
                transfer_datetime = timezone.make_aware(dt_cls.combine(transfer_date, timezone.now().time()))
            except ValueError:
                transfer_datetime = timezone.now()
        else:
            transfer_datetime = timezone.now()

        success_count = 0

        try:
            with transaction.atomic():
                for i in range(len(part_nos)):
                    p_no = (part_nos[i] or "").strip()
                    stock_id = (stock_ids[i] or "").strip()
                    f_loc_val = (from_locs[i] or "").strip()
                    t_loc_val = (to_locs[i] or "").strip()

                    if not p_no or not stock_id or not f_loc_val or not t_loc_val:
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

                    from_wh = Warehouse.objects.get(code=f_loc_val)
                    to_wh = Warehouse.objects.get(code=t_loc_val)

                    # 2-1. LOT별 재고(source_stock) 조회 및 차감
                    try:
                        source_stock = MaterialStock.objects.select_for_update().get(id=stock_id)
                    except MaterialStock.DoesNotExist:
                        raise ValueError(f"선택한 LOT의 재고를 찾을 수 없습니다.")

                    # 재고 검증
                    if source_stock.warehouse != from_wh:
                        raise ValueError(f"선택한 LOT가 출고 창고[{from_wh.name}]에 없습니다.")

                    if source_stock.part != part_obj:
                        raise ValueError(f"선택한 LOT가 품번[{p_no}]과 일치하지 않습니다.")

                    if int(source_stock.quantity) < qty:
                        raise ValueError(f"[{p_no}] LOT 재고 부족 (보유: {source_stock.quantity}, 요청: {qty})")

                    # LOT 정보 저장 (이동 시 동일 LOT 유지)
                    lot_no = source_stock.lot_no

                    # 출고 창고 재고 차감
                    MaterialStock.objects.filter(pk=source_stock.pk).update(
                        quantity=F('quantity') - qty
                    )

                    # 2-2. 받는 창고에 동일 LOT로 재고 증가
                    target_stock, _ = MaterialStock.objects.select_for_update().get_or_create(
                        warehouse=to_wh,
                        part=part_obj,
                        lot_no=lot_no,  # 동일 LOT 번호로 이동
                        defaults={'quantity': 0}
                    )

                    MaterialStock.objects.filter(pk=target_stock.pk).update(
                        quantity=F('quantity') + qty
                    )

                    # ✅ update(F()) 이후 실제 수량을 다시 읽어서 result_stock 숫자 저장
                    target_stock.refresh_from_db()

                    # 2-3. 이력(Transaction) 생성
                    trx_no = f"TRX-{timezone.now().strftime('%y%m%d%H%M%S%f')}-{request.user.id}-{i}"

                    lot_display = lot_no.strftime('%Y-%m-%d') if lot_no else 'NO LOT'

                    trx_obj = MaterialTransaction.objects.create(
                        transaction_no=trx_no,
                        transaction_type='TRANSFER',
                        date=transfer_datetime,
                        part=part_obj,
                        quantity=qty,
                        lot_no=lot_no,  # LOT 정보 기록
                        warehouse_from=from_wh,
                        warehouse_to=to_wh,
                        result_stock=target_stock.quantity,   # ✅ 실제 숫자
                        actor=request.user,
                        remark=f"재고이동 ({from_wh.name} -> {to_wh.name}) [LOT: {lot_display}]"
                    )

                    # 제조현장 이동 시 선택된 라벨 USED 처리
                    if to_wh.is_production and i < len(label_ids_list):
                        import json
                        try:
                            selected_ids = json.loads(label_ids_list[i]) if label_ids_list[i] else []
                        except (json.JSONDecodeError, TypeError):
                            selected_ids = []

                        if selected_ids:
                            RawMaterialLabel.objects.filter(
                                id__in=selected_ids,
                                part_no=p_no,
                                lot_no=lot_no,
                                status__in=['INSTOCK', 'PRINTED']
                            ).update(
                                status='USED',
                                used_at=transfer_datetime,
                                used_by=request.user,
                                used_transaction=trx_obj,
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

@wms_permission_required('can_wms_stock_view')
def api_part_exists(request):
    part_no = (request.GET.get('part_no') or '').strip()
    exists = Part.objects.filter(part_no=part_no).exists()
    return JsonResponse({'exists': exists})


# =============================================================================
# 6. LOT 관리 - LOT별 재고 상세 조회 API
# =============================================================================
@wms_permission_required('can_wms_stock_view')
def get_lot_details(request, part_no):
    """
    특정 품목의 LOT별 재고 상세 정보를 JSON으로 반환 (WMS용)
    warehouse 파라미터가 있으면 해당 창고만 조회
    """
    try:
        part = Part.objects.filter(part_no=part_no).first()
        if not part:
            return JsonResponse({'error': '품목을 찾을 수 없습니다.'}, status=404)

        # MaterialStock에서 해당 품목의 LOT별 재고 조회
        lot_stocks = MaterialStock.objects.filter(part=part, quantity__gt=0).select_related('warehouse')

        # 창고 필터링 (warehouse 파라미터가 있으면)
        warehouse_code = request.GET.get('warehouse')
        if warehouse_code:
            lot_stocks = lot_stocks.filter(warehouse__code=warehouse_code)

        lot_stocks = lot_stocks.order_by('lot_no')

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


@wms_permission_required('can_wms_stock_view')
def api_get_available_lots(request):
    """
    [재고 이동용] 특정 품번 + 보내는 창고의 사용 가능한 LOT 목록 반환
    FIFO 순서(오래된 순)로 정렬하여 반환
    """
    try:
        part_no = request.GET.get('part_no', '').strip()
        warehouse_code = request.GET.get('warehouse_code', '').strip()

        if not part_no or not warehouse_code:
            return JsonResponse({'error': '품번과 창고 코드가 필요합니다.'}, status=400)

        # 품번 확인
        part = Part.objects.filter(part_no=part_no).first()
        if not part:
            return JsonResponse({'error': '존재하지 않는 품번입니다.'}, status=404)

        # 창고 확인
        warehouse = Warehouse.objects.filter(code=warehouse_code).first()
        if not warehouse:
            return JsonResponse({'error': '존재하지 않는 창고입니다.'}, status=404)

        # 해당 창고의 해당 품목 LOT별 재고 조회 (FIFO 순서: 오래된 순)
        lot_stocks = MaterialStock.objects.filter(
            warehouse=warehouse,
            part=part,
            quantity__gt=0
        ).order_by('lot_no')  # 오래된 LOT가 먼저 나오도록

        lots = []
        for stock in lot_stocks:
            lot_info = {
                'stock_id': stock.id,
                'lot_no': stock.lot_no.strftime('%Y-%m-%d') if stock.lot_no else None,
                'quantity': stock.quantity,
                'days_old': (timezone.now().date() - stock.lot_no).days if stock.lot_no else 0
            }
            lots.append(lot_info)

        return JsonResponse({
            'success': True,
            'part_name': part.part_name,
            'warehouse_name': warehouse.name,
            'lots': lots
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# =============================================================================
# 7. BOM 관리 (Bill of Materials)
# =============================================================================

from .models import Product, BOMItem
import csv
import io
from decimal import Decimal, InvalidOperation

@wms_permission_required('can_wms_bom_view')
def bom_list(request):
    """
    [WMS] BOM 관리 - 모품(제품) 목록 및 BOM 조회
    """
    q = request.GET.get('q', '').strip()
    account_type = request.GET.get('account_type', '')

    products = Product.objects.filter(is_active=True)

    if q:
        products = products.filter(
            Q(part_no__icontains=q) | Q(part_name__icontains=q)
        )

    if account_type:
        products = products.filter(account_type=account_type)

    products = products.order_by('part_no')

    # 페이징
    paginator = Paginator(products, 20)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # 각 제품의 BOM 아이템 수 추가
    for product in page_obj:
        product.bom_count = product.bom_items.filter(is_active=True).count()

    context = {
        'page_obj': page_obj,
        'q': q,
        'account_type': account_type,
    }
    return render(request, 'material/bom_list.html', context)


@wms_permission_required('can_wms_bom_view')
def bom_detail(request, part_no):
    """
    [WMS] 특정 제품의 BOM 상세 조회
    """
    product = get_object_or_404(Product, part_no=part_no)
    bom_items = product.bom_items.filter(is_active=True).order_by('seq')

    context = {
        'product': product,
        'bom_items': bom_items,
    }
    return render(request, 'material/bom_detail.html', context)


@wms_permission_required('can_wms_bom_edit')
def bom_delete_all(request):
    """
    [WMS] BOM 전체 삭제
    """
    if request.method == 'POST':
        try:
            with transaction.atomic():
                bom_count = BOMItem.objects.count()
                product_count = Product.objects.count()
                BOMItem.objects.all().delete()
                Product.objects.all().delete()
                messages.success(request, f"BOM 전체 삭제 완료! (모품 {product_count}건, BOM 항목 {bom_count}건 삭제)")
        except Exception as e:
            messages.error(request, f"삭제 중 오류 발생: {str(e)}")
    return redirect('material:bom_upload')


@wms_permission_required('can_wms_bom_edit')
def bom_upload(request):
    """
    [WMS] BOM 데이터 엑셀(CSV) 업로드
    - 엑셀 헤더: No,모품번,모품명,규격,재고단위,계정구분,조달구분,BOM등록여부,순번,자품번,자품명,규격,재고단위,정미수량,LOSS(%),필요수량,사급구분,외주구분,시작일자,종료일자,도면번호,재질,주거래처,사용여부,BOM사용여부,비고
    - 주의: '규격'과 '재고단위' 컬럼이 모품/자품용으로 2번 나오므로 인덱스 기반으로 처리
    """
    # 현재 BOM 데이터 수량 (삭제 버튼 표시용)
    bom_count = BOMItem.objects.count()
    product_count = Product.objects.count()

    if request.method == 'POST':
        upload_file = request.FILES.get('bom_file')

        if not upload_file:
            messages.error(request, "파일을 선택해주세요.")
            return redirect('material:bom_upload')

        # 파일 확장자 확인
        file_name = upload_file.name.lower()
        if not (file_name.endswith('.csv') or file_name.endswith('.xlsx')):
            messages.error(request, "CSV 또는 XLSX 파일만 업로드 가능합니다.")
            return redirect('material:bom_upload')

        try:
            rows = []
            headers = []

            if file_name.endswith('.csv'):
                # CSV 파일 처리 - 인덱스 기반으로 읽기
                decoded_file = upload_file.read().decode('utf-8-sig')
                lines = decoded_file.strip().split('\n')
                if lines:
                    # 헤더 파싱 (중복 컬럼명 처리를 위해 인덱스 사용)
                    headers = lines[0].split(',')
                    for line in lines[1:]:
                        # CSV 파싱 (쉼표가 따옴표 안에 있을 수 있음)
                        import re
                        # 간단한 CSV 파싱 - 따옴표 안의 쉼표 처리
                        values = []
                        in_quotes = False
                        current = ''
                        for char in line:
                            if char == '"':
                                in_quotes = not in_quotes
                            elif char == ',' and not in_quotes:
                                values.append(current.strip().strip('"'))
                                current = ''
                            else:
                                current += char
                        values.append(current.strip().strip('"'))

                        if len(values) >= 10:  # 최소 필수 컬럼 수
                            rows.append(values)
            else:
                # XLSX 파일 처리
                wb = openpyxl.load_workbook(upload_file, read_only=True)
                ws = wb.active

                # 헤더 읽기
                headers = [cell.value or '' for cell in next(ws.iter_rows(min_row=1, max_row=1))]

                for row in ws.iter_rows(min_row=2, values_only=True):
                    values = [v if v is not None else '' for v in row]
                    if len(values) >= 10:
                        rows.append(values)

            # 컬럼 인덱스 매핑 (헤더 기반)
            # No,모품번,모품명,규격,재고단위,계정구분,조달구분,BOM등록여부,순번,자품번,자품명,규격,재고단위,정미수량,LOSS(%),필요수량,...
            # 0   1     2     3    4       5       6       7          8    9     10    11   12      13       14      15
            col_map = {
                '모품번': 1, '모품명': 2, '모품규격': 3, '모품단위': 4,
                '계정구분': 5, '조달구분': 6, 'BOM등록여부': 7,
                '순번': 8, '자품번': 9, '자품명': 10, '자품규격': 11, '자품단위': 12,
                '정미수량': 13, 'LOSS': 14, '필요수량': 15,
                '사급구분': 16, '외주구분': 17, '시작일자': 18, '종료일자': 19,
                '도면번호': 20, '재질': 21, '주거래처': 22,
                '사용여부': 23, 'BOM사용여부': 24, '비고': 25
            }

            def get_val(row_data, idx, default=''):
                if idx < len(row_data):
                    return str(row_data[idx]).strip() if row_data[idx] else default
                return default

            def parse_decimal(val, default=0):
                if val is None or str(val).strip() == '':
                    return Decimal(default)
                try:
                    return Decimal(str(val).strip().replace(',', ''))
                except (InvalidOperation, ValueError):
                    return Decimal(default)

            def parse_int(val, default=1):
                if val is None or str(val).strip() == '':
                    return default
                try:
                    return int(float(str(val).strip()))
                except (ValueError, TypeError):
                    return default

            def parse_date(val):
                if not val or str(val).strip() == '':
                    return None
                try:
                    from datetime import datetime
                    val_str = str(val).strip()
                    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y%m%d'):
                        try:
                            return datetime.strptime(val_str, fmt).date()
                        except ValueError:
                            continue
                    return None
                except:
                    return None

            product_count = 0
            bom_item_count = 0
            products_cache = {}  # 모품번 -> Product 객체 캐시

            with transaction.atomic():
                for row_data in rows:
                    # 모품번 가져오기
                    part_no = get_val(row_data, col_map['모품번'])
                    if not part_no:
                        continue

                    # Product 캐시에서 찾거나 생성
                    if part_no not in products_cache:
                        bom_registered_val = get_val(row_data, col_map['BOM등록여부'], 'Y')
                        is_bom = bom_registered_val.upper() in ('Y', 'YES', '1', 'TRUE', 'O', '등록')

                        product, created = Product.objects.update_or_create(
                            part_no=part_no,
                            defaults={
                                'part_name': get_val(row_data, col_map['모품명']),
                                'spec': get_val(row_data, col_map['모품규격']) or None,
                                'unit': get_val(row_data, col_map['모품단위'], 'EA'),
                                'account_type': get_val(row_data, col_map['계정구분'], '제품'),
                                'procurement_type': get_val(row_data, col_map['조달구분'], '생산'),
                                'is_bom_registered': is_bom,
                                'is_active': True,
                            }
                        )
                        products_cache[part_no] = product
                        if created:
                            product_count += 1

                    current_product = products_cache[part_no]

                    # 자품번이 있으면 BOMItem 생성
                    child_part_no = get_val(row_data, col_map['자품번'])
                    if not child_part_no:
                        continue

                    seq = parse_int(get_val(row_data, col_map['순번']), 1)

                    use_val = get_val(row_data, col_map['사용여부'], 'Y')
                    bom_use_val = get_val(row_data, col_map['BOM사용여부'], 'Y')

                    # BOMItem 생성 또는 업데이트
                    bom_item, created = BOMItem.objects.update_or_create(
                        product=current_product,
                        seq=seq,
                        child_part_no=child_part_no,
                        defaults={
                            'child_part_name': get_val(row_data, col_map['자품명']),
                            'child_spec': get_val(row_data, col_map['자품규격']) or None,
                            'child_unit': get_val(row_data, col_map['자품단위'], 'EA'),
                            'net_qty': parse_decimal(get_val(row_data, col_map['정미수량'])),
                            'loss_rate': parse_decimal(get_val(row_data, col_map['LOSS'])) if get_val(row_data, col_map['LOSS']) else None,
                            'required_qty': parse_decimal(get_val(row_data, col_map['필요수량'])),
                            'supply_type': get_val(row_data, col_map['사급구분'], '자재'),
                            'outsource_type': get_val(row_data, col_map['외주구분'], '무상'),
                            'start_date': parse_date(get_val(row_data, col_map['시작일자'])),
                            'end_date': parse_date(get_val(row_data, col_map['종료일자'])),
                            'drawing_no': get_val(row_data, col_map['도면번호']) or None,
                            'material': get_val(row_data, col_map['재질']) or None,
                            'vendor_name': get_val(row_data, col_map['주거래처']) or None,
                            'is_active': use_val.upper() in ('Y', 'YES', '1', 'TRUE', 'O', '사용', ''),
                            'is_bom_active': bom_use_val.upper() in ('Y', 'YES', '1', 'TRUE', 'O', '사용', ''),
                            'remark': get_val(row_data, col_map['비고']) or None,
                        }
                    )
                    if created:
                        bom_item_count += 1

            messages.success(request, f"BOM 업로드 완료! 모품 {product_count}건, BOM 항목 {bom_item_count}건 추가됨")
            return redirect('material:bom_list')

        except Exception as e:
            import traceback
            messages.error(request, f"파일 처리 중 오류 발생: {str(e)}")
            return redirect('material:bom_upload')

    return render(request, 'material/bom_upload.html', {
        'bom_count': bom_count,
        'product_count': product_count,
    })


def _calculate_bom_requirements(part_no, production_qty):
    """
    BOM 소요량 계산 공통 함수
    """
    product = Product.objects.filter(part_no=part_no, is_active=True).first()
    if not product:
        return None, None, []

    bom_items = product.bom_items.filter(is_active=True, is_bom_active=True).order_by('seq')
    result = []

    for item in bom_items:
        required_qty = item.required_qty * production_qty

        # WMS 재고 조회 (자품번으로 Part 매칭)
        stock_qty = 0
        part_obj = Part.objects.filter(part_no=item.child_part_no).first()
        if part_obj:
            stock_qty = MaterialStock.objects.filter(
                part=part_obj,
                quantity__gt=0
            ).aggregate(total=Sum('quantity'))['total'] or 0

        shortage = max(0, float(required_qty) - stock_qty)

        result.append({
            'seq': item.seq,
            'child_part_no': item.child_part_no,
            'child_part_name': item.child_part_name,
            'child_unit': item.child_unit,
            'unit_qty': float(item.required_qty),
            'required_qty': float(required_qty),
            'stock_qty': float(stock_qty),
            'shortage': float(shortage),
            'supply_type': item.supply_type,
            'vendor_name': item.vendor_name,
        })

    return product, product.part_name if product else None, result


@wms_permission_required('can_wms_bom_view')
def bom_calculate(request):
    """
    [WMS] 소요량 계산 - 제품 품번과 생산수량 입력 시 필요 자재량 계산
    - 단일 계산: 개별 품번 입력
    - 일괄 계산: 엑셀 업로드
    """
    result = None
    product = None
    production_qty = 0
    calc_type = None
    batch_results = None
    shortage_count = 0
    sufficient_count = 0
    total_material_count = 0
    total_shortage_count = 0
    total_sufficient_count = 0
    session_key = None

    if request.method == 'POST':
        calc_type = request.POST.get('calc_type', 'single')

        if calc_type == 'single':
            # 단일 제품 계산
            part_no = request.POST.get('part_no', '').strip()
            production_qty = int(request.POST.get('production_qty', 0) or 0)

            if part_no and production_qty > 0:
                product, part_name, result = _calculate_bom_requirements(part_no, production_qty)

                if product and result:
                    shortage_count = sum(1 for item in result if item['shortage'] > 0)
                    sufficient_count = len(result) - shortage_count
                else:
                    messages.warning(request, f"품번 '{part_no}'에 해당하는 BOM이 없습니다.")

        elif calc_type == 'batch':
            # 일괄 계산 (엑셀 업로드)
            upload_file = request.FILES.get('calc_file')

            if not upload_file:
                messages.error(request, "파일을 선택해주세요.")
            else:
                file_name = upload_file.name.lower()
                if not (file_name.endswith('.csv') or file_name.endswith('.xlsx')):
                    messages.error(request, "CSV 또는 XLSX 파일만 업로드 가능합니다.")
                else:
                    try:
                        rows = []

                        if file_name.endswith('.csv'):
                            decoded_file = upload_file.read().decode('utf-8-sig')
                            lines = decoded_file.strip().split('\n')
                            if len(lines) > 1:
                                headers = [h.strip() for h in lines[0].split(',')]
                                for line in lines[1:]:
                                    values = [v.strip().strip('"') for v in line.split(',')]
                                    if len(values) >= 2:
                                        rows.append(dict(zip(headers, values)))
                        else:
                            wb = openpyxl.load_workbook(upload_file, read_only=True)
                            ws = wb.active
                            headers = [cell.value or '' for cell in next(ws.iter_rows(min_row=1, max_row=1))]
                            headers = [str(h).strip() for h in headers]

                            for row in ws.iter_rows(min_row=2, values_only=True):
                                values = [v if v is not None else '' for v in row]
                                if len(values) >= 2:
                                    rows.append(dict(zip(headers, values)))

                        # 일괄 계산 수행
                        batch_results = []
                        for row in rows:
                            part_no = str(row.get('품번', '')).strip()
                            qty = row.get('수량', 0)
                            need_date = row.get('필요일자', '')

                            # need_date가 datetime 객체인 경우 문자열로 변환
                            if need_date and hasattr(need_date, 'strftime'):
                                need_date = need_date.strftime('%Y-%m-%d')
                            else:
                                need_date = str(need_date) if need_date else ''

                            if not part_no:
                                continue

                            try:
                                qty = int(float(str(qty).replace(',', '')))
                            except:
                                qty = 0

                            if qty <= 0:
                                continue

                            product_obj, part_name, items = _calculate_bom_requirements(part_no, qty)

                            batch_results.append({
                                'part_no': part_no,
                                'part_name': part_name or '-',
                                'qty': qty,
                                'need_date': need_date,
                                'items': items,
                            })

                            # 통계
                            total_material_count += len(items)
                            total_shortage_count += sum(1 for item in items if item['shortage'] > 0)
                            total_sufficient_count += sum(1 for item in items if item['shortage'] == 0)

                        if batch_results:
                            # 세션에 결과 저장 (엑셀 다운로드용)
                            import uuid
                            session_key = str(uuid.uuid4())
                            request.session[f'batch_calc_{session_key}'] = batch_results
                            messages.success(request, f"{len(batch_results)}개 제품의 소요량 계산이 완료되었습니다.")
                        else:
                            messages.warning(request, "유효한 데이터가 없습니다. 파일 형식을 확인해주세요.")

                    except Exception as e:
                        messages.error(request, f"파일 처리 중 오류: {str(e)}")

    # 제품 목록 (자동완성용)
    products = Product.objects.filter(is_active=True, is_bom_registered=True).order_by('part_no')

    context = {
        'products': products,
        'product': product,
        'production_qty': production_qty,
        'result': result,
        'calc_type': calc_type,
        'batch_results': batch_results,
        'shortage_count': shortage_count,
        'sufficient_count': sufficient_count,
        'total_material_count': total_material_count,
        'total_shortage_count': total_shortage_count,
        'total_sufficient_count': total_sufficient_count,
        'session_key': session_key,
    }
    return render(request, 'material/bom_calculate.html', context)


@wms_permission_required('can_wms_bom_view')
def bom_calc_template(request):
    """
    [WMS] 소요량 계산용 엑셀 양식 다운로드
    """
    from datetime import datetime, timedelta

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "소요량계산양식"

    # 헤더
    headers = ['품번', '수량', '필요일자']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = openpyxl.styles.Font(bold=True)
        cell.fill = openpyxl.styles.PatternFill(start_color="DAEEF3", end_color="DAEEF3", fill_type="solid")

    # 예시 데이터 (오늘 기준으로 날짜 생성)
    today = datetime.now().date()
    ws.cell(row=2, column=1, value="064133-0010")
    ws.cell(row=2, column=2, value=100)
    date_cell1 = ws.cell(row=2, column=3, value=today + timedelta(days=4))
    date_cell1.number_format = 'YYYY-MM-DD'

    ws.cell(row=3, column=1, value="064133-0020")
    ws.cell(row=3, column=2, value=50)
    date_cell2 = ws.cell(row=3, column=3, value=today + timedelta(days=5))
    date_cell2.number_format = 'YYYY-MM-DD'

    # 컬럼 너비 조정
    ws.column_dimensions['A'].width = 20
    ws.column_dimensions['B'].width = 12
    ws.column_dimensions['C'].width = 15

    # C열(필요일자) 전체를 날짜 형식으로 지정
    for row in range(2, 1000):  # 충분한 행 수 지정
        ws.cell(row=row, column=3).number_format = 'YYYY-MM-DD'

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="bom_calc_template.xlsx"'
    wb.save(response)
    return response


@wms_permission_required('can_wms_bom_view')
def bom_calc_export(request):
    """
    [WMS] 단일 제품 소요량 계산 결과 엑셀 다운로드
    """
    part_no = request.GET.get('part_no', '').strip()
    qty = int(request.GET.get('qty', 0) or 0)

    if not part_no or qty <= 0:
        messages.error(request, "품번과 수량이 필요합니다.")
        return redirect('material:bom_calculate')

    product, part_name, items = _calculate_bom_requirements(part_no, qty)

    if not product or not items:
        messages.error(request, "BOM 데이터가 없습니다.")
        return redirect('material:bom_calculate')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "소요량계산결과"

    # 제목
    ws.merge_cells('A1:I1')
    ws['A1'] = f"소요량 계산 결과 - {part_no} ({part_name}) / 생산수량: {qty}개"
    ws['A1'].font = openpyxl.styles.Font(bold=True, size=14)

    # 헤더
    headers = ['순번', '자품번', '자품명', '단위', '단위소요량', '필요수량', '현재고', '부족수량', '주거래처']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=3, column=col, value=header)
        cell.font = openpyxl.styles.Font(bold=True)
        cell.fill = openpyxl.styles.PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF")

    # 데이터
    for row_idx, item in enumerate(items, 4):
        ws.cell(row=row_idx, column=1, value=item['seq'])
        ws.cell(row=row_idx, column=2, value=item['child_part_no'])
        ws.cell(row=row_idx, column=3, value=item['child_part_name'])
        ws.cell(row=row_idx, column=4, value=item['child_unit'])
        ws.cell(row=row_idx, column=5, value=float(item['unit_qty']))
        ws.cell(row=row_idx, column=6, value=float(item['required_qty']))
        ws.cell(row=row_idx, column=7, value=item['stock_qty'])
        ws.cell(row=row_idx, column=8, value=item['shortage'])
        ws.cell(row=row_idx, column=9, value=item['vendor_name'] or '-')

        # 부족분 강조
        if item['shortage'] > 0:
            for col in range(1, 10):
                ws.cell(row=row_idx, column=col).fill = openpyxl.styles.PatternFill(
                    start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"
                )

    # 컬럼 너비 조정
    widths = [8, 18, 25, 8, 12, 12, 12, 12, 15]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = width

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="bom_calc_{part_no}.xlsx"'
    wb.save(response)
    return response


@wms_permission_required('can_wms_bom_view')
def bom_calc_batch_export(request):
    """
    [WMS] 일괄 소요량 계산 결과 엑셀 다운로드
    """
    session_key = request.GET.get('session_key', '')
    batch_results = request.session.get(f'batch_calc_{session_key}')

    if not batch_results:
        messages.error(request, "계산 결과가 없습니다. 다시 계산해주세요.")
        return redirect('material:bom_calculate')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "일괄소요량계산결과"

    # 헤더
    headers = ['모품번', '모품명', '생산수량', '필요일자', '자품번', '자품명', '필요수량', '현재고', '부족수량', '거래처']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
        cell.fill = openpyxl.styles.PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")

    # 데이터
    row_idx = 2
    for batch in batch_results:
        if batch['items']:
            for item_idx, item in enumerate(batch['items']):
                if item_idx == 0:
                    ws.cell(row=row_idx, column=1, value=batch['part_no'])
                    ws.cell(row=row_idx, column=2, value=batch['part_name'])
                    ws.cell(row=row_idx, column=3, value=batch['qty'])
                    ws.cell(row=row_idx, column=4, value=batch['need_date'] or '')

                ws.cell(row=row_idx, column=5, value=item['child_part_no'])
                ws.cell(row=row_idx, column=6, value=item['child_part_name'])
                ws.cell(row=row_idx, column=7, value=float(item['required_qty']))
                ws.cell(row=row_idx, column=8, value=item['stock_qty'])
                ws.cell(row=row_idx, column=9, value=item['shortage'])
                ws.cell(row=row_idx, column=10, value=item['vendor_name'] or '-')

                # 부족분 강조
                if item['shortage'] > 0:
                    for col in range(1, 11):
                        ws.cell(row=row_idx, column=col).fill = openpyxl.styles.PatternFill(
                            start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"
                        )

                row_idx += 1
        else:
            # BOM 없는 경우
            ws.cell(row=row_idx, column=1, value=batch['part_no'])
            ws.cell(row=row_idx, column=2, value=batch['part_name'])
            ws.cell(row=row_idx, column=3, value=batch['qty'])
            ws.cell(row=row_idx, column=4, value=batch['need_date'] or '')
            ws.cell(row=row_idx, column=5, value='BOM 없음')
            for col in range(1, 11):
                ws.cell(row=row_idx, column=col).fill = openpyxl.styles.PatternFill(
                    start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"
                )
            row_idx += 1

    # 컬럼 너비 조정
    widths = [18, 20, 10, 12, 18, 20, 12, 12, 12, 15]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = width

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="bom_calc_batch_result.xlsx"'
    wb.save(response)
    return response


@wms_permission_required('can_wms_bom_view')
def api_bom_calculate(request):
    """
    [API] 소요량 계산 AJAX 엔드포인트
    """
    part_no = request.GET.get('part_no', '').strip()
    production_qty = int(request.GET.get('qty', 0) or 0)

    if not part_no or production_qty <= 0:
        return JsonResponse({'error': '품번과 생산수량을 입력하세요.'}, status=400)

    product = Product.objects.filter(part_no=part_no, is_active=True).first()
    if not product:
        return JsonResponse({'error': '해당 품번의 BOM을 찾을 수 없습니다.'}, status=404)

    bom_items = product.bom_items.filter(is_active=True, is_bom_active=True).order_by('seq')
    result = []

    for item in bom_items:
        required_qty = float(item.required_qty) * production_qty

        # WMS 재고 조회
        stock_qty = 0
        part_obj = Part.objects.filter(part_no=item.child_part_no).first()
        if part_obj:
            stock_qty = MaterialStock.objects.filter(
                part=part_obj,
                quantity__gt=0
            ).aggregate(total=Sum('quantity'))['total'] or 0

        shortage = max(0, required_qty - stock_qty)

        result.append({
            'seq': item.seq,
            'child_part_no': item.child_part_no,
            'child_part_name': item.child_part_name,
            'child_unit': item.child_unit,
            'unit_qty': float(item.required_qty),
            'required_qty': required_qty,
            'stock_qty': stock_qty,
            'shortage': shortage,
            'supply_type': item.supply_type,
            'vendor_name': item.vendor_name or '-',
        })

    return JsonResponse({
        'success': True,
        'product': {
            'part_no': product.part_no,
            'part_name': product.part_name,
            'account_type': product.account_type,
        },
        'production_qty': production_qty,
        'items': result,
    })


@wms_permission_required('can_wms_bom_edit')
def bom_register_demand(request):
    """
    [WMS] BOM 일괄 소요량 계산 결과를 SCM 소요량(Demand)으로 등록
    - 품목 마스터(Part)에 존재하는 자재만 등록
    - part + due_date 기준으로 update_or_create (기존 데이터 업데이트)
    """
    from datetime import datetime

    if request.method != 'POST':
        messages.error(request, "잘못된 요청입니다.")
        return redirect('material:bom_calculate')

    session_key = request.POST.get('session_key', '')
    batch_results = request.session.get(f'batch_calc_{session_key}')

    if not batch_results:
        messages.error(request, "계산 결과가 없습니다. 다시 계산해주세요.")
        return redirect('material:bom_calculate')

    # 동일 자품번+필요일자 기준으로 필요수량 합산
    demand_map = {}  # key: (child_part_no, need_date), value: required_qty 합계

    for batch in batch_results:
        need_date = batch.get('need_date', '')

        # 날짜 형식 파싱
        if need_date:
            if isinstance(need_date, str):
                try:
                    need_date = datetime.strptime(need_date, '%Y-%m-%d').date()
                except:
                    need_date = None
            elif hasattr(need_date, 'date'):
                need_date = need_date.date()
        else:
            need_date = None

        if not need_date:
            continue  # 필요일자 없는 항목은 스킵

        for item in batch.get('items', []):
            child_part_no = item.get('child_part_no', '')
            required_qty = item.get('required_qty', 0)

            if not child_part_no or required_qty <= 0:
                continue

            key = (child_part_no, need_date)
            if key in demand_map:
                demand_map[key] += required_qty
            else:
                demand_map[key] = required_qty

    if not demand_map:
        messages.warning(request, "등록할 소요량 데이터가 없습니다. 필요일자가 입력된 항목이 있는지 확인해주세요.")
        return redirect('material:bom_calculate')

    # SCM 소요량 등록
    registered_count = 0
    updated_count = 0
    skipped_count = 0
    skipped_parts = []

    with transaction.atomic():
        for (child_part_no, need_date), total_qty in demand_map.items():
            # Part 마스터에서 조회
            part = Part.objects.filter(part_no=child_part_no).first()

            if not part:
                skipped_count += 1
                if child_part_no not in skipped_parts:
                    skipped_parts.append(child_part_no)
                continue

            # Demand update_or_create
            demand, created = Demand.objects.update_or_create(
                part=part,
                due_date=need_date,
                defaults={
                    'quantity': int(total_qty)
                }
            )

            if created:
                registered_count += 1
            else:
                updated_count += 1

    # 결과 메시지
    result_msg = f"SCM 소요량 등록 완료: 신규 {registered_count}건, 업데이트 {updated_count}건"
    if skipped_count > 0:
        result_msg += f", 스킵 {skipped_count}건 (품목마스터 미존재)"
        if len(skipped_parts) <= 5:
            result_msg += f" - {', '.join(skipped_parts)}"
        else:
            result_msg += f" - {', '.join(skipped_parts[:5])} 외 {len(skipped_parts) - 5}건"

    if registered_count > 0 or updated_count > 0:
        messages.success(request, result_msg)
    else:
        messages.warning(request, result_msg)

    return redirect('material:bom_calculate')


# =============================================================================
# 재고조사 (Inventory Check)
# =============================================================================

from .models import InventoryCheckSession, InventoryCheckSessionItem, ProcessTag


@login_required
def inventory_check_list(request):
    """재고조사 목록"""
    qs = InventoryCheckSession.objects.select_related('warehouse', 'created_by').all()

    # 필터
    status = request.GET.get('status')
    warehouse_id = request.GET.get('warehouse')
    if status:
        qs = qs.filter(status=status)
    if warehouse_id:
        qs = qs.filter(warehouse_id=warehouse_id)

    paginator = Paginator(qs, 20)
    page = request.GET.get('page', 1)
    checks = paginator.get_page(page)

    warehouses = Warehouse.objects.filter(is_active=True)

    return render(request, 'material/inventory_check_list.html', {
        'checks': checks,
        'warehouses': warehouses,
        'status_choices': InventoryCheckSession.STATUS_CHOICES,
    })


@login_required
def inventory_check_create(request):
    """재고조사 세션 생성"""
    if request.method == 'POST':
        warehouse_id = request.POST.get('warehouse')
        check_date = request.POST.get('check_date') or timezone.localdate()
        remark = request.POST.get('remark', '')

        check = InventoryCheckSession.objects.create(
            check_no=InventoryCheckSession.generate_check_no(),
            warehouse_id=warehouse_id,
            check_date=check_date,
            status='IN_PROGRESS',
            created_by=request.user,
            remark=remark,
        )
        messages.success(request, f'재고조사 {check.check_no}가 시작되었습니다.')
        return redirect('material:inventory_check_scan', pk=check.pk)

    warehouses = Warehouse.objects.filter(is_active=True)
    return render(request, 'material/inventory_check_create.html', {
        'warehouses': warehouses,
    })


@login_required
def inventory_check_scan(request, pk):
    """재고조사 QR 스캔 페이지"""
    check = get_object_or_404(InventoryCheckSession.objects.select_related('warehouse'), pk=pk)

    if check.status not in ('DRAFT', 'IN_PROGRESS'):
        messages.warning(request, '이 조사는 이미 완료되었습니다.')
        return redirect('material:inventory_check_result', pk=check.pk)

    items = check.check_items.select_related('part').order_by('-scanned_at')[:50]

    return render(request, 'material/inventory_check_scan.html', {
        'check': check,
        'items': items,
    })


from django.views.decorators.http import require_POST

@require_POST
def inventory_check_scan_api(request, pk):
    """재고조사 QR 스캔 API (AJAX)"""
    from django.http import JsonResponse
    import json
    import traceback

    # AJAX API는 로그인 안됐을 때 JSON 반환
    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'error': '로그인이 필요합니다.'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST only'}, status=405)

    try:
        check = InventoryCheckSession.objects.get(pk=pk)
    except InventoryCheckSession.DoesNotExist:
        return JsonResponse({'success': False, 'error': '조사를 찾을 수 없습니다.'}, status=404)

    if check.status not in ('DRAFT', 'IN_PROGRESS'):
        return JsonResponse({'success': False, 'error': '조사가 이미 완료되었습니다.'})

    try:
        data = json.loads(request.body)
        raw_input = data.get('tag_id', '').strip()
    except json.JSONDecodeError:
        raw_input = request.POST.get('tag_id', '').strip()

    if not raw_input:
        return JsonResponse({'success': False, 'error': '태그ID가 없습니다.'})

    # QR 데이터 파싱: TAG_ID|품번|수량|LOT 형식에서 TAG_ID만 추출
    tag_id = raw_input.split('|')[0].strip() if '|' in raw_input else raw_input

    # 이미 스캔한 태그인지 확인
    if check.check_items.filter(tag_id=tag_id).exists():
        return JsonResponse({'success': False, 'error': f'이미 스캔된 태그입니다: {tag_id}', 'duplicate': True})

    # 현품표 조회
    try:
        tag = ProcessTag.objects.select_related('part').get(tag_id=tag_id)
    except ProcessTag.DoesNotExist:
        return JsonResponse({'success': False, 'error': f'현품표를 찾을 수 없습니다: {tag_id}'})

    try:
        # 품목 마스터 조회
        part = tag.part
        part_no = tag.part_no
        part_name = tag.part_name

        if part:
            # 마스터에서 최신 품명 가져오기
            part_name = part.part_name

        # 스캔 수량만 기록 (품목별 합계 비교는 결과 화면에서)
        scanned_qty = tag.quantity

        # 항목 저장 (개별 비교 없이 스캔 기록만)
        item = InventoryCheckSessionItem.objects.create(
            check_session=check,
            process_tag=tag,
            tag_id=tag_id,
            part=part,
            part_no=part_no,
            part_name=part_name,
            lot_no=tag.lot_no,
            scanned_qty=scanned_qty,
            system_qty=0,  # 결과 화면에서 품목별로 계산
            discrepancy=0,
            is_matched=True,  # 임시
            scanned_by=request.user,
        )

        # 요약 업데이트
        check.update_summary()

        return JsonResponse({
            'success': True,
            'item': {
                'id': item.id,
                'tag_id': item.tag_id,
                'part_no': item.part_no,
                'part_name': item.part_name,
                'lot_no': item.lot_no.strftime('%Y-%m-%d') if item.lot_no else '-',
                'scanned_qty': item.scanned_qty,
                'system_qty': item.system_qty,
                'discrepancy': item.discrepancy,
                'is_matched': item.is_matched,
            },
            'summary': {
                'total_scanned': check.total_scanned,
                'total_matched': check.total_matched,
                'total_discrepancy': check.total_discrepancy,
            }
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': f'서버 오류: {str(e)}',
            'traceback': traceback.format_exc()
        }, status=500)


@login_required
def inventory_check_complete(request, pk):
    """재고조사 완료"""
    check = get_object_or_404(InventoryCheckSession, pk=pk)

    if request.method == 'POST':
        check.status = 'COMPLETED'
        check.completed_by = request.user
        check.completed_at = timezone.now()
        check.update_summary()
        check.save()
        messages.success(request, f'재고조사 {check.check_no}가 완료되었습니다.')
        return redirect('material:inventory_check_result', pk=check.pk)

    return redirect('material:inventory_check_scan', pk=check.pk)


@login_required
def inventory_check_result(request, pk):
    """재고조사 결과 - 품목별 집계"""
    from django.db.models import Sum, Count
    from collections import defaultdict

    check = get_object_or_404(InventoryCheckSession.objects.select_related('warehouse', 'created_by', 'completed_by'), pk=pk)
    items = check.check_items.select_related('part').order_by('-scanned_at')

    # 품목별 스캔 수량 집계
    part_scanned = check.check_items.values('part_no', 'part_name', 'part_id').annotate(
        total_scanned=Sum('scanned_qty'),
        tag_count=Count('id')
    ).order_by('part_no')

    # 품목별 결과 생성
    part_results = []
    total_matched = 0
    total_discrepancy = 0

    for row in part_scanned:
        part_no = row['part_no']
        part_id = row['part_id']
        scanned_total = row['total_scanned'] or 0
        tag_count = row['tag_count']

        # 시스템 재고 조회
        system_qty = MaterialStock.objects.filter(
            warehouse=check.warehouse,
            part_id=part_id,
        ).aggregate(total=Sum('quantity'))['total'] or 0

        discrepancy = scanned_total - system_qty
        is_matched = (discrepancy == 0)

        if is_matched:
            total_matched += 1
        else:
            total_discrepancy += 1

        part_results.append({
            'part_no': part_no,
            'part_name': row['part_name'],
            'tag_count': tag_count,
            'scanned_total': scanned_total,
            'system_qty': system_qty,
            'discrepancy': discrepancy,
            'is_matched': is_matched,
        })

    return render(request, 'material/inventory_check_result.html', {
        'check': check,
        'items': items,  # 개별 스캔 내역
        'part_results': part_results,  # 품목별 집계 결과
        'total_matched': total_matched,
        'total_discrepancy': total_discrepancy,
    })


# =============================================================================
# 재고 종합 조회 (피벗 테이블)
# =============================================================================

@login_required
def inventory_summary(request):
    """재고조사 종합 집계표 - 창고별 피벗 테이블 (스캔 데이터 기반)"""
    from collections import defaultdict
    from orders.models import Part

    # 활성 창고 목록
    warehouses = Warehouse.objects.filter(is_active=True).order_by('code')

    # 필터
    part_no_q = request.GET.get('part_no', '').strip()
    part_name_q = request.GET.get('part_name', '').strip()
    category_q = request.GET.get('category', '').strip()

    # 스캔 데이터 조회 (InventoryCheckSessionItem에서)
    items = InventoryCheckSessionItem.objects.select_related(
        'check_session', 'check_session__warehouse', 'part'
    )

    if part_no_q:
        items = items.filter(part_no__icontains=part_no_q)
    if part_name_q:
        items = items.filter(part_name__icontains=part_name_q)
    if category_q:
        items = items.filter(part__part_group=category_q)

    # 품목별/창고별로 그룹화 (피벗 테이블 생성)
    part_data = defaultdict(lambda: {
        'part': None,
        'part_name': '',
        'total': 0,
        'warehouses': defaultdict(int)
    })

    for item in items:
        part_no = item.part_no
        wh_code = item.check_session.warehouse.code
        part_data[part_no]['part'] = item.part
        part_data[part_no]['part_name'] = item.part_name
        part_data[part_no]['total'] += item.scanned_qty
        part_data[part_no]['warehouses'][wh_code] += item.scanned_qty

    # 리스트로 변환하여 정렬
    summary_list = []
    for part_no, data in sorted(part_data.items()):
        row = {
            'part_no': part_no,
            'part': data['part'],
            'part_name': data.get('part_name', ''),
            'total': data['total'],
            'warehouse_qty': {wh.code: data['warehouses'].get(wh.code, 0) for wh in warehouses}
        }
        summary_list.append(row)

    # 창고별 소계
    warehouse_totals = {wh.code: 0 for wh in warehouses}
    grand_total = 0
    for row in summary_list:
        grand_total += row['total']
        for wh in warehouses:
            warehouse_totals[wh.code] += row['warehouse_qty'].get(wh.code, 0)

    # 품목군 목록 (필터용)
    categories = Part.objects.exclude(part_group__isnull=True).exclude(part_group='').values_list('part_group', flat=True).distinct()

    return render(request, 'material/inventory_summary.html', {
        'summary_list': summary_list,
        'warehouses': warehouses,
        'warehouse_totals': warehouse_totals,
        'grand_total': grand_total,
        'categories': categories,
        'part_no_q': part_no_q,
        'part_name_q': part_name_q,
        'category_q': category_q,
    })


@login_required
def inventory_summary_excel(request):
    """재고조사 종합 집계표 - 엑셀 다운로드 (창고별 피벗)"""
    from collections import defaultdict
    from django.http import HttpResponse
    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
    from openpyxl.utils import get_column_letter
    from datetime import datetime

    # 활성 창고 목록
    warehouses = Warehouse.objects.filter(is_active=True).order_by('code')

    # 필터
    part_no_q = request.GET.get('part_no', '').strip()
    part_name_q = request.GET.get('part_name', '').strip()
    category_q = request.GET.get('category', '').strip()

    # 스캔 데이터 조회
    items = InventoryCheckSessionItem.objects.select_related(
        'check_session', 'check_session__warehouse', 'part'
    )

    if part_no_q:
        items = items.filter(part_no__icontains=part_no_q)
    if part_name_q:
        items = items.filter(part_name__icontains=part_name_q)
    if category_q:
        items = items.filter(part__part_group=category_q)

    # 품목별/창고별로 그룹화
    part_data = defaultdict(lambda: {
        'part': None,
        'part_name': '',
        'total': 0,
        'warehouses': defaultdict(int)
    })

    for item in items:
        part_no = item.part_no
        wh_code = item.check_session.warehouse.code
        part_data[part_no]['part'] = item.part
        part_data[part_no]['part_name'] = item.part_name
        part_data[part_no]['total'] += item.scanned_qty
        part_data[part_no]['warehouses'][wh_code] += item.scanned_qty

    # 엑셀 생성
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "재고조사집계"

    # 스타일 정의
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="2E7D32", end_color="2E7D32", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )
    number_format = '#,##0'

    # 제목
    today = datetime.now().strftime('%y년 %m월')
    ws['A1'] = f"{today} 재고조사 통합 집계본"
    ws['A1'].font = Font(bold=True, size=14, color="2E7D32")
    ws.merge_cells('A1:F1')

    ws['A2'] = f"Ver. 2 [{datetime.now().strftime('%y.%m.%d')}]"
    ws['A2'].font = Font(size=9, color="666666")

    # 헤더 행 (4행부터)
    headers = ['품번', '품명', '규격', '계정', '단위', '품목군명', 'TOTAL']
    for wh in warehouses:
        headers.append(wh.name)

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=4, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = thin_border

    # 데이터 행
    row_num = 5
    grand_total = 0
    warehouse_totals = {wh.code: 0 for wh in warehouses}

    for part_no, data in sorted(part_data.items()):
        part = data['part']
        ws.cell(row=row_num, column=1, value=part_no).border = thin_border
        ws.cell(row=row_num, column=2, value=data['part_name']).border = thin_border
        ws.cell(row=row_num, column=3, value=getattr(part, 'spec', '') or '' if part else '').border = thin_border
        ws.cell(row=row_num, column=4, value=getattr(part, 'account_type', '원재료') or '원재료' if part else '원재료').border = thin_border
        ws.cell(row=row_num, column=5, value=getattr(part, 'unit', 'EA') or 'EA' if part else 'EA').border = thin_border
        ws.cell(row=row_num, column=6, value=getattr(part, 'part_group', '') or '' if part else '').border = thin_border

        total_cell = ws.cell(row=row_num, column=7, value=data['total'])
        total_cell.border = thin_border
        total_cell.number_format = number_format
        total_cell.font = Font(bold=True)
        grand_total += data['total']

        for col_idx, wh in enumerate(warehouses, 8):
            qty = data['warehouses'].get(wh.code, 0)
            cell = ws.cell(row=row_num, column=col_idx, value=qty if qty > 0 else None)
            cell.border = thin_border
            if qty > 0:
                cell.number_format = number_format
                warehouse_totals[wh.code] += qty

        row_num += 1

    # 합계 행
    if part_data:
        ws.cell(row=row_num, column=1, value="합계").font = Font(bold=True)
        for col in range(1, 7):
            ws.cell(row=row_num, column=col).border = thin_border

        total_cell = ws.cell(row=row_num, column=7, value=grand_total)
        total_cell.border = thin_border
        total_cell.number_format = number_format
        total_cell.font = Font(bold=True)

        for col_idx, wh in enumerate(warehouses, 8):
            cell = ws.cell(row=row_num, column=col_idx, value=warehouse_totals[wh.code] or None)
            cell.border = thin_border
            if warehouse_totals[wh.code] > 0:
                cell.number_format = number_format
                cell.font = Font(bold=True)

    # 열 너비 조정
    ws.column_dimensions['A'].width = 15
    ws.column_dimensions['B'].width = 25
    ws.column_dimensions['C'].width = 18
    ws.column_dimensions['D'].width = 8
    ws.column_dimensions['E'].width = 6
    ws.column_dimensions['F'].width = 12
    ws.column_dimensions['G'].width = 12
    for i, wh in enumerate(warehouses, 8):
        ws.column_dimensions[get_column_letter(i)].width = 14

    # 응답 생성
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f"inventory_check_summary_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    wb.save(response)
    return response


# =============================================================================
# ERP 동기화 - 엑셀 변환 내보내기
# =============================================================================

@wms_permission_required('can_wms_stock_view')
def erp_sync(request):
    """
    ERP 동기화 페이지 - 입고/출고/이동 내역을 ERP 양식 엑셀로 변환
    """
    from datetime import datetime, timedelta

    # 기본 날짜 범위 (오늘)
    today = timezone.now().date()
    date_from = request.GET.get('date_from', today.strftime('%Y-%m-%d'))
    date_to = request.GET.get('date_to', today.strftime('%Y-%m-%d'))
    sync_type = request.GET.get('sync_type', 'IN')  # IN, OUT, TRANSFER

    # 날짜 파싱
    try:
        start_date = datetime.strptime(date_from, '%Y-%m-%d').date()
        end_date = datetime.strptime(date_to, '%Y-%m-%d').date()
    except ValueError:
        start_date = today
        end_date = today

    # 트랜잭션 유형별 필터링
    if sync_type == 'IN':
        type_filter = ['IN_SCM', 'IN_MANUAL']
        title = '입고 처리'
    elif sync_type == 'OUT':
        type_filter = ['OUT_PROD', 'OUT_RETURN']
        title = '출고 처리'
    else:  # TRANSFER
        type_filter = ['TRANSFER']
        title = '이동 처리'

    # 트랜잭션 조회
    transactions = MaterialTransaction.objects.filter(
        transaction_type__in=type_filter,
        date__date__gte=start_date,
        date__date__lte=end_date
    ).select_related('part', 'warehouse_from', 'warehouse_to', 'vendor', 'actor').order_by('-date')

    # 창고 목록 (이동처리용)
    warehouses = Warehouse.objects.filter(is_active=True).order_by('code')

    context = {
        'title': title,
        'sync_type': sync_type,
        'date_from': date_from,
        'date_to': date_to,
        'transactions': transactions,
        'warehouses': warehouses,
        'transaction_count': transactions.count(),
    }

    return render(request, 'material/erp_sync.html', context)


@wms_permission_required('can_wms_stock_view')
def erp_sync_export(request):
    """
    ERP 양식 엑셀 내보내기 - 더존 iCUBE 업로드 형식
    - 입고: 거래구분, 입고일자, 거래처코드, 환종, 환율, 과세구분, 단가구분, 창고코드, 품번, 입고수량, 재고단위수량, 장소코드
    """
    from datetime import datetime
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    sync_type = request.GET.get('sync_type', 'IN')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')

    # 날짜 파싱
    today = timezone.now().date()
    try:
        start_date = datetime.strptime(date_from, '%Y-%m-%d').date() if date_from else today
        end_date = datetime.strptime(date_to, '%Y-%m-%d').date() if date_to else today
    except ValueError:
        start_date = today
        end_date = today

    # 트랜잭션 유형별 필터링
    if sync_type == 'IN':
        type_filter = ['IN_SCM', 'IN_MANUAL']
        title = '입고등록'
    elif sync_type == 'OUT':
        type_filter = ['OUT_PROD', 'OUT_RETURN']
        title = '출고등록'
    else:  # TRANSFER
        type_filter = ['TRANSFER']
        title = '재고이동'

    # 트랜잭션 조회
    transactions = MaterialTransaction.objects.filter(
        transaction_type__in=type_filter,
        date__date__gte=start_date,
        date__date__lte=end_date
    ).select_related('part', 'warehouse_from', 'warehouse_to', 'vendor', 'actor').order_by('date')

    # 엑셀 생성
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'sheet1'

    # 스타일
    header_font = Font(bold=True, size=10)
    header_fill = PatternFill(start_color='DAEEF3', end_color='DAEEF3', fill_type='solid')
    code_fill = PatternFill(start_color='FDE9D9', end_color='FDE9D9', fill_type='solid')

    if sync_type == 'IN':
        # ========== 입고등록 ERP 양식 ==========
        # Row 1: 한글 헤더
        headers_kr = ['거래구분', '입고일자', '거래처코드', '환종', '환율', '과세구분', '단가구분', '창고코드', '품번', '입고수량', '재고단위수량', '장소코드']
        for col, header in enumerate(headers_kr, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center')

        # Row 2: ERP 코드
        headers_code = ['PO_FG', 'RCV_DT', 'TR_CD', 'EXCH_CD', 'EXCH_RT', 'VAT_FG', 'UMVAT_FG', 'WH_CD', 'ITEM_CD', 'PO_QT', 'RCV_QT', 'LC_CD']
        for col, code in enumerate(headers_code, 1):
            cell = ws.cell(row=2, column=col, value=code)
            cell.font = Font(size=9)
            cell.fill = code_fill
            cell.alignment = Alignment(horizontal='center')

        # Row 3+: 데이터
        row_num = 3
        for trx in transactions:
            part = trx.part
            vendor = trx.vendor

            row_data = [
                '0',  # 거래구분: 0=DOMESTIC
                trx.date.strftime('%Y%m%d'),  # 입고일자: YYYYMMDD
                vendor.erp_code if vendor and vendor.erp_code else '',  # 거래처코드 (ERP코드)
                'KRW',  # 환종
                1,  # 환율
                '0',  # 과세구분: 0=매입과세
                '0',  # 단가구분: 0=부가세미포함
                trx.warehouse_to.code if trx.warehouse_to else '',  # 창고코드
                part.part_no if part else '',  # 품번
                abs(trx.quantity),  # 입고수량
                abs(trx.quantity),  # 재고단위수량 (동일)
                trx.warehouse_to.code if trx.warehouse_to else '',  # 장소코드 = 창고코드
            ]

            for col, value in enumerate(row_data, 1):
                ws.cell(row=row_num, column=col, value=value)

            row_num += 1

        # 열 너비 조정
        col_widths = {'A': 10, 'B': 12, 'C': 12, 'D': 8, 'E': 8, 'F': 10, 'G': 10, 'H': 10, 'I': 20, 'J': 12, 'K': 14, 'L': 10}

    elif sync_type == 'OUT':
        # ========== 출고등록 (임시 - 출고 양식 확인 후 수정 필요) ==========
        headers_kr = ['품번', '품명', '수량', '출고창고', '출고일자', '용도', '비고']
        for col, header in enumerate(headers_kr, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill

        row_num = 2
        for trx in transactions:
            part = trx.part
            usage = '생산불출' if trx.transaction_type == 'OUT_PROD' else '반품출고'
            row_data = [
                part.part_no if part else '',
                part.part_name if part else '',
                abs(trx.quantity),
                trx.warehouse_from.code if trx.warehouse_from else '',
                trx.date.strftime('%Y%m%d'),
                usage,
                trx.remark or '',
            ]
            for col, value in enumerate(row_data, 1):
                ws.cell(row=row_num, column=col, value=value)
            row_num += 1

        col_widths = {'A': 20, 'B': 25, 'C': 10, 'D': 12, 'E': 12, 'F': 12, 'G': 20}

    else:  # TRANSFER
        # ========== 재고이동 (임시 - 이동 양식 확인 후 수정 필요) ==========
        headers_kr = ['품번', '품명', '수량', '출발창고', '도착창고', '이동일자', '비고']
        for col, header in enumerate(headers_kr, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill

        row_num = 2
        for trx in transactions:
            part = trx.part
            row_data = [
                part.part_no if part else '',
                part.part_name if part else '',
                abs(trx.quantity),
                trx.warehouse_from.code if trx.warehouse_from else '',
                trx.warehouse_to.code if trx.warehouse_to else '',
                trx.date.strftime('%Y%m%d'),
                trx.remark or '',
            ]
            for col, value in enumerate(row_data, 1):
                ws.cell(row=row_num, column=col, value=value)
            row_num += 1

        col_widths = {'A': 20, 'B': 25, 'C': 10, 'D': 12, 'E': 12, 'F': 12, 'G': 20}

    # 열 너비 적용
    for col, width in col_widths.items():
        ws.column_dimensions[col].width = width

    # 응답 생성
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    type_name = {'IN': 'incoming', 'OUT': 'outgoing', 'TRANSFER': 'transfer'}.get(sync_type, 'sync')
    filename = f"ERP_{title}_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    wb.save(response)
    return response


# =============================================================================
# 원재료 관리 - 창고 레이아웃, 입고, 랙 관리
# =============================================================================

from .models import RawMaterialRack, RawMaterialSetting, RawMaterialLabel


@wms_permission_required('can_wms_stock_view')
def raw_material_layout(request):
    """
    원재료 창고 레이아웃 - 실시간 재고 현황 표시
    실제 창고 구조 반영:
    - A열: 왼쪽 벽면 (2층/1층)
    - B열: 오른쪽 벽면 (2층/1층) - 마주보는 형태
    - 가운데: 통로
    """
    section = request.GET.get('section', '3F')

    # 해당 구역의 랙 목록 조회
    racks = RawMaterialRack.objects.filter(
        section=section,
        is_active=True
    ).select_related('part')

    def get_rack_info(rack):
        """랙별 재고 정보 조회"""
        stock_qty = 0
        stock_status = 'empty'

        if rack.part:
            stock = MaterialStock.objects.filter(
                part=rack.part,
                warehouse__code='3000'
            ).aggregate(total=Sum('quantity'))
            stock_qty = stock['total'] or 0

            try:
                setting = rack.part.raw_material_setting
                safety = setting.safety_stock
                warning = setting.warning_stock
            except RawMaterialSetting.DoesNotExist:
                safety = 0
                warning = 0

            if stock_qty <= 0:
                stock_status = 'empty'
            elif stock_qty < safety:
                stock_status = 'danger'
            elif stock_qty < warning:
                stock_status = 'warning'
            else:
                stock_status = 'safe'

        return {
            'rack': rack,
            'stock_qty': stock_qty,
            'stock_status': stock_status,
        }

    # A열, B열 분리 후 col_num 오름차순 정렬
    wall_a = {'1': [], '2': []}
    wall_b = {'1': [], '2': []}

    for rack in racks:
        rack_info = get_rack_info(rack)
        floor = str(rack.row_num)

        if rack.row_label.upper() == 'A':
            wall_a[floor].append(rack_info)
        elif rack.row_label.upper() == 'B':
            wall_b[floor].append(rack_info)

    # col_num 오름차순 정렬
    for floor in wall_a:
        wall_a[floor] = sorted(wall_a[floor], key=lambda x: x['rack'].col_num)
    for floor in wall_b:
        wall_b[floor] = sorted(wall_b[floor], key=lambda x: x['rack'].col_num)

    # 유효기간 경고 데이터 (사이드바용)
    today = timezone.now().date()
    from datetime import timedelta
    expiry_expired = RawMaterialLabel.objects.filter(
        expiry_date__isnull=False,
        expiry_date__lt=today,
        status__in=['INSTOCK', 'PRINTED']
    ).count()
    expiry_imminent = RawMaterialLabel.objects.filter(
        expiry_date__isnull=False,
        expiry_date__gte=today,
        expiry_date__lte=today + timedelta(days=30),
        status__in=['INSTOCK', 'PRINTED']
    ).count()
    expiry_warning = RawMaterialLabel.objects.filter(
        expiry_date__isnull=False,
        expiry_date__gt=today + timedelta(days=30),
        expiry_date__lte=today + timedelta(days=90),
        status__in=['INSTOCK', 'PRINTED']
    ).count()

    context = {
        'section': section,
        'section_display': '3공장' if section == '3F' else '2공장',
        'wall_a': wall_a,
        'wall_b': wall_b,
        'sections': RawMaterialRack.SECTION_CHOICES,
        'expiry_expired': expiry_expired,
        'expiry_imminent': expiry_imminent,
        'expiry_warning': expiry_warning,
    }

    return render(request, 'material/raw_material_layout.html', context)


@wms_permission_required('can_wms_stock_view')
def raw_material_expiry(request):
    """
    유효기간 관리 - 임박/경과 품목 모니터링
    """
    from datetime import timedelta

    today = timezone.now().date()
    filter_status = request.GET.get('status', 'all')
    stock_search = request.GET.get('search', '').strip()

    # 유효기간이 있는 재고 라벨만 조회
    base_qs = RawMaterialLabel.objects.filter(
        expiry_date__isnull=False,
        status__in=['INSTOCK', 'PRINTED']
    ).select_related('part', 'vendor').order_by('expiry_date')

    if stock_search:
        from django.db.models import Q as _Q
        base_qs = base_qs.filter(
            _Q(part_no__icontains=stock_search) | _Q(part_name__icontains=stock_search)
        )

    # 상태별 필터링
    if filter_status == 'expired':
        labels = base_qs.filter(expiry_date__lt=today)
    elif filter_status == 'imminent':
        labels = base_qs.filter(expiry_date__gte=today, expiry_date__lte=today + timedelta(days=30))
    elif filter_status == 'warning':
        labels = base_qs.filter(expiry_date__gt=today + timedelta(days=30), expiry_date__lte=today + timedelta(days=90))
    elif filter_status == 'safe':
        labels = base_qs.filter(expiry_date__gt=today + timedelta(days=90))
    else:
        labels = base_qs

    # D-day 계산
    for label in labels:
        delta = (label.expiry_date - today).days
        label.d_day = delta
        if delta < 0:
            label.expiry_status = 'expired'
        elif delta <= 30:
            label.expiry_status = 'imminent'
        elif delta <= 90:
            label.expiry_status = 'warning'
        else:
            label.expiry_status = 'safe'

    # 요약 카운트
    count_expired = base_qs.filter(expiry_date__lt=today).count()
    count_imminent = base_qs.filter(expiry_date__gte=today, expiry_date__lte=today + timedelta(days=30)).count()
    count_warning = base_qs.filter(expiry_date__gt=today + timedelta(days=30), expiry_date__lte=today + timedelta(days=90)).count()
    count_safe = base_qs.filter(expiry_date__gt=today + timedelta(days=90)).count()

    # 현장 투입 이력 (USED 라벨)
    from django.db.models import Q
    active_tab = request.GET.get('tab', 'stock')
    used_search = request.GET.get('used_search', '').strip()
    used_start = request.GET.get('used_start', '')
    used_end = request.GET.get('used_end', '')

    used_qs = RawMaterialLabel.objects.filter(
        status='USED',
        used_at__isnull=False,
    ).select_related('part', 'vendor', 'used_by').order_by('-used_at')

    if used_search:
        used_qs = used_qs.filter(
            Q(part_no__icontains=used_search) | Q(part_name__icontains=used_search)
        )
    if used_start:
        used_qs = used_qs.filter(used_at__date__gte=used_start)
    if used_end:
        used_qs = used_qs.filter(used_at__date__lte=used_end)

    # 투입 시점 잔여 D-DAY 계산 (고정값)
    for ul in used_qs:
        if ul.expiry_date and ul.used_at:
            ul.used_d_day = (ul.expiry_date - ul.used_at.date()).days
        else:
            ul.used_d_day = None

    context = {
        'labels': labels,
        'filter_status': filter_status,
        'today': today,
        'count_expired': count_expired,
        'count_imminent': count_imminent,
        'count_warning': count_warning,
        'count_safe': count_safe,
        'count_total': count_expired + count_imminent + count_warning + count_safe,
        'active_tab': active_tab,
        'used_labels': used_qs,
        'used_count': used_qs.count(),
        'used_search': used_search,
        'used_start': used_start,
        'used_end': used_end,
        'stock_search': stock_search,
    }

    return render(request, 'material/raw_material_expiry.html', context)


@wms_permission_required('can_wms_inout_view')
def raw_material_incoming(request):
    """
    원재료 입고 처리 - QR 라벨 발행 포함
    수입검사 OK 판정된 건에 대해서만 라벨 발행 가능
    각 라벨은 고유하며, 동일 입고 건에 대해 중복 발행 불가
    """
    from datetime import timedelta

    if request.method == 'POST':
        action = request.POST.get('action')

        # 라벨 취소 (삭제)
        if action == 'cancel_labels':
            inspection_id = request.POST.get('inspection_id')
            try:
                insp = ImportInspection.objects.get(id=inspection_id)
                trx = insp.inbound_transaction
                deleted_count = RawMaterialLabel.objects.filter(incoming_transaction=trx).delete()[0]
                if deleted_count > 0:
                    # 트랜잭션 remark에서 라벨 발행 기록 제거
                    remark = trx.remark or ''
                    import re
                    remark = re.sub(r'\s*\[라벨 \d+장 발행.*?\]', '', remark).strip()
                    trx.remark = remark
                    trx.save(update_fields=['remark'])
                    messages.success(request, f'{trx.part.part_no} - 라벨 {deleted_count}장 취소 완료')
                else:
                    messages.info(request, '취소할 라벨이 없습니다.')
            except ImportInspection.DoesNotExist:
                messages.error(request, '해당 수입검사 건을 찾을 수 없습니다.')
            except Exception as e:
                messages.error(request, f'라벨 취소 실패: {str(e)}')
            return redirect('material:raw_material_incoming')

        # 라벨 발행 처리 (수입검사 OK 건)
        if action == 'print_labels':
            inspection_id = request.POST.get('inspection_id')

            try:
                inspection = ImportInspection.objects.get(id=inspection_id)

                # 검사 상태 확인
                if inspection.status != 'APPROVED':
                    messages.error(request, '수입검사 합격 판정이 필요합니다.')
                    return redirect('material:raw_material_incoming')

                trx = inspection.inbound_transaction
                part = trx.part
                qty = float(trx.quantity)
                lot = trx.lot_no or timezone.now().date()
                vendor = trx.vendor

                # 이미 라벨이 발행된 건인지 확인 (중복 방지)
                existing_labels = RawMaterialLabel.objects.filter(incoming_transaction=trx)
                if existing_labels.exists():
                    messages.warning(request, f'이미 라벨이 발행된 건입니다. (발행 라벨: {existing_labels.count()}장)')
                    label_ids = ','.join([str(l.id) for l in existing_labels])
                    return redirect(f'/wms/raw-material/label-print/?ids={label_ids}')

                # 모달에서 입력받은 값
                unit = request.POST.get('unit', 'KG')
                pkg_qty = float(request.POST.get('pkg_qty', 25))
                pkg_count = int(request.POST.get('pkg_count', 1))

                if pkg_qty <= 0 or pkg_count <= 0:
                    messages.error(request, '포장 단위수량과 포장 수는 0보다 커야 합니다.')
                    return redirect('material:raw_material_incoming')

                # 유효기간 - 모달에서 직접 선택한 날짜 사용
                expiry_date_str = request.POST.get('expiry_date', '').strip()
                if expiry_date_str:
                    from datetime import datetime as _dt
                    try:
                        expiry_date = _dt.strptime(expiry_date_str, '%Y-%m-%d').date()
                    except ValueError:
                        expiry_date = None
                else:
                    expiry_date = None

                # 라벨 발행만 수행 (재고는 입고처리 시 이미 추가됨)
                with transaction.atomic():
                    labels = []

                    for i in range(pkg_count):
                        label = RawMaterialLabel.objects.create(
                            label_id=RawMaterialLabel.generate_label_id(),
                            part=part,
                            part_no=part.part_no,
                            part_name=part.part_name,
                            lot_no=lot,
                            quantity=pkg_qty,
                            unit=unit,
                            expiry_date=expiry_date,
                            incoming_transaction=trx,
                            vendor=vendor,
                            status='INSTOCK',
                            printed_by=request.user,
                        )
                        labels.append(label)

                    # 입고 트랜잭션에 라벨 발행 기록
                    trx.remark = f'{trx.remark or ""} [라벨 {pkg_count}장 발행 ({pkg_qty}{dict(RawMaterialLabel.UNIT_CHOICES).get(unit, unit)}×{pkg_count})]'.strip()
                    trx.save()

                messages.success(request, f'{part.part_no} - {pkg_count}장 라벨 발행 완료 ({pkg_qty}{dict(RawMaterialLabel.UNIT_CHOICES).get(unit, unit)}×{pkg_count})')

                # 라벨 출력 페이지로 리다이렉트
                label_ids = ','.join([str(l.id) for l in labels])
                return redirect(f'/wms/raw-material/label-print/?ids={label_ids}')

            except ImportInspection.DoesNotExist:
                messages.error(request, '해당 수입검사 건을 찾을 수 없습니다.')
            except Exception as e:
                messages.error(request, f'라벨 발행 실패: {str(e)}')

            return redirect('material:raw_material_incoming')

    # 수입검사 합격(APPROVED) 건 목록 조회 - 전체 품목
    approved_inspections = []
    if ImportInspection:
        approved_inspections = ImportInspection.objects.filter(
            status='APPROVED'
        ).select_related(
            'inbound_transaction',
            'inbound_transaction__part',
            'inbound_transaction__vendor'
        ).order_by('-inspected_at')

        # 이미 라벨 발행된 건 표시 + SCM/WMS 구분 + 품목설정 로딩
        for insp in approved_inspections:
            trx = insp.inbound_transaction
            insp.is_scm = (trx.transaction_type == 'IN_SCM')
            insp.label_count = RawMaterialLabel.objects.filter(
                incoming_transaction=trx
            ).count()
            # 품목설정 데이터 (모달 기본값용)
            try:
                setting = trx.part.raw_material_setting
                insp.setting_unit_weight = float(setting.unit_weight)
                insp.setting_shelf_life = setting.shelf_life_days
                insp.has_setting = True
            except RawMaterialSetting.DoesNotExist:
                insp.setting_unit_weight = 0
                insp.setting_shelf_life = 365
                insp.has_setting = False

    # 수입검사 대기중인 건 목록 - 전체 품목
    pending_inspections = []
    if ImportInspection:
        pending_inspections = ImportInspection.objects.filter(
            status='PENDING'
        ).select_related(
            'inbound_transaction',
            'inbound_transaction__part',
            'inbound_transaction__vendor'
        ).order_by('-inbound_transaction__date')[:10]

    # 최근 발행된 라벨 목록
    recent_labels = RawMaterialLabel.objects.select_related(
        'part', 'vendor'
    ).order_by('-printed_at')[:20]

    context = {
        'approved_inspections': approved_inspections,
        'pending_inspections': pending_inspections,
        'recent_labels': recent_labels,
        'today': timezone.now().date().strftime('%Y-%m-%d'),
    }

    return render(request, 'material/raw_material_incoming.html', context)


@wms_permission_required('can_wms_stock_view')
def raw_material_rack_manage(request):
    """
    랙 위치 관리 - 추가/수정/삭제, 품목 배치
    """
    section = request.GET.get('section', '3F')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add_rack':
            position_code = request.POST.get('position_code')
            row_label = request.POST.get('row_label')
            row_num = request.POST.get('row_num')
            col_num = request.POST.get('col_num')

            if position_code and row_label:
                # 중복 체크 (같은 구역 내에서만)
                if RawMaterialRack.objects.filter(section=section, position_code=position_code).exists():
                    messages.error(request, f'[{section}] 위치코드 {position_code}는 이미 존재합니다.')
                else:
                    RawMaterialRack.objects.create(
                        section=section,
                        position_code=position_code,
                        row_label=row_label,
                        row_num=int(row_num or 1),
                        col_num=int(col_num or 1),
                    )
                    messages.success(request, f'랙 위치 {position_code} 추가 완료')

        elif action == 'assign_part':
            rack_id = request.POST.get('rack_id')
            part_id = request.POST.get('part_id')

            rack = RawMaterialRack.objects.get(id=rack_id)
            rack.part_id = part_id if part_id else None
            rack.save()
            messages.success(request, f'{rack.position_code} 품목 배치 완료')

        elif action == 'swap_parts':
            # 랙 간 품목 교환
            source_rack_id = request.POST.get('source_rack_id')
            target_rack_id = request.POST.get('target_rack_id')

            source_rack = RawMaterialRack.objects.get(id=source_rack_id)
            target_rack = RawMaterialRack.objects.get(id=target_rack_id)

            # 두 랙의 품목을 교환
            source_part = source_rack.part
            target_part = target_rack.part

            source_rack.part = target_part
            target_rack.part = source_part

            source_rack.save()
            target_rack.save()

            messages.success(request, f'{source_rack.position_code} ↔ {target_rack.position_code} 품목 교환 완료')

        elif action == 'delete_rack':
            rack_id = request.POST.get('rack_id')
            rack = RawMaterialRack.objects.get(id=rack_id)
            position = rack.position_code
            rack.delete()
            messages.success(request, f'랙 위치 {position} 삭제 완료')

        return redirect(f'/wms/raw-material/rack-manage/?section={section}')

    racks = RawMaterialRack.objects.filter(section=section).select_related('part')
    parts = Part.objects.all().order_by('part_no')

    # 이미 랙에 배치된 품목 ID 목록
    assigned_parts = set(RawMaterialRack.objects.filter(part__isnull=False).values_list('part_id', flat=True))

    # A열, B열 분리 후 col_num 오름차순 정렬
    wall_a = {'1': [], '2': []}
    wall_b = {'1': [], '2': []}

    for rack in racks:
        floor = str(rack.row_num)
        if rack.row_label.upper() == 'A':
            wall_a[floor].append(rack)
        elif rack.row_label.upper() == 'B':
            wall_b[floor].append(rack)

    # col_num 오름차순 정렬
    for floor in wall_a:
        wall_a[floor] = sorted(wall_a[floor], key=lambda x: x.col_num)
    for floor in wall_b:
        wall_b[floor] = sorted(wall_b[floor], key=lambda x: x.col_num)

    # 각 층별 최대 col_num (다음 번호 계산용)
    max_col = {
        'A': {
            '1': max([r.col_num for r in wall_a['1']], default=0),
            '2': max([r.col_num for r in wall_a['2']], default=0),
        },
        'B': {
            '1': max([r.col_num for r in wall_b['1']], default=0),
            '2': max([r.col_num for r in wall_b['2']], default=0),
        }
    }

    context = {
        'section': section,
        'section_display': '3공장' if section == '3F' else '2공장',
        'wall_a': wall_a,
        'wall_b': wall_b,
        'max_col': max_col,
        'parts': parts,
        'assigned_parts': assigned_parts,
        'sections': RawMaterialRack.SECTION_CHOICES,
    }

    return render(request, 'material/raw_material_rack_manage.html', context)


@wms_permission_required('can_wms_stock_view')
def raw_material_setting(request):
    """
    원재료 품목 설정 - 안전재고, 보관기간 설정
    """
    if request.method == 'POST':
        part_id = request.POST.get('part_id')
        safety_stock = request.POST.get('safety_stock', 0)
        warning_stock = request.POST.get('warning_stock', 0)
        shelf_life_days = request.POST.get('shelf_life_days', 365)
        unit_weight = request.POST.get('unit_weight', 25)

        part = Part.objects.get(id=part_id)
        setting, created = RawMaterialSetting.objects.update_or_create(
            part=part,
            defaults={
                'safety_stock': int(safety_stock),
                'warning_stock': int(warning_stock),
                'shelf_life_days': int(shelf_life_days),
                'unit_weight': float(unit_weight),
            }
        )
        messages.success(request, f'{part.part_no} 설정 저장 완료')
        return redirect('/wms/raw-material/setting/')

    # 랙에 배치된 품목 목록
    rack_parts = RawMaterialRack.objects.filter(
        part__isnull=False
    ).values_list('part_id', flat=True).distinct()

    settings = RawMaterialSetting.objects.select_related('part').order_by('part__part_no')
    parts = Part.objects.filter(id__in=rack_parts).order_by('part_no')

    context = {
        'settings': settings,
        'parts': parts,
    }

    return render(request, 'material/raw_material_setting.html', context)


@wms_permission_required('can_wms_stock_view')
def api_raw_material_labels(request):
    """
    수입검사 건의 발행된 라벨 목록 조회 API
    """
    from django.http import JsonResponse

    inspection_id = request.GET.get('inspection_id')
    if not inspection_id:
        return JsonResponse({'labels': []})

    try:
        inspection = ImportInspection.objects.get(id=inspection_id)
        trx = inspection.inbound_transaction
        labels = RawMaterialLabel.objects.filter(incoming_transaction=trx).order_by('id')

        result = []
        for label in labels:
            result.append({
                'id': label.id,
                'label_id': label.label_id,
                'part_no': label.part_no,
                'quantity': float(label.quantity),
                'unit': label.get_unit_display(),
                'lot_no': label.lot_no.strftime('%Y-%m-%d') if label.lot_no else '-',
                'expiry_date': label.expiry_date.strftime('%Y-%m-%d') if label.expiry_date else None,
                'printed_at': label.printed_at.strftime('%m/%d %H:%M') if label.printed_at else '-',
            })

        return JsonResponse({
            'labels': result,
            'part_no': trx.part.part_no,
            'part_name': trx.part.part_name,
            'total_qty': float(trx.quantity),
        })
    except ImportInspection.DoesNotExist:
        return JsonResponse({'labels': []})


@wms_permission_required('can_wms_stock_view')
def raw_material_label_print(request):
    """
    QR 라벨 출력 화면
    """
    ids = request.GET.get('ids', '')
    label_ids = [int(i) for i in ids.split(',') if i.isdigit()]

    labels = RawMaterialLabel.objects.filter(id__in=label_ids).order_by('id')

    context = {
        'labels': labels,
    }

    return render(request, 'material/raw_material_label_print.html', context)


@wms_permission_required('can_wms_stock_view')
def api_part_search(request):
    """
    품목 검색 API - 품번/품명으로 검색
    """
    from django.http import JsonResponse
    from django.db.models import Q

    query = request.GET.get('q', '').strip()

    if len(query) < 2:
        return JsonResponse({'results': []})

    # 이미 랙에 배치된 품목 ID 목록
    assigned_parts = set(RawMaterialRack.objects.filter(
        part__isnull=False
    ).values_list('part_id', flat=True))

    # 품번 또는 품명으로 검색 (최대 30개)
    parts = Part.objects.filter(
        Q(part_no__icontains=query) | Q(part_name__icontains=query)
    ).order_by('part_no')[:30]

    results = []
    for part in parts:
        results.append({
            'id': part.id,
            'part_no': part.part_no,
            'part_name': part.part_name,
            'assigned': part.id in assigned_parts,
        })

    return JsonResponse({'results': results})


@wms_permission_required('can_wms_stock_view')
def api_labels_for_lot(request):
    """
    [API] 특정 품번+LOT의 사용 가능 라벨 목록 반환
    재고이동 시 라벨 선택용
    """
    part_no = (request.GET.get('part_no') or '').strip()
    lot_no_str = (request.GET.get('lot_no') or '').strip()

    if not part_no or not lot_no_str:
        return JsonResponse({'success': False, 'error': '품번과 LOT를 지정해주세요.'})

    try:
        from datetime import datetime as dt
        lot_date = dt.strptime(lot_no_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'success': False, 'error': 'LOT 형식이 올바르지 않습니다.'})

    labels = RawMaterialLabel.objects.filter(
        part_no=part_no,
        lot_no=lot_date,
        status__in=['INSTOCK', 'PRINTED']
    ).order_by('printed_at')

    result = []
    today = timezone.now().date()
    for lb in labels:
        d_day = (lb.expiry_date - today).days if lb.expiry_date else None
        result.append({
            'id': lb.id,
            'label_id': lb.label_id,
            'quantity': float(lb.quantity),
            'unit': lb.get_unit_display(),
            'expiry_date': lb.expiry_date.strftime('%Y-%m-%d') if lb.expiry_date else None,
            'd_day': d_day,
            'printed_at': lb.printed_at.strftime('%Y-%m-%d %H:%M') if lb.printed_at else None,
        })

    return JsonResponse({'success': True, 'labels': result, 'count': len(result)})


@wms_permission_required('can_wms_stock_view')
def api_transfer_detail(request, trx_id):
    """
    [API] 재고이동 트랜잭션 상세 + 연결된 라벨 목록 반환
    """
    try:
        trx = MaterialTransaction.objects.select_related(
            'part', 'warehouse_from', 'warehouse_to', 'actor'
        ).get(pk=trx_id, transaction_type='TRANSFER')
    except MaterialTransaction.DoesNotExist:
        return JsonResponse({'success': False, 'error': '이동 내역을 찾을 수 없습니다.'})

    # 연결된 라벨 (used_transaction FK)
    labels_data = []
    for lb in trx.used_labels.all().order_by('label_id'):
        used_d_day = None
        if lb.expiry_date and lb.used_at:
            used_d_day = (lb.expiry_date - lb.used_at.date()).days
        labels_data.append({
            'label_id': lb.label_id,
            'quantity': float(lb.quantity),
            'unit': lb.get_unit_display(),
            'expiry_date': lb.expiry_date.strftime('%Y-%m-%d') if lb.expiry_date else '-',
            'used_d_day': used_d_day,
        })

    return JsonResponse({
        'success': True,
        'trx': {
            'transaction_no': trx.transaction_no,
            'date': trx.date.strftime('%Y-%m-%d %H:%M'),
            'part_no': trx.part.part_no,
            'part_name': trx.part.part_name,
            'quantity': int(trx.quantity),
            'lot_no': trx.lot_no.strftime('%Y-%m-%d') if trx.lot_no else '-',
            'from_wh': f"({trx.warehouse_from.code}) {trx.warehouse_from.name}" if trx.warehouse_from else '-',
            'to_wh': f"({trx.warehouse_to.code}) {trx.warehouse_to.name}" if trx.warehouse_to else '-',
            'actor': trx.actor.username if trx.actor else '-',
            'remark': trx.remark or '',
        },
        'labels': labels_data,
        'label_count': len(labels_data),
    })