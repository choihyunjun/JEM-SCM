from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.contrib import messages
from django.db import models, transaction
from django.utils import timezone
import datetime

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
    scope_formal4m_queryset,   # ✅ 추가 (NameError 해결)
    can_view_m4,
    can_view_formal4m,         # ✅ 추가 (NameError 해결)
    can_edit_m4,
    can_add_internal_review,
    can_vendor_respond,
    get_or_create_vendor_review,
)

# Organization(협력사) 데이터가 비어있는 환경에서 4M 협력사 리스트가 안 뜨는 문제 방지
from orders.services import ensure_org_and_profile_sync


# 정식 4M 제출요구서류 템플릿(고정 19개) - 필요 시 추후 테이블화 가능
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
    """정식 4M 진행률/완료조건을 계산한다.

    NOTE:
    - 별도의 상태 필드를 추가하지 않고(마이그레이션 없이) '현재 입력/검토 상태' 기반으로 계산한다.
    - 완료 기준은 기본값이며, 향후 업무 규정에 맞게 조정 가능.
    """
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

    # 간단한 퍼센트(기본값): 필수서류 업로드/검토 + (FULL이면 추가 섹션) 평균
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
    """정식 4M 번호 생성 (사전 번호 기반)."""
    base = pre.request_no or f"PRE-{pre.pk}"
    candidate = f"FORMAL-{base}"
    if not Formal4MRequest.objects.filter(formal_no=candidate).exists():
        return candidate
    return f"{candidate}-{timezone.now().strftime('%H%M%S')}"


def ensure_formal4m_full(formal: Formal4MRequest) -> None:
    """정식 4M을 FULL(확장) 양식 상태로 보장한다(멱등).

    - template_type을 FULL로 설정
    - 사내승인/단계기록/일정/검토결과 기본 row를 생성
    """
    # 이미 FULL이면 그대로 두되, 누락 row만 보강할 수 있도록 아래 로직은 그대로 수행한다.
    if formal.template_type != "FULL":
        formal.template_type = "FULL"
        formal.save(update_fields=["template_type"])

    # 사내승인 row 보장
    Formal4MApproval.objects.get_or_create(formal_request=formal)

    # 단계기록 기본 row 보장
    for stage in ["ISIR", "OEM_APPROVAL", "INTERNAL_APPLY", "CUSTOMER_APPLY"]:
        Formal4MStageRecord.objects.get_or_create(formal_request=formal, stage=stage)

    # 일정 템플릿 row 보장
    existing = set(formal.schedule_items.values_list("item_name", flat=True))
    to_create = []
    for name in FORMAL_4M_SCHEDULE_TEMPLATE:
        if name not in existing:
            to_create.append(Formal4MScheduleItem(formal_request=formal, item_name=name))
    if to_create:
        Formal4MScheduleItem.objects.bulk_create(to_create)

    # 검사항목 템플릿 row 보장
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


@login_required
def m4_list(request):
    """목록: 필터링 및 검색 기능 포함"""
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


@login_required
def m4_detail(request, pk):
    """상세 페이지"""
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


@login_required
def m4_create(request):
    """요청서 작성 (기안 임시저장 - 사내/사외 구분 번호 생성 로직)"""
    actor = get_actor(request.user)

    # ✅ 설계 의도: 내부가 협력사(대상)를 선택해 신규 기안 / 협력사는 참여(회신)만
    if actor.is_vendor:
        return HttpResponseForbidden("협력사 계정은 사전 4M 신규 작성 권한이 없습니다.")

    # ✅ 협력사 리스트(Organization)가 비어있으면 폼에서 선택지가 안 뜸
    #    (Vendor 테이블만 있는 기존 데이터셋을 고려하여, 필요할 때만 최소 보정)
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
    """결재 상신 로직 (검토2 단계 지원)"""
    if request.method == "POST":
        item = get_object_or_404(M4Request, pk=pk)
        if item.user == request.user and item.status == "DRAFT":
            # 기본: 검토1 -> (있으면) 검토2 -> 최종승인
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
    """수정 및 변경 이력 기록 (원본 권한 체크 및 루프 로직 100% 복구)"""
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
    """결재 승인 로직 (검토2 단계 지원)"""
    if request.method == "POST":
        item = get_object_or_404(M4Request, pk=pk)

        # 1) 검토1
        if item.status == "PENDING_REVIEW" and request.user == item.reviewer_user:
            item.reviewed_at = timezone.now()
            item.is_reviewed = True
            if getattr(item, "reviewer_user2_id", None):
                item.status = "PENDING_REVIEW2"
            else:
                item.status = "PENDING_APPROVE"
            item.save()
            messages.success(request, "검토 승인되었습니다.")

        # 2) 검토2
        elif item.status == "PENDING_REVIEW2" and request.user == getattr(item, "reviewer_user2", None):
            item.reviewed2_at = timezone.now()
            item.is_reviewed2 = True
            item.status = "PENDING_APPROVE"
            item.save()
            messages.success(request, "검토2 승인되었습니다.")

        # 3) 최종 승인
        elif item.status == "PENDING_APPROVE" and request.user == item.approver_user:
            item.status = "APPROVED"
            item.approved_at = timezone.now()
            item.is_approved = True
            item.save()
            # ✅ 승인 완료 시 정식 4M 자동 생성
            try:
                get_or_create_formal4m(item)
                messages.success(request, "최종 승인되었습니다. (정식 4M 생성 완료)")
            except Exception:
                messages.warning(request, "최종 승인되었습니다. (정식 4M 생성은 실패했습니다)")

    return redirect("qms:m4_detail", pk=pk)


