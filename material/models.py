from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

# SCM(Orders) 앱의 마스터 데이터 참조
from orders.models import Part, Vendor


# -----------------------------------------------------------------------------
# 1. 창고 기준정보 (Warehouse Master)
# -----------------------------------------------------------------------------
class Warehouse(models.Model):
    """
    창고/장소 기준정보
    예: 자재창고, 생산라인, 불량격리장, 예비창고 등
    """
    name = models.CharField("창고명", max_length=50) 
    code = models.CharField("창고코드", max_length=20, unique=True) # 예: WH_MAT, WH_LINE
    description = models.CharField("설명", max_length=200, blank=True, null=True)
    is_active = models.BooleanField("사용여부", default=True)

    def __str__(self):
        return f"[{self.code}] {self.name}"

    class Meta:
        verbose_name = "창고(장소) 관리"
        verbose_name_plural = "1. 창고(장소) 관리"


# -----------------------------------------------------------------------------
# 2. 창고별 재고 현황 (Current Stock)
# -----------------------------------------------------------------------------
class MaterialStock(models.Model):
    """
    [WMS 핵심] 특정 창고에 특정 품목이 몇 개 있는지 저장 (Snapshot)
    - 기존 orders.Inventory는 '전체 총량' 개념이라면, 이것은 '위치별 수량'입니다.
    - LOT별 재고 관리 지원
    """
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, verbose_name="창고")
    part = models.ForeignKey(Part, on_delete=models.CASCADE, verbose_name="품목")
    lot_no = models.DateField("LOT 번호(생산일)", null=True, blank=True)
    quantity = models.IntegerField("현재고", default=0)

    # 로케이션(Rack/Bin) 관리까지 필요하다면 추후 여기에 location 필드 추가

    class Meta:
        verbose_name = "창고별 재고 현황"
        verbose_name_plural = "2. 창고별 재고 현황"
        unique_together = ('warehouse', 'part', 'lot_no') # 한 창고에 같은 품목/LOT가 중복되지 않도록

    def __str__(self):
        # Part 모델의 part_no 필드 사용 (orders.models.Part)
        return f"{self.warehouse.name} | {self.part.part_no} : {self.quantity}개"


# -----------------------------------------------------------------------------
# 3. 수불 대장 (Transaction History)
# -----------------------------------------------------------------------------
class MaterialTransaction(models.Model):
    """
    [WMS 핵심] 모든 입고, 출고, 이동, 조정의 역사를 기록 (History)
    - 누가, 언제, 무엇을, 어디서, 어디로, 얼마나, 왜 움직였는가?
    """
    # 수불 유형 정의
    TYPE_CHOICES = [
        ('IN_SCM', 'SCM 납품입고'),     # 납품서 QR 스캔 입고
        ('IN_MANUAL', '수기 입고'),     # 담당자 수동 입력
        ('OUT_PROD', '생산 불출'),      # 생산 불출 (재고 감소)
        ('TRANSFER', '창고 이동'),      # A창고 -> B창고
        ('ADJUST', '재고 조정'),        # 실사 후 수량 강제 조정
        ('OUT_RETURN', '반품 출고'),    # [신규 추가] 불량 반품 등
    ]

    transaction_no = models.CharField("수불번호", max_length=30, unique=True) # 예: TRX-20250112-0001
    
    # [수정] 내역 조회 시 필터링 속도를 위해 db_index=True 추가
    transaction_type = models.CharField("구분", max_length=20, choices=TYPE_CHOICES, db_index=True)
    
    # [수정] 날짜별 조회 속도를 위해 db_index=True 추가
    date = models.DateTimeField("처리일시", default=timezone.now, db_index=True)
    
    # 품목 및 수량
    part = models.ForeignKey(Part, on_delete=models.CASCADE, verbose_name="품목")
    lot_no = models.DateField("LOT 번호(생산일)", null=True, blank=True)
    quantity = models.IntegerField("변동수량") # 입고(+), 출고(-)

    # 위치 정보 (From -> To)
    # 입고 시: To만 있음 / 출고 시: From만 있음 / 이동 시: 둘 다 있음
    warehouse_from = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True, related_name='tx_from', verbose_name="보낸 창고")
    warehouse_to = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True, related_name='tx_to', verbose_name="받은 창고")
    
    # 변동 후 해당 창고의 재고량 (이력 추적용)
    result_stock = models.IntegerField("변동후 잔량", default=0)

    # 관련 정보
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="관련 거래처")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="처리자")
    remark = models.CharField("비고", max_length=200, blank=True, null=True)
    
    # SCM 연동용 (어떤 납품서로 들어왔는지)
    ref_delivery_order = models.CharField("참조 납품서번호", max_length=50, blank=True, null=True)

    class Meta:
        verbose_name = "수불(입출고) 이력"
        verbose_name_plural = "3. 수불(입출고) 이력"
        ordering = ['-date', '-id'] # 최신순 정렬

    def __str__(self):
        return f"[{self.get_transaction_type_display()}] {self.part.part_no} ({self.quantity})"


