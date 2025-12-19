from django.db import models
from django.contrib.auth.models import User

# 1. 협력사(Vendor)
class Vendor(models.Model):
    name = models.CharField(max_length=100, verbose_name="업체명")
    code = models.CharField(max_length=20, unique=True, verbose_name='업체코드', default='V000')
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True)
    can_view_inventory = models.BooleanField(default=True, verbose_name="과부족 조회 권한")

    class Meta:
        verbose_name = "협력사"
        verbose_name_plural = "협력사 관리"

    def __str__(self):
        return f"[{self.code}] {self.name}"

# 2. 품목 마스터(Part)
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

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            Inventory.objects.get_or_create(part=self)

# [✅ 신규 추가] 2-1. 소요량 관리 (Demand)
# 발주와 별개로 "실제 필요한 수량"을 기록하는 테이블입니다.
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
# 이제 과부족 계산이 아닌, 업체와의 확정된 거래 내역으로 사용됩니다.
class Order(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, verbose_name="협력사")
    part_group = models.CharField(max_length=50, verbose_name='품목군', blank=True, null=True)
    part_no = models.CharField(max_length=50, verbose_name="품번")
    part_name = models.CharField(max_length=100, verbose_name="품명")
    quantity = models.IntegerField(verbose_name="수량")
    due_date = models.DateField(verbose_name="납기일")
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
    printed_at = models.DateTimeField(auto_now_add=True, verbose_name="발행일시")

    class Meta:
        verbose_name = "라벨 발행 이력"
        verbose_name_plural = "3. 라벨 발행 이력"

# 7. 납품서 (DeliveryOrder)
class DeliveryOrder(models.Model):
    order_no = models.CharField(max_length=50, unique=True, verbose_name="납품서 번호")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")
    is_received = models.BooleanField(default=False, verbose_name="입고완료여부")

    class Meta:
        verbose_name = "납품서"
        verbose_name_plural = "4. 납품서 관리"

# 8. 납품서 상세 품목 (DeliveryOrderItem)
class DeliveryOrderItem(models.Model):
    order = models.ForeignKey(DeliveryOrder, on_delete=models.CASCADE, related_name='items', verbose_name="납품서 번호")
    part_no = models.CharField(max_length=100, verbose_name="품번")
    part_name = models.CharField(max_length=200, verbose_name="품명")
    snp = models.IntegerField(default=0, verbose_name="SNP(박스당수량)")
    box_count = models.IntegerField(default=0, verbose_name="박스 수")
    total_qty = models.IntegerField(default=0, verbose_name="총 수량")

    class Meta:
        verbose_name = "납품서 상세 품목"
        verbose_name_plural = "납품서 상세 품목"