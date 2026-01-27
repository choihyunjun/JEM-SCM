from __future__ import annotations

from django.db import models
from django.contrib.auth.models import User
from django.conf import settings

import datetime
import calendar
from django.utils import timezone

from orders.models import Organization


class M4Request(models.Model):
    # --- [상태값 수정: DRAFT 추가] ---
    STATUS_CHOICES = [
        ("DRAFT", "작성중"),  # 기안자가 '결재' 버튼을 누르기 전 초기 상태
        ("PENDING_REVIEW", "검토대기"),
        ("PENDING_REVIEW2", "검토2대기"),
        ("PENDING_APPROVE", "승인대기"),
        ("APPROVED", "승인완료"),
        ("REJECTED", "반려"),
    ]

    M4_TYPES = [
        ("MAN", "작업자(Man)"),
        ("MACHINE", "설비(Machine)"),
        ("MATERIAL", "재료(Material)"),
        ("METHOD", "공법(Method)"),
    ]

    CHANGE_CLASS_CHOICES = [
        ("A", "품질시스템(A)"),
        ("B", "설계변경(B)"),
        ("C", "공정변경(C)"),
        ("D", "구매변경(D)"),
    ]

    change_class = models.CharField(
        max_length=1,
        choices=CHANGE_CLASS_CHOICES,
        null=True,
        blank=True,
        verbose_name="변경구분(A/B/C/D)",
    )


    # 기본 정보 필드
    user = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="신청자")

    # 협력사도 일부 기능을 사용하므로, 요청이 어느 협력사(org)에 해당하는지 명시
    # (내부 작성 시 선택, 협력사 작성 시 자동 설정)
    vendor_org = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="m4_requests",
        verbose_name="협력사(대상)",
        limit_choices_to={"org_type": "VENDOR"},
    )

    part_no = models.CharField(max_length=50, verbose_name="품번")
    part_name = models.CharField(max_length=100, verbose_name="품명")
    request_no = models.CharField(max_length=20, unique=True, verbose_name="관리번호")
    m4_type = models.CharField(max_length=10, choices=M4_TYPES, verbose_name="변경구분")

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="DRAFT",
        verbose_name="진행상태",
    )

    # ✅ 반려 사유(필드 추가)
    reject_reason = models.TextField(blank=True, null=True, verbose_name="반려사유")

    # 체크리스트 및 날짜
    has_isir = models.BooleanField(default=False, verbose_name="ISIR")
    has_process_flow = models.BooleanField(default=False, verbose_name="공정흐름도")
    has_control_plan = models.BooleanField(default=False, verbose_name="관리계획서")
    has_fmea = models.BooleanField(default=False, verbose_name="FMEA")
    has_work_standard = models.BooleanField(default=False, verbose_name="작업표준서")
    due_date = models.DateField(null=True, blank=True, verbose_name="완료목표일")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # 상세 정보 필드
    factory = models.CharField(max_length=50, blank=True, null=True, verbose_name="공장")
    product = models.CharField(max_length=50, blank=True, null=True, verbose_name="제품")
    model_name = models.CharField(max_length=50, blank=True, null=True, verbose_name="품목군")  # 차종 → 품목군
    quality_rank = models.CharField(max_length=10, blank=True, null=True, verbose_name="품질RANK")
    is_internal = models.BooleanField(default=False, verbose_name="사내")
    is_external = models.BooleanField(default=False, verbose_name="사외")
    reason = models.TextField(blank=True, null=True, verbose_name="변경사유")
    content_before = models.TextField(blank=True, null=True, verbose_name="변경전")
    content_after = models.TextField(blank=True, null=True, verbose_name="변경후")
    affected_features = models.TextField(blank=True, null=True, verbose_name="영향받는 특성치")

    # 실시 계획 및 사진
    plan_step1 = models.DateField(null=True, blank=True, verbose_name="1.공정준비")
    plan_step2 = models.DateField(null=True, blank=True, verbose_name="2.표준류 정비")
    plan_step3 = models.DateField(null=True, blank=True, verbose_name="3.공정안정화")
    plan_step4 = models.DateField(null=True, blank=True, verbose_name="4.초품검사")
    plan_step5 = models.DateField(null=True, blank=True, verbose_name="5.초품 SAMPLE 제출")
    plan_step6 = models.DateField(null=True, blank=True, verbose_name="6.초품승인 완료")
    plan_step7 = models.DateField(null=True, blank=True, verbose_name="7.양산개시")
    plan_step8 = models.DateField(null=True, blank=True, verbose_name="8.변경초품 납품")

    photo_before = models.FileField(
        upload_to="qms/m4/", null=True, blank=True, verbose_name="변경전 사진"
    )
    photo_after = models.FileField(
        upload_to="qms/m4/", null=True, blank=True, verbose_name="변경후 사진"
    )

    # 결재 시스템 필드
    reviewer_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,  # ✅ admin/폼에서 빈값 허용
        related_name="m4_reviewer_set",
        verbose_name="검토자",
    )

    reviewer_user2 = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="m4_reviewer2_set",
        verbose_name="검토2",
    )
    approver_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,  # ✅ admin/폼에서 빈값 허용
        related_name="m4_approver_set",
        verbose_name="최종승인자",
    )

    is_submitted = models.BooleanField(default=False, verbose_name="기안여부")
    submitted_at = models.DateTimeField(null=True, blank=True, verbose_name="기안일시")
    is_reviewed = models.BooleanField(default=False, verbose_name="검토여부")
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="검토일시")

    is_reviewed2 = models.BooleanField(default=False, verbose_name="검토2여부")
    reviewed2_at = models.DateTimeField(null=True, blank=True, verbose_name="검토2일시")
    is_approved = models.BooleanField(default=False, verbose_name="승인여부")
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name="승인일시")

    def __str__(self):
        return f"{self.request_no} - {self.part_no}"


class M4Review(models.Model):
    request = models.ForeignKey(M4Request, on_delete=models.CASCADE, related_name="reviews")
    department = models.CharField(max_length=50, verbose_name="검토부서")
    reviewer_name = models.CharField(max_length=50, blank=True, null=True, verbose_name="검토자성함")
    reviewer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="기록자")

    # 품질팀의 요청 사항
    request_content = models.TextField(null=True, blank=True, verbose_name="품질팀 요청사항")

    # 해당 부서의 답변 내용
    content = models.TextField(null=True, blank=True, verbose_name="검토답변")

    # 증빙 파일 (하나만 유지)
    evidence_file = models.FileField(
        upload_to="qms/m4/review/", null=True, blank=True, verbose_name="증빙파일"
    )

    # 날짜(뷰에서 제어)
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name="발송일(요청)")
    received_at = models.DateTimeField(null=True, blank=True, verbose_name="접수일(회신)")

    def __str__(self):
        return f"[{self.department}] {self.request.request_no} 검토"


class M4ChangeLog(models.Model):
    request = models.ForeignKey(
        M4Request, on_delete=models.CASCADE, related_name="change_logs", verbose_name="관련요청"
    )
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="수정자")
    field_name = models.CharField(max_length=50, verbose_name="수정필드")
    old_value = models.TextField(null=True, blank=True, verbose_name="이전내용")
    new_value = models.TextField(null=True, blank=True, verbose_name="변경내용")
    changed_at = models.DateTimeField(auto_now_add=True, verbose_name="수정일시")

    class Meta:
        ordering = ["-changed_at"]

    def __str__(self):
        return f"{self.request.request_no} - {self.field_name} 변경"


# =========================
# 정식 4M (사전 4M 승인 후 생성)
# =========================

