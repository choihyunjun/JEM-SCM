from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.core.paginator import Paginator
from orders.decorators import wms_permission_required
from .models import ERPIncomingOperation
from .erp_outbox import dispatch, reconcile, retry_after_verification


@wms_permission_required('can_wms_incoming_process')
def incoming_operations(request):
    if request.method == 'POST':
        job = get_object_or_404(ERPIncomingOperation, pk=request.POST.get('operation'))
        try:
            action = request.POST.get('action')
            if action == 'reconcile':
                ok, message = reconcile(job.pk)
                (messages.success if ok else messages.warning)(request, message)
            elif action == 'send':
                dispatch(job.pk)
                messages.info(request, '전송 상태를 확인해 주세요.')
            elif action == 'verified_retry':
                retry_after_verification(job.pk, request.user, request.POST.get('note', ''))
                messages.info(request, '확인 내용을 기록하고 재전송을 요청했습니다.')
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect('material:erp_incoming_operations')
    qs = ERPIncomingOperation.objects.prefetch_related('events__actor').order_by('-created_at')
    status = request.GET.get('status', '')
    if status:
        qs = qs.filter(status=status)
    return render(request, 'material/erp_incoming_operations.html', {
        'page_obj': Paginator(qs, 30).get_page(request.GET.get('page')),
        'status': status,
        'statuses': ERPIncomingOperation._meta.get_field('status').choices,
    })
