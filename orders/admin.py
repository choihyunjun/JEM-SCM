from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from import_export import resources, fields
from import_export.admin import ImportExportModelAdmin
from import_export.fields import Field
from import_export.widgets import ForeignKeyWidget
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.mail import send_mail
from .models import Vendor, Order, Part, Inventory, Incoming, UserProfile 

# [도움말] 위젯이 에러를 던지지 않도록 커스텀 위젯 설정
class SafeForeignKeyWidget(ForeignKeyWidget):
    def clean(self, value, row=None, **kwargs):
        try:
            return super().clean(value, row, **kwargs)
        except ObjectDoesNotExist:
            return None 

# --- CSS 적용을 위한 클래스 ---
class CustomAdminMixin:
    class Media:
        css = {
            'all': ('/static/admin/css/admin_custom.css',)
        }

# [0] 유저 권한 설정 (수정됨: 드롭박스 및 메뉴 체크박스 반영)
class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = '진영전기 세부 권한 설정'
    extra = 0
    max_num = 1
    # 필드를 그룹화하여 보기 좋게 배치
    fieldsets = (
        (None, {
            'fields': ('role',)
        }),
        ('메뉴별 접근 권한 (체크 시 해당 메뉴 노출)', {
            'fields': (
                'can_view_orders', 
                'can_register_orders', 
                'can_view_inventory', 
                'can_manage_incoming', 
                'can_access_scm_admin'
            ),
        }),
        ('기타 설정', {
            'fields': ('is_jinyoung_staff',),
            'classes': ('collapse',), # 평소에는 숨겨둠
        }),
    )

class UserAdmin(BaseUserAdmin):
    inlines = (UserProfileInline,)
    list_display = ('username', 'email', 'get_role', 'is_staff', 'is_active')
    
    def get_role(self, instance):
        profile = getattr(instance, 'profile', None)
        return profile.get_role_display() if profile else "-"
    get_role.short_description = '사용자 그룹'

admin.site.unregister(User)
admin.site.register(User, UserAdmin)

# [1] 협력사 관리자 (수정됨: 사업자등록번호 및 신규 필드 노출)
class VendorAdmin(CustomAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'code', 'biz_registration_number', 'representative', 'user') 
    search_fields = ('name', 'code', 'biz_registration_number')
    # 상세 페이지 레이아웃 정리
    fieldsets = (
        ('기본 정보', {
            'fields': ('name', 'code', 'user', 'can_view_inventory')
        }),
        ('납품서/ERP 마스터 정보', {
            'fields': ('biz_registration_number', 'representative', 'address', 'biz_type', 'biz_item', 'erp_code'),
            'description': 'ERP 코드는 DB 저장용으로만 사용되며 납품서에는 사업자번호가 출력됩니다.'
        }),
    )

admin.site.register(Vendor, VendorAdmin)

# [2] 품목 마스터용 설정
class PartResource(resources.ModelResource):
    vendor = Field(column_name='vendor', attribute='vendor', widget=ForeignKeyWidget(Vendor, field='name'))
    class Meta:
        model = Part
        fields = ('vendor', 'part_group', 'part_no', 'part_name')
        import_id_fields = ('part_no',) 

class PartAdmin(CustomAdminMixin, ImportExportModelAdmin):
    resource_class = PartResource
    list_display = ('vendor', 'part_group', 'part_no', 'part_name')
    list_filter = ('vendor', 'part_group')
    search_fields = ('part_no', 'part_name')
admin.site.register(Part, PartAdmin)

# [3] 발주 관리용 설정
class OrderResource(resources.ModelResource):
    vendor = Field(column_name='vendor', attribute='vendor', widget=SafeForeignKeyWidget(Vendor, field='name'))
    def get_instance(self, instance_loader, row): return None
    def skip_row(self, instance, original, row, import_validation_errors=None):
        excel_vendor = row.get('vendor')
        excel_part_no = row.get('part_no')
        part_exists = Part.objects.filter(part_no=excel_part_no, vendor__name=excel_vendor).exists()
        return not part_exists or super().skip_row(instance, original, row, import_validation_errors)
    def before_import_row(self, row, **kwargs):
        part_master = Part.objects.filter(part_no=row.get('part_no'), vendor__name=row.get('vendor')).first()
        if part_master:
            row['part_name'] = part_master.part_name
            row['part_group'] = part_master.part_group
    class Meta:
        model = Order
        fields = ('vendor', 'part_group', 'part_no', 'part_name', 'quantity', 'due_date')
        exclude = ('id',) 

class OrderAdmin(CustomAdminMixin, ImportExportModelAdmin):
    resource_class = OrderResource
    list_display = ('vendor', 'part_group', 'part_no', 'part_name', 'quantity', 'due_date')
    list_filter = ('vendor', 'part_group', 'due_date')
    search_fields = ('part_no', 'part_name', 'vendor__name')
admin.site.register(Order, OrderAdmin)

# [4] 기초재고 관리용 설정
class InventoryResource(resources.ModelResource):
    part_no = Field(column_name='품번', attribute='part', widget=SafeForeignKeyWidget(Part, field='part_no'))
    class Meta:
        model = Inventory
        import_id_fields = ('part_no',) 
        fields = ('part_no', 'base_stock')
        exclude = ('id',) 

@admin.register(Inventory)
class InventoryAdmin(CustomAdminMixin, ImportExportModelAdmin):
    resource_class = InventoryResource
    list_display = ('get_part_no', 'get_part_name', 'get_vendor', 'base_stock', 'updated_at')
    search_fields = ('part__part_no', 'part__part_name', 'part__vendor__name')
    list_filter = ('part__vendor', 'part__part_group')
    def get_part_no(self, obj): return obj.part.part_no if obj.part else ""
    def get_part_name(self, obj): return obj.part.part_name if obj.part else ""
    def get_vendor(self, obj): return obj.part.vendor.name if obj.part else ""

# [5] 일자별 입고 관리용 설정
class IncomingResource(resources.ModelResource):
    part_no = Field(column_name='품번', attribute='part', widget=SafeForeignKeyWidget(Part, field='part_no'))
    class Meta:
        model = Incoming
        fields = ('part_no', 'in_date', 'quantity')
        exclude = ('id',) 

@admin.register(Incoming)
class IncomingAdmin(CustomAdminMixin, ImportExportModelAdmin):
    resource_class = IncomingResource
    list_display = ('get_part_no', 'get_part_name', 'in_date', 'quantity', 'created_at')
    list_filter = ('in_date', 'part__vendor')
    search_fields = ('part__part_no', 'part__part_name')
    def get_part_no(self, obj): return obj.part.part_no if obj.part else ""
    def get_part_name(self, obj): return obj.part.part_name if obj.part else ""

# 관리자 페이지 헤더 설정
admin.site.site_header = '진영전기 SCM 관리자'
admin.site.site_title = '진영전기 관리자'
admin.site.index_title = '시스템 관리 및 설정'