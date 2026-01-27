from django.db import models
from django.contrib.auth.models import User

# 1. 협력사(Vendor)
class Vendor(models.Model):
    name = models.CharField(max_length=100, verbose_name="업체명")
    code = models.CharField(max_length=20, unique=True, verbose_name='업체코드', default='V000')
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True)
    can_view_inventory = models.BooleanField(default=True, verbose_name="과부족 조회 권한")

    # 납품서 출력용 정보
    biz_registration_number = models.CharField(max_length=20, blank=True, null=True, verbose_name="사업자등록번호")
    erp_code = models.CharField(max_length=50, blank=True, null=True, verbose_name="ERP 업체코드")
    representative = models.CharField(max_length=50, blank=True, null=True, verbose_name="성명(대표이사)")
    address = models.CharField(max_length=255, blank=True, null=True, verbose_name="주소")
    biz_type = models.CharField(max_length=100, blank=True, null=True, verbose_name="업태")
    biz_item = models.CharField(max_length=100, blank=True, null=True, verbose_name="종목")

    class Meta:
        verbose_name = "협력사"
        verbose_name_plural = "협력사 관리"

    def __str__(self):
        return f"[{self.code}] {self.name}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # 연결된 Organization이 없으면 자동 생성
        if not hasattr(self, 'organization') or self.organization is None:
            Organization.objects.create(
                name=self.name,
                org_type="VENDOR",
                linked_vendor=self
            )

# 1-1. 조직(회사/협력사)
class Organization(models.Model):
    ORG_TYPE_CHOICES = [
        ("INTERNAL", "내부"),
        ("VENDOR", "협력사"),
    ]
    name = models.CharField(max_length=100, unique=True, verbose_name="조직명(회사/협력사)")
    org_type = models.CharField(max_length=20, choices=ORG_TYPE_CHOICES, verbose_name="조직 구분")
    linked_vendor = models.OneToOneField(
        Vendor, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="organization", verbose_name="연결된 협력사"
    )

    class Meta:
        verbose_name = "내부 조직"
        verbose_name_plural = "내부 조직 관리"

    def __str__(self):
        return f"{self.name} [{self.get_org_type_display()}]"

# 2. 품목 마스터(Part)
class Part(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, verbose_name='전담 업체',
                               null=True, blank=True)  # nullable: WMS 전용 품목은 업체 없음
    part_group = models.CharField(max_length=50, verbose_name='품목군', default='일반')
    part_no = models.CharField(max_length=50, verbose_name='품번', unique=True)  # 품번은 전체에서 유니크
    part_name = models.CharField(max_length=100, verbose_name='품명')

    class Meta:
        verbose_name = "품목 마스터"
        verbose_name_plural = "품목 마스터 관리"

    def __str__(self):
        vendor_name = self.vendor.name if self.vendor else "WMS전용"
        return f"[{self.part_group}] {self.part_no} ({vendor_name})"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            Inventory.objects.get_or_create(part=self)

# 2-1. 소요량 관리 (Demand)
class Demand(models.Model):
    part = models.ForeignKey(Part, on_delete=models.CASCADE, verbose_name="품목")
    due_date = models.DateField(verbose_name="소요 발생일(납기일)")
    quantity = models.IntegerField(verbose_name="필요 소요량")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "소요량 관리"
        verbose_name_plural = "소요량 관리"

    def __str__(self):
        return f"{self.part.part_no} 소요: {self.quantity}개 ({self.due_date})"

# 3. 발주 정보(Order)
class Order(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, verbose_name="협력사")
    part_group = models.CharField(max_length=50, verbose_name='품목군', blank=True, null=True)
    part_no = models.CharField(max_length=50, verbose_name="품번")
    part_name = models.CharField(max_length=100, verbose_name="품명")
    
    quantity = models.IntegerField(verbose_name="수량")
    due_date = models.DateField(verbose_name="납기일")
    
    # [ERP 연동 필드]
    erp_order_no = models.CharField(max_length=50, blank=True, null=True, verbose_name="ERP 발주번호")
    erp_order_seq = models.CharField(max_length=20, blank=True, null=True, verbose_name="ERP 발주순번")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="발주등록일")
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name="발주승인일")
    is_closed = models.BooleanField(default=False, verbose_name="발주마감여부")
    
    class Meta:
        verbose_name = "발주서"
        verbose_name_plural = "발주서 관리"

    def __str__(self):
        return f"{self.part_name} ({self.quantity}개)"

