from django.db import models
from django.contrib.auth.models import User
from django.conf import settings

class M4Request(models.Model):
    # --- [상태값 수정: DRAFT 추가] ---
    STATUS_CHOICES = [
        ('DRAFT', '작성중'),           # 기안자가 '결재' 버튼을 누르기 전 초기 상태
        ('PENDING_REVIEW', '검토대기'),
        ('PENDING_APPROVE', '승인대기'),
        ('APPROVED', '승인완료'),
        ('REJECTED', '반려'),
    ]

    M4_TYPES = [
        ('MAN', '작업자(Man)'),
        ('MACHINE', '설비(Machine)'),
        ('MATERIAL', '재료(Material)'),
        ('METHOD', '공법(Method)'),
    ]

    # 기본 정보 필드
    user = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="신청자")
    part_no = models.CharField(max_length=50, verbose_name="품번")
    part_name = models.CharField(max_length=100, verbose_name="품명")
    request_no = models.CharField(max_length=20, unique=True, verbose_name="관리번호")
    m4_type = models.CharField(max_length=10, choices=M4_TYPES, verbose_name="변경구분")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT', verbose_name="진행상태")
    
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
    photo_before = models.FileField(upload_to='qms/m4/', null=True, blank=True, verbose_name="변경전 사진")
    photo_after = models.FileField(upload_to='qms/m4/', null=True, blank=True, verbose_name="변경후 사진")

    # 결재 시스템 필드
    reviewer_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='m4_reviewer_set', verbose_name="검토자")
    approver_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='m4_approver_set', verbose_name="최종승인자")
    is_submitted = models.BooleanField(default=False, verbose_name="기안여부")
    submitted_at = models.DateTimeField(null=True, blank=True, verbose_name="기안일시")
    is_reviewed = models.BooleanField(default=False, verbose_name="검토여부")
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="검토일시")
    is_approved = models.BooleanField(default=False, verbose_name="승인여부")
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name="승인일시")

    def __str__(self):
        return f"{self.request_no} - {self.part_no}"

class M4Review(models.Model):
    request = models.ForeignKey(M4Request, on_delete=models.CASCADE, related_name='reviews')
    department = models.CharField(max_length=50, verbose_name="검토부서")
    reviewer_name = models.CharField(max_length=50, blank=True, null=True, verbose_name="검토자성함")
    reviewer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="기록자") 
    
    # [수정] 품질팀의 요청 사항 필드 추가
    request_content = models.TextField(null=True, blank=True, verbose_name="품질팀 요청사항")
    
    # 해당 부서의 답변 내용
    content = models.TextField(null=True, blank=True, verbose_name="검토답변")
    
    # [수정] 날짜 필드에서 auto_now 속성 제거 (View에서 직접 제어하여 정확한 시점 기록)
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name="발송일(요청)")
    received_at = models.DateTimeField(null=True, blank=True, verbose_name="접수일(회신)")

    def __str__(self):
        return f"[{self.department}] {self.request.request_no} 검토"

class M4ChangeLog(models.Model):
    request = models.ForeignKey(M4Request, on_delete=models.CASCADE, related_name='change_logs', verbose_name="관련요청")
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="수정자")
    field_name = models.CharField(max_length=50, verbose_name="수정필드")
    old_value = models.TextField(null=True, blank=True, verbose_name="이전내용")
    new_value = models.TextField(null=True, blank=True, verbose_name="변경내용")
    changed_at = models.DateTimeField(auto_now_add=True, verbose_name="수정일시")

    class Meta:
        ordering = ['-changed_at']

    def __str__(self):
        return f"{self.request.request_no} - {self.field_name} 변경"