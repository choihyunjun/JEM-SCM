from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction, models
from django.utils import timezone
from django.http import HttpResponseForbidden
from django.views.decorators.http import require_POST
from functools import wraps

# ✅ [새 기능 필수] 검색/정렬(Case, When, Q) 및 페이징(Paginator)용
from django.db.models import F, Q, Case, When, Value, IntegerField
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger

# ✅ [모델 가져오기]
# 주의: M4 관련 모델(예: M4Request, Formal4M 등)이 있다면 여기에 콤마(,)로 추가해주셔야 M4 기능이 안 깨집니다!
from .models import ImportInspection 

# ✅ [새 기능 필수] 판정 시 재고 이동을 위해 필요 (없으면 판정 버튼 누를 때 에러남)
from material.models import Warehouse, MaterialStock, MaterialTransaction

# ✅ [새 기능 필수] 판정 시 SCM 연동을 위해 필요
from orders.models import Incoming, Inventory as ScmInventory, DeliveryOrder, DeliveryOrderItem, ReturnLog, Organization
from .models import (
    M4Request,
    M4Review,
    M4ChangeLog,
    Formal4MRequest,
    Formal4MDocumentItem,
    Formal4MAttachment,
    Formal4MInspectionResult,
    Formal4MScheduleItem,
    Formal4MStageRecord,
    Formal4MApproval,
    ImportInspection,
    # 신규 기능 모델
    VOC,
    VOCAttachment,
    Gauge,
    GaugeCalibration,
    QualityDocument,
    DocumentRevision,
    # ISIR 확장 모델
    ISIRAttachment,
    ISIRChecklist,
)
from .forms import (
    M4RequestForm,
    M4VendorResponseForm,
    Formal4MWorkflowForm,
    Formal4MInspectionResultUpdateForm,
    Formal4MScheduleItemUpdateForm,
    Formal4MStageRecordUpdateForm,
    Formal4MApprovalUpdateForm,
)
from .policies import (
    get_actor,
    scope_m4_queryset,
    scope_formal4m_queryset,
    can_view_m4,
    can_view_formal4m,
    can_edit_m4,
    can_add_internal_review,
    can_vendor_respond,
    get_or_create_vendor_review,
)

# Organization 데이터 동기화 유틸
from orders.services import ensure_org_and_profile_sync

# WMS 모델 (재고 이동용)
from material.models import Warehouse, MaterialStock, MaterialTransaction

# [신규] SCM 모델 (과부족/장부 연동용)
try:
    from orders.models import Incoming, Inventory as ScmInventory, DeliveryOrder, LabelPrintLog
except ImportError:
    pass


# =============================================================================
# QMS 권한 체크 데코레이터
# =============================================================================

def _get_profile(user):
    """UserProfile을 안전하게 가져온다"""
    try:
        return getattr(user, 'profile', None)
    except Exception:
        return None