class Formal4MRequest(models.Model):
    """사전 4M 승인 완료 후, 제출서류(증빙) 체크리스트를 관리하는 '정식 4M'.

    확장(FULL) 양식에서는 일정/검토결과/단계기록/사내승인을 함께 관리한다.
    """

    pre_request = models.OneToOneField(
        M4Request,
        on_delete=models.CASCADE,
        related_name="formal_4m",
        verbose_name="사전 4M",
    )

    formal_no = models.CharField(max_length=40, unique=True, verbose_name="정식 4M 번호")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일")

    # 3개월 유효성 평가 시작일(사내 절차 기준)
    validity_start_date = models.DateField(null=True, blank=True, verbose_name="유효성평가 시작일")

    # 절차서(JEM-QP-202) 기준: 변경구분(A/B/C/D)
    CHANGE_CLASS_CHOICES = [
        ("A", "품질시스템(A)"),
        ("B", "설계변경(B)"),
        ("C", "공정변경(C)"),
        ("D", "구매변경(D)"),
    ]
    change_class = models.CharField(
        max_length=1,
        choices=CHANGE_CLASS_CHOICES,
        null=True,
        blank=True,
        verbose_name="변경구분(A/B/C/D)",
    )

    # 고객 변경신고 필요 여부 및 결정(품질보증팀장 판단 근거 기록)
    customer_notice_required = models.BooleanField(default=False, verbose_name="고객변경신고 필요")
    customer_notice_decided_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="formal4m_customer_notice_decisions",
        verbose_name="고객변경신고 필요여부 결정자",
    )
    customer_notice_decided_at = models.DateTimeField(null=True, blank=True, verbose_name="결정일시")
    customer_notice_reason = models.TextField(null=True, blank=True, verbose_name="결정사유/근거")

    # 유효성평가(3개월) 결과 및 증빙
    VALIDITY_RESULT_CHOICES = [
        ("ONGOING", "진행중"),
        ("PASS", "적합"),
        ("FAIL", "부적합"),
    ]
    validity_result = models.CharField(
        max_length=10,
        choices=VALIDITY_RESULT_CHOICES,
        null=True,
        blank=True,
        verbose_name="유효성평가 결과",
    )
    validity_closed_at = models.DateTimeField(null=True, blank=True, verbose_name="유효성평가 완료일시")
    validity_evidence = models.FileField(
        upload_to="qms/formal4m/validity/",
        null=True,
        blank=True,
        verbose_name="유효성평가 증빙파일",
    )

    TEMPLATE_TYPE_CHOICES = [
        ("BASIC", "기본"),
        ("FULL", "확장"),
    ]

    template_type = models.CharField(
        max_length=10,
        choices=TEMPLATE_TYPE_CHOICES,
        default="BASIC",
        verbose_name="양식 유형",
    )

    # 결재(워크플로우) - 정식 4M
    APPROVAL_STATUS_CHOICES = [
        ("DRAFT", "작성중"),
        ("PENDING_REVIEW", "검토대기"),
        ("PENDING_REVIEW2", "검토2대기"),
        ("PENDING_APPROVE", "승인대기"),
        ("APPROVED", "승인완료"),
        ("REJECTED", "반려"),
    ]

    approval_status = models.CharField(
        max_length=20, choices=APPROVAL_STATUS_CHOICES, default="DRAFT", verbose_name="결재상태"
    )
    approval_reviewer_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="formal4m_reviewer1_set",
        verbose_name="검토1",
    )
    approval_reviewer_user2 = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="formal4m_reviewer2_set",
        verbose_name="검토2",
    )
    approval_approver_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="formal4m_approver_set",
        verbose_name="최종승인자",
    )

    approval_is_submitted = models.BooleanField(default=False, verbose_name="상신여부")
    approval_submitted_at = models.DateTimeField(null=True, blank=True, verbose_name="상신일시")

    approval_is_reviewed = models.BooleanField(default=False, verbose_name="검토1여부")
    approval_reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="검토1일시")

    approval_is_reviewed2 = models.BooleanField(default=False, verbose_name="검토2여부")
    approval_reviewed2_at = models.DateTimeField(null=True, blank=True, verbose_name="검토2일시")

    approval_is_approved = models.BooleanField(default=False, verbose_name="최종승인여부")
    approval_approved_at = models.DateTimeField(null=True, blank=True, verbose_name="최종승인일시")

    approval_reject_reason = models.TextField(null=True, blank=True, verbose_name="반려사유")

    class Meta:
        verbose_name = "정식 4M"
        verbose_name_plural = "정식 4M"

    def __str__(self):
        return f"{self.formal_no} (from {self.pre_request.request_no})"

    # --- 유효성 평가 D-카운트 ---
    @staticmethod
    def _add_months(d: datetime.date, months: int) -> datetime.date:
        """월 단위 더하기(말일 보정). dateutil 없이 동작."""
        y = d.year + (d.month - 1 + months) // 12
        m = (d.month - 1 + months) % 12 + 1
        last_day = calendar.monthrange(y, m)[1]
        day = min(d.day, last_day)
        return datetime.date(y, m, day)

    @property
    def validity_due_date(self):
        """유효성 평가(3개월) 종료 예정일."""
        if not self.validity_start_date:
            return None
        return self._add_months(self.validity_start_date, 3)

    @property
    def validity_dday_text(self) -> str:
        """D-카운트 텍스트 (예: D-10 / D-DAY / D+3)."""
        due = self.validity_due_date
        if not due:
            return "-"
        today = timezone.localdate()
        delta = (due - today).days
        if delta > 0:
            return f"D-{delta}"
        if delta == 0:
            return "D-DAY"
        return f"D+{abs(delta)}"


class Formal4MDocumentItem(models.Model):
    """정식 4M 제출요구서류(체크리스트) 항목"""

    REVIEW_STATUS_CHOICES = [
        ("PENDING", "미검토"),
        ("OK", "검토완료"),
        ("REJECT", "반려/보완"),
    ]

    formal = models.ForeignKey(
        Formal4MRequest,
        on_delete=models.CASCADE,
        related_name="doc_items",
        verbose_name="정식 4M",
    )
    seq = models.PositiveIntegerField(verbose_name="순번")
    name = models.CharField(max_length=100, verbose_name="제출요구서류")
    is_required = models.BooleanField(default=True, verbose_name="필수")

    review_status = models.CharField(
        max_length=10,
        choices=REVIEW_STATUS_CHOICES,
        default="PENDING",
        verbose_name="검토",
    )
    remark = models.TextField(blank=True, null=True, verbose_name="비고")

    class Meta:
        verbose_name = "정식 4M 제출서류 항목"
        verbose_name_plural = "정식 4M 제출서류 항목"
        ordering = ["seq", "id"]
        unique_together = ("formal", "seq")

    def __str__(self):
        return f"[{self.formal.formal_no}] {self.seq}. {self.name}"


class Formal4MAttachment(models.Model):
    """정식 4M 제출서류 항목에 대한 첨부파일"""

    item = models.ForeignKey(
        Formal4MDocumentItem,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name="제출서류 항목",
    )
    file = models.FileField(upload_to="qms/formal4m/", verbose_name="첨부")
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="업로더")
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name="업로드일")

    class Meta:
        verbose_name = "정식 4M 첨부"
        verbose_name_plural = "정식 4M 첨부"
        ordering = ["-uploaded_at", "-id"]

    def __str__(self):
        return f"{self.item} 첨부"


class Formal4MInspectionResult(models.Model):
    """정식 4M - 변경 검토 결과(검사항목별)"""

    formal_request = models.ForeignKey(
        Formal4MRequest,
        on_delete=models.CASCADE,
        related_name="inspection_results",
        verbose_name="정식 4M",
    )
    inspection_item = models.CharField(max_length=100, verbose_name="검사항목")
    spec = models.CharField(max_length=100, blank=True, null=True, verbose_name="규격")
    method = models.CharField(max_length=100, blank=True, null=True, verbose_name="검사방법")
    judgment = models.CharField(max_length=30, blank=True, null=True, verbose_name="판정")
    remark = models.CharField(max_length=200, blank=True, null=True, verbose_name="비고")
    attachment = models.FileField(
        upload_to="formal4m/inspection/", blank=True, null=True, verbose_name="첨부"
    )

    class Meta:
        verbose_name = "정식4M 검토결과"
        verbose_name_plural = "정식4M 검토결과"

    def __str__(self):
        return f"{self.formal_request.formal_no} - {self.inspection_item}"


class Formal4MScheduleItem(models.Model):
    """정식 4M - 일정계획 수립(항목별)"""

    formal_request = models.ForeignKey(
        Formal4MRequest,
        on_delete=models.CASCADE,
        related_name="schedule_items",
        verbose_name="정식 4M",
    )
    oem = models.CharField(max_length=50, blank=True, null=True, verbose_name="OEM")
    item_name = models.CharField(max_length=100, verbose_name="항목")
    is_required = models.BooleanField(default=False, verbose_name="진행유무(필수)")
    plan_date = models.DateField(blank=True, null=True, verbose_name="계획일")
    owner_name = models.CharField(max_length=50, blank=True, null=True, verbose_name="담당자")
    department = models.CharField(max_length=50, blank=True, null=True, verbose_name="부서")
    note = models.CharField(max_length=200, blank=True, null=True, verbose_name="비고")

    class Meta:
        verbose_name = "정식4M 일정항목"
        verbose_name_plural = "정식4M 일정항목"

    def __str__(self):
        return f"{self.formal_request.formal_no} - {self.item_name}"


class Formal4MStageRecord(models.Model):
    """정식 4M - 단계별 기록/첨부 (ISIR/OEM승인/사내적용/고객적용 등)"""

    STAGE_CHOICES = [
        ("ISIR", "ISIR제출"),
        ("OEM_APPROVAL", "OEM승인"),
        ("INTERNAL_APPLY", "사내적용"),
        ("CUSTOMER_APPLY", "고객적용"),
        ("MASS_PRODUCTION_REVIEW", "양산이행회의(Go/No-Go)"),
        ("CUSTOMER_NOTICE", "고객변경신고/통보"),
        ("OTHER", "기타"),
    ]

    formal_request = models.ForeignKey(
        Formal4MRequest,
        on_delete=models.CASCADE,
        related_name="stage_records",
        verbose_name="정식 4M",
    )
    stage = models.CharField(max_length=30, choices=STAGE_CHOICES, verbose_name="단계")
    record_date = models.DateField(blank=True, null=True, verbose_name="일자")
    remark = models.CharField(max_length=200, blank=True, null=True, verbose_name="비고")
    attachment = models.FileField(
        upload_to="formal4m/stages/", blank=True, null=True, verbose_name="문서첨부"
    )

    class Meta:
        verbose_name = "정식4M 단계기록"
        verbose_name_plural = "정식4M 단계기록"

    def __str__(self):
        return f"{self.formal_request.formal_no} - {self.get_stage_display()}"


class Formal4MApproval(models.Model):
    """정식 4M - 사내 승인 정보"""

    formal_request = models.OneToOneField(
        Formal4MRequest,
        on_delete=models.CASCADE,
        related_name="approval",
        verbose_name="정식 4M",
    )
    is_approved = models.BooleanField(default=False, verbose_name="사내승인")
    approval_no = models.CharField(max_length=40, blank=True, null=True, verbose_name="승인번호")
    judgment_date = models.DateField(blank=True, null=True, verbose_name="판정일자")
    remark = models.CharField(max_length=200, blank=True, null=True, verbose_name="비고")

    class Meta:
        verbose_name = "정식4M 사내승인"
        verbose_name_plural = "정식4M 사내승인"

    def __str__(self):
        return f"{self.formal_request.formal_no} - {'승인' if self.is_approved else '미승인'}"

# qms/models.py 하단에 추가

from material.models import MaterialTransaction # WMS 수불 모델 참조

