"""
알림 발송 유틸리티 (단순화 버전)
- NotificationRule → User 직접 연결
- 기본 템플릿 자동 사용
"""
import logging
from django.core.mail import send_mail
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_MESSAGES = {
    'MOLD_REPAIR_REQUESTED': {
        'subject': '[금형 수리의뢰] {part_no} {mold_name}',
        'title': '금형 수리의뢰가 등록되었습니다',
        'color': '#553c9a',
    },
    'MOLD_REPAIR_RECEIVED': {
        'subject': '[금형 수리접수] {part_no} {mold_name}',
        'title': '수리의뢰가 접수되었습니다',
        'color': '#2f855a',
    },
    'MOLD_REPAIR_IN_PROGRESS': {
        'subject': '[금형 수리진행] {part_no} {mold_name}',
        'title': '수리가 진행 중입니다',
        'color': '#c05621',
    },
    'MOLD_REPAIR_COMPLETED': {
        'subject': '[금형 수리완료] {part_no} {mold_name}',
        'title': '수리가 완료되었습니다',
        'color': '#22543d',
    },
    'ORDER_CREATED': {
        'subject': '[발주 등록] {part_no}',
        'title': '발주가 등록되었습니다',
        'color': '#2b6cb0',
    },
}


def build_email_body(title, color, context_vars):
    """HTML 이메일 본문 생성"""
    rows = ''
    field_labels = {
        'part_no': '품번', 'mold_name': '금형명', 'item_group': '품목',
        'priority': '중요도', 'status': '상태', 'repair_types': '수리유형',
        'request_content': '의뢰내용', 'requester': '의뢰자',
        'repair_by': '수리담당', 'repair_content': '수리내용',
        'received_date': '접수일', 'expected_date': '완료예정일',
        'completed_date': '완료일',
    }
    display_fields = ['part_no', 'mold_name', 'item_group', 'priority', 'status',
                      'repair_types', 'request_content', 'requester',
                      'repair_by', 'repair_content', 'received_date',
                      'expected_date', 'completed_date']

    for field in display_fields:
        val = context_vars.get(field, '')
        if val and val != '-':
            label = field_labels.get(field, field)
            rows += f'<tr><td style="padding:8px 12px;background:#f8f9fa;font-weight:600;width:100px;border:1px solid #e5e7eb;font-size:13px;color:#374151;">{label}</td><td style="padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;">{val}</td></tr>'

    return f'''
    <div style="font-family:'Malgun Gothic',sans-serif;max-width:560px;margin:0 auto;">
        <div style="background:{color};color:#fff;padding:16px 20px;border-radius:8px 8px 0 0;">
            <h3 style="margin:0;font-size:16px;">{title}</h3>
        </div>
        <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:16px;">
            <table style="border-collapse:collapse;width:100%;">{rows}</table>
        </div>
        <p style="color:#9ca3af;font-size:11px;margin-top:12px;text-align:center;">JEM SCM 시스템에서 자동 발송된 메일입니다.</p>
    </div>
    '''


def send_notification(event_type, context_vars=None, reference_id='', requester_email=''):
    """이벤트 타입에 맞는 알림 규칙을 조회하여 이메일 발송"""
    from .models import NotificationRule, NotificationLog

    if context_vars is None:
        context_vars = {}

    try:
        rule = NotificationRule.objects.get(event_type=event_type, is_active=True)
    except NotificationRule.DoesNotExist:
        return 0

    # 기본 메시지
    defaults = DEFAULT_MESSAGES.get(event_type, {
        'subject': f'[JEM SCM] {event_type}',
        'title': event_type,
        'color': '#4a5568',
    })

    try:
        subject = defaults['subject'].format(**context_vars)
    except (KeyError, ValueError):
        subject = defaults['subject']

    body = build_email_body(defaults['title'], defaults['color'], context_vars)

    # 수신자 이메일 수집
    email_map = {}  # {email: name}

    # 1) 내부 수신자 (User)
    for user in rule.recipients.filter(is_active=True):
        if user.email:
            profile = getattr(user, 'profile', None)
            name = ''
            if profile:
                name = getattr(profile, 'display_name', '') or ''
            if not name:
                name = user.get_full_name() or user.username
            email_map[user.email] = name

    # 2) 의뢰자
    if rule.send_to_requester and requester_email:
        if requester_email not in email_map:
            email_map[requester_email] = '의뢰자'

    # 발송
    sent_count = 0
    for email, name in email_map.items():
        log = NotificationLog.objects.create(
            event_type=event_type,
            recipient_email=email,
            recipient_name=name,
            subject=subject,
            body=body,
            status='PENDING',
            reference_id=str(reference_id),
        )

        try:
            send_mail(
                subject=subject,
                message='',
                html_message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
                fail_silently=False,
            )
            log.status = 'SENT'
            log.save(update_fields=['status'])
            sent_count += 1
            logger.info(f'알림 발송 성공: {event_type} → {email}')
        except Exception as e:
            logger.error(f'알림 발송 실패 [{email}]: {e}')
            log.status = 'FAILED'
            log.error_message = str(e)
            log.save(update_fields=['status', 'error_message'])

    return sent_count
