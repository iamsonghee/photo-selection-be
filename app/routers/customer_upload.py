"""고객 직접 셀렉 서비스 전용 업로드 라우터.

기존 /api/upload/photos(app/routers/upload.py)는 작가 인증(get_current_photographer),
베타 등급 쿼터, 원본 보관(original_jobs) 등 작가 프로젝트 생애주기에 강하게 결합돼 있어
그대로 재사용할 수 없다(단계 0 분석 결과). 이미지 리사이즈 로직(_make_thumb_and_preview_sync)만
그대로 가져다 쓰고, 나머지는 고객 프로젝트 모델(customer_projects/customer_photos)에 맞춰
훨씬 단순하게 새로 만든다 — 원본(납품) 보관은 1차 범위에서 제외(사용자 결정).
"""
import asyncio
import json
import logging
import re
import time
import uuid as uuid_module
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.beta_policy import ADMIN_EMAILS
from app.database import get_supabase
from app.dependencies import verify_supabase_jwt
from app.env_utils import env_int
from app.routers.upload import ALLOWED_CONTENT_TYPES, _infer_content_type, _make_thumb_and_preview_sync
from app.storage import delete_r2_objects, upload_to_r2

router = APIRouter()
logger = logging.getLogger(__name__)

# 1차 범위 상한(사용자 결정) — 등급별 쿼터 테이블 없이 상수 하나로 충분하다(YAGNI).
MAX_PHOTOS_PER_CUSTOMER_ACCOUNT = 3000

UPLOAD_CONCURRENCY = env_int("CUSTOMER_UPLOAD_CONCURRENCY", 5, 1, 12)
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"

# 프록시 계약 호환을 위해 optional로 받되, 사진 관리는 소유자 JWT만 허용한다.
_optional_bearer = HTTPBearer(auto_error=False)


class CustomerPhotoDeleteRequest(BaseModel):
    project_id: str
    photo_ids: list[str]
    share_token: Optional[str] = None


def _get_customer_project(supabase, project_id: str) -> dict:
    r = (
        supabase.table("customer_projects")
        .select("id, owner_id, photo_count, lifetime_uploaded_count, exported, delivery_count")
        .eq("id", project_id)
        .limit(1)
        .execute()
    )
    if not r.data:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    return r.data[0]


def _get_customer_account_photo_count(supabase, owner_id: str) -> int:
    result = (
        supabase.table("customer_projects")
        .select("photo_count")
        .eq("owner_id", owner_id)
        .execute()
    )
    return sum(max(0, int(row.get("photo_count") or 0)) for row in (result.data or []))


def _customer_photo_limit(supabase, owner_id: str) -> Optional[int]:
    """계정 전체 사진 한도. 관리자(ADMIN_EMAILS)는 무제한(None) — DB 트리거·FE 이용량도 같은 기준."""
    try:
        email = supabase.auth.admin.get_user_by_id(owner_id).user.email
    except Exception as e:
        logger.warning("customer owner email lookup failed: %s", e)
        email = None
    return None if email in ADMIN_EMAILS else MAX_PHOTOS_PER_CUSTOMER_ACCOUNT


def _require_photo_set_mutable(project: dict) -> None:
    if project.get("exported") or project.get("delivery_count", 0) > 0:
        raise HTTPException(
            status_code=409,
            detail="한 번 전달한 프로젝트의 사진 구성은 변경할 수 없습니다.",
        )


def _authorize_customer_project(
    supabase,
    project_id: str,
    credentials: Optional[HTTPAuthorizationCredentials],
    share_token: Optional[str],
) -> dict:
    """사진 업로드·삭제·보정본 관리는 프로젝트 소유자만 할 수 있다."""
    project = _get_customer_project(supabase, project_id)
    if credentials is not None:
        auth_user_id = verify_supabase_jwt(credentials.credentials)
        if auth_user_id == project["owner_id"]:
            return project
    del share_token
    raise HTTPException(status_code=403, detail="프로젝트 소유자만 사진을 관리할 수 있습니다.")


