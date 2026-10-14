from django.db import migrations
from django.db.models import F


def link_history(apps, schema_editor):
    db = schema_editor.connection.alias
    Case = apps.get_model('orders', 'ReturnLog')
    Item = apps.get_model('orders', 'DeliveryOrderItem')
    Order = apps.get_model('orders', 'Order')
    Delivery = apps.get_model('orders', 'DeliveryOrder')
    Shipment = apps.get_model('orders', 'ReturnShipment')
    Receipt = apps.get_model('material', 'MaterialTransaction')
    Inspection = apps.get_model('qms', 'ImportInspection')

    Case.objects.using(db).filter(is_confirmed=True).update(legacy_confirmed_quantity=F('quantity'))
    for case in Case.objects.using(db).select_related('delivery_order', 'part').iterator():
        if not case.delivery_order_id:
            continue
        candidates = list(Inspection.objects.using(db).filter(
            inbound_transaction__ref_delivery_order=case.delivery_order.order_no,
            inbound_transaction__part_id=case.part_id, qty_bad=case.quantity,
            status__in=['APPROVED', 'REJECTED'],
        ).select_related('inbound_transaction')[:2])
        peers = Case.objects.using(db).filter(delivery_order_id=case.delivery_order_id,
                                              part_id=case.part_id, quantity=case.quantity)
        if len(candidates) != 1 or peers.count() != 1:
            continue
        origin = candidates[0].inbound_transaction
        if Case.objects.using(db).filter(source_incoming_id=origin.pk).exists():
            continue
        items = Item.objects.using(db).filter(order_id=case.delivery_order_id,
            part_no=case.part.part_no, lot_no=origin.lot_no)
        if origin.delivery_item_id:
            items = items.filter(pk=origin.delivery_item_id)
        items = list(items[:2])
        case.source_incoming_id = origin.pk
        if len(items) == 1:
            item = items[0]
            case.delivery_item_id = item.pk
            if item.erp_order_no and item.erp_order_seq and not item.linked_order_id:
                orders = list(Order.objects.using(db).filter(part_no=item.part_no,
                    vendor_id=case.part.vendor_id, erp_order_no=item.erp_order_no,
                    erp_order_seq=item.erp_order_seq)[:2])
                if len(orders) == 1:
                    item.linked_order_id = orders[0].pk
                    item.save(using=db, update_fields=['linked_order'])
        case.save(using=db, update_fields=['source_incoming', 'delivery_item'])

        # 명확한 원본 FK가 있는 반출만 연결하며 품번·수량만으로 추정하지 않는다.
        movements = list(Receipt.objects.using(db).filter(transaction_type='OUT_RETURN',
            source_incoming_id=origin.pk, part_id=case.part_id, lot_no=origin.lot_no,
            production_lot=origin.production_lot, return_shipment__isnull=True))
        total = sum(m.quantity for m in movements)
        if not total or total > case.quantity or any(m.quantity <= 0 for m in movements):
            continue
        for movement in movements:
            Shipment.objects.using(db).create(return_log_id=case.pk, movement_id=movement.pk,
                request_key=f'legacy:{movement.pk}',
                confirmed_at=(case.confirmed_at or case.created_at) if case.is_confirmed else None)
        if case.is_confirmed:
            case.legacy_confirmed_quantity -= total
            case.save(using=db, update_fields=['legacy_confirmed_quantity'])

    # 과거 수기 입고에는 반출 대상이 만들어지지 않았으므로 검사 기록에서 보완한다.
    for inspection in Inspection.objects.using(db).filter(qty_bad__gt=0,
            status__in=['APPROVED', 'REJECTED']).select_related('inbound_transaction').iterator():
        origin = inspection.inbound_transaction
        if origin.ref_delivery_order or Case.objects.using(db).filter(source_incoming_id=origin.pk).exists():
            continue
        case = Case.objects.using(db).create(source_incoming_id=origin.pk, part_id=origin.part_id,
            quantity=inspection.qty_bad, reason=(inspection.remark or '')[:255])
        # 과거 반출을 새 판정 이후로 오인하지 않도록 판정일 보존.
        Case.objects.using(db).filter(pk=case.pk).update(created_at=inspection.inspected_at or origin.date)

    # 전량 부적합을 미입고로 되돌렸던 과거 상태도 실제 입고/검사 결과로 복구한다.
    for delivery in Delivery.objects.using(db).all().iterator():
        receipts = Receipt.objects.using(db).filter(ref_delivery_order=delivery.order_no,
            transaction_type__in=['IN_SCM', 'IN_MANUAL', 'IN_ERP'])
        if not receipts.exists():
            continue
        inspections = Inspection.objects.using(db).filter(inbound_transaction__in=receipts)
        if inspections.filter(status='PENDING').exists() or receipts.count() < Item.objects.using(db).filter(order_id=delivery.pk).count():
            status = 'RECEIVED'
        elif inspections.filter(qty_good__gt=0).exists() or receipts.filter(inspection__isnull=True).exists():
            status = 'APPROVED'
        else:
            status = 'REJECTED'
        Delivery.objects.using(db).filter(pk=delivery.pk).update(status=status, is_received=True)


class Migration(migrations.Migration):
    dependencies = [('orders', '0053_returnlog_delivery_item_and_more'),
                    ('qms', '0035_importinspection_correction_history')]
    operations = [migrations.RunPython(link_history, migrations.RunPython.noop)]
