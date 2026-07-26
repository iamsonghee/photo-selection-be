"""
클로즈드 베타 사용자 등급(관리자/베타/일반) 판정 및 이용량 한도.

정책 상수의 단일 소스는 런타임(Node/Python)별로 하나씩이다 — 두 서비스가 별도 배포라
코드 레벨로 공유할 방법이 없다. FE 쪽 단일 소스는 photo-selection-fe/src/lib/beta-limits.ts +
src/lib/beta-policy.ts이고, 이 파일이 BE 쪽 단일 소스다. 두 파일의 값은 서로 동일하게 유지해야 한다.

ADMIN_EMAILS는 photo-selection-fe/src/lib/admin-auth.ts의 ADMIN_EMAILS와 반드시 같은 값을 유지할 것.
"""
from datetime import date
from typing import Optional
from uuid import UUID

ADMIN_EMAILS = ["realsong88@gmail.com"]

# 베타 사용자 기본 한도(override 없음, 전원 동일)
BETA_MAX_PHOTOS_PER_PROJECT = 2000

# 일반(Trial) 사용자 한도
GENERAL_MAX_PHOTOS_PER_PROJECT = 500


def _is_beta_active(beta_status: Optional[str], beta_end_date: Optional[str]) -> bool:
    if beta_status != "active":
        return False
    if not beta_end_date:
        return True
    try:
        end = date.fromisoformat(beta_end_date)
    except ValueError:
        return True
    return end >= date.today()


def get_max_photos_per_project(supabase, photographer_id: UUID) -> Optional[int]:
    """해당 작가의 등급별 프로젝트당 사진 업로드 한도. None이면 무제한(관리자)."""
    r = (
        supabase.table("photographers")
        .select("email, beta_status, beta_end_date")
        .eq("id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not r.data:
        # 조회 실패 시 가장 보수적인(일반 사용자) 한도로 폴백
        return GENERAL_MAX_PHOTOS_PER_PROJECT

    row = r.data[0]
    email = row.get("email")
    if email and email in ADMIN_EMAILS:
        return None

    if _is_beta_active(row.get("beta_status"), row.get("beta_end_date")):
        return BETA_MAX_PHOTOS_PER_PROJECT

    return GENERAL_MAX_PHOTOS_PER_PROJECT