# 4. 기초재고 관리 (Inventory)
class Inventory(models.Model):
    part = models.OneToOneField(Part, on_delete=models.CASCADE, verbose_name="품목")
    base_stock = models.IntegerField(default=0, verbose_name="기초재고(현재고)")
    last_inventory_date = models.DateField(null=True, blank=True, verbose_name="재고실사기준일")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="최종갱신일")

    class Meta:
        verbose_name = "기초재고 관리"
        verbose_name_plural = "1. 기초재고 관리"

    def __str__(self):
        return f"{self.part.part_no} 기초재고"

# 5. 일자별 입고 관리 (Incoming)
class Incoming(models.Model):
    part = models.ForeignKey(Part, on_delete=models.CASCADE, verbose_name="품목")
    in_date = models.DateField(verbose_name="입고예정일")
    quantity = models.IntegerField(verbose_name="입고수량")
    delivery_order_no = models.CharField(max_length=50, null=True, blank=True, verbose_name="연결 납품서 번호")
    
    # [✅ 신규 추가] ERP 추적용 필드 (입고 시점에도 남겨둠)
    erp_order_no = models.CharField(max_length=50, blank=True, null=True, verbose_name="ERP 발주번호")
    erp_order_seq = models.CharField(max_length=20, blank=True, null=True, verbose_name="ERP 발주순번")
    confirmed_qty = models.IntegerField("확정(양품)수량", default=0, null=True, blank=True)
    
    def __str__(self):
        return f"{self.part.part_name} ({self.quantity})"

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "일자별 입고 관리"
        verbose_name_plural = "2. 일자별 입고 관리"

    def __str__(self):
        return f"{self.part.part_no} 입고 ({self.in_date})"

# 6. 라벨 발행 이력 (LabelPrintLog)
class LabelPrintLog(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, verbose_name="협력사")
    part = models.ForeignKey(Part, on_delete=models.CASCADE, verbose_name="품목")
    part_no = models.CharField(max_length=50, verbose_name="품번")
    printed_qty = models.IntegerField(verbose_name="발행수량")
    snp = models.IntegerField(verbose_name="포장단위(SNP)")
    
    # [✅ 신규 추가] 어떤 발주 건에 대한 라벨인지 연결 (FK)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="원본 발주서")
    
    printed_at = models.DateTimeField(auto_now_add=True, verbose_name="발행일시")

    class Meta:
        verbose_name = "라벨 발행 이력"
        verbose_name_plural = "3. 라벨 발행 이력"

# 7. 납품서 (DeliveryOrder)
class DeliveryOrder(models.Model):
    order_no = models.CharField(max_length=50, unique=True, verbose_name="납품서 번호")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")
    is_received = models.BooleanField(default=False, verbose_name="입고완료여부")

    STATUS_CHOICES = [
        ('PENDING', '배송중/대기'),
        ('RECEIVED', '입고완료(검사대기)'),
        ('APPROVED', '검사합격(입고확정)'),
        ('REJECTED', '검사불합격(반려)'),
    ]
    status = models.CharField("상태", max_length=20, choices=STATUS_CHOICES, default='PENDING')

    class Meta:
        verbose_name = "납품서"
        verbose_name_plural = "4. 납품서 관리"

    def __str__(self):
        return f"[{self.status}] {self.order_no}"

# 8. 납품서 상세 품목 (DeliveryOrderItem)
class DeliveryOrderItem(models.Model):
    order = models.ForeignKey(DeliveryOrder, on_delete=models.CASCADE, related_name='items', verbose_name="납품서 번호")
    
    # [✅ 신규 추가] ERP 추적용 필드 및 원본 발주 연결
    linked_order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="원본 발주서")
    erp_order_no = models.CharField(max_length=50, blank=True, null=True, verbose_name="ERP 발주번호")
    erp_order_seq = models.CharField(max_length=20, blank=True, null=True, verbose_name="ERP 발주순번")

    part_no = models.CharField(max_length=100, verbose_name="품번")
    part_name = models.CharField(max_length=200, verbose_name="품명")
    snp = models.IntegerField(default=0, verbose_name="SNP(박스당수량)")
    box_count = models.IntegerField(default=0, verbose_name="박스 수")
    total_qty = models.IntegerField(default=0, verbose_name="총 수량")
    lot_no = models.DateField(verbose_name="LOT 번호(생산일)", null=True, blank=True)

    class Meta:
        verbose_name = "납품서 상세 품목"
        verbose_name_plural = "납품서 상세 품목"

