"""사진 업로드 라우터."""
import asyncio
import gc
import io
import logging
import os
import re
import time
import uuid as uuid_module
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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
from app.beta_policy import get_max_photos_per_project
from app.storage import (
    delete_r2_objects,
    generate_presigned_put_url,
    get_r2_object_bytes_sync,
    head_r2_object_sync,
    upload_to_r2,
)

router = APIRouter()
logger = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}

# 원본 사진 썸네일/프리뷰는 업로드마다 새 랜덤 UUID를 key로 써서 같은 key가 절대
# 재사용되지 않는다 — 그래서만 안전하게 영구 캐싱 가능 (photo_versions 등 같은 key가
# 재업로드로 덮어써질 수 있는 경로에는 절대 쓰지 말 것).
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"

# 원본 썸네일 (갤러리) — OPT-01: 300px로 축소 (갤러리 카드 크기 기준 충분)
THUMB_MAX_SIZE = 300
THUMB_JPEG_QUALITY = 75

# 원본 미리보기 (뷰어)
PREVIEW_MAX_SIZE = 1200
PREVIEW_JPEG_QUALITY = 82

# 보정본 (원본과 동일한 사이즈·품질 기준)
VERSION_MAX_SIZE = 1200
VERSION_JPEG_QUALITY = 82
VERSION_THUMB_MAX_SIZE = 300
VERSION_THUMB_JPEG_QUALITY = 75

# 프로필
PROFILE_MAX_SIZE = 400
PROFILE_JPEG_QUALITY = 85

# 베타 제한 (프로젝트당 사진 수는 등급별로 다름 — app/beta_policy.py 참고)
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
# 원본 포함 업로드 시 presigned PUT 방식으로 서버 부담 낮춤 (기본 3)
UPLOAD_WITH_ORIGINAL_CONCURRENCY = _env_int("UPLOAD_WITH_ORIGINAL_CONCURRENCY", 3, 1, 8)
VERSION_UPLOAD_CONCURRENCY = _env_int("VERSION_UPLOAD_CONCURRENCY", 3, 1, 12)
# Pillow/R2 동기 작업 스레드 수 (동시 이미지 디코딩 상한에 맞춤, 기본 8) — /photos 외 다른
# 엔드포인트(보정본 업로드, 원본 압축, R2 head/get/delete, 프로필 이미지)가 공유해서 쓴다.
IMAGE_EXECUTOR_MAX_WORKERS = _env_int("IMAGE_EXECUTOR_MAX_WORKERS", 8, 2, 16)
# 후보 C: /photos 파이프라인(_process_one) 전용 CPU(Pillow)/I/O(R2 PUT) 분리 풀.
# 동시 2요청 실측(큐 대기: 단일 요청 ~0ms → 동시 2요청 시 Pillow/R2 모두 수백ms)에서 공유
# executor 경쟁이 확인되어 적용. 위 IMAGE_EXECUTOR_MAX_WORKERS(다른 엔드포인트용)는 그대로 두고
# 이 파이프라인만 별도 풀로 분리 — CPU 풀은 코어 낭비 방지 위해 작게, I/O 풀은 네트워크 대기라
# 메모리 부담이 적어 조금 더 크게 기본값을 잡는다.
PILLOW_EXECUTOR_MAX_WORKERS = _env_int("PILLOW_EXECUTOR_MAX_WORKERS", 4, 2, 12)
R2_EXECUTOR_MAX_WORKERS = _env_int("R2_EXECUTOR_MAX_WORKERS", 6, 2, 16)
# 비동기 납품 원본 검증 worker 동시성. 현재 작업은 R2 객체 HEAD와 DB 상태 전이만 수행하며
# 이미지 디코딩/재압축을 하지 않으므로, 전역 대기열을 한 장씩 막지 않도록 기본 4개로 처리한다.
# 환경변수로 더 보수적으로 낮출 수 있고, 공유 I/O executor(기본 8)를 넘지 않게 상한을 둔다.
ORIGINAL_COMPRESS_CONCURRENCY = _env_int("ORIGINAL_COMPRESS_CONCURRENCY", 4, 1, 8)
# presigned PUT URL 유효 시간 (초)
ORIGINAL_PRESIGNED_EXPIRES = 3600

# 원본(납품) 파일 상한 (20MB)
ORIGINAL_MAX_BYTES = 20 * 1024 * 1024

# content_type → 파일 확장자
_CT_TO_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/heic": "heic",
    "image/heif": "heic",
    "image/png": "png",
    "image/webp": "webp",
}

# Pillow / boto3 블로킹 작업용 스레드풀 (/photos 외 엔드포인트 공용)
_executor = ThreadPoolExecutor(max_workers=IMAGE_EXECUTOR_MAX_WORKERS)
# 후보 C: /photos 파이프라인 전용 — CPU(Pillow 생성) / I/O(R2 PUT) 분리
_cpu_executor = ThreadPoolExecutor(max_workers=PILLOW_EXECUTOR_MAX_WORKERS)
_r2_executor = ThreadPoolExecutor(max_workers=R2_EXECUTOR_MAX_WORKERS)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


