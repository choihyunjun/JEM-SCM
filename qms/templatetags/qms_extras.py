from django import template

register = template.Library()


@register.filter
def getattribute(obj, attr):
    """
    객체에서 동적으로 속성을 가져오는 필터
    사용법: {{ obj|getattribute:"attr_name" }}
    """
    if obj is None:
        return None

    # attr이 튜플/리스트의 인덱스로 접근하는 경우 (예: elem.4)
    if isinstance(attr, int):
        try:
            return obj[attr]
        except (IndexError, KeyError, TypeError):
            return None

    # 문자열 속성명인 경우
    try:
        return getattr(obj, str(attr), None)
    except (AttributeError, TypeError):
        return None