# 9. 유저 프로필 확장
class UserProfile(models.Model):
    ROLE_ADMIN = 'ADMIN'
    ROLE_STAFF = 'STAFF'
    ROLE_VENDOR = 'VENDOR'

    ROLE_CHOICES = [
        (ROLE_ADMIN, '0. 관리자 (전체 권한)'),
        (ROLE_STAFF, '1. 직원'),
        (ROLE_VENDOR, '2. 협력업체'),
    ]

    ACCOUNT_TYPE_CHOICES = [
        ("INTERNAL", "내부"),
        ("VENDOR", "협력사"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_VENDOR, verbose_name="사용자 그룹")

    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES, default="VENDOR", verbose_name="계정 구분")
    org = models.ForeignKey("orders.Organization", on_delete=models.SET_NULL, null=True, blank=True, related_name="user_profiles", verbose_name="소속 조직(회사/협력사)")

    display_name = models.CharField(max_length=50, blank=True, null=True, verbose_name="표시 이름")
    employee_no = models.CharField(max_length=30, blank=True, null=True, verbose_name="사번")
    department = models.CharField(max_length=50, blank=True, null=True, verbose_name="부서/소속")
    position = models.CharField(max_length=30, blank=True, null=True, verbose_name="직급")
    job_title = models.CharField(max_length=30, blank=True, null=True, verbose_name="직책")

    # ========== SCM 권한 (View/Edit 분리) ==========
    # 발주 관리
    can_scm_order_view = models.BooleanField(default=False, verbose_name="발주 조회")
    can_scm_order_edit = models.BooleanField(default=False, verbose_name="발주 등록/수정/삭제")

    # 과부족/소요량 관리
    can_scm_inventory_view = models.BooleanField(default=False, verbose_name="과부족 조회")
    can_scm_inventory_edit = models.BooleanField(default=False, verbose_name="소요량 등록/수정")

    # 입고 관리
    can_scm_incoming_view = models.BooleanField(default=False, verbose_name="입고 조회")
    can_scm_incoming_edit = models.BooleanField(default=False, verbose_name="입고 처리/취소")

    # 납품서 관리
    can_scm_label_view = models.BooleanField(default=False, verbose_name="납품서 조회")
    can_scm_label_edit = models.BooleanField(default=False, verbose_name="납품서 등록/삭제")

    # 리포트/관리
    can_scm_report = models.BooleanField(default=False, verbose_name="리포트 조회")
    can_scm_admin = models.BooleanField(default=False, verbose_name="통합 관리자")

    # ========== WMS 권한 (View/Edit 분리) ==========
    # 재고 관리
    can_wms_stock_view = models.BooleanField(default=False, verbose_name="재고 조회")
    can_wms_stock_edit = models.BooleanField(default=False, verbose_name="재고 이동/조정")

    # 입출고 관리
    can_wms_inout_view = models.BooleanField(default=False, verbose_name="입출고 조회")
    can_wms_inout_edit = models.BooleanField(default=False, verbose_name="입출고 처리")

    # BOM 관리
    can_wms_bom_view = models.BooleanField(default=False, verbose_name="BOM 조회")
    can_wms_bom_edit = models.BooleanField(default=False, verbose_name="BOM 등록/수정")

    # ========== QMS 권한 (View/Edit 분리) ==========
    # 4M 변경점 관리
    can_qms_4m_view = models.BooleanField(default=False, verbose_name="4M 조회")
    can_qms_4m_edit = models.BooleanField(default=False, verbose_name="4M 등록/수정")

    # 수입검사 관리
    can_qms_inspection_view = models.BooleanField(default=False, verbose_name="검사 조회")
    can_qms_inspection_edit = models.BooleanField(default=False, verbose_name="검사 등록/판정")

    # 부적합품/시정조치
    can_qms_nc_view = models.BooleanField(default=False, verbose_name="부적합/CAPA 조회")
    can_qms_nc_edit = models.BooleanField(default=False, verbose_name="부적합/CAPA 등록/처리")

    # 클레임 관리
    can_qms_claim_view = models.BooleanField(default=False, verbose_name="클레임 조회")
    can_qms_claim_edit = models.BooleanField(default=False, verbose_name="클레임 등록/처리")

    # ISIR (초도품검사)
    can_qms_isir_view = models.BooleanField(default=False, verbose_name="ISIR 조회")
    can_qms_isir_edit = models.BooleanField(default=False, verbose_name="ISIR 등록/승인")

    # 협력사 평가
    can_qms_rating_view = models.BooleanField(default=False, verbose_name="협력사평가 조회")
    can_qms_rating_edit = models.BooleanField(default=False, verbose_name="협력사평가 등록")

    # ========== 레거시 필드 (호환성 유지) ==========
    can_view_orders = models.BooleanField(default=False, verbose_name="[구]발주 조회")
    can_register_orders = models.BooleanField(default=False, verbose_name="[구]발주 등록")
    can_view_inventory = models.BooleanField(default=False, verbose_name="[구]과부족 조회")
    can_manage_incoming = models.BooleanField(default=False, verbose_name="[구]입고 관리")
    can_manage_parts = models.BooleanField(default=False, verbose_name="[구]품목 관리")
    can_view_reports = models.BooleanField(default=False, verbose_name="[구]리포트 조회")
    can_access_scm_admin = models.BooleanField(default=False, verbose_name="[구]통합 관리자")
    can_access_wms = models.BooleanField(default=False, verbose_name="[구]WMS 접근")
    can_wms_inout = models.BooleanField(default=False, verbose_name="[구]입출고 처리")
    can_wms_adjustment = models.BooleanField(default=False, verbose_name="[구]재고 조정")
    can_wms_bom = models.BooleanField(default=False, verbose_name="[구]BOM 관리")
    can_access_qms = models.BooleanField(default=False, verbose_name="[구]QMS 접근")
    can_qms_inspection = models.BooleanField(default=False, verbose_name="[구]검사 관리")
    can_qms_4m = models.BooleanField(default=False, verbose_name="[구]4M 관리")

    is_jinyoung_staff = models.BooleanField(default=False, verbose_name="진영전기 직원 여부(기본)")

    class Meta:
        verbose_name = "유저 권한 설정"
        verbose_name_plural = "유저 권한 설정"

    @property
    def is_internal(self) -> bool:
        return self.account_type == "INTERNAL"

    def save(self, *args, **kwargs):
        # 새로 생성되는 협력사 계정에 기본 권한 자동 부여
        is_new = self.pk is None
        if is_new and (self.role == self.ROLE_VENDOR or self.account_type == "VENDOR"):
            # 협력사 기본 권한: 발주 조회, 과부족 조회, 납품서 조회/등록, 4M 조회
            self.can_scm_order_view = True
            self.can_scm_inventory_view = True
            self.can_scm_label_view = True
            self.can_scm_label_edit = True
            self.can_qms_4m_view = True
        super().save(*args, **kwargs)

    def __str__(self):
        name = self.display_name or self.user.get_full_name() or self.user.username
        return f"{name} ({self.get_role_display()})"

        # orders/models.py (파일 맨 아래에 추가)

