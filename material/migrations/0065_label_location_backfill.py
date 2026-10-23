"""기존 라벨의 창고를 채우고, 파렛트 라벨을 쓰던 품목은 라벨 단위를 '파렛트'로 둔다."""
from django.db import migrations
from django.db.models import Sum


def forwards(apps, schema_editor):
    RawMaterialLabel = apps.get_model('material', 'RawMaterialLabel')
    RawMaterialSetting = apps.get_model('material', 'RawMaterialSetting')
    MaterialStock = apps.get_model('material', 'MaterialStock')
    Warehouse = apps.get_model('material', 'Warehouse')

    # 살아 있는 라벨: 그 LOT 재고가 있는 창고 (원재료창고 3200 우선, 없으면 가장 많이 가진 창고)
    raw_store = Warehouse.objects.filter(code='3200').first()
    for label in RawMaterialLabel.objects.filter(status__in=['PRINTED', 'INSTOCK'], warehouse__isnull=True):
        holders = list(MaterialStock.objects.filter(part_id=label.part_id, lot_no=label.lot_no, quantity__gt=0)
                       .values('warehouse_id').annotate(t=Sum('quantity')).order_by('-t'))
        ids = [h['warehouse_id'] for h in holders]
        target = raw_store.pk if raw_store and raw_store.pk in ids else (ids[0] if ids else (raw_store.pk if raw_store else None))
        if target:
            RawMaterialLabel.objects.filter(pk=label.pk).update(warehouse_id=target)
    # 예전 방식의 '사용완료'는 실제로는 '스캔으로 옮김'이었다: 옮긴 창고에 재고로 되돌리고,
    # 실제 소진 여부는 이후 재고 맞추기(sync_labels)가 재고와 비교해서 정한다.
    for label in RawMaterialLabel.objects.filter(used_transaction__isnull=False, status__in=['USED', 'PRINTED', 'INSTOCK']):
        to_id = label.used_transaction.warehouse_to_id
        if to_id:
            RawMaterialLabel.objects.filter(pk=label.pk).update(warehouse_id=to_id, status='INSTOCK')

    pallet_parts = RawMaterialLabel.objects.filter(label_type='PALLET').values_list('part_id', flat=True).distinct()
    RawMaterialSetting.objects.filter(part_id__in=list(pallet_parts)).update(label_unit='PALLET')


class Migration(migrations.Migration):
    dependencies = [('material', '0064_label_location_and_unit')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
