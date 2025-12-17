from django.contrib import admin
from import_export import resources, fields
from import_export.admin import ImportExportModelAdmin
from import_export.fields import Field
from import_export.widgets import ForeignKeyWidget
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.mail import send_mail
from .models import Vendor, Order, Part, Inventory, Incoming 

# [도움말] 위젯이 에러를 던지지 않도록 커스텀 위젯 설정
class SafeForeignKeyWidget(ForeignKeyWidget):
    def clean(self, value, row=None, **kwargs):
        try:
            return super().clean(value, row, **kwargs)
        except ObjectDoesNotExist:
            return None # 마스터에 없으면 에러 대신 None 반환 (스킵의 밑거름)

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
        import_id_fields = ('part_no',) 

class PartAdmin(ImportExportModelAdmin):
    resource_class = PartResource
    list_display = ('vendor', 'part_group', 'part_no', 'part_name')
    list_filter = ('vendor', 'part_group')
    search_fields = ('part_no', 'part_name')

admin.site.register(Part, PartAdmin)

# [3] 발주 관리용 설정 (OrderResource)
class OrderResource(resources.ModelResource):
    vendor = Field(
        column_name='vendor', 
        attribute='vendor', 
        widget=SafeForeignKeyWidget(Vendor, field='name') # 커스텀 위젯 적용
    )

    def get_instance(self, instance_loader, row):
        return None

    def skip_row(self, instance, original, row, import_validation_errors=None):
        excel_vendor = row.get('vendor')
        excel_part_no = row.get('part_no')
        # 마스터에 해당 품번과 업체가 동시에 존재하는지 확인
        part_exists = Part.objects.filter(part_no=excel_part_no, vendor__name=excel_vendor).exists()
        if not part_exists:
            return True 
        return super().skip_row(instance, original, row, import_validation_errors)

    def before_import_row(self, row, **kwargs):
        excel_vendor = row.get('vendor')
        excel_part_no = row.get('part_no')
        part_master = Part.objects.filter(part_no=excel_part_no, vendor__name=excel_vendor).first()
        if part_master:
            row['part_name'] = part_master.part_name
            row['part_group'] = part_master.part_group

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


# [4] 기초재고 관리용 설정 (InventoryResource)
class InventoryResource(resources.ModelResource):
    part_no = Field(
        column_name='품번', 
        attribute='part',
        widget=SafeForeignKeyWidget(Part, field='part_no') # 커스텀 위젯 적용
    )
    base_stock = Field(column_name='기초재고', attribute='base_stock')

    class Meta:
        model = Inventory
        import_id_fields = ('part_no',) 
        fields = ('part_no', 'base_stock')
        exclude = ('id',) 

    def skip_row(self, instance, original, row, import_validation_errors=None):
        excel_part_no = row.get('품번')
        # 위젯에서 못 찾은 경우(None) 혹은 DB에 없는 경우 둘 다 스킵
        if not excel_part_no or not Part.objects.filter(part_no=excel_part_no).exists():
            return True 
        return super().skip_row(instance, original, row, import_validation_errors)

@admin.register(Inventory)
class InventoryAdmin(ImportExportModelAdmin):
    resource_class = InventoryResource
    list_display = ('get_part_no', 'get_part_name', 'get_vendor', 'base_stock', 'updated_at')
    search_fields = ('part__part_no', 'part__part_name', 'part__vendor__name')
    list_filter = ('part__vendor', 'part__part_group')

    def get_part_no(self, obj): return obj.part.part_no if obj.part else ""
    def get_part_name(self, obj): return obj.part.part_name if obj.part else ""
    def get_vendor(self, obj): return obj.part.vendor.name if obj.part else ""
    
    get_part_no.short_description = '품번'
    get_part_name.short_description = '품명'
    get_vendor.short_description = '협력사'


# [5] 일자별 입고 관리용 설정 (IncomingResource)
class IncomingResource(resources.ModelResource):
    part_no = Field(
        column_name='품번', 
        attribute='part',
        widget=SafeForeignKeyWidget(Part, field='part_no') # 커스텀 위젯 적용
    )
    in_date = Field(column_name='입고일', attribute='in_date')
    quantity = Field(column_name='입고수량', attribute='quantity')

    class Meta:
        model = Incoming
        import_id_fields = [] 
        fields = ('part_no', 'in_date', 'quantity')
        exclude = ('id',) 

    def skip_row(self, instance, original, row, import_validation_errors=None):
        excel_part_no = row.get('품번')
        if not excel_part_no or not Part.objects.filter(part_no=excel_part_no).exists():
            return True 
        return super().skip_row(instance, original, row, import_validation_errors)

@admin.register(Incoming)
class IncomingAdmin(ImportExportModelAdmin):
    resource_class = IncomingResource
    list_display = ('get_part_no', 'get_part_name', 'in_date', 'quantity', 'created_at')
    list_filter = ('in_date', 'part__vendor')
    search_fields = ('part__part_no', 'part__part_name')

    def get_part_no(self, obj): return obj.part.part_no if obj.part else ""
    def get_part_name(self, obj): return obj.part.part_name if obj.part else ""
    
    get_part_no.short_description = '품번'
    get_part_name.short_description = '품명'