from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('material', '0060_erp_stock_move_operation')]

    operations = [
        migrations.AlterField(
            model_name='materialstock', name='quantity',
            field=models.DecimalField('현재고', max_digits=20, decimal_places=4, default=0),
        ),
        migrations.AlterField(
            model_name='materialtransaction', name='quantity',
            field=models.DecimalField('변동수량', max_digits=20, decimal_places=4),
        ),
        migrations.AlterField(
            model_name='materialtransaction', name='result_stock',
            field=models.DecimalField('변동후 잔량', max_digits=20, decimal_places=4, default=0),
        ),
    ]
