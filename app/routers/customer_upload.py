"""고객 직접 셀렉 서비스 전용 업로드 라우터.

기존 /api/upload/photos(app/routers/upload.py)는 작가 인증(get_current_photographer),
베타 등급 쿼터, 원본 보관(original_jobs) 등 작가 프로젝트 생애주기에 강하게 결합돼 있어
그대로 재사용할 수 없다(단계 0 분석 결과). 이미지 리사이즈 로직(_make_thumb_and_preview_sync)만
그대로 가져다 쓰고, 나머지는 고객 프로젝트 모델(customer_projects/customer_photos)에 맞춰
훨씬 단순하게 새로 만든다 — 원본(납품) 보관은 1차 범위에서 제외(사용자 결정).
"""
import asyncio
import logging
import uuid as uuid_module
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.database import get_supabase
from app.dependencies import verify_supabase_jwt
from app.env_utils import env_int
from app.routers.upload import ALLOWED_CONTENT_TYPES, _infer_content_type, _make_thumb_and_preview_sync
from app.storage import delete_r2_objects, upload_to_r2

router = APIRouter()
logger = logging.getLogger(__name__)

# 1차 범위 상한(사용자 결정) — 등급별 쿼터 테이블 없이 상수 하나로 충분하다(YAGNI).
MAX_PHOTOS_PER_CUSTOMER_PROJECT = 2000

UPLOAD_CONCURRENCY = env_int("CUSTOMER_UPLOAD_CONCURRENCY", 5, 1, 12)
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"

# 참가자(공유 링크)는 로그인하지 않으므로 Authorization 헤더가 없을 수 있다.
_optional_bearer = HTTPBearer(auto_error=False)


class CustomerPhotoDeleteRequest(BaseModel):
    project_id: str
    photo_ids: list[str]
    share_token: Optional[str] = None


def _get_customer_project(supabase, project_id: str) -> dict:
    r = (
        supabase.table("customer_projects")
        .select("id, owner_id, share_token, photo_count")
        .eq("id", project_id)
        .limit(1)
        .execute()
    )
    if not r.data:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    return r.data[0]


def _authorize_customer_project(
    supabase,
    project_id: str,
    credentials: Optional[HTTPAuthorizationCredentials],
    share_token: Optional[str],
) -> dict:
    """소유자는 Supabase JWT로, 공유 링크 참가자는 share_token으로 접근한다."""
    project = _get_customer_project(supabase, project_id)
    if credentials is not None:
        auth_user_id = verify_supabase_jwt(credentials.credentials)
        if auth_user_id == project["owner_id"]:
            return project
    if share_token and share_token == project["share_token"]:
        return project
    raise HTTPException(status_code=403, detail="이 프로젝트에 접근할 권한이 없습니다.")


async def _process_one_customer_photo(
    loop: asyncio.AbstractEventLoop,
    contents: bytes,
    project_id: str,
) -> Optional[tuple[str, str, str]]:
    """파일 하나: 썸네일+미리보기 생성 → R2 업로드. 반환값 (photo_id, thumb_url, preview_url)."""
    # ponytail: 기본 asyncio 스레드풀을 그대로 쓴다(작가 업로드처럼 CPU/IO 전용 풀로 분리하지 않음).
    # 고객 프로젝트는 동시 다중 업로드 배치 규모가 작아 병목이 아니다 — 실측으로 문제가 확인되면 분리.
    photo_id = str(uuid_module.uuid4())
    try:
        thumb_bytes, preview_bytes, _w, _h = await loop.run_in_executor(
            None, _make_thumb_and_preview_sync, contents
        )
    except Exception as e:
        logger.warning("customer photo resize failed: %s", e)
        return None

    thumb_key = f"customer-photos/{project_id}/{photo_id}_thumb.jpg"
    preview_key = f"customer-photos/{project_id}/{photo_id}_preview.jpg"
    try:
        thumb_url, preview_url = await asyncio.gather(
            loop.run_in_executor(None, upload_to_r2, thumb_key, thumb_bytes, "image/jpeg", IMMUTABLE_CACHE_CONTROL),
            loop.run_in_executor(None, upload_to_r2, preview_key, preview_bytes, "image/jpeg", IMMUTABLE_CACHE_CONTROL),
        )
    except Exception as e:
        logger.warning("customer photo R2 upload failed: %s", e)
        return None
    if not thumb_url or not preview_url:
        return None
    return photo_id, thumb_url, preview_url