# -----------------------------------------------------------------------------
# 4. BOM 관리 (Bill of Materials)
# -----------------------------------------------------------------------------
class Product(models.Model):
    """
    [BOM] 모품 (완제품/반제품) 마스터
    - 제품 또는 반제품(사출품 등)의 정보를 저장
    """
    ACCOUNT_TYPE_CHOICES = [
        ('제품', '제품'),
        ('반제품', '반제품'),
    ]

    part_no = models.CharField("모품번", max_length=50, unique=True, db_index=True)
    part_name = models.CharField("모품명", max_length=200)
    spec = models.CharField("규격", max_length=100, blank=True, null=True)
    unit = models.CharField("재고단위", max_length=20, default='EA')
    account_type = models.CharField("계정구분", max_length=20, choices=ACCOUNT_TYPE_CHOICES, default='제품')
    procurement_type = models.CharField("조달구분", max_length=20, default='생산')
    is_bom_registered = models.BooleanField("BOM등록여부", default=True)
    is_active = models.BooleanField("사용여부", default=True)

    created_at = models.DateTimeField("등록일시", auto_now_add=True)
    updated_at = models.DateTimeField("수정일시", auto_now=True)

    class Meta:
        verbose_name = "모품(제품) 마스터"
        verbose_name_plural = "4. 모품(제품) 마스터"
        ordering = ['part_no']

    def __str__(self):
        return f"[{self.account_type}] {self.part_no} - {self.part_name}"

    def get_bom_items(self):
        """해당 제품의 BOM 구성품목 조회"""
        return self.bom_items.filter(is_active=True).order_by('seq')

    def calculate_requirement(self, qty):
        """생산수량에 따른 자재 소요량 계산"""
        result = []
        for item in self.get_bom_items():
            required_qty = item.required_qty * qty
            result.append({
                'child_part_no': item.child_part_no,
                'child_part_name': item.child_part_name,
                'child_spec': item.child_spec,
                'child_unit': item.child_unit,
                'unit_qty': item.required_qty,
                'required_qty': required_qty,
                'vendor_name': item.vendor_name,
            })
        return result


class BOMItem(models.Model):
    """
    [BOM] 자품 (자재/부품) - BOM 구성품목
    - 특정 모품(Product)을 만들기 위해 필요한 자재 정보
    """
    SUPPLY_TYPE_CHOICES = [
        ('자재', '자재'),
        ('사급', '사급'),
    ]

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='bom_items', verbose_name="모품")
    seq = models.IntegerField("순번", default=1)

    # 자품 정보
    child_part_no = models.CharField("자품번", max_length=50, db_index=True)
    child_part_name = models.CharField("자품명", max_length=200)
    child_spec = models.CharField("규격", max_length=100, blank=True, null=True)
    child_unit = models.CharField("재고단위", max_length=20, default='EA')

    # 소요량 정보
    net_qty = models.DecimalField("정미수량", max_digits=15, decimal_places=6, default=0)
    loss_rate = models.DecimalField("LOSS(%)", max_digits=5, decimal_places=2, null=True, blank=True)
    required_qty = models.DecimalField("필요수량", max_digits=15, decimal_places=6, default=0)

    # 조달 정보
    supply_type = models.CharField("사급구분", max_length=20, choices=SUPPLY_TYPE_CHOICES, default='자재')
    outsource_type = models.CharField("외주구분", max_length=20, default='무상')
    vendor_name = models.CharField("주거래처", max_length=100, blank=True, null=True)

    # 유효기간
    start_date = models.DateField("시작일자", null=True, blank=True)
    end_date = models.DateField("종료일자", null=True, blank=True)

    # 기타
    drawing_no = models.CharField("도면번호", max_length=50, blank=True, null=True)
    material = models.CharField("재질", max_length=100, blank=True, null=True)
    remark = models.CharField("비고", max_length=200, blank=True, null=True)

    is_active = models.BooleanField("사용여부", default=True)
    is_bom_active = models.BooleanField("BOM사용여부", default=True)

    created_at = models.DateTimeField("등록일시", auto_now_add=True)
    updated_at = models.DateTimeField("수정일시", auto_now=True)

    class Meta:
        verbose_name = "BOM 구성품목"
        verbose_name_plural = "5. BOM 구성품목"
        ordering = ['product', 'seq']
        unique_together = ('product', 'seq', 'child_part_no')  # 동일 모품에 동일 순번/자품번 중복 방지

    def __str__(self):
        return f"{self.product.part_no} → {self.child_part_no} ({self.required_qty} {self.child_unit})"


