from django.db import migrations


def seed_role_defaults(apps, schema_editor):
    """기존 하드코딩된 협력사 기본 권한을 RoleDefaultPermission 테이블에 시드"""
    RoleDefaultPermission = apps.get_model('orders', 'RoleDefaultPermission')

    # 협력사 기본 권한 (기존 하드코딩에서 이전)
    vendor_defaults = [
        'can_scm_order_view',
        'can_scm_inventory_view',
        'can_scm_label_view',
        'can_scm_label_edit',
        'can_qms_4m_view',
    ]
    for field in vendor_defaults:
        RoleDefaultPermission.objects.get_or_create(role='VENDOR', permission_field=field)

    # 직원 기본 권한
    staff_defaults = [
        'can_scm_order_view',
        'can_scm_incoming_view',
        'can_scm_inventory_view',
        'can_scm_label_view',
        'can_wms_stock_view',
        'can_wms_inout_view',
        'can_wms_bom_view',
        'can_qms_4m_view',
        'can_qms_inspection_view',
    ]
    for field in staff_defaults:
        RoleDefaultPermission.objects.get_or_create(role='STAFF', permission_field=field)


def reverse_seed(apps, schema_editor):
    RoleDefaultPermission = apps.get_model('orders', 'RoleDefaultPermission')
    RoleDefaultPermission.objects.filter(role__in=['VENDOR', 'STAFF']).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('orders', '0044_add_new_permissions_and_role_defaults'),
    ]

    operations = [
        migrations.RunPython(seed_role_defaults, reverse_seed),
    ]