@router.post("/photos")
async def upload_customer_photos(
    project_id: str = Form(...),
    files: list[UploadFile] = File(...),
    share_token: Optional[str] = Form(None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
):
    if not files:
        raise HTTPException(status_code=400, detail="At least one file required")

    supabase = get_supabase()
    project = _authorize_customer_project(supabase, project_id, credentials, share_token)

    valid: list[tuple[bytes, str]] = []  # (contents, original_filename)
    rejected_filenames: list[str] = []
    for f in files:
        ct = (f.content_type or "").lower()
        if not ct or ct not in ALLOWED_CONTENT_TYPES:
            inferred = _infer_content_type(f.filename or "")
            if inferred is None:
                rejected_filenames.append(f.filename or "(unknown)")
                continue
            ct = inferred
        contents = await f.read()
        if not contents:
            rejected_filenames.append(f.filename or "(unknown)")
            continue
        valid.append((contents, f.filename or ""))

    if not valid:
        raise HTTPException(
            status_code=400,
            detail={"error": "no_valid_files", "message": "지원하지 않는 파일 형식입니다.", "rejected": rejected_filenames},
        )

    current_count = project["photo_count"]
    remaining = MAX_PHOTOS_PER_CUSTOMER_PROJECT - current_count
    if remaining <= 0:
        raise HTTPException(
            status_code=403,
            detail={"error": "limit_exceeded", "max": MAX_PHOTOS_PER_CUSTOMER_PROJECT, "message": f"프로젝트당 최대 {MAX_PHOTOS_PER_CUSTOMER_PROJECT}장까지 업로드할 수 있습니다."},
        )
    if len(valid) > remaining:
        rejected_filenames.extend(filename for _, filename in valid[remaining:])
        valid = valid[:remaining]

    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)

    async def _limited(contents: bytes):
        async with sem:
            return await _process_one_customer_photo(loop, contents, project_id)

    results = await asyncio.gather(*[_limited(contents) for contents, _ in valid], return_exceptions=True)

    rows: list[dict] = []
    for order_offset, (r, (_, filename)) in enumerate(zip(results, valid)):
        if isinstance(r, Exception) or r is None:
            if isinstance(r, Exception):
                logger.warning("customer photo task failed: %s", r)
            rejected_filenames.append(filename)
            continue
        photo_id, thumb_url, preview_url = r
        rows.append({
            "id": photo_id,
            "project_id": project_id,
            "filename": filename,
            "order_index": current_count + order_offset,
            "storage_key": f"customer-photos/{project_id}/{photo_id}",
            "_thumb_url": thumb_url,
            "_preview_url": preview_url,
        })

    if not rows:
        return {"uploaded": 0, "rejected": rejected_filenames}

    insert_rows = [
        {
            "id": r["id"], "project_id": r["project_id"], "filename": r["filename"],
            "order_index": r["order_index"], "storage_key": r["storage_key"],
            "thumb_url": r["_thumb_url"], "preview_url": r["_preview_url"],
        }
        for r in rows
    ]
    try:
        supabase.table("customer_photos").insert(insert_rows).execute()
        new_count = current_count + len(rows)
        supabase.table("customer_projects").update({"photo_count": new_count}).eq("id", project_id).execute()
    except Exception as e:
        logger.exception("customer_photos insert failed: %s", e)
        raise HTTPException(status_code=500, detail="사진 저장 실패") from e

    return {
        "uploaded": len(rows),
        "rejected": rejected_filenames,
        "photos": [
            {"id": r["id"], "filename": r["filename"], "thumb_url": r["_thumb_url"], "preview_url": r["_preview_url"]}
            for r in rows
        ],
    }


@router.delete("/photos")
async def delete_customer_photos(
    body: CustomerPhotoDeleteRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
):
    supabase = get_supabase()
    project = _authorize_customer_project(supabase, body.project_id, credentials, body.share_token)
    photo_ids = list(dict.fromkeys(body.photo_ids))
    if not photo_ids:
        raise HTTPException(status_code=400, detail="삭제할 사진이 없습니다.")

    photos = (
        supabase.table("customer_photos")
        .select("id")
        .eq("project_id", project["id"])
        .in_("id", photo_ids)
        .execute()
    ).data or []
    owned_ids = [row["id"] for row in photos]
    if len(owned_ids) != len(photo_ids):
        raise HTTPException(status_code=403, detail="이 프로젝트의 사진이 아닙니다.")

    versions = (
        supabase.table("customer_photo_versions")
        .select("id")
        .in_("photo_id", owned_ids)
        .execute()
    ).data or []
    try:
        supabase.table("customer_photos").delete().eq("project_id", project["id"]).in_("id", owned_ids).execute()
        remaining = (
            supabase.table("customer_photos")
            .select("id", count="exact")
            .eq("project_id", project["id"])
            .execute()
        ).count or 0
        supabase.table("customer_projects").update({"photo_count": remaining}).eq("id", project["id"]).execute()
    except Exception as e:
        logger.exception("customer photo delete failed: %s", e)
        raise HTTPException(status_code=500, detail="사진 삭제 실패") from e

    keys = [key for photo_id in owned_ids for key in (
        f"customer-photos/{project['id']}/{photo_id}_thumb.jpg",
        f"customer-photos/{project['id']}/{photo_id}_preview.jpg",
    )]
    keys.extend(key for row in versions for key in (
        f"customer-photos/{project['id']}/retouched/{row['id']}_thumb.jpg",
        f"customer-photos/{project['id']}/retouched/{row['id']}_preview.jpg",
    ))
    try:
        await asyncio.get_event_loop().run_in_executor(None, delete_r2_objects, keys)
    except Exception as e:
        logger.warning("deleted customer photo R2 cleanup failed: %s", e)
    return {"deleted": len(owned_ids), "photo_count": remaining}


