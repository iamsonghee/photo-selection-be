"""사진 업로드 라우터."""
import asyncio
import gc
import io
import logging
import os
import re
import uuid as uuid_module
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple
from uuid import UUID

try:
    import psutil as _psutil
    _proc = _psutil.Process()
    def _rss_mb() -> float:
        return _proc.memory_info().rss / 1024 / 1024
except ImportError:
    def _rss_mb() -> float:
        return -1.0

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

# 원본 썸네일 (갤러리) — OPT-01: 300px로 축소 (갤러리 카드 크기 기준 충분)
THUMB_MAX_SIZE = 300
THUMB_JPEG_QUALITY = 75

# 원본 미리보기 (뷰어)
PREVIEW_MAX_SIZE = 1200
PREVIEW_JPEG_QUALITY = 82

# 보정본
VERSION_MAX_SIZE = 1500
VERSION_JPEG_QUALITY = 85
VERSION_MAX_BYTES = 2_000_000  # 2MB
VERSION_THUMB_MAX_SIZE = 400
VERSION_THUMB_JPEG_QUALITY = 78

# 프로필
PROFILE_MAX_SIZE = 400
PROFILE_JPEG_QUALITY = 85

# 베타 제한
BETA_MAX_PHOTOS_PER_PROJECT = 3000
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


# 요청 한 번에 여러 장 병렬 처리 시 메모리·CPU 피크 완화 (기본 5, 환경으로 조절)
UPLOAD_PHOTOS_CONCURRENCY = _env_int("UPLOAD_PHOTOS_CONCURRENCY", 5, 1, 12)
VERSION_UPLOAD_CONCURRENCY = _env_int("VERSION_UPLOAD_CONCURRENCY", 3, 1, 12)
# Pillow/R2 동기 작업 스레드 수 (동시 이미지 디코딩 상한에 맞춤, 기본 8)
IMAGE_EXECUTOR_MAX_WORKERS = _env_int("IMAGE_EXECUTOR_MAX_WORKERS", 8, 2, 16)

# Pillow / boto3 블로킹 작업용 스레드풀
_executor = ThreadPoolExecutor(max_workers=IMAGE_EXECUTOR_MAX_WORKERS)


def _apply_exif_orientation(img: Image.Image) -> Image.Image:
    """EXIF Orientation에 맞게 픽셀을 회전한다. 세로 촬영본이 가로로 보이는 문제를 방지한다."""
    try:
        out = ImageOps.exif_transpose(img)
        return out if out is not None else img
    except Exception:
        return img


def _count_photos(supabase, project_id: str) -> int:
    """베타 업로드 장수 제한 체크/잔여량 계산용 — 잠금 없는 빠른 카운트.
    실제 number 할당은 insert_photos_with_numbers RPC가 INSERT 시점에 원자적으로 처리한다."""
    count_r = (
        supabase.table("photos")
        .select("id", count="exact")
        .eq("project_id", project_id)
        .execute()
    )
    return count_r.count or 0


def _insert_photos_with_numbers(supabase, project_id: str, rows: list[dict]) -> list[dict]:
    """프로젝트 행을 잠그고 max(number)를 구한 뒤 순서대로 번호를 매겨 INSERT — 모두 한 트랜잭션(RPC) 안에서 처리해
    번호 계산과 INSERT 사이에 락이 풀리는 시간 간극을 없앤다 (claim_photo_number_base는 락을 readonly RPC 호출
    동안만 쥐고 있었고, 실제 INSERT는 이미지 리사이즈+R2 업로드가 끝난 한참 뒤 별도 트랜잭션에서 실행돼
    동시 업로드 배치끼리 같은 번호를 할당받는 레이스 컨디션이 있었다)."""
    insert_r = supabase.rpc(
        "insert_photos_with_numbers",
        {"p_project_id": project_id, "p_rows": rows},
    ).execute()
    return insert_r.data or []


