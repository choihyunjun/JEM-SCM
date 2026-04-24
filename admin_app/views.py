from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from orders.decorators import admin_required
from .models import (
    NotificationRule, NotificationLog,
    ACTIVE_EVENT_CHOICES, NOTIFICATION_EVENT_CHOICES,
)
import json


@login_required
@admin_required
def admin_dashboard(request):
    """관리자 대시보드"""
    from orders.models import Vendor, Part
    from django.contrib.auth.models import User

    context = {
        'vendor_count': Vendor.objects.count(),
        'part_count': Part.objects.count(),
        'user_count': User.objects.filter(is_active=True).count(),
        'rule_count': NotificationRule.objects.filter(is_active=True).count(),
        'pending_count': NotificationLog.objects.filter(status='PENDING').count(),
    }
    return render(request, 'admin_app/dashboard.html', context)


@login_required
@admin_required
def notification_manage(request):
    """알림 관리 페이지"""
    context = {
        'event_choices': ACTIVE_EVENT_CHOICES,
    }
    return render(request, 'admin_app/notification_manage.html', context)


@login_required
@admin_required
def api_rules(request):
    """알림 규칙 CRUD API"""
    if request.method == 'GET':
        rules = NotificationRule.objects.prefetch_related('recipients').all()
        data = [{
            'id': r.id,
            'event_type': r.event_type,
            'event_display': r.get_event_type_display(),
            'is_active': r.is_active,
            'send_to_vendor': r.send_to_vendor,
            'send_to_requester': r.send_to_requester,
            'description': r.description,
            'recipient_ids': list(r.recipients.values_list('id', flat=True)),
            'recipient_names': ', '.join(
                f"{getattr(u, 'profile', None) and getattr(u.profile, 'department', '') or ''} {getattr(u, 'profile', None) and getattr(u.profile, 'display_name', '') or u.username}".strip()
                for u in r.recipients.all()
            ),
        } for r in rules]
        return JsonResponse({'rules': data})

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'JSON 파싱 오류'})

        action = data.get('action', '')

        if action == 'add':
            event_type = data.get('event_type', '')
            if not event_type:
                return JsonResponse({'success': False, 'error': '이벤트를 선택하세요.'})
            if NotificationRule.objects.filter(event_type=event_type).exists():
                return JsonResponse({'success': False, 'error': '이미 해당 이벤트 규칙이 존재합니다.'})
            rule = NotificationRule.objects.create(
                event_type=event_type,
                send_to_vendor=bool(data.get('send_to_vendor', False)),
                send_to_requester=bool(data.get('send_to_requester', False)),
                description=(data.get('description') or '').strip(),
                is_active=True,
            )
            recipient_ids = data.get('recipient_ids', [])
            if recipient_ids:
                rule.recipients.set(recipient_ids)
            return JsonResponse({'success': True, 'message': '알림 규칙 등록 완료'})

        elif action == 'update':
            try:
                rule = NotificationRule.objects.get(id=data.get('id'))
            except NotificationRule.DoesNotExist:
                return JsonResponse({'success': False, 'error': '규칙을 찾을 수 없습니다.'})
            if 'description' in data:
                rule.description = (data['description'] or '').strip()
            if 'is_active' in data:
                rule.is_active = bool(data['is_active'])
            if 'send_to_vendor' in data:
                rule.send_to_vendor = bool(data['send_to_vendor'])
            if 'send_to_requester' in data:
                rule.send_to_requester = bool(data['send_to_requester'])
            rule.save()
            if 'recipient_ids' in data:
                rule.recipients.set(data['recipient_ids'])
            return JsonResponse({'success': True, 'message': '알림 규칙 수정 완료'})

        elif action == 'delete':
            try:
                rule = NotificationRule.objects.get(id=data.get('id'))
            except NotificationRule.DoesNotExist:
                return JsonResponse({'success': False, 'error': '규칙을 찾을 수 없습니다.'})
            rule.delete()
            return JsonResponse({'success': True, 'message': '알림 규칙 삭제 완료'})

    return JsonResponse({'success': False, 'error': 'GET/POST만 허용'})


@login_required
@admin_required
def api_notification_logs(request):
    """알림 발송 이력 조회 API"""
    from django.utils import timezone
    logs = NotificationLog.objects.order_by('-created_at')[:100]
    data = [{
        'id': l.id,
        'event_type': l.event_type,
        'event_display': l.get_event_type_display(),
        'recipient_name': l.recipient_name or '-',
        'recipient_email': l.recipient_email,
        'subject': l.subject,
        'status': l.status,
        'status_display': l.get_status_display(),
        'error_message': l.error_message,
        'created_at': timezone.localtime(l.created_at).strftime('%Y-%m-%d %H:%M') if l.created_at else '-',
        'sent_at': timezone.localtime(l.sent_at).strftime('%Y-%m-%d %H:%M') if l.sent_at else '-',
    } for l in logs]
    return JsonResponse({'logs': data})