# 후보 A: 상시 [mem] 디버그 로그(psutil RSS 조회 포함)를 기본 OFF로 — OOM 디버깅 시에만 켠다.
UPLOAD_MEM_LOG = _env_flag("UPLOAD_MEM_LOG")


def _apply_exif_orientation(img: Image.Image) -> Image.Image:
    """EXIF Orientation에 맞게 픽셀을 회전한다. 세로 촬영본이 가로로 보이는 문제를 방지한다."""
    try:
        out = ImageOps.exif_transpose(img)
        return out if out is not None else img
    except Exception:
        return img


def _count_photos(supabase, project_id: str) -> int:
    """베타 업로드 장수 제한 체크/잔여량 계산용 — 잠금 없는 빠른 카운트.
    실제 number 할당은 insert_photos_with_numbers RPC가 INSERT 시점에 원자적으로 처리한다.
    동시 배치 업로드 시 Supabase 연결 일시 소진으로 간헐적 실패 가능 → 최대 3회 재시도."""
    last_exc: Exception = RuntimeError("unreachable")
    for attempt in range(3):
        if attempt > 0:
            time.sleep(0.5 * attempt)  # 0.5s, 1.0s
        try:
            count_r = (
                supabase.table("photos")
                .select("id", count="exact")
                .eq("project_id", project_id)
                .execute()
            )
            return count_r.count or 0
        except Exception as e:
            last_exc = e
            logger.warning("_count_photos attempt %d failed: %s", attempt + 1, e)
    raise last_exc




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


def _upload_to_r2_sync(key: str, body: bytes, content_type: str, cache_control: Optional[str] = None):
    """동기 R2 업로드 (executor에서 호출)."""
    return upload_to_r2(key, body, content_type, cache_control=cache_control)


# ── 원본 사진: 썸네일 + 미리보기 ────────────────────────────────────────────

def _make_thumb_and_preview_sync(image_bytes: bytes) -> Tuple[bytes, bytes]:
    """동기: 썸네일(300px/75%) + 미리보기(1200px/82%) 동시 생성.
    OPT-01: 대형 JPEG(>4000px)는 Draft 모드로 1/8 축소 후 LANCZOS 리샘플링 → 처리 속도 ~40% 향상.
    """
    rss0 = _rss_mb() if UPLOAD_MEM_LOG else 0.0
    file_kb = len(image_bytes) / 1024

    buf = io.BytesIO(image_bytes)

    # OPT-01: 대형 이미지 pre-shrink — JPEG Draft 모드로 디코딩 크기 줄이기
    try:
        probe = Image.open(io.BytesIO(image_bytes))
        w, h = probe.size
        probe.close()
        del probe
        if UPLOAD_MEM_LOG:
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
        if UPLOAD_MEM_LOG:
            print(f"[mem] start rss={rss0:.1f}MB | file={file_kb:.0f}KB size=unknown", flush=True)
        buf.seek(0)
        img = Image.open(buf)

    if UPLOAD_MEM_LOG:
        rss2 = _rss_mb()
        print(f"[mem] after_Image_open rss={rss2:.1f}MB Δ{rss2 - rss0:.1f}MB mode={img.mode}", flush=True)

    img = _apply_exif_orientation(img)
    if UPLOAD_MEM_LOG:
        rss3 = _rss_mb()
        print(f"[mem] after_exif_transpose rss={rss3:.1f}MB Δ{rss3 - rss0:.1f}MB", flush=True)

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if UPLOAD_MEM_LOG:
        rss4 = _rss_mb()
        print(f"[mem] after_convert_RGB rss={rss4:.1f}MB Δ{rss4 - rss0:.1f}MB size={img.size[0]}x{img.size[1]}", flush=True)

    # 썸네일 (갤러리용)
    thumb = img.copy()
    thumb.thumbnail((THUMB_MAX_SIZE, THUMB_MAX_SIZE), Image.Resampling.LANCZOS)
    thumb_buf = io.BytesIO()
    thumb.save(thumb_buf, format="JPEG", quality=THUMB_JPEG_QUALITY)
    thumb.close()
    del thumb
    if UPLOAD_MEM_LOG:
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
    if UPLOAD_MEM_LOG:
        rss6 = _rss_mb()
        print(f"[mem] after_preview+del rss={rss6:.1f}MB Δ{rss6 - rss0:.1f}MB", flush=True)

    return thumb_buf.getvalue(), preview_buf.getvalue()


def _process_original_sync(image_bytes: bytes, content_type: str) -> bytes:
    """원본 bytes → JPEG 변환 + 자동 압축. 20MB는 목표치이며 초과해도 최선 결과를 반환한다."""
    is_jpeg = content_type == "image/jpeg"
    if is_jpeg and len(image_bytes) <= ORIGINAL_MAX_BYTES:
        return image_bytes
    img = Image.open(io.BytesIO(image_bytes))
    img = _apply_exif_orientation(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if not is_jpeg:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        data = buf.getvalue()
        if len(data) <= ORIGINAL_MAX_BYTES:
            img.close()
            return data
    for quality in (90, 85, 80, 75):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= ORIGINAL_MAX_BYTES:
            img.close()
            return data
    last: bytes = b""
    for max_edge in (6000, 5000, 4000, 3200, 2400, 1600):
        resized = img.copy()
        resized.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=90)
        last = buf.getvalue()
        resized.close()
        if len(last) <= ORIGINAL_MAX_BYTES:
            img.close()
            return last
    img.close()
    return last


