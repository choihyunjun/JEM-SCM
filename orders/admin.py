from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from import_export import resources, fields
from import_export.admin import ImportExportModelAdmin
from import_export.fields import Field
from import_export.widgets import ForeignKeyWidget
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.mail import send_mail
from .models import Vendor, Order, Part, Inventory, Incoming, Organization, UserProfile 

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
            'fields': ('role', 'account_type', 'org')
        }),
        ('인사/표시 정보', {
            'fields': (
                'display_name',
                'employee_no',
                'department',
                'position',
                'job_title',
            ),
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
    # list_display를 클래스 속성으로 올바르게 위치시킵니다.
    list_display = ('username', 'email', 'get_role', 'is_staff', 'is_active')

    # 메서드를 클래스 내부(들여쓰기)로 이동
    def get_inline_instances(self, request, obj=None):
        # User 생성(add) 화면(obj가 None인 경우)에서는 인라인을 숨깁니다.
        if obj is None:
            return []
        return super().get_inline_instances(request, obj)

    def get_role(self, instance):
        # profile은 User 모델의 related_name 설정에 따라 달라질 수 있습니다.
        # 보통 userprofile 또는 profile로 연결됩니다.
        profile = getattr(instance, 'userprofile', None) 
        return profile.get_role_display() if profile else "-"
    get_role.short_description = '사용자 그룹'

admin.site.unregister(User)
admin.site.register(User, UserAdmin)

@admin.register(Organization)
class OrganizationAdmin(CustomAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'org_type')
    list_filter = ('org_type',)
    search_fields = ('name',)
