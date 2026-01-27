from django.contrib import admin
from .models import (
    Warehouse, MaterialStock, MaterialTransaction, Product, BOMItem,
    InventoryClosing, InventoryCheck, InventorySnapshot,
    ProcessTag, ProcessTagScanLog, InventoryCheckSession, InventoryCheckSessionItem
)

@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'is_active')
    search_fields = ('name', 'code')

@admin.register(MaterialStock)
class MaterialStockAdmin(admin.ModelAdmin):
    list_display = ('warehouse', 'part', 'quantity')
    list_filter = ('warehouse',)
    search_fields = ('part__part_no', 'part__part_name')

@admin.register(MaterialTransaction)
class MaterialTransactionAdmin(admin.ModelAdmin):
    list_display = ('date', 'transaction_type', 'part', 'quantity', 'warehouse_from', 'warehouse_to', 'actor')
    list_filter = ('transaction_type', 'date', 'warehouse_to')
    search_fields = ('transaction_no', 'part__part_no')


# =============================================================================
# BOM 관리 (Bill of Materials)
# =============================================================================

class BOMItemInline(admin.TabularInline):
    """BOM 구성품목 인라인 (Product 상세에서 자품 목록 확인)"""
    model = BOMItem
    extra = 0
    fields = ('seq', 'child_part_no', 'child_part_name', 'required_qty', 'child_unit', 'supply_type', 'vendor_name', 'is_active')
    readonly_fields = ('seq', 'child_part_no', 'child_part_name', 'required_qty', 'child_unit', 'supply_type', 'vendor_name')
    can_delete = False
    show_change_link = True


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('part_no', 'part_name', 'account_type', 'is_bom_registered', 'is_active', 'created_at')
    list_filter = ('account_type', 'is_bom_registered', 'is_active')
    search_fields = ('part_no', 'part_name')
    ordering = ('part_no',)
    inlines = [BOMItemInline]

    fieldsets = (
        ('기본 정보', {
            'fields': ('part_no', 'part_name', 'spec', 'unit')
        }),
        ('분류', {
            'fields': ('account_type', 'procurement_type', 'is_bom_registered')
        }),
        ('상태', {
            'fields': ('is_active',)
        }),
    )


@admin.register(BOMItem)
class BOMItemAdmin(admin.ModelAdmin):
    list_display = ('product', 'seq', 'child_part_no', 'child_part_name', 'required_qty', 'child_unit', 'supply_type', 'vendor_name', 'is_active')
    list_filter = ('product__account_type', 'supply_type', 'is_active', 'is_bom_active')
    search_fields = ('product__part_no', 'product__part_name', 'child_part_no', 'child_part_name')
    ordering = ('product', 'seq')
    raw_id_fields = ('product',)

    fieldsets = (
        ('모품 정보', {
            'fields': ('product', 'seq')
        }),
        ('자품 정보', {
            'fields': ('child_part_no', 'child_part_name', 'child_spec', 'child_unit')
        }),
        ('소요량', {
            'fields': ('net_qty', 'loss_rate', 'required_qty')
        }),
        ('조달 정보', {
            'fields': ('supply_type', 'outsource_type', 'vendor_name')
        }),
        ('유효기간', {
            'fields': ('start_date', 'end_date'),
            'classes': ('collapse',)
        }),
        ('기타', {
            'fields': ('drawing_no', 'material', 'remark', 'is_active', 'is_bom_active'),
            'classes': ('collapse',)
        }),
    )


# =============================================================================
# 재고 마감 관리
# =============================================================================

@admin.register(InventoryClosing)
class InventoryClosingAdmin(admin.ModelAdmin):
    list_display = ('closing_month', 'closed_at', 'closed_by', 'is_active', 'remark')
    list_filter = ('is_active',)
    ordering = ('-closing_month',)


@admin.register(InventoryCheck)
class InventoryCheckAdmin(admin.ModelAdmin):
    list_display = ('warehouse', 'part', 'lot_no', 'system_qty', 'actual_qty', 'diff_qty', 'status', 'checked_at')
    list_filter = ('status', 'warehouse', 'closing')
    search_fields = ('part__part_no', 'part__part_name')
    ordering = ('-checked_at',)


@admin.register(InventorySnapshot)
class InventorySnapshotAdmin(admin.ModelAdmin):
    list_display = ('closing', 'warehouse', 'part', 'lot_no', 'quantity', 'created_at')
    list_filter = ('closing', 'warehouse')
    search_fields = ('part__part_no', 'part__part_name')
    ordering = ('-closing__closing_month', 'warehouse__code')


# =============================================================================
# 공정 현품표 관리
# =============================================================================

@admin.register(ProcessTag)
class ProcessTagAdmin(admin.ModelAdmin):
    list_display = ('tag_id', 'part_no', 'part_name', 'quantity', 'lot_no', 'status', 'printed_at', 'scan_count')
    list_filter = ('status', 'printed_at')
    search_fields = ('tag_id', 'part_no', 'part_name')
    ordering = ('-printed_at',)
    readonly_fields = ('tag_id', 'scan_count', 'used_at', 'used_by', 'used_warehouse')


@admin.register(ProcessTagScanLog)
class ProcessTagScanLogAdmin(admin.ModelAdmin):
    list_display = ('tag', 'scanned_at', 'scanned_by', 'warehouse', 'is_first_scan', 'ip_address')
    list_filter = ('is_first_scan', 'scanned_at')
    search_fields = ('tag__tag_id', 'tag__part_no')
    ordering = ('-scanned_at',)


# =============================================================================
# 재고조사 세션 관리
# =============================================================================

class InventoryCheckSessionItemInline(admin.TabularInline):
    model = InventoryCheckSessionItem
    extra = 0
    fields = ('tag_id', 'part_no', 'part_name', 'lot_no', 'scanned_qty', 'system_qty', 'discrepancy', 'is_matched')
    readonly_fields = ('tag_id', 'part_no', 'part_name', 'lot_no', 'scanned_qty', 'system_qty', 'discrepancy', 'is_matched')
    can_delete = False


@admin.register(InventoryCheckSession)
class InventoryCheckSessionAdmin(admin.ModelAdmin):
    list_display = ('check_no', 'warehouse', 'check_date', 'status', 'total_scanned', 'total_matched', 'total_discrepancy', 'created_by')
    list_filter = ('status', 'warehouse', 'check_date')
    search_fields = ('check_no',)
    ordering = ('-check_date', '-created_at')
    inlines = [InventoryCheckSessionItemInline]


@admin.register(InventoryCheckSessionItem)
class InventoryCheckSessionItemAdmin(admin.ModelAdmin):
    list_display = ('check_session', 'tag_id', 'part_no', 'part_name', 'lot_no', 'scanned_qty', 'system_qty', 'discrepancy', 'is_matched')
    list_filter = ('is_matched', 'check_session__warehouse')
    search_fields = ('tag_id', 'part_no', 'part_name')
    ordering = ('-scanned_at',)