async def _process_one_customer_photo(
    loop: asyncio.AbstractEventLoop,
    contents: bytes,
    project_id: str,
    timings: Optional[dict[str, list[float]]] = None,
    photo_id: Optional[str] = None,
) -> Optional[tuple[str, str, str]]:
    """파일 하나: 썸네일+미리보기 생성 → R2 업로드. 반환값 (photo_id, thumb_url, preview_url)."""
    # ponytail: 기본 asyncio 스레드풀을 그대로 쓴다(작가 업로드처럼 CPU/IO 전용 풀로 분리하지 않음).
    # 고객 프로젝트는 동시 다중 업로드 배치 규모가 작아 병목이 아니다 — 실측으로 문제가 확인되면 분리.
    photo_id = photo_id or str(uuid_module.uuid4())
    started = time.perf_counter()
    try:
        thumb_bytes, preview_bytes, _w, _h = await loop.run_in_executor(
            None, _make_thumb_and_preview_sync, contents
        )
    except Exception as e:
        logger.warning("customer photo resize failed: %s", e)
        return None
    resized = time.perf_counter()
    if timings is not None:
        timings["resize"].append(resized - started)

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
    if timings is not None:
        timings["r2"].append(time.perf_counter() - resized)
    if not thumb_url or not preview_url:
        return None
    return photo_id, thumb_url, preview_url


_TAKEN_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def _parse_taken_at(raw: Optional[str], count: int) -> list[Optional[str]]:
    """브라우저가 원본 EXIF에서 읽은 촬영 시각 목록(files와 같은 순서). 형식이 틀린 값은 버린다 —
    촬영 시각은 장면 구분 보조 정보일 뿐이라 업로드 자체를 실패시키지 않는다."""
    try:
        values = json.loads(raw) if raw else []
    except ValueError:
        values = []
    if not isinstance(values, list):
        values = []
    parsed = [v if isinstance(v, str) and _TAKEN_AT_PATTERN.match(v) else None for v in values[:count]]
    return parsed + [None] * (count - len(parsed))


def _json_list(raw: Optional[str], count: int) -> list:
    """files와 같은 순서의 JSON 배열 폼 필드. 없거나 깨졌으면 None으로 채운다."""
    try:
        values = json.loads(raw) if isinstance(raw, str) and raw else []
    except ValueError:
        values = []
    if not isinstance(values, list):
        values = []
    values = values[:count]
    return values + [None] * (count - len(values))


def _parse_client_upload_ids(raw: Optional[str], count: int) -> list[str]:
    """브라우저가 사진마다 만든 UUID를 사진 ID로 그대로 쓴다 — 응답을 못 받아 같은 사진을 재시도해도
    같은 ID라 중복 행이 생기지 않는다. 값이 없거나 형식이 틀리면 새 UUID(멱등성 없음)."""
    ids: list[str] = []
    for value in _json_list(raw, count):
        try:
            ids.append(str(uuid_module.UUID(value)))
        except (TypeError, ValueError, AttributeError):
            ids.append(str(uuid_module.uuid4()))
    return ids


def _parse_original_filenames(raw: Optional[str], files: list[UploadFile]) -> list[str]:
    """압축 과정에서 전송 파일명이 `이름.jpg`로 바뀌므로, 브라우저가 따로 보낸 원본 파일명을 저장한다."""
    return [
        value[:255] if isinstance(value, str) and value.strip() else (f.filename or "")
        for value, f in zip(_json_list(raw, len(files)), files)
    ]


def _existing_photo_ids(supabase, project_id: str, photo_ids: list[str]) -> set[str]:
    return {row["id"] for chunk in _chunks(photo_ids) for row in (
        supabase.table("customer_photos").select("id").eq("project_id", project_id).in_("id", chunk).execute()
    ).data or []}


# 이 시간을 넘긴 배치는 원인 구간을 바로 볼 수 있게 warning으로 남긴다(로컬 기본 로깅에서도 출력됨).
SLOW_UPLOAD_SECONDS = 15.0


def _log_upload_timing(project_id: str, files: int, ok: int, rejected: int, upload_bytes: int,
                       marks: dict[str, float], timings: dict[str, list[float]]) -> None:
    """배치 하나의 단계별 소요 시간. 느린 업로드가 디코딩·R2·DB 중 어디서 걸렸는지 구분하기 위함."""
    ms = lambda seconds: round(seconds * 1000)  # noqa: E731
    total = marks["end"] - marks["start"]
    fields = {
        "project": project_id, "files": files, "ok": ok, "rejected": rejected, "mb": round(upload_bytes / 1_048_576, 1),
        "auth_quota_ms": ms(marks["authorized"] - marks["start"]),
        "read_ms": ms(marks["checked"] - marks["authorized"]),
        "process_ms": ms(marks["processed"] - marks["checked"]),
        "resize_max_ms": ms(max(timings["resize"], default=0)),
        "r2_max_ms": ms(max(timings["r2"], default=0)),
        "db_ms": ms(marks["end"] - marks["processed"]),
        "total_ms": ms(total),
    }
    (logger.warning if total > SLOW_UPLOAD_SECONDS else logger.info)(
        "customer upload timing %s", " ".join(f"{key}={value}" for key, value in fields.items())
    )


