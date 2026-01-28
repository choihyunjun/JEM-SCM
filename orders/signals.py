from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import UserProfile, LoginLog


@receiver(post_save, sender=User)
def ensure_user_profile(sender, instance: User, created: bool, **kwargs):
    """User 저장 시 UserProfile이 항상 존재하도록 보장"""
    if created:
        UserProfile.objects.create(user=instance)
    else:
        UserProfile.objects.get_or_create(user=instance)


@receiver(user_logged_in)
def log_user_login(sender, request, user, **kwargs):
    """로그인 시 로그 기록"""
    # IP 주소 추출
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR')

    # User-Agent 추출
    user_agent = request.META.get('HTTP_USER_AGENT', '')[:500]

    LoginLog.objects.create(
        user=user,
        ip_address=ip,
        user_agent=user_agent
    )
