from django.contrib import admin
from import_export import resources, fields
from import_export.admin import ImportExportModelAdmin
from import_export.fields import Field
from import_export.widgets import ForeignKeyWidget
from django.core.exceptions import ValidationError 
from django.core.mail import send_mail
from .models import Vendor, Order, Part, Inventory, Incoming 

# [1] 협력사 관리자
class VendorAdmin(admin.ModelAdmin):
    list_display = ('name',) 
    search_fields = ('name',) 

admin.site.register(Vendor, VendorAdmin)

# [2] 품목 마스터용 설정
class PartResource(resources.ModelResource):
    vendor = Field(
        column_name='vendor', 
        attribute='vendor', 
        widget=ForeignKeyWidget(Vendor, field='name')
    )
    
    class Meta:
        model = Part
        fields = ('vendor', 'part_group', 'part_no', 'part_name')
        import_id_fields = ('part_no',) # id 대신 품번을 고유 키로 사용

class PartAdmin(ImportExportModelAdmin):
    resource_class = PartResource
    list_display = ('vendor', 'part_group', 'part_no', 'part_name')
    list_filter = ('vendor', 'part_group')
    search_fields = ('part_no', 'part_name')

admin.site.register(Part, PartAdmin)

# [3] 발주 관리용 설정
class OrderResource(resources.ModelResource):
    vendor = Field(
        column_name='vendor', 
        attribute='vendor', 
        widget=ForeignKeyWidget(Vendor, field='name')
    )

    def get_instance(self, instance_loader, row):
        return None

    def before_import_row(self, row, **kwargs):
        excel_vendor = row.get('vendor')
        excel_part_no = row.get('part_no')
        
        if not excel_vendor or not excel_part_no:
            return

        part_master = Part.objects.filter(part_no=excel_part_no, vendor__name=excel_vendor).first()

        if not part_master:
            raise ValidationError(f"⛔ 품목 마스터에 등록되지 않은 정보입니다: {excel_part_no}")

        row['part_name'] = part_master.part_name
        row['part_group'] = part_master.part_group
        row['품목군'] = part_master.part_group

    def after_save_instance(self, instance, *args, **kwargs):
        if not kwargs.get('dry_run', False):
            try:
                if instance.vendor.user and instance.vendor.user.email:
                    subject = f"[SCM] 새로운 발주가 등록되었습니다."
                    message = f"품번: {instance.part_no}\n수량: {instance.quantity}\n납기: {instance.due_date}"
                    send_mail(subject, message, "system@jemscm.com", [instance.vendor.user.email], fail_silently=True)
            except Exception:
                pass

    class Meta:
        model = Order
        fields = ('vendor', 'part_group', 'part_no', 'part_name', 'quantity', 'due_date')
        exclude = ('id',) 

class OrderAdmin(ImportExportModelAdmin):
    resource_class = OrderResource
    list_display = ('vendor', 'part_group', 'part_no', 'part_name', 'quantity', 'due_date')
    list_filter = ('vendor', 'part_group', 'due_date')
    search_fields = ('part_no', 'part_name', 'vendor__name')

admin.site.register(Order, OrderAdmin)


# [4] 기초재고 관리용 설정 (ERP 덮어쓰기 전용)
class InventoryResource(resources.ModelResource):
    part_no = Field(
        column_name='품번', 
        attribute='part',
        widget=ForeignKeyWidget(Part, field='part_no')
    )
    base_stock = Field(column_name='기초재고', attribute='base_stock')

    class Meta:
        model = Inventory
        import_id_fields = ('part_no',) 
        fields = ('part_no', 'base_stock')
        exclude = ('id',) 

    def skip_row(self, instance, original, row, import_validation_errors=None):
        if not instance.part_id:
            return True
        return super().skip_row(instance, original, row, import_validation_errors)

@admin.register(Inventory)
class InventoryAdmin(ImportExportModelAdmin):
    resource_class = InventoryResource
    list_display = ('get_part_no', 'get_part_name', 'get_vendor', 'base_stock', 'updated_at')
    search_fields = ('part__part_no', 'part__part_name', 'part__vendor__name')
    list_filter = ('part__vendor', 'part__part_group')

    def get_part_no(self, obj): return obj.part.part_no
    def get_part_name(self, obj): return obj.part.part_name
    def get_vendor(self, obj): return obj.part.vendor.name
    
    get_part_no.short_description = '품번'
    get_part_name.short_description = '품명'
    get_vendor.short_description = '협력사'


# [5] 일자별 입고 관리용 설정 (누적 등록용 - id 에러 해결 핵심)
class IncomingResource(resources.ModelResource):
    part_no = Field(
        column_name='품번', 
        attribute='part',
        widget=ForeignKeyWidget(Part, field='part_no')
    )
    in_date = Field(column_name='입고일', attribute='in_date')
    quantity = Field(column_name='입고수량', attribute='quantity')

    class Meta:
        model = Incoming
        # [수정 핵심] import_id_fields를 비워야 id 없이 신규 등록이 가능합니다.
        import_id_fields = [] 
        fields = ('part_no', 'in_date', 'quantity')
        exclude = ('id',) 

@admin.register(Incoming)
class IncomingAdmin(ImportExportModelAdmin):
    resource_class = IncomingResource
    list_display = ('get_part_no', 'get_part_name', 'in_date', 'quantity', 'created_at')
    list_filter = ('in_date', 'part__vendor')
    search_fields = ('part__part_no', 'part__part_name')

    def get_part_no(self, obj): return obj.part.part_no
    def get_part_name(self, obj): return obj.part.part_name
    
    get_part_no.short_description = '품번'
    get_part_name.short_description = '품명'