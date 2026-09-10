import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('material', '0049_alter_materialstock_unique_together_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='materialtransaction',
            name='source_incoming',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='inspection_transfers', to='material.materialtransaction',
                verbose_name='원본 입고이력',
            ),
        ),
    ]