def qms_permission_required(permission_field):
    """
    QMS 메뉴 권한 체크 데코레이터
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
                    'can_qms_4m_view': ['can_qms_4m'],
                    'can_qms_4m_edit': ['can_qms_4m'],
                    'can_qms_inspection_view': ['can_qms_inspection'],
                    'can_qms_inspection_edit': ['can_qms_inspection'],
                }
                for legacy_field in legacy_map.get(permission_field, []):
                    if getattr(profile, legacy_field, False):
                        return view_func(request, *args, **kwargs)

            messages.error(request, "해당 메뉴에 대한 접근 권한이 없습니다.")
            return redirect('qms:m4_list')
        return _wrapped_view
    return decorator


# 정식 4M 제출요구서류 템플릿(고정 19개)
FORMAL_4M_DOCS_TEMPLATE = [
    (1, "검사성적서"),
    (2, "검사지침서"),
    (3, "관리계획서"),
    (4, "작업표준서"),
    (5, "검사성적서(부품/공정)"),
    (6, "내구시험성적서"),
    (7, "납입증명/의뢰/승인서"),
    (8, "제품중량"),
    (9, "측정설비현황"),
    (10, "세부작업실행 현황"),
    (11, "도면"),
    (12, "중요치공정능력"),
    (13, "중금속 분석 성적서"),
    (14, "IMDS"),
    (15, "품질이력현황(해당부품)"),
    (16, "설계FMEA"),
    (17, "공정FMEA"),
    (18, "재고현황"),
    (19, "기타"),
]

def _formal4m_progress(formal: Formal4MRequest) -> dict:
    """정식 4M 진행률/완료조건 계산"""
    items = formal.doc_items.all().prefetch_related('attachments')
    req_items = [it for it in items if it.is_required]
    req_total = len(req_items)
    req_uploaded = sum(1 for it in req_items if it.attachments.all().exists())
    req_review_ok = sum(1 for it in req_items if it.review_status == 'OK')

    full = (formal.template_type == 'FULL')
    inspection_total = inspection_done = schedule_total = schedule_done = stages_total = stages_done = 0
    approval_done = False

    if full:
        ins = list(formal.inspection_results.all())
        inspection_total = len(ins)
        inspection_done = sum(1 for r in ins if (r.judgment or '').strip())

        sch = list(formal.schedule_items.all())
        schedule_total = sum(1 for s in sch if s.is_required)
        schedule_done = sum(1 for s in sch if s.is_required and s.plan_date)

        st = list(formal.stage_records.all())
        stages_total = len(st)
        stages_done = sum(1 for r in st if r.record_date)

        ap = getattr(formal, 'approval', None)
        approval_done = bool(ap and ap.is_approved)

    parts = []
    if req_total:
        parts.append(req_uploaded / req_total)
        parts.append(req_review_ok / req_total)
    if full:
        if inspection_total:
            parts.append(inspection_done / inspection_total)
        if schedule_total:
            parts.append(schedule_done / schedule_total)
        if stages_total:
            parts.append(stages_done / stages_total)
        parts.append(1.0 if approval_done else 0.0)

    percent = int(round((sum(parts) / len(parts)) * 100)) if parts else 0

    return {
        'required_total': req_total,
        'required_uploaded': req_uploaded,
        'required_review_ok': req_review_ok,
        'full': full,
        'inspection_total': inspection_total,
        'inspection_done': inspection_done,
        'schedule_total': schedule_total,
        'schedule_done': schedule_done,
        'stages_total': stages_total,
        'stages_done': stages_done,
        'approval_done': approval_done,
        'percent': percent,
    }

def _make_formal4m_no(pre: M4Request) -> str:
    """정식 4M 번호 생성"""
    base = pre.request_no or f"PRE-{pre.pk}"
    candidate = f"FORMAL-{base}"
    if not Formal4MRequest.objects.filter(formal_no=candidate).exists():
        return candidate
    return f"{candidate}-{timezone.now().strftime('%H%M%S')}"


def ensure_formal4m_full(formal: Formal4MRequest) -> None:
    """정식 4M을 FULL(확장) 양식 상태로 보장"""
    if formal.template_type != "FULL":
        formal.template_type = "FULL"
        formal.save(update_fields=["template_type"])

    if getattr(formal, "change_class", None) in (None, "",) and getattr(formal, "pre_request", None) and getattr(formal.pre_request, "change_class", None):
        formal.change_class = formal.pre_request.change_class
        formal.save(update_fields=["change_class"])

    Formal4MApproval.objects.get_or_create(formal_request=formal)

    for stage in ["ISIR", "OEM_APPROVAL", "INTERNAL_APPLY", "CUSTOMER_APPLY", "MASS_PRODUCTION_REVIEW", "CUSTOMER_NOTICE"]:
        Formal4MStageRecord.objects.get_or_create(formal_request=formal, stage=stage)

    existing = set(formal.schedule_items.values_list("item_name", flat=True))
    to_create = []
    for name in FORMAL_4M_SCHEDULE_TEMPLATE:
        if name not in existing:
            to_create.append(Formal4MScheduleItem(formal_request=formal, item_name=name))
    if to_create:
        Formal4MScheduleItem.objects.bulk_create(to_create)

    existing_ins = set(formal.inspection_results.values_list("inspection_item", flat=True))
    to_create_ins = []
    for name in FORMAL_4M_INSPECTION_TEMPLATE:
        if name not in existing_ins:
            to_create_ins.append(Formal4MInspectionResult(formal_request=formal, inspection_item=name))
    if to_create_ins:
        Formal4MInspectionResult.objects.bulk_create(to_create_ins)

def get_or_create_formal4m(pre: M4Request) -> Formal4MRequest:
    """사전 4M 승인 완료 문서에 대해 정식 4M을 생성/조회."""
    formal = getattr(pre, "formal_4m", None)
    if formal:
        ensure_formal4m_full(formal)
        return formal

    with transaction.atomic():
        formal = (
            Formal4MRequest.objects.select_for_update()
            .filter(pre_request=pre)
            .first()
        )
        if formal:
            ensure_formal4m_full(formal)
            return formal

        formal = Formal4MRequest.objects.create(
            pre_request=pre,
            formal_no=_make_formal4m_no(pre),
        )
        Formal4MDocumentItem.objects.bulk_create(
            [
                Formal4MDocumentItem(formal=formal, seq=seq, name=name, is_required=True)
                for (seq, name) in FORMAL_4M_DOCS_TEMPLATE
            ]
        )
        ensure_formal4m_full(formal)
        return formal


@qms_permission_required('can_qms_4m_view')
def m4_list(request):
    actor = get_actor(request.user)
    requests = scope_m4_queryset(actor, M4Request.objects.all()).order_by("-created_at")

    status_filter = request.GET.get("status")
    if status_filter:
        requests = requests.filter(status=status_filter)

    query = request.GET.get("q")
    if query:
        requests = requests.filter(
            models.Q(part_no__icontains=query) |
            models.Q(part_name__icontains=query)
        )

    context = {
        "requests": requests,
        "actor": actor,
        "status_filter": status_filter,
        "query": query,
    }
    return render(request, "qms/m4_list.html", context)


@qms_permission_required('can_qms_4m_view')
def m4_detail(request, pk):
    actor = get_actor(request.user)
    item = get_object_or_404(M4Request, pk=pk)
    if not can_view_m4(actor, item):
        return HttpResponseForbidden("접근 권한이 없습니다.")

    vendor_review = None
    vendor_form = None
    if can_vendor_respond(actor, item):
        vendor_review = get_or_create_vendor_review(actor, item)
        vendor_form = M4VendorResponseForm(instance=vendor_review)

    formal = None
    if item.status == "APPROVED":
        formal = Formal4MRequest.objects.filter(pre_request=item).first()

    return render(
        request,
        "qms/m4_detail.html",
        {
            "item": item,
            "actor": actor,
            "vendor_review": vendor_review,
            "vendor_form": vendor_form,
            "formal": formal,
        },
    )


@qms_permission_required('can_qms_4m_edit')
def m4_create(request):
    actor = get_actor(request.user)
    if actor.is_vendor:
        return HttpResponseForbidden("협력사 계정은 사전 4M 신규 작성 권한이 없습니다.")

    # 권한 체크: can_qms_4m_edit 권한 필요
    if not request.user.is_superuser:
        profile = getattr(request.user, 'profile', None)
        if not profile or not getattr(profile, 'can_qms_4m_edit', False):
            return HttpResponseForbidden("사전 4M 신규 작성 권한이 없습니다.")

    ensure_org_and_profile_sync()

    if request.method == "POST":
        form = M4RequestForm(request.POST, request.FILES, user=request.user)
        if "request_no" in form.errors:
            del form.errors["request_no"]

        if form.is_valid():
            m4_instance = form.save(commit=False)
            m4_instance.user = request.user
            m4_instance.status = "DRAFT"

            if not m4_instance.request_no:
                today_str = datetime.date.today().strftime("%Y%m%d")
                category = request.POST.get("quality_rank", "미분류")
                prefix = f"4M-{category}-{today_str}"
                last_entry = (
                    M4Request.objects.filter(request_no__startswith=prefix)
                    .order_by("request_no")
                    .last()
                )

                if last_entry:
                    try:
                        last_no = int(last_entry.request_no.split("-")[-1])
                        new_no = f"{prefix}-{str(last_no + 1).zfill(2)}"
                    except (ValueError, IndexError):
                        new_no = f"{prefix}-01"
                else:
                    new_no = f"{prefix}-01"
                m4_instance.request_no = new_no

            m4_instance.save()
            messages.success(request, f"기안이 저장되었습니다. (번호: {m4_instance.request_no})")
            return redirect("qms:m4_detail", pk=m4_instance.pk)
        else:
            messages.error(request, f"등록 실패: {form.errors.as_text()}")
    else:
        form = M4RequestForm(user=request.user)
    return render(request, "qms/m4_form.html", {"form": form, "mode": "create"})


@login_required
def m4_submit(request, pk):
    if request.method == "POST":
        item = get_object_or_404(M4Request, pk=pk)
        if item.user == request.user and item.status == "DRAFT":
            if getattr(item, "reviewer_user_id", None):
                item.status = "PENDING_REVIEW"
            elif getattr(item, "reviewer_user2_id", None):
                item.status = "PENDING_REVIEW2"
            else:
                item.status = "PENDING_APPROVE"

            item.is_submitted = True
            item.submitted_at = timezone.now()
            item.save()
            messages.success(request, "성공적으로 상신되었습니다.")
        else:
            messages.error(request, "상신 권한이 없거나 이미 처리된 문서입니다.")
    return redirect("qms:m4_detail", pk=pk)


@login_required
def m4_update(request, pk):
    actor = get_actor(request.user)
    item = get_object_or_404(M4Request, pk=pk)
    if not can_edit_m4(actor, item):
        messages.error(request, "수정 권한이 없습니다.")
        return redirect("qms:m4_detail", pk=pk)

    if request.method == "POST":
        form = M4RequestForm(request.POST, request.FILES, instance=item, user=request.user)
        if "request_no" in form.errors:
            del form.errors["request_no"]

        if form.is_valid():
            changed_fields = form.changed_data
            if changed_fields:
                old_instance = M4Request.objects.get(pk=pk)
                for field in changed_fields:
                    try:
                        old_val = getattr(old_instance, field)
                        new_val = form.cleaned_data.get(field)
                        if str(old_val) != str(new_val):
                            M4ChangeLog.objects.create(
                                request=item,
                                user=request.user,
                                field_name=item._meta.get_field(field).verbose_name,
                                old_value=str(old_val) if old_val else "내용 없음",
                                new_value=str(new_val) if new_val else "내용 없음",
                            )
                    except Exception:
                        continue
            form.save()
            messages.success(request, "수정 이력이 기록되었습니다.")
            return redirect("qms:m4_detail", pk=pk)
    else:
        form = M4RequestForm(instance=item, user=request.user)
    return render(request, "qms/m4_form.html", {"form": form, "item": item, "mode": "update"})


@login_required
def m4_approve(request, pk):
    if request.method == "POST":
        item = get_object_or_404(M4Request, pk=pk)

        if item.status == "PENDING_REVIEW" and request.user == item.reviewer_user:
            item.reviewed_at = timezone.now()
            item.is_reviewed = True
            if getattr(item, "reviewer_user2_id", None):
                item.status = "PENDING_REVIEW2"
            else:
                item.status = "PENDING_APPROVE"
            item.save()
            messages.success(request, "검토 승인되었습니다.")

        elif item.status == "PENDING_REVIEW2" and request.user == getattr(item, "reviewer_user2", None):
            item.reviewed2_at = timezone.now()
            item.is_reviewed2 = True
            item.status = "PENDING_APPROVE"
            item.save()
            messages.success(request, "검토2 승인되었습니다.")

        elif item.status == "PENDING_APPROVE" and request.user == item.approver_user:
            item.status = "APPROVED"
            item.approved_at = timezone.now()
            item.is_approved = True
            item.save()
            try:
                get_or_create_formal4m(item)
                messages.success(request, "최종 승인되었습니다. (정식 4M 생성 완료)")
            except Exception:
                messages.warning(request, "최종 승인되었습니다. (정식 4M 생성은 실패했습니다)")

    return redirect("qms:m4_detail", pk=pk)


# =========================
# 정식 4M (좌측 메뉴: 목록/상세)
# =========================

@qms_permission_required('can_qms_4m_view')
def formal4m_list(request):
    actor = get_actor(request.user)
    qs = Formal4MRequest.objects.select_related("pre_request").order_by("-created_at")
    qs = scope_formal4m_queryset(actor, qs)
    return render(request, "qms/formal4m_list.html", {"actor": actor, "items": qs})


@qms_permission_required('can_qms_4m_view')
def formal4m_detail_by_id(request, formal_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(
        Formal4MRequest.objects.select_related("pre_request"),
        pk=formal_id,
    )
    if not can_view_formal4m(actor, formal):
        return HttpResponseForbidden("접근 권한이 없습니다.")

    if formal.template_type != "FULL":
        with transaction.atomic():
            ensure_formal4m_full(formal)
            formal.refresh_from_db()

    items = formal.doc_items.all().prefetch_related("attachments").order_by("seq")

    inspection_results = formal.inspection_results.all().order_by("id")
    schedule_items = formal.schedule_items.all().order_by("id")
    stage_records = formal.stage_records.all().order_by("stage")
    approval = getattr(formal, "approval", None)

    progress = _formal4m_progress(formal)
    inspection_rows = [(r, Formal4MInspectionResultUpdateForm(instance=r)) for r in inspection_results]
    schedule_rows = [(s, Formal4MScheduleItemUpdateForm(instance=s)) for s in schedule_items]
    stage_rows = [(st, Formal4MStageRecordUpdateForm(instance=st)) for st in stage_records]
    approval_form = Formal4MApprovalUpdateForm(instance=approval) if approval else None
    workflow_form = Formal4MWorkflowForm(instance=formal) if actor.is_internal else None

    return render(
        request,
        "qms/formal4m_detail.html",
        {
            "actor": actor,
            "formal": formal,
            "pre": formal.pre_request,
            "items": items,
            "doc_items": items,
            "inspection_results": inspection_results,
            "schedule_items": schedule_items,
            "stage_records": stage_records,
            "approval": approval,
            "progress": progress,
            "inspection_rows": inspection_rows,
            "schedule_rows": schedule_rows,
            "stage_rows": stage_rows,
            "approval_form": approval_form,
            "workflow_form": workflow_form,
        },
    )


@qms_permission_required('can_qms_4m_view')
def formal4m_detail(request, pk):
    actor = get_actor(request.user)
    pre = get_object_or_404(M4Request, pk=pk)
    if not can_view_m4(actor, pre):
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if pre.status != "APPROVED":
        messages.error(request, "정식 4M은 사전 4M 승인 완료 후에만 열람할 수 있습니다.")
        return redirect("qms:m4_detail", pk=pk)

    formal = get_or_create_formal4m(pre)
    return redirect("qms:formal4m_detail_by_id", formal_id=formal.id)


def _formal4m_next_status_on_submit(formal: Formal4MRequest) -> str:
    if getattr(formal, "approval_reviewer_user_id", None):
        return "PENDING_REVIEW"
    if getattr(formal, "approval_reviewer_user2_id", None):
        return "PENDING_REVIEW2"
    return "PENDING_APPROVE"


@login_required
def formal4m_workflow_set(request, formal_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal) or not actor.is_internal:
        return HttpResponseForbidden("접근 권한이 없습니다.")

    if request.method != "POST":
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    if formal.approval_status == "APPROVED":
        messages.error(request, "승인 완료된 문서는 결재선을 변경할 수 없습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    form = Formal4MWorkflowForm(request.POST, instance=formal)
    if form.is_valid():
        form.save()
        messages.success(request, "정식 4M 결재선이 저장되었습니다.")
    else:
        messages.error(request, f"결재선 저장 실패: {form.errors.as_text()}")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)


@login_required
def formal4m_workflow_submit(request, formal_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal):
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if request.method != "POST":
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    progress = _formal4m_progress(formal)
    if progress['required_review_ok'] < progress['required_total']:
        messages.warning(request, f"주의: 필수 서류 {progress['required_total']}건 중 {progress['required_review_ok']}건만 검토 완료된 상태로 상신되었습니다.")

    if formal.pre_request.user_id != request.user.id:
        messages.error(request, "상신 권한이 없습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    if formal.approval_status != "DRAFT":
        messages.error(request, "작성중 상태에서만 상신할 수 있습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    if not getattr(formal, "approval_approver_user_id", None):
        messages.error(request, "최종 승인자를 지정해야 상신할 수 있습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    with transaction.atomic():
        formal.approval_status = _formal4m_next_status_on_submit(formal)
        formal.approval_is_submitted = True
        formal.approval_submitted_at = timezone.now()
        formal.approval_reject_reason = ""

        formal.approval_is_reviewed = False
        formal.approval_reviewed_at = None
        formal.approval_is_reviewed2 = False
        formal.approval_reviewed2_at = None
        formal.approval_is_approved = False
        formal.approval_approved_at = None

        formal.save()

    messages.success(request, "정식 4M이 상신되었습니다.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)


@login_required
def formal4m_workflow_approve(request, formal_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal) or not actor.is_internal:
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if request.method != "POST":
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    with transaction.atomic():
        if formal.approval_status == "PENDING_REVIEW" and request.user == formal.approval_reviewer_user:
            formal.approval_is_reviewed = True
            formal.approval_reviewed_at = timezone.now()

            if getattr(formal, "approval_reviewer_user2_id", None):
                formal.approval_status = "PENDING_REVIEW2"
            else:
                formal.approval_status = "PENDING_APPROVE"

            formal.save()
            messages.success(request, "검토1 승인되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

        if formal.approval_status == "PENDING_REVIEW2" and request.user == formal.approval_reviewer_user2:
            formal.approval_is_reviewed2 = True
            formal.approval_reviewed2_at = timezone.now()
            formal.approval_status = "PENDING_APPROVE"
            formal.save()
            messages.success(request, "검토2 승인되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

        if formal.approval_status == "PENDING_APPROVE" and request.user == formal.approval_approver_user:
            formal.approval_is_approved = True
            formal.approval_approved_at = timezone.now()
            formal.approval_status = "APPROVED"
            
            if not formal.validity_start_date:
                formal.validity_start_date = timezone.localdate()
                
            formal.save()
            messages.success(request, "정식 4M 최종 승인되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    messages.error(request, "승인 권한이 없거나 처리할 수 없는 상태입니다.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)


@login_required
def formal4m_workflow_reject(request, formal_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal) or not actor.is_internal:
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if request.method != "POST":
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    reason = (request.POST.get("reject_reason") or "").strip()

    allowed = False
    if formal.approval_status == "PENDING_REVIEW" and request.user == formal.approval_reviewer_user:
        allowed = True
    elif formal.approval_status == "PENDING_REVIEW2" and request.user == formal.approval_reviewer_user2:
        allowed = True
    elif formal.approval_status == "PENDING_APPROVE" and request.user == formal.approval_approver_user:
        allowed = True

    if not allowed:
        messages.error(request, "반려 권한이 없거나 처리할 수 없는 상태입니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    with transaction.atomic():
        formal.approval_status = "REJECTED"
        formal.approval_reject_reason = reason
        formal.save(update_fields=["approval_status", "approval_reject_reason"])

    messages.warning(request, "반려 처리되었습니다.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)


@login_required
def formal4m_workflow_resubmit(request, formal_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal):
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if request.method != "POST":
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    if formal.pre_request.user_id != request.user.id:
        messages.error(request, "재상신 권한이 없습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    if formal.approval_status != "REJECTED":
        messages.error(request, "반려 상태에서만 재상신할 수 있습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    if not getattr(formal, "approval_approver_user_id", None):
        messages.error(request, "최종 승인자를 지정해야 재상신할 수 있습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    with transaction.atomic():
        formal.approval_status = _formal4m_next_status_on_submit(formal)
        formal.approval_is_submitted = True
        formal.approval_submitted_at = timezone.now()
        formal.approval_reject_reason = ""

        formal.approval_is_reviewed = False
        formal.approval_reviewed_at = None
        formal.approval_is_reviewed2 = False
        formal.approval_reviewed2_at = None
        formal.approval_is_approved = False
        formal.approval_approved_at = None

        formal.save()

    messages.success(request, "정식 4M이 다시 상신되었습니다.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)


@login_required
def formal4m_workflow_cancel(request, formal_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal) or not actor.is_internal:
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if request.method != "POST":
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    with transaction.atomic():
        if formal.approval_status == "PENDING_REVIEW" and request.user.id == formal.pre_request.user_id:
            formal.approval_status = "DRAFT"
            formal.approval_is_submitted = False
            formal.approval_submitted_at = None
            formal.save()
            messages.info(request, "상신이 취소되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

        elif formal.approval_status == "PENDING_REVIEW2" and request.user == formal.approval_reviewer_user:
            formal.approval_status = "PENDING_REVIEW"
            formal.approval_is_reviewed = False
            formal.approval_reviewed_at = None
            formal.approval_is_reviewed2 = False
            formal.approval_reviewed2_at = None
            formal.save()
            messages.info(request, "검토1 승인이 취소되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

        elif formal.approval_status == "PENDING_APPROVE" and request.user == formal.approval_reviewer_user2:
            formal.approval_status = "PENDING_REVIEW2"
            if hasattr(formal, "approval_is_reviewed2"):
                formal.approval_is_reviewed2 = False
            if hasattr(formal, "approval_reviewed2_at"):
                formal.approval_reviewed2_at = None
            formal.save()
            messages.info(request, "검토2 승인이 취소되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

        elif (
            formal.approval_status == "PENDING_APPROVE"
            and request.user == formal.approval_reviewer_user
            and not getattr(formal, "approval_reviewer_user2_id", None)
        ):
            formal.approval_status = "PENDING_REVIEW"
            formal.approval_is_reviewed = False
            formal.approval_reviewed_at = None
            formal.save()
            messages.info(request, "검토1 승인이 취소되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

        elif formal.approval_status == "APPROVED" and request.user == formal.approval_approver_user:
            formal.approval_status = "PENDING_APPROVE"
            formal.approval_is_approved = False
            formal.approval_approved_at = None
            formal.save()
            messages.info(request, "최종 승인이 취소되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    messages.error(request, "취소 권한이 없거나 처리할 수 없는 상태입니다.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)



@login_required
def formal4m_set_validity_start(request, formal_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not actor.is_internal:
        return HttpResponseForbidden("접근 권한이 없습니다.")

    if request.method != "POST":
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    date_str = (request.POST.get("validity_start_date") or "").strip()
    if not date_str:
        formal.validity_start_date = None
        formal.save(update_fields=["validity_start_date"])
        messages.success(request, "유효성평가 시작일이 초기화되었습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    try:
        formal.validity_start_date = datetime.date.fromisoformat(date_str)
    except ValueError:
        messages.error(request, "날짜 형식이 올바르지 않습니다. (YYYY-MM-DD)")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    formal.save(update_fields=["validity_start_date"])
    messages.success(request, "유효성평가 시작일이 저장되었습니다.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)


FORMAL_4M_SCHEDULE_TEMPLATE = [
    "고객의뢰",
    "고객승인",
    "협력사적용",
    "사내적용",
    "고객적용",
]


FORMAL_4M_INSPECTION_TEMPLATE = [
    "치수 검사",
    "외관 검사",
    "기능/성능 검사",
    "포장/라벨 검사",
    "성적서/측정데이터",
]


@login_required
def formal4m_upgrade_to_full(request, formal_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(
        Formal4MRequest.objects.select_related("pre_request"),
        pk=formal_id,
    )
    if not actor.is_internal:
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if request.method != "POST":
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    if formal.template_type == "FULL":
        messages.info(request, "이미 확장 양식입니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    with transaction.atomic():
        formal.template_type = "FULL"
        formal.save(update_fields=["template_type"])

    if getattr(formal, "change_class", None) in (None, "",) and getattr(formal, "pre_request", None) and getattr(formal.pre_request, "change_class", None):
        formal.change_class = formal.pre_request.change_class
        formal.save(update_fields=["change_class"])

        Formal4MApproval.objects.get_or_create(formal_request=formal)

        for stage in ["ISIR", "OEM_APPROVAL", "INTERNAL_APPLY", "CUSTOMER_APPLY", "MASS_PRODUCTION_REVIEW", "CUSTOMER_NOTICE"]:
            Formal4MStageRecord.objects.get_or_create(formal_request=formal, stage=stage)

        existing = set(formal.schedule_items.values_list("item_name", flat=True))
        to_create = []
        for name in FORMAL_4M_SCHEDULE_TEMPLATE:
            if name not in existing:
                to_create.append(Formal4MScheduleItem(formal_request=formal, item_name=name))
        if to_create:
            Formal4MScheduleItem.objects.bulk_create(to_create)

        existing_ins = set(formal.inspection_results.values_list("inspection_item", flat=True))
        to_create_ins = []
        for name in FORMAL_4M_INSPECTION_TEMPLATE:
            if name not in existing_ins:
                to_create_ins.append(Formal4MInspectionResult(formal_request=formal, inspection_item=name))
        if to_create_ins:
            Formal4MInspectionResult.objects.bulk_create(to_create_ins)

    messages.success(request, "정식 4M이 확장 양식으로 전환되었습니다.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)


@login_required
def formal4m_inspection_update(request, formal_id: int, row_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal):
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if not actor.is_internal:
        return HttpResponseForbidden("내부 사용자만 수정할 수 있습니다.")
    
    if formal.approval_status in ["PENDING_APPROVE", "APPROVED"]:
        messages.error(request, "최종 승인 단계에 진입한 문서는 수정할 수 없습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    row = get_object_or_404(Formal4MInspectionResult, pk=row_id, formal_request=formal)
    if request.method == "POST":
        form = Formal4MInspectionResultUpdateForm(request.POST, request.FILES, instance=row)
        if form.is_valid():
            form.save()
            messages.success(request, "검토 결과가 저장되었습니다.")
        else:
            messages.error(request, "입력값을 확인해주세요.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal.id)


@login_required
def formal4m_schedule_update(request, formal_id: int, row_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal):
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if not actor.is_internal:
        return HttpResponseForbidden("내부 사용자만 수정할 수 있습니다.")
        
    if formal.approval_status in ["PENDING_APPROVE", "APPROVED"]:
        messages.error(request, "최종 승인 단계에 진입한 문서는 수정할 수 없습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    row = get_object_or_404(Formal4MScheduleItem, pk=row_id, formal_request=formal)
    if request.method == "POST":
        form = Formal4MScheduleItemUpdateForm(request.POST, instance=row)
        if form.is_valid():
            form.save()
            messages.success(request, "일정 항목이 저장되었습니다.")
        else:
            messages.error(request, "입력값을 확인해주세요.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal.id)


@login_required
def formal4m_stage_update(request, formal_id: int, row_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal):
        return HttpResponseForbidden("접근 권한이 없습니다.")

    if formal.approval_status == "APPROVED":
        messages.error(request, "최종 승인 완료된 문서는 수정할 수 없습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    row = get_object_or_404(Formal4MStageRecord, pk=row_id, formal_request=formal)
    if request.method == "POST":
        form = Formal4MStageRecordUpdateForm(request.POST, request.FILES, instance=row)
        if form.is_valid():
            form.save()
            messages.success(request, "단계 기록이 저장되었습니다.")
        else:
            messages.error(request, "입력값을 확인해주세요.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal.id)


@login_required
def formal4m_approval_update(request, formal_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal):
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if not actor.is_internal:
        return HttpResponseForbidden("내부 사용자만 수정할 수 있습니다.")

    approval, _ = Formal4MApproval.objects.get_or_create(formal_request=formal)
    if request.method == "POST":
        form = Formal4MApprovalUpdateForm(request.POST, instance=approval)
        if form.is_valid():
            form.save()
            messages.success(request, "사내 승인 정보가 저장되었습니다.")
        else:
            messages.error(request, "입력값을 확인해주세요.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal.id)


@login_required
def formal4m_inspection_add(request, formal_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal): return HttpResponseForbidden("접근 불가.")
    if not actor.is_internal: return HttpResponseForbidden("내부 전용.")
    
    if formal.approval_status in ["PENDING_APPROVE", "APPROVED"]:
        messages.error(request, "승인 진행 중에는 항목을 추가할 수 없습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    if request.method == "POST":
        inspection_item = (request.POST.get("inspection_item") or "").strip()
        if inspection_item:
            Formal4MInspectionResult.objects.create(formal_request=formal, inspection_item=inspection_item)
            messages.success(request, f"검토 항목 '{inspection_item}' 추가됨.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal.id)


@login_required
def formal4m_schedule_add(request, formal_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal): return HttpResponseForbidden("접근 불가.")
    if not actor.is_internal: return HttpResponseForbidden("내부 전용.")
    
    if formal.approval_status in ["PENDING_APPROVE", "APPROVED"]:
        messages.error(request, "승인 진행 중에는 항목을 추가할 수 없습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    if request.method == "POST":
        item_name = (request.POST.get("item_name") or "").strip()
        if item_name:
            Formal4MScheduleItem.objects.create(formal_request=formal, item_name=item_name, is_required=True)
            messages.success(request, f"일정 항목 '{item_name}' 추가됨.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal.id)


@login_required
def formal4m_upload(request, formal_id, item_id):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if formal.approval_status in ["PENDING_APPROVE", "APPROVED"]:
        messages.error(request, "승인 단계 진입 후 파일 업로드 불가.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal.id)

    pre = formal.pre_request
    if not can_view_m4(actor, pre): return HttpResponseForbidden("접근 불가.")
    item = get_object_or_404(Formal4MDocumentItem, pk=item_id, formal=formal)
    if request.method == "POST":
        f = request.FILES.get("file")
        if not f: messages.error(request, "파일 선택 필요.")
        else: Formal4MAttachment.objects.create(item=item, file=f, uploaded_by=request.user); messages.success(request, "업로드됨.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal.id)


@login_required
def formal4m_review_update(request, formal_id, item_id):
    actor = get_actor(request.user)
    if not actor.is_internal: return HttpResponseForbidden("내부 전용.")
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if formal.approval_status in ["PENDING_APPROVE", "APPROVED"]:
        messages.error(request, "승인 단계 진입 후 검토 수정 불가.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal.id)

    item = get_object_or_404(Formal4MDocumentItem, pk=item_id, formal=formal)
    if request.method == "POST":
        s = request.POST.get("review_status"); r = request.POST.get("remark")
        if s in {"PENDING", "OK", "REJECT"}: item.review_status = s
        item.remark = r; item.save(); messages.success(request, "검토 상태 저장됨.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal.id)


@login_required
def add_m4_review(request, pk):
    actor = get_actor(request.user)
    if request.method == "POST":
        m4_request = get_object_or_404(M4Request, pk=pk)
        if not can_view_m4(actor, m4_request): return HttpResponseForbidden("접근 불가.")
        if not can_add_internal_review(actor): return HttpResponseForbidden("내부 전용.")
        M4Review.objects.create(request=m4_request, department=request.POST.get("department"), reviewer_name=request.POST.get("reviewer_name"), request_content=request.POST.get("request_content"), reviewer=request.user, sent_at=timezone.now())
        messages.success(request, "검토 요청 등록됨.")
    return redirect("qms:m4_detail", pk=pk)

@login_required
def edit_m4_review(request, review_id):
    review = get_object_or_404(M4Review, id=review_id); actor = get_actor(request.user)
    can_post = actor.is_internal and ((review.reviewer_id == request.user.id) or request.user.is_staff)
    if request.method == "POST" and can_post:
        review.content = request.POST.get("review_content"); review.received_at = timezone.now(); review.save(); messages.success(request, "의견 저장됨.")
    return redirect("qms:m4_detail", pk=review.request.pk)

@login_required
def delete_m4_review(request, review_id):
    review = get_object_or_404(M4Review, id=review_id); actor = get_actor(request.user); pk = review.request.pk
    if actor.is_internal and (review.reviewer_id == request.user.id or request.user.is_staff): review.delete(); messages.success(request, "삭제됨.")
    return redirect("qms:m4_detail", pk=pk)

@login_required
def m4_vendor_response(request, pk):
    actor = get_actor(request.user); item = get_object_or_404(M4Request, pk=pk)
    if not can_vendor_respond(actor, item): return HttpResponseForbidden("권한 없음.")
    review = get_or_create_vendor_review(actor, item)
    if request.method == "POST":
        form = M4VendorResponseForm(request.POST, request.FILES, instance=review)
        if form.is_valid(): updated = form.save(commit=False); updated.reviewer, updated.received_at = request.user, timezone.now(); updated.save(); messages.success(request, "답변 등록됨.")
    return redirect("qms:m4_detail", pk=pk)

@login_required
def m4_reject(request, pk):
    if request.method == "POST":
        item = get_object_or_404(M4Request, pk=pk)
        if request.user in (item.reviewer_user, getattr(item, "reviewer_user2", None), item.approver_user):
            item.status = "REJECTED"; reason = request.POST.get("reject_reason", "")
            if hasattr(item, "reject_reason"): item.reject_reason = reason; item.save()
            else: item.save(update_fields=["status"])
            messages.warning(request, "반려됨.")
    return redirect("qms:m4_detail", pk=pk)

@login_required
def m4_resubmit(request, pk):
    if request.method == "POST":
        item = get_object_or_404(M4Request, pk=pk)
        if item.user == request.user and item.status == "REJECTED":
            item.status = "PENDING_REVIEW" if getattr(item, "reviewer_user_id", None) else ("PENDING_REVIEW2" if getattr(item, "reviewer_user2_id", None) else "PENDING_APPROVE")
            item.is_submitted, item.submitted_at = True, timezone.now(); item.is_reviewed = False; item.is_approved = False; item.save(); messages.success(request, "재상신됨.")
    return redirect("qms:m4_detail", pk=pk)

@login_required
def m4_cancel_approval(request, pk):
    if request.method == "POST":
        item = get_object_or_404(M4Request, pk=pk)
        if item.status == "PENDING_REVIEW" and request.user == item.user: item.status, item.is_submitted = "DRAFT", False
        elif item.status == "PENDING_REVIEW2" and request.user == item.reviewer_user: item.status, item.is_reviewed = "PENDING_REVIEW"
        elif item.status == "PENDING_APPROVE" and getattr(item, "reviewer_user2", None) and request.user == item.reviewer_user2: item.status = "PENDING_REVIEW2"
        elif item.status == "APPROVED" and request.user == item.approver_user: item.status, item.is_approved = "PENDING_APPROVE", False
        item.save(); messages.info(request, "취소됨.")
    return redirect("qms:m4_detail", pk=pk)

@login_required
def m4_delete(request, pk):
    item = get_object_or_404(M4Request, pk=pk)
    if item.user == request.user and item.status == "DRAFT" and request.method == "POST": item.delete(); messages.success(request, "삭제됨."); return redirect("qms:m4_list")
    return redirect("qms:m4_detail", pk=pk)


# =============================================================================
# [핵심] 수입검사 로직 (WMS & SCM 연동)
# =============================================================================

@qms_permission_required('can_qms_inspection_view')
def import_inspection_list(request):
    """
    [QMS] 수입검사 대기/완료 목록 조회 (필터 + 페이징)
    """
    qs = ImportInspection.objects.select_related(
        "inbound_transaction",
        "inbound_transaction__part",
        "inbound_transaction__vendor",
    ).annotate(
        status_rank=Case(
            When(status="PENDING", then=Value(0)),     # 검사대기 최상위
            When(status="APPROVED", then=Value(1)),    # 합격
            When(status="REJECTED", then=Value(2)),    # 불합격
            default=Value(9),
            output_field=IntegerField(),
        )
    ).order_by("status_rank", "-created_at", "-id")

    # ===== GET 파라미터 =====
    start_date = request.GET.get("start_date", "")
    end_date = request.GET.get("end_date", "")
    status_filter = request.GET.get("status", "")
    q = (request.GET.get("q", "") or "").strip()

    # ===== 기간 필터 =====
    if start_date:
        qs = qs.filter(created_at__date__gte=start_date)
    if end_date:
        qs = qs.filter(created_at__date__lte=end_date)

    # ===== 상태 필터 =====
    if status_filter == "PASSED":
        qs = qs.filter(status="APPROVED")
    elif status_filter == "FAILED":
        qs = qs.filter(status="REJECTED")
    elif status_filter:
        qs = qs.filter(status=status_filter)

    # ===== 검색 =====
    if q:
        qs = qs.filter(
            Q(inbound_transaction__part__part_no__icontains=q) |
            Q(inbound_transaction__part__part_name__icontains=q)
        )

    # ===== 페이징 =====
    paginator = Paginator(qs, 20)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "qms/import_inspection_list.html",
        {
            "page_obj": page_obj,
            "start_date": start_date,
            "end_date": end_date,
            "status_filter": status_filter,
            "q": q,
        },
    )


# qms/views.py 의 import_inspection_detail 함수 교체

@qms_permission_required('can_qms_inspection_edit')
def import_inspection_detail(request, pk):
    """
    [QMS] 수입검사 상세 및 판정 (양품/불량 분할)
    - 양품: WH_MAT로 이동 + SCM Incoming.confirmed_qty 반영
    - 불량: 8200(부적합창고)로 이동 + ReturnLog 생성
    """
    inspection = get_object_or_404(ImportInspection, pk=pk)
    origin_trx = inspection.inbound_transaction

    display_ref_no = (
        origin_trx.ref_delivery_order if origin_trx.ref_delivery_order else origin_trx.transaction_no
    )
    is_scm_linked = bool(origin_trx.ref_delivery_order)

    if request.method == "POST":
        decision = request.POST.get("decision")  # 'COMPLETE' or 'CANCEL'

        try:
            with transaction.atomic():
                # [A] 판정 취소
                if decision == "CANCEL":
                    if inspection.status == "PENDING":
                        messages.warning(request, "아직 판정되지 않은 건입니다.")
                    else:
                        messages.error(request, "분할 판정된 건은 시스템 관리자를 통해 재고를 수동 조정해야 합니다.")
                    return redirect("qms:import_inspection_detail", pk=pk)

                # [B] 이미 판정 완료면 중단
                if inspection.status != "PENDING":
                    messages.error(request, "이미 판정이 완료된 건입니다.")
                    return redirect("qms:import_inspection_detail", pk=pk)

                # 1) 수량 입력
                try:
                    qty_good = int(request.POST.get("qty_good", 0))
                    qty_bad = int(request.POST.get("qty_bad", 0))
                except ValueError:
                    messages.error(request, "수량은 숫자만 입력 가능합니다.")
                    return redirect("qms:import_inspection_detail", pk=pk)

                total_input = qty_good + qty_bad
                if total_input != origin_trx.quantity:
                    messages.error(
                        request,
                        f"입력 수량 합계({total_input})가 입고 수량({origin_trx.quantity})과 다릅니다.",
                    )
                    return redirect("qms:import_inspection_detail", pk=pk)

                # 2) 검사 결과 저장
                inspection.status = "APPROVED" if qty_good > 0 else "REJECTED"
                inspection.inspector = request.user
                inspection.inspected_at = timezone.now()
                inspection.qty_good = qty_good
                inspection.qty_bad = qty_bad
                inspection.remark = request.POST.get('remark', '')

                # 체크 항목
                inspection.check_report = request.POST.get("check_report") == "on"
                inspection.check_visual = request.POST.get("check_visual") == "on"
                inspection.check_dimension = request.POST.get("check_dimension") == "on"

                if request.FILES.get("attachment"):
                    inspection.attachment = request.FILES["attachment"]

                inspection.save()

                # 3) 재고 이동
                part = origin_trx.part
                lot_no = origin_trx.lot_no  # 원래 입고 시 LOT 번호 유지
                from_wh = origin_trx.warehouse_to  # 검사대기장

                # (A) 검사대기장 전체 차감
                src_stock = MaterialStock.objects.filter(
                    warehouse=from_wh,
                    part=part,
                    lot_no=lot_no
                ).first()
                if src_stock:
                    src_stock.quantity = F("quantity") - total_input
                    src_stock.save()
                    src_stock.refresh_from_db()

                # (B) 양품 -> 목표 창고 (inspection.target_warehouse_code 사용)
                if qty_good > 0:
                    target_code = inspection.target_warehouse_code or '2000'
                    wh_good = Warehouse.objects.filter(code=target_code).first()
                    if not wh_good:
                        wh_good = Warehouse.objects.filter(code="2000").first()
                    if not wh_good:
                        raise Exception(f"시스템 오류: 목표 창고({target_code})가 없습니다.")

                    stock_good, _ = MaterialStock.objects.get_or_create(
                        warehouse=wh_good,
                        part=part,
                        lot_no=lot_no
                    )
                    stock_good.quantity = F("quantity") + qty_good
                    stock_good.save()
                    stock_good.refresh_from_db()

                    MaterialTransaction.objects.create(
                        transaction_no=f"TRX-OK-{timezone.now().strftime('%y%m%d%H%M%S')}",
                        transaction_type="TRANSFER",
                        date=timezone.now(),
                        part=part,
                        lot_no=lot_no,
                        quantity=qty_good,
                        warehouse_from=from_wh,
                        warehouse_to=wh_good,
                        result_stock=stock_good.quantity,
                        vendor=origin_trx.vendor,
                        actor=request.user,
                        remark="[수입검사] 양품 입고",
                        ref_delivery_order=origin_trx.ref_delivery_order,
                    )

                # (C) 불량 -> 8200(부적합창고)
                if qty_bad > 0:
                    wh_bad = Warehouse.objects.filter(code="8200").first()
                    if not wh_bad:
                        wh_bad = Warehouse.objects.create(code="8200", name="부적합창고")

                    stock_bad, _ = MaterialStock.objects.get_or_create(
                        warehouse=wh_bad,
                        part=part,
                        lot_no=lot_no
                    )
                    stock_bad.quantity = F("quantity") + qty_bad
                    stock_bad.save()
                    stock_bad.refresh_from_db()

                    MaterialTransaction.objects.create(
                        transaction_no=f"TRX-NG-{timezone.now().strftime('%y%m%d%H%M%S')}",
                        transaction_type="TRANSFER",
                        date=timezone.now(),
                        part=part,
                        lot_no=lot_no,
                        quantity=qty_bad,
                        warehouse_from=from_wh,
                        warehouse_to=wh_bad,
                        result_stock=stock_bad.quantity,
                        vendor=origin_trx.vendor,
                        actor=request.user,
                        remark=f"[수입검사] 불량 격리 (사유: {request.POST.get('remark')})",
                    )

                # 4) SCM 연동 (ref_delivery_order 있을 때만)
                ref_do_no = origin_trx.ref_delivery_order
                if ref_do_no:
                    target_do_item = DeliveryOrderItem.objects.filter(
                        order__order_no=ref_do_no, part_no=part.part_no
                    ).first()
                    erp_no = target_do_item.erp_order_no if target_do_item else ""
                    erp_seq = target_do_item.erp_order_seq if target_do_item else ""

                    incoming_obj, _ = Incoming.objects.get_or_create(
                        delivery_order_no=ref_do_no,
                        part=part,
                        defaults={
                            "in_date": timezone.localtime().date(),
                            "quantity": total_input,
                            "erp_order_no": erp_no,
                            "erp_order_seq": erp_seq,
                        },
                    )
                    incoming_obj.quantity = total_input
                    incoming_obj.confirmed_qty = qty_good
                    incoming_obj.save()

                    # (주의) SCM Inventory.base_stock 누적은 정책에 따라 ON/OFF
                    if qty_good > 0:
                        scm_inv, _ = ScmInventory.objects.get_or_create(part=part)
                        scm_inv.base_stock += qty_good
                        scm_inv.save()

                    target_do = DeliveryOrder.objects.filter(order_no=ref_do_no).first()
                    if target_do:
                        if qty_good > 0:
                            target_do.status = "APPROVED"
                        elif qty_bad == total_input and target_do.status != "APPROVED":
                            target_do.status = "REJECTED"
                        target_do.save()

                    if qty_bad > 0:
                        ReturnLog.objects.create(
                            delivery_order=target_do,
                            part=part,
                            quantity=qty_bad,
                            reason=request.POST.get("remark", "수입검사 불량"),
                            is_confirmed=False,
                        )

            messages.success(request, f"판정 완료. (양품: {qty_good} / 불량: {qty_bad})")
            return redirect("qms:import_inspection_list")

        except Exception as e:
            messages.error(request, f"오류 발생: {str(e)}")
            return redirect("qms:import_inspection_detail", pk=pk)

    return render(
        request,
        "qms/import_inspection_detail.html",
        {
            "inspection": inspection,
            "display_ref_no": display_ref_no,
            "is_scm_linked": is_scm_linked,
        },
    )


# ============================================================================
# 새로운 4M 변경점 관리 시스템 (v2)
# ============================================================================

from .models import (
    ChangeRequest, ApprovalStep, VendorResponse, VendorResponseAttachment,
    ChangeDocument, ChangeHistory
)


def _is_vendor_user(user):
    """협력사 사용자 여부 확인"""
    if user.is_superuser:
        return False
    try:
        return user.profile.org and user.profile.org.org_type == 'VENDOR'
    except:
        return False


def _get_user_vendor_org(user):
    """사용자 소속 협력사 Organization 반환"""
    try:
        if user.profile.org and user.profile.org.org_type == 'VENDOR':
            return user.profile.org
    except:
        pass
    return None


def _log_change(change_request, action, description, user, field_name='', old_value='', new_value=''):
    """변경 이력 기록"""
    ChangeHistory.objects.create(
        change_request=change_request,
        action=action,
        description=description,
        actor=user,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value
    )


@login_required
def change_request_list(request):
    """4M 변경 신청 목록"""
    user = request.user
    qs = ChangeRequest.objects.select_related('vendor', 'created_by')

    # 협력사 사용자는 자신의 협력사 건만 조회
    vendor_org = _get_user_vendor_org(user)
    if vendor_org:
        qs = qs.filter(vendor=vendor_org)

    # 필터링
    phase = request.GET.get('phase', '')
    change_type = request.GET.get('type', '')
    q = request.GET.get('q', '')

    if phase:
        qs = qs.filter(phase=phase)
    if change_type:
        qs = qs.filter(change_type=change_type)
    if q:
        qs = qs.filter(
            models.Q(request_no__icontains=q) |
            models.Q(part_no__icontains=q) |
            models.Q(part_name__icontains=q)
        )

    # 내 결재 대기 건 수
    my_pending_count = ApprovalStep.objects.filter(
        assignee=user, status='PENDING'
    ).count()

    return render(request, 'qms/change_request_list.html', {
        'requests': qs[:100],
        'phase_choices': ChangeRequest.PHASE_CHOICES,
        'type_choices': ChangeRequest.TYPE_CHOICES,
        'selected_phase': phase,
        'selected_type': change_type,
        'q': q,
        'my_pending_count': my_pending_count,
        'is_vendor': bool(vendor_org),
    })


@login_required
def change_request_create(request):
    """4M 변경 신청 작성"""
    user = request.user
    vendor_org = _get_user_vendor_org(user)

    # 협력사 목록 (내부 사용자용)
    vendors = []
    if not vendor_org:
        vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')

    # 결재자 목록 (내부 사용자들)
    approvers = User.objects.filter(
        is_active=True,
        profile__org__org_type='INTERNAL'
    ).exclude(id=user.id).select_related('profile').order_by('username')

    if request.method == 'POST':
        # 기본 정보
        factory = request.POST.get('factory', '2공장').strip()
        change_type = request.POST.get('change_type')
        change_grade = request.POST.get('change_grade') or None
        part_no = request.POST.get('part_no', '').strip()
        part_name = request.POST.get('part_name', '').strip()
        part_group = request.POST.get('part_group', '').strip()  # 품목군 (model_name 필드로 저장)
        is_internal = request.POST.get('is_internal') == '1'
        is_external = request.POST.get('is_external') == '1'

        # 협력사
        if vendor_org:
            vendor = vendor_org
        else:
            vendor_id = request.POST.get('vendor_id')
            vendor = Organization.objects.get(id=vendor_id)

        # 변경 내용
        reason = request.POST.get('reason', '').strip()
        content_before = request.POST.get('content_before', '').strip()
        content_after = request.POST.get('content_after', '').strip()
        affected_items = request.POST.get('affected_items', '').strip()

        # 일정
        target_date = request.POST.get('target_date') or None

        # 생성
        cr = ChangeRequest.objects.create(
            factory=factory,
            change_type=change_type,
            change_grade=change_grade,
            part_no=part_no,
            part_name=part_name,
            model_name=part_group,  # 품목군 저장
            is_internal=is_internal,
            is_external=is_external,
            vendor=vendor,
            created_by=user,
            reason=reason,
            content_before=content_before,
            content_after=content_after,
            affected_items=affected_items,
            target_date=target_date,
            phase='DRAFT'
        )

        # 결재 단계 설정
        reviewer_id = request.POST.get('reviewer_id')
        reviewer2_id = request.POST.get('reviewer2_id')  # 검토2 추가
        approver_id = request.POST.get('approver_id')

        step_order = 1
        if reviewer_id:
            ApprovalStep.objects.create(
                change_request=cr,
                step_order=step_order,
                step_type='REVIEW',
                step_name='검토1',
                assignee_id=reviewer_id,
                status='WAITING'
            )
            step_order += 1

        if reviewer2_id:
            ApprovalStep.objects.create(
                change_request=cr,
                step_order=step_order,
                step_type='REVIEW',
                step_name='검토2',
                assignee_id=reviewer2_id,
                status='WAITING'
            )
            step_order += 1

        if approver_id:
            ApprovalStep.objects.create(
                change_request=cr,
                step_order=step_order,
                step_type='APPROVE',
                step_name='승인',
                assignee_id=approver_id,
                status='WAITING'
            )

        _log_change(cr, 'CREATE', '4M 변경신청서 작성', user)

        messages.success(request, f'4M 변경신청서가 작성되었습니다. ({cr.request_no})')
        return redirect('qms:change_request_detail', pk=cr.pk)

    return render(request, 'qms/change_request_form.html', {
        'vendors': vendors,
        'approvers': approvers,
        'type_choices': ChangeRequest.TYPE_CHOICES,
        'grade_choices': ChangeRequest.GRADE_CHOICES,
        'is_vendor': bool(vendor_org),
        'vendor_org': vendor_org,
        'mode': 'create',
    })


@login_required
def change_request_detail(request, pk):
    """4M 변경 신청 상세"""
    user = request.user
    cr = get_object_or_404(ChangeRequest.objects.select_related('vendor', 'created_by'), pk=pk)

    vendor_org = _get_user_vendor_org(user)

    # 협력사는 자신의 건만 조회 가능
    if vendor_org and cr.vendor != vendor_org:
        messages.error(request, '접근 권한이 없습니다.')
        return redirect('qms:change_request_list')

    # 결재 단계
    approval_steps = cr.approval_steps.select_related('assignee').order_by('step_order')

    # 현재 사용자가 결재 가능한지
    my_pending_step = approval_steps.filter(assignee=user, status='PENDING').first()

    # 협력사 회신 목록
    vendor_responses = cr.vendor_responses.select_related('requested_by', 'responded_by').prefetch_related('attachments')

    # 제출 서류
    documents = cr.documents.select_related('reviewed_by').order_by('id')

    # 변경 이력
    history = cr.history.select_related('actor').order_by('-created_at')[:20]

    return render(request, 'qms/change_request_detail.html', {
        'cr': cr,
        'approval_steps': approval_steps,
        'my_pending_step': my_pending_step,
        'vendor_responses': vendor_responses,
        'documents': documents,
        'history': history,
        'is_vendor': bool(vendor_org),
        'can_edit': cr.can_edit and (cr.created_by == user or user.is_superuser),
        'can_submit': cr.phase == 'DRAFT' and approval_steps.exists(),
    })


@login_required
def change_request_edit(request, pk):
    """4M 변경 신청 수정"""
    user = request.user
    cr = get_object_or_404(ChangeRequest, pk=pk)

    if not cr.can_edit:
        messages.error(request, '수정할 수 없는 상태입니다.')
        return redirect('qms:change_request_detail', pk=pk)

    if cr.created_by != user and not user.is_superuser:
        messages.error(request, '수정 권한이 없습니다.')
        return redirect('qms:change_request_detail', pk=pk)

    vendor_org = _get_user_vendor_org(user)
    vendors = []
    if not vendor_org:
        vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')

    approvers = User.objects.filter(
        is_active=True,
        profile__org__org_type='INTERNAL'
    ).exclude(id=user.id).select_related('profile').order_by('username')

    # step_name 기준으로 구분 (검토1, 검토2, 승인)
    existing_steps = {s.step_name: s for s in cr.approval_steps.all()}

    if request.method == 'POST':
        # 업데이트
        cr.factory = request.POST.get('factory', '2공장').strip()
        cr.change_type = request.POST.get('change_type')
        cr.change_grade = request.POST.get('change_grade') or None
        cr.part_no = request.POST.get('part_no', '').strip()
        cr.part_name = request.POST.get('part_name', '').strip()
        cr.model_name = request.POST.get('part_group', '').strip()  # 품목군
        cr.is_internal = request.POST.get('is_internal') == '1'
        cr.is_external = request.POST.get('is_external') == '1'
        cr.reason = request.POST.get('reason', '').strip()
        cr.content_before = request.POST.get('content_before', '').strip()
        cr.content_after = request.POST.get('content_after', '').strip()
        cr.affected_items = request.POST.get('affected_items', '').strip()
        cr.target_date = request.POST.get('target_date') or None

        if not vendor_org:
            vendor_id = request.POST.get('vendor_id')
            cr.vendor_id = vendor_id

        # 반려 상태에서 수정 시 DRAFT로 변경
        if cr.phase == 'REJECTED':
            cr.phase = 'DRAFT'
            cr.reject_reason = ''
            # 결재 단계 초기화
            cr.approval_steps.update(status='WAITING', comment='', processed_at=None)

        cr.save()

        # 결재자 업데이트
        reviewer_id = request.POST.get('reviewer_id')
        reviewer2_id = request.POST.get('reviewer2_id')  # 검토2 추가
        approver_id = request.POST.get('approver_id')

        # 기존 단계 삭제 후 재생성
        cr.approval_steps.all().delete()
        step_order = 1
        if reviewer_id:
            ApprovalStep.objects.create(
                change_request=cr, step_order=step_order,
                step_type='REVIEW', step_name='검토1',
                assignee_id=reviewer_id, status='WAITING'
            )
            step_order += 1
        if reviewer2_id:
            ApprovalStep.objects.create(
                change_request=cr, step_order=step_order,
                step_type='REVIEW', step_name='검토2',
                assignee_id=reviewer2_id, status='WAITING'
            )
            step_order += 1
        if approver_id:
            ApprovalStep.objects.create(
                change_request=cr, step_order=step_order,
                step_type='APPROVE', step_name='승인',
                assignee_id=approver_id, status='WAITING'
            )

        _log_change(cr, 'UPDATE', '4M 변경신청서 수정', user)

        messages.success(request, '수정되었습니다.')
        return redirect('qms:change_request_detail', pk=pk)

    return render(request, 'qms/change_request_form.html', {
        'cr': cr,
        'vendors': vendors,
        'approvers': approvers,
        'type_choices': ChangeRequest.TYPE_CHOICES,
        'grade_choices': ChangeRequest.GRADE_CHOICES,
        'is_vendor': bool(vendor_org),
        'vendor_org': vendor_org,
        'mode': 'edit',
        'existing_reviewer': existing_steps.get('검토1') or existing_steps.get('검토'),  # 기존 호환성
        'existing_reviewer2': existing_steps.get('검토2'),
        'existing_approver': existing_steps.get('승인'),
    })


@login_required
@require_POST
def change_request_submit(request, pk):
    """4M 변경 신청 상신"""
    user = request.user
    cr = get_object_or_404(ChangeRequest, pk=pk)

    if cr.phase != 'DRAFT':
        messages.error(request, '상신할 수 없는 상태입니다.')
        return redirect('qms:change_request_detail', pk=pk)

    if cr.created_by != user and not user.is_superuser:
        messages.error(request, '상신 권한이 없습니다.')
        return redirect('qms:change_request_detail', pk=pk)

    # 첫 번째 결재 단계 활성화
    first_step = cr.approval_steps.order_by('step_order').first()
    if not first_step:
        messages.error(request, '결재자가 지정되지 않았습니다.')
        return redirect('qms:change_request_detail', pk=pk)

    first_step.status = 'PENDING'
    first_step.save()

    cr.phase = 'REVIEW'
    cr.save()

    _log_change(cr, 'SUBMIT', '결재 상신', user)

    messages.success(request, '결재 상신되었습니다.')
    return redirect('qms:change_request_detail', pk=pk)


@login_required
@require_POST
def approval_step_process(request, pk):
    """결재 처리 (승인/반려)"""
    user = request.user
    step = get_object_or_404(ApprovalStep.objects.select_related('change_request'), pk=pk)
    cr = step.change_request

    if step.assignee != user:
        messages.error(request, '결재 권한이 없습니다.')
        return redirect('qms:change_request_detail', pk=cr.pk)

    if step.status != 'PENDING':
        messages.error(request, '결재할 수 없는 상태입니다.')
        return redirect('qms:change_request_detail', pk=cr.pk)

    action = request.POST.get('action')
    comment = request.POST.get('comment', '').strip()

    if action == 'approve':
        step.approve(user, comment)
        _log_change(cr, 'APPROVE', f'{step.step_name} 승인: {comment or "(의견없음)"}', user)
        messages.success(request, '승인되었습니다.')
    elif action == 'reject':
        if not comment:
            messages.error(request, '반려 사유를 입력해주세요.')
            return redirect('qms:change_request_detail', pk=cr.pk)
        step.reject(user, comment)
        _log_change(cr, 'REJECT', f'{step.step_name} 반려: {comment}', user)
        messages.warning(request, '반려되었습니다.')
    else:
        messages.error(request, '잘못된 요청입니다.')

    return redirect('qms:change_request_detail', pk=cr.pk)


@login_required
@require_POST
def change_request_phase_change(request, pk):
    """단계 전환 (APPROVED → FORMAL → VALIDATION → CLOSED)"""
    user = request.user
    cr = get_object_or_404(ChangeRequest, pk=pk)

    if _is_vendor_user(user):
        messages.error(request, '단계 전환 권한이 없습니다.')
        return redirect('qms:change_request_detail', pk=pk)

    new_phase = request.POST.get('new_phase')
    valid_transitions = {
        'APPROVED': ['FORMAL'],
        'FORMAL': ['VALIDATION'],
        'VALIDATION': ['CLOSED'],
    }

    if new_phase not in valid_transitions.get(cr.phase, []):
        messages.error(request, f'현재 상태({cr.get_phase_display()})에서 {new_phase}로 전환할 수 없습니다.')
        return redirect('qms:change_request_detail', pk=pk)

    old_phase = cr.phase
    cr.phase = new_phase

    if new_phase == 'FORMAL':
        cr.formal_start_date = timezone.localdate()
        # 유효성 평가 기한 = 3개월 후 (약 90일)
        import datetime
        cr.validation_due_date = cr.formal_start_date + datetime.timedelta(days=90)
        # 기본 제출 서류 생성
        for doc_name in ChangeDocument.DEFAULT_DOCUMENTS:
            ChangeDocument.objects.get_or_create(
                change_request=cr, doc_name=doc_name,
                defaults={'is_required': doc_name in ['검사성적서', '공정흐름도', '관리계획서']}
            )
    elif new_phase == 'CLOSED':
        cr.closed_date = timezone.localdate()

    cr.save()

    _log_change(cr, 'PHASE_CHANGE', f'단계 전환: {old_phase} → {new_phase}', user)

    messages.success(request, f'{cr.get_phase_display()} 단계로 전환되었습니다.')
    return redirect('qms:change_request_detail', pk=pk)


@login_required
@require_POST
def vendor_response_create(request, pk):
    """협력사 회신 요청 생성 (내부 → 협력사)"""
    user = request.user
    cr = get_object_or_404(ChangeRequest, pk=pk)

    if _is_vendor_user(user):
        messages.error(request, '요청 권한이 없습니다.')
        return redirect('qms:change_request_detail', pk=pk)

    title = request.POST.get('request_title', '').strip()
    content = request.POST.get('request_content', '').strip()

    if not title or not content:
        messages.error(request, '요청 제목과 내용을 입력해주세요.')
        return redirect('qms:change_request_detail', pk=pk)

    VendorResponse.objects.create(
        change_request=cr,
        request_title=title,
        request_content=content,
        requested_by=user
    )

    _log_change(cr, 'VENDOR_REQUEST', f'협력사 회신 요청: {title}', user)

    messages.success(request, '협력사에 회신 요청을 보냈습니다.')
    return redirect('qms:change_request_detail', pk=pk)


@login_required
@require_POST
def vendor_response_submit(request, pk):
    """협력사 회신 제출"""
    user = request.user
    vr = get_object_or_404(VendorResponse.objects.select_related('change_request'), pk=pk)
    cr = vr.change_request

    vendor_org = _get_user_vendor_org(user)
    if not vendor_org or cr.vendor != vendor_org:
        messages.error(request, '회신 권한이 없습니다.')
        return redirect('qms:change_request_detail', pk=cr.pk)

    content = request.POST.get('response_content', '').strip()
    if not content:
        messages.error(request, '회신 내용을 입력해주세요.')
        return redirect('qms:change_request_detail', pk=cr.pk)

    vr.response_content = content
    vr.responded_by = user
    vr.responded_at = timezone.now()
    vr.status = 'RESPONDED'
    vr.save()

    # 첨부파일 처리
    files = request.FILES.getlist('attachments')
    for f in files:
        VendorResponseAttachment.objects.create(
            response=vr,
            file=f,
            file_name=f.name
        )

    _log_change(cr, 'VENDOR_RESPONSE', f'협력사 회신: {vr.request_title}', user)

    messages.success(request, '회신이 제출되었습니다.')
    return redirect('qms:change_request_detail', pk=cr.pk)


@login_required
@require_POST
def document_upload(request, pk):
    """제출 서류 업로드"""
    user = request.user
    doc = get_object_or_404(ChangeDocument.objects.select_related('change_request'), pk=pk)
    cr = doc.change_request

    # 협력사 또는 내부 모두 업로드 가능
    vendor_org = _get_user_vendor_org(user)
    if vendor_org and cr.vendor != vendor_org:
        messages.error(request, '업로드 권한이 없습니다.')
        return redirect('qms:change_request_detail', pk=cr.pk)

    file = request.FILES.get('file')
    if not file:
        messages.error(request, '파일을 선택해주세요.')
        return redirect('qms:change_request_detail', pk=cr.pk)

    doc.file = file
    doc.uploaded_at = timezone.now()
    doc.review_status = 'PENDING'
    doc.save()

    _log_change(cr, 'DOC_UPLOAD', f'서류 업로드: {doc.doc_name}', user)

    messages.success(request, '파일이 업로드되었습니다.')
    return redirect('qms:change_request_detail', pk=cr.pk)


@login_required
@require_POST
def document_review(request, pk):
    """제출 서류 검토 (내부)"""
    user = request.user
    doc = get_object_or_404(ChangeDocument.objects.select_related('change_request'), pk=pk)
    cr = doc.change_request

    if _is_vendor_user(user):
        messages.error(request, '검토 권한이 없습니다.')
        return redirect('qms:change_request_detail', pk=cr.pk)

    status = request.POST.get('review_status')
    comment = request.POST.get('review_comment', '').strip()

    if status not in ['OK', 'REJECT']:
        messages.error(request, '잘못된 요청입니다.')
        return redirect('qms:change_request_detail', pk=cr.pk)

    doc.review_status = status
    doc.review_comment = comment
    doc.reviewed_by = user
    doc.reviewed_at = timezone.now()
    doc.save()

    _log_change(cr, 'DOC_REVIEW', f'서류 검토: {doc.doc_name} - {doc.get_review_status_display()}', user)

    messages.success(request, '검토 결과가 저장되었습니다.')
    return redirect('qms:change_request_detail', pk=cr.pk)


@login_required
@require_POST
def validity_evaluation(request, pk):
    """유효성 평가 결과 입력"""
    user = request.user
    cr = get_object_or_404(ChangeRequest, pk=pk)

    if _is_vendor_user(user):
        messages.error(request, '평가 권한이 없습니다.')
        return redirect('qms:change_request_detail', pk=pk)

    if cr.phase != 'VALIDATION':
        messages.error(request, '유효성 평가 단계가 아닙니다.')
        return redirect('qms:change_request_detail', pk=pk)

    result = request.POST.get('validity_result')
    remark = request.POST.get('validity_remark', '').strip()

    if result not in ['PASS', 'FAIL']:
        messages.error(request, '평가 결과를 선택해주세요.')
        return redirect('qms:change_request_detail', pk=pk)

    cr.validity_result = result
    cr.validity_remark = remark
    cr.save()

    _log_change(cr, 'UPDATE', f'유효성 평가: {cr.get_validity_result_display()}', user)

    messages.success(request, '유효성 평가 결과가 저장되었습니다.')
    return redirect('qms:change_request_detail', pk=pk)


# ============================================================================
# 출하검사 (Outgoing Inspection)
# ============================================================================

from .models import OutgoingInspection, NonConformance, CorrectiveAction, VendorClaim, VendorRating, ISIR, ISIRItem

@login_required
def outgoing_inspection_list(request):
    """출하검사 목록"""
    qs = OutgoingInspection.objects.all()

    # 필터링
    status = request.GET.get('status')
    part_no = request.GET.get('part_no', '').strip()
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')

    if status:
        qs = qs.filter(status=status)
    if part_no:
        qs = qs.filter(Q(part_no__icontains=part_no) | Q(part_name__icontains=part_no))
    if date_from:
        qs = qs.filter(inspection_date__gte=date_from)
    if date_to:
        qs = qs.filter(inspection_date__lte=date_to)

    # 페이징
    paginator = Paginator(qs, 20)
    page = request.GET.get('page')
    inspections = paginator.get_page(page)

    # 통계
    stats = {
        'total': OutgoingInspection.objects.count(),
        'pending': OutgoingInspection.objects.filter(status='PENDING').count(),
        'pass': OutgoingInspection.objects.filter(status='PASS').count(),
        'fail': OutgoingInspection.objects.filter(status='FAIL').count(),
    }

    return render(request, 'qms/outgoing_inspection_list.html', {
        'inspections': inspections,
        'stats': stats,
        'status_choices': OutgoingInspection.STATUS_CHOICES,
    })


@login_required
def outgoing_inspection_create(request):
    """출하검사 등록"""
    if request.method == 'POST':
        oi = OutgoingInspection(
            inspection_date=request.POST.get('inspection_date'),
            part_no=request.POST.get('part_no'),
            part_name=request.POST.get('part_name'),
            lot_no=request.POST.get('lot_no', ''),
            total_qty=int(request.POST.get('total_qty', 0)),
            sample_qty=int(request.POST.get('sample_qty', 0)),
            customer_name=request.POST.get('customer_name', ''),
            delivery_date=request.POST.get('delivery_date') or None,
            inspector=request.user,
        )
        oi.save()
        messages.success(request, f'출하검사 {oi.inspection_no}가 등록되었습니다.')
        return redirect('qms:outgoing_inspection_detail', pk=oi.pk)

    return render(request, 'qms/outgoing_inspection_form.html', {
        'mode': 'create',
    })


@login_required
def outgoing_inspection_detail(request, pk):
    """출하검사 상세"""
    oi = get_object_or_404(OutgoingInspection, pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'update_result':
            # 검사 결과 업데이트
            oi.check_visual = request.POST.get('check_visual') == 'on'
            oi.check_dimension = request.POST.get('check_dimension') == 'on'
            oi.check_function = request.POST.get('check_function') == 'on'
            oi.check_packing = request.POST.get('check_packing') == 'on'
            oi.check_label = request.POST.get('check_label') == 'on'
            oi.pass_qty = int(request.POST.get('pass_qty', 0))
            oi.fail_qty = int(request.POST.get('fail_qty', 0))
            oi.remark = request.POST.get('remark', '')
            oi.status = request.POST.get('status', 'PENDING')

            if request.FILES.get('attachment'):
                oi.attachment = request.FILES['attachment']

            oi.save()
            messages.success(request, '검사 결과가 저장되었습니다.')

    return render(request, 'qms/outgoing_inspection_detail.html', {
        'oi': oi,
        'status_choices': OutgoingInspection.STATUS_CHOICES,
    })


# ============================================================================
# 부적합품 관리 (Non-conformance)
# ============================================================================

@login_required
def nc_list(request):
    """부적합품 목록"""
    qs = NonConformance.objects.select_related('vendor', 'reported_by', 'assigned_to').all()

    # 필터링
    status = request.GET.get('status')
    source = request.GET.get('source')
    part_no = request.GET.get('part_no', '').strip()
    vendor_id = request.GET.get('vendor')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')

    if status:
        qs = qs.filter(status=status)
    if source:
        qs = qs.filter(source=source)
    if part_no:
        qs = qs.filter(Q(part_no__icontains=part_no) | Q(part_name__icontains=part_no))
    if vendor_id:
        qs = qs.filter(vendor_id=vendor_id)
    if date_from:
        qs = qs.filter(occurred_date__gte=date_from)
    if date_to:
        qs = qs.filter(occurred_date__lte=date_to)

    # 페이징
    paginator = Paginator(qs, 20)
    page = request.GET.get('page')
    ncs = paginator.get_page(page)

    # 통계
    stats = {
        'total': NonConformance.objects.count(),
        'open': NonConformance.objects.filter(status='OPEN').count(),
        'action': NonConformance.objects.filter(status='ACTION').count(),
        'closed': NonConformance.objects.filter(status='CLOSED').count(),
    }

    vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')

    return render(request, 'qms/nc_list.html', {
        'ncs': ncs,
        'stats': stats,
        'status_choices': NonConformance.STATUS_CHOICES,
        'source_choices': NonConformance.SOURCE_CHOICES,
        'vendors': vendors,
    })


@login_required
def nc_create(request):
    """부적합품 등록"""
    if request.method == 'POST':
        vendor_id = request.POST.get('vendor')
        nc = NonConformance(
            source=request.POST.get('source'),
            occurred_date=request.POST.get('occurred_date'),
            part_no=request.POST.get('part_no'),
            part_name=request.POST.get('part_name'),
            lot_no=request.POST.get('lot_no', ''),
            vendor_id=vendor_id if vendor_id else None,
            defect_qty=int(request.POST.get('defect_qty', 0)),
            defect_type=request.POST.get('defect_type'),
            defect_detail=request.POST.get('defect_detail'),
            reported_by=request.user,
        )
        if request.FILES.get('photo'):
            nc.photo = request.FILES['photo']
        nc.save()
        messages.success(request, f'부적합 {nc.nc_no}가 등록되었습니다.')
        return redirect('qms:nc_detail', pk=nc.pk)

    vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')
    return render(request, 'qms/nc_form.html', {
        'mode': 'create',
        'source_choices': NonConformance.SOURCE_CHOICES,
        'vendors': vendors,
    })


@login_required
def nc_detail(request, pk):
    """부적합품 상세"""
    nc = get_object_or_404(NonConformance.objects.select_related('vendor', 'reported_by', 'assigned_to'), pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'update':
            # 상태 및 처리내역 업데이트
            nc.status = request.POST.get('status', nc.status)
            nc.cause_analysis = request.POST.get('cause_analysis', '')
            nc.root_cause = request.POST.get('root_cause', '')
            nc.disposition = request.POST.get('disposition', '')
            nc.disposition_detail = request.POST.get('disposition_detail', '')

            if request.POST.get('assigned_to'):
                nc.assigned_to_id = request.POST.get('assigned_to')

            if nc.status == 'CLOSED' and not nc.closed_date:
                nc.closed_date = timezone.localdate()

            nc.save()
            messages.success(request, '부적합 정보가 업데이트되었습니다.')

        elif action == 'create_capa':
            # 시정조치 요청 생성
            if nc.vendor:
                capa = CorrectiveAction(
                    non_conformance=nc,
                    vendor=nc.vendor,
                    part_no=nc.part_no,
                    part_name=nc.part_name,
                    issue_title=nc.defect_type,
                    issue_detail=nc.defect_detail,
                    request_date=timezone.localdate(),
                    due_date=timezone.localdate() + timezone.timedelta(days=7),
                    requested_by=request.user,
                )
                capa.save()
                messages.success(request, f'시정조치 요청 {capa.capa_no}가 생성되었습니다.')
                return redirect('qms:capa_detail', pk=capa.pk)
            else:
                messages.error(request, '협력사 정보가 없어 시정조치를 생성할 수 없습니다.')

    users = User.objects.filter(is_active=True).order_by('username')

    return render(request, 'qms/nc_detail.html', {
        'nc': nc,
        'status_choices': NonConformance.STATUS_CHOICES,
        'disposition_choices': NonConformance.DISPOSITION_CHOICES,
        'users': users,
    })


# ============================================================================
# 시정조치 (CAPA)
# ============================================================================

@login_required
def capa_list(request):
    """시정조치 목록"""
    qs = CorrectiveAction.objects.select_related('vendor', 'requested_by', 'non_conformance').all()

    # 필터링
    status = request.GET.get('status')
    capa_type = request.GET.get('capa_type')
    vendor_id = request.GET.get('vendor')
    overdue = request.GET.get('overdue')

    if status:
        qs = qs.filter(status=status)
    if capa_type:
        qs = qs.filter(capa_type=capa_type)
    if vendor_id:
        qs = qs.filter(vendor_id=vendor_id)
    if overdue == 'yes':
        qs = qs.filter(due_date__lt=timezone.localdate()).exclude(status__in=['CLOSED', 'VERIFYING'])

    # 페이징
    paginator = Paginator(qs, 20)
    page = request.GET.get('page')
    capas = paginator.get_page(page)

    # 통계
    stats = {
        'total': CorrectiveAction.objects.count(),
        'requested': CorrectiveAction.objects.filter(status='REQUESTED').count(),
        'overdue': CorrectiveAction.objects.filter(due_date__lt=timezone.localdate()).exclude(status__in=['CLOSED', 'VERIFYING']).count(),
        'closed': CorrectiveAction.objects.filter(status='CLOSED').count(),
    }

    vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')

    return render(request, 'qms/capa_list.html', {
        'capas': capas,
        'stats': stats,
        'status_choices': CorrectiveAction.STATUS_CHOICES,
        'type_choices': CorrectiveAction.TYPE_CHOICES,
        'vendors': vendors,
    })


@login_required
def capa_create(request):
    """시정조치 등록"""
    if request.method == 'POST':
        capa = CorrectiveAction(
            capa_type=request.POST.get('capa_type', 'CA'),
            vendor_id=request.POST.get('vendor'),
            part_no=request.POST.get('part_no'),
            part_name=request.POST.get('part_name'),
            issue_title=request.POST.get('issue_title'),
            issue_detail=request.POST.get('issue_detail'),
            request_date=request.POST.get('request_date'),
            due_date=request.POST.get('due_date'),
            requested_by=request.user,
        )
        capa.save()
        messages.success(request, f'시정조치 요청 {capa.capa_no}가 등록되었습니다.')
        return redirect('qms:capa_detail', pk=capa.pk)

    vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')
    return render(request, 'qms/capa_form.html', {
        'mode': 'create',
        'type_choices': CorrectiveAction.TYPE_CHOICES,
        'vendors': vendors,
    })


@login_required
def capa_detail(request, pk):
    """시정조치 상세"""
    capa = get_object_or_404(CorrectiveAction.objects.select_related('vendor', 'requested_by', 'non_conformance'), pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'update_status':
            capa.status = request.POST.get('status', capa.status)
            if capa.status == 'CLOSED' and not capa.closed_date:
                capa.closed_date = timezone.localdate()
            capa.save()
            messages.success(request, '상태가 업데이트되었습니다.')

        elif action == 'vendor_response':
            # 협력사 회신 등록
            capa.cause_analysis = request.POST.get('cause_analysis', '')
            capa.corrective_action = request.POST.get('corrective_action', '')
            capa.preventive_action = request.POST.get('preventive_action', '')
            capa.action_date = request.POST.get('action_date') or None
            capa.response_date = timezone.localdate()

            if request.FILES.get('attachment'):
                capa.attachment = request.FILES['attachment']

            if capa.status in ('REQUESTED', 'RECEIVED'):
                capa.status = 'ANALYZING'

            capa.save()
            messages.success(request, '회신 내용이 저장되었습니다.')

        elif action == 'verify':
            # 효과 검증
            capa.verification_result = request.POST.get('verification_result', '')
            capa.is_effective = request.POST.get('is_effective') == 'yes'
            capa.verified_date = timezone.localdate()
            capa.status = 'VERIFYING'
            capa.save()
            messages.success(request, '검증 결과가 저장되었습니다.')

    return render(request, 'qms/capa_detail.html', {
        'capa': capa,
        'status_choices': CorrectiveAction.STATUS_CHOICES,
    })


# ============================================================================
# 협력사 클레임 (Vendor Claim)
# ============================================================================

@login_required
def claim_list(request):
    """클레임 목록"""
    qs = VendorClaim.objects.select_related('vendor', 'issued_by', 'non_conformance').all()

    # 필터링
    status = request.GET.get('status')
    claim_type = request.GET.get('claim_type')
    vendor_id = request.GET.get('vendor')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')

    if status:
        qs = qs.filter(status=status)
    if claim_type:
        qs = qs.filter(claim_type=claim_type)
    if vendor_id:
        qs = qs.filter(vendor_id=vendor_id)
    if date_from:
        qs = qs.filter(issue_date__gte=date_from)
    if date_to:
        qs = qs.filter(issue_date__lte=date_to)

    # 페이징
    paginator = Paginator(qs, 20)
    page = request.GET.get('page')
    claims = paginator.get_page(page)

    # 통계
    stats = {
        'total': VendorClaim.objects.count(),
        'draft': VendorClaim.objects.filter(status='DRAFT').count(),
        'processing': VendorClaim.objects.filter(status='PROCESSING').count(),
        'closed': VendorClaim.objects.filter(status='CLOSED').count(),
    }

    vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')

    return render(request, 'qms/claim_list.html', {
        'claims': claims,
        'stats': stats,
        'status_choices': VendorClaim.STATUS_CHOICES,
        'type_choices': VendorClaim.CLAIM_TYPE_CHOICES,
        'vendors': vendors,
    })


@login_required
def claim_create(request):
    """클레임 등록"""
    if request.method == 'POST':
        claim = VendorClaim(
            claim_type=request.POST.get('claim_type'),
            issue_date=request.POST.get('issue_date'),
            vendor_id=request.POST.get('vendor'),
            part_no=request.POST.get('part_no'),
            part_name=request.POST.get('part_name'),
            lot_no=request.POST.get('lot_no', ''),
            claim_qty=int(request.POST.get('claim_qty', 0)),
            claim_amount=request.POST.get('claim_amount') or None,
            claim_detail=request.POST.get('claim_detail'),
            issued_by=request.user,
        )
        if request.FILES.get('photo'):
            claim.photo = request.FILES['photo']
        claim.save()
        messages.success(request, f'클레임 {claim.claim_no}가 등록되었습니다.')
        return redirect('qms:claim_detail', pk=claim.pk)

    vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')
    return render(request, 'qms/claim_form.html', {
        'mode': 'create',
        'type_choices': VendorClaim.CLAIM_TYPE_CHOICES,
        'vendors': vendors,
    })


@login_required
def claim_detail(request, pk):
    """클레임 상세"""
    claim = get_object_or_404(VendorClaim.objects.select_related('vendor', 'issued_by', 'non_conformance'), pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'issue':
            # 클레임 발행
            claim.status = 'ISSUED'
            claim.issued_date = timezone.localdate()
            claim.save()
            messages.success(request, '클레임이 발행되었습니다.')

        elif action == 'update':
            # 처리 정보 업데이트
            claim.status = request.POST.get('status', claim.status)
            claim.vendor_response = request.POST.get('vendor_response', '')
            claim.compensation_type = request.POST.get('compensation_type', '')
            claim.compensation_amount = request.POST.get('compensation_amount') or None
            claim.resolution_detail = request.POST.get('resolution_detail', '')

            # 정산 정보 업데이트
            claim.settlement_status = request.POST.get('settlement_status', claim.settlement_status)
            settlement_date = request.POST.get('settlement_date')
            claim.settlement_date = settlement_date if settlement_date else None
            claim.settlement_remark = request.POST.get('settlement_remark', '')

            if claim.status == 'RESOLVED' and not claim.resolved_date:
                claim.resolved_date = timezone.localdate()
            if claim.status == 'CLOSED' and not claim.closed_date:
                claim.closed_date = timezone.localdate()

            claim.save()
            messages.success(request, '클레임 정보가 업데이트되었습니다.')

    return render(request, 'qms/claim_detail.html', {
        'claim': claim,
        'status_choices': VendorClaim.STATUS_CHOICES,
    })


# ============================================================================
# 협력사 평가 (Vendor Rating)
# ============================================================================

@login_required
def vendor_rating_list(request):
    """협력사 평가 목록"""
    qs = VendorRating.objects.select_related('vendor', 'evaluated_by').all()

    # 필터링
    year = request.GET.get('year')
    month = request.GET.get('month')
    vendor_id = request.GET.get('vendor')
    grade = request.GET.get('grade')

    if year:
        qs = qs.filter(year=int(year))
    if month:
        qs = qs.filter(month=int(month))
    if vendor_id:
        qs = qs.filter(vendor_id=vendor_id)
    if grade:
        qs = qs.filter(grade=grade)

    # 페이징
    paginator = Paginator(qs, 20)
    page = request.GET.get('page')
    ratings = paginator.get_page(page)

    # 연도/월 선택 옵션
    current_year = timezone.localdate().year
    years = list(range(current_year - 2, current_year + 1))
    months = list(range(1, 13))

    vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')

    return render(request, 'qms/vendor_rating_list.html', {
        'ratings': ratings,
        'years': years,
        'months': months,
        'grade_choices': VendorRating.GRADE_CHOICES,
        'vendors': vendors,
    })


@login_required
def vendor_rating_create(request):
    """협력사 평가 등록/계산"""
    if request.method == 'POST':
        vendor_id = request.POST.get('vendor')
        year = int(request.POST.get('year'))
        month = int(request.POST.get('month'))

        # 기존 평가 확인
        rating, created = VendorRating.objects.get_or_create(
            vendor_id=vendor_id, year=year, month=month,
            defaults={'evaluated_by': request.user}
        )

        # Organization과 연결된 Vendor 가져오기
        vendor = Organization.objects.get(pk=vendor_id)
        linked_vendor = vendor.linked_vendor  # Organization -> Vendor 연결

        from django.db.models import Count, Sum
        from orders.models import Order, Incoming

        # ====================================
        # 1. 수입검사 합격률 집계 (ImportInspection)
        # ====================================
        if linked_vendor:
            import_stats = ImportInspection.objects.filter(
                inbound_transaction__part__vendor=linked_vendor,
                inbound_transaction__date__year=year,
                inbound_transaction__date__month=month,
            ).aggregate(
                total=Count('id'),
                approved=Count('id', filter=Q(status='APPROVED')),
                rejected=Count('id', filter=Q(status='REJECTED')),
                total_qty=Sum('inbound_transaction__quantity'),
                defect_qty=Sum('qty_bad'),  # 불량수량
            )
        else:
            import_stats = {'total': 0, 'approved': 0, 'rejected': 0, 'total_qty': 0, 'defect_qty': 0}

        rating.incoming_total = import_stats['total'] or 0
        rating.incoming_pass = import_stats['approved'] or 0
        rating.incoming_fail = import_stats['rejected'] or 0
        rating.incoming_rate = (rating.incoming_pass / rating.incoming_total * 100) if rating.incoming_total > 0 else 100

        # ====================================
        # 2. PPM 계산
        # ====================================
        rating.total_incoming_qty = import_stats.get('total_qty') or 0
        rating.defect_qty = import_stats.get('defect_qty') or 0
        if rating.total_incoming_qty > 0:
            rating.ppm = (rating.defect_qty / rating.total_incoming_qty) * 1000000
        else:
            rating.ppm = 0

        # ====================================
        # 3. 납기준수율 집계 (Order + Incoming 비교)
        # ====================================
        if linked_vendor:
            # 해당 월에 입고된 건 조회
            incoming_list = Incoming.objects.filter(
                part__vendor=linked_vendor,
                in_date__year=year,
                in_date__month=month,
            ).select_related('part')

            delivery_total = 0
            delivery_ontime = 0
            delivery_late = 0

            for inc in incoming_list:
                delivery_total += 1
                # 연결된 발주의 납기일 찾기
                order = Order.objects.filter(
                    vendor=linked_vendor,
                    part_no=inc.part.part_no,
                    due_date__lte=inc.in_date,  # 납기일 이전 또는 당일
                ).order_by('-due_date').first()

                if order:
                    if inc.in_date <= order.due_date:
                        delivery_ontime += 1
                    else:
                        delivery_late += 1
                else:
                    # 발주 없으면 정시로 간주
                    delivery_ontime += 1

            rating.delivery_total = delivery_total
            rating.delivery_ontime = delivery_ontime
            rating.delivery_late = delivery_late
            rating.delivery_rate = (delivery_ontime / delivery_total * 100) if delivery_total > 0 else 100
        else:
            rating.delivery_total = 0
            rating.delivery_ontime = 0
            rating.delivery_late = 0
            rating.delivery_rate = 100

        # ====================================
        # 4. 클레임 집계
        # ====================================
        claim_stats = VendorClaim.objects.filter(
            vendor_id=vendor_id,
            issue_date__year=year,
            issue_date__month=month,
        ).aggregate(
            count=Count('id'),
            total_claim=Sum('claim_amount'),  # 클레임 청구금액
            total_compensation=Sum('compensation_amount'),  # 실제 보상금액
        )
        rating.claim_count = claim_stats['count'] or 0
        # 보상금액이 있으면 보상금액, 없으면 클레임금액 사용
        rating.claim_amount = claim_stats['total_compensation'] or claim_stats['total_claim'] or 0

        # ====================================
        # 5. 점수 계산 및 저장
        # ====================================
        rating.calculate_scores()
        rating.evaluated_by = request.user
        rating.evaluated_at = timezone.now()
        rating.save()

        if created:
            messages.success(request, f'{vendor.name}의 {year}년 {month}월 평가가 생성되었습니다.')
        else:
            messages.success(request, f'{vendor.name}의 {year}년 {month}월 평가가 갱신되었습니다.')

        return redirect('qms:rating_detail', pk=rating.pk)

    current = timezone.localdate()
    vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')

    return render(request, 'qms/vendor_rating_form.html', {
        'mode': 'create',
        'vendors': vendors,
        'current_year': current.year,
        'current_month': current.month,
        'years': list(range(current.year - 2, current.year + 1)),
        'months': list(range(1, 13)),
    })


@login_required
def vendor_rating_detail(request, pk):
    """협력사 평가 상세"""
    rating = get_object_or_404(VendorRating.objects.select_related('vendor', 'evaluated_by'), pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'update':
            # 수동 점수 조정
            rating.quality_score = float(request.POST.get('quality_score', 0))
            rating.delivery_score = float(request.POST.get('delivery_score', 0))
            rating.remark = request.POST.get('remark', '')

            # 종합점수 재계산
            rating.total_score = (rating.quality_score * 0.6) + (rating.delivery_score * 0.4)
            if rating.total_score >= 95:
                rating.grade = 'A'
            elif rating.total_score >= 85:
                rating.grade = 'B'
            elif rating.total_score >= 70:
                rating.grade = 'C'
            else:
                rating.grade = 'D'

            rating.save()
            messages.success(request, '평가 정보가 업데이트되었습니다.')

        elif action == 'recalculate':
            # 데이터 재집계
            from django.db.models import Count, Sum
            from orders.models import Order, Incoming

            vendor = rating.vendor
            linked_vendor = vendor.linked_vendor
            year = rating.year
            month = rating.month

            # 1. 수입검사 합격률 집계
            if linked_vendor:
                import_stats = ImportInspection.objects.filter(
                    inbound_transaction__part__vendor=linked_vendor,
                    inbound_transaction__date__year=year,
                    inbound_transaction__date__month=month,
                ).aggregate(
                    total=Count('id'),
                    approved=Count('id', filter=Q(status='APPROVED')),
                    rejected=Count('id', filter=Q(status='REJECTED')),
                    total_qty=Sum('inbound_transaction__quantity'),
                    defect_qty=Sum('qty_bad'),
                )
            else:
                import_stats = {'total': 0, 'approved': 0, 'rejected': 0, 'total_qty': 0, 'defect_qty': 0}

            rating.incoming_total = import_stats['total'] or 0
            rating.incoming_pass = import_stats['approved'] or 0
            rating.incoming_fail = import_stats['rejected'] or 0
            rating.incoming_rate = (rating.incoming_pass / rating.incoming_total * 100) if rating.incoming_total > 0 else 100

            # 2. PPM 계산
            rating.total_incoming_qty = import_stats.get('total_qty') or 0
            rating.defect_qty = import_stats.get('defect_qty') or 0
            if rating.total_incoming_qty > 0:
                rating.ppm = (rating.defect_qty / rating.total_incoming_qty) * 1000000
            else:
                rating.ppm = 0

            # 3. 납기준수율 집계
            if linked_vendor:
                incoming_list = Incoming.objects.filter(
                    part__vendor=linked_vendor,
                    in_date__year=year,
                    in_date__month=month,
                ).select_related('part')

                delivery_total = 0
                delivery_ontime = 0
                delivery_late = 0

                for inc in incoming_list:
                    delivery_total += 1
                    order = Order.objects.filter(
                        vendor=linked_vendor,
                        part_no=inc.part.part_no,
                        due_date__lte=inc.in_date,
                    ).order_by('-due_date').first()

                    if order:
                        if inc.in_date <= order.due_date:
                            delivery_ontime += 1
                        else:
                            delivery_late += 1
                    else:
                        delivery_ontime += 1

                rating.delivery_total = delivery_total
                rating.delivery_ontime = delivery_ontime
                rating.delivery_late = delivery_late
                rating.delivery_rate = (delivery_ontime / delivery_total * 100) if delivery_total > 0 else 100
            else:
                rating.delivery_total = 0
                rating.delivery_ontime = 0
                rating.delivery_late = 0
                rating.delivery_rate = 100

            # 4. 클레임 집계
            claim_stats = VendorClaim.objects.filter(
                vendor_id=vendor.id,
                issue_date__year=year,
                issue_date__month=month,
            ).aggregate(
                count=Count('id'),
                total_claim=Sum('claim_amount'),
                total_compensation=Sum('compensation_amount'),
            )
            rating.claim_count = claim_stats['count'] or 0
            rating.claim_amount = claim_stats['total_compensation'] or claim_stats['total_claim'] or 0

            # 5. 점수 계산 및 저장
            rating.calculate_scores()
            rating.evaluated_by = request.user
            rating.evaluated_at = timezone.now()
            rating.save()

            messages.success(request, f'{vendor.name}의 {year}년 {month}월 평가가 재계산되었습니다.')
            return redirect('qms:rating_detail', pk=rating.pk)

    # 해당 월 클레임 목록 조회
    claims = VendorClaim.objects.filter(
        vendor=rating.vendor,
        issue_date__year=rating.year,
        issue_date__month=rating.month,
    ).order_by('-issue_date')

    return render(request, 'qms/vendor_rating_detail.html', {
        'rating': rating,
        'claims': claims,
        'grade_choices': VendorRating.GRADE_CHOICES,
    })


# ============================================================================
# QMS 대시보드
# ============================================================================

@login_required
def qms_dashboard(request):
    """QMS 통합 대시보드"""
    today = timezone.localdate()

    # 수입검사 통계
    import_stats = {
        'pending': ImportInspection.objects.filter(status='PENDING').count(),
        'today': ImportInspection.objects.filter(created_at__date=today).count(),
    }

    # 출하검사 통계
    outgoing_stats = {
        'pending': OutgoingInspection.objects.filter(status='PENDING').count(),
        'today': OutgoingInspection.objects.filter(inspection_date=today).count(),
    }

    # 부적합 통계
    nc_stats = {
        'open': NonConformance.objects.filter(status='OPEN').count(),
        'action': NonConformance.objects.filter(status='ACTION').count(),
        'this_month': NonConformance.objects.filter(
            occurred_date__year=today.year, occurred_date__month=today.month
        ).count(),
    }

    # CAPA 통계
    capa_stats = {
        'requested': CorrectiveAction.objects.filter(status='REQUESTED').count(),
        'overdue': CorrectiveAction.objects.filter(
            due_date__lt=today
        ).exclude(status__in=['CLOSED', 'VERIFYING']).count(),
    }

    # 클레임 통계
    claim_stats = {
        'processing': VendorClaim.objects.filter(status='PROCESSING').count(),
        'this_month': VendorClaim.objects.filter(
            issue_date__year=today.year, issue_date__month=today.month
        ).count(),
    }

    # 최근 부적합 목록
    recent_ncs = NonConformance.objects.select_related('vendor').order_by('-occurred_date')[:5]

    # 기한 초과 CAPA
    overdue_capas = CorrectiveAction.objects.select_related('vendor').filter(
        due_date__lt=today
    ).exclude(status__in=['CLOSED', 'VERIFYING']).order_by('due_date')[:5]

    # 4M 변경 통계
    change_stats = {
        'review': ChangeRequest.objects.filter(phase='REVIEW').count(),
        'formal': ChangeRequest.objects.filter(phase='FORMAL').count(),
    }

    # ISIR 통계
    isir_stats = {
        'draft': ISIR.objects.filter(status='DRAFT').count(),
        'reviewing': ISIR.objects.filter(status='REVIEWING').count(),
        'pending_approval': ISIR.objects.filter(status__in=['SUBMITTED', 'REVIEWING']).count(),
    }

    return render(request, 'qms/dashboard.html', {
        'import_stats': import_stats,
        'outgoing_stats': outgoing_stats,
        'nc_stats': nc_stats,
        'capa_stats': capa_stats,
        'claim_stats': claim_stats,
        'change_stats': change_stats,
        'isir_stats': isir_stats,
        'recent_ncs': recent_ncs,
        'overdue_capas': overdue_capas,
    })


# ============================================================================
# ISIR (Initial Sample Inspection Report) - 초도품 검사
# ============================================================================

@login_required
def isir_list(request):
    """ISIR 목록"""
    qs = ISIR.objects.select_related('vendor', 'inspector', 'approved_by').all()

    # 필터링
    status = request.GET.get('status')
    isir_type = request.GET.get('isir_type')
    vendor_id = request.GET.get('vendor')
    part_no = request.GET.get('part_no', '').strip()

    if status:
        qs = qs.filter(status=status)
    if isir_type:
        qs = qs.filter(isir_type=isir_type)
    if vendor_id:
        qs = qs.filter(vendor_id=vendor_id)
    if part_no:
        qs = qs.filter(part_no__icontains=part_no)

    qs = qs.order_by('-created_at')

    # 페이징
    paginator = Paginator(qs, 20)
    page = request.GET.get('page')
    isirs = paginator.get_page(page)

    # 통계
    stats = {
        'total': ISIR.objects.count(),
        'draft': ISIR.objects.filter(status='DRAFT').count(),
        'reviewing': ISIR.objects.filter(status='REVIEWING').count(),
        'approved': ISIR.objects.filter(status='APPROVED').count(),
    }

    vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')

    return render(request, 'qms/isir_list.html', {
        'isirs': isirs,
        'stats': stats,
        'status_choices': ISIR.STATUS_CHOICES,
        'type_choices': ISIR.ISIR_TYPE_CHOICES,
        'vendors': vendors,
    })


@login_required
def isir_create(request):
    """ISIR 등록"""
    if request.method == 'POST':
        isir = ISIR(
            isir_type=request.POST.get('isir_type'),
            vendor_id=request.POST.get('vendor'),
            part_no=request.POST.get('part_no'),
            part_name=request.POST.get('part_name'),
            part_rev=request.POST.get('part_rev', ''),
            drawing_no=request.POST.get('drawing_no', ''),
            sample_qty=int(request.POST.get('sample_qty', 5)),
            sample_lot=request.POST.get('sample_lot', ''),
            created_by=request.user,
        )

        sample_received = request.POST.get('sample_received_date')
        if sample_received:
            isir.sample_received_date = sample_received

        if request.FILES.get('report_file'):
            isir.report_file = request.FILES['report_file']
        if request.FILES.get('photo1'):
            isir.photo1 = request.FILES['photo1']
        if request.FILES.get('photo2'):
            isir.photo2 = request.FILES['photo2']

        # SPC 데이터 (선택사항)
        cpk_value = request.POST.get('cpk_value')
        if cpk_value:
            isir.cpk_value = cpk_value
        ppk_value = request.POST.get('ppk_value')
        if ppk_value:
            isir.ppk_value = ppk_value
        isir.cpk_characteristic = request.POST.get('cpk_characteristic', '')
        usl = request.POST.get('usl')
        if usl:
            isir.usl = usl
        lsl = request.POST.get('lsl')
        if lsl:
            isir.lsl = lsl
        process_mean = request.POST.get('process_mean')
        if process_mean:
            isir.process_mean = process_mean
        process_std = request.POST.get('process_std')
        if process_std:
            isir.process_std = process_std

        # MSA 데이터 (선택사항)
        msa_grr = request.POST.get('msa_grr_percent')
        if msa_grr:
            isir.msa_grr_percent = msa_grr
        msa_ndc = request.POST.get('msa_ndc')
        if msa_ndc:
            isir.msa_ndc = msa_ndc
        isir.msa_result = request.POST.get('msa_result', '')

        isir.save()
        messages.success(request, f'ISIR {isir.isir_no}가 등록되었습니다.')
        return redirect('qms:isir_detail', pk=isir.pk)

    vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')
    return render(request, 'qms/isir_form.html', {
        'mode': 'create',
        'type_choices': ISIR.ISIR_TYPE_CHOICES,
        'vendors': vendors,
    })


@login_required
def isir_detail(request, pk):
    """ISIR 상세"""
    isir = get_object_or_404(
        ISIR.objects.select_related('vendor', 'inspector', 'approved_by', 'created_by').prefetch_related('items', 'attachments'),
        pk=pk
    )

    # 체크리스트 조회 (없으면 None)
    try:
        checklist = isir.checklist
    except ISIRChecklist.DoesNotExist:
        checklist = None

    # PPAP 18개 요소 (번호, 필드명, 한글명, 영문명, DB필드명)
    ppap_elements = [
        (1, 'design_records', '설계기록 (도면)', 'Design Records', 'elem_01_design_records'),
        (2, 'ecn', '설계변경문서', 'Engineering Change Documents', 'elem_02_ecn'),
        (3, 'customer_approval', '고객 기술승인', 'Customer Engineering Approval', 'elem_03_customer_approval'),
        (4, 'dfmea', '설계 FMEA', 'Design FMEA', 'elem_04_dfmea'),
        (5, 'process_flow', '공정흐름도', 'Process Flow Diagram', 'elem_05_process_flow'),
        (6, 'pfmea', '공정 FMEA', 'Process FMEA', 'elem_06_pfmea'),
        (7, 'control_plan', '관리계획서', 'Control Plan', 'elem_07_control_plan'),
        (8, 'msa', '측정시스템분석', 'Measurement System Analysis', 'elem_08_msa'),
        (9, 'dimensional', '치수검사결과', 'Dimensional Results', 'elem_09_dimensional'),
        (10, 'material_test', '재료/성능시험', 'Material/Performance Test', 'elem_10_material_test'),
        (11, 'initial_process', '초기공정연구 (Cpk)', 'Initial Process Studies', 'elem_11_initial_process'),
        (12, 'lab_doc', '공인시험성적서', 'Qualified Laboratory Documentation', 'elem_12_lab_doc'),
        (13, 'aar', '외관승인보고서', 'Appearance Approval Report', 'elem_13_aar'),
        (14, 'sample', '샘플 제품', 'Sample Production Parts', 'elem_14_sample'),
        (15, 'master_sample', '마스터 샘플', 'Master Sample', 'elem_15_master_sample'),
        (16, 'checking_aids', '검사 보조기구', 'Checking Aids', 'elem_16_checking_aids'),
        (17, 'csr', '고객별 요구사항', 'Customer-Specific Requirements', 'elem_17_csr'),
        (18, 'psw', '부품제출보증서', 'Part Submission Warrant', 'elem_18_psw'),
    ]

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'submit':
            isir.status = 'SUBMITTED'
            isir.save()
            messages.success(request, 'ISIR이 검토 제출되었습니다.')

        elif action == 'start_review':
            isir.status = 'REVIEWING'
            isir.inspector = request.user
            isir.inspection_date = timezone.localdate()
            isir.save()
            messages.success(request, 'ISIR 검토가 시작되었습니다.')

        elif action == 'update_inspection':
            isir.dimension_result = request.POST.get('dimension_result', '')
            isir.appearance_result = request.POST.get('appearance_result', '')
            isir.function_result = request.POST.get('function_result', '')
            isir.material_result = request.POST.get('material_result', '')
            isir.inspection_detail = request.POST.get('inspection_detail', '')
            isir.issue_found = request.POST.get('issue_found', '')
            isir.corrective_action = request.POST.get('corrective_action', '')
            isir.save()
            messages.success(request, '검사 결과가 저장되었습니다.')

        elif action == 'update_spc':
            # SPC/Cpk 데이터 업데이트
            isir.cpk_value = request.POST.get('cpk_value') or None
            isir.ppk_value = request.POST.get('ppk_value') or None
            isir.cpk_characteristic = request.POST.get('cpk_characteristic', '')
            isir.spc_sample_size = request.POST.get('spc_sample_size') or None
            isir.process_mean = request.POST.get('process_mean') or None
            isir.process_std = request.POST.get('process_std') or None
            isir.usl = request.POST.get('usl') or None
            isir.lsl = request.POST.get('lsl') or None
            isir.save()
            messages.success(request, '공정능력 데이터가 저장되었습니다.')

        elif action == 'update_msa':
            # MSA 데이터 업데이트
            isir.msa_grr_percent = request.POST.get('msa_grr_percent') or None
            isir.msa_ndc = request.POST.get('msa_ndc') or None
            isir.msa_result = request.POST.get('msa_result', '')
            isir.save()
            messages.success(request, 'MSA 데이터가 저장되었습니다.')

        elif action == 'update_checklist':
            # PPAP 체크리스트 업데이트 (없으면 생성)
            checklist, created = ISIRChecklist.objects.get_or_create(isir=isir)
            for elem in ppap_elements:
                field_name = elem[4]
                value = request.POST.get(field_name, 'NOT_SUBMITTED')
                setattr(checklist, field_name, value)
            checklist.remark = request.POST.get('checklist_remark', '')
            checklist.save()
            messages.success(request, 'PPAP 체크리스트가 저장되었습니다.')

        elif action == 'upload_attachment':
            # 첨부파일 업로드
            file = request.FILES.get('file')
            if file:
                ISIRAttachment.objects.create(
                    isir=isir,
                    file_type=request.POST.get('file_type', 'OTHER'),
                    file=file,
                    file_name=file.name,
                    description=request.POST.get('description', ''),
                    uploaded_by=request.user,
                )
                messages.success(request, '파일이 업로드되었습니다.')

        elif action == 'delete_attachment':
            # 첨부파일 삭제
            att_id = request.POST.get('attachment_id')
            ISIRAttachment.objects.filter(pk=att_id, isir=isir).delete()
            messages.success(request, '파일이 삭제되었습니다.')

        elif action == 'approve':
            overall = request.POST.get('overall_result')
            isir.overall_result = overall
            isir.approval_remark = request.POST.get('approval_remark', '')
            isir.approved_by = request.user
            isir.approved_date = timezone.localdate()
            if overall == 'PASS':
                isir.status = 'APPROVED'
            elif overall == 'CONDITIONAL':
                isir.status = 'CONDITIONAL'
            else:
                isir.status = 'REJECTED'
            isir.save()
            messages.success(request, f'ISIR이 {isir.get_status_display()} 처리되었습니다.')

        elif action == 'add_item':
            item_no = ISIRItem.objects.filter(isir=isir).count() + 1
            ISIRItem.objects.create(
                isir=isir,
                item_no=item_no,
                item_name=request.POST.get('item_name'),
                specification=request.POST.get('specification'),
                tolerance=request.POST.get('tolerance', ''),
                unit=request.POST.get('unit', ''),
                measured_1=request.POST.get('measured_1', ''),
                measured_2=request.POST.get('measured_2', ''),
                measured_3=request.POST.get('measured_3', ''),
                measured_4=request.POST.get('measured_4', ''),
                measured_5=request.POST.get('measured_5', ''),
                result=request.POST.get('result', 'PASS'),
                remark=request.POST.get('remark', ''),
            )
            messages.success(request, '검사 항목이 추가되었습니다.')

        elif action == 'delete_item':
            item_id = request.POST.get('item_id')
            ISIRItem.objects.filter(pk=item_id, isir=isir).delete()
            messages.success(request, '검사 항목이 삭제되었습니다.')

        return redirect('qms:isir_detail', pk=pk)

    # 체크리스트 다시 조회 (POST 처리 후 생성되었을 수 있음)
    try:
        checklist = isir.checklist
    except ISIRChecklist.DoesNotExist:
        checklist = None

    return render(request, 'qms/isir_detail.html', {
        'isir': isir,
        'items': isir.items.all(),
        'attachments': isir.attachments.all(),
        'checklist': checklist,
        'ppap_elements': ppap_elements,
        'result_choices': [('PASS', '합격'), ('FAIL', '불합격'), ('NA', 'N/A')],
        'overall_choices': [('PASS', '합격'), ('FAIL', '불합격'), ('CONDITIONAL', '조건부')],
    })


@login_required
def isir_pdf(request, pk):
    """ISIR PDF 출력"""
    from django.http import HttpResponse
    from django.template.loader import render_to_string

    isir = get_object_or_404(
        ISIR.objects.select_related('vendor', 'approved_by', 'created_by').prefetch_related('items', 'attachments'),
        pk=pk
    )

    try:
        checklist = isir.checklist
    except ISIRChecklist.DoesNotExist:
        checklist = None

    # PPAP 18 요소 리스트
    ppap_elements = [
        {'name': '설계기록', 'checked': checklist.elem_01_design_records == 'SUBMITTED' if checklist else False},
        {'name': '기술변경문서', 'checked': checklist.elem_02_ecn == 'SUBMITTED' if checklist else False},
        {'name': '고객 기술승인', 'checked': checklist.elem_03_customer_approval == 'SUBMITTED' if checklist else False},
        {'name': '설계 FMEA', 'checked': checklist.elem_04_dfmea == 'SUBMITTED' if checklist else False},
        {'name': '공정흐름도', 'checked': checklist.elem_05_process_flow == 'SUBMITTED' if checklist else False},
        {'name': '공정 FMEA', 'checked': checklist.elem_06_pfmea == 'SUBMITTED' if checklist else False},
        {'name': '관리계획서', 'checked': checklist.elem_07_control_plan == 'SUBMITTED' if checklist else False},
        {'name': 'MSA 연구', 'checked': checklist.elem_08_msa == 'SUBMITTED' if checklist else False},
        {'name': '치수검사결과', 'checked': checklist.elem_09_dimensional == 'SUBMITTED' if checklist else False},
        {'name': '재료/성능시험', 'checked': checklist.elem_10_material_test == 'SUBMITTED' if checklist else False},
        {'name': '초기공정연구', 'checked': checklist.elem_11_initial_process == 'SUBMITTED' if checklist else False},
        {'name': '공인시험성적서', 'checked': checklist.elem_12_lab_doc == 'SUBMITTED' if checklist else False},
        {'name': '외관승인보고서', 'checked': checklist.elem_13_aar == 'SUBMITTED' if checklist else False},
        {'name': '샘플 제품', 'checked': checklist.elem_14_sample == 'SUBMITTED' if checklist else False},
        {'name': '마스터 샘플', 'checked': checklist.elem_15_master_sample == 'SUBMITTED' if checklist else False},
        {'name': '검사보조구', 'checked': checklist.elem_16_checking_aids == 'SUBMITTED' if checklist else False},
        {'name': '고객특정요구', 'checked': checklist.elem_17_csr == 'SUBMITTED' if checklist else False},
        {'name': 'PSW (보증서)', 'checked': checklist.elem_18_psw == 'SUBMITTED' if checklist else False},
    ]

    # HTML 템플릿 렌더링
    html_content = render_to_string('qms/isir_pdf.html', {
        'isir': isir,
        'items': isir.items.all(),
        'checklist': checklist,
        'ppap_elements': ppap_elements,
        'attachments': isir.attachments.all(),
    })

    # HTML로 반환 (브라우저에서 인쇄 가능)
    response = HttpResponse(html_content, content_type='text/html; charset=utf-8')
    return response


# ============================================================================
# VOC 관리 (Voice of Customer)
# ============================================================================

@login_required
def voc_list(request):
    """VOC 목록"""
    qs = VOC.objects.select_related('linked_vendor', 'received_by', 'assigned_to').all()

    # 필터링
    status = request.GET.get('status')
    severity = request.GET.get('severity')
    source = request.GET.get('source')
    q = request.GET.get('q', '').strip()
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')

    if status:
        qs = qs.filter(status=status)
    if severity:
        qs = qs.filter(severity=severity)
    if source:
        qs = qs.filter(source=source)
    if q:
        qs = qs.filter(
            Q(voc_no__icontains=q) |
            Q(customer_name__icontains=q) |
            Q(part_no__icontains=q) |
            Q(part_name__icontains=q)
        )
    if date_from:
        qs = qs.filter(received_date__gte=date_from)
    if date_to:
        qs = qs.filter(received_date__lte=date_to)

    qs = qs.order_by('-received_date', '-id')

    # 페이징
    paginator = Paginator(qs, 20)
    page = request.GET.get('page')
    vocs = paginator.get_page(page)

    # 통계
    today = timezone.localdate()
    stats = {
        'total': VOC.objects.count(),
        'open': VOC.objects.exclude(status='CLOSED').count(),
        'critical': VOC.objects.filter(severity='CRITICAL').exclude(status='CLOSED').count(),
        'this_month': VOC.objects.filter(
            received_date__year=today.year, received_date__month=today.month
        ).count(),
    }

    return render(request, 'qms/voc_list.html', {
        'vocs': vocs,
        'stats': stats,
        'status_choices': VOC.STATUS_CHOICES,
        'severity_choices': VOC.SEVERITY_CHOICES,
        'source_choices': VOC.SOURCE_CHOICES,
    })


@login_required
def voc_create(request):
    """VOC 등록"""
    if request.method == 'POST':
        voc = VOC(
            source=request.POST.get('source'),
            severity=request.POST.get('severity'),
            received_date=request.POST.get('received_date'),
            customer_name=request.POST.get('customer_name'),
            customer_contact=request.POST.get('customer_contact', ''),
            customer_phone=request.POST.get('customer_phone', ''),
            part_no=request.POST.get('part_no'),
            part_name=request.POST.get('part_name'),
            lot_no=request.POST.get('lot_no', ''),
            defect_qty=int(request.POST.get('defect_qty', 0)),
            defect_type=request.POST.get('defect_type'),
            defect_detail=request.POST.get('defect_detail'),
            received_by=request.user,
        )

        due_date = request.POST.get('due_date')
        if due_date:
            voc.due_date = due_date

        assigned_to = request.POST.get('assigned_to')
        if assigned_to:
            voc.assigned_to_id = assigned_to

        if request.FILES.get('photo'):
            voc.photo = request.FILES['photo']

        voc.save()
        messages.success(request, f'VOC {voc.voc_no}가 등록되었습니다.')
        return redirect('qms:voc_detail', pk=voc.pk)

    from django.contrib.auth.models import User
    users = User.objects.filter(is_active=True).order_by('username')

    return render(request, 'qms/voc_form.html', {
        'mode': 'create',
        'severity_choices': VOC.SEVERITY_CHOICES,
        'source_choices': VOC.SOURCE_CHOICES,
        'users': users,
        'today': timezone.localdate(),
    })


@login_required
def voc_detail(request, pk):
    """VOC 상세"""
    voc = get_object_or_404(
        VOC.objects.select_related('linked_vendor', 'linked_claim', 'received_by', 'assigned_to'),
        pk=pk
    )

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'update_analysis':
            # 원인 분석 업데이트
            voc.cause_analysis = request.POST.get('cause_analysis', '')
            voc.root_cause = request.POST.get('root_cause', '')
            voc.responsible_type = request.POST.get('responsible_type', '')

            linked_vendor_id = request.POST.get('linked_vendor')
            if linked_vendor_id:
                voc.linked_vendor_id = linked_vendor_id

            voc.status = 'ANALYZING'
            voc.save()
            messages.success(request, '원인분석이 저장되었습니다.')

        elif action == 'update_action':
            # 대책 업데이트
            voc.immediate_action = request.POST.get('immediate_action', '')
            voc.corrective_action = request.POST.get('corrective_action', '')
            voc.preventive_action = request.POST.get('preventive_action', '')
            voc.status = 'ACTION'
            voc.save()
            messages.success(request, '대책이 저장되었습니다.')

        elif action == 'verify':
            # 효과검증
            voc.verification_result = request.POST.get('verification_result', '')
            voc.is_effective = request.POST.get('is_effective') == 'true'
            voc.status = 'VERIFY'
            voc.save()
            messages.success(request, '효과검증이 저장되었습니다.')

        elif action == 'close':
            # 완료
            voc.status = 'CLOSED'
            voc.closed_date = timezone.localdate()
            voc.save()
            messages.success(request, 'VOC가 완료 처리되었습니다.')

        elif action == 'create_claim':
            # 협력사 클레임 생성 연계
            if voc.linked_vendor:
                claim = VendorClaim(
                    claim_type='QUALITY',
                    issue_date=voc.received_date,
                    vendor=voc.linked_vendor,
                    part_no=voc.part_no,
                    part_name=voc.part_name,
                    lot_no=voc.lot_no,
                    claim_qty=voc.defect_qty,
                    claim_detail=f"[VOC 연계] {voc.voc_no}\n\n{voc.defect_detail}",
                    issued_by=request.user,
                    issued_date=timezone.localdate(),
                    status='ISSUED',
                )
                claim.save()
                voc.linked_claim = claim
                voc.save()
                messages.success(request, f'협력사 클레임 {claim.claim_no}가 생성되었습니다.')

        return redirect('qms:voc_detail', pk=pk)

    vendors = Organization.objects.filter(org_type='VENDOR').order_by('name')

    return render(request, 'qms/voc_detail.html', {
        'voc': voc,
        'attachments': voc.attachments.all(),
        'status_choices': VOC.STATUS_CHOICES,
        'severity_choices': VOC.SEVERITY_CHOICES,
        'vendors': vendors,
    })


# ============================================================================
# 계측기 관리 (Gauge Management)
# ============================================================================

@login_required
def gauge_list(request):
    """계측기 목록"""
    qs = Gauge.objects.select_related('manager').all()

    # 필터링
    status = request.GET.get('status')
    gauge_type = request.GET.get('gauge_type')
    location = request.GET.get('location')
    q = request.GET.get('q', '').strip()
    calibration_due = request.GET.get('calibration_due')

    if status:
        qs = qs.filter(status=status)
    if gauge_type:
        qs = qs.filter(gauge_type=gauge_type)
    if location:
        qs = qs.filter(location__icontains=location)
    if q:
        qs = qs.filter(
            Q(gauge_no__icontains=q) |
            Q(gauge_name__icontains=q) |
            Q(serial_no__icontains=q)
        )
    if calibration_due == 'overdue':
        qs = qs.filter(next_calibration_date__lt=timezone.localdate())
    elif calibration_due == 'soon':
        today = timezone.localdate()
        qs = qs.filter(
            next_calibration_date__gte=today,
            next_calibration_date__lte=today + timezone.timedelta(days=30)
        )

    qs = qs.order_by('next_calibration_date', 'gauge_no')

    # 페이징
    paginator = Paginator(qs, 20)
    page = request.GET.get('page')
    gauges = paginator.get_page(page)

    # 통계
    today = timezone.localdate()
    stats = {
        'total': Gauge.objects.filter(status='ACTIVE').count(),
        'overdue': Gauge.objects.filter(
            status='ACTIVE', next_calibration_date__lt=today
        ).count(),
        'due_soon': Gauge.objects.filter(
            status='ACTIVE',
            next_calibration_date__gte=today,
            next_calibration_date__lte=today + timezone.timedelta(days=30)
        ).count(),
    }

    # 위치 목록 (필터용)
    locations = Gauge.objects.values_list('location', flat=True).distinct().order_by('location')

    return render(request, 'qms/gauge_list.html', {
        'gauges': gauges,
        'stats': stats,
        'status_choices': Gauge.STATUS_CHOICES,
        'type_choices': Gauge.TYPE_CHOICES,
        'locations': [l for l in locations if l],
    })


@login_required
def gauge_create(request):
    """계측기 등록"""
    if request.method == 'POST':
        gauge = Gauge(
            gauge_no=request.POST.get('gauge_no'),
            gauge_name=request.POST.get('gauge_name'),
            gauge_type=request.POST.get('gauge_type'),
            manufacturer=request.POST.get('manufacturer', ''),
            model_no=request.POST.get('model_no', ''),
            serial_no=request.POST.get('serial_no', ''),
            measurement_range=request.POST.get('measurement_range', ''),
            resolution=request.POST.get('resolution', ''),
            accuracy=request.POST.get('accuracy', ''),
            location=request.POST.get('location', ''),
            department=request.POST.get('department', ''),
            usage=request.POST.get('usage', ''),
            calibration_type=request.POST.get('calibration_type', 'EXTERNAL'),
            calibration_cycle=int(request.POST.get('calibration_cycle', 12)),
            calibration_agency=request.POST.get('calibration_agency', ''),
            remark=request.POST.get('remark', ''),
        )

        purchase_date = request.POST.get('purchase_date')
        if purchase_date:
            gauge.purchase_date = purchase_date

        purchase_cost = request.POST.get('purchase_cost')
        if purchase_cost:
            gauge.purchase_cost = purchase_cost

        manager_id = request.POST.get('manager')
        if manager_id:
            gauge.manager_id = manager_id

        if request.FILES.get('photo'):
            gauge.photo = request.FILES['photo']
        if request.FILES.get('manual'):
            gauge.manual = request.FILES['manual']

        gauge.save()
        messages.success(request, f'계측기 {gauge.gauge_no}가 등록되었습니다.')
        return redirect('qms:gauge_detail', pk=gauge.pk)

    from django.contrib.auth.models import User
    users = User.objects.filter(is_active=True).order_by('username')

    return render(request, 'qms/gauge_form.html', {
        'mode': 'create',
        'type_choices': Gauge.TYPE_CHOICES,
        'calibration_type_choices': Gauge.CALIBRATION_TYPE_CHOICES,
        'users': users,
    })


@login_required
def gauge_detail(request, pk):
    """계측기 상세"""
    gauge = get_object_or_404(Gauge.objects.select_related('manager'), pk=pk)
    calibrations = gauge.calibrations.all()[:10]

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'update':
            # 기본 정보 업데이트
            gauge.gauge_name = request.POST.get('gauge_name', gauge.gauge_name)
            gauge.location = request.POST.get('location', gauge.location)
            gauge.department = request.POST.get('department', gauge.department)
            gauge.usage = request.POST.get('usage', gauge.usage)
            gauge.calibration_cycle = int(request.POST.get('calibration_cycle', gauge.calibration_cycle))
            gauge.calibration_agency = request.POST.get('calibration_agency', gauge.calibration_agency)
            gauge.remark = request.POST.get('remark', gauge.remark)
            gauge.status = request.POST.get('status', gauge.status)
            gauge.save()
            messages.success(request, '계측기 정보가 업데이트되었습니다.')

        elif action == 'add_calibration':
            # 교정 이력 추가
            from dateutil.relativedelta import relativedelta

            cal_date = request.POST.get('calibration_date')
            result = request.POST.get('result')

            cal = GaugeCalibration(
                gauge=gauge,
                calibration_date=cal_date,
                calibration_type=request.POST.get('calibration_type', 'EXTERNAL'),
                calibration_agency=request.POST.get('cal_agency', ''),
                result=result,
                certificate_no=request.POST.get('certificate_no', ''),
                uncertainty=request.POST.get('uncertainty', ''),
                remark=request.POST.get('cal_remark', ''),
                performed_by=request.user,
            )

            cost = request.POST.get('cost')
            if cost:
                cal.cost = cost

            # 차기 교정일 계산
            if result in ('PASS', 'ADJUSTED'):
                import datetime
                cal_date_obj = datetime.datetime.strptime(cal_date, '%Y-%m-%d').date()
                cal.next_date = cal_date_obj + relativedelta(months=gauge.calibration_cycle)

            if request.FILES.get('certificate_file'):
                cal.certificate_file = request.FILES['certificate_file']

            cal.save()
            messages.success(request, '교정 이력이 추가되었습니다.')

        return redirect('qms:gauge_detail', pk=pk)

    return render(request, 'qms/gauge_detail.html', {
        'gauge': gauge,
        'calibrations': calibrations,
        'status_choices': Gauge.STATUS_CHOICES,
        'type_choices': Gauge.TYPE_CHOICES,
        'calibration_type_choices': Gauge.CALIBRATION_TYPE_CHOICES,
        'result_choices': GaugeCalibration.RESULT_CHOICES,
    })


# ============================================================================
# 품질문서 관리 (Quality Document Management)
# ============================================================================

@login_required
def qdoc_list(request):
    """품질문서 목록"""
    qs = QualityDocument.objects.select_related('created_by', 'approved_by').all()

    # 필터링
    status = request.GET.get('status')
    category = request.GET.get('category')
    q = request.GET.get('q', '').strip()

    if status:
        qs = qs.filter(status=status)
    if category:
        qs = qs.filter(category=category)
    if q:
        qs = qs.filter(
            Q(doc_no__icontains=q) |
            Q(doc_name__icontains=q) |
            Q(related_part_no__icontains=q)
        )

    qs = qs.order_by('category', 'doc_no')

    # 페이징
    paginator = Paginator(qs, 20)
    page = request.GET.get('page')
    documents = paginator.get_page(page)

    # 통계
    stats = {
        'total': QualityDocument.objects.filter(status='APPROVED').count(),
        'draft': QualityDocument.objects.filter(status='DRAFT').count(),
        'review': QualityDocument.objects.filter(status='REVIEW').count(),
    }

    return render(request, 'qms/qdoc_list.html', {
        'documents': documents,
        'stats': stats,
        'status_choices': QualityDocument.STATUS_CHOICES,
        'category_choices': QualityDocument.CATEGORY_CHOICES,
    })


@login_required
def qdoc_create(request):
    """품질문서 등록"""
    if request.method == 'POST':
        doc = QualityDocument(
            doc_no=request.POST.get('doc_no'),
            doc_name=request.POST.get('doc_name'),
            category=request.POST.get('category'),
            version=request.POST.get('version', '1.0'),
            description=request.POST.get('description', ''),
            related_part_no=request.POST.get('related_part_no', ''),
            related_process=request.POST.get('related_process', ''),
            effective_date=request.POST.get('effective_date'),
            is_controlled=request.POST.get('is_controlled') == 'on',
            created_by=request.user,
        )

        expiry_date = request.POST.get('expiry_date')
        if expiry_date:
            doc.expiry_date = expiry_date

        if request.FILES.get('file'):
            doc.file = request.FILES['file']
            doc.file_name = request.FILES['file'].name

        doc.save()
        messages.success(request, f'문서 {doc.doc_no}가 등록되었습니다.')
        return redirect('qms:qdoc_detail', pk=doc.pk)

    return render(request, 'qms/qdoc_form.html', {
        'mode': 'create',
        'category_choices': QualityDocument.CATEGORY_CHOICES,
        'today': timezone.localdate(),
    })


@login_required
def qdoc_detail(request, pk):
    """품질문서 상세"""
    doc = get_object_or_404(
        QualityDocument.objects.select_related('created_by', 'reviewed_by', 'approved_by'),
        pk=pk
    )
    revisions = doc.revisions.all()

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'submit_review':
            # 검토 제출
            doc.status = 'REVIEW'
            doc.save()
            messages.success(request, '문서가 검토 제출되었습니다.')

        elif action == 'approve':
            # 승인
            doc.status = 'APPROVED'
            doc.approved_by = request.user
            doc.approved_at = timezone.now()
            doc.save()
            messages.success(request, '문서가 승인되었습니다.')

        elif action == 'revise':
            # 개정
            # 1. 기존 파일 백업
            old_file = doc.file
            old_version = doc.version
            old_revision = doc.revision

            # 2. 개정이력 생성
            rev = DocumentRevision(
                document=doc,
                revision=old_revision,
                version=old_version,
                change_reason=request.POST.get('change_reason'),
                change_detail=request.POST.get('change_detail'),
                revised_by=request.user,
            )
            if old_file:
                rev.previous_file = old_file
            rev.save()

            # 3. 문서 업데이트
            doc.revision = old_revision + 1
            doc.version = request.POST.get('new_version', f'{old_revision + 1}.0')
            doc.status = 'DRAFT'
            doc.effective_date = request.POST.get('new_effective_date', timezone.localdate())

            if request.FILES.get('new_file'):
                doc.file = request.FILES['new_file']
                doc.file_name = request.FILES['new_file'].name

            doc.save()
            messages.success(request, f'문서가 Rev.{doc.revision}으로 개정되었습니다.')

        elif action == 'obsolete':
            # 폐기
            doc.status = 'OBSOLETE'
            doc.save()
            messages.success(request, '문서가 폐기 처리되었습니다.')

        return redirect('qms:qdoc_detail', pk=pk)

    return render(request, 'qms/qdoc_detail.html', {
        'doc': doc,
        'revisions': revisions,
        'status_choices': QualityDocument.STATUS_CHOICES,
        'category_choices': QualityDocument.CATEGORY_CHOICES,
    })