def _infer_content_type(filename: str) -> Optional[str]:
    """파일 확장자로 content-type 추론. 알 수 없는 확장자는 None 반환 (BUG-01: CR3 등 RAW 파일 조용한 실패 방지)."""
    lower = (filename or "").lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith((".heic", ".heif")):
        return "image/heic"
    return None  # 알 수 없는 확장자 → 명시적 거부


def _upload_to_r2_sync(key: str, body: bytes, content_type: str):
    """동기 R2 업로드 (executor에서 호출)."""
    return upload_to_r2(key, body, content_type)


# ── 원본 사진: 썸네일 + 미리보기 ────────────────────────────────────────────

def _make_thumb_and_preview_sync(image_bytes: bytes) -> Tuple[bytes, bytes]:
    """동기: 썸네일(300px/75%) + 미리보기(1200px/82%) 동시 생성.
    OPT-01: 대형 JPEG(>4000px)는 Draft 모드로 1/8 축소 후 LANCZOS 리샘플링 → 처리 속도 ~40% 향상.
    """
    rss0 = _rss_mb()
    file_kb = len(image_bytes) / 1024

    buf = io.BytesIO(image_bytes)

    # OPT-01: 대형 이미지 pre-shrink — JPEG Draft 모드로 디코딩 크기 줄이기
    try:
        probe = Image.open(io.BytesIO(image_bytes))
        w, h = probe.size
        probe.close()
        del probe
        rss1 = _rss_mb()
        print(f"[mem] start rss={rss0:.1f}MB | file={file_kb:.0f}KB size={w}x{h}", flush=True)
        print(f"[mem] after_probe rss={rss1:.1f}MB Δ{rss1 - rss0:.1f}MB", flush=True)

        if w > 4000 or h > 4000:
            buf.seek(0)
            img = Image.open(buf)
            try:
                img.draft("RGB", (max(PREVIEW_MAX_SIZE, THUMB_MAX_SIZE * 2),
                                   max(PREVIEW_MAX_SIZE, THUMB_MAX_SIZE * 2)))
            except Exception:
                pass
        else:
            buf.seek(0)
            img = Image.open(buf)
    except Exception:
        w, h = 0, 0
        rss1 = rss0
        print(f"[mem] start rss={rss0:.1f}MB | file={file_kb:.0f}KB size=unknown", flush=True)
        buf.seek(0)
        img = Image.open(buf)

    rss2 = _rss_mb()
    print(f"[mem] after_Image_open rss={rss2:.1f}MB Δ{rss2 - rss0:.1f}MB mode={img.mode}", flush=True)

    img = _apply_exif_orientation(img)
    rss3 = _rss_mb()
    print(f"[mem] after_exif_transpose rss={rss3:.1f}MB Δ{rss3 - rss0:.1f}MB", flush=True)

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    rss4 = _rss_mb()
    print(f"[mem] after_convert_RGB rss={rss4:.1f}MB Δ{rss4 - rss0:.1f}MB size={img.size[0]}x{img.size[1]}", flush=True)

    # 썸네일 (갤러리용)
    thumb = img.copy()
    thumb.thumbnail((THUMB_MAX_SIZE, THUMB_MAX_SIZE), Image.Resampling.LANCZOS)
    thumb_buf = io.BytesIO()
    thumb.save(thumb_buf, format="JPEG", quality=THUMB_JPEG_QUALITY)
    thumb.close()
    del thumb
    rss5 = _rss_mb()
    print(f"[mem] after_thumb rss={rss5:.1f}MB Δ{rss5 - rss0:.1f}MB", flush=True)

    # 미리보기 (뷰어용)
    preview = img.copy()
    preview.thumbnail((PREVIEW_MAX_SIZE, PREVIEW_MAX_SIZE), Image.Resampling.LANCZOS)
    preview_buf = io.BytesIO()
    preview.save(preview_buf, format="JPEG", quality=PREVIEW_JPEG_QUALITY)
    preview.close()
    del preview

    img.close()
    del img
    del buf
    rss6 = _rss_mb()
    print(f"[mem] after_preview+del rss={rss6:.1f}MB Δ{rss6 - rss0:.1f}MB", flush=True)

    return thumb_buf.getvalue(), preview_buf.getvalue()


