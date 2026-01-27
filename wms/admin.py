from django.contrib import admin
from .models import Warehouse, WmsItemLookup, ErpStockSnapshot, WmsReceipt, WmsReceiptAttachment

@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")

@admin.register(WmsItemLookup)
class WmsItemLookupAdmin(admin.ModelAdmin):
    list_display = ("part_no", "part_name", "updated_at")
    search_fields = ("part_no", "part_name")

@admin.register(ErpStockSnapshot)
class ErpStockSnapshotAdmin(admin.ModelAdmin):
    list_display = ("snapshot_at", "warehouse_code", "part_no", "qty_onhand", "source_file_name", "uploaded_by", "uploaded_at")
    list_filter = ("warehouse_code",)
    search_fields = ("warehouse_code", "part_no")

class AttachmentInline(admin.TabularInline):
    model = WmsReceiptAttachment
    extra = 0

@admin.register(WmsReceipt)
class WmsReceiptAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "warehouse_code", "part_no", "part_name", "receipt_qty", "receipt_date", "created_by", "created_at")
    list_filter = ("status", "warehouse_code")
    search_fields = ("part_no", "part_name", "lot_no")
    inlines = [AttachmentInline]