# -----------------------------------------------------------------------------
# 6. 재고 마감 관리 (Inventory Closing)
# -----------------------------------------------------------------------------
class InventoryClosing(models.Model):
    """
    [WMS] 월별 재고 마감 관리
    - 마감 월(closing_month)을 기준으로 해당 월 이전의 수불 변경을 제한
    - 재고 실사 후 마감 처리하면 해당 월은 수정 불가 (경고 후 관리자만 허용)
    """
    closing_month = models.DateField("마감월", unique=True)  # 매월 1일로 저장 (예: 2025-01-01)
    closed_at = models.DateTimeField("마감 처리일시", auto_now_add=True)
    closed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="마감 처리자")
    remark = models.CharField("비고", max_length=200, blank=True, null=True)
    is_active = models.BooleanField("활성화", default=True)  # False면 마감 해제

    class Meta:
        verbose_name = "재고 마감"
        verbose_name_plural = "6. 재고 마감"
        ordering = ['-closing_month']

    def __str__(self):
        return f"{self.closing_month.strftime('%Y년 %m월')} 마감"

    @classmethod
    def get_latest_closing(cls):
        """가장 최근 마감월 조회"""
        return cls.objects.filter(is_active=True).order_by('-closing_month').first()

    @classmethod
    def is_date_closed(cls, target_date):
        """
        특정 날짜가 마감된 기간에 속하는지 확인
        - target_date가 마감월 말일 이전이면 True (마감됨)
        """
        from datetime import date
        from calendar import monthrange

        latest = cls.get_latest_closing()
        if not latest:
            return False

        # 마감월의 마지막 날 계산
        closing_year = latest.closing_month.year
        closing_month = latest.closing_month.month
        _, last_day = monthrange(closing_year, closing_month)
        closing_end_date = date(closing_year, closing_month, last_day)

        # target_date를 date 객체로 변환
        if hasattr(target_date, 'date'):
            target_date = target_date.date()

        return target_date <= closing_end_date


# -----------------------------------------------------------------------------
# 7. 재고 실사 기록 (Inventory Check)
# -----------------------------------------------------------------------------
class InventoryCheck(models.Model):
    """
    [WMS] 재고 실사 기록
    - 실사 시점의 시스템 재고와 실제 재고를 비교하여 차이를 기록
    """
    CHECK_STATUS_CHOICES = [
        ('PENDING', '대기'),
        ('MATCHED', '일치'),
        ('ADJUSTED', '조정완료'),
    ]

    closing = models.ForeignKey(InventoryClosing, on_delete=models.CASCADE, null=True, blank=True,
                                 related_name='checks', verbose_name="마감")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, verbose_name="창고")
    part = models.ForeignKey(Part, on_delete=models.CASCADE, verbose_name="품목")
    lot_no = models.DateField("LOT 번호", null=True, blank=True)

    system_qty = models.IntegerField("시스템 재고", default=0)
    actual_qty = models.IntegerField("실사 재고", default=0)
    diff_qty = models.IntegerField("차이수량", default=0)  # actual - system

    status = models.CharField("상태", max_length=20, choices=CHECK_STATUS_CHOICES, default='PENDING')
    checked_at = models.DateTimeField("실사일시", auto_now_add=True)
    checked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="실사자")
    adjusted_at = models.DateTimeField("조정일시", null=True, blank=True)
    remark = models.CharField("비고", max_length=200, blank=True, null=True)

    class Meta:
        verbose_name = "재고 실사 기록"
        verbose_name_plural = "7. 재고 실사 기록"
        ordering = ['-checked_at']

    def __str__(self):
        return f"[{self.warehouse.code}] {self.part.part_no} 실사: {self.system_qty} → {self.actual_qty}"

    def save(self, *args, **kwargs):
        self.diff_qty = self.actual_qty - self.system_qty
        super().save(*args, **kwargs)


