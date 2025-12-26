from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import models
from .models import M4Request, M4Review, M4ChangeLog 
from .forms import M4RequestForm
from django.utils import timezone
import datetime

@login_required
def m4_list(request):
    """목록: 필터링 및 검색 기능 포함"""
    requests = M4Request.objects.all().order_by('-created_at')
    status_filter = request.GET.get('status')
    if status_filter:
        requests = requests.filter(status=status_filter)
    query = request.GET.get('q')
    if query:
        requests = requests.filter(
            models.Q(part_no__icontains=query) | 
            models.Q(part_name__icontains=query)
        )
    context = {
        'requests': requests,
        'status_filter': status_filter,
        'query': query,
    }
    return render(request, 'qms/m4_list.html', context)

@login_required
def m4_detail(request, pk):
    """상세 페이지"""
    item = get_object_or_404(M4Request, pk=pk)
    return render(request, 'qms/m4_detail.html', {'item': item})

@login_required
def m4_create(request):
    """요청서 작성 (기안 임시저장 - 사내/사외 구분 번호 생성 로직)"""
    if request.method == "POST":
        form = M4RequestForm(request.POST, request.FILES)
        if 'request_no' in form.errors:
            del form.errors['request_no']

        if form.is_valid():
            m4_instance = form.save(commit=False)
            m4_instance.user = request.user 
            m4_instance.status = 'DRAFT'
            
            if not m4_instance.request_no:
                today_str = datetime.date.today().strftime('%Y%m%d')
                category = request.POST.get('quality_rank', '미분류') 
                prefix = f"4M-{category}-{today_str}"
                last_entry = M4Request.objects.filter(request_no__startswith=prefix).order_by('request_no').last()
                
                if last_entry:
                    try:
                        last_no = int(last_entry.request_no.split('-')[-1])
                        new_no = f"{prefix}-{str(last_no + 1).zfill(2)}"
                    except (ValueError, IndexError):
                        new_no = f"{prefix}-01"
                else:
                    new_no = f"{prefix}-01"
                m4_instance.request_no = new_no
            
            m4_instance.save()
            messages.success(request, f"기안이 저장되었습니다. (번호: {m4_instance.request_no})")
            return redirect('qms:m4_detail', pk=m4_instance.pk)
        else:
            messages.error(request, f"등록 실패: {form.errors.as_text()}")
    else:
        form = M4RequestForm()
    return render(request, 'qms/m4_form.html', {'form': form, 'mode': 'create'})

@login_required
def m4_submit(request, pk):
    """결재 상신 로직 (원본 보존)"""
    if request.method == 'POST':
        item = get_object_or_404(M4Request, pk=pk)
        if item.user == request.user and item.status == 'DRAFT':
            item.status = 'PENDING_REVIEW'
            item.is_submitted = True
            item.submitted_at = timezone.now()
            item.save()
            messages.success(request, "성공적으로 상신되었습니다.")
        else:
            messages.error(request, "상신 권한이 없거나 이미 처리된 문서입니다.")
    return redirect('qms:m4_detail', pk=pk)

@login_required
def m4_update(request, pk):
    """수정 및 변경 이력 기록 (원본 권한 체크 및 루프 로직 100% 복구)"""
    item = get_object_or_404(M4Request, pk=pk)
    can_edit = False
    
    if item.status != 'APPROVED':
        if item.user == request.user:
            can_edit = True
        elif item.status == 'PENDING_REVIEW' and item.reviewer_user == request.user:
            can_edit = True
        elif item.status == 'PENDING_APPROVE' and item.approver_user == request.user:
            can_edit = True
    
    if item.status == 'REJECTED' and item.user == request.user:
        can_edit = True

    if not can_edit:
        messages.error(request, "수정 권한이 없습니다.")
        return redirect('qms:m4_detail', pk=pk)

    if request.method == "POST":
        form = M4RequestForm(request.POST, request.FILES, instance=item)
        if 'request_no' in form.errors:
            del form.errors['request_no']

        if form.is_valid():
            changed_fields = form.changed_data
            if changed_fields:
                old_instance = M4Request.objects.get(pk=pk)
                for field in changed_fields:
                    try:
                        old_val = getattr(old_instance, field)
                        new_val = form.cleaned_data.get(field)
                        if str(old_val) != str(new_val):
                            M4ChangeLog.objects.create(
                                request=item,
                                user=request.user,
                                field_name=item._meta.get_field(field).verbose_name,
                                old_value=str(old_val) if old_val else "내용 없음",
                                new_value=str(new_val) if new_val else "내용 없음"
                            )
                    except Exception:
                        continue
            form.save()
            messages.success(request, "수정 이력이 기록되었습니다.")
            return redirect('qms:m4_detail', pk=pk)
    else:
        form = M4RequestForm(instance=item)
    return render(request, 'qms/m4_form.html', {'form': form, 'item': item, 'mode': 'update'})

