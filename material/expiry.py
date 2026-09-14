"""Read-only expiry history, using transfers as the quantity source of truth."""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import MaterialTransaction, RawMaterialLabel


def expiry_movement_history(settings_map, search='', start='', end=''):
    transfers = MaterialTransaction.objects.filter(
        part_id__in=settings_map,
        transaction_type__in=['TRANSFER', 'TRF_ERP'],
    ).select_related('part', 'actor', 'warehouse_from', 'warehouse_to').prefetch_related(
        'used_labels', 'used_tags',
    )
    # Preserve older label-only records, but never count a linked label in addition
    # to its transaction (even when that transaction falls outside the filters).
    legacy_labels = RawMaterialLabel.objects.filter(
        part_id__in=settings_map, status='USED', used_at__isnull=False,
        used_transaction__isnull=True,
    ).select_related('part', 'used_by')

    if search:
        transfers = transfers.filter(
            Q(part__part_no__icontains=search) | Q(part__part_name__icontains=search)
        )
        legacy_labels = legacy_labels.filter(
            Q(part__part_no__icontains=search) | Q(part__part_name__icontains=search)
            | Q(part_no__icontains=search) | Q(part_name__icontains=search)
        )
    if start:
        transfers = transfers.filter(date__date__gte=start)
        legacy_labels = legacy_labels.filter(used_at__date__gte=start)
    if end:
        transfers = transfers.filter(date__date__lte=end)
        legacy_labels = legacy_labels.filter(used_at__date__lte=end)

    def expiry_fields(part_id, lot_no, used_at, saved_expiry=None):
        expiry_date = saved_expiry
        if expiry_date is None and lot_no is not None:
            expiry_date = lot_no + timedelta(days=settings_map[part_id].shelf_life_days)
        used_date = timezone.localdate(used_at) if timezone.is_aware(used_at) else used_at.date()
        return {
            'expiry_date': expiry_date,
            'used_d_day': (expiry_date - used_date).days if expiry_date else None,
        }

    rows = []
    for trx in transfers:
        rows.append({
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
            **expiry_fields(trx.part_id, trx.lot_no, trx.date),
        })
    for label in legacy_labels:
        rows.append({
            'used_at': label.used_at,
            'part_no': label.part_no,
            'part_name': label.part_name,
            'quantity': label.quantity,
            'unit_display': label.get_unit_display(),
            'lot_no': label.lot_no,
            'used_by': label.used_by,
            'source': '라벨 기록',
            'label_ids': [label.label_id],
            **expiry_fields(label.part_id, label.lot_no, label.used_at, label.expiry_date),
        })
    rows.sort(key=lambda row: row['used_at'], reverse=True)
    return rows
