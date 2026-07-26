"""
클로즈드 베타 사용자 등급(관리자/베타/일반) 판정 및 이용량 한도.

실제 유효 한도 값은 Supabase `app_settings` 테이블(id=1, 싱글턴)에서 조회한다 —
FE의 /admin/settings에서 관리자가 값을 바꾸면 재배포 없이 즉시 반영된다.
아래 DEFAULT_* 상수는 그 테이블 조회가 실패했을 때만 쓰는 폴백 값이다.

ADMIN_EMAILS는 photo-selection-fe/src/lib/admin-emails.ts의 ADMIN_EMAILS와 반드시 같은 값을 유지할 것
(이 값은 이번 설정 실시간화 범위에서 제외 — 여전히 하드코딩 유지).
"""
from datetime import date
from typing import Optional
from uuid import UUID

ADMIN_EMAILS = ["realsong88@gmail.com", "hilee6461@gmail.com"]

# 베타 사용자 기본 한도(override 없음, 전원 동일) — app_settings 조회 실패 시 폴백
DEFAULT_BETA_MAX_PHOTOS_PER_PROJECT = 2000

# 일반(Trial) 사용자 한도 — app_settings 조회 실패 시 폴백
DEFAULT_GENERAL_MAX_PHOTOS_PER_PROJECT = 500


def _get_settings(supabase) -> dict:
    """app_settings(id=1) 조회. 실패/행 없음이면 DEFAULT_* 값으로 조립해 반환(절대 raise하지 않음)."""
    try:
        r = (
            supabase.table("app_settings")
            .select("general_max_photos_per_project, beta_max_photos_per_project")
            .eq("id", 1)
            .limit(1)
            .execute()
        )
        if r.data:
            row = r.data[0]
            return {
                "general_max_photos_per_project": row.get(
                    "general_max_photos_per_project", DEFAULT_GENERAL_MAX_PHOTOS_PER_PROJECT
                ),
                "beta_max_photos_per_project": row.get(
                    "beta_max_photos_per_project", DEFAULT_BETA_MAX_PHOTOS_PER_PROJECT
                ),
            }
    except Exception:
        pass
    return {
        "general_max_photos_per_project": DEFAULT_GENERAL_MAX_PHOTOS_PER_PROJECT,
        "beta_max_photos_per_project": DEFAULT_BETA_MAX_PHOTOS_PER_PROJECT,
    }


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
    settings = _get_settings(supabase)

    if not r.data:
        # 조회 실패 시 가장 보수적인(일반 사용자) 한도로 폴백
        return settings["general_max_photos_per_project"]

    row = r.data[0]
    email = row.get("email")
    if email and email in ADMIN_EMAILS:
        return None

    if _is_beta_active(row.get("beta_status"), row.get("beta_end_date")):
        return settings["beta_max_photos_per_project"]

    return settings["general_max_photos_per_project"]