class ImportInspection(models.Model):
    """
    [QMS] 수입검사 관리
    - WMS에서 '수입검사 진행'으로 입고된 건에 대해 생성됨
    """
    STATUS_CHOICES = [
        ('PENDING', '검사대기'),
        ('APPROVED', '합격(입고승인)'),
        ('REJECTED', '불합격(반품/폐기)'),
    ]

    # WMS 입고 트랜잭션과 1:1 연결 (어떤 입고 건인지 추적)
    inbound_transaction = models.OneToOneField(
        MaterialTransaction,
        on_delete=models.CASCADE,
        related_name='inspection',
        verbose_name="관련 입고이력"
    )

    # LOT 정보 추가 (선입선출 관리용)
    lot_no = models.DateField("LOT 번호(생산일)", null=True, blank=True)

    # 수입검사 완료 후 입고될 목표 창고
    target_warehouse_code = models.CharField("목표 창고", max_length=20, default='2000')

    status = models.CharField("검사상태", max_length=20, choices=STATUS_CHOICES, default='PENDING')
    
    # 검사 결과 데이터
    inspector = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="검사자")
    inspected_at = models.DateTimeField("검사일시", null=True, blank=True)
    
    # 검사 항목 (간략화 버전)
    check_report = models.BooleanField("성적서 확인", default=False)
    check_visual = models.BooleanField("외관 검사", default=False)
    check_dimension = models.BooleanField("치수/기능 검사", default=False)

    # 판정 수량 (양품/불량 분할)
    qty_good = models.IntegerField("양품 수량", default=0)
    qty_bad = models.IntegerField("불량 수량", default=0)

    remark = models.TextField("검사 의견", blank=True, null=True)
    attachment = models.FileField("검사 성적서/사진", upload_to="qms/inspection/", blank=True, null=True)

    created_at = models.DateTimeField("요청일시", auto_now_add=True)

    class Meta:
        verbose_name = "수입검사"
        verbose_name_plural = "수입검사 관리"

    def __str__(self):
        return f"검사요청: {self.inbound_transaction.part.part_name}"


# ============================================================================
# 새로운 4M 변경점 관리 시스템 (v2)
# ============================================================================

class ChangeRequest(models.Model):
    """
    4M 변경 신청서 (통합 모델)
    - 사전 검토 → 승인 → 정식 관리 → 완료까지 단일 모델로 관리
    """

    # 진행 단계 (Phase)
    PHASE_CHOICES = [
        ('DRAFT', '작성중'),
        ('REVIEW', '검토중'),
        ('APPROVED', '승인완료'),
        ('FORMAL', '정식진행'),
        ('VALIDATION', '유효성평가'),
        ('CLOSED', '완료'),
        ('REJECTED', '반려'),
        ('CANCELED', '취소'),
    ]

    # 4M 유형
    TYPE_CHOICES = [
        ('MAN', '작업자(Man)'),
        ('MACHINE', '설비(Machine)'),
        ('MATERIAL', '자재(Material)'),
        ('METHOD', '공법(Method)'),
    ]

    # 변경 등급 (고객사 기준)
    GRADE_CHOICES = [
        ('A', 'A등급 - 품질시스템'),
        ('B', 'B등급 - 설계변경'),
        ('C', 'C등급 - 공정변경'),
        ('D', 'D등급 - 구매변경'),
    ]

    # === 기본 정보 ===
    request_no = models.CharField('관리번호', max_length=30, unique=True)
    phase = models.CharField('진행단계', max_length=15, choices=PHASE_CHOICES, default='DRAFT')

    change_type = models.CharField('4M유형', max_length=10, choices=TYPE_CHOICES)
    change_grade = models.CharField('변경등급', max_length=1, choices=GRADE_CHOICES, blank=True, null=True)

    # === 품목 정보 ===
    factory = models.CharField('공장', max_length=20, default='2공장', blank=True)
    part_no = models.CharField('품번', max_length=50)
    part_name = models.CharField('품명', max_length=100)
    model_name = models.CharField('품목군', max_length=50, blank=True)  # 차종 → 품목군
    is_internal = models.BooleanField('사내', default=False)
    is_external = models.BooleanField('사외', default=False)

    # === 조직 정보 ===
    vendor = models.ForeignKey(
        Organization, on_delete=models.PROTECT,
        limit_choices_to={'org_type': 'VENDOR'},
        verbose_name='협력사'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='change_requests_created',
        verbose_name='신청자'
    )

    # === 변경 내용 ===
    reason = models.TextField('변경사유')
    content_before = models.TextField('변경 전 내용')
    content_after = models.TextField('변경 후 내용')
    affected_items = models.TextField('영향받는 특성/항목', blank=True)

    # === 일정 ===
    target_date = models.DateField('목표완료일', null=True, blank=True)
    formal_start_date = models.DateField('정식 시작일', null=True, blank=True)
    validation_due_date = models.DateField('유효성평가 기한', null=True, blank=True)
    closed_date = models.DateField('완료일', null=True, blank=True)

    # === 고객 변경신고 ===
    customer_notice_required = models.BooleanField('고객신고 필요', default=False)
    customer_notice_date = models.DateField('고객신고일', null=True, blank=True)
    customer_approval_date = models.DateField('고객승인일', null=True, blank=True)

    # === 유효성 평가 ===
    VALIDITY_CHOICES = [
        ('PENDING', '대기'),
        ('PASS', '합격'),
        ('FAIL', '불합격'),
    ]
    validity_result = models.CharField('유효성결과', max_length=10, choices=VALIDITY_CHOICES, default='PENDING')
    validity_remark = models.TextField('유효성평가 비고', blank=True)

    # === 반려 정보 ===
    reject_reason = models.TextField('반려사유', blank=True)

    # === 타임스탬프 ===
    created_at = models.DateTimeField('신청일시', auto_now_add=True)
    updated_at = models.DateTimeField('수정일시', auto_now=True)

    class Meta:
        verbose_name = '4M변경신청'
        verbose_name_plural = '4M변경신청 관리'
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.request_no}] {self.part_no} - {self.get_change_type_display()}"

    def save(self, *args, **kwargs):
        if not self.request_no:
            self.request_no = self._generate_request_no()
        super().save(*args, **kwargs)

    def _generate_request_no(self):
        """관리번호 자동 생성: 4M-{유형}-{YYYYMMDD}-{SEQ}"""
        today = timezone.localdate()
        prefix = f"4M-{self.change_type}-{today.strftime('%Y%m%d')}"
        last = ChangeRequest.objects.filter(request_no__startswith=prefix).order_by('-request_no').first()
        if last:
            seq = int(last.request_no.split('-')[-1]) + 1
        else:
            seq = 1
        return f"{prefix}-{seq:03d}"

    @property
    def phase_display_class(self):
        """진행단계별 CSS 클래스"""
        mapping = {
            'DRAFT': 'secondary',
            'REVIEW': 'warning',
            'APPROVED': 'info',
            'FORMAL': 'primary',
            'VALIDATION': 'purple',
            'CLOSED': 'success',
            'REJECTED': 'danger',
            'CANCELED': 'dark',
        }
        return mapping.get(self.phase, 'secondary')

    @property
    def can_edit(self):
        """수정 가능 여부"""
        return self.phase in ('DRAFT', 'REJECTED')

    @property
    def current_approval_step(self):
        """현재 결재 단계"""
        return self.approval_steps.filter(status='PENDING').order_by('step_order').first()


class ApprovalStep(models.Model):
    """
    결재 단계
    - 각 ChangeRequest마다 여러 단계의 결재자 지정 가능
    """

    STEP_TYPE_CHOICES = [
        ('REVIEW', '검토'),
        ('APPROVE', '승인'),
    ]

    STATUS_CHOICES = [
        ('WAITING', '대기'),      # 이전 단계 완료 전
        ('PENDING', '결재대기'),   # 결재 가능 상태
        ('APPROVED', '승인'),
        ('REJECTED', '반려'),
    ]

    change_request = models.ForeignKey(
        ChangeRequest, on_delete=models.CASCADE,
        related_name='approval_steps'
    )
    step_order = models.PositiveSmallIntegerField('순서', default=1)
    step_type = models.CharField('단계유형', max_length=10, choices=STEP_TYPE_CHOICES)
    step_name = models.CharField('단계명', max_length=30)  # 예: "품질팀 검토", "팀장 승인"

    assignee = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='assigned_approval_steps',
        verbose_name='결재자'
    )

    status = models.CharField('상태', max_length=10, choices=STATUS_CHOICES, default='WAITING')
    comment = models.TextField('의견', blank=True)

    processed_at = models.DateTimeField('처리일시', null=True, blank=True)

    class Meta:
        verbose_name = '결재단계'
        verbose_name_plural = '결재단계'
        ordering = ['change_request', 'step_order']
        unique_together = ['change_request', 'step_order']

    def __str__(self):
        return f"{self.change_request.request_no} - {self.step_name}"

    def approve(self, user, comment=''):
        """결재 승인"""
        if user != self.assignee:
            raise PermissionError("결재 권한이 없습니다.")
        if self.status != 'PENDING':
            raise ValueError("결재 가능 상태가 아닙니다.")

        self.status = 'APPROVED'
        self.comment = comment
        self.processed_at = timezone.now()
        self.save()

        # 다음 단계 활성화 또는 최종 승인 처리
        self._process_next_step()

    def reject(self, user, comment):
        """결재 반려"""
        if user != self.assignee:
            raise PermissionError("결재 권한이 없습니다.")
        if self.status != 'PENDING':
            raise ValueError("결재 가능 상태가 아닙니다.")
        if not comment:
            raise ValueError("반려 사유를 입력해주세요.")

        self.status = 'REJECTED'
        self.comment = comment
        self.processed_at = timezone.now()
        self.save()

        # 신청서 반려 처리
        cr = self.change_request
        cr.phase = 'REJECTED'
        cr.reject_reason = comment
        cr.save()

    def _process_next_step(self):
        """다음 결재 단계 처리"""
        cr = self.change_request
        next_step = cr.approval_steps.filter(step_order__gt=self.step_order).order_by('step_order').first()

        if next_step:
            next_step.status = 'PENDING'
            next_step.save()
        else:
            # 모든 결재 완료 → 승인 상태로 전환
            cr.phase = 'APPROVED'
            cr.save()


class VendorResponse(models.Model):
    """
    협력사 회신
    - 내부에서 협력사에 요청 → 협력사가 회신 및 증빙 제출
    """

    STATUS_CHOICES = [
        ('REQUESTED', '요청됨'),
        ('RESPONDED', '회신완료'),
    ]

    change_request = models.ForeignKey(
        ChangeRequest, on_delete=models.CASCADE,
        related_name='vendor_responses'
    )

    # 요청 정보 (내부에서 작성)
    request_title = models.CharField('요청제목', max_length=100)
    request_content = models.TextField('요청내용')
    requested_by = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='vendor_requests_sent',
        verbose_name='요청자'
    )
    requested_at = models.DateTimeField('요청일시', auto_now_add=True)

    # 회신 정보 (협력사에서 작성)
    status = models.CharField('상태', max_length=15, choices=STATUS_CHOICES, default='REQUESTED')
    response_content = models.TextField('회신내용', blank=True)
    responded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='vendor_responses_sent',
        verbose_name='회신자'
    )
    responded_at = models.DateTimeField('회신일시', null=True, blank=True)

    class Meta:
        verbose_name = '협력사회신'
        verbose_name_plural = '협력사회신'
        ordering = ['-requested_at']

    def __str__(self):
        return f"{self.change_request.request_no} - {self.request_title}"


