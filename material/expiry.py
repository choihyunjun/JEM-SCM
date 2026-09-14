"""Read-only expiry history, using transfers as the quantity source of truth."""
from datetime import timedelta

from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from .models import MaterialTransaction, MovementExpiryEvent, MovementVisibilityEvent


def expiry_transfers():
    return MaterialTransaction.objects.filter(
        Q(warehouse_from__code='4200', warehouse_to__code='4300')
        | Q(warehouse_from__code='3200', warehouse_to__code='3000'),
        transaction_type__in=['TRANSFER', 'TRF_ERP'],
        part__raw_material_setting__isnull=False,
    )


def can_edit_movement_expiry(user):
    from orders.access import has_permission
    profile = getattr(user, 'profile', None)
    return has_permission(user, 'can_wms_storage_expiry') and (
        user.is_superuser or getattr(profile, 'role', None) == 'ADMIN'
    )


def calculate_movement_expiry(reference_date, shelf_life_days):
    if reference_date is None:
        return None
    try:
        return reference_date + timedelta(days=shelf_life_days)
    except OverflowError:
        return None


def expiry_movement_history(settings_map, search='', start='', end='', include_hidden=False):
    latest_event = MovementExpiryEvent.objects.filter(movement_id=OuterRef('pk')).order_by('-pk')
    latest_visibility = MovementVisibilityEvent.objects.filter(movement_id=OuterRef('pk')).order_by('-pk')
    transfers = expiry_transfers().filter(part_id__in=settings_map).annotate(
        manufacturing_date=Subquery(latest_event.values('manufacturing_date')[:1]),
        expiry_revision=Subquery(latest_event.values('pk')[:1]),
        history_hidden=Subquery(latest_visibility.values('hidden')[:1]),
        visibility_revision=Subquery(latest_visibility.values('pk')[:1]),
    ).select_related('part', 'actor', 'warehouse_from', 'warehouse_to').prefetch_related(
        'used_labels', 'used_tags',
    )
    # Label-only records have no reliable route, so they cannot match this view.
    if not include_hidden:
        transfers = transfers.filter(Q(history_hidden=False) | Q(history_hidden__isnull=True))

    if search:
        transfers = transfers.filter(
            Q(part__part_no__icontains=search) | Q(part__part_name__icontains=search)
        )
    if start:
        transfers = transfers.filter(date__date__gte=start)
    if end:
        transfers = transfers.filter(date__date__lte=end)

    def expiry_fields(part_id, lot_no, used_at, manufacturing_date):
        reference_date = lot_no if lot_no is not None else manufacturing_date
        expiry_date = calculate_movement_expiry(reference_date, settings_map[part_id].shelf_life_days)
        used_date = timezone.localdate(used_at) if timezone.is_aware(used_at) else used_at.date()
        return {
            'expiry_date': expiry_date,
            'reference_date': reference_date,
            'used_d_day': (expiry_date - used_date).days if expiry_date else None,
        }

    rows = []
    for trx in transfers:
        rows.append({
            'id': trx.pk,
            'history_hidden': bool(trx.history_hidden),
            'visibility_revision': trx.visibility_revision or 0,
            'expiry_revision': trx.expiry_revision or 0,
            'expiry_editable': trx.lot_no is None,
            'expiry_manual': trx.lot_no is None and trx.manufacturing_date is not None,
            'manufacturing_date': trx.manufacturing_date,
            'used_at': trx.date,
            'part_no': trx.part.part_no,
            'part_name': trx.part.part_name,
            'quantity': trx.quantity,
            # Transactions do not store a unit; do not infer kg from a label.
            'unit_display': '',
            'lot_no': trx.lot_no,
            'production_lot': trx.production_lot,
            'used_by': trx.actor,
            'source': 'ERP' if trx.transaction_type == 'TRF_ERP' else 'SCM',
            'transaction_no': trx.transaction_no,
            'warehouse_from': trx.warehouse_from,
            'warehouse_to': trx.warehouse_to,
            'label_ids': [label.label_id for label in trx.used_labels.all()]
                         + [tag.tag_id for tag in trx.used_tags.all()],
            **expiry_fields(trx.part_id, trx.lot_no, trx.date, trx.manufacturing_date),
        })
    rows.sort(key=lambda row: row['used_at'], reverse=True)
    return rows
