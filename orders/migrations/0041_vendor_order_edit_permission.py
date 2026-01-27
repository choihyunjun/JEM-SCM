# Generated migration for vendor order edit permission

from django.db import migrations


def add_order_edit_to_vendors(apps, schema_editor):
    """협력사 계정들에게 발주확인 권한 부여"""
    UserProfile = apps.get_model('orders', 'UserProfile')
    Organization = apps.get_model('orders', 'Organization')

    # 협력사와 연결된 조직의 모든 사용자에게 권한 부여
    vendor_orgs = Organization.objects.filter(linked_vendor__isnull=False)
    updated_profiles = 0

    for org in vendor_orgs:
        count = UserProfile.objects.filter(org=org).update(
            can_scm_order_edit=True,
            can_view_orders=True,  # 엑셀 다운로드용 권한도 추가
        )
        updated_profiles += count

    # role이 VENDOR인 계정도 업데이트 (레거시 지원)
    legacy_count = UserProfile.objects.filter(role='VENDOR').update(
        can_scm_order_edit=True,
        can_view_orders=True,
    )

    print(f"  → 협력사 조직 {vendor_orgs.count()}개의 사용자 {updated_profiles}명에게 발주확인 권한 부여")
    print(f"  → 레거시 VENDOR role 계정 {legacy_count}명에게 발주확인 권한 부여")


def reverse_permission(apps, schema_editor):
    """롤백: 권한 해제 (주의: 모든 협력사 권한이 해제됨)"""
    pass  # 롤백 시에는 아무것도 하지 않음 (수동 관리 필요)


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0040_add_part_account_type_and_upload_log'),
    ]

    operations = [
        migrations.RunPython(add_order_edit_to_vendors, reverse_permission),
    ]