async def _process_one(
    loop: asyncio.AbstractEventLoop,
    contents: bytes,
    index: int,
    project_id: str,
    photographer_id: UUID,
) -> Optional[Tuple[str, str, int]]:
    """파일 하나: 썸네일+미리보기 생성 → R2 병렬 업로드. (photos.number는 모든 파일 업로드가 끝난 뒤
    insert_photos_with_numbers RPC가 한 번에 원자적으로 할당하므로 여기서는 다루지 않는다 — index는 로그 추적용.)
    성공 시 (thumb_url, preview_url, r2_stored_bytes) — r2_stored_bytes는 썸네일+미리보기 JPEG 합계."""
    photo_id = uuid_module.uuid4().hex

    try:
        thumb_bytes, preview_bytes = await loop.run_in_executor(
            _executor,
            _make_thumb_and_preview_sync,
            contents,
        )
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.warning("resize failed for index %s: %s", index, e)
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
        logger.warning("R2 upload failed for index %s: %s", index, e)
        return None

    if not thumb_url or not preview_url:
        return None

    r2_stored_bytes = len(thumb_bytes) + len(preview_bytes)
    rss_r2 = _rss_mb()
    del thumb_bytes, preview_bytes
    gc.collect()
    rss_gc = _rss_mb()
    print(f"[mem] after_r2_upload rss={rss_r2:.1f}MB | after_gc rss={rss_gc:.1f}MB Δ{rss_gc - rss_r2:.1f}MB", flush=True)
    return (thumb_url, preview_url, r2_stored_bytes)


