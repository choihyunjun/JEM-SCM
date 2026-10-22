"""Apply the same ownership boundary to lists, details, exports and mutations."""
from orders.access import has_permission, scope_vendor_queryset, vendor_identity

# QMS 메뉴 → 조회 권한. 좌측 메뉴 표시(templatetags)와 화면 권한(views 데코레이터)이
# 모두 이 표 하나만 본다. 등록/수정은 같은 이름의 *_edit 권한.
QMS_MENU_PERMISSIONS = {
    'm4': 'can_qms_4m_view',                  # 사전 4M · 정식 4M
    'inspection': 'can_qms_inspection_view',  # 수입검사 · 출하검사 · 성적서 · VOC
    'nc': 'can_qms_nc_view',                  # 부적합품 · 시정조치
    'claim': 'can_qms_claim_view',
    'rating': 'can_qms_rating_view',
    'isir': 'can_qms_isir_view',
    'gauge': 'can_qms_gauge_view',
    'qdoc': 'can_qms_doc_view',
}


def menu_permission(menu, edit=False):
    perm = QMS_MENU_PERMISSIONS[menu]
    return perm[:-len('_view')] + '_edit' if edit else perm


def can_open_menu(user, menu):
    return has_permission(user, menu_permission(menu))


def visible_menus(user):
    return {menu: has_permission(user, perm) for menu, perm in QMS_MENU_PERMISSIONS.items()}


def scoped(request, model):
    qs = model.objects.all()
    name = model.__name__
    if name == 'Organization':
        restricted, vendor, org = vendor_identity(request.user)
        return qs.filter(pk=org.pk) if restricted and org else (qs.none() if restricted else qs)
    paths = {
        'ImportInspection': ('inbound_transaction__vendor', False),
        'NonConformance': ('vendor', True),
        'CorrectiveAction': ('vendor', True),
        'VendorClaim': ('vendor', True),
        'VendorRating': ('vendor', True),
        'ISIR': ('vendor', True),
        'VOC': ('linked_vendor', True),
    }
    field, organization = paths[name]
    return scope_vendor_queryset(request.user, qs, field, organization)
