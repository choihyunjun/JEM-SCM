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


@register.filter
def qms_menu(user, menu):
    """QMS 메뉴 표시 여부. 화면 권한과 같은 표(qms.access.QMS_MENU_PERMISSIONS)를 쓴다.
    사용법: {% if user|qms_menu:'gauge' %}"""
    from qms.access import can_open_menu
    return can_open_menu(user, menu)


@register.filter
def qms_any_menu(user, menus=''):
    """메뉴 중 하나라도 열 수 있으면 True. 인자 없으면 QMS 전체 메뉴 대상.
    사용법: {% if user|qms_any_menu %} / {% if user|qms_any_menu:'claim,rating' %}"""
    from qms.access import QMS_MENU_PERMISSIONS, can_open_menu
    keys = [m.strip() for m in menus.split(',') if m.strip()] or list(QMS_MENU_PERMISSIONS)
    return any(can_open_menu(user, m) for m in keys)


@register.filter
def qms_menu_edit(user, menu):
    """QMS 메뉴의 등록/수정 권한(예: 'ISIR 등록' 메뉴). 사용법: {% if user|qms_menu_edit:'isir' %}"""
    from orders.access import has_permission
    from qms.access import menu_permission
    return has_permission(user, menu_permission(menu, edit=True))