class VendorResponseAttachment(models.Model):
    """협력사 회신 첨부파일"""

    response = models.ForeignKey(
        VendorResponse, on_delete=models.CASCADE,
        related_name='attachments'
    )
    file = models.FileField('파일', upload_to='qms/vendor_response/')
    file_name = models.CharField('파일명', max_length=200)
    uploaded_at = models.DateTimeField('업로드일시', auto_now_add=True)

    class Meta:
        verbose_name = '회신첨부파일'
        verbose_name_plural = '회신첨부파일'


class ChangeDocument(models.Model):
    """
    변경 관련 제출 서류
    - 정식 4M 진행 시 필요한 서류 관리
    """

    REVIEW_STATUS_CHOICES = [
        ('PENDING', '검토대기'),
        ('OK', '적합'),
        ('REJECT', '부적합'),
    ]

    # 기본 서류 목록 (템플릿)
    DEFAULT_DOCUMENTS = [
        '검사성적서',
        '공정흐름도',
        '관리계획서',
        'FMEA',
        '작업표준서',
        'MSA',
        'SPC',
        '포장사양서',
        '기타',
    ]

    change_request = models.ForeignKey(
        ChangeRequest, on_delete=models.CASCADE,
        related_name='documents'
    )

    doc_name = models.CharField('서류명', max_length=100)
    is_required = models.BooleanField('필수여부', default=False)

    file = models.FileField('파일', upload_to='qms/change_docs/', blank=True, null=True)
    review_status = models.CharField('검토상태', max_length=10, choices=REVIEW_STATUS_CHOICES, default='PENDING')
    review_comment = models.TextField('검토의견', blank=True)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name='검토자'
    )
    reviewed_at = models.DateTimeField('검토일시', null=True, blank=True)

    uploaded_at = models.DateTimeField('업로드일시', null=True, blank=True)

    class Meta:
        verbose_name = '제출서류'
        verbose_name_plural = '제출서류'
        ordering = ['change_request', 'id']

    def __str__(self):
        return f"{self.change_request.request_no} - {self.doc_name}"


class ChangeHistory(models.Model):
    """
    변경 이력 (자동 기록)
    """

    ACTION_CHOICES = [
        ('CREATE', '생성'),
        ('UPDATE', '수정'),
        ('SUBMIT', '상신'),
        ('APPROVE', '승인'),
        ('REJECT', '반려'),
        ('PHASE_CHANGE', '단계전환'),
        ('VENDOR_REQUEST', '협력사요청'),
        ('VENDOR_RESPONSE', '협력사회신'),
        ('DOC_UPLOAD', '서류업로드'),
        ('DOC_REVIEW', '서류검토'),
    ]

    change_request = models.ForeignKey(
        ChangeRequest, on_delete=models.CASCADE,
        related_name='history'
    )

    action = models.CharField('액션', max_length=20, choices=ACTION_CHOICES)
    description = models.TextField('상세내용')

    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        verbose_name='수행자'
    )
    created_at = models.DateTimeField('일시', auto_now_add=True)

    # 변경 전/후 값 (필드 수정 시)
    field_name = models.CharField('변경필드', max_length=50, blank=True)
    old_value = models.TextField('변경전', blank=True)
    new_value = models.TextField('변경후', blank=True)

    class Meta:
        verbose_name = '변경이력'
        verbose_name_plural = '변경이력'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.change_request.request_no} - {self.get_action_display()}"


# ============================================================================
# 출하검사 (Outgoing Inspection)
# ============================================================================

class OutgoingInspection(models.Model):
    """출하검사 - 고객 납품 전 최종 품질 검사"""

    STATUS_CHOICES = [
        ('PENDING', '검사대기'),
        ('INSPECTING', '검사중'),
        ('PASS', '합격'),
        ('FAIL', '불합격'),
        ('CONDITIONAL', '조건부합격'),
    ]

    # 검사 대상 정보
    inspection_no = models.CharField('검사번호', max_length=30, unique=True)
    inspection_date = models.DateField('검사일')

    part_no = models.CharField('품번', max_length=50)
    part_name = models.CharField('품명', max_length=100)
    lot_no = models.CharField('LOT번호', max_length=50, blank=True)

    # 수량
    total_qty = models.IntegerField('검사수량')
    sample_qty = models.IntegerField('샘플수량', default=0)
    pass_qty = models.IntegerField('합격수량', default=0)
    fail_qty = models.IntegerField('불합격수량', default=0)

    # 납품처 정보
    customer_name = models.CharField('고객사', max_length=100, blank=True)
    delivery_date = models.DateField('납품예정일', null=True, blank=True)

    # 검사 결과
    status = models.CharField('검사상태', max_length=15, choices=STATUS_CHOICES, default='PENDING')

    # 검사 항목 체크
    check_visual = models.BooleanField('외관검사', default=False)
    check_dimension = models.BooleanField('치수검사', default=False)
    check_function = models.BooleanField('기능검사', default=False)
    check_packing = models.BooleanField('포장검사', default=False)
    check_label = models.BooleanField('라벨검사', default=False)

    remark = models.TextField('검사의견', blank=True)
    attachment = models.FileField('검사성적서', upload_to='qms/outgoing/', blank=True, null=True)

    # 담당자
    inspector = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='outgoing_inspections',
        verbose_name='검사자'
    )

    created_at = models.DateTimeField('등록일시', auto_now_add=True)
    updated_at = models.DateTimeField('수정일시', auto_now=True)

    class Meta:
        verbose_name = '출하검사'
        verbose_name_plural = '출하검사 관리'
        ordering = ['-inspection_date', '-id']

    def __str__(self):
        return f"[{self.inspection_no}] {self.part_no}"

    def save(self, *args, **kwargs):
        if not self.inspection_no:
            self.inspection_no = self._generate_no()
        super().save(*args, **kwargs)

    def _generate_no(self):
        from django.utils import timezone
        today = timezone.localdate()
        prefix = f"OI-{today.strftime('%Y%m%d')}"
        last = OutgoingInspection.objects.filter(inspection_no__startswith=prefix).order_by('-inspection_no').first()
        seq = int(last.inspection_no.split('-')[-1]) + 1 if last else 1
        return f"{prefix}-{seq:03d}"


# ============================================================================
# 부적합품 관리 (Non-conformance)
# ============================================================================

class NonConformance(models.Model):
    """부적합품 관리 - 불량 발생 시 처리 및 추적"""

    SOURCE_CHOICES = [
        ('INCOMING', '수입검사'),
        ('PROCESS', '공정검사'),
        ('OUTGOING', '출하검사'),
        ('CUSTOMER', '고객클레임'),
        ('INTERNAL', '내부발견'),
    ]

    STATUS_CHOICES = [
        ('OPEN', '접수'),
        ('ANALYZING', '원인분석중'),
        ('ACTION', '조치중'),
        ('VERIFY', '검증중'),
        ('CLOSED', '완료'),
    ]

    DISPOSITION_CHOICES = [
        ('RETURN', '반품'),
        ('REWORK', '재작업'),
        ('SCRAP', '폐기'),
        ('USE_AS_IS', '특채사용'),
        ('SORT', '선별'),
    ]

    # 기본 정보
    nc_no = models.CharField('부적합번호', max_length=30, unique=True)
    source = models.CharField('발생구분', max_length=15, choices=SOURCE_CHOICES)
    status = models.CharField('처리상태', max_length=15, choices=STATUS_CHOICES, default='OPEN')

    occurred_date = models.DateField('발생일')

    # 품목 정보
    part_no = models.CharField('품번', max_length=50)
    part_name = models.CharField('품명', max_length=100)
    lot_no = models.CharField('LOT번호', max_length=50, blank=True)

    # 협력사 (수입검사 불량 시)
    vendor = models.ForeignKey(
        Organization, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'org_type': 'VENDOR'},
        verbose_name='협력사'
    )

    # 불량 내용
    defect_qty = models.IntegerField('불량수량')
    defect_type = models.CharField('불량유형', max_length=100)
    defect_detail = models.TextField('불량상세')
    photo = models.FileField('불량사진', upload_to='qms/nc/photos/', blank=True, null=True)

    # 원인분석
    cause_analysis = models.TextField('원인분석', blank=True)
    root_cause = models.TextField('근본원인', blank=True)

    # 처리방법
    disposition = models.CharField('처리방법', max_length=15, choices=DISPOSITION_CHOICES, blank=True)
    disposition_detail = models.TextField('처리내역', blank=True)

    # 담당자
    reported_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='nc_reported',
        verbose_name='보고자'
    )
    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='nc_assigned',
        verbose_name='담당자'
    )

    closed_date = models.DateField('완료일', null=True, blank=True)

    created_at = models.DateTimeField('등록일시', auto_now_add=True)
    updated_at = models.DateTimeField('수정일시', auto_now=True)

    class Meta:
        verbose_name = '부적합품'
        verbose_name_plural = '부적합품 관리'
        ordering = ['-occurred_date', '-id']

    def __str__(self):
        return f"[{self.nc_no}] {self.part_no} - {self.defect_type}"

    def save(self, *args, **kwargs):
        if not self.nc_no:
            self.nc_no = self._generate_no()
        super().save(*args, **kwargs)

    def _generate_no(self):
        from django.utils import timezone
        today = timezone.localdate()
        prefix = f"NC-{today.strftime('%Y%m%d')}"
        last = NonConformance.objects.filter(nc_no__startswith=prefix).order_by('-nc_no').first()
        seq = int(last.nc_no.split('-')[-1]) + 1 if last else 1
        return f"{prefix}-{seq:03d}"


# ============================================================================
# 시정조치 요청 (CAPA - Corrective and Preventive Action)
# ============================================================================

