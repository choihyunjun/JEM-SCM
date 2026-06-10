from django import template

register = template.Library()

# VENDOR 계정이 절대 가질 수 없는 권한 목록 (편집/관리 계열)
_VENDOR_BLOCKED_PERMS = frozenset({
    'can_scm_order_edit', 'can_scm_incoming_edit', 'can_scm_admin', 'can_scm_report',
    'can_register_orders', 'can_manage_incoming', 'can_manage_parts',
    'can_view_reports', 'can_access_scm_admin',
})

@register.filter(name='has_group')
def has_group(user, group_name):
    """
    사용자가 특정 그룹에 속해 있는지 확인하는 커스텀 필터
    """
    return user.groups.filter(name=group_name).exists()


@register.filter(name='getattribute')
def getattribute(obj, attr):
    """
    객체에서 동적으로 속성값을 가져오는 필터
    사용법: {{ object|getattribute:'field_name' }}
    """
    if obj is None:
        return False
    return getattr(obj, attr, False)


@register.filter(name='has_perm')
def has_perm(user, perm_name):
    """
    사용자가 특정 권한을 가지고 있는지 안전하게 확인
    사용법: {{ user|has_perm:'can_scm_order_edit' }}

    - superuser는 항상 True
    - VENDOR 역할은 편집/관리 권한 불가
    - profile이 없으면 False
    - 권한 필드가 없으면 False
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, 'profile', None)
    if not profile:
        return False
    if getattr(profile, 'role', None) == 'VENDOR' and perm_name in _VENDOR_BLOCKED_PERMS:
        return False
    return getattr(profile, perm_name, False)


@register.filter(name='in_list')
def in_list(value, arg):
    """
    값이 콤마 구분 리스트에 정확히 포함되는지 체크 (substring 아닌 exact match)
    사용법: {{ url_name|in_list:'dashboard,home,list' }}
    """
    return str(value) in [x.strip() for x in arg.split(',')]