# -----------------------------------------------------------------------------
# 8. 마감 시점 재고 스냅샷 (Inventory Snapshot)
# -----------------------------------------------------------------------------
class InventorySnapshot(models.Model):
    """
    [WMS] 월 마감 시점의 재고 스냅샷
    - 마감 처리 시 현재 재고 상태를 그대로 복사하여 저장
    - 나중에 "N월말 재고가 얼마였지?" 조회 가능
    """
    closing = models.ForeignKey(InventoryClosing, on_delete=models.CASCADE,
                                 related_name='snapshots', verbose_name="마감")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, verbose_name="창고")
    part = models.ForeignKey(Part, on_delete=models.CASCADE, verbose_name="품목")
    lot_no = models.DateField("LOT 번호", null=True, blank=True)
    quantity = models.IntegerField("마감 시점 재고", default=0)

    created_at = models.DateTimeField("생성일시", auto_now_add=True)

    class Meta:
        verbose_name = "마감 재고 스냅샷"
        verbose_name_plural = "8. 마감 재고 스냅샷"
        ordering = ['-closing__closing_month', 'warehouse__code', 'part__part_no']
        # 동일 마감+창고+품목+LOT 중복 방지
        unique_together = ('closing', 'warehouse', 'part', 'lot_no')

    def __str__(self):
        return f"[{self.closing.closing_month.strftime('%Y-%m')}] {self.warehouse.code}/{self.part.part_no}: {self.quantity}"


# -----------------------------------------------------------------------------
# 9. 공정 현품표 (Process Tag) - 중복 스캔 방지
# -----------------------------------------------------------------------------
class ProcessTag(models.Model):
    """
    [WMS] 공정 현품표 발행 및 사용 이력
    - 현품표 발행 시 고유 ID 생성
    - 스캔 시 이미 사용된 태그인지 확인 (경고만, 차단 아님)
    """
    STATUS_CHOICES = [
        ('PRINTED', '발행'),
        ('USED', '사용완료'),
        ('CANCELLED', '취소'),
    ]

    # 고유 식별자 (QR에 포함)
    tag_id = models.CharField("태그ID", max_length=30, unique=True, db_index=True)  # TAG-20260124-0001

    # 품목 정보
    part = models.ForeignKey(Part, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="품목")
    part_no = models.CharField("품번", max_length=50)
    part_name = models.CharField("품명", max_length=200)
    quantity = models.IntegerField("수량")
    lot_no = models.DateField("LOT 번호", null=True, blank=True)

    # 상태 관리
    status = models.CharField("상태", max_length=15, choices=STATUS_CHOICES, default='PRINTED')

    # 발행 정보
    printed_at = models.DateTimeField("발행일시", auto_now_add=True)
    printed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                    related_name='tags_printed', verbose_name="발행자")

    # 사용(스캔) 정보
    used_at = models.DateTimeField("최초 사용일시", null=True, blank=True)
    used_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='tags_used', verbose_name="사용자")
    used_warehouse = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True,
                                        verbose_name="사용 창고")
    scan_count = models.IntegerField("스캔 횟수", default=0)

    class Meta:
        verbose_name = "공정 현품표"
        verbose_name_plural = "9. 공정 현품표"
        ordering = ['-printed_at']

    def __str__(self):
        return f"[{self.tag_id}] {self.part_no} x {self.quantity}"

    @classmethod
    def generate_tag_id(cls):
        """고유 태그 ID 생성 (TAG-YYYYMMDD-XXXX)"""
        from django.utils import timezone
        today = timezone.now().strftime('%Y%m%d')
        prefix = f"TAG-{today}-"

        # 오늘 발행된 마지막 태그 번호 조회
        last_tag = cls.objects.filter(tag_id__startswith=prefix).order_by('-tag_id').first()
        if last_tag:
            try:
                last_seq = int(last_tag.tag_id.split('-')[-1])
                new_seq = last_seq + 1
            except (ValueError, IndexError):
                new_seq = 1
        else:
            new_seq = 1

        return f"{prefix}{new_seq:04d}"

    def record_scan(self, user=None, warehouse=None):
        """
        스캔 기록
        Returns: (success: bool, is_first_scan: bool, error_message: str or None)
        - 이미 사용된 태그는 차단됨
        """
        if self.status == 'PRINTED':
            # 최초 스캔 - 성공
            self.scan_count += 1
            self.status = 'USED'
            self.used_at = timezone.now()
            self.used_by = user
            self.used_warehouse = warehouse
            self.save()
            return True, True, None
        else:
            # 이미 사용된 태그 - 차단
            self.scan_count += 1
            self.save()
            error = f"[중복 스캔 차단] 이 현품표는 이미 {self.used_at.strftime('%Y-%m-%d %H:%M')}에 "
            if self.used_by:
                error += f"'{self.used_by.username}'님이 "
            if self.used_warehouse:
                error += f"'{self.used_warehouse.name}'에서 "
            error += f"사용되었습니다. (시도 횟수: {self.scan_count}회)"
            return False, False, error