class CorrectiveAction(models.Model):
    """시정조치 요청 - 불량 원인분석 및 재발방지 대책"""

    TYPE_CHOICES = [
        ('CA', '시정조치'),
        ('PA', '예방조치'),
    ]

    STATUS_CHOICES = [
        ('REQUESTED', '요청'),
        ('RECEIVED', '접수'),
        ('ANALYZING', '원인분석'),
        ('ACTION', '대책수립'),
        ('IMPLEMENTING', '조치이행'),
        ('VERIFYING', '효과검증'),
        ('CLOSED', '완료'),
    ]

    # 기본 정보
    capa_no = models.CharField('CAPA번호', max_length=30, unique=True)
    capa_type = models.CharField('유형', max_length=5, choices=TYPE_CHOICES, default='CA')
    status = models.CharField('상태', max_length=15, choices=STATUS_CHOICES, default='REQUESTED')

    # 관련 부적합
    non_conformance = models.ForeignKey(
        NonConformance, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='capa_requests',
        verbose_name='관련 부적합'
    )

    # 대상 협력사
    vendor = models.ForeignKey(
        Organization, on_delete=models.PROTECT,
        limit_choices_to={'org_type': 'VENDOR'},
        verbose_name='대상 협력사'
    )

    # 품목 정보
    part_no = models.CharField('품번', max_length=50)
    part_name = models.CharField('품명', max_length=100)

    # 요청 내용
    issue_title = models.CharField('문제점', max_length=200)
    issue_detail = models.TextField('문제상세')
    request_date = models.DateField('요청일')
    due_date = models.DateField('회신기한')

    # 협력사 회신
    cause_analysis = models.TextField('원인분석(협력사)', blank=True)
    corrective_action = models.TextField('시정조치(협력사)', blank=True)
    preventive_action = models.TextField('예방조치(협력사)', blank=True)
    action_date = models.DateField('조치완료(예정)일', null=True, blank=True)
    response_date = models.DateField('회신일', null=True, blank=True)

    # 첨부파일
    attachment = models.FileField('첨부파일', upload_to='qms/capa/', blank=True, null=True)

    # 효과검증
    verification_result = models.TextField('효과검증 결과', blank=True)
    verified_date = models.DateField('검증일', null=True, blank=True)
    is_effective = models.BooleanField('효과있음', null=True, blank=True)

    # 담당자
    requested_by = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='capa_requested',
        verbose_name='요청자'
    )
    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='capa_assigned',
        verbose_name='담당자(협력사)'
    )

    closed_date = models.DateField('완료일', null=True, blank=True)

    created_at = models.DateTimeField('등록일시', auto_now_add=True)
    updated_at = models.DateTimeField('수정일시', auto_now=True)

    class Meta:
        verbose_name = '시정조치요청'
        verbose_name_plural = '시정조치(CAPA) 관리'
        ordering = ['-request_date', '-id']

    def __str__(self):
        return f"[{self.capa_no}] {self.vendor.name} - {self.issue_title}"

    def save(self, *args, **kwargs):
        if not self.capa_no:
            self.capa_no = self._generate_no()
        super().save(*args, **kwargs)

    def _generate_no(self):
        from django.utils import timezone
        today = timezone.localdate()
        prefix = f"CAPA-{today.strftime('%Y%m%d')}"
        last = CorrectiveAction.objects.filter(capa_no__startswith=prefix).order_by('-capa_no').first()
        seq = int(last.capa_no.split('-')[-1]) + 1 if last else 1
        return f"{prefix}-{seq:03d}"

    @property
    def is_overdue(self):
        """회신기한 초과 여부"""
        from django.utils import timezone
        if self.status in ('CLOSED', 'VERIFYING'):
            return False
        return timezone.localdate() > self.due_date


# ============================================================================
# 협력사 클레임 (Vendor Claim)
# ============================================================================

class VendorClaim(models.Model):
    """협력사 클레임 - 협력사 귀책 불량에 대한 클레임 관리"""

    STATUS_CHOICES = [
        ('DRAFT', '작성중'),
        ('ISSUED', '발행'),
        ('RECEIVED', '접수확인'),
        ('PROCESSING', '처리중'),
        ('RESOLVED', '처리완료'),
        ('CLOSED', '종결'),
    ]

    CLAIM_TYPE_CHOICES = [
        ('QUALITY', '품질불량'),
        ('DELIVERY', '납기지연'),
        ('QUANTITY', '수량차이'),
        ('PACKING', '포장불량'),
        ('DOCUMENT', '서류미비'),
        ('OTHER', '기타'),
    ]

    # 기본 정보
    claim_no = models.CharField('클레임번호', max_length=30, unique=True)
    claim_type = models.CharField('클레임유형', max_length=15, choices=CLAIM_TYPE_CHOICES)
    status = models.CharField('상태', max_length=15, choices=STATUS_CHOICES, default='DRAFT')

    issue_date = models.DateField('발생일')

    # 대상 협력사
    vendor = models.ForeignKey(
        Organization, on_delete=models.PROTECT,
        limit_choices_to={'org_type': 'VENDOR'},
        verbose_name='협력사'
    )

    # 품목 정보
    part_no = models.CharField('품번', max_length=50)
    part_name = models.CharField('품명', max_length=100)
    lot_no = models.CharField('LOT번호', max_length=50, blank=True)

    # 클레임 내용
    claim_qty = models.IntegerField('클레임수량')
    claim_detail = models.TextField('클레임내용')
    photo = models.FileField('불량사진', upload_to='qms/claim/photos/', blank=True, null=True)

    # 비용 관련
    claim_amount = models.DecimalField('클레임금액', max_digits=12, decimal_places=0, null=True, blank=True,
                                        help_text='클레임 청구 금액 (원)')

    # 관련 부적합
    non_conformance = models.ForeignKey(
        NonConformance, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='claims',
        verbose_name='관련 부적합'
    )

    # 처리 결과
    vendor_response = models.TextField('협력사 답변', blank=True)
    compensation_type = models.CharField('보상방법', max_length=50, blank=True)  # 교체, 수리, 반품, 비용공제 등
    compensation_amount = models.DecimalField('보상금액', max_digits=12, decimal_places=0, null=True, blank=True)
    resolution_detail = models.TextField('처리결과', blank=True)

    # 정산 관리
    SETTLEMENT_STATUS_CHOICES = [
        ('NOT_SETTLED', '미정산'),
        ('IN_PROGRESS', '정산중'),
        ('SETTLED', '정산완료'),
    ]
    settlement_status = models.CharField('정산상태', max_length=15, choices=SETTLEMENT_STATUS_CHOICES, default='NOT_SETTLED')
    settlement_date = models.DateField('정산일', null=True, blank=True)
    settlement_remark = models.CharField('정산비고', max_length=200, blank=True)

    # 담당자
    issued_by = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='claims_issued',
        verbose_name='발행자'
    )

    issued_date = models.DateField('발행일', null=True, blank=True)
    resolved_date = models.DateField('처리완료일', null=True, blank=True)
    closed_date = models.DateField('종결일', null=True, blank=True)

    created_at = models.DateTimeField('등록일시', auto_now_add=True)
    updated_at = models.DateTimeField('수정일시', auto_now=True)

    class Meta:
        verbose_name = '협력사클레임'
        verbose_name_plural = '협력사 클레임 관리'
        ordering = ['-issue_date', '-id']

    def __str__(self):
        return f"[{self.claim_no}] {self.vendor.name} - {self.part_no}"

    def save(self, *args, **kwargs):
        if not self.claim_no:
            self.claim_no = self._generate_no()
        super().save(*args, **kwargs)

    def _generate_no(self):
        from django.utils import timezone
        today = timezone.localdate()
        prefix = f"CLM-{today.strftime('%Y%m%d')}"
        last = VendorClaim.objects.filter(claim_no__startswith=prefix).order_by('-claim_no').first()
        seq = int(last.claim_no.split('-')[-1]) + 1 if last else 1
        return f"{prefix}-{seq:03d}"


# ============================================================================
# 협력사 평가 (Vendor Rating)
# ============================================================================

class VendorRating(models.Model):
    """협력사 월별 품질 평가"""

    GRADE_CHOICES = [
        ('A', 'A등급 (우수)'),
        ('B', 'B등급 (양호)'),
        ('C', 'C등급 (보통)'),
        ('D', 'D등급 (불량)'),
    ]

    # 평가 기간
    vendor = models.ForeignKey(
        Organization, on_delete=models.PROTECT,
        limit_choices_to={'org_type': 'VENDOR'},
        verbose_name='협력사'
    )
    year = models.IntegerField('연도')
    month = models.IntegerField('월')

    # 품질 지표
    incoming_total = models.IntegerField('수입검사 건수', default=0)
    incoming_pass = models.IntegerField('수입검사 합격', default=0)
    incoming_fail = models.IntegerField('수입검사 불합격', default=0)
    incoming_rate = models.DecimalField('수입검사 합격률(%)', max_digits=5, decimal_places=2, default=100)

    # 납기 지표
    delivery_total = models.IntegerField('납품 건수', default=0)
    delivery_ontime = models.IntegerField('정시납품', default=0)
    delivery_late = models.IntegerField('지연납품', default=0)
    delivery_rate = models.DecimalField('납기준수율(%)', max_digits=5, decimal_places=2, default=100)

    # 클레임 지표
    claim_count = models.IntegerField('클레임 건수', default=0)
    claim_amount = models.DecimalField('클레임 금액', max_digits=12, decimal_places=0, default=0)

    # PPM
    total_incoming_qty = models.IntegerField('총 입고수량', default=0)
    defect_qty = models.IntegerField('불량수량', default=0)
    ppm = models.DecimalField('PPM', max_digits=10, decimal_places=2, default=0)

    # 종합 평가
    quality_score = models.DecimalField('품질점수', max_digits=5, decimal_places=2, default=0)
    delivery_score = models.DecimalField('납기점수', max_digits=5, decimal_places=2, default=0)
    total_score = models.DecimalField('종합점수', max_digits=5, decimal_places=2, default=0)
    grade = models.CharField('등급', max_length=1, choices=GRADE_CHOICES, blank=True)

    remark = models.TextField('비고', blank=True)

    evaluated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name='평가자'
    )
    evaluated_at = models.DateTimeField('평가일시', null=True, blank=True)

    created_at = models.DateTimeField('등록일시', auto_now_add=True)
    updated_at = models.DateTimeField('수정일시', auto_now=True)

    class Meta:
        verbose_name = '협력사평가'
        verbose_name_plural = '협력사 평가 관리'
        ordering = ['-year', '-month', 'vendor']
        unique_together = ['vendor', 'year', 'month']

    def __str__(self):
        return f"[{self.year}-{self.month:02d}] {self.vendor.name} - {self.grade}"

    def calculate_scores(self):
        """점수 자동 계산"""
        # 품질점수 (합격률 기반)
        self.quality_score = float(self.incoming_rate)

        # 납기점수 (준수율 기반)
        self.delivery_score = float(self.delivery_rate)

        # 종합점수 (품질 60%, 납기 40%)
        self.total_score = (self.quality_score * 0.6) + (self.delivery_score * 0.4)

        # 등급 결정
        if self.total_score >= 95:
            self.grade = 'A'
        elif self.total_score >= 85:
            self.grade = 'B'
        elif self.total_score >= 70:
            self.grade = 'C'
        else:
            self.grade = 'D'

    def calculate_ppm(self):
        """PPM 계산"""
        if self.total_incoming_qty > 0:
            self.ppm = (self.defect_qty / self.total_incoming_qty) * 1000000
        else:
            self.ppm = 0


