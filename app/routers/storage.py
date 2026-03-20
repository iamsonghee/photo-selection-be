"""R2 스토리지 삭제 API."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.storage import delete_r2_objects

router = APIRouter()


class DeleteKeysBody(BaseModel):
    keys: list[str]


@router.post("/delete")
def delete_objects(body: DeleteKeysBody):
    """R2에서 지정한 key들 삭제. body: { \"keys\": string[] }"""
    if not body.keys:
        return {"deleted": 0}
    try:
        deleted = delete_r2_objects(body.keys)
        return {"deleted": deleted}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"R2 삭제 실패: {e!s}") from e