async def _process_one(
    loop: asyncio.AbstractEventLoop,
    contents: bytes,
    index: int,
    project_id: str,
    photographer_id: UUID,
    include_original: bool = False,
    original_content_type: str = "",  # 브라우저 원본 파일의 MIME type (presigned key 확장자 결정용)
) -> Optional[Tuple[str, str, int, Optional[dict]]]:
    """파일 하나: 썸네일+미리보기 생성 → R2 업로드.
    include_original=True 시 presigned PUT 정보를 반환 (원본 압축은 worker가 비동기 처리).
    성공 시 (thumb_url, preview_url, r2_stored_bytes, original_presigned_or_None).
    original_presigned = {source_key, photo_hex, content_type}
    B plan: contents는 항상 압축본(2MB JPEG), original_content_type은 원본 파일 타입.
    썸네일/프리뷰 생성(CPU)과 R2 PUT(I/O)은 별도 스레드풀(_cpu_executor/_r2_executor)에서
    실행한다 — 동시 요청 실측에서 공유 풀 경쟁이 확인되어 분리함(OPT-02).
    """
    photo_id = uuid_module.uuid4().hex

    try:
        thumb_bytes, preview_bytes = await loop.run_in_executor(
            _cpu_executor,
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
            loop.run_in_executor(_r2_executor, _upload_to_r2_sync, thumb_key, thumb_bytes, "image/jpeg", IMMUTABLE_CACHE_CONTROL),
            loop.run_in_executor(_r2_executor, _upload_to_r2_sync, preview_key, preview_bytes, "image/jpeg", IMMUTABLE_CACHE_CONTROL),
        )
    except Exception as e:
        logger.error(f"에러내용: {e}")
        logger.warning("R2 upload failed for index %s: %s", index, e)
        return None

    if not thumb_url or not preview_url:
        return None

    r2_stored_bytes = len(thumb_bytes) + len(preview_bytes)
    if UPLOAD_MEM_LOG:
        rss_r2 = _rss_mb()
    del thumb_bytes, preview_bytes
    gc.collect()
    if UPLOAD_MEM_LOG:
        rss_gc = _rss_mb()
        print(f"[mem] after_r2_upload rss={rss_r2:.1f}MB | after_gc rss={rss_gc:.1f}MB Δ{rss_gc - rss_r2:.1f}MB", flush=True)

    original_presigned: Optional[dict] = None
    if include_original and original_content_type:
        ext = _CT_TO_EXT.get(original_content_type, "jpg")
        source_key = f"originals/source/{project_id}/{photo_id}.{ext}"
        original_presigned = {"source_key": source_key, "photo_hex": photo_id, "content_type": original_content_type}

    return (thumb_url, preview_url, r2_stored_bytes, original_presigned)


