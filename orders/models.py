from django.db import models
from django.contrib.auth.models import User

# 1. 협력사(Vendor) 정보를 담는 테이블
class Vendor(models.Model):
    name = models.CharField(max_length=100, verbose_name="업체명")
    code = models.CharField(max_length=20, unique=True, verbose_name='업체코드', default='V000')
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True)

    class Meta:
        verbose_name = "협력사"
        verbose_name_plural = "협력사 관리"

    def __str__(self):
        return f"[{self.code}] {self.name}"


# 2. 품목 마스터(Part) - 족보 테이블
class Part(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, verbose_name='전담 업체')
    part_group = models.CharField(max_length=50, verbose_name='품목군', default='일반')
    part_no = models.CharField(max_length=50, verbose_name='품번')
    part_name = models.CharField(max_length=100, verbose_name='품명')

    class Meta:
        verbose_name = "품목 마스터"
        verbose_name_plural = "품목 마스터 관리"
        unique_together = ('vendor', 'part_no')

    def __str__(self):
        return f"[{self.part_group}] {self.part_no} ({self.vendor.name})"

    # 품목 저장 시 Inventory(기초재고) 행을 자동으로 생성
    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            Inventory.objects.get_or_create(part=self)


# 3. 발주 정보(Order) - 소요량(날짜별 발생)
class Order(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, verbose_name="협력사")
    part_group = models.CharField(max_length=50, verbose_name='품목군', blank=True, null=True)
    part_no = models.CharField(max_length=50, verbose_name="품번")
    part_name = models.CharField(max_length=100, verbose_name="품명")
    quantity = models.IntegerField(verbose_name="수량")
    due_date = models.DateField(verbose_name="납기일")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="발주등록일")
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name="발주승인일")
    # [추가] 발주 마감 여부 필드: 라벨 발행 제한 및 소요량 계산 제외용
    is_closed = models.BooleanField(default=False, verbose_name="발주마감여부")
    
    class Meta:
        verbose_name = "발주서"
        verbose_name_plural = "발주서 관리"

    def __str__(self):
        return f"{self.part_name} ({self.quantity}개)"


# 4. 기초재고 관리 (Inventory) - 현재고 덮어쓰기 전용
class Inventory(models.Model):
    part = models.OneToOneField(Part, on_delete=models.CASCADE, verbose_name="품목")
    base_stock = models.IntegerField(default=0, verbose_name="기초재고(현재고)")
    # [추가] 재고 실사일 필드: 이 날짜 이전의 입고/발주 이력은 무시하고 연산함
    last_inventory_date = models.DateField(null=True, blank=True, verbose_name="재고실사기준일")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="최종갱신일")

    class Meta:
        verbose_name = "기초재고 관리"
        verbose_name_plural = "1. 기초재고 관리"

    def __str__(self):
        return f"{self.part.part_no} 기초재고"


# 5. 일자별 입고 관리 (Incoming) - 날짜별 입고 예정
class Incoming(models.Model):
    part = models.ForeignKey(Part, on_delete=models.CASCADE, verbose_name="품목")
    in_date = models.DateField(verbose_name="입고예정일")
    quantity = models.IntegerField(verbose_name="입고수량")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "일자별 입고 관리"
        verbose_name_plural = "2. 일자별 입고 관리"

    def __str__(self):
        return f"{self.part.part_no} 입고 ({self.in_date})"


# 6. 라벨 발행 이력 (LabelPrintLog) - 발행 수량 차감용
class LabelPrintLog(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, verbose_name="협력사")
    part = models.ForeignKey(Part, on_delete=models.CASCADE, verbose_name="품목")
    part_no = models.CharField(max_length=50, verbose_name="품번") # 조회 속도를 위해 품번 중복 저장
    printed_qty = models.IntegerField(verbose_name="발행수량") # 이번에 발행한 총 수량
    snp = models.IntegerField(verbose_name="포장단위(SNP)") # 협력사가 입력한 박스당 수량
    printed_at = models.DateTimeField(auto_now_add=True, verbose_name="발행일시")

    class Meta:
        verbose_name = "라벨 발행 이력"
        verbose_name_plural = "3. 라벨 발행 이력"

    def __str__(self):
        return f"{self.part_no} - {self.printed_qty}개 ({self.printed_at})"


# =========================================================
# [2단계 추가] 납품서(DeliveryOrder) 및 납품상세(DeliveryOrderItem)
# =========================================================

# 7. 납품서 (DeliveryOrder) - 여러 품목을 묶는 영수증 개념
class DeliveryOrder(models.Model):
    # 납품서 번호 (예: DO-20251218-001)
    order_no = models.CharField(max_length=50, unique=True, verbose_name="납품서 번호")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")
    
    # [✅ 신규 추가] 입고 완료 여부 (기본값: False) - QR 스캔 시 True로 변경됨
    is_received = models.BooleanField(default=False, verbose_name="입고완료여부")

    class Meta:
        verbose_name = "납품서"
        verbose_name_plural = "4. 납품서 관리"

    def __str__(self):
        return self.order_no


# 8. 납품서 상세 품목 (DeliveryOrderItem) - 납품서 안에 들어가는 개별 품목들
class DeliveryOrderItem(models.Model):
    # 어떤 납품서에 속하는지 연결
    order = models.ForeignKey(DeliveryOrder, on_delete=models.CASCADE, related_name='items', verbose_name="납품서 번호")
    
    # 품목 정보 (단순 텍스트로 저장하여 이력 보존)
    part_no = models.CharField(max_length=100, verbose_name="품번")
    part_name = models.CharField(max_length=200, verbose_name="품명")
    
    # 수량 정보
    snp = models.IntegerField(default=0, verbose_name="SNP(박스당수량)")
    box_count = models.IntegerField(default=0, verbose_name="박스 수")
    total_qty = models.IntegerField(default=0, verbose_name="총 수량")

    class Meta:
        verbose_name = "납품서 상세 품목"
        verbose_name_plural = "납품서 상세 품목"

    def __str__(self):
        return f"[{self.order.order_no}] {self.part_no} ({self.total_qty}ea)"