# ============================================================================
# ISIR (Initial Sample Inspection Report) - 초도품 검사
# ============================================================================

class ISIR(models.Model):
    """
    초도품 검사 보고서 (ISIR)
    - 신규 부품/협력사/양산 전 초도품 검사
    - 품질 확인 후 양산 승인
    """

    STATUS_CHOICES = [
        ('DRAFT', '작성중'),
        ('SUBMITTED', '제출'),
        ('REVIEWING', '검토중'),
        ('APPROVED', '승인'),
        ('CONDITIONAL', '조건부승인'),
        ('REJECTED', '반려'),
    ]

    ISIR_TYPE_CHOICES = [
        ('NEW_PART', '신규부품'),
        ('NEW_VENDOR', '신규협력사'),
        ('DESIGN_CHANGE', '설계변경'),
        ('PROCESS_CHANGE', '공정변경'),
        ('MATERIAL_CHANGE', '재질변경'),
        ('RELOCATION', '이전'),
        ('REQUALIFICATION', '재인증'),
    ]

    # 기본 정보
    isir_no = models.CharField('ISIR번호', max_length=30, unique=True)
    isir_type = models.CharField('유형', max_length=20, choices=ISIR_TYPE_CHOICES)
    status = models.CharField('상태', max_length=15, choices=STATUS_CHOICES, default='DRAFT')

    # 협력사
    vendor = models.ForeignKey(
        Organization, on_delete=models.PROTECT,
        limit_choices_to={'org_type': 'VENDOR'},
        verbose_name='협력사'
    )

    # 품목 정보
    part_no = models.CharField('품번', max_length=50)
    part_name = models.CharField('품명', max_length=100)
    part_rev = models.CharField('도면REV', max_length=20, blank=True)
    drawing_no = models.CharField('도면번호', max_length=50, blank=True)

    # 샘플 정보
    sample_qty = models.IntegerField('샘플수량', default=5)
    sample_lot = models.CharField('샘플LOT', max_length=50, blank=True)
    sample_received_date = models.DateField('샘플입고일', null=True, blank=True)

    # 검사 정보
    inspection_date = models.DateField('검사일', null=True, blank=True)
    inspector = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='isir_inspected',
        verbose_name='검사자'
    )

    # 판정
    dimension_result = models.CharField('치수검사', max_length=10, blank=True,
                                         choices=[('PASS', '합격'), ('FAIL', '불합격'), ('NA', 'N/A')])
    appearance_result = models.CharField('외관검사', max_length=10, blank=True,
                                          choices=[('PASS', '합격'), ('FAIL', '불합격'), ('NA', 'N/A')])
    function_result = models.CharField('기능검사', max_length=10, blank=True,
                                        choices=[('PASS', '합격'), ('FAIL', '불합격'), ('NA', 'N/A')])
    material_result = models.CharField('재질검사', max_length=10, blank=True,
                                        choices=[('PASS', '합격'), ('FAIL', '불합격'), ('NA', 'N/A')])

    overall_result = models.CharField('종합판정', max_length=15, blank=True,
                                       choices=[('PASS', '합격'), ('FAIL', '불합격'), ('CONDITIONAL', '조건부')])

    # 상세 내용
    inspection_detail = models.TextField('검사 상세', blank=True)
    issue_found = models.TextField('발견 문제점', blank=True)
    corrective_action = models.TextField('시정조치 요구사항', blank=True)

    # 공정능력 데이터 (Cpk/Ppk)
    cpk_value = models.DecimalField('Cpk', max_digits=5, decimal_places=2, null=True, blank=True,
                                     help_text='공정능력지수 (≥1.33 권장)')
    ppk_value = models.DecimalField('Ppk', max_digits=5, decimal_places=2, null=True, blank=True,
                                     help_text='공정성능지수 (≥1.67 권장)')
    cpk_characteristic = models.CharField('Cpk 측정특성', max_length=100, blank=True,
                                           help_text='Cpk 산출에 사용된 주요 특성')
    spc_sample_size = models.IntegerField('SPC 샘플수', null=True, blank=True,
                                           help_text='공정능력 산출 샘플 수 (최소 30개 권장)')
    process_mean = models.DecimalField('공정평균', max_digits=10, decimal_places=4, null=True, blank=True)
    process_std = models.DecimalField('표준편차', max_digits=10, decimal_places=6, null=True, blank=True)
    usl = models.DecimalField('USL (상한규격)', max_digits=10, decimal_places=4, null=True, blank=True)
    lsl = models.DecimalField('LSL (하한규격)', max_digits=10, decimal_places=4, null=True, blank=True)

    # MSA/Gage R&R 결과
    msa_grr_percent = models.DecimalField('Gage R&R (%)', max_digits=5, decimal_places=2, null=True, blank=True,
                                           help_text='≤10%: 적합, 10~30%: 조건부, >30%: 부적합')
    msa_ndc = models.IntegerField('NDC (구별범주수)', null=True, blank=True,
                                   help_text='Number of Distinct Categories (≥5 권장)')
    msa_result = models.CharField('MSA 판정', max_length=15, blank=True,
                                   choices=[('PASS', '적합'), ('CONDITIONAL', '조건부'), ('FAIL', '부적합')])

    # 첨부파일 (기존 - 호환성 유지)
    report_file = models.FileField('검사성적서', upload_to='qms/isir/reports/', blank=True, null=True)
    photo1 = models.FileField('사진1', upload_to='qms/isir/photos/', blank=True, null=True)
    photo2 = models.FileField('사진2', upload_to='qms/isir/photos/', blank=True, null=True)

    # 승인
    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='isir_approved',
        verbose_name='승인자'
    )
    approved_date = models.DateField('승인일', null=True, blank=True)
    approval_remark = models.TextField('승인비고', blank=True)

    # 조건부 승인 시
    condition_detail = models.TextField('조건부 승인 조건', blank=True)
    condition_due_date = models.DateField('조건 이행기한', null=True, blank=True)

    # 등록 정보
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='isir_created',
        verbose_name='등록자'
    )
    created_at = models.DateTimeField('등록일시', auto_now_add=True)
    updated_at = models.DateTimeField('수정일시', auto_now=True)

    class Meta:
        verbose_name = '초도품검사'
        verbose_name_plural = 'ISIR (초도품검사)'
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.isir_no}] {self.vendor.name} - {self.part_no}"

    def save(self, *args, **kwargs):
        if not self.isir_no:
            self.isir_no = self._generate_no()
        super().save(*args, **kwargs)

    def _generate_no(self):
        from django.utils import timezone
        today = timezone.localdate()
        prefix = f"ISIR-{today.strftime('%Y%m%d')}"
        last = ISIR.objects.filter(isir_no__startswith=prefix).order_by('-isir_no').first()
        seq = int(last.isir_no.split('-')[-1]) + 1 if last else 1
        return f"{prefix}-{seq:03d}"


class ISIRItem(models.Model):
    """ISIR 검사 항목별 측정 결과"""

    isir = models.ForeignKey(ISIR, on_delete=models.CASCADE, related_name='items', verbose_name='ISIR')

    item_no = models.IntegerField('항목번호')
    item_name = models.CharField('검사항목', max_length=100)
    specification = models.CharField('규격', max_length=100)
    tolerance = models.CharField('공차', max_length=50, blank=True)
    unit = models.CharField('단위', max_length=20, blank=True)

    # 측정값 (최대 5개 샘플)
    measured_1 = models.CharField('측정값1', max_length=50, blank=True)
    measured_2 = models.CharField('측정값2', max_length=50, blank=True)
    measured_3 = models.CharField('측정값3', max_length=50, blank=True)
    measured_4 = models.CharField('측정값4', max_length=50, blank=True)
    measured_5 = models.CharField('측정값5', max_length=50, blank=True)

    result = models.CharField('판정', max_length=10,
                               choices=[('PASS', '합격'), ('FAIL', '불합격')],
                               default='PASS')
    remark = models.CharField('비고', max_length=200, blank=True)

    class Meta:
        verbose_name = 'ISIR 검사항목'
        verbose_name_plural = 'ISIR 검사항목'
        ordering = ['isir', 'item_no']

    def __str__(self):
        return f"[{self.isir.isir_no}] #{self.item_no} {self.item_name}"


