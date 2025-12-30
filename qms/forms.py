from django import forms
from django.contrib.auth.models import User
from django.db.models import Q
from orders.models import Organization
from .policies import get_actor
from .models import (
    M4Request,
    M4Review,
    Formal4MRequest,
    Formal4MApproval,
    Formal4MInspectionResult,
    Formal4MScheduleItem,
    Formal4MStageRecord,
)


class Formal4MInspectionResultForm(forms.ModelForm):
    class Meta:
        model = Formal4MInspectionResult
        # inspection_item 은 기본 템플릿 값이므로 기본적으로 수정하지 않도록 둔다.
        fields = ["inspection_item", "spec", "method", "judgment", "remark", "attachment"]
        widgets = {
            "inspection_item": forms.TextInput(attrs={"class": "form-control form-control-sm", "readonly": True}),
            "spec": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "method": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "judgment": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "remark": forms.Textarea(
                attrs={"class": "form-control form-control-sm", "rows": 2, "placeholder": "비고"}
            ),
            "attachment": forms.ClearableFileInput(attrs={"class": "form-control form-control-sm"}),
        }


class Formal4MScheduleItemForm(forms.ModelForm):
    class Meta:
        model = Formal4MScheduleItem
        fields = ["item_name", "is_required", "plan_date", "owner_name", "department", "note"]
        widgets = {
            "item_name": forms.TextInput(attrs={"class": "form-control form-control-sm", "readonly": True}),
            "is_required": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "plan_date": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "owner_name": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "department": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "note": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 2, "placeholder": "비고"}),
        }


class Formal4MApprovalForm(forms.ModelForm):
    class Meta:
        model = Formal4MApproval
        fields = ["is_approved", "approval_no", "judgment_date", "remark"]
        widgets = {
            "is_approved": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "approval_no": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "judgment_date": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "remark": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 3, "placeholder": "특기사항"}),
        }


class Formal4MStageRecordForm(forms.ModelForm):
    class Meta:
        model = Formal4MStageRecord
        fields = ["stage", "record_date", "remark", "attachment"]
        widgets = {
            "stage": forms.TextInput(attrs={"class": "form-control form-control-sm", "readonly": True}),
            "record_date": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "remark": forms.Textarea(
                attrs={"class": "form-control form-control-sm", "rows": 2, "placeholder": "비고"}
            ),
            "attachment": forms.ClearableFileInput(attrs={"class": "form-control form-control-sm"}),
        }




def _internal_users_qs():
    """결재자 드롭다운에 노출할 내부 사용자 쿼리셋.

    - 협력사(VENDOR) 계정은 제외
    - UserProfile 값이 꼬였을 수 있어 role/is_jinyoung_staff/is_staff 등을 함께 고려
    """
    return (
        User.objects.filter(
            Q(is_superuser=True)
            | Q(is_staff=True)
            | Q(profile__role__in=["ADMIN", "STAFF"])
            | Q(profile__is_jinyoung_staff=True)
            | Q(profile__account_type="INTERNAL")
        )
        .distinct()
        .order_by("username")
    )


class InternalUserChoiceField(forms.ModelChoiceField):
    """드롭다운 표시: '부서명 + 이름' 형태."""

    def label_from_instance(self, obj):
        profile = getattr(obj, "profile", None)
        dept = ""
        if profile is not None:
            dept = (getattr(profile, "department", None) or "").strip()
        name = ""
        if profile is not None:
            name = (getattr(profile, "display_name", None) or "").strip()
        if not name:
            name = (obj.get_full_name() or f"{obj.last_name}{obj.first_name}" or obj.username).strip()
        return f"{dept} {name}".strip() if dept else name


