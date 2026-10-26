"""1000 수입검사 대기장의 가비지 정리: 품목별 합계가 0인 행(+850/-850 같은 상쇄 짝)과 수량 0 행을 지운다.
합계가 0인 것만 지우므로 재고 숫자는 바뀌지 않는다. 수불 이력은 건드리지 않는다."""
from django.db import migrations
from django.db.models import Sum


def forwards(apps, schema_editor):
    MaterialStock = apps.get_model('material', 'MaterialStock')
    rows = MaterialStock.objects.filter(warehouse__code='1000')
    for r in rows.values('part_id').annotate(t=Sum('quantity')):
        if abs(r['t'] or 0) < 0.00005:
            rows.filter(part_id=r['part_id']).delete()
    rows.filter(quantity=0).delete()


class Migration(migrations.Migration):
    dependencies = [('material', '0065_label_location_backfill')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
