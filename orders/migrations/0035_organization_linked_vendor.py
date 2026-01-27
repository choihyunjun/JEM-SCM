# Generated migration for Organization-Vendor integration

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0034_vendor_default_permissions'),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='linked_vendor',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='organization',
                to='orders.vendor',
                verbose_name='연결된 협력사'
            ),
        ),
    ]
