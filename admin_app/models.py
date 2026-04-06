from django.db import models
from django.contrib.auth.models import User


NOTIFICATION_EVENT_CHOICES = [
    # SCM
    ('ORDER_CREATED', '발주 등록'),
    ('ORDER_APPROVED', '발주 승인'),
    # QMS
    ('INSPECTION_CREATED', '수입검사 등록'),
    ('INSPECTION_PASSED', '수입검사 합격'),
    ('INSPECTION_FAILED', '수입검사 불합격'),
    ('M4_CREATED', '4M 변경 등록'),
    ('M4_APPROVED', '4M 승인'),
    ('M4_REJECTED', '4M 반려'),
    ('NC_CREATED', '부적합 등록'),
    ('CAPA_REQUESTED', 'CAPA 요청'),
    ('CAPA_OVERDUE', 'CAPA 기한 초과'),
    ('CLAIM_ISSUED', '클레임 발행'),
    ('ISIR_SUBMITTED', 'ISIR 등록'),
    ('ISIR_APPROVED', 'ISIR 승인'),
    ('ISIR_REJECTED', 'ISIR 반려'),
    ('OUTGOING_FAILED', '출하검사 불합격'),
    # WMS
    ('DELIVERY_RECEIVED', '납품서 접수'),
    ('LOW_STOCK', '재고 부족 경고'),
    ('VENDOR_DOWNGRADED', '협력사 등급 하락'),
]

RECIPIENT_TYPE_CHOICES = [
    ('INTERNAL', '내부 직원'),
    ('VENDOR', '협력사'),
]


class NotificationRecipient(models.Model):
    """알림 수신자"""
    name = models.CharField("이름", max_length=50)
    organization = models.CharField("소속", max_length=100, blank=True)
    position = models.CharField("직함", max_length=50, blank=True)
    email = models.EmailField("이메일")
    recipient_type = models.CharField("구분", max_length=10, choices=RECIPIENT_TYPE_CHOICES, default='INTERNAL')
    is_active = models.BooleanField("활성", default=True)
    created_at = models.DateTimeField("등록일", auto_now_add=True)

    class Meta:
        verbose_name = "알림 수신자"
        verbose_name_plural = "알림 수신자"
        ordering = ['organization', 'name']

    def __str__(self):
        org = f" ({self.organization})" if self.organization else ""
        return f"{self.name}{org}"


class NotificationRule(models.Model):
    """알림 규칙 (이벤트 × 수신자 매핑)"""
    event_type = models.CharField("이벤트", max_length=30, choices=NOTIFICATION_EVENT_CHOICES)
    recipients = models.ManyToManyField(NotificationRecipient, verbose_name="수신자", related_name='rules')
    is_active = models.BooleanField("활성", default=True)
    description = models.CharField("설명", max_length=200, blank=True)
    created_at = models.DateTimeField("등록일", auto_now_add=True)
    updated_at = models.DateTimeField("수정일", auto_now=True)

    class Meta:
        verbose_name = "알림 규칙"
        verbose_name_plural = "알림 규칙"
        ordering = ['event_type']

    def __str__(self):
        return f"{self.get_event_type_display()} -> {self.recipients.count()}명"


class NotificationLog(models.Model):
    """알림 발송 이력"""
    STATUS_CHOICES = [
        ('PENDING', '대기'),
        ('SENT', '발송완료'),
        ('FAILED', '발송실패'),
    ]

    event_type = models.CharField("이벤트", max_length=30, choices=NOTIFICATION_EVENT_CHOICES)
    recipient = models.ForeignKey(NotificationRecipient, on_delete=models.SET_NULL, null=True, verbose_name="수신자")
    recipient_email = models.EmailField("수신 이메일")
    subject = models.CharField("제목", max_length=300)
    body = models.TextField("내용", blank=True)
    status = models.CharField("상태", max_length=10, choices=STATUS_CHOICES, default='PENDING')
    error_message = models.TextField("오류 메시지", blank=True)
    reference_id = models.CharField("참조 ID", max_length=100, blank=True)
    created_at = models.DateTimeField("생성일", auto_now_add=True)
    sent_at = models.DateTimeField("발송일", null=True, blank=True)

    class Meta:
        verbose_name = "알림 발송 이력"
        verbose_name_plural = "알림 발송 이력"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['event_type', 'reference_id']),
            models.Index(fields=['status', 'created_at']),
        ]

    def __str__(self):
        return f"[{self.get_status_display()}] {self.subject}"