@router.post("/photos")
async def upload_customer_photos(
    project_id: str = Form(...),
    files: list[UploadFile] = File(...),
    share_token: Optional[str] = Form(None),
    taken_at: Optional[str] = Form(None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
    client_upload_ids: Optional[str] = Form(None),
    original_filenames: Optional[str] = Form(None),
    taken_at_source: Optional[str] = Form(None),
):
    if not files:
        raise HTTPException(status_code=400, detail="At least one file required")
    taken_at_values = _parse_taken_at(taken_at, len(files))
    # 촬영 시각 출처: "exif" | "file"(파일 수정 시각으로 대신함 — 장면 경계에 안 씀). 시각이 없으면 출처도 없다.
    taken_at_sources = [source if value and source in {"exif", "file"} else None
                        for value, source in zip(taken_at_values, _json_list(taken_at_source, len(files)))]
    photo_ids = _parse_client_upload_ids(client_upload_ids, len(files))
    filenames = _parse_original_filenames(original_filenames, files)
    marks = {"start": time.perf_counter()}
    timings: dict[str, list[float]] = {"resize": [], "r2": []}

    supabase = get_supabase()
    project = _authorize_customer_project(supabase, project_id, credentials, share_token)
    _require_photo_set_mutable(project)

    # 재시도 배치 중 이미 저장된 사진은 다시 처리하지 않고 성공으로 센다.
    already_saved = _existing_photo_ids(supabase, project_id, photo_ids)
    new_count = len(files) - sum(photo_id in already_saved for photo_id in photo_ids)

    limit = _customer_photo_limit(supabase, project["owner_id"])
    if limit is not None:
        remaining = max(0, limit - _get_customer_account_photo_count(supabase, project["owner_id"]))
        if new_count > remaining:
            raise HTTPException(
                status_code=403,
                detail={"error": "limit_exceeded", "max": limit,
                        "remaining": remaining,
                        "message": f"{new_count}장을 선택했어요. 셀프 고객 전체 한도에서 {remaining}장까지 추가할 수 있습니다. 파일을 다시 선택해 주세요."},
            )

    marks["authorized"] = time.perf_counter()
    # (files 내 위치, contents, 원본 파일명, (taken_at, taken_at_source), photo_id). 실패 보고는 위치로 한다 — 이름은 겹칠 수 있다.
    valid: list[tuple[int, bytes, str, tuple[Optional[str], Optional[str]], str]] = []
    rejected_indices: list[int] = []
    for index, (f, filename, file_taken_at, file_taken_at_source, photo_id) in enumerate(
            zip(files, filenames, taken_at_values, taken_at_sources, photo_ids)):
        if photo_id in already_saved:
            continue
        ct = (f.content_type or "").lower()
        if not ct or ct not in ALLOWED_CONTENT_TYPES:
            inferred = _infer_content_type(f.filename or "")
            if inferred is None:
                rejected_indices.append(index)
                continue
            ct = inferred
        contents = await f.read()
        if not contents:
            rejected_indices.append(index)
            continue
        valid.append((index, contents, filename, (file_taken_at, file_taken_at_source), photo_id))

    def _rejected_names() -> list[str]:
        return [filenames[index] or "(unknown)" for index in rejected_indices]

    if not valid and not already_saved:
        raise HTTPException(
            status_code=400,
            detail={"error": "no_valid_files", "message": "지원하지 않는 파일 형식입니다.",
                    "rejected": _rejected_names(), "rejected_indices": rejected_indices},
        )

    current_count = project["photo_count"]
    upload_bytes = sum(len(contents) for _, contents, _, _, _ in valid)
    marks["checked"] = time.perf_counter()

    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)

    async def _limited(contents: bytes, photo_id: str):
        async with sem:
            return await _process_one_customer_photo(loop, contents, project_id, timings, photo_id)

    results = await asyncio.gather(*[_limited(contents, photo_id) for _, contents, _, _, photo_id in valid],
                                   return_exceptions=True)
    marks["processed"] = time.perf_counter()

    rows: list[dict] = []
    for order_offset, (r, (index, _, filename, file_taken_at, _)) in enumerate(zip(results, valid)):
        if isinstance(r, Exception) or r is None:
            if isinstance(r, Exception):
                logger.warning("customer photo task failed: %s", r)
            rejected_indices.append(index)
            continue
        photo_id, thumb_url, preview_url = r
        rows.append({
            "id": photo_id,
            "project_id": project_id,
            "filename": filename,
            "order_index": current_count + order_offset,
            "storage_key": f"customer-photos/{project_id}/{photo_id}",
            "taken_at": file_taken_at[0],
            "taken_at_source": file_taken_at[1],
            "_thumb_url": thumb_url,
            "_preview_url": preview_url,
        })

    if not rows:
        marks["end"] = time.perf_counter()
        _log_upload_timing(project_id, len(files), 0, len(rejected_indices), upload_bytes, marks, timings)
        return {"uploaded": len(already_saved), "rejected": _rejected_names(), "rejected_indices": rejected_indices}

    insert_rows = [
        {
            "id": r["id"], "project_id": r["project_id"], "filename": r["filename"],
            "order_index": r["order_index"], "storage_key": r["storage_key"],
            "thumb_url": r["_thumb_url"], "preview_url": r["_preview_url"],
            "taken_at": r["taken_at"], "taken_at_source": r["taken_at_source"],
        }
        for r in rows
    ]
    try:
        # 시간 초과된 요청이 서버에서 아직 처리 중일 때 재시도가 겹치면 같은 ID가 동시에 들어온다 —
        # 충돌한 행은 건너뛴다. 응답에는 실제로 새로 들어간 행만 담긴다.
        inserted = supabase.table("customer_photos").upsert(
            insert_rows, on_conflict="id", ignore_duplicates=True
        ).execute().data or []
        # photo_count는 삭제와 같이 실제 행 수로 다시 센다 — 동시 업로드가 서로의 증가분을 덮어쓰지 않게.
        photo_count = (
            supabase.table("customer_photos").select("id", count="exact").eq("project_id", project_id).execute()
        ).count or 0
        supabase.table("customer_projects").update({
            "photo_count": photo_count,
            "lifetime_uploaded_count": project.get("lifetime_uploaded_count", current_count) + len(inserted),
        }).eq("id", project_id).execute()
    except Exception as e:
        logger.exception("customer_photos insert failed: %s", e)
        # 같은 ID로 먼저 저장된 행(동시 재시도)의 이미지는 지우지 않는다.
        try:
            saved_ids = _existing_photo_ids(supabase, project_id, [row["id"] for row in rows])
        except Exception:
            saved_ids = {row["id"] for row in rows}
        keys = [key for row in rows if row["id"] not in saved_ids for key in (
            f"customer-photos/{project_id}/{row['id']}_thumb.jpg",
            f"customer-photos/{project_id}/{row['id']}_preview.jpg",
        )]
        try:
            await loop.run_in_executor(None, delete_r2_objects, keys)
        except Exception as cleanup_error:
            logger.warning("failed customer photo R2 cleanup after insert error: %s", cleanup_error)
        if "customer account photo limit exceeded" in str(e):
            remaining = max(0, MAX_PHOTOS_PER_CUSTOMER_ACCOUNT - _get_customer_account_photo_count(supabase, project["owner_id"]))
            raise HTTPException(
                status_code=403,
                detail={"error": "limit_exceeded", "max": MAX_PHOTOS_PER_CUSTOMER_ACCOUNT,
                        "remaining": remaining,
                        "message": f"전체 사진 한도({MAX_PHOTOS_PER_CUSTOMER_ACCOUNT:,}장)를 넘어 이번 사진을 올리지 못했어요. 지금은 {remaining:,}장까지 더 올릴 수 있어요."},
            ) from e
        raise HTTPException(status_code=500, detail="사진 저장 실패") from e

    marks["end"] = time.perf_counter()
    _log_upload_timing(project_id, len(files), len(rows), len(rejected_indices), upload_bytes, marks, timings)
    return {
        "uploaded": len(rows) + len(already_saved),
        "rejected": _rejected_names(),
        "rejected_indices": rejected_indices,
        "photos": [
            {"id": r["id"], "filename": r["filename"], "thumb_url": r["_thumb_url"], "preview_url": r["_preview_url"]}
            for r in rows
        ],
    }


