from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('material', '0053_movementvisibilityevent')]

    # Existing entered dates are manufacturing dates. Rename without altering
    # values, revisions, cleared entries, or audit history.
    operations = [
        migrations.RenameField('movementexpiryevent', 'previous_date', 'previous_manufacturing_date'),
        migrations.RenameField('movementexpiryevent', 'expiry_date', 'manufacturing_date'),
        migrations.AlterField('movementexpiryevent', 'previous_manufacturing_date',
                              models.DateField('이전 제조일', null=True, blank=True)),
        migrations.AlterField('movementexpiryevent', 'manufacturing_date',
                              models.DateField('지정 제조일', null=True, blank=True)),
    ]
