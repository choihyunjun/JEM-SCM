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