# PostgREST는 `in.(...)` 목록을 URL에 싣는다 — 사진 ID 약 600개(≈24KB)를 넘으면 400으로 거절해
# 전체 선택 삭제(최대 3,000장)가 실패했다. 목록 조회·삭제는 이 크기로 나눠 보낸다.
ID_CHUNK = 200


def _chunks(ids: list[str]) -> list[list[str]]:
    return [ids[start:start + ID_CHUNK] for start in range(0, len(ids), ID_CHUNK)]


@router.delete("/photos")
async def delete_customer_photos(
    body: CustomerPhotoDeleteRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
):
    supabase = get_supabase()
    project = _authorize_customer_project(supabase, body.project_id, credentials, body.share_token)
    _require_photo_set_mutable(project)
    photo_ids = list(dict.fromkeys(body.photo_ids))
    if not photo_ids:
        raise HTTPException(status_code=400, detail="삭제할 사진이 없습니다.")

    owned_ids = [row["id"] for chunk in _chunks(photo_ids) for row in (
        supabase.table("customer_photos").select("id").eq("project_id", project["id"]).in_("id", chunk).execute()
    ).data or []]
    if len(owned_ids) != len(photo_ids):
        raise HTTPException(status_code=403, detail="이 프로젝트의 사진이 아닙니다.")

    versions = [row for chunk in _chunks(owned_ids) for row in (
        supabase.table("customer_photo_versions").select("id").in_("photo_id", chunk).execute()
    ).data or []]
    try:
        for chunk in _chunks(owned_ids):
            supabase.table("customer_photos").delete().eq("project_id", project["id"]).in_("id", chunk).execute()
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


