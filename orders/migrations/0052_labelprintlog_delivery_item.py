import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('orders', '0051_alter_deliveryorder_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='labelprintlog',
            name='delivery_item',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='label_logs', to='orders.deliveryorderitem',
                verbose_name='원본 납품서 품목',
            ),
        ),
    ]