class ProcessTagScanLog(models.Model):
    """
    [WMS] 현품표 스캔 이력 (모든 스캔 기록)
    - 중복 스캔 포함 모든 스캔 이력 보관
    """
    tag = models.ForeignKey(ProcessTag, on_delete=models.CASCADE, related_name='scan_logs', verbose_name="현품표")
    scanned_at = models.DateTimeField("스캔일시", auto_now_add=True)
    scanned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="스캔자")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="창고")
    is_first_scan = models.BooleanField("최초스캔여부", default=False)
    ip_address = models.GenericIPAddressField("IP주소", null=True, blank=True)
    remark = models.CharField("비고", max_length=200, blank=True)

    class Meta:
        verbose_name = "현품표 스캔 이력"
        verbose_name_plural = "10. 현품표 스캔 이력"
        ordering = ['-scanned_at']

    def __str__(self):
        return f"[{self.tag.tag_id}] {self.scanned_at.strftime('%Y-%m-%d %H:%M')}"


# -----------------------------------------------------------------------------
# 11. 재고조사 세션 (Inventory Check Session / Cycle Count)
# -----------------------------------------------------------------------------
class InventoryCheckSession(models.Model):
    """
    [WMS] 재고조사 세션
    - 창고별 실사를 수행하고 시스템 재고와 비교
    """
    STATUS_CHOICES = [
        ('DRAFT', '준비중'),
        ('IN_PROGRESS', '진행중'),
        ('COMPLETED', '완료'),
        ('CANCELLED', '취소'),
    ]

    # 조사 정보
    check_no = models.CharField("조사번호", max_length=30, unique=True)  # INV-20260125-001
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, verbose_name="대상 창고",
                                   related_name='check_sessions')
    check_date = models.DateField("조사일")
    status = models.CharField("상태", max_length=15, choices=STATUS_CHOICES, default='DRAFT')

    # 조사 결과 요약
    total_scanned = models.IntegerField("총 스캔건수", default=0)
    total_matched = models.IntegerField("일치건수", default=0)
    total_discrepancy = models.IntegerField("불일치건수", default=0)

    # 담당자
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                    related_name='check_sessions_created', verbose_name="생성자")
    completed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='check_sessions_completed', verbose_name="완료자")
    completed_at = models.DateTimeField("완료일시", null=True, blank=True)

    remark = models.TextField("비고", blank=True)
    created_at = models.DateTimeField("생성일시", auto_now_add=True)
    updated_at = models.DateTimeField("수정일시", auto_now=True)

    class Meta:
        verbose_name = "재고조사 세션"
        verbose_name_plural = "11. 재고조사 세션"
        ordering = ['-check_date', '-created_at']

    def __str__(self):
        return f"[{self.check_no}] {self.warehouse.name} - {self.check_date}"

    @classmethod
    def generate_check_no(cls):
        """조사번호 생성 (INV-YYYYMMDD-XXX)"""
        today = timezone.now().strftime('%Y%m%d')
        prefix = f"INV-{today}-"
        last = cls.objects.filter(check_no__startswith=prefix).order_by('-check_no').first()
        if last:
            try:
                seq = int(last.check_no.split('-')[-1]) + 1
            except (ValueError, IndexError):
                seq = 1
        else:
            seq = 1
        return f"{prefix}{seq:03d}"

    def update_summary(self):
        """조사 결과 요약 업데이트"""
        items = self.check_items.all()
        self.total_scanned = items.count()
        self.total_matched = items.filter(is_matched=True).count()
        self.total_discrepancy = items.filter(is_matched=False).count()
        self.save(update_fields=['total_scanned', 'total_matched', 'total_discrepancy'])