@router.delete("/projects/{project_id}")
async def delete_customer_project(
    project_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
):
    """소유자만 프로젝트와 파생 데이터·R2 이미지를 함께 삭제한다."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    supabase = get_supabase()
    project = _get_customer_project(supabase, project_id)
    if verify_supabase_jwt(credentials.credentials) != project["owner_id"]:
        raise HTTPException(status_code=403, detail="프로젝트 소유자만 삭제할 수 있습니다.")

    photos = supabase.table("customer_photos").select("id").eq("project_id", project_id).execute().data or []
    photo_ids = [row["id"] for row in photos]
    versions = []
    if photo_ids:
        versions = supabase.table("customer_photo_versions").select("id").in_("photo_id", photo_ids).execute().data or []
    try:
        supabase.table("customer_projects").delete().eq("id", project_id).eq("owner_id", project["owner_id"]).execute()
    except Exception as e:
        logger.exception("customer project delete failed: %s", e)
        raise HTTPException(status_code=500, detail="프로젝트 삭제 실패") from e

    keys = [key for photo_id in photo_ids for key in (
        f"customer-photos/{project_id}/{photo_id}_thumb.jpg",
        f"customer-photos/{project_id}/{photo_id}_preview.jpg",
    )]
    keys.extend(key for row in versions for key in (
        f"customer-photos/{project_id}/retouched/{row['id']}_thumb.jpg",
        f"customer-photos/{project_id}/retouched/{row['id']}_preview.jpg",
    ))
    if keys:
        try:
            await asyncio.get_event_loop().run_in_executor(None, delete_r2_objects, keys)
        except Exception as e:
            logger.warning("deleted customer project R2 cleanup failed: %s", e)
    return {"deleted": True}


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


@router.delete("/retouched/{version_id}")
async def delete_customer_retouched_photo(
    version_id: str,
    project_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
):
    """잘못 연결해 올린 보정본 한 장을 지운다 — 다시 올리면 그 원본의 다음 회차로 붙는다."""
    supabase = get_supabase()
    project = _authorize_customer_project(supabase, project_id, credentials, None)
    version = (
        supabase.table("customer_photo_versions").select("id, photo_id").eq("id", version_id).execute()
    ).data or []
    owned = version and (
        supabase.table("customer_photos").select("id").eq("project_id", project["id"]).eq("id", version[0]["photo_id"]).execute()
    ).data
    if not owned:
        raise HTTPException(status_code=404, detail="이 프로젝트의 보정본이 아닙니다.")
    try:
        supabase.table("customer_photo_versions").delete().eq("id", version_id).execute()
    except Exception as e:
        logger.exception("customer retouched delete failed: %s", e)
        raise HTTPException(status_code=500, detail="보정본 삭제 실패") from e
    try:
        await asyncio.get_event_loop().run_in_executor(None, delete_r2_objects, [
            f"customer-photos/{project['id']}/retouched/{version_id}_thumb.jpg",
            f"customer-photos/{project['id']}/retouched/{version_id}_preview.jpg",
        ])
    except Exception as e:
        logger.warning("deleted customer retouched R2 cleanup failed: %s", e)
    return {"deleted": True}
