"""Apply the same ownership boundary to lists, details, exports and mutations."""
from orders.access import scope_vendor_queryset, vendor_identity


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
        'ChangeRequest': ('vendor', True),
        'ApprovalStep': ('change_request__vendor', True),
        'VendorResponse': ('change_request__vendor', True),
        'ChangeDocument': ('change_request__vendor', True),
        'VOC': ('linked_vendor', True),
    }
    field, organization = paths[name]
    return scope_vendor_queryset(request.user, qs, field, organization)
