"""
알림 발송 유틸리티
- NotificationRule에 설정된 수신자에게 이메일 발송
- NotificationLog에 이력 기록
"""
import logging
from django.core.mail import send_mail
from django.conf import settings

logger = logging.getLogger(__name__)


def send_notification(event_type, subject, body, reference_id=''):
    """
    이벤트 타입에 맞는 알림 규칙을 조회하여 이메일 발송

    Args:
        event_type: NOTIFICATION_EVENT_CHOICES의 코드 (예: 'MOLD_REPAIR_REQUESTED')
        subject: 이메일 제목
        body: 이메일 본문 (HTML)
        reference_id: 참조 ID (예: 수리의뢰 PK)
    """
    from .models import NotificationRule, NotificationLog

    rules = NotificationRule.objects.filter(
        event_type=event_type, is_active=True
    ).prefetch_related('recipients')

    if not rules.exists():
        return 0

    sent_count = 0

    for rule in rules:
        recipients = rule.recipients.filter(is_active=True)

        for recipient in recipients:
            log = NotificationLog.objects.create(
                event_type=event_type,
                recipient=recipient,
                recipient_email=recipient.email,
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
                    recipient_list=[recipient.email],
                    fail_silently=False,
                )
                log.status = 'SENT'
                log.save(update_fields=['status', 'updated_at'])
                sent_count += 1
            except Exception as e:
                logger.error(f'알림 발송 실패 [{recipient.email}]: {e}')
                log.status = 'FAILED'
                log.error_message = str(e)
                log.save(update_fields=['status', 'error_message', 'updated_at'])

    return sent_count
