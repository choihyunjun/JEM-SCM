from django.contrib import admin

from .models import (
    M4Request,
    M4Review,
    Formal4MRequest,
    Formal4MDocumentItem,
    Formal4MAttachment,
    Formal4MInspectionResult,
    Formal4MScheduleItem,
    Formal4MStageRecord,
    Formal4MApproval,
)


@admin.register(M4Request)
class M4RequestAdmin(admin.ModelAdmin):
    list_display = ("request_no", "part_no", "part_name", "status", "vendor_org", "user", "created_at")
    list_filter = ("status", "vendor_org")
    search_fields = ("request_no", "part_no", "part_name")


@admin.register(M4Review)
class M4ReviewAdmin(admin.ModelAdmin):
    list_display = ("request", "department", "reviewer_name", "sent_at", "received_at")
    list_filter = ("department",)
    search_fields = ("request__request_no", "department", "reviewer_name")


@admin.register(Formal4MRequest)
class Formal4MRequestAdmin(admin.ModelAdmin):
    list_display = ("formal_no", "template_type", "pre_request", "created_at")
    search_fields = ("formal_no", "pre_request__request_no", "pre_request__part_no")


@admin.register(Formal4MDocumentItem)
class Formal4MDocumentItemAdmin(admin.ModelAdmin):
    list_display = ("formal", "seq", "name", "is_required", "review_status")
    list_filter = ("review_status", "is_required")
    search_fields = ("formal__formal_no", "name")


@admin.register(Formal4MAttachment)
class Formal4MAttachmentAdmin(admin.ModelAdmin):
    list_display = ("item", "uploaded_by", "uploaded_at")
    search_fields = ("item__formal__formal_no",)

@admin.register(Formal4MInspectionResult)
class Formal4MInspectionResultAdmin(admin.ModelAdmin):
    list_display = ("formal_request", "inspection_item", "judgment")
    search_fields = ("formal_request__formal_no", "inspection_item")


@admin.register(Formal4MScheduleItem)
class Formal4MScheduleItemAdmin(admin.ModelAdmin):
    list_display = ("formal_request", "oem", "item_name", "plan_date")
    search_fields = ("formal_request__formal_no", "item_name", "oem")


@admin.register(Formal4MStageRecord)
class Formal4MStageRecordAdmin(admin.ModelAdmin):
    list_display = ("formal_request", "stage", "record_date")
    list_filter = ("stage",)
    search_fields = ("formal_request__formal_no",)


@admin.register(Formal4MApproval)
class Formal4MApprovalAdmin(admin.ModelAdmin):
    list_display = ("formal_request", "is_approved", "approval_no", "judgment_date")
    search_fields = ("formal_request__formal_no", "approval_no")
