from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages
from django.http import HttpResponseForbidden


def permission_required(*permissions, redirect_url=None, message=None):
    """
    사용자 권한을 체크하는 데코레이터

    사용법:
        @permission_required('can_view_orders')
        @permission_required('can_view_orders', 'can_register_orders')  # OR 조건
        @permission_required('can_view_orders', redirect_url='home', message='권한이 없습니다.')

    Args:
        *permissions: 확인할 권한 필드명 (하나라도 True면 통과)
        redirect_url: 권한 없을 때 리다이렉트할 URL name (None이면 403 반환)
        message: 리다이렉트 시 보여줄 메시지
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            user = request.user

            # 비로그인 사용자
            if not user.is_authenticated:
                if redirect_url:
                    return redirect('login')
                return HttpResponseForbidden("로그인이 필요합니다.")

            # superuser는 모든 권한 통과
            if user.is_superuser:
                return view_func(request, *args, **kwargs)

            # 프로필이 없는 경우
            if not hasattr(user, 'profile'):
                if redirect_url:
                    messages.error(request, message or '사용자 프로필이 없습니다.')
                    return redirect(redirect_url)
                return HttpResponseForbidden("사용자 프로필이 없습니다.")

            # 권한 체크 (OR 조건: 하나라도 True면 통과)
            profile = user.profile
            has_permission = any(getattr(profile, perm, False) for perm in permissions)

            if has_permission:
                return view_func(request, *args, **kwargs)

            # 권한 없음
            if redirect_url:
                messages.error(request, message or '접근 권한이 없습니다.')
                return redirect(redirect_url)
            return HttpResponseForbidden("접근 권한이 없습니다.")

        return _wrapped_view
    return decorator


def scm_permission_required(*permissions):
    """SCM 권한 체크 (권한 없으면 홈으로 리다이렉트)"""
    return permission_required(*permissions, redirect_url='home', message='SCM 접근 권한이 없습니다.')


def wms_permission_required(*permissions):
    """WMS 권한 체크 (권한 없으면 홈으로 리다이렉트)"""
    return permission_required(*permissions, redirect_url='home', message='WMS 접근 권한이 없습니다.')


def qms_permission_required(*permissions):
    """QMS 권한 체크 (권한 없으면 홈으로 리다이렉트)"""
    return permission_required(*permissions, redirect_url='home', message='QMS 접근 권한이 없습니다.')


def admin_required(view_func):
    """
    관리자(superuser 또는 ADMIN 역할) 전용 데코레이터
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        user = request.user

        if not user.is_authenticated:
            return redirect('login')

        if user.is_superuser:
            return view_func(request, *args, **kwargs)

        if hasattr(user, 'profile') and user.profile.role == 'ADMIN':
            return view_func(request, *args, **kwargs)

        messages.error(request, '관리자 권한이 필요합니다.')
        return redirect('home')

    return _wrapped_view
