"""orders 앱 데이터 정합성 보정용 유틸.

배경
-----
사전 4M에서 협력사 선택은 Organization(org_type=VENDOR)을 기준으로 노출된다.
그런데 기존 데이터가 Vendor 테이블만 채워져 있고 Organization 테이블이 비어있으면
폼에서 협력사 리스트가 비어 보일 수 있다.

원칙
-----
- 운영 데이터에 영향(부작용)을 최소화하기 위해 '필요할 때만' 보정한다.
- Organization(VENDOR)이 하나도 없을 때에 한해서만 생성/매핑을 시도한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from django.contrib.auth.models import User

from .models import Organization, UserProfile, Vendor


@dataclass
class SyncResult:
    created_org: int = 0
    updated_profiles: int = 0


def _is_internal_user(u: User, profile: Optional[UserProfile]) -> bool:
    if getattr(u, "is_superuser", False) or getattr(u, "is_staff", False):
        return True
    if not profile:
        return False
    return bool(profile.is_jinyoung_staff or profile.role in ("ADMIN", "STAFF"))


def ensure_org_and_profile_sync(internal_org_name: str = "진영전기") -> SyncResult:
    """Organization/Vendor/UserProfile 간 최소 정합성을 보정한다.

    - Organization(VENDOR)이 하나도 없을 때만 실행한다.
    - Vendor.name을 기반으로 Organization(VENDOR)을 생성한다.
    - Vendor.user가 연결된 경우 해당 UserProfile에 org/account_type을 매핑한다.
    - 내부 사용자로 판정되는 계정은 INTERNAL + 내부 org로 보정한다.
    """

    # 이미 Organization(VENDOR)이 존재하면 아무 것도 하지 않는다.
    if Organization.objects.filter(org_type="VENDOR").exists():
        return SyncResult()

    internal_org, _ = Organization.objects.get_or_create(
        name=internal_org_name,
        defaults={"org_type": "INTERNAL"},
    )

    result = SyncResult()

    # 1) Vendor -> Organization 생성
    for v in Vendor.objects.all().iterator():
        org, created = Organization.objects.get_or_create(
            name=v.name,
            defaults={"org_type": "VENDOR"},
        )
        if created:
            result.created_org += 1

        # 2) Vendor.user 연결이 있으면 프로필 org/account_type 매핑
        if v.user_id:
            u = v.user
            profile, _ = UserProfile.objects.get_or_create(user=u)

            if _is_internal_user(u, profile):
                new_type = "INTERNAL"
                new_org = internal_org
            else:
                new_type = "VENDOR"
                new_org = org

            changed = False
            if profile.account_type != new_type:
                profile.account_type = new_type
                changed = True
            if profile.org_id != (new_org.id if new_org else None):
                profile.org = new_org
                changed = True

            if changed:
                profile.save()
                result.updated_profiles += 1

    # 3) 내부 사용자로 판정되는 계정은 org/account_type을 내부로 보정
    for u in User.objects.all().iterator():
        profile, _ = UserProfile.objects.get_or_create(user=u)
        if not _is_internal_user(u, profile):
            continue

        changed = False
        if profile.account_type != "INTERNAL":
            profile.account_type = "INTERNAL"
            changed = True
        if profile.org_id != internal_org.id:
            profile.org = internal_org
            changed = True
        if changed:
            profile.save()
            result.updated_profiles += 1

    return result