@router.post("/photos")
async def upload_photos(
    project_id: str = Form(...),
    files: list[UploadFile] = File(...),
    include_original: bool = Form(False),
    original_filenames: list[str] = Form(default=[]),
    original_file_sizes: list[int] = Form(default=[]),
    original_last_modifieds: list[int] = Form(default=[]),
    original_content_types: list[str] = Form(default=[]),
    photographer_id: UUID = Depends(get_current_photographer),
):
    """
    사진 일괄 업로드: 썸네일(300px/75%) + 미리보기(1200px/82%) 생성 후 R2 병렬 업로드.
    include_original=true 시 원본(납품)도 R2 originals/ 경로에 함께 저장.
    동시 처리 상한: include_original 여부에 따라 UPLOAD_WITH_ORIGINAL_CONCURRENCY(3) / UPLOAD_PHOTOS_CONCURRENCY(5).
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
        .select("id, status")
        .eq("id", project_id)
        .eq("photographer_id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not project_r.data or len(project_r.data) == 0:
        raise HTTPException(status_code=404, detail="Project not found")

    # 초대 링크 활성화(preparing 이탈) 이후에는 납품용 원본 추가 업로드를 금지 —
    # 이미 생성됐거나 생성 중인 아카이브와 실제 사진 구성이 어긋나는 것을 원천 차단한다.
    if include_original and project_r.data[0].get("status") != "preparing":
        raise HTTPException(
            status_code=403,
            detail="초대 링크 활성화 이후에는 납품용 원본을 추가할 수 없습니다.",
        )

    # 허용된 파일만 읽음 (BUG-01: 거부 파일 목록 수집 / BUG-02: 소문자 정규화)
    valid: list[tuple[bytes, str, str, int]] = []  # (contents, content_type, compressed_filename, file_size)
    # 복구 매칭용 원본 파일 메타 (valid와 1:1 대응, FE가 보낸 original_* Form 필드 기반)
    meta: list[tuple[str, str, Optional[int], Optional[int]]] = []  # (orig_fn, orig_ct, orig_size, orig_lm)
    rejected_filenames: list[str] = []
    for i, f in enumerate(files):
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
        # HEIC 정책: include_original=True 베타에서는 JPEG/PNG/WebP만 허용 (HEIC 디코딩 실패 → raw 전송 위험)
        if include_original and ct in ("image/heic", "image/heif"):
            rejected_filenames.append(f.filename or "(unknown)")
            logger.warning("rejected HEIC for include_original upload (beta policy): %r", f.filename)
            continue
        contents = await f.read()
        if not contents:
            rejected_filenames.append(f.filename or "(unknown)")
            continue
        valid.append((contents, ct, f.filename or "", len(contents)))
        # original_* 배열은 files와 인덱스 동기화 — 파싱 실패 시 압축 파일 정보로 fallback
        orig_fn = original_filenames[i] if i < len(original_filenames) else (f.filename or "")
        orig_ct = original_content_types[i] if i < len(original_content_types) else ct
        orig_sz: Optional[int] = original_file_sizes[i] if i < len(original_file_sizes) else None
        orig_lm: Optional[int] = original_last_modifieds[i] if i < len(original_last_modifieds) else None
        meta.append((orig_fn, orig_ct, orig_sz, orig_lm))

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

    # 등급별 업로드 한도 체크 (관리자는 None=무제한)
    max_photos = get_max_photos_per_project(supabase, photographer_id)
    if max_photos is not None and current_count >= max_photos:
        try:
            supabase.table("admin_audit_logs").insert({
                "photographer_id": str(photographer_id),
                "actor": "system",
                "action": "photo_limit_hit",
                "detail": {"project_id": project_id, "current": current_count, "max": max_photos},
            }).execute()
        except Exception:
            logger.exception("admin_audit_logs insert failed (photo_limit_hit)")
        raise HTTPException(
            status_code=403,
            detail={
                "error": "beta_limit_exceeded",
                "limit_type": "photos_per_project",
                "current": current_count,
                "max": max_photos,
                "message": f"프로젝트당 최대 {max_photos}장까지 업로드할 수 있습니다.",
            },
        )

    # 부분 초과 시 가능한 만큼만(무제한이면 제한 없음)
    if max_photos is not None:
        remaining = max_photos - current_count
        if len(valid) > remaining:
            valid = valid[:remaining]

    loop = asyncio.get_event_loop()
    effective_concurrency = UPLOAD_WITH_ORIGINAL_CONCURRENCY if include_original else UPLOAD_PHOTOS_CONCURRENCY
    sem = asyncio.Semaphore(effective_concurrency)

    async def _limited_process_one(contents: bytes, index: int, orig_ct: str):
        async with sem:
            return await _process_one(loop, contents, index, project_id, photographer_id, include_original, orig_ct)

    results = await asyncio.gather(
        *[
            _limited_process_one(contents, idx, meta[idx][1])
            for idx, (contents, _, __, ___) in enumerate(valid)
        ],
        return_exceptions=True,
    )

    # asyncio.gather는 입력 순서를 보존하므로, valid의 원래 파일 순서 그대로 number가 매겨진다.
    rows: list[dict] = []
    # presigned_infos: (presigned_dict | None, orig_fn, orig_ct, orig_sz, orig_lm) — rows와 1:1 대응
    presigned_infos: list[tuple[Optional[dict], str, str, Optional[int], Optional[int]]] = []
    for r, (_, __, compressed_fn, ___), (orig_fn, orig_ct, orig_sz, orig_lm) in zip(results, valid, meta):
        if isinstance(r, Exception):
            logger.error(f"에러내용: {r}")
            logger.warning("process task failed: %s", r)
            continue
        if r is not None:
            thumb_url, preview_url, r2_stored_bytes, original_presigned = r
            row: dict = {
                "r2_thumb_url": thumb_url,
                "r2_preview_url": preview_url,
                "file_size": r2_stored_bytes,
            }
            # include_original일 때는 브라우저 원본 파일명, 아닐 때는 압축 파일명 사용
            display_fn = orig_fn if include_original else compressed_fn
            if display_fn:
                row["original_filename"] = display_fn
            if original_presigned:
                row["original_status"] = "awaiting_upload"
                # photo와 original_job을 insert_photos_with_numbers RPC의 같은 트랜잭션에서
                # 생성해, photo INSERT 뒤 job INSERT 실패로 생기는 고아 행을 막는다.
                original_job: dict = {
                    "r2_source_key": original_presigned["source_key"],
                    "source_content_type": original_presigned["content_type"],
                    "original_filename": orig_fn or None,
                    "original_content_type": orig_ct or None,
                }
                if orig_sz is not None:
                    original_job["original_file_size"] = orig_sz
                if orig_lm is not None:
                    original_job["original_last_modified"] = orig_lm
                row["_original_job"] = original_job
            rows.append(row)
            presigned_infos.append((original_presigned, orig_fn, orig_ct, orig_sz, orig_lm))

    # 파일 바이트 + 처리 결과 해제 후 OS에 힙 반환 (Python은 freed 메모리를 OS에 자동 반환 안 함)
    del valid
    del meta
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass
    if UPLOAD_MEM_LOG:
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

    # original_jobs는 위 RPC 트랜잭션에서 이미 생성됐다. 생성된 job을 조회해 URL만 발급한다.
    original_presigned_response: list[dict] = []
    now_utc = datetime.now(timezone.utc)
    expires_at = (now_utc + timedelta(seconds=ORIGINAL_PRESIGNED_EXPIRES)).isoformat()

    original_photo_ids = [
        photo_row["id"]
        for photo_row, (presigned_info, *_rest) in zip(inserted, presigned_infos)
        if presigned_info
    ]
    jobs_by_photo: dict[str, dict] = {}
    if original_photo_ids:
        try:
            jobs_r = (
                supabase.table("original_jobs")
                .select("id,photo_id,r2_source_key,source_content_type")
                .in_("photo_id", original_photo_ids)
                .execute()
            )
            jobs_by_photo = {str(job["photo_id"]): job for job in (jobs_r.data or [])}
        except Exception as e:
            # job은 이미 저장됐다. 다음 페이지 진입 시 복구 배너가 다시 조회할 수 있다.
            logger.exception("created original_jobs lookup failed: %s", e)

    for photo_row, (presigned_info, _orig_fn, _orig_ct, _orig_sz, _orig_lm) in zip(inserted, presigned_infos):
        if not presigned_info:
            continue
        job = jobs_by_photo.get(str(photo_row["id"]))
        if not job:
            # DB 마이그레이션보다 BE가 먼저 배포된 짧은 구간의 하위 호환. 신규 RPC가
            # 적용된 뒤에는 도달하지 않으며, 적용 전에도 원본 업로드 자체는 중단하지 않는다.
            try:
                fallback_payload: dict = {
                    "photo_id": photo_row["id"],
                    "project_id": project_id,
                    "r2_source_key": presigned_info["source_key"],
                    "source_content_type": presigned_info["content_type"],
                    "status": "awaiting_upload",
                    "original_filename": _orig_fn or None,
                    "original_content_type": _orig_ct or None,
                }
                if _orig_sz is not None:
                    fallback_payload["original_file_size"] = _orig_sz
                if _orig_lm is not None:
                    fallback_payload["original_last_modified"] = _orig_lm
                fallback_r = supabase.table("original_jobs").insert(fallback_payload).execute()
                job = (fallback_r.data or [None])[0]
            except Exception as e:
                logger.exception("fallback original_job insert failed for photo %s: %s", photo_row["id"], e)
                continue
        if not job:
            logger.error("original_job not found for photo %s", photo_row["id"])
            continue
        try:
            source_key = job["r2_source_key"]
            ct = job["source_content_type"]
            presigned_url = generate_presigned_put_url(source_key, ct, ORIGINAL_PRESIGNED_EXPIRES)
            original_presigned_response.append({
                "job_id": job["id"],
                "url": presigned_url,
                "source_key": source_key,
                "content_type": ct,
                "expires_at": expires_at,
            })
        except Exception as e:
            logger.exception("original presign failed for photo %s: %s", photo_row["id"], e)

    response: dict = {"uploaded": len(inserted), "rejected": rejected_filenames}
    if original_presigned_response:
        response["original_presigned"] = original_presigned_response
    return response


# ── 원본 압축 비동기 처리 ────────────────────────────────────────────────────

@router.post("/originals/confirm")
async def confirm_original_upload(
    job_id: str = Form(...),
    photographer_id: UUID = Depends(get_current_photographer),
):
    """presigned PUT 완료 통지: 소유권 확인 → 멱등 상태 체크 → R2 HEAD → pending 전이."""
    supabase = get_supabase()
    # 소유권 확인 (job → project → photographer)
    job_r = (
        supabase.table("original_jobs")
        .select("id,status,r2_source_key,project_id")
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    if not job_r.data:
        raise HTTPException(status_code=404, detail="job not found")
    job = job_r.data[0]
    proj_r = (
        supabase.table("projects")
        .select("id")
        .eq("id", job["project_id"])
        .eq("photographer_id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not proj_r.data:
        raise HTTPException(status_code=403, detail="forbidden")
    # 멱등: 이미 pending/processing/completed이면 바로 OK 반환
    if job["status"] in ("pending", "processing", "completed"):
        return {"ok": True}
    # awaiting_upload: R2 HEAD로 파일 실제 존재 확인 (서버 측 수행)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_executor, _head_r2_object_sync, job["r2_source_key"])
    except KeyError:
        raise HTTPException(status_code=409, detail="R2 object not found — upload may still be in progress")
    except Exception as e:
        logger.exception("R2 HEAD failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=502, detail=f"R2 HEAD 확인 실패: {e}")
    # 조건부 UPDATE: status = 'awaiting_upload' 행만 전이 (RPC 내부에서 WHERE status='awaiting_upload')
    try:
        supabase.rpc("confirm_original_upload", {"p_job_id": job_id}).execute()
    except Exception as e:
        logger.exception("confirm_original_upload RPC failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="confirm 처리 실패") from e
    return {"ok": True}


@router.post("/originals/finalize")
async def finalize_original_upload(
    project_id: str = Form(...),
    photographer_id: UUID = Depends(get_current_photographer),
):
    """업로드 세션 종료 시 DB 상태만 집계한다.

    R2 HEAD나 worker 완료를 기다리지 않고, confirm을 통과한 pending/processing/completed를
    수락 상태로 본다. 따라서 정상 업로드 경로에는 작은 DB count 쿼리만 추가된다.
    """
    supabase = get_supabase()
    proj_r = (
        supabase.table("projects")
        .select("id")
        .eq("id", project_id)
        .eq("photographer_id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not proj_r.data:
        raise HTTPException(status_code=404, detail="Project not found")

    def result_count(result) -> int:
        count = getattr(result, "count", None)
        return int(count) if count is not None else len(result.data or [])

    try:
        total = result_count(
            supabase.table("photos")
            .select("id", count="exact")
            .eq("project_id", project_id)
            .execute()
        )
        accepted = result_count(
            supabase.table("photos")
            .select("id", count="exact")
            .eq("project_id", project_id)
            .in_("original_status", ["pending", "processing", "completed"])
            .execute()
        )
        completed = result_count(
            supabase.table("photos")
            .select("id", count="exact")
            .eq("project_id", project_id)
            .eq("original_status", "completed")
            .execute()
        )
        jobs = result_count(
            supabase.table("original_jobs")
            .select("id", count="exact")
            .eq("project_id", project_id)
            .execute()
        )
    except Exception as e:
        logger.exception("original upload finalize failed for project %s: %s", project_id, e)
        raise HTTPException(status_code=500, detail="원본 업로드 상태 확인 실패") from e

    incomplete = max(0, total - accepted)
    missing_jobs = max(0, total - jobs)
    return {
        "ok": total > 0 and incomplete == 0 and missing_jobs == 0,
        "total": total,
        "accepted": accepted,
        "completed": completed,
        "incomplete": incomplete,
        "missing_jobs": missing_jobs,
    }


def _get_r2_object_sync(key: str) -> bytes:
    """동기 R2 다운로드 (executor에서 호출)."""
    return get_r2_object_bytes_sync(key)


def _head_r2_object_sync(key: str) -> int:
    """동기 R2 HEAD 확인 (executor에서 호출)."""
    return head_r2_object_sync(key)


def _delete_r2_objects_sync(keys: list[str]) -> None:
    """동기 R2 삭제 (executor에서 호출)."""
    delete_r2_objects(keys)


@router.get("/originals/pending")
async def get_pending_originals(
    project_id: str,
    photographer_id: UUID = Depends(get_current_photographer),
):
    """awaiting_upload/failed 상태 original_jobs 목록 반환. FE 복구 배너 표시용 — 내부 필드 비노출.
    failed도 포함하는 이유: presigned PUT이 조용히 실패하면 job이 awaiting_upload에 머물다
    24h sweep(stuck_job_sweep_worker)에서 failed로 전환되는데, 이 상태를 배너에서 빼면
    사용자가 복구할 방법이 전혀 없어 원본 아카이브 enqueue가 영구히 막힌다(재업로드로만 복구 가능)."""
    supabase = get_supabase()
    proj_r = (
        supabase.table("projects")
        .select("id")
        .eq("id", project_id)
        .eq("photographer_id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not proj_r.data:
        raise HTTPException(status_code=404, detail="Project not found")
    jobs_r = (
        supabase.table("original_jobs")
        .select("id,original_filename,original_file_size,original_last_modified,created_at")
        .eq("project_id", project_id)
        .in_("status", ["awaiting_upload", "failed"])
        .order("created_at")
        .execute()
    )
    return {"jobs": jobs_r.data or []}


@router.post("/originals/recover")
async def recover_original(
    job_id: str = Form(...),
    photographer_id: UUID = Depends(get_current_photographer),
):
    """awaiting_upload job 복구: R2 HEAD 확인 → 이미 업로드됐으면 confirm, 없으면 새 presigned URL 발급."""
    supabase = get_supabase()
    job_r = (
        supabase.table("original_jobs")
        .select("id,status,r2_source_key,project_id,source_content_type")
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    if not job_r.data:
        raise HTTPException(status_code=404, detail="job not found")
    job = job_r.data[0]
    proj_r = (
        supabase.table("projects")
        .select("id")
        .eq("id", job["project_id"])
        .eq("photographer_id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not proj_r.data:
        raise HTTPException(status_code=403, detail="forbidden")
    # 이미 처리된 job이면 바로 OK
    if job["status"] in ("pending", "processing", "completed"):
        return {"status": "confirmed"}
    source_key = job["r2_source_key"]
    ct = job["source_content_type"]
    loop = asyncio.get_event_loop()
    # R2 HEAD: 파일이 이미 올라와 있으면 confirm으로 전이
    r2_exists = False
    try:
        await loop.run_in_executor(_executor, _head_r2_object_sync, source_key)
        r2_exists = True
    except KeyError:
        pass  # 파일 없음 → 새 presigned URL
    except Exception as e:
        logger.exception("R2 HEAD failed in recover for job %s: %s", job_id, e)
        raise HTTPException(status_code=502, detail=f"R2 HEAD 실패: {e}")
    if r2_exists:
        try:
            supabase.rpc("confirm_original_upload", {"p_job_id": job_id}).execute()
        except Exception as e:
            logger.exception("confirm in recover failed for job %s: %s", job_id, e)
            raise HTTPException(status_code=500, detail="confirm 처리 실패") from e
        return {"status": "confirmed"}
    # 파일 없음 → 새 presigned PUT URL 발급
    presigned_url = generate_presigned_put_url(source_key, ct, ORIGINAL_PRESIGNED_EXPIRES)
    now_utc = datetime.now(timezone.utc)
    expires_at = (now_utc + timedelta(seconds=ORIGINAL_PRESIGNED_EXPIRES)).isoformat()
    return {
        "status": "needs_upload",
        "url": presigned_url,
        "source_key": source_key,
        "content_type": ct,
        "expires_at": expires_at,
    }


@router.post("/originals/abandon")
async def abandon_original(
    job_id: str = Form(...),
    photographer_id: UUID = Depends(get_current_photographer),
):
    """사용자가 원본 업로드를 포기할 때 job을 명시적으로 failed 처리."""
    supabase = get_supabase()
    job_r = (
        supabase.table("original_jobs")
        .select("id,photo_id,status,project_id")
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    if not job_r.data:
        raise HTTPException(status_code=404, detail="job not found")
    job = job_r.data[0]
    proj_r = (
        supabase.table("projects")
        .select("id")
        .eq("id", job["project_id"])
        .eq("photographer_id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not proj_r.data:
        raise HTTPException(status_code=403, detail="forbidden")
    if job["status"] in ("completed", "failed"):
        return {"ok": True}
    try:
        supabase.rpc("fail_original_job", {
            "p_job_id": job_id,
            "p_photo_id": job["photo_id"],
            "p_last_error": "abandoned by user",
        }).execute()
    except Exception as e:
        logger.exception("abandon failed for job %s: %s", job_id, e)
        raise HTTPException(status_code=500, detail="abandon 처리 실패") from e
    return {"ok": True}


async def _process_original_job(job: dict) -> None:
    """original_jobs 행 1개를 원본 객체 검증 후 완료 처리한다.

    브라우저가 presigned PUT으로 올린 바이트가 납품 원본이다. 이 객체를 다시 JPEG로
    압축하거나 삭제하면 고객 ZIP이 원본이 아니게 되므로, 존재·크기만 검증해 그대로
    photos.r2_original_url에 연결한다.
    """
    job_id: str = job["id"]
    photo_id: str = job["photo_id"]
    project_id: str = job["project_id"]
    source_key: str = job["r2_source_key"]
    attempts: int = job["attempts"]
    max_attempts: int = job["max_attempts"]

    supabase = get_supabase()
    loop = asyncio.get_event_loop()

    def _fail(reason: str) -> None:
        try:
            supabase.rpc("fail_original_job", {
                "p_job_id": job_id, "p_photo_id": photo_id, "p_last_error": reason,
            }).execute()
        except Exception as db_err:
            logger.exception("fail_original_job DB call failed: %s", db_err)

    def _requeue(reason: str) -> None:
        backoff_minutes = 5 if attempts <= 1 else 30
        next_at = (datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes)).isoformat()
        try:
            supabase.rpc("requeue_original_job", {
                "p_job_id": job_id, "p_photo_id": photo_id,
                "p_last_error": reason, "p_next_attempt_at": next_at,
            }).execute()
        except Exception as db_err:
            logger.exception("requeue_original_job DB call failed: %s", db_err)

    # R2에 브라우저 원본이 존재하는지만 확인한다. 다운로드/재압축/재업로드는 하지 않는다.
    try:
        original_size = await loop.run_in_executor(_executor, _head_r2_object_sync, source_key)
    except KeyError:
        logger.warning("source not found for job %s key %s — failing immediately", job_id, source_key)
        _fail(f"source file not found in R2: {source_key}")
        return
    except Exception as e:
        logger.exception("R2 HEAD failed for job %s: %s", job_id, e)
        if attempts >= max_attempts:
            _fail(f"R2 download failed after {attempts} attempts: {e}")
        else:
            _requeue(f"R2 HEAD error: {e}")
        return

    # DB 완료 처리: source_key 자체가 보존 대상 원본이다.
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        supabase.rpc("complete_original_job", {
            "p_job_id": job_id,
            "p_photo_id": photo_id,
            "p_r2_original_url": source_key,
            "p_completed_at": now_iso,
            "p_file_size": original_size,
        }).execute()
    except Exception as e:
        logger.exception("complete_original_job DB failed for job %s: %s", job_id, e)
        if attempts >= max_attempts:
            _fail(f"DB update failed: {e}")
        else:
            _requeue(f"DB update error: {e}")
        return

    logger.info("[worker] completed original job=%s source_key=%s size=%d", job_id, source_key, original_size)

    # 고객 링크가 이미 열린 프로젝트라면 마지막 원본 완료 시점에도 ZIP 작업 등록을 다시
    # 시도한다. 링크 활성화 시점에는 일부 원본이 아직 처리 중일 수 있으므로, 그 한 번의
    # 시도만으로는 ZIP 작업이 영구히 누락될 수 있다. RPC는 모든 원본 완료 여부와 NULL →
    # pending 전환을 원자적으로 검사하므로 사진마다 호출해도 안전하다.
    try:
        supabase.rpc("enqueue_original_archive_build", {
            "p_project_id": project_id,
        }).execute()
    except Exception as e:
        # 원본 보존 완료 자체는 성공 처리한다. 아카이브 워커의 주기적 복구와 다음 원본 완료
        # 이벤트가 재시도할 수 있도록 오류만 기록한다.
        logger.exception("enqueue_original_archive_build failed for project %s: %s", project_id, e)


async def original_compress_worker() -> None:
    """startup 시 실행되는 비동기 원본 압축 worker. pending job을 폴링해 처리한다."""
    logger.info("original_compress_worker started (concurrency=%d)", ORIGINAL_COMPRESS_CONCURRENCY)
    while True:
        try:
            supabase = get_supabase()
            jobs_r = supabase.rpc("claim_original_job", {"p_limit": ORIGINAL_COMPRESS_CONCURRENCY}).execute()
            jobs = jobs_r.data or []
            if jobs:
                logger.info("[worker] claimed %d job(s)", len(jobs))
                await asyncio.gather(*[_process_original_job(j) for j in jobs], return_exceptions=True)
        except Exception as e:
            logger.exception("original_compress_worker cycle error: %s", e)
        await asyncio.sleep(5)


async def stuck_job_sweep_worker() -> None:
    """30분마다 processing stuck job을 복구하고 awaiting_upload 24h 초과 job을 처리한다."""
    logger.info("stuck_job_sweep_worker started")
    while True:
        await asyncio.sleep(1800)  # 30분
        try:
            supabase = get_supabase()
            # stuck processing 복구
            r = supabase.rpc("recover_stuck_original_jobs", {"p_stuck_minutes": 15, "p_next_attempt_minutes": 5}).execute()
            recovered = r.data or 0
            if recovered:
                logger.info("[sweep] recovered %d stuck processing job(s)", recovered)

            # awaiting_upload 24h 초과 → R2 HEAD 확인 후 pending or failed
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            old_awaiting_r = supabase.table("original_jobs")\
                .select("id,photo_id,r2_source_key")\
                .eq("status", "awaiting_upload")\
                .lt("created_at", cutoff)\
                .execute()
            for job in (old_awaiting_r.data or []):
                job_id = job["id"]
                photo_id = job["photo_id"]
                source_key = job["r2_source_key"]
                try:
                    await asyncio.get_event_loop().run_in_executor(_executor, _head_r2_object_sync, source_key)
                    # 파일 존재 → pending 승격
                    supabase.rpc("confirm_original_upload", {"p_job_id": job_id}).execute()
                    logger.info("[sweep] promoted awaiting job %s to pending (R2 file found)", job_id)
                except KeyError:
                    # 파일 없음 → failed
                    supabase.rpc("fail_original_job", {
                        "p_job_id": job_id, "p_photo_id": photo_id,
                        "p_last_error": "source file never uploaded (24h timeout)",
                    }).execute()
                    logger.warning("[sweep] failed awaiting job %s (R2 file not found after 24h)", job_id)
                except Exception as e:
                    logger.exception("[sweep] awaiting recovery error for job %s: %s", job_id, e)
        except Exception as e:
            logger.exception("stuck_job_sweep_worker error: %s", e)


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

    # full (1200px, 고정 품질 82%)
    full = img.copy()
    full.thumbnail((VERSION_MAX_SIZE, VERSION_MAX_SIZE), Image.Resampling.LANCZOS)
    full_buf = io.BytesIO()
    full.save(full_buf, format="JPEG", quality=VERSION_JPEG_QUALITY)

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
    try:
        photos_r = (
            supabase.table("photos")
            .select("id")
            .eq("project_id", project_id)
            .limit(1000)
            .execute()
        )
        photo_ids_list = [p["id"] for p in photos_r.data or []]
        existing_versions: set[int] = set()
        if photo_ids_list:
            pv_r = (
                supabase.table("photo_versions")
                .select("version")
                .in_("photo_id", photo_ids_list[:200])
                .execute()
            )
            existing_versions = {v["version"] for v in pv_r.data or []}
    except Exception as e:
        logger.error(f"에러내용: version beta check failed: {e}")
        raise HTTPException(status_code=500, detail=f"보정본 횟수 확인 실패: {e}") from e
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