@router.post("/retouched")
async def upload_customer_retouched_photos(
    project_id: str = Form(...),
    files: list[UploadFile] = File(...),
    # files와 같은 순서/길이 — FE가 lib/version-mapping.ts로 원본과 미리 매칭(자동+수동)해서 보낸다.
    photo_ids: list[str] = Form(...),
    share_token: Optional[str] = Form(None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
):
    """보정본 업로드(단계 7, S10). 원본과 달리 photo_id가 이미 정해져 들어오므로 순서 배정이
    필요 없고, 같은 photo_id에 몇 번째 회차인지(round)만 계산해서 붙인다."""
    if not files or len(files) != len(photo_ids):
        raise HTTPException(status_code=400, detail="files와 photo_ids 길이가 일치해야 합니다.")

    supabase = get_supabase()
    project = _authorize_customer_project(supabase, project_id, credentials, share_token)

    # photo_id가 실제로 이 프로젝트 소유인지 확인 — 다른 프로젝트 사진에 보정본을 붙이는 것 방지.
    owned = (
        supabase.table("customer_photos").select("id").eq("project_id", project["id"]).in_("id", list(set(photo_ids))).execute()
    )
    owned_ids = {row["id"] for row in (owned.data or [])}
    if not owned_ids.issuperset(set(photo_ids)):
        raise HTTPException(status_code=403, detail="이 프로젝트의 사진이 아닙니다.")

    existing = (
        supabase.table("customer_photo_versions").select("photo_id, round").in_("photo_id", list(set(photo_ids))).execute()
    )
    next_round: dict[str, int] = {}
    for row in existing.data or []:
        next_round[row["photo_id"]] = max(next_round.get(row["photo_id"], 0), row["round"])
    for pid in photo_ids:
        next_round.setdefault(pid, 0)

    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)
    rejected_filenames: list[str] = []

    async def _process(f: UploadFile, photo_id: str):
        ct = (f.content_type or "").lower()
        if not ct or ct not in ALLOWED_CONTENT_TYPES:
            inferred = _infer_content_type(f.filename or "")
            if inferred is None:
                rejected_filenames.append(f.filename or "(unknown)")
                return None
        contents = await f.read()
        if not contents:
            rejected_filenames.append(f.filename or "(unknown)")
            return None
        async with sem:
            version_id = str(uuid_module.uuid4())
            try:
                thumb_bytes, preview_bytes, _w, _h = await loop.run_in_executor(None, _make_thumb_and_preview_sync, contents)
            except Exception as e:
                logger.warning("retouched resize failed: %s", e)
                rejected_filenames.append(f.filename or "(unknown)")
                return None
            thumb_key = f"customer-photos/{project_id}/retouched/{version_id}_thumb.jpg"
            preview_key = f"customer-photos/{project_id}/retouched/{version_id}_preview.jpg"
            try:
                thumb_url, preview_url = await asyncio.gather(
                    loop.run_in_executor(None, upload_to_r2, thumb_key, thumb_bytes, "image/jpeg", IMMUTABLE_CACHE_CONTROL),
                    loop.run_in_executor(None, upload_to_r2, preview_key, preview_bytes, "image/jpeg", IMMUTABLE_CACHE_CONTROL),
                )
            except Exception as e:
                logger.warning("retouched R2 upload failed: %s", e)
                rejected_filenames.append(f.filename or "(unknown)")
                return None
        return {
            "id": version_id, "photo_id": photo_id, "filename": f.filename or "",
            "thumb_url": thumb_url, "preview_url": preview_url,
        }

    results = await asyncio.gather(*[_process(f, pid) for f, pid in zip(files, photo_ids)], return_exceptions=True)

    rows: list[dict] = []
    for r, pid in zip(results, photo_ids):
        if isinstance(r, Exception) or r is None:
            if isinstance(r, Exception):
                logger.warning("retouched task failed: %s", r)
            continue
        next_round[pid] += 1
        rows.append({**r, "round": next_round[pid]})

    if not rows:
        return {"uploaded": 0, "rejected": rejected_filenames}

    try:
        supabase.table("customer_photo_versions").insert(
            [{k: v for k, v in r.items() if k != ""} for r in rows]
        ).execute()
    except Exception as e:
        logger.exception("customer_photo_versions insert failed: %s", e)
        raise HTTPException(status_code=500, detail="보정본 저장 실패") from e

    return {"uploaded": len(rows), "rejected": rejected_filenames, "versions": rows}
