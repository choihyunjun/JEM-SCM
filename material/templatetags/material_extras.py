from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """딕셔너리에서 키로 값을 가져오는 필터"""
    if dictionary is None:
        return None
    return dictionary.get(key, 0)


@register.filter
def subtract(value, arg):
    """빼기 연산"""
    try:
        return value - arg
    except (ValueError, TypeError):
        return value
