from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.database import get_supabase
from app.dependencies import get_current_photographer
from app.storage import delete_r2_objects_by_prefix

router = APIRouter()

BETA_MAX_PROJECTS_TOTAL = 10


class ProjectCreate(BaseModel):
    name: str


@router.get("")
def list_my_projects(photographer_id: UUID = Depends(get_current_photographer)):
    """내 프로젝트 목록."""
    client = get_supabase()
    r = (
        client.table("projects")
        .select("*")
        .eq("photographer_id", str(photographer_id))
        .order("created_at", desc=True)
        .execute()
    )
    return {"projects": r.data or []}


@router.post("")
def create_project(
    body: ProjectCreate,
    photographer_id: UUID = Depends(get_current_photographer),
):
    """프로젝트 생성."""
    client = get_supabase()

    # 베타 제한: 누적 프로젝트 수 체크
    count_r = (
        client.table("projects")
        .select("id")
        .eq("photographer_id", str(photographer_id))
        .execute()
    )
    current_count = len(count_r.data or [])
    if current_count >= BETA_MAX_PROJECTS_TOTAL:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "beta_limit_exceeded",
                "limit_type": "projects_total",
                "current": current_count,
                "max": BETA_MAX_PROJECTS_TOTAL,
                "message": f"베타 기간 중 최대 {BETA_MAX_PROJECTS_TOTAL}개의 프로젝트를 생성할 수 있습니다.",
            },
        )

    r = (
        client.table("projects")
        .insert({"photographer_id": str(photographer_id), "name": body.name})
        .select()
        .execute()
    )
    if not r.data or len(r.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create project",
        )
    return r.data[0]


@router.get("/{project_id}")
def get_project(
    project_id: UUID,
    photographer_id: UUID = Depends(get_current_photographer),
):
    """프로젝트 상세."""
    client = get_supabase()
    r = (
        client.table("projects")
        .select("*")
        .eq("id", str(project_id))
        .eq("photographer_id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not r.data or len(r.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    return r.data[0]


@router.delete("/{project_id}/r2")
def delete_project_r2(
    project_id: UUID,
    photographer_id: UUID = Depends(get_current_photographer),
):
    """프로젝트에 속한 R2 객체 삭제 (사진·버전·보존 원본·ZIP). 프로젝트 DB 삭제 전 호출."""
    client = get_supabase()
    r = (
        client.table("projects")
        .select("id")
        .eq("id", str(project_id))
        .eq("photographer_id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not r.data or len(r.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    pid = str(project_id)
    photographer_id_str = str(photographer_id)
    total = 0
    try:
        total += delete_r2_objects_by_prefix(f"photos/{photographer_id_str}/{pid}/")
        total += delete_r2_objects_by_prefix(f"versions/{pid}/")
        total += delete_r2_objects_by_prefix(f"versions/delivery-archives/{pid}/")
        total += delete_r2_objects_by_prefix(f"originals/source/{pid}/")
        total += delete_r2_objects_by_prefix(f"originals/archives/{pid}/")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"R2 삭제 실패: {e!s}",
        ) from e
    return {"deleted": total}
