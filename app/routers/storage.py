"""R2 스토리지 삭제 / Presign API."""
import os
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.storage import (
    delete_r2_objects,
    generate_presigned_urls_batch,
    validate_r2_key,
    PRESIGN_EXPIRES_SECONDS,
)

router = APIRouter()

INTERNAL_PRESIGN_SECRET = os.getenv("INTERNAL_PRESIGN_SECRET")
MAX_PRESIGN_BATCH = 200


class DeleteKeysBody(BaseModel):
    keys: list[str]


class PresignBatchBody(BaseModel):
    keys: list[str]
    dispositions: Optional[dict[str, str]] = None


@router.post("/delete")
def delete_objects(body: DeleteKeysBody):
    """R2에서 지정한 key들 삭제. body: { "keys": string[] }"""
    if not body.keys:
        return {"deleted": 0}
    try:
        deleted = delete_r2_objects(body.keys)
        return {"deleted": deleted}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"R2 삭제 실패: {e!s}") from e


@router.post("/presign")
def presign_batch(body: PresignBatchBody, authorization: str = Header(None)):
    """R2 key 목록에 대해 presigned GET URL을 일괄 생성합니다 (내부 전용).

    인증: Authorization: Bearer {INTERNAL_PRESIGN_SECRET}
    요청: { "keys": ["photos/...", ...] }  — 최대 200개
    응답: { "urls": { key: presigned_url }, "expiresAt": unix_seconds }
    """
    if not INTERNAL_PRESIGN_SECRET:
        raise HTTPException(status_code=503, detail="Presign secret not configured")
    if authorization != f"Bearer {INTERNAL_PRESIGN_SECRET}":
        raise HTTPException(status_code=403, detail="Forbidden")

    if not body.keys:
        return {"urls": {}, "expiresAt": int(time.time()) + PRESIGN_EXPIRES_SECONDS}

    if len(body.keys) > MAX_PRESIGN_BATCH:
        raise HTTPException(
            status_code=400, detail=f"Max {MAX_PRESIGN_BATCH} keys per request"
        )

    invalid = [k for k in body.keys if not validate_r2_key(k)]
    if invalid:
        raise HTTPException(
            status_code=400, detail=f"Invalid key pattern: {invalid[:3]}"
        )

    try:
        urls = generate_presigned_urls_batch(body.keys, dispositions=body.dispositions)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Presign 실패: {e!s}") from e

    return {"urls": urls, "expiresAt": int(time.time()) + PRESIGN_EXPIRES_SECONDS}
