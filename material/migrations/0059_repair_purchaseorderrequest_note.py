from django.db import migrations


def restore_missing_note(apps, schema_editor):
    """Repair databases whose recorded migration state already contains note.

    Some installations have applied 0045 but lack this physical column. Use
    the historical field definition and the backend's schema editor so that
    existing rows receive the TextField's empty-string default.
    """
    model = apps.get_model('material', 'PurchaseOrderRequest')
    field = model._meta.get_field('note')
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, model._meta.db_table,
            )
        }
    if field.column not in columns:
        schema_editor.add_field(model, field)


class Migration(migrations.Migration):
    dependencies = [('material', '0058_scan_history_visibility')]

    # State already has note from 0045. Reversing this repair must retain the
    # column (and any notes), because the previous state also requires it.
    operations = [
        # Avoid RunPython's extra transaction around DDL on MySQL/Oracle.
        migrations.RunPython(restore_missing_note, migrations.RunPython.noop, atomic=False),
    ]