class M4RequestForm(forms.ModelForm):    # [결재선] 내부 사용자만 노출 (부서명 표시)
    reviewer_user = InternalUserChoiceField(
        queryset=User.objects.none(),
        label="검토1",
        empty_label="--- 검토1 선택 ---",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    reviewer_user2 = InternalUserChoiceField(
        queryset=User.objects.none(),
        label="검토2",
        required=False,
        empty_label="--- 검토2 선택(해당 시) ---",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    approver_user = InternalUserChoiceField(
        queryset=User.objects.none(),
        label="최종 승인자",
        empty_label="--- 승인자 선택 ---",
        widget=forms.Select(attrs={'class': 'form-select'})
    )

    vendor_org = forms.ModelChoiceField(
        queryset=Organization.objects.filter(org_type="VENDOR").order_by("name"),
        label="협력사(대상)",
        required=False,
        empty_label="--- 협력사 선택(해당 시) ---",
        widget=forms.Select(attrs={'class': 'form-select'})
    )

    class Meta:
        model = M4Request
        # [수정] 필드 중복 제거 및 논리적 순서 배치
        fields = [
            'factory', 'product', 'model_name', 'quality_rank',
            'part_no', 'part_name', 'request_no', 'm4_type',
            'vendor_org',
            'reviewer_user', 'reviewer_user2', 'approver_user',  # 결재선 지정 필드
            'reason', 'content_before', 'content_after', 
            'photo_before', 'photo_after',
            'affected_features', 'due_date',
            'plan_step1', 'plan_step2', 'plan_step3', 'plan_step4',
            'plan_step5', 'plan_step6', 'plan_step7', 'plan_step8',
            'reject_reason',
        ]
        
        # 기본 위젯 설정
        widgets = {
            # 날짜 필드 캘린더 적용
            **{f'plan_step{i}': forms.DateInput(attrs={'type': 'date'}) for i in range(1, 9)},
            'due_date': forms.DateInput(attrs={'type': 'date'}),
            'reason': forms.Textarea(attrs={'rows': 2}),
            'content_before': forms.Textarea(attrs={'rows': 4}),
            'content_after': forms.Textarea(attrs={'rows': 4}),
            'photo_before': forms.ClearableFileInput(),
            'photo_after': forms.ClearableFileInput(),
        }

    def __init__(self, *args, **kwargs):
        self._actor_user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        # 결재자 목록: 내부 사용자만
        internal_qs = _internal_users_qs()
        if "reviewer_user" in self.fields:
            self.fields["reviewer_user"].queryset = internal_qs
        if "reviewer_user2" in self.fields:
            self.fields["reviewer_user2"].queryset = internal_qs
        if "approver_user" in self.fields:
            self.fields["approver_user"].queryset = internal_qs

        
        # [기존 로직 유지] 사진 필드 필수 제외
        if 'photo_before' in self.fields:
            self.fields['photo_before'].required = False
        if 'photo_after' in self.fields:
            self.fields['photo_after'].required = False

        # [기존 로직 유지] 모든 필드에 Bootstrap 클래스 자동 부여
        for field_name, field in self.fields.items():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.update({'class': 'form-select'})
            else:
                # 이미 위젯 attrs에 클래스가 있어도 덮어쓰지 않고 추가하거나 유지
                current_class = field.widget.attrs.get('class', '')
                if 'form-control' not in current_class and 'form-select' not in current_class:
                    field.widget.attrs.update({'class': f'{current_class} form-control'.strip()})

        # 내부/협력사 권한에 따른 필드 제어
        # - 내부: 작성자가 협력사(대상)를 선택할 수 있어야 함
        # - 협력사: (만약 접근하더라도) 협력사 선택을 임의로 바꾸지 못하게 잠금
        if self._actor_user is not None:
            actor = get_actor(self._actor_user)
            if actor.is_vendor and actor.profile:
                self.fields["vendor_org"].disabled = True
                self.fields["vendor_org"].required = False
                if actor.profile.org_id:
                    self.fields["vendor_org"].initial = actor.profile.org_id




class Formal4MWorkflowForm(forms.ModelForm):
    """정식 4M 결재선 지정용 폼."""

    approval_reviewer_user = InternalUserChoiceField(
        queryset=User.objects.none(),
        label="검토1",
        empty_label="--- 검토1 선택 ---",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    approval_reviewer_user2 = InternalUserChoiceField(
        queryset=User.objects.none(),
        required=False,
        label="검토2",
        empty_label="--- 검토2 선택(선택) ---",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    approval_approver_user = InternalUserChoiceField(
        queryset=User.objects.none(),
        label="최종 승인자",
        empty_label="--- 최종 승인자 선택 ---",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = Formal4MRequest
        fields = [
            "approval_reviewer_user",
            "approval_reviewer_user2",
            "approval_approver_user",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = _internal_users_qs()
        self.fields["approval_reviewer_user"].queryset = qs
        self.fields["approval_reviewer_user2"].queryset = qs
        self.fields["approval_approver_user"].queryset = qs


class M4VendorResponseForm(forms.ModelForm):
    """협력사 회신/증빙 업로드 폼"""
    class Meta:
        model = M4Review
        fields = ["content", "evidence_file"]
        widgets = {
            "content": forms.Textarea(attrs={"rows": 3, "class": "form-control", "placeholder": "조치/답변 내용을 입력하세요"}),
            "evidence_file": forms.ClearableFileInput(attrs={"class": "form-control"}),
        }

class Formal4MInspectionResultUpdateForm(forms.ModelForm):
    """정식 4M - 변경 검토 결과 수정용(내부)."""

    class Meta:
        model = Formal4MInspectionResult
        fields = ["spec", "method", "judgment", "remark", "attachment"]
        widgets = {
            "spec": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "method": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            # 판단값은 조직마다 코드가 다를 수 있어 텍스트로 둔다.
            "judgment": forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "예: OK / NG"}),
            "remark": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "attachment": forms.ClearableFileInput(attrs={"class": "form-control form-control-sm"}),
        }


class Formal4MScheduleItemUpdateForm(forms.ModelForm):
    """정식 4M - 일정 계획 수정용(내부)."""

    class Meta:
        model = Formal4MScheduleItem
        fields = ["oem", "item_name", "is_required", "plan_date", "owner_name", "department", "note"]
        widgets = {
            "oem": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "item_name": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "is_required": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "plan_date": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "owner_name": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "department": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "note": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
        }


class Formal4MStageRecordUpdateForm(forms.ModelForm):
    """정식 4M - 단계별 기록 수정용(내부/협력사 공용).

    stage 자체는 서버에서 고정(숨김)하고, 날짜/비고/첨부만 수정한다.
    """

    class Meta:
        model = Formal4MStageRecord
        fields = ["record_date", "remark", "attachment"]
        widgets = {
            "record_date": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "remark": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "attachment": forms.ClearableFileInput(attrs={"class": "form-control form-control-sm"}),
        }


class Formal4MApprovalUpdateForm(forms.ModelForm):
    """정식 4M - 사내 승인 수정용(내부)."""

    class Meta:
        model = Formal4MApproval
        fields = ["is_approved", "approval_no", "judgment_date", "remark"]
        widgets = {
            "is_approved": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "approval_no": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "judgment_date": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "remark": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
        }
