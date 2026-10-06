"""하객 사진 모으기 — R2 서명·확인 전용 내부 API.

앨범·하객·한도 판단은 모두 Next API 라우트(service-role)가 한다. 여기는 Next 서버만 호출하며
(Authorization: Bearer INTERNAL_PRESIGN_SECRET), R2 자격 증명이 필요한 일만 대신한다:
  - presign-put: 브라우저가 파일을 R2에 직접 올릴 PUT 주소(크기·형식 서명 포함)
  - head: 올라간 파일 크기 확인
  - delete: 거절한 파일 정리
키는 guest-albums/{album_id}/{media_id}/(original|thumb|preview)만 허용한다.
"""
import asyncio
import os
import re
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.storage import delete_r2_objects, generate_presigned_put_url, head_r2_object_sync

router = APIRouter()

INTERNAL_PRESIGN_SECRET = os.getenv("INTERNAL_PRESIGN_SECRET")
GUEST_PUT_EXPIRES_SECONDS = 3600
MAX_ITEMS = 30

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
GUEST_KEY_PATTERN = re.compile(rf"^guest-albums/{_UUID}/{_UUID}/(original|thumb|preview)$")
_CONTENT_TYPE_PATTERN = re.compile(r"^(image|video)/[a-z0-9.+-]{1,60}$")


def _require_internal(authorization: Optional[str]) -> None:
    if not INTERNAL_PRESIGN_SECRET:
        raise HTTPException(status_code=503, detail="Presign secret not configured")
    if authorization != f"Bearer {INTERNAL_PRESIGN_SECRET}":
        raise HTTPException(status_code=403, detail="Forbidden")


def _require_guest_keys(keys: list[str]) -> None:
    invalid = [key for key in keys if not GUEST_KEY_PATTERN.match(key)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid key pattern: {invalid[:3]}")


class PutItem(BaseModel):
    key: str
    content_type: str
    content_length: int = Field(gt=0)


class PresignPutBody(BaseModel):
    items: list[PutItem] = Field(min_length=1, max_length=MAX_ITEMS)


class KeysBody(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=MAX_ITEMS)


@router.post("/presign-put")
def presign_put(body: PresignPutBody, authorization: str = Header(None)):
    _require_internal(authorization)
    _require_guest_keys([item.key for item in body.items])
    if any(not _CONTENT_TYPE_PATTERN.match(item.content_type) for item in body.items):
        raise HTTPException(status_code=400, detail="Invalid content type")
    try:
        urls = {
            item.key: generate_presigned_put_url(item.key, item.content_type, GUEST_PUT_EXPIRES_SECONDS, item.content_length)
            for item in body.items
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Presign 실패: {e!s}") from e
    return {"urls": urls}


@router.post("/head")
async def head(body: KeysBody, authorization: str = Header(None)):
    """key마다 R2 크기. 없거나 비었으면 null."""
    _require_internal(authorization)
    _require_guest_keys(body.keys)
    loop = asyncio.get_event_loop()

    async def size(key: str):
        try:
            return await loop.run_in_executor(None, head_r2_object_sync, key)
        except KeyError:
            return None

    try:
        sizes = await asyncio.gather(*[size(key) for key in body.keys])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"R2 확인 실패: {e!s}") from e
    return {"sizes": dict(zip(body.keys, sizes))}


@router.post("/delete")
def delete(body: KeysBody, authorization: str = Header(None)):
    _require_internal(authorization)
    _require_guest_keys(body.keys)
    try:
        return {"deleted": delete_r2_objects(body.keys)}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"R2 삭제 실패: {e!s}") from e
