"""사진 업로드 라우터."""
import asyncio
import io
import logging
import os
import uuid as uuid_module
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
register_heif_opener()

from app.database import get_supabase
from app.dependencies import get_current_photographer
from app.storage import upload_to_r2

router = APIRouter()
logger = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}

# 원본 썸네일 (갤러리)
THUMB_MAX_SIZE = 400
THUMB_JPEG_QUALITY = 75

# 원본 미리보기 (뷰어)
PREVIEW_MAX_SIZE = 1200
PREVIEW_JPEG_QUALITY = 82

# 보정본
VERSION_MAX_SIZE = 1500
VERSION_JPEG_QUALITY = 85
VERSION_MAX_BYTES = 2_000_000  # 2MB

# 프로필
PROFILE_MAX_SIZE = 400
PROFILE_JPEG_QUALITY = 85

# 베타 제한
BETA_MAX_PHOTOS_PER_PROJECT = 1500
BETA_MAX_REVISION_COUNT = 2


def _env_int(name: str, default: int, min_v: int, max_v: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(min_v, min(max_v, v))


# 요청 한 번에 여러 장 병렬 처리 시 메모리·CPU 피크 완화 (기본 3)
UPLOAD_PHOTOS_CONCURRENCY = _env_int("UPLOAD_PHOTOS_CONCURRENCY", 3, 1, 12)
VERSION_UPLOAD_CONCURRENCY = _env_int("VERSION_UPLOAD_CONCURRENCY", 3, 1, 12)
# Pillow/R2 동기 작업 스레드 수 (동시 이미지 디코딩 상한에 맞춤)
IMAGE_EXECUTOR_MAX_WORKERS = _env_int("IMAGE_EXECUTOR_MAX_WORKERS", 6, 2, 16)

# Pillow / boto3 블로킹 작업용 스레드풀
_executor = ThreadPoolExecutor(max_workers=IMAGE_EXECUTOR_MAX_WORKERS)


def _apply_exif_orientation(img: Image.Image) -> Image.Image:
    """EXIF Orientation에 맞게 픽셀을 회전한다. 세로 촬영본이 가로로 보이는 문제를 방지한다."""
    try:
        out = ImageOps.exif_transpose(img)
        return out if out is not None else img
    except Exception:
        return img


def _infer_content_type(filename: str) -> str:
    """파일 확장자로 content-type 추론 (프록시 등에서 Content-Type이 비어 있을 때 사용)."""
    lower = (filename or "").lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith((".heic", ".heif")):
        return "image/heic"
    return "image/jpeg"


def _upload_to_r2_sync(key: str, body: bytes, content_type: str):
    """동기 R2 업로드 (executor에서 호출)."""
    return upload_to_r2(key, body, content_type)


# ── 원본 사진: 썸네일 + 미리보기 ────────────────────────────────────────────

def _make_thumb_and_preview_sync(image_bytes: bytes) -> Tuple[bytes, bytes]:
    """동기: 썸네일(400px/75%) + 미리보기(1200px/82%) 동시 생성."""
    img = Image.open(io.BytesIO(image_bytes))
    img = _apply_exif_orientation(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # 썸네일 (갤러리용)
    thumb = img.copy()
    thumb.thumbnail((THUMB_MAX_SIZE, THUMB_MAX_SIZE), Image.Resampling.LANCZOS)
    thumb_buf = io.BytesIO()
    thumb.save(thumb_buf, format="JPEG", quality=THUMB_JPEG_QUALITY)

    # 미리보기 (뷰어용)
    preview = img.copy()
    preview.thumbnail((PREVIEW_MAX_SIZE, PREVIEW_MAX_SIZE), Image.Resampling.LANCZOS)
    preview_buf = io.BytesIO()
    preview.save(preview_buf, format="JPEG", quality=PREVIEW_JPEG_QUALITY)

    return thumb_buf.getvalue(), preview_buf.getvalue()


async def _process_one(
    loop: asyncio.AbstractEventLoop,
    contents: bytes,
    number: int,
    project_id: str,
    photographer_id: UUID,
) -> Optional[Tuple[str, str, int, int]]:
    """파일 하나: 썸네일+미리보기 생성 → R2 병렬 업로드.
    성공 시 (thumb_url, preview_url, number, r2_stored_bytes) — r2_stored_bytes는 썸네일+미리보기 JPEG 합계."""
    photo_id = uuid_module.uuid4().hex

    try:
        thumb_bytes, preview_bytes = await loop.run_in_executor(
            _executor,
            _make_thumb_and_preview_sync,
            contents,
        )
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.warning("resize failed for number %s: %s", number, e)
        return None

    thumb_key = f"photos/{photographer_id}/{project_id}/{photo_id}_thumb.jpg"
    preview_key = f"photos/{photographer_id}/{project_id}/{photo_id}_preview.jpg"

    try:
        thumb_url, preview_url = await asyncio.gather(
            loop.run_in_executor(_executor, _upload_to_r2_sync, thumb_key, thumb_bytes, "image/jpeg"),
            loop.run_in_executor(_executor, _upload_to_r2_sync, preview_key, preview_bytes, "image/jpeg"),
        )
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.warning("R2 upload failed for number %s: %s", number, e)
        return None

    if not thumb_url or not preview_url:
        return None

    r2_stored_bytes = len(thumb_bytes) + len(preview_bytes)
    return (thumb_url, preview_url, number, r2_stored_bytes)


@router.post("/photos")
async def upload_photos(
    project_id: str = Form(...),
    files: list[UploadFile] = File(...),
    photographer_id: UUID = Depends(get_current_photographer),
):
    """
    사진 일괄 업로드: 썸네일(400px/75%) + 미리보기(1200px/82%) 생성 후 R2 병렬 업로드.
    photos.r2_thumb_url (갤러리), photos.r2_preview_url (뷰어) 저장.
    동시 처리 상한: UPLOAD_PHOTOS_CONCURRENCY(기본 3). 스레드 풀: IMAGE_EXECUTOR_MAX_WORKERS(기본 6).
    """
    if not files:
        raise HTTPException(status_code=400, detail="At least one file required")

    try:
        supabase = get_supabase()
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.exception("get_supabase failed")
        raise HTTPException(status_code=503, detail="DB 연결 실패") from e

    # 프로젝트 소유 확인
    project_r = (
        supabase.table("projects")
        .select("id")
        .eq("id", project_id)
        .eq("photographer_id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not project_r.data or len(project_r.data) == 0:
        raise HTTPException(status_code=404, detail="Project not found")

    # 허용된 파일만 읽고 number 미리 순서대로 할당
    valid: list[tuple[bytes, str, str, int]] = []  # (contents, content_type, original_filename, file_size)
    for f in files:
        ct = f.content_type or ""
        if not ct or ct not in ALLOWED_CONTENT_TYPES:
            ct = _infer_content_type(f.filename or "")
        if ct not in ALLOWED_CONTENT_TYPES:
            continue
        contents = await f.read()
        if not contents:
            continue
        valid.append((contents, ct, f.filename or "", len(contents)))

    if not valid:
        raise HTTPException(status_code=400, detail="No valid image files (jpeg, png, webp)")

    # base_number 조회 후 number 순서 할당
    max_r = (
        supabase.table("photos")
        .select("number")
        .eq("project_id", project_id)
        .order("number", desc=True)
        .limit(1)
        .execute()
    )
    base_number = max_r.data[0]["number"] if max_r.data else 0

    # 베타 제한: 사진 수 체크
    if base_number >= BETA_MAX_PHOTOS_PER_PROJECT:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "beta_limit_exceeded",
                "limit_type": "photos_per_project",
                "current": base_number,
                "max": BETA_MAX_PHOTOS_PER_PROJECT,
                "message": f"베타 기간 중 프로젝트당 최대 {BETA_MAX_PHOTOS_PER_PROJECT}장까지 업로드할 수 있습니다.",
            },
        )

    # 부분 초과 시 가능한 만큼만
    remaining = BETA_MAX_PHOTOS_PER_PROJECT - base_number
    if len(valid) > remaining:
        valid = valid[:remaining]

    numbers = [base_number + i for i in range(1, len(valid) + 1)]

    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(UPLOAD_PHOTOS_CONCURRENCY)

    async def _limited_process_one(contents: bytes, num: int):
        async with sem:
            return await _process_one(loop, contents, num, project_id, photographer_id)

    results = await asyncio.gather(
        *[
            _limited_process_one(contents, num)
            for (contents, _, __, ___), num in zip(valid, numbers)
        ],
        return_exceptions=True,
    )

    rows: list[dict] = []
    for r, (_, __, original_filename, _) in zip(results, valid):
        if isinstance(r, Exception):
            logger.error(f"에러내용: {r}")
            logger.warning("process task failed: %s", r)
            continue
        if r is not None:
            thumb_url, preview_url, number, r2_stored_bytes = r
            row: dict = {
                "project_id": project_id,
                "number": number,
                "r2_thumb_url": thumb_url,
                "r2_preview_url": preview_url,
                "file_size": r2_stored_bytes,
            }
            if original_filename:
                row["original_filename"] = original_filename
            rows.append(row)

    if not rows:
        return {"uploaded": 0}

    rows.sort(key=lambda x: x["number"])
    try:
        supabase.table("photos").insert(rows).execute()
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.exception("photos insert failed: %s", e)
        raise HTTPException(status_code=500, detail="사진 저장 실패") from e

    photo_count = base_number + len(rows)
    try:
        supabase.table("projects").update({"photo_count": photo_count}).eq("id", project_id).execute()
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.exception("projects photo_count update failed: %s", e)
        raise HTTPException(status_code=500, detail="프로젝트 업데이트 실패") from e

    return {"uploaded": len(rows)}


# ── 프로필 이미지 ────────────────────────────────────────────────────────────

def _resize_profile_image_sync(image_bytes: bytes, content_type: str) -> bytes:
    """프로필 이미지 리사이즈: 최장변 400px, JPEG 85%."""
    img = Image.open(io.BytesIO(image_bytes))
    img = _apply_exif_orientation(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((PROFILE_MAX_SIZE, PROFILE_MAX_SIZE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=PROFILE_JPEG_QUALITY)
    return buf.getvalue()


@router.post("/profile-image")
async def upload_profile_image(
    file: UploadFile = File(...),
    photographer_id: UUID = Depends(get_current_photographer),
):
    """
    프로필 이미지 1장 업로드: 리사이즈(최장변 400px, JPEG 85%) 후 R2 업로드.
    경로: profiles/{photographer_id}/{uuid}.jpg
    """
    if not file.content_type or file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="image/jpeg, image/png, image/webp only")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty file")

    loop = asyncio.get_event_loop()
    try:
        resized = await loop.run_in_executor(
            _executor,
            _resize_profile_image_sync,
            contents,
            file.content_type or "image/jpeg",
        )
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.warning("profile image resize failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid image") from e

    key = f"profiles/{photographer_id}/{uuid_module.uuid4().hex}.jpg"
    try:
        r2_url = await loop.run_in_executor(
            _executor,
            _upload_to_r2_sync,
            key,
            resized,
            "image/jpeg",
        )
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.exception("profile image R2 upload failed: %s", e)
        raise HTTPException(status_code=500, detail="Upload failed") from e

    if not r2_url:
        raise HTTPException(status_code=500, detail="R2 URL not configured")

    return {"url": r2_url}


# ── 보정본 업로드 (리사이즈 후 R2, photo_versions INSERT) ────────────────────

def _resize_version_sync(image_bytes: bytes) -> bytes:
    """보정본 리사이즈: 최장변 1500px, JPEG 85%. 2MB 초과 시 품질 낮춰 2MB 이하로 맞춤."""
    img = Image.open(io.BytesIO(image_bytes))
    img = _apply_exif_orientation(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((VERSION_MAX_SIZE, VERSION_MAX_SIZE), Image.Resampling.LANCZOS)

    quality = VERSION_JPEG_QUALITY
    while quality >= 60:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= VERSION_MAX_BYTES:
            return buf.getvalue()
        quality -= 5

    # quality 60에서도 초과하면 그대로 반환
    return buf.getvalue()


def _make_version_key_sync(project_id: str, version: int, photo_id: str, filename: str) -> str:
    """보정본 R2 key 생성 (동기)."""
    safe = (filename or f"{uuid_module.uuid4().hex}.jpg").replace(" ", "_")
    if not safe.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        safe = f"{safe}.jpg"
    return f"versions/{project_id}/v{version}/{photo_id}_{safe}"


async def _process_one_version(
    loop: asyncio.AbstractEventLoop,
    project_id: str,
    version: int,
    photo_id: str,
    filename: str,
    contents: bytes,
    content_type: str,
) -> Optional[Tuple[str, str, int]]:
    """보정본 1건: 리사이즈(1500px/85%/2MB 제한) → R2 업로드. 성공 시 (r2_url, photo_id, file_size_bytes) 반환."""
    try:
        resized_bytes = await loop.run_in_executor(
            _executor,
            _resize_version_sync,
            contents,
        )
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.warning("version resize failed for photo %s: %s", photo_id, e)
        return None

    try:
        key = await loop.run_in_executor(
            _executor,
            _make_version_key_sync,
            project_id,
            version,
            photo_id,
            filename,
        )
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.warning("version key failed for photo %s: %s", photo_id, e)
        return None

    try:
        r2_url = await loop.run_in_executor(
            _executor,
            _upload_to_r2_sync,
            key,
            resized_bytes,
            "image/jpeg",
        )
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.warning("version R2 upload failed for photo %s: %s", photo_id, e)
        return None

    if not r2_url:
        return None
    return (r2_url, photo_id, len(resized_bytes))


@router.post("/versions")
async def upload_versions(
    project_id: str = Form(...),
    version: int = Form(...),
    photo_ids: str = Form(..., description="comma-separated photo_id list, order matches files"),
    files: list[UploadFile] = File(...),
    photographer_id: UUID = Depends(get_current_photographer),
):
    """
    보정본 일괄 업로드: 리사이즈(최장변 1500px, JPEG 85%, 2MB 제한) 후 R2 업로드, photo_versions INSERT.
    form: project_id, version (1 or 2), photo_ids (id1,id2,id3), files (multipart).
    """
    if version not in (1, 2):
        raise HTTPException(status_code=400, detail="version must be 1 or 2")
    if not files:
        raise HTTPException(status_code=400, detail="At least one file required")

    try:
        supabase = get_supabase()
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.exception("get_supabase failed")
        raise HTTPException(status_code=503, detail="DB 연결 실패") from e

    project_r = (
        supabase.table("projects")
        .select("id")
        .eq("id", project_id)
        .eq("photographer_id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not project_r.data or len(project_r.data) == 0:
        raise HTTPException(status_code=404, detail="Project not found")

    # 베타 제한: 보정본 횟수 체크
    photos_r = (
        supabase.table("photos")
        .select("id")
        .eq("project_id", project_id)
        .execute()
    )
    photo_ids_list = [p["id"] for p in photos_r.data or []]
    existing_versions: set[int] = set()
    if photo_ids_list:
        pv_r = (
            supabase.table("photo_versions")
            .select("version")
            .in_("photo_id", photo_ids_list)
            .execute()
        )
        existing_versions = {v["version"] for v in pv_r.data or []}
    if version not in existing_versions and len(existing_versions) >= BETA_MAX_REVISION_COUNT:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "beta_limit_exceeded",
                "limit_type": "revision_count",
                "current": len(existing_versions),
                "max": BETA_MAX_REVISION_COUNT,
                "message": f"베타 기간 중 최대 {BETA_MAX_REVISION_COUNT}회까지 보정본을 업로드할 수 있습니다.",
            },
        )

    pid_list = [p.strip() for p in photo_ids.split(",") if p.strip()]
    if len(pid_list) != len(files):
        raise HTTPException(
            status_code=400,
            detail="photo_ids count must match files count",
        )

    valid: list[tuple[str, bytes, str, str]] = []  # (photo_id, contents, content_type, filename)
    for photo_id, upload_file in zip(pid_list, files):
        content_type = upload_file.content_type
        if not content_type or content_type not in ALLOWED_CONTENT_TYPES:
            content_type = _infer_content_type(upload_file.filename or "")
            if content_type not in ALLOWED_CONTENT_TYPES:
                logger.warning("skip photo_id=%s: unsupported content_type=%r filename=%r", photo_id, upload_file.content_type, upload_file.filename)
                continue
        contents = await upload_file.read()
        if not contents:
            logger.warning("skip photo_id=%s: empty file filename=%r", photo_id, upload_file.filename)
            continue
        valid.append((photo_id, contents, content_type, upload_file.filename or ""))

    if not valid:
        return {"uploaded": 0, "items": [], "message": "처리된 파일이 없습니다. JPEG/PNG/WebP 형식과 파일 크기를 확인하세요."}

    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(VERSION_UPLOAD_CONCURRENCY)

    async def _limited_version(
        photo_id: str,
        contents: bytes,
        content_type: str,
        filename: str,
    ):
        async with sem:
            return await _process_one_version(
                loop, project_id, version, photo_id, filename, contents, content_type
            )

    gathered = await asyncio.gather(
        *[
            _limited_version(photo_id, contents, content_type, filename)
            for photo_id, contents, content_type, filename in valid
        ],
        return_exceptions=True,
    )

    results: list[dict] = []
    for r, (photo_id, _, __, ___) in zip(gathered, valid):
        if isinstance(r, Exception):
            logger.error(f"에러내용: {r}")
            logger.warning("version upload task failed: %s", r)
            continue
        if r is not None:
            r2_url, pid, file_size = r
            results.append(
                {"photo_id": pid, "version": version, "r2_url": r2_url, "file_size": file_size}
            )

    if not results:
        logger.error("에러내용: 업로드 결과가 0건입니다. R2 업로드 결과를 확인하세요.")
        raise HTTPException(
            status_code=503,
            detail="스토리지 업로드 실패. R2 설정을 확인하세요.",
        )

    rows = [
        {
            "photo_id": item["photo_id"],
            "version": item["version"],
            "r2_url": item["r2_url"],
            "photographer_memo": None,
            "file_size": item["file_size"],
        }
        for item in results
    ]
    try:
        supabase.table("photo_versions").upsert(
            rows,
            on_conflict="photo_id,version",
        ).execute()
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.exception("photo_versions upsert failed: %s", e)
        err_msg = str(e).strip() or "사진 버전 저장 실패"
        raise HTTPException(status_code=500, detail=err_msg) from e

    return {"uploaded": len(results), "items": results}