@router.post("/photos")
async def upload_photos(
    project_id: str = Form(...),
    files: list[UploadFile] = File(...),
    photographer_id: UUID = Depends(get_current_photographer),
):
    """
    사진 일괄 업로드: 썸네일(400px/75%) + 미리보기(1200px/82%) 생성 후 R2 병렬 업로드.
    photos.r2_thumb_url (갤러리), photos.r2_preview_url (뷰어) 저장.
    동시 처리 상한: UPLOAD_PHOTOS_CONCURRENCY(기본 5). 스레드 풀: IMAGE_EXECUTOR_MAX_WORKERS(기본 8).
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

    # 허용된 파일만 읽음 (BUG-01: 거부 파일 목록 수집 / BUG-02: 소문자 정규화)
    valid: list[tuple[bytes, str, str, int]] = []  # (contents, content_type, original_filename, file_size)
    rejected_filenames: list[str] = []
    for f in files:
        ct = (f.content_type or "").lower()  # BUG-02: 대문자 MIME 타입 정규화
        if not ct or ct not in ALLOWED_CONTENT_TYPES:
            inferred = _infer_content_type(f.filename or "")
            if inferred is None:  # BUG-01: 알 수 없는 확장자 → 명시적 거부
                rejected_filenames.append(f.filename or "(unknown)")
                logger.warning("rejected unsupported file: %r (content_type=%r)", f.filename, f.content_type)
                continue
            ct = inferred
        if ct not in ALLOWED_CONTENT_TYPES:
            rejected_filenames.append(f.filename or "(unknown)")
            continue
        contents = await f.read()
        if not contents:
            rejected_filenames.append(f.filename or "(unknown)")
            continue
        valid.append((contents, ct, f.filename or "", len(contents)))

    if not valid:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "no_valid_files",
                "message": "지원하지 않는 파일 형식입니다. JPEG, PNG, WebP, HEIC만 가능합니다.",
                "rejected": rejected_filenames,
            }
        )

    # 베타 제한 체크/잔여량 계산용 — 잠금 없는 빠른 카운트.
    # 실제 number 할당은 모든 파일 처리가 끝난 뒤 insert_photos_with_numbers RPC가 INSERT와 함께 원자적으로 처리한다.
    try:
        current_count = _count_photos(supabase, project_id)
    except Exception as e:
        logger.exception("photo count check failed: %s", e)
        raise HTTPException(status_code=500, detail="사진 수 확인 실패") from e

    # 베타 제한: 사진 수 체크
    if current_count >= BETA_MAX_PHOTOS_PER_PROJECT:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "beta_limit_exceeded",
                "limit_type": "photos_per_project",
                "current": current_count,
                "max": BETA_MAX_PHOTOS_PER_PROJECT,
                "message": f"베타 기간 중 프로젝트당 최대 {BETA_MAX_PHOTOS_PER_PROJECT}장까지 업로드할 수 있습니다.",
            },
        )

    # 부분 초과 시 가능한 만큼만
    remaining = BETA_MAX_PHOTOS_PER_PROJECT - current_count
    if len(valid) > remaining:
        valid = valid[:remaining]

    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(UPLOAD_PHOTOS_CONCURRENCY)

    async def _limited_process_one(contents: bytes, index: int):
        async with sem:
            return await _process_one(loop, contents, index, project_id, photographer_id)

    results = await asyncio.gather(
        *[
            _limited_process_one(contents, idx)
            for idx, (contents, _, __, ___) in enumerate(valid)
        ],
        return_exceptions=True,
    )

    # asyncio.gather는 입력 순서를 보존하므로, valid의 원래 파일 순서 그대로 number가 매겨진다.
    rows: list[dict] = []
    for r, (_, __, original_filename, _) in zip(results, valid):
        if isinstance(r, Exception):
            logger.error(f"에러내용: {r}")
            logger.warning("process task failed: %s", r)
            continue
        if r is not None:
            thumb_url, preview_url, r2_stored_bytes = r
            row: dict = {
                "r2_thumb_url": thumb_url,
                "r2_preview_url": preview_url,
                "file_size": r2_stored_bytes,
            }
            if original_filename:
                row["original_filename"] = original_filename
            rows.append(row)

    # 파일 바이트 + 처리 결과 해제 후 OS에 힙 반환 (Python은 freed 메모리를 OS에 자동 반환 안 함)
    del valid
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass
    print(f"[mem] after_batch_trim rss={_rss_mb():.1f}MB", flush=True)

    if not rows:
        return {"uploaded": 0, "rejected": rejected_filenames}

    # number 할당 + INSERT를 한 트랜잭션(RPC) 안에서 원자 처리 — 동시 업로드 배치 간 번호 충돌 방지
    try:
        inserted = _insert_photos_with_numbers(supabase, project_id, rows)
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.exception("photos insert failed: %s", e)
        raise HTTPException(status_code=500, detail="사진 저장 실패") from e

    if not inserted:
        raise HTTPException(status_code=500, detail="사진 저장 실패")

    count_res = (
        supabase.table("photos")
        .select("id", count="exact")
        .eq("project_id", project_id)
        .execute()
    )
    photo_count = (
        count_res.count
        if getattr(count_res, "count", None) is not None
        else current_count + len(inserted)
    )
    try:
        supabase.table("projects").update({"photo_count": photo_count}).eq("id", project_id).execute()
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.exception("projects photo_count update failed: %s", e)
        raise HTTPException(status_code=500, detail="프로젝트 업데이트 실패") from e

    return {"uploaded": len(inserted), "rejected": rejected_filenames}


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

def _resize_version_and_thumb_sync(image_bytes: bytes) -> tuple[bytes, bytes]:
    """보정본 1500px(full) + 400px(thumb) 동시 생성. (full_bytes, thumb_bytes) 반환."""
    img = Image.open(io.BytesIO(image_bytes))
    img = _apply_exif_orientation(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # full (1500px, 최대 2MB)
    full = img.copy()
    full.thumbnail((VERSION_MAX_SIZE, VERSION_MAX_SIZE), Image.Resampling.LANCZOS)
    quality = VERSION_JPEG_QUALITY
    full_buf = io.BytesIO()
    while quality >= 60:
        full_buf = io.BytesIO()
        full.save(full_buf, format="JPEG", quality=quality)
        if full_buf.tell() <= VERSION_MAX_BYTES:
            break
        quality -= 5

    # thumb (400px, 그리드 표시용)
    thumb = img.copy()
    thumb.thumbnail((VERSION_THUMB_MAX_SIZE, VERSION_THUMB_MAX_SIZE), Image.Resampling.LANCZOS)
    thumb_buf = io.BytesIO()
    thumb.save(thumb_buf, format="JPEG", quality=VERSION_THUMB_JPEG_QUALITY)

    return full_buf.getvalue(), thumb_buf.getvalue()


def _make_version_key_sync(project_id: str, version: int, photo_id: str, filename: str) -> str:
    """보정본 R2 key 생성 (동기). BUG-03: 특수문자 → 언더스코어 치환."""
    base = filename or f"{uuid_module.uuid4().hex}.jpg"
    # URL 예약 문자(#, &, ?, %, 공백 등) 및 ASCII 비출력 문자 → 언더스코어
    safe = re.sub(r"[^\w\-.]", "_", base)
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
) -> Optional[Tuple[str, str, str, int]]:
    """보정본 1건: 리사이즈(1500px + 400px thumb) → R2 병렬 업로드.
    성공 시 (r2_url, r2_thumb_url, photo_id, file_size_bytes, filename) 반환."""
    try:
        full_bytes, thumb_bytes = await loop.run_in_executor(
            _executor,
            _resize_version_and_thumb_sync,
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
        thumb_key = f"versions/{project_id}/v{version}/{photo_id}_thumb.jpg"
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.warning("version key failed for photo %s: %s", photo_id, e)
        return None

    try:
        r2_url, r2_thumb_url = await asyncio.gather(
            loop.run_in_executor(_executor, _upload_to_r2_sync, key, full_bytes, "image/jpeg"),
            loop.run_in_executor(_executor, _upload_to_r2_sync, thumb_key, thumb_bytes, "image/jpeg"),
        )
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.warning("version R2 upload failed for photo %s: %s", photo_id, e)
        return None

    if not r2_url:
        return None
    return (r2_url, r2_thumb_url or "", photo_id, len(full_bytes), filename)


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
            r2_url, r2_thumb_url, pid, file_size, orig_filename = r
            results.append(
                {"photo_id": pid, "version": version, "r2_url": r2_url, "r2_thumb_url": r2_thumb_url, "file_size": file_size, "filename": orig_filename}
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
            "r2_thumb_url": item["r2_thumb_url"] or None,
            "photographer_memo": None,
            "file_size": item["file_size"],
            "filename": item.get("filename") or None,
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

    # 교체된 보정본의 기존 version_reviews 삭제 (재보정 요청 상태 초기화)
    # → editing_v2 재진입 시 교체 전에 CTA가 활성화되는 문제 방지
    try:
        uploaded_photo_ids = [item["photo_id"] for item in results]
        if uploaded_photo_ids:
            pv_ids_r = (
                supabase.table("photo_versions")
                .select("id")
                .in_("photo_id", uploaded_photo_ids)
                .eq("version", version)
                .execute()
            )
            pv_ids = [row["id"] for row in pv_ids_r.data or []]
            if pv_ids:
                supabase.table("version_reviews") \
                    .delete() \
                    .in_("photo_version_id", pv_ids) \
                    .execute()
    except Exception as e:
        logger.error(f"에러내용: version_reviews 삭제 실패 {e}")

    return {"uploaded": len(results), "items": results}