@login_required
def m4_approve(request, pk):
    """결재 승인 로직 (원본 분기 보존)"""
    if request.method == 'POST':
        item = get_object_or_404(M4Request, pk=pk)
        if item.status == 'PENDING_REVIEW' and request.user == item.reviewer_user:
            item.status = 'PENDING_APPROVE'
            item.reviewed_at = timezone.now()
            item.is_reviewed = True
            item.save()
            messages.success(request, "검토 승인되었습니다.")
        elif item.status == 'PENDING_APPROVE' and request.user == item.approver_user:
            item.status = 'APPROVED'
            item.approved_at = timezone.now()
            item.is_approved = True
            item.save()
            messages.success(request, "최종 승인되었습니다.")
    return redirect('qms:m4_detail', pk=pk)

@login_required
def m4_reject(request, pk):
    """반려 로직 (원본 보존)"""
    if request.method == 'POST':
        item = get_object_or_404(M4Request, pk=pk)
        if request.user == item.reviewer_user or request.user == item.approver_user:
            item.status = 'REJECTED'
            item.reject_reason = request.POST.get('reject_reason')
            item.save()
            messages.warning(request, "반려 처리되었습니다.")
    return redirect('qms:m4_detail', pk=pk)

@login_required
def m4_resubmit(request, pk):
    """반려 후 재상신 (원본 필드 초기화 로직 복구)"""
    if request.method == 'POST':
        item = get_object_or_404(M4Request, pk=pk)
        if item.user == request.user and item.status == 'REJECTED':
            item.status = 'PENDING_REVIEW'
            item.reject_reason = "" 
            item.is_submitted = True
            item.submitted_at = timezone.now()
            item.is_reviewed = False
            item.reviewed_at = None
            item.is_approved = False
            item.approved_at = None
            item.save()
            messages.success(request, "다시 상신되었습니다.")
    return redirect('qms:m4_detail', pk=pk)

@login_required
def m4_cancel_approval(request, pk):
    """결재 취소 로직 (원본 물리기 로직 복구)"""
    if request.method == 'POST':
        item = get_object_or_404(M4Request, pk=pk)
        if item.status == 'PENDING_REVIEW' and request.user == item.user:
             item.status = 'DRAFT'
             item.is_submitted = False
             item.submitted_at = None
             item.save()
             messages.info(request, "상신이 취소되었습니다.")
        elif item.status == 'PENDING_APPROVE' and request.user == item.reviewer_user:
            item.status = 'PENDING_REVIEW'
            item.is_reviewed = False
            item.reviewed_at = None
            item.save()
            messages.info(request, "검토 승인이 취소되었습니다.")
        elif item.status == 'APPROVED' and request.user == item.approver_user:
            item.status = 'PENDING_APPROVE'
            item.is_approved = False
            item.approved_at = None
            item.save()
            messages.info(request, "최종 승인이 취소되었습니다.")
    return redirect('qms:m4_detail', pk=pk)

@login_required
def m4_delete(request, pk):
    """문서 삭제 로직 (원본 보존)"""
    item = get_object_or_404(M4Request, pk=pk)
    if item.user == request.user and item.status == 'DRAFT':
        if request.method == 'POST':
            item.delete()
            messages.success(request, "문서가 삭제되었습니다.")
            return redirect('qms:m4_list')
    else:
        messages.error(request, "삭제 권한이 없습니다.")
    return redirect('qms:m4_detail', pk=pk)

# --- 개선된 사내 검토 기능 (원본 권한 로직 유지 + 날짜 자동화 + 요청내용 필드 반영) ---

@login_required
def add_m4_review(request, pk):
    """검토 요청 등록 (발송일 및 품질팀 요청내용 자동 기록)"""
    if request.method == 'POST':
        m4_request = get_object_or_404(M4Request, pk=pk)
        M4Review.objects.create(
            request=m4_request,
            department=request.POST.get('department'),
            reviewer_name=request.POST.get('reviewer_name'),
            request_content=request.POST.get('request_content'), # 품질팀 상세 요청사항 반영
            reviewer=request.user,
            sent_at=timezone.now() # 품질팀 등록 시 발송일 기록
        )
        messages.success(request, "검토 요청(발송)이 등록되었습니다.")
    return redirect('qms:m4_detail', pk=pk)

@login_required
def edit_m4_review(request, review_id):
    """검토 의견 입력 및 수정 (접수일 자동 기록)"""
    review = get_object_or_404(M4Review, id=review_id)
    if request.method == 'POST' and (review.reviewer == request.user or request.user.is_staff):
        review.department = request.POST.get('department', review.department)
        review.reviewer_name = request.POST.get('reviewer_name', review.reviewer_name)
        review.content = request.POST.get('review_content')
        # 의견 입력 시 접수일 기록
        review.received_at = timezone.now() 
        review.save()
        messages.success(request, "검토 의견이 등록되어 접수 처리되었습니다.")
    return redirect('qms:m4_detail', pk=review.request.pk)

@login_required
def delete_m4_review(request, review_id):
    """검토 의견 삭제 (원본 권한 유지)"""
    review = get_object_or_404(M4Review, id=review_id)
    request_pk = review.request.pk
    if review.reviewer == request.user or request.user.is_staff:
        review.delete()
        messages.success(request, "검토 의견이 삭제되었습니다.")
    return redirect('qms:m4_detail', pk=request_pk)