class ReturnLog(models.Model):
    """
    부적합 반출(반품) 관리 테이블
    - 수입검사 불량 시 데이터 생성 (is_confirmed=False)
    - 협력사 확인 시 (is_confirmed=True) -> 재고 차감 및 납품가능수량 복구
    """
    delivery_order = models.ForeignKey('DeliveryOrder', on_delete=models.CASCADE, verbose_name="관련 납품서")
    part = models.ForeignKey('Part', on_delete=models.CASCADE, verbose_name="품목")
    quantity = models.IntegerField("반출 대상 수량")
    
    # 불량 사유 (QMS에서 입력한 내용 연동)
    reason = models.CharField("반출/불량 사유", max_length=255, blank=True)

    created_at = models.DateTimeField("판정 일시", auto_now_add=True)
    
    # 협력사 확인 여부
    is_confirmed = models.BooleanField("반출 확인 여부", default=False)
    confirmed_at = models.DateTimeField("반출 확인 일시", null=True, blank=True)

    def __str__(self):
        status = "확인완료" if self.is_confirmed else "미확인"
        return f"[{status}] {self.part.part_name} - {self.quantity}ea"


class VendorMonthlyPerformance(models.Model):
    """
    협력사 월별 납기준수율 기록
    - 매월 마감 시 해당 월의 실적을 저장
    - 마감된 데이터는 수정 불가 (is_closed=True)
    """
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, verbose_name="협력사")
    year_month = models.CharField(max_length=7, verbose_name="년월")  # "2026-01"

    # 발주 데이터
    order_qty = models.IntegerField(default=0, verbose_name="발주수량")

    # 입고 데이터
    incoming_qty = models.IntegerField(default=0, verbose_name="입고수량")
    on_time_qty = models.IntegerField(default=0, verbose_name="납기준수 수량")
    delayed_qty = models.IntegerField(default=0, verbose_name="납기지연 수량")

    # 계산된 지표
    compliance_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0, verbose_name="납기준수율(%)")
    incoming_rate = models.DecimalField(max_digits=6, decimal_places=2, default=0, verbose_name="입고율(%)")
    avg_lead_time = models.DecimalField(max_digits=5, decimal_places=1, default=0, verbose_name="평균 리드타임(일)")

    # 등급 (마감 시점 기준)
    GRADE_CHOICES = [
        ('A', 'A등급 (95%+)'),
        ('B', 'B등급 (85-95%)'),
        ('C', 'C등급 (85%미만)'),
    ]
    grade = models.CharField(max_length=1, choices=GRADE_CHOICES, default='C', verbose_name="등급")

    # 마감 상태
    is_closed = models.BooleanField(default=False, verbose_name="마감 여부")
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name="마감 일시")
    closed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="마감 처리자")

    # 메타 정보
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="수정일시")

    class Meta:
        verbose_name = "협력사 월별 실적"
        verbose_name_plural = "협력사 월별 실적"
        unique_together = ['vendor', 'year_month']
        ordering = ['-year_month', 'vendor__name']

    def __str__(self):
        status = "마감" if self.is_closed else "미마감"
        return f"{self.vendor.name} {self.year_month} [{status}] 준수율:{self.compliance_rate}%"

    def calculate_grade(self):
        """준수율에 따른 등급 계산"""
        if self.compliance_rate >= 95:
            return 'A'
        elif self.compliance_rate >= 85:
            return 'B'
        else:
            return 'C'


