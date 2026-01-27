# Data migration to link existing Vendors with Organizations

from django.db import migrations


def link_vendor_organization(apps, schema_editor):
    """기존 Vendor와 Organization을 이름으로 매칭하여 연결"""
    Vendor = apps.get_model('orders', 'Vendor')
    Organization = apps.get_model('orders', 'Organization')

    linked_count = 0
    created_count = 0

    for vendor in Vendor.objects.all():
        # 이름이 같은 Organization 찾기
        org = Organization.objects.filter(name=vendor.name, org_type='VENDOR').first()

        if org:
            # 기존 Organization에 linked_vendor 연결
            org.linked_vendor = vendor
            org.save()
            linked_count += 1
        else:
            # 매칭되는 Organization이 없으면 새로 생성
            Organization.objects.create(
                name=vendor.name,
                org_type='VENDOR',
                linked_vendor=vendor
            )
            created_count += 1

    print(f"  → 기존 연결: {linked_count}개, 신규 생성: {created_count}개")


def reverse_link(apps, schema_editor):
    """롤백: linked_vendor 연결 해제"""
    Organization = apps.get_model('orders', 'Organization')
    Organization.objects.filter(linked_vendor__isnull=False).update(linked_vendor=None)


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0035_organization_linked_vendor'),
    ]

    operations = [
        migrations.RunPython(link_vendor_organization, reverse_link),
    ]
