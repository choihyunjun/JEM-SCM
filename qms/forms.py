from django import forms
from .models import M4Request
from django.contrib.auth.models import User

class M4RequestForm(forms.ModelForm):
    # [신규] 결재선 지정을 위한 필드 추가
    reviewer_user = forms.ModelChoiceField(
        queryset=User.objects.all(),
        label="검토자",
        empty_label="--- 검토자 선택 ---",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    approver_user = forms.ModelChoiceField(
        queryset=User.objects.all(),
        label="최종 승인자",
        empty_label="--- 승인자 선택 ---",
        widget=forms.Select(attrs={'class': 'form-select'})
    )

    class Meta:
        model = M4Request
        # [수정] 결재자 필드를 포함한 전체 필드 나열
        fields = [
            'factory', 'product', 'model_name', 'quality_rank',
            'part_no', 'part_name', 'request_no', 'm4_type',
            'reviewer_user', 'approver_user',  # <--- 결재자 필드 추가
            'reason', 'content_before', 'content_after', 
            'photo_before', 'photo_after',
            'affected_features', 'due_date',
            'plan_step1', 'plan_step2', 'plan_step3', 'plan_step4',
            'plan_step5', 'plan_step6', 'plan_step7', 'plan_step8'
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
        super().__init__(*args, **kwargs)
        
        # [기존 로직 유지] 사진 필드 필수 제외
        if 'photo_before' in self.fields:
            self.fields['photo_before'].required = False
        if 'photo_after' in self.fields:
            self.fields['photo_after'].required = False

        # [기존 로직 유지] 모든 필드에 Bootstrap 클래스 자동 부여
        for field_name, field in self.fields.items():
            # Select(드롭다운)와 나머지 입력창 구분하여 클래스 부여
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.update({'class': 'form-select'})
            else:
                field.widget.attrs.update({'class': 'form-control'})