# ============================================
# 공지사항 / QnA
# ============================================

class Notice(models.Model):
    """공지사항"""
    title = models.CharField("제목", max_length=200)
    content = models.TextField("내용")
    is_important = models.BooleanField("중요 공지", default=False)
    is_active = models.BooleanField("노출 여부", default=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="작성자")
    created_at = models.DateTimeField("작성일", auto_now_add=True)
    updated_at = models.DateTimeField("수정일", auto_now=True)

    class Meta:
        verbose_name = "공지사항"
        verbose_name_plural = "공지사항"
        ordering = ['-is_important', '-created_at']

    def __str__(self):
        return f"{'[중요] ' if self.is_important else ''}{self.title}"


class QnA(models.Model):
    """Q&A (질문/답변)"""
    title = models.CharField("제목", max_length=200)
    content = models.TextField("질문 내용")

    # 질문자 (협력사 또는 직원)
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='qna_questions', verbose_name="질문자")
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="협력사")

    # 답변
    answer = models.TextField("답변", blank=True, null=True)
    answered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='qna_answers', verbose_name="답변자")
    answered_at = models.DateTimeField("답변일시", null=True, blank=True)

    created_at = models.DateTimeField("질문일시", auto_now_add=True)
    updated_at = models.DateTimeField("수정일", auto_now=True)

    class Meta:
        verbose_name = "Q&A"
        verbose_name_plural = "Q&A"
        ordering = ['-created_at']

    @property
    def is_answered(self):
        return bool(self.answer)

    def __str__(self):
        status = "답변완료" if self.is_answered else "대기중"
        return f"[{status}] {self.title}"