class InventoryCheckSessionItem(models.Model):
    """
    [WMS] 재고조사 항목 (스캔 내역)
    - 현품표 스캔 시 기록
    """
    check_session = models.ForeignKey(InventoryCheckSession, on_delete=models.CASCADE, related_name='check_items', verbose_name="조사")

    # 스캔 정보
    process_tag = models.ForeignKey(ProcessTag, on_delete=models.SET_NULL, null=True, blank=True,
                                     verbose_name="현품표")
    tag_id = models.CharField("태그ID", max_length=30)

    # 품목 정보 (현품표에서 가져옴)
    part = models.ForeignKey(Part, on_delete=models.SET_NULL, null=True, verbose_name="품목")
    part_no = models.CharField("품번", max_length=50)
    part_name = models.CharField("품명", max_length=200)
    lot_no = models.DateField("LOT번호", null=True, blank=True)

    # 수량 비교
    scanned_qty = models.IntegerField("현품표 수량")  # 현품표에 적힌 수량
    system_qty = models.IntegerField("시스템 수량", default=0)  # MaterialStock 수량
    discrepancy = models.IntegerField("차이", default=0)  # scanned - system
    is_matched = models.BooleanField("일치여부", default=True)

    # 스캔 정보
    scanned_at = models.DateTimeField("스캔일시", auto_now_add=True)
    scanned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="스캔자")

    remark = models.CharField("비고", max_length=200, blank=True)

    class Meta:
        verbose_name = "재고조사 세션 항목"
        verbose_name_plural = "12. 재고조사 세션 항목"
        ordering = ['-scanned_at']

    def __str__(self):
        status = "✅" if self.is_matched else "⚠️"
        return f"{status} [{self.tag_id}] {self.part_no} x {self.scanned_qty}"


# -----------------------------------------------------------------------------
# 13. 원재료 랙 위치 (Raw Material Rack)
# -----------------------------------------------------------------------------
class RawMaterialRack(models.Model):
    """
    [WMS] 원재료 창고 랙 위치 정의
    - 격자형 레이아웃의 각 칸 정보
    - 예: A-1-1, B-2-5 등
    """
    SECTION_CHOICES = [
        ('3F', '3공장'),
        ('2F', '2공장'),
    ]

    section = models.CharField("구역", max_length=10, choices=SECTION_CHOICES, default='3F')
    position_code = models.CharField("위치코드", max_length=20, db_index=True)  # A-1-1, B-2-5 (구역별 unique)
    row_label = models.CharField("행 라벨", max_length=10)  # A, B
    row_num = models.IntegerField("행 번호", default=1)  # 1, 2, 3
    col_num = models.IntegerField("열 번호", default=1)  # 1, 2, 3...

    # 배치된 품목 (null이면 빈 칸)
    part = models.ForeignKey(Part, on_delete=models.SET_NULL, null=True, blank=True,
                              verbose_name="배치 품목", related_name='rack_positions')

    display_order = models.IntegerField("표시순서", default=0)
    is_active = models.BooleanField("사용여부", default=True)

    created_at = models.DateTimeField("생성일시", auto_now_add=True)
    updated_at = models.DateTimeField("수정일시", auto_now=True)

    class Meta:
        verbose_name = "원재료 랙 위치"
        verbose_name_plural = "13. 원재료 랙 위치"
        ordering = ['section', 'row_label', '-row_num', '-col_num']
        unique_together = [['section', 'position_code']]  # 구역별로 위치코드 unique

    def __str__(self):
        part_info = f" - {self.part.part_no}" if self.part else " (빈칸)"
        return f"[{self.get_section_display()}] {self.position_code}{part_info}"


