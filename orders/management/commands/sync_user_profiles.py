from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from orders.models import UserProfile, Vendor, Organization


class Command(BaseCommand):
    help = "기존 User/UserProfile/Vendor 데이터를 기반으로 Organization + UserProfile(org/account_type) 정리"

    def add_arguments(self, parser):
        parser.add_argument(
            "--internal-org-name",
            default="진영전기",
            help="내부 조직명(기본: 진영전기)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="DB 반영 없이 로그만 출력",
        )

    def handle(self, *args, **options):
        internal_org_name = options["internal_org_name"]
        dry_run = options["dry_run"]

        internal_org, _ = Organization.objects.get_or_create(
            name=internal_org_name,
            defaults={"org_type": "INTERNAL"},
        )

        updated = 0
        created_org = 0

        for u in User.objects.all().iterator():
            profile, _ = UserProfile.objects.get_or_create(user=u)

            # 내부 판별(기존 필드 + Django 기본 플래그)
            is_internal = (
                u.is_superuser
                or u.is_staff
                or profile.is_jinyoung_staff
                or profile.role in ("ADMIN", "STAFF")
            )

            if is_internal:
                new_type = "INTERNAL"
                new_org = internal_org
            else:
                # 협력사 매핑(가능하면 Vendor.user 기반)
                v = Vendor.objects.filter(user=u).first()
                if v:
                    org, org_created = Organization.objects.get_or_create(
                        name=v.name,
                        defaults={"org_type": "VENDOR"},
                    )
                    if org_created:
                        created_org += 1
                    new_type = "VENDOR"
                    new_org = org
                else:
                    # 협력사인데 Vendor도 없는 경우: org는 비워둠(정책 확정 전 안전 처리)
                    new_type = "VENDOR"
                    new_org = None

            changed = False
            if profile.account_type != new_type:
                profile.account_type = new_type
                changed = True
            if profile.org_id != (new_org.id if new_org else None):
                profile.org = new_org
                changed = True

            if changed:
                updated += 1
                if not dry_run:
                    profile.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"완료: updated_profiles={updated}, created_org={created_org}, dry_run={dry_run}"
            )
        )