class ISIRAttachment(models.Model):
    """ISIR 첨부파일 (다중 업로드)"""

    FILE_TYPE_CHOICES = [
        ('DRAWING', '도면'),
        ('REPORT', '검사성적서'),
        ('CERTIFICATE', '인증서/시험성적서'),
        ('SPC', 'SPC 데이터'),
        ('MSA', 'MSA/Gage R&R'),
        ('FMEA', 'FMEA'),
        ('CONTROL_PLAN', 'Control Plan'),
        ('FLOW_CHART', '공정흐름도'),
        ('PHOTO', '사진'),
        ('OTHER', '기타'),
    ]

    isir = models.ForeignKey(ISIR, on_delete=models.CASCADE, related_name='attachments', verbose_name='ISIR')
    file_type = models.CharField('파일유형', max_length=20, choices=FILE_TYPE_CHOICES)
    file = models.FileField('파일', upload_to='qms/isir/attachments/')
    file_name = models.CharField('파일명', max_length=200)
    description = models.CharField('설명', max_length=200, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name='업로드자')
    uploaded_at = models.DateTimeField('업로드일시', auto_now_add=True)

    class Meta:
        verbose_name = 'ISIR 첨부파일'
        verbose_name_plural = 'ISIR 첨부파일'
        ordering = ['file_type', '-uploaded_at']

    def __str__(self):
        return f"[{self.isir.isir_no}] {self.get_file_type_display()} - {self.file_name}"