# =========================
# 정식 4M (좌측 메뉴: 목록/상세)
# =========================

@login_required
def formal4m_list(request):
    """정식 4M 목록 (좌측 메뉴용)"""
    actor = get_actor(request.user)
    qs = Formal4MRequest.objects.select_related("pre_request").order_by("-created_at")
    qs = scope_formal4m_queryset(actor, qs)
    return render(request, "qms/formal4m_list.html", {"actor": actor, "items": qs})


@login_required
def formal4m_detail_by_id(request, formal_id: int):
    """정식 4M 상세 (정식 4M id 기반)"""
    actor = get_actor(request.user)
    formal = get_object_or_404(
        Formal4MRequest.objects.select_related("pre_request"),
        pk=formal_id,
    )
    if not can_view_formal4m(actor, formal):
        return HttpResponseForbidden("접근 권한이 없습니다.")

    # 확장(FULL) 양식은 버튼 없이 자동으로 보이도록 보장
    if formal.template_type != "FULL":
        with transaction.atomic():
            ensure_formal4m_full(formal)
            formal.refresh_from_db()

    items = formal.doc_items.all().prefetch_related("attachments").order_by("seq")

    # FULL 섹션 데이터
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


@login_required
def formal4m_detail(request, pk):
    """정식 4M 상세 (사전 4M pk 기반) - 기존 링크 호환"""
    actor = get_actor(request.user)
    pre = get_object_or_404(M4Request, pk=pk)
    if not can_view_m4(actor, pre):
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if pre.status != "APPROVED":
        messages.error(request, "정식 4M은 사전 4M 승인 완료 후에만 열람할 수 있습니다.")
        return redirect("qms:m4_detail", pk=pk)

    formal = get_or_create_formal4m(pre)
    return redirect("qms:formal4m_detail_by_id", formal_id=formal.id)


# =========================
# 정식 4M 결재(워크플로우)
# - 사전 4M과 동일한 흐름: 작성중 → 검토1 → (선택) 검토2 → 최종승인
# =========================


def _formal4m_next_status_on_submit(formal: Formal4MRequest) -> str:
    """정식 4M 상신 시 첫 단계 상태를 계산한다."""
    if getattr(formal, "approval_reviewer_user_id", None):
        return "PENDING_REVIEW"
    if getattr(formal, "approval_reviewer_user2_id", None):
        return "PENDING_REVIEW2"
    return "PENDING_APPROVE"


