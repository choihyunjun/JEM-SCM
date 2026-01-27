from django.conf import settings
from django.db import models

class Warehouse(models.Model):
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "창고"
        verbose_name_plural = "창고"

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"

class WmsItemLookup(models.Model):
    part_no = models.CharField("품번", max_length=100, unique=True, db_index=True)
    part_name = models.CharField("품명", max_length=255)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "WMS 품목(조회용)"
        verbose_name_plural = "WMS 품목(조회용)"

    def __str__(self) -> str:
        return f"{self.part_no} - {self.part_name}"

class ErpStockSnapshot(models.Model):
    batch_id = models.CharField(max_length=36, db_index=True)
    snapshot_at = models.DateTimeField(db_index=True)
    warehouse_code = models.CharField(max_length=50, db_index=True)
    part_no = models.CharField(max_length=100, db_index=True)
    qty_onhand = models.DecimalField(max_digits=18, decimal_places=3)

    source_file_name = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="wms_stock_uploads"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "ERP 재고 스냅샷"
        verbose_name_plural = "ERP 재고 스냅샷"
        indexes = [
            models.Index(fields=["snapshot_at", "warehouse_code", "part_no"]),
        ]

    def __str__(self) -> str:
        return f"{self.snapshot_at:%Y-%m-%d %H:%M} {self.warehouse_code} {self.part_no}"

class WmsReceipt(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_REQUESTED = "requested"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "임시저장"),
        (STATUS_REQUESTED, "검사의뢰"),
        (STATUS_APPROVED, "입고완료"),
        (STATUS_REJECTED, "반려"),
    ]

    warehouse_code = models.CharField("입고창고", max_length=50, db_index=True)
    part_no = models.CharField("품번", max_length=100, db_index=True)
    part_name = models.CharField("품명", max_length=255)  # 스냅샷/증빙 목적(조회용)
    receipt_qty = models.DecimalField("입고수량", max_digits=18, decimal_places=3)
    receipt_date = models.DateField("입고일")
    mfg_date = models.DateField("제조일", null=True, blank=True)
    lot_no = models.CharField("LOT No", max_length=100, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="wms_receipts_requested"
    )
    requested_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="wms_receipts_approved"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="wms_receipts_rejected"
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="wms_receipts_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "입고 등록"
        verbose_name_plural = "입고 등록"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.warehouse_code} {self.part_no} {self.receipt_qty}"

class WmsReceiptAttachment(models.Model):
    receipt = models.ForeignKey(WmsReceipt, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="wms/receipts/%Y/%m/")
    original_name = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="wms_receipt_files"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "입고 성적서(첨부)"
        verbose_name_plural = "입고 성적서(첨부)"

    def __str__(self) -> str:
        return self.original_name or str(self.file)
