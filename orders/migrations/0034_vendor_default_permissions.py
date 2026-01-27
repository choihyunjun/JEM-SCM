# Generated migration for vendor default permissions

from django.db import migrations, models


def set_vendor_default_permissions(apps, schema_editor):
    """기존 협력사 계정들에게 기본 권한 일괄 부여"""
    UserProfile = apps.get_model('orders', 'UserProfile')

    # role이 VENDOR이거나 account_type이 VENDOR인 계정 찾기
    vendor_profiles = UserProfile.objects.filter(
        models.Q(role='VENDOR') | models.Q(account_type='VENDOR')
    )

    updated = vendor_profiles.update(
        can_scm_order_view=True,
        can_scm_inventory_view=True,
        can_scm_label_view=True,
        can_scm_label_edit=True,
        can_qms_4m_view=True,
    )

    print(f"  → {updated}개 협력사 계정에 기본 권한 부여 완료")


def reverse_vendor_permissions(apps, schema_editor):
    """롤백: 권한 해제 (주의: 모든 협력사 권한이 해제됨)"""
    pass  # 롤백 시에는 아무것도 하지 않음 (수동 관리 필요)


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0033_permission_view_edit_split'),
    ]

    operations = [
        migrations.RunPython(set_vendor_default_permissions, reverse_vendor_permissions),
    ]