@login_required
def formal4m_workflow_set(request, formal_id: int):
    """정식 4M 결재선 지정 (내부만)."""
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal) or not actor.is_internal:
        return HttpResponseForbidden("접근 권한이 없습니다.")

    if request.method != "POST":
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    # 승인완료 이후에는 결재선 변경 금지
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
    """정식 4M 결재 상신 (Soft-Warning 반영)."""
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal):
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if request.method != "POST":
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    # ✅ [개선: Soft-Warning] 상신을 막지 않고 알림만 준 뒤 진행
    progress = _formal4m_progress(formal)
    if progress['required_review_ok'] < progress['required_total']:
        messages.warning(request, f"주의: 필수 서류 {progress['required_total']}건 중 {progress['required_review_ok']}건만 검토 완료된 상태로 상신되었습니다.")

    # 기본: 사전 4M 기안자(신청자)만 상신/재상신
    if formal.pre_request.user_id != request.user.id:
        messages.error(request, "상신 권한이 없습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    if formal.approval_status != "DRAFT":
        messages.error(request, "작성중 상태에서만 상신할 수 있습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    # 결재선이 비어있을 수 있으므로 최소한 최종승인자는 요구
    if not getattr(formal, "approval_approver_user_id", None):
        messages.error(request, "최종 승인자를 지정해야 상신할 수 있습니다.")
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    with transaction.atomic():
        formal.approval_status = _formal4m_next_status_on_submit(formal)
        formal.approval_is_submitted = True
        formal.approval_submitted_at = timezone.now()
        formal.approval_reject_reason = ""

        # 결재 흔적 초기화
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
    """정식 4M 결재 승인(검토1/검토2/최종)."""
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal) or not actor.is_internal:
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if request.method != "POST":
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    with transaction.atomic():
        # 1) 검토1
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

        # 2) 검토2
        if formal.approval_status == "PENDING_REVIEW2" and request.user == formal.approval_reviewer_user2:
            formal.approval_is_reviewed2 = True
            formal.approval_reviewed2_at = timezone.now()
            formal.approval_status = "PENDING_APPROVE"
            formal.save()
            messages.success(request, "검토2 승인되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

        # 3) 최종 승인 (유효성 시작일 자동화 포함)
        if formal.approval_status == "PENDING_APPROVE" and request.user == formal.approval_approver_user:
            formal.approval_is_approved = True
            formal.approval_approved_at = timezone.now()
            formal.approval_status = "APPROVED"
            
            # ✅ [개선: 자동화] 최종 승인 시 유효성 평가 시작일 자동 설정
            if not formal.validity_start_date:
                formal.validity_start_date = timezone.localdate()
                
            formal.save()
            messages.success(request, "정식 4M 최종 승인되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    messages.error(request, "승인 권한이 없거나 처리할 수 없는 상태입니다.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)


@login_required
def formal4m_workflow_reject(request, formal_id: int):
    """정식 4M 반려(검토1/검토2/최종 단계에서 가능)."""
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
    """정식 4M 반려 후 재상신."""
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
    """정식 4M 결재 취소(상신/승인 취소). 사전 4M과 동일한 규칙."""
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal) or not actor.is_internal:
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if request.method != "POST":
        return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

    with transaction.atomic():
        # 기안자: 상신 취소(검토1 대기 단계에서만)
        if formal.approval_status == "PENDING_REVIEW" and request.user.id == formal.pre_request.user_id:
            formal.approval_status = "DRAFT"
            formal.approval_is_submitted = False
            formal.approval_submitted_at = None
            formal.save()
            messages.info(request, "상신이 취소되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

        # 검토1 승인 취소 (검토2 대기 상태에서)
        elif formal.approval_status == "PENDING_REVIEW2" and request.user == formal.approval_reviewer_user:
            formal.approval_status = "PENDING_REVIEW"
            formal.approval_is_reviewed = False
            formal.approval_reviewed_at = None
            formal.approval_is_reviewed2 = False
            formal.approval_reviewed2_at = None
            formal.save()
            messages.info(request, "검토1 승인이 취소되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

        # 검토2 승인 취소 (최종 승인 대기 상태에서)
        elif formal.approval_status == "PENDING_APPROVE" and request.user == formal.approval_reviewer_user2:
            formal.approval_status = "PENDING_REVIEW2"
            if hasattr(formal, "approval_is_reviewed2"):
                formal.approval_is_reviewed2 = False
            if hasattr(formal, "approval_reviewed2_at"):
                formal.approval_reviewed2_at = None
            formal.save()
            messages.info(request, "검토2 승인이 취소되었습니다.")
            return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)

        # (기존) 검토1 승인 취소 (검토2가 없을 때)
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

        # 최종 승인 취소
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
    """3개월 유효성 평가 시작일 설정 (내부만)."""
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
    """정식 4M 양식을 FULL(확장)로 전환 (내부만)"""
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

        # 사내승인 row 보장
        Formal4MApproval.objects.get_or_create(formal_request=formal)

        # 단계기록 기본 row 보장
        for stage in ["ISIR", "OEM_APPROVAL", "INTERNAL_APPLY", "CUSTOMER_APPLY"]:
            Formal4MStageRecord.objects.get_or_create(formal_request=formal, stage=stage)

        # 일정 템플릿 row 보장
        existing = set(formal.schedule_items.values_list("item_name", flat=True))
        to_create = []
        for name in FORMAL_4M_SCHEDULE_TEMPLATE:
            if name not in existing:
                to_create.append(Formal4MScheduleItem(formal_request=formal, item_name=name))
        if to_create:
            Formal4MScheduleItem.objects.bulk_create(to_create)

        # 검사항목 템플릿 row 보장
        existing_ins = set(formal.inspection_results.values_list("inspection_item", flat=True))
        to_create_ins = []
        for name in FORMAL_4M_INSPECTION_TEMPLATE:
            if name not in existing_ins:
                to_create_ins.append(Formal4MInspectionResult(formal_request=formal, inspection_item=name))
        if to_create_ins:
            Formal4MInspectionResult.objects.bulk_create(to_create_ins)

    messages.success(request, "정식 4M이 확장 양식으로 전환되었습니다.")
    return redirect("qms:formal4m_detail_by_id", formal_id=formal_id)


# ✅ [개선: 보안] 최종 승인 단계 시 수정 방지 로직 추가 (inspection_update)
@login_required
def formal4m_inspection_update(request, formal_id: int, row_id: int):
    actor = get_actor(request.user)
    formal = get_object_or_404(Formal4MRequest, pk=formal_id)
    if not can_view_formal4m(actor, formal):
        return HttpResponseForbidden("접근 권한이 없습니다.")
    if not actor.is_internal:
        return HttpResponseForbidden("내부 사용자만 수정할 수 있습니다.")
    
    # 승인대기 또는 완료 상태 시 수정 차단
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


# ✅ [개선: 보안] 최종 승인 단계 시 수정 방지 로직 추가 (schedule_update)
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


# ✅ [개선: 보안] 최종 승인 완료 시 수정 방지 로직 추가 (stage_update)
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
    """사내 승인(확정) 항목은 승인 후에도 기록용으로 수정 가능하도록 유지한다."""
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


# --- [원본 복구] 사내 검토 기능 ---
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