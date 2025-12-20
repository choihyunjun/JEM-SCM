from django import template

register = template.Library()

@register.filter(name='has_group')
def has_group(user, group_name):
    """
    사용자가 특정 그룹에 속해 있는지 확인하는 커스텀 필터
    """
    return user.groups.filter(name=group_name).exists()