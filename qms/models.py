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
    model_name = models.CharField(max_length=50, blank=True, null=True, verbose_name="차종")
    quality_rank = models.CharField(max_length=10, blank=True, null=True, verbose_name="품질RANK")
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

    status = models.CharField("검사상태", max_length=20, choices=STATUS_CHOICES, default='PENDING')
    
    # 검사 결과 데이터
    inspector = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="검사자")
    inspected_at = models.DateTimeField("검사일시", null=True, blank=True)
    
    # 검사 항목 (간략화 버전)
    check_report = models.BooleanField("성적서 확인", default=False)
    check_visual = models.BooleanField("외관 검사", default=False)
    check_dimension = models.BooleanField("치수/기능 검사", default=False)
    
    remark = models.TextField("검사 의견", blank=True, null=True)
    attachment = models.FileField("검사 성적서/사진", upload_to="qms/inspection/", blank=True, null=True)

    created_at = models.DateTimeField("요청일시", auto_now_add=True)

    class Meta:
        verbose_name = "수입검사"
        verbose_name_plural = "수입검사 관리"

    def __str__(self):
        return f"검사요청: {self.inbound_transaction.part.part_name}"