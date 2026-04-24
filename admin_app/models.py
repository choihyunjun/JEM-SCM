from django.db import models
from django.contrib.auth.models import User


# 실제 트리거가 연결된 이벤트만 노출
ACTIVE_EVENT_CHOICES = [
    ('MOLD_REPAIR_REQUESTED', '금형 수리의뢰 등록'),
    ('MOLD_REPAIR_RECEIVED', '금형 수리 접수'),
    ('MOLD_REPAIR_IN_PROGRESS', '금형 수리 진행'),
    ('MOLD_REPAIR_COMPLETED', '금형 수리 완료'),
    ('ORDER_CREATED', '발주 등록'),
]

# 전체 이벤트 (하위 호환 + 향후 확장용, DB choices)
NOTIFICATION_EVENT_CHOICES = ACTIVE_EVENT_CHOICES + [
    ('ORDER_APPROVED', '발주 승인'),
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
    ('DELIVERY_RECEIVED', '납품서 접수'),
    ('LOW_STOCK', '재고 부족 경고'),
    ('VENDOR_DOWNGRADED', '협력사 등급 하락'),
]


class NotificationRule(models.Model):
    """알림 규칙 (이벤트 × 수신자 매핑) - 단순화 버전"""
    event_type = models.CharField("이벤트", max_length=30, choices=NOTIFICATION_EVENT_CHOICES, unique=True)
    recipients = models.ManyToManyField(User, verbose_name="내부 수신자",
        related_name='notification_rules', blank=True,
        limit_choices_to={'is_active': True})
    send_to_vendor = models.BooleanField("협력사 자동 발송", default=False)
    send_to_requester = models.BooleanField("의뢰자에게 발송", default=False)
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
    recipient_email = models.EmailField("수신 이메일")
    recipient_name = models.CharField("수신자명", max_length=100, blank=True)
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