# -----------------------------------------------------------------------------
# 14. 원재료 품목 설정 (Raw Material Setting)
# -----------------------------------------------------------------------------
class RawMaterialSetting(models.Model):
    """
    [WMS] 원재료 품목별 설정
    - 안전재고, 보관기간 등 품목별 관리 설정
    """
    part = models.OneToOneField(Part, on_delete=models.CASCADE, verbose_name="품목",
                                 related_name='raw_material_setting')

    safety_stock = models.IntegerField("안전재고", default=0, help_text="이 수량 이하면 경고")
    warning_stock = models.IntegerField("경고재고", default=0, help_text="이 수량 이하면 주의 (안전재고보다 높게)")

    shelf_life_days = models.IntegerField("보관기간(일)", default=365, help_text="입고일로부터 유효기간")
    unit_weight = models.DecimalField("단위중량(kg)", max_digits=10, decimal_places=2, default=25,
                                       help_text="포대당 중량 (기본 25kg)")

    remark = models.TextField("비고", blank=True)

    created_at = models.DateTimeField("생성일시", auto_now_add=True)
    updated_at = models.DateTimeField("수정일시", auto_now=True)

    class Meta:
        verbose_name = "원재료 품목 설정"
        verbose_name_plural = "14. 원재료 품목 설정"

    def __str__(self):
        return f"{self.part.part_no} - 안전재고: {self.safety_stock}, 보관: {self.shelf_life_days}일"


# -----------------------------------------------------------------------------
# 15. 원재료 QR 라벨 (Raw Material Label)
# -----------------------------------------------------------------------------
class RawMaterialLabel(models.Model):
    """
    [WMS] 원재료 QR 라벨 발행 이력
    - 입고 시 포대별 QR 라벨 발행
    - 각 라벨은 고유 ID를 가짐
    """
    STATUS_CHOICES = [
        ('PRINTED', '발행'),
        ('INSTOCK', '재고'),
        ('USED', '사용완료'),
        ('EXPIRED', '유효기간만료'),
        ('DISPOSED', '폐기'),
    ]

    # 고유 라벨 ID
    label_id = models.CharField("라벨ID", max_length=30, unique=True, db_index=True)  # RM-20260207-0001

    # 품목 정보
    part = models.ForeignKey(Part, on_delete=models.CASCADE, verbose_name="품목")
    part_no = models.CharField("품번", max_length=50)
    part_name = models.CharField("품명", max_length=200)

    # LOT 및 수량
    UNIT_CHOICES = [
        ('KG', 'kg'),
        ('EA', 'EA'),
        ('L', 'L'),
        ('M', 'm'),
    ]
    lot_no = models.DateField("LOT번호(입고일)")
    quantity = models.DecimalField("수량", max_digits=10, decimal_places=2, default=25)
    unit = models.CharField("단위", max_length=5, choices=UNIT_CHOICES, default='KG')

    # 유효기간
    expiry_date = models.DateField("유효기간", null=True, blank=True)

    # 입고 정보
    incoming_transaction = models.ForeignKey(MaterialTransaction, on_delete=models.SET_NULL,
                                              null=True, blank=True, verbose_name="입고 트랜잭션")
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="거래처")

    # 상태 관리
    status = models.CharField("상태", max_length=15, choices=STATUS_CHOICES, default='PRINTED')

    # 현재 위치
    current_rack = models.ForeignKey(RawMaterialRack, on_delete=models.SET_NULL, null=True, blank=True,
                                      verbose_name="현재 위치")

    # 발행 정보
    printed_at = models.DateTimeField("발행일시", auto_now_add=True)
    printed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                    related_name='rawmaterial_labels_printed', verbose_name="발행자")

    # 사용(출고) 정보
    used_at = models.DateTimeField("사용일시", null=True, blank=True)
    used_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='rawmaterial_labels_used', verbose_name="사용자")

    class Meta:
        verbose_name = "원재료 QR 라벨"
        verbose_name_plural = "15. 원재료 QR 라벨"
        ordering = ['-printed_at']

    def __str__(self):
        return f"[{self.label_id}] {self.part_no} / {self.lot_no} / {self.quantity}{self.get_unit_display()}"

    @classmethod
    def generate_label_id(cls):
        """고유 라벨 ID 생성 (RM-YYYYMMDD-XXXX)"""
        today = timezone.now().strftime('%Y%m%d')
        prefix = f"RM-{today}-"

        last_label = cls.objects.filter(label_id__startswith=prefix).order_by('-label_id').first()
        if last_label:
            try:
                last_seq = int(last_label.label_id.split('-')[-1])
                new_seq = last_seq + 1
            except (ValueError, IndexError):
                new_seq = 1
        else:
            new_seq = 1

        return f"{prefix}{new_seq:04d}"

    def is_expired(self):
        """유효기간 만료 여부 확인"""
        if self.expiry_date:
            return timezone.now().date() > self.expiry_date
        return False