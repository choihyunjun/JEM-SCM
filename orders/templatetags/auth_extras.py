from django import template

register = template.Library()

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
    return getattr(profile, perm_name, False)


@register.filter(name='in_list')
def in_list(value, arg):
    """
    값이 콤마 구분 리스트에 정확히 포함되는지 체크 (substring 아닌 exact match)
    사용법: {{ url_name|in_list:'dashboard,home,list' }}
    """
    return str(value) in [x.strip() for x in arg.split(',')]