from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.database import get_supabase
from app.dependencies import get_current_photographer
from app.storage import delete_r2_objects_by_prefix

router = APIRouter()


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
    """프로젝트에 속한 R2 객체 삭제 (photos/..., versions/...). 프로젝트 DB 삭제 전 호출."""
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
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"R2 삭제 실패: {e!s}",
        ) from e
    return {"deleted": total}