class ISIRChecklist(models.Model):
    """ISIR/PPAP 체크리스트 (18개 요소)"""

    # PPAP 18개 요소 (AIAG PPAP 4th Edition 기준)
    PPAP_ELEMENTS = [
        (1, 'design_records', '설계기록 (도면)', 'Design Records'),
        (2, 'ecn', '설계변경문서', 'Engineering Change Documents'),
        (3, 'customer_approval', '고객 기술승인', 'Customer Engineering Approval'),
        (4, 'dfmea', '설계 FMEA', 'Design FMEA'),
        (5, 'process_flow', '공정흐름도', 'Process Flow Diagram'),
        (6, 'pfmea', '공정 FMEA', 'Process FMEA'),
        (7, 'control_plan', '관리계획서', 'Control Plan'),
        (8, 'msa', '측정시스템분석', 'Measurement System Analysis'),
        (9, 'dimensional', '치수검사결과', 'Dimensional Results'),
        (10, 'material_test', '재료/성능시험', 'Material/Performance Test'),
        (11, 'initial_process', '초기공정연구 (Cpk)', 'Initial Process Studies'),
        (12, 'lab_doc', '공인시험성적서', 'Qualified Laboratory Documentation'),
        (13, 'aar', '외관승인보고서', 'Appearance Approval Report'),
        (14, 'sample', '샘플 제품', 'Sample Production Parts'),
        (15, 'master_sample', '마스터 샘플', 'Master Sample'),
        (16, 'checking_aids', '검사 보조기구', 'Checking Aids'),
        (17, 'csr', '고객별 요구사항', 'Customer-Specific Requirements'),
        (18, 'psw', '부품제출보증서', 'Part Submission Warrant'),
    ]

    RESULT_CHOICES = [
        ('SUBMITTED', '제출'),
        ('NOT_REQUIRED', '해당없음'),
        ('PENDING', '보류'),
        ('NOT_SUBMITTED', '미제출'),
    ]

    isir = models.OneToOneField(ISIR, on_delete=models.CASCADE, related_name='checklist', verbose_name='ISIR')

    # PPAP 18개 요소 체크 (제출여부)
    elem_01_design_records = models.CharField('설계기록', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_02_ecn = models.CharField('설계변경문서', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_03_customer_approval = models.CharField('고객기술승인', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_04_dfmea = models.CharField('설계FMEA', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_05_process_flow = models.CharField('공정흐름도', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_06_pfmea = models.CharField('공정FMEA', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_07_control_plan = models.CharField('관리계획서', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_08_msa = models.CharField('MSA', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_09_dimensional = models.CharField('치수검사결과', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_10_material_test = models.CharField('재료시험', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_11_initial_process = models.CharField('초기공정연구', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_12_lab_doc = models.CharField('공인시험성적', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_13_aar = models.CharField('외관승인', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_14_sample = models.CharField('샘플제품', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_15_master_sample = models.CharField('마스터샘플', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_16_checking_aids = models.CharField('검사보조기구', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_17_csr = models.CharField('고객요구사항', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')
    elem_18_psw = models.CharField('PSW', max_length=15, choices=RESULT_CHOICES, default='NOT_SUBMITTED')

    # 비고
    remark = models.TextField('비고', blank=True)
    updated_at = models.DateTimeField('수정일시', auto_now=True)

    class Meta:
        verbose_name = 'ISIR 체크리스트'
        verbose_name_plural = 'ISIR 체크리스트'

    def __str__(self):
        return f"[{self.isir.isir_no}] PPAP 체크리스트"

    def get_submission_count(self):
        """제출된 항목 수"""
        count = 0
        for i in range(1, 19):
            field = getattr(self, f'elem_{i:02d}_{self.PPAP_ELEMENTS[i-1][1]}', None)
            if field == 'SUBMITTED':
                count += 1
        return count

    def get_completion_rate(self):
        """제출/해당없음 비율"""
        total = 18
        completed = 0
        for i in range(1, 19):
            field_name = f'elem_{i:02d}_{self.PPAP_ELEMENTS[i-1][1]}'
            value = getattr(self, field_name, None)
            if value in ['SUBMITTED', 'NOT_REQUIRED']:
                completed += 1
        return round(completed / total * 100)


# ============================================================================
# VOC 관리 (Voice of Customer) - 고객 불만/클레임
# ============================================================================

class VOC(models.Model):
    """
    VOC (Voice of Customer) - 고객의 소리
    - 고객 클레임/불만 접수 및 처리
    - 협력사 클레임과 연계 가능
    """

    STATUS_CHOICES = [
        ('RECEIVED', '접수'),
        ('ANALYZING', '원인분석'),
        ('ACTION', '조치중'),
        ('VERIFY', '효과검증'),
        ('CLOSED', '완료'),
    ]

    SEVERITY_CHOICES = [
        ('CRITICAL', '긴급'),
        ('MAJOR', '중대'),
        ('MINOR', '경미'),
    ]

    SOURCE_CHOICES = [
        ('FIELD', '필드클레임'),
        ('LINE_STOP', '라인스톱'),
        ('CUSTOMER_AUDIT', '고객감사'),
        ('COMPLAINT', '일반불만'),
        ('RETURN', '반품'),
        ('WARRANTY', '보증'),
    ]

    # 기본 정보
    voc_no = models.CharField('VOC번호', max_length=30, unique=True)
    status = models.CharField('상태', max_length=15, choices=STATUS_CHOICES, default='RECEIVED')
    severity = models.CharField('심각도', max_length=10, choices=SEVERITY_CHOICES, default='MINOR')
    source = models.CharField('접수경로', max_length=20, choices=SOURCE_CHOICES)

    received_date = models.DateField('접수일')

    # 고객 정보
    customer_name = models.CharField('고객사', max_length=100)
    customer_contact = models.CharField('고객담당자', max_length=50, blank=True)
    customer_phone = models.CharField('연락처', max_length=50, blank=True)

    # 품목 정보
    part_no = models.CharField('품번', max_length=50)
    part_name = models.CharField('품명', max_length=100)
    lot_no = models.CharField('LOT번호', max_length=50, blank=True)

    # 문제 내용
    defect_qty = models.IntegerField('불량수량', default=0)
    defect_type = models.CharField('불량유형', max_length=100)
    defect_detail = models.TextField('불량상세')
    photo = models.FileField('불량사진', upload_to='qms/voc/photos/', blank=True, null=True)

    # 원인 분석
    cause_analysis = models.TextField('원인분석', blank=True)
    root_cause = models.TextField('근본원인', blank=True)
    responsible_type = models.CharField('귀책구분', max_length=20, blank=True,
                                         choices=[('INTERNAL', '사내'), ('VENDOR', '협력사'), ('CUSTOMER', '고객')])

    # 협력사 연계 (협력사 귀책 시)
    linked_vendor = models.ForeignKey(
        Organization, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'org_type': 'VENDOR'},
        verbose_name='귀책 협력사'
    )
    linked_claim = models.ForeignKey(
        VendorClaim, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='voc_links',
        verbose_name='연계된 협력사 클레임'
    )

    # 대책/조치
    immediate_action = models.TextField('즉시대책', blank=True)
    corrective_action = models.TextField('시정조치', blank=True)
    preventive_action = models.TextField('예방조치', blank=True)

    # 비용
    claim_cost = models.DecimalField('클레임비용', max_digits=12, decimal_places=0, null=True, blank=True)

    # 효과 검증
    verification_result = models.TextField('효과검증 결과', blank=True)
    is_effective = models.BooleanField('효과있음', null=True, blank=True)

    # 담당자
    received_by = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='voc_received',
        verbose_name='접수자'
    )
    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='voc_assigned',
        verbose_name='담당자'
    )

    due_date = models.DateField('처리기한', null=True, blank=True)
    closed_date = models.DateField('완료일', null=True, blank=True)

    created_at = models.DateTimeField('등록일시', auto_now_add=True)
    updated_at = models.DateTimeField('수정일시', auto_now=True)

    class Meta:
        verbose_name = 'VOC'
        verbose_name_plural = 'VOC (고객의소리)'
        ordering = ['-received_date', '-id']

    def __str__(self):
        return f"[{self.voc_no}] {self.customer_name} - {self.defect_type}"

    def save(self, *args, **kwargs):
        if not self.voc_no:
            self.voc_no = self._generate_no()
        super().save(*args, **kwargs)

    def _generate_no(self):
        today = timezone.localdate()
        prefix = f"VOC-{today.strftime('%Y%m%d')}"
        last = VOC.objects.filter(voc_no__startswith=prefix).order_by('-voc_no').first()
        seq = int(last.voc_no.split('-')[-1]) + 1 if last else 1
        return f"{prefix}-{seq:03d}"

    @property
    def is_overdue(self):
        """처리기한 초과 여부"""
        if self.status == 'CLOSED' or not self.due_date:
            return False
        return timezone.localdate() > self.due_date


class VOCAttachment(models.Model):
    """VOC 첨부파일"""

    voc = models.ForeignKey(VOC, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField('파일', upload_to='qms/voc/attachments/')
    file_name = models.CharField('파일명', max_length=200)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    uploaded_at = models.DateTimeField('업로드일시', auto_now_add=True)

    class Meta:
        verbose_name = 'VOC첨부파일'
        verbose_name_plural = 'VOC첨부파일'


# ============================================================================
# 계측기 관리 (Gauge Management)
# ============================================================================

class Gauge(models.Model):
    """
    계측기 관리
    - 측정기기 등록 및 교정 이력 관리
    - IATF 16949 요구사항 대응
    """

    STATUS_CHOICES = [
        ('ACTIVE', '사용중'),
        ('CALIBRATING', '교정중'),
        ('REPAIR', '수리중'),
        ('SCRAPPED', '폐기'),
        ('INACTIVE', '미사용'),
    ]

    TYPE_CHOICES = [
        ('CALIPER', '버니어캘리퍼스'),
        ('MICROMETER', '마이크로미터'),
        ('DIAL_GAUGE', '다이얼게이지'),
        ('HEIGHT_GAUGE', '하이트게이지'),
        ('SCALE', '저울'),
        ('THERMOMETER', '온도계'),
        ('PRESSURE', '압력계'),
        ('TORQUE', '토크렌치'),
        ('CMM', '3차원측정기'),
        ('TESTER', '시험기'),
        ('OTHER', '기타'),
    ]

    CALIBRATION_TYPE_CHOICES = [
        ('INTERNAL', '사내교정'),
        ('EXTERNAL', '외부교정'),
    ]

    # 기본 정보
    gauge_no = models.CharField('관리번호', max_length=30, unique=True)
    gauge_name = models.CharField('계측기명', max_length=100)
    gauge_type = models.CharField('유형', max_length=20, choices=TYPE_CHOICES)
    status = models.CharField('상태', max_length=15, choices=STATUS_CHOICES, default='ACTIVE')

    # 제원
    manufacturer = models.CharField('제조사', max_length=100, blank=True)
    model_no = models.CharField('모델번호', max_length=50, blank=True)
    serial_no = models.CharField('시리얼번호', max_length=50, blank=True)
    measurement_range = models.CharField('측정범위', max_length=100, blank=True)
    resolution = models.CharField('분해능', max_length=50, blank=True)
    accuracy = models.CharField('정밀도', max_length=50, blank=True)

    # 위치/용도
    location = models.CharField('설치장소', max_length=100, blank=True)
    department = models.CharField('관리부서', max_length=50, blank=True)
    usage = models.CharField('용도', max_length=200, blank=True)

    # 교정 관련
    calibration_type = models.CharField('교정유형', max_length=10, choices=CALIBRATION_TYPE_CHOICES, default='EXTERNAL')
    calibration_cycle = models.IntegerField('교정주기(월)', default=12)
    last_calibration_date = models.DateField('최근교정일', null=True, blank=True)
    next_calibration_date = models.DateField('차기교정일', null=True, blank=True)
    calibration_agency = models.CharField('교정기관', max_length=100, blank=True)

    # 구입 정보
    purchase_date = models.DateField('구입일', null=True, blank=True)
    purchase_cost = models.DecimalField('구입가격', max_digits=12, decimal_places=0, null=True, blank=True)

    # 담당자
    manager = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='managed_gauges',
        verbose_name='관리담당자'
    )

    # 첨부
    photo = models.FileField('사진', upload_to='qms/gauge/photos/', blank=True, null=True)
    manual = models.FileField('매뉴얼', upload_to='qms/gauge/manuals/', blank=True, null=True)

    remark = models.TextField('비고', blank=True)

    created_at = models.DateTimeField('등록일시', auto_now_add=True)
    updated_at = models.DateTimeField('수정일시', auto_now=True)

    class Meta:
        verbose_name = '계측기'
        verbose_name_plural = '계측기 관리'
        ordering = ['gauge_no']

    def __str__(self):
        return f"[{self.gauge_no}] {self.gauge_name}"

    @property
    def is_calibration_due(self):
        """교정 기한 임박/초과 여부"""
        if not self.next_calibration_date:
            return False
        days_left = (self.next_calibration_date - timezone.localdate()).days
        return days_left <= 30

    @property
    def calibration_status_text(self):
        """교정 상태 텍스트"""
        if not self.next_calibration_date:
            return '미설정'
        days_left = (self.next_calibration_date - timezone.localdate()).days
        if days_left < 0:
            return f'기한초과 ({abs(days_left)}일)'
        elif days_left <= 7:
            return f'긴급 (D-{days_left})'
        elif days_left <= 30:
            return f'임박 (D-{days_left})'
        else:
            return f'정상 (D-{days_left})'


class GaugeCalibration(models.Model):
    """계측기 교정 이력"""

    RESULT_CHOICES = [
        ('PASS', '합격'),
        ('FAIL', '불합격'),
        ('ADJUSTED', '조정후합격'),
    ]

    gauge = models.ForeignKey(Gauge, on_delete=models.CASCADE, related_name='calibrations', verbose_name='계측기')

    calibration_date = models.DateField('교정일')
    calibration_type = models.CharField('교정유형', max_length=10,
                                         choices=[('INTERNAL', '사내'), ('EXTERNAL', '외부')])
    calibration_agency = models.CharField('교정기관', max_length=100, blank=True)

    result = models.CharField('결과', max_length=10, choices=RESULT_CHOICES)
    certificate_no = models.CharField('성적서번호', max_length=50, blank=True)

    # 측정 불확도
    uncertainty = models.CharField('측정불확도', max_length=100, blank=True)

    # 비용
    cost = models.DecimalField('교정비용', max_digits=10, decimal_places=0, null=True, blank=True)

    # 다음 교정일
    next_date = models.DateField('차기교정일', null=True, blank=True)

    # 교정 성적서
    certificate_file = models.FileField('교정성적서', upload_to='qms/gauge/certificates/', blank=True, null=True)

    performed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name='교정자/등록자'
    )

    remark = models.TextField('비고', blank=True)
    created_at = models.DateTimeField('등록일시', auto_now_add=True)

    class Meta:
        verbose_name = '교정이력'
        verbose_name_plural = '교정이력'
        ordering = ['-calibration_date']

    def __str__(self):
        return f"[{self.gauge.gauge_no}] {self.calibration_date} 교정"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # 계측기 최근/차기 교정일 업데이트
        if self.result in ('PASS', 'ADJUSTED'):
            self.gauge.last_calibration_date = self.calibration_date
            if self.next_date:
                self.gauge.next_calibration_date = self.next_date
            self.gauge.save(update_fields=['last_calibration_date', 'next_calibration_date'])


# ============================================================================
# 품질문서 관리 (Quality Document Management)
# ============================================================================

class QualityDocument(models.Model):
    """
    품질문서 관리
    - 검사기준서, 작업표준서, 관리계획서 등 문서 관리
    - 버전/개정 이력 관리
    """

    STATUS_CHOICES = [
        ('DRAFT', '작성중'),
        ('REVIEW', '검토중'),
        ('APPROVED', '승인'),
        ('OBSOLETE', '폐기'),
    ]

    CATEGORY_CHOICES = [
        ('MANUAL', '매뉴얼'),
        ('PROCEDURE', '절차서'),
        ('INSTRUCTION', '지시서/기준서'),
        ('FORM', '양식'),
        ('SPEC', '규격서'),
        ('DRAWING', '도면'),
        ('CONTROL_PLAN', '관리계획서'),
        ('FMEA', 'FMEA'),
        ('SOP', '작업표준서'),
        ('INSPECTION', '검사기준서'),
        ('OTHER', '기타'),
    ]

    # 기본 정보
    doc_no = models.CharField('문서번호', max_length=50, unique=True)
    doc_name = models.CharField('문서명', max_length=200)
    category = models.CharField('문서유형', max_length=20, choices=CATEGORY_CHOICES)
    status = models.CharField('상태', max_length=15, choices=STATUS_CHOICES, default='DRAFT')

    # 버전
    version = models.CharField('버전', max_length=20, default='1.0')
    revision = models.IntegerField('개정차수', default=0)

    # 내용
    description = models.TextField('문서설명', blank=True)

    # 파일
    file = models.FileField('문서파일', upload_to='qms/documents/')
    file_name = models.CharField('파일명', max_length=200)

    # 관련 정보
    related_part_no = models.CharField('관련품번', max_length=50, blank=True)
    related_process = models.CharField('관련공정', max_length=100, blank=True)

    # 유효기간
    effective_date = models.DateField('시행일')
    expiry_date = models.DateField('만료일', null=True, blank=True)

    # 작성/검토/승인
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='qdoc_created',
        verbose_name='작성자'
    )
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='qdoc_reviewed',
        verbose_name='검토자'
    )
    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='qdoc_approved',
        verbose_name='승인자'
    )

    reviewed_at = models.DateTimeField('검토일시', null=True, blank=True)
    approved_at = models.DateTimeField('승인일시', null=True, blank=True)

    # 관리
    is_controlled = models.BooleanField('관리문서', default=True,
                                         help_text='체크시 배포/회수 관리 대상')

    remark = models.TextField('비고', blank=True)

    created_at = models.DateTimeField('등록일시', auto_now_add=True)
    updated_at = models.DateTimeField('수정일시', auto_now=True)

    class Meta:
        verbose_name = '품질문서'
        verbose_name_plural = '품질문서 관리'
        ordering = ['category', 'doc_no']

    def __str__(self):
        return f"[{self.doc_no}] {self.doc_name} (Rev.{self.revision})"

    @property
    def is_expired(self):
        """만료 여부"""
        if not self.expiry_date:
            return False
        return timezone.localdate() > self.expiry_date


class DocumentRevision(models.Model):
    """문서 개정 이력"""

    document = models.ForeignKey(
        QualityDocument, on_delete=models.CASCADE,
        related_name='revisions',
        verbose_name='문서'
    )

    revision = models.IntegerField('개정차수')
    version = models.CharField('버전', max_length=20)

    change_reason = models.TextField('개정사유')
    change_detail = models.TextField('개정내용')

    # 이전 버전 파일 보관
    previous_file = models.FileField('이전파일', upload_to='qms/documents/archive/', blank=True, null=True)

    revised_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        verbose_name='개정자'
    )
    revised_at = models.DateTimeField('개정일시', auto_now_add=True)

    class Meta:
        verbose_name = '개정이력'
        verbose_name_plural = '개정이력'
        ordering = ['-revision']
        unique_together = ['document', 'revision']

    def __str__(self):
        return f"[{self.document.doc_no}] Rev.{self.revision}"