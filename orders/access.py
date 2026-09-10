"""Explicit permissions and vendor ownership shared by views and templates."""
from django.core.exceptions import PermissionDenied

LEGACY_PERMISSIONS = {
    'can_view_orders': 'can_scm_order_view',
    'can_view_order': 'can_scm_order_view',
    'can_register_orders': 'can_scm_order_edit',
    'can_view_inventory': 'can_scm_inventory_view',
    'can_manage_incoming': 'can_scm_incoming_edit',
    'can_manage_parts': 'can_scm_admin',
    'can_view_reports': 'can_scm_report',
    'can_access_scm_admin': 'can_scm_admin',
}
VENDOR_BLOCKED = {
    'can_scm_order_edit', 'can_scm_incoming_edit', 'can_scm_admin', 'can_scm_report',
}


def has_permission(user, permission):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, 'profile', None)
    permission = LEGACY_PERMISSIONS.get(permission, permission)
    if not profile:
        return False
    if profile.role == 'VENDOR' and permission in VENDOR_BLOCKED:
        return False
    return bool(getattr(profile, permission, False))


def vendor_identity(user):
    """Return (restricted, vendor, organization); missing links never mean internal."""
    from .models import Vendor, Organization
    if user.is_superuser:
        return False, None, None
    profile = getattr(user, 'profile', None)
    if not profile:
        return True, None, None
    org = profile.org
    direct = Vendor.objects.filter(user=user).first()
    restricted = profile.role == 'VENDOR' or bool(org and org.org_type == 'VENDOR') or bool(direct)
    if not restricted:
        return False, None, None
    linked = org.linked_vendor if org and org.org_type == 'VENDOR' else None
    if direct and linked and direct.pk != linked.pk:
        return True, None, None
    vendor = linked or direct
    if not org or org.org_type != 'VENDOR':
        org = Organization.objects.filter(linked_vendor=vendor).first() if vendor else None
    return True, vendor, org


def scope_vendor_queryset(user, qs, field='vendor', organization=False):
    restricted, vendor, org = vendor_identity(user)
    if not restricted:
        return qs
    owner = org if organization else vendor
    return qs.filter(**{field: owner}) if owner else qs.none()


def validate_vendor_assignment(user, vendor_id):
    restricted, vendor, org = vendor_identity(user)
    if restricted and (not org or str(vendor_id) != str(org.pk)):
        raise PermissionDenied('다른 협력사의 자료를 등록하거나 변경할 수 없습니다.')
