"""
알림 발송 유틸리티
- NotificationRule에 설정된 수신자에게 이메일 발송
- 메시지 템플릿 변수 치환 지원
- NotificationLog에 이력 기록
"""
import logging
from django.core.mail import send_mail
from django.conf import settings

logger = logging.getLogger(__name__)

# 상태별 기본 메시지
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
    """
    이벤트 타입에 맞는 알림 규칙을 조회하여 이메일 발송

    Args:
        event_type: 이벤트 코드 (예: 'MOLD_REPAIR_REQUESTED')
        context_vars: 템플릿 변수 dict (part_no, mold_name, status 등)
        reference_id: 참조 ID
        requester_email: 의뢰자 이메일 (send_to_requester 용)
    """
    from .models import NotificationRule, NotificationLog

    if context_vars is None:
        context_vars = {}

    rules = NotificationRule.objects.filter(
        event_type=event_type, is_active=True
    ).prefetch_related('recipients')

    if not rules.exists():
        return 0

    # 기본 메시지
    defaults = DEFAULT_MESSAGES.get(event_type, {
        'subject': f'[JEM SCM] {event_type}',
        'title': event_type,
        'color': '#4a5568',
    })

    sent_count = 0

    for rule in rules:
        # 제목 결정: 커스텀 템플릿 > 기본
        if rule.subject_template:
            try:
                subject = rule.subject_template.format(**context_vars)
            except (KeyError, ValueError):
                subject = rule.subject_template
        else:
            try:
                subject = defaults['subject'].format(**context_vars)
            except (KeyError, ValueError):
                subject = defaults['subject']

        # 본문 결정: 커스텀 템플릿 > 기본 HTML
        if rule.body_template:
            try:
                body = f'<div style="font-family:sans-serif;max-width:560px;margin:0 auto;"><p>{rule.body_template.format(**context_vars)}</p></div>'
            except (KeyError, ValueError):
                body = f'<div style="font-family:sans-serif;"><p>{rule.body_template}</p></div>'
        else:
            body = build_email_body(defaults['title'], defaults['color'], context_vars)

        # 수신자 목록 수집
        email_list = []
        recipients_for_log = []

        # 등록된 내부 수신자
        for recipient in rule.recipients.filter(is_active=True):
            if recipient.email not in email_list:
                email_list.append(recipient.email)
                recipients_for_log.append(recipient)

        # 의뢰자에게 발송
        if rule.send_to_requester and requester_email:
            if requester_email not in email_list:
                email_list.append(requester_email)
                recipients_for_log.append(None)  # 로그에 수신자 없음

        # 발송
        for i, email in enumerate(email_list):
            recipient_obj = recipients_for_log[i] if i < len(recipients_for_log) else None

            log = NotificationLog.objects.create(
                event_type=event_type,
                recipient=recipient_obj,
                recipient_email=email,
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
                log.save(update_fields=['status', 'updated_at'])
                sent_count += 1
                logger.info(f'알림 발송 성공: {event_type} → {email}')
            except Exception as e:
                logger.error(f'알림 발송 실패 [{email}]: {e}')
                log.status = 'FAILED'
                log.error_message = str(e)
                log.save(update_fields=['status', 'error_message', 'updated_at'])

    return sent_count
