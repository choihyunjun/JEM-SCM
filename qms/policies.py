"""QMS 권한/스코프 정책 레이어

목표:
- 내부/협력사 구분을 UserProfile(account_type/org)로 단일화
- 협력사는 자기 org 대상 문서만 접근
- 뷰에서 분기 로직을 흩뿌리지 않고 여기서 통일

NOTE(패치):
- admin/superuser/staff 계정은 UserProfile이 없어도 '내부'로 취급
- UserProfile related_name이 프로젝트마다 달라질 수 있어(profile/userprofile) 안전하게 조회
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from django.contrib.auth.models import User
from django.db import models
from django.db.models import QuerySet

from orders.models import UserProfile
from .models import M4Request, M4Review, Formal4MRequest


@dataclass(frozen=True)
class Actor:
    user: User
    profile: Optional[UserProfile]

    @property
    def is_internal(self) -> bool:
        """내부 사용자 판별.

        설계 의도:
        - 내부 사용자는 협력사를 선택해 사전 4M을 기안/진행시킨다.
        - 협력사는 내부가 지정한 대상 문서에 참여(회신/증빙 업로드)만 한다.

        현실적인 운영 이슈:
        - UserProfile.account_type 값이 초기 세팅/마이그레이션 과정에서 꼬일 수 있어
          role(ADMIN/STAFF) 또는 is_jinyoung_staff 를 함께 내부 판정에 포함한다.
        """
        # UserProfile이 없어도, Django 기본 플래그가 있으면 내부로 간주
        if getattr(self.user, "is_superuser", False) or getattr(self.user, "is_staff", False):
            return True

        if not self.profile:
            return False

        role = getattr(self.profile, "role", None)

        return bool(
            (self.profile.account_type == "INTERNAL" and role != "VENDOR")
            or getattr(self.profile, "is_jinyoung_staff", False)
            or role in ("ADMIN", "STAFF")
        )

    @property
    def is_vendor(self) -> bool:
        # 내부로 판정되는 계정은 협력사로 취급하지 않는다.
        if self.is_internal:
            return False
        if not self.profile:
            return False
        return bool(
            self.profile.account_type == "VENDOR" or getattr(self.profile, "role", None) == "VENDOR"
        )


def _safe_get_profile(user: User) -> Optional[UserProfile]:
    """UserProfile을 안전하게 가져온다.

    OneToOneField related_name이 profile/userprofile 등으로 달라질 수 있어 순차적으로 시도.
    접근 시 DoesNotExist 예외가 날 수 있어 try/except 처리.
    """
    for attr in ("profile", "userprofile"):
        try:
            prof = getattr(user, attr)
        except UserProfile.DoesNotExist:
            prof = None
        except Exception:
            prof = None
        if prof is not None:
            return prof

    # 마지막 안전망: DB에서 직접 조회
    try:
        return UserProfile.objects.filter(user=user).first()
    except Exception:
        return None


def get_actor(user: User) -> Actor:
    """항상 안전하게 profile을 가져온다."""
    profile = _safe_get_profile(user)
    return Actor(user=user, profile=profile)


def scope_m4_queryset(actor: Actor, qs: QuerySet[M4Request]) -> QuerySet[M4Request]:
    """목록/검색에서 보여줄 범위를 제한한다."""
    if actor.is_internal:
        return qs

    # 협력사: 자기 org에 해당하는 문서 또는 본인이 작성한 문서만
    if actor.is_vendor and actor.profile and actor.profile.org_id:
        return qs.filter(models.Q(vendor_org_id=actor.profile.org_id) | models.Q(user_id=actor.user.id))

    # profile/org가 없는 협력사 계정은 안전하게 본인 작성 건만
    return qs.filter(user_id=actor.user.id)


def can_view_m4(actor: Actor, item: M4Request) -> bool:
    if actor.is_internal:
        return True
    if actor.is_vendor and actor.profile and actor.profile.org_id:
        return (item.vendor_org_id == actor.profile.org_id) or (item.user_id == actor.user.id)
    return item.user_id == actor.user.id


def can_edit_m4(actor: Actor, item: M4Request) -> bool:
    """M4Request 수정 권한.

    기존 로직(기안자/결재자 등)은 유지하되, 협력사는 자기 스코프 안에서만 동작.
    """
    if not can_view_m4(actor, item):
        return False

    # 승인 완료는 수정 불가(기존 로직 유지)
    if item.status == "APPROVED":
        return False

    if item.user_id == actor.user.id:
        return True
    if item.status == "PENDING_REVIEW" and item.reviewer_user_id == actor.user.id:
        return True
    if item.status == "PENDING_REVIEW2" and getattr(item, "reviewer_user2_id", None) == actor.user.id:
        return True
    if item.status == "PENDING_APPROVE" and item.approver_user_id == actor.user.id:
        return True
    if item.status == "REJECTED" and item.user_id == actor.user.id:
        return True
    return False


def can_add_internal_review(actor: Actor) -> bool:
    """사내 검토 요청 발송은 내부 사용자만."""
    return actor.is_internal


def can_vendor_respond(actor: Actor, item: M4Request) -> bool:
    """협력사 답변(회신/증빙업로드) 가능 여부."""
    if not actor.is_vendor or not actor.profile or not actor.profile.org_id:
        return False
    return item.vendor_org_id == actor.profile.org_id


def get_or_create_vendor_review(actor: Actor, item: M4Request) -> M4Review:
    """협력사 회신용 Review row를 확보한다.

    - department: 협력사명
    - reviewer_name: 프로필 display_name이 있으면 사용
    """
    dept = actor.profile.org.name if actor.profile and actor.profile.org else "협력사"
    reviewer_name = actor.profile.display_name if actor.profile else ""
    review, _ = M4Review.objects.get_or_create(
        request=item,
        department=dept,
        defaults={
            "reviewer_name": reviewer_name,
            "reviewer": actor.user,
        },
    )
    return review


# --- 정식 4M(Formal4M) 스코프/권한 ---

def scope_formal4m_queryset(actor: Actor, qs: QuerySet[Formal4MRequest]) -> QuerySet[Formal4MRequest]:
    if actor.is_internal:
        return qs
    if actor.is_vendor and actor.profile and actor.profile.org_id:
        return qs.filter(pre_request__vendor_org_id=actor.profile.org_id)
    return qs.none()


def can_view_formal4m(actor: Actor, formal: Formal4MRequest) -> bool:
    if actor.is_internal:
        return True
    if actor.is_vendor and actor.profile and actor.profile.org_id:
        return formal.pre_request.vendor_org_id == actor.profile.org_id
    return False
