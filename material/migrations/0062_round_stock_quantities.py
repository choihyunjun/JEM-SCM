from django.db import migrations
from django.db.models import F
from django.db.models.functions import Round


def round_quantities(apps, schema_editor):
    # SQLite stored F() arithmetic results as REAL (e.g. 0.19999999999999998,
    # 1.1e-13). Snap them back to the 4-place decimal the application reads.
    apps.get_model('material', 'MaterialStock').objects.update(quantity=Round(F('quantity'), 4))
    apps.get_model('material', 'MaterialTransaction').objects.update(
        quantity=Round(F('quantity'), 4), result_stock=Round(F('result_stock'), 4),
    )


class Migration(migrations.Migration):
    dependencies = [('material', '0061_stock_decimal_quantities')]

    operations = [migrations.RunPython(round_quantities, migrations.RunPython.noop)]
