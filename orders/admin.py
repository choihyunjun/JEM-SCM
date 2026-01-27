# orders/admin.py

from django.contrib import admin
from django import forms
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from import_export.admin import ImportExportModelAdmin
from import_export.widgets import ForeignKeyWidget

from django.core.exceptions import ObjectDoesNotExist

from .models import Vendor, Order, Part, Inventory, Incoming, Organization, UserProfile, Notice, QnA


class UserProfileInlineForm(forms.ModelForm):
    """관리자/직원/협력사 3단 Role만 노출하기 위한 인라인 폼.

    - DB에 남아있는 레거시 역할(PURCHASE/PLAN/WAREHOUSE/QMS 등)은
      화면에서는 '직원(STAFF)'로 보이게 하고, 저장 시 STAFF로 정리합니다.
    """

    class Meta:
        model = UserProfile
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        inst = getattr(self, "instance", None)
        if inst and inst.pk:
            if inst.role not in {UserProfile.ROLE_ADMIN, UserProfile.ROLE_STAFF, UserProfile.ROLE_VENDOR}:
                # 화면에서는 직원으로 보이게
                inst.role = UserProfile.ROLE_STAFF



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
            "all": ("/static/admin/css/admin_custom.css",)
        }


# [0] 유저 권한 설정 (수정됨: 드롭박스 및 메뉴 체크박스 반영)
class UserProfileInline(admin.StackedInline):
    model = UserProfile
    form = UserProfileInlineForm
    can_delete = False
    verbose_name_plural = "진영전기 세부 권한 설정"
    extra = 0
    max_num = 1

    # 필드를 그룹화하여 보기 좋게 배치
    fieldsets = (
        (None, {
            "fields": ("role", "account_type", "org")
        }),
        ("인사/표시 정보", {
            "fields": (
                "display_name",
                "employee_no",
                "department",
                "position",
                "job_title",
            ),
        }),
        ("기타 설정", {
            "fields": ("is_jinyoung_staff",),
            "classes": ("collapse",),  # 평소에는 숨겨둠
        }),
    )


class UserAdmin(BaseUserAdmin):
    inlines = (UserProfileInline,)

    # list_display를 클래스 속성으로 올바르게 위치시킵니다.
    list_display = ("username", "email", "get_role", "is_staff", "is_active")

    def get_inline_instances(self, request, obj=None):
        # User 생성(add) 화면(obj가 None인 경우)에서는 인라인을 숨깁니다.
        if obj is None:
            return []
        return super().get_inline_instances(request, obj)

    def get_role(self, instance):
        # UserProfile related_name은 models.py에서 'profile' 입니다.
        profile = getattr(instance, "profile", None)
        return profile.get_role_display() if profile else "-"

    get_role.short_description = "사용자 그룹"


# 기본 User admin 교체
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(Organization)
class OrganizationAdmin(CustomAdminMixin, admin.ModelAdmin):
    list_display = ("name", "org_type", "linked_vendor")
    list_filter = ("org_type",)
    search_fields = ("name", "linked_vendor__name")
    autocomplete_fields = ["linked_vendor"]

    def get_queryset(self, request):
        """협력사 Organization은 Vendor에서 자동 생성되므로 내부 조직만 표시"""
        qs = super().get_queryset(request)
        return qs.filter(org_type="INTERNAL")


# ✅ 협력업체별 과부족 조회 ON/OFF 체크박스 복구 + UserProfile 권한 동기화
@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "get_linked_org", "user")
    search_fields = ("name", "code", "user__username", "user__email")

    def get_linked_org(self, obj):
        if hasattr(obj, 'organization') and obj.organization:
            return obj.organization.name
        return "-"
    get_linked_org.short_description = "연결된 조직"


# ============================================
# 공지사항 / QnA 관리
# ============================================

@admin.register(Notice)
class NoticeAdmin(admin.ModelAdmin):
    list_display = ("title", "is_important", "is_active", "created_by", "created_at")
    list_filter = ("is_important", "is_active")
    search_fields = ("title", "content")
    list_editable = ("is_important", "is_active")
    ordering = ("-is_important", "-created_at")

    def save_model(self, request, obj, form, change):
        if not change:  # 신규 생성 시
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(QnA)
class QnAAdmin(admin.ModelAdmin):
    list_display = ("title", "vendor", "is_answered_display", "created_at", "answered_at")
    list_filter = ("vendor", "answered_at")
    search_fields = ("title", "content", "answer", "vendor__name")
    readonly_fields = ("author", "vendor", "created_at")
    ordering = ("-created_at",)

    fieldsets = (
        ("질문 정보", {
            "fields": ("title", "content", "author", "vendor", "created_at")
        }),
        ("답변", {
            "fields": ("answer", "answered_by", "answered_at"),
            "classes": ("wide",)
        }),
    )

    def is_answered_display(self, obj):
        return "답변완료" if obj.is_answered else "대기중"
    is_answered_display.short_description = "답변상태"

    def save_model(self, request, obj, form, change):
        # 답변이 입력되었고 answered_at이 없으면 자동 설정
        if obj.answer and not obj.answered_at:
            from django.utils import timezone
            obj.answered_by = request.user
            obj.answered_at = timezone.now()
        super().save_model(request, obj, form, change)
