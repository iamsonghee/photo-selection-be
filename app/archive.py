"""납품용 원본 고객 다운로드 — ZIP 아카이브 비동기 생성 워커.

흐름: 원본 전체 완료(_maybe_enqueue_archive_build) → enqueue(NULL→pending)
  → claim(pending→processing) 후 파트 최초 생성(_create_parts_for_claimed_project)
  → 파트별 ZIP 빌드(_process_archive_part) → 전체 파트 완료 시 프로젝트 ready.

재시도("아카이브 다시 시도")는 신규 enqueue가 아니라 retry_archive_build RPC가 기존
failed 파트만 pending으로 되돌리는 방식이다 — original_archive_worker()는 파트
status='pending'을 폴링하므로, 최초 생성된 파트든 재시도로 되돌려진 파트든 동일한
경로(_process_archive_part)로 처리된다(별도 루프 없음, 하나의 폴링 루프로 일관 처리).
"""
import asyncio
import gc
import logging
import os
import re
import tempfile
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import get_supabase
from app.storage import (
    delete_r2_objects,
    get_r2_object_bytes_sync,
    head_r2_object_sync,
    r2_key_from_url,
    upload_local_file_to_r2,
)

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, min_v: int, max_v: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(min_v, min(max_v, v))


# 파트 최대 크기(bytes) — 기본 500MB. Railway 컨테이너 임시 디스크 실제 여유를 배포 전
# 확인 후 필요시 환경변수로 조정할 것(코드에서 2GB 등을 임의 확정하지 않음).
ARCHIVE_PART_MAX_BYTES = _env_int(
    "ARCHIVE_PART_MAX_BYTES", 500 * 1024 * 1024, 50 * 1024 * 1024, 5 * 1024 * 1024 * 1024
)
# original_compressed_size가 없는(레거시) 사진의 빈-패킹용 추정치
_FALLBACK_PHOTO_BYTES = 20 * 1024 * 1024
# 프로젝트/파트 claim 동시성 (기본 1 — Railway 512MB RAM 보호, 원본 압축 워커와 동일 기준)
ARCHIVE_BUILD_CONCURRENCY = _env_int("ARCHIVE_BUILD_CONCURRENCY", 1, 1, 4)
# ZIP은 파일을 순서대로 써야 하지만, R2 객체 요청 대기는 겹칠 수 있다. 기본 2개만
# 미리 받아 Railway 메모리를 과도하게 쓰지 않으면서 네트워크 왕복 대기를 줄인다.
ARCHIVE_DOWNLOAD_CONCURRENCY = _env_int("ARCHIVE_DOWNLOAD_CONCURRENCY", 2, 1, 4)

# 다운로드 만료(30일) + 유예(7일) 후 R2 아카이브 ZIP 삭제
ARCHIVE_RETENTION_DAYS = 30
ARCHIVE_GRACE_DAYS = 7

_executor = ThreadPoolExecutor(max_workers=2)


def _maybe_enqueue_archive_build(project_id: str) -> None:
    """원본 job 완료/최종실패 직후 호출 — 조건 미충족이면 조용히 no-op(멱등)."""
    try:
        supabase = get_supabase()
        supabase.rpc("enqueue_original_archive_build", {"p_project_id": project_id}).execute()
    except Exception as e:
        logger.exception("enqueue_original_archive_build failed for project %s: %s", project_id, e)


def _sanitize_arcname(filename: str) -> str:
    """ZIP 내부 파일명: 작가가 올린 원본 파일명을 유지한다."""
    base = re.sub(r'[/\\\x00-\x1f]', "_", filename or "photo.jpg").strip()
    if not base:
        base = "photo.jpg"
    return base


def _fetch_completed_originals_sync(project_id: str) -> list[dict]:
    """프로젝트의 완료된 원본 사진 목록(번호순) — bin-pack 입력. PostgREST 1000행 limit 우회."""
    supabase = get_supabase()
    rows: list[dict] = []
    for i in range(3):
        res = (
            supabase.table("photos")
            .select("id, number, r2_original_url, original_filename, original_compressed_size")
            .eq("project_id", project_id)
            .eq("original_status", "completed")
            .order("number")
            .range(i * 1000, (i + 1) * 1000 - 1)
            .execute()
        )
        page = res.data or []
        rows.extend(page)
        if len(page) < 1000:
            break
    return rows


def _bin_pack(photos: list[dict]) -> list[list[dict]]:
    """누적 크기 기준 그룹핑 — 한 그룹이 ARCHIVE_PART_MAX_BYTES를 넘지 않게(사진 1장은
    항상 자기 그룹에 담아 무한루프 없이 최소 1장씩은 진행됨)."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_size = 0
    for p in photos:
        size = p.get("original_compressed_size") or _FALLBACK_PHOTO_BYTES
        if current and current_size + size > ARCHIVE_PART_MAX_BYTES:
            groups.append(current)
            current = []
            current_size = 0
        current.append(p)
        current_size += size
    if current:
        groups.append(current)
    return groups


async def _create_parts_for_claimed_project(project: dict) -> None:
    """claim_original_archive_builds로 pending→processing claim된 프로젝트에 대해
    완료된 원본을 bin-pack해 original_archive_parts 행을 최초 생성한다.
    claim은 프로젝트당 단 하나의 워커만 성공하므로 경쟁자 없이 안전하게 insert 가능."""
    project_id = project["id"]
    supabase = get_supabase()
    loop = asyncio.get_event_loop()
    photos = await loop.run_in_executor(_executor, _fetch_completed_originals_sync, project_id)

    if not photos:
        # enqueue 조건(EXISTS completed)이 이미 걸러내므로 이례적인 경우 — 방어적으로 실패 확정
        logger.warning("[archive] claimed project %s has no completed originals — marking failed", project_id)
        supabase.table("projects").update({
            "original_archive_status": "failed",
            "original_archive_processing_started_at": None,
        }).eq("id", project_id).eq("original_archive_status", "processing").execute()
        return

    groups = _bin_pack(photos)
    rows = []
    for idx, group in enumerate(groups, start=1):
        byte_size = sum((p.get("original_compressed_size") or _FALLBACK_PHOTO_BYTES) for p in group)
        rows.append({
            "project_id": project_id,
            "part_number": idx,
            "r2_key": f"originals/archives/{project_id}/part-{idx}.zip",
            "file_count": len(group),
            "byte_size": byte_size,
            "manifest": [p["id"] for p in group],
            "status": "pending",
        })
    try:
        supabase.table("original_archive_parts").insert(rows).execute()
        logger.info("[archive] created %d part(s) for project %s (%d photos)", len(rows), project_id, len(photos))
    except Exception as e:
        logger.exception("[archive] part insert failed for project %s: %s", project_id, e)
        supabase.table("projects").update({
            "original_archive_status": "failed",
            "original_archive_processing_started_at": None,
        }).eq("id", project_id).eq("original_archive_status", "processing").execute()


def _download_and_zip_sync(manifest_photo_ids: list[str]) -> tuple[str, int]:
    """동기: manifest의 photo_id들을 재조회해 원본을 ZIP으로 기록한다.

    ZIP 기록 순서는 manifest 그대로 유지하고, 제한된 개수의 다음 R2 다운로드만 미리
    시작한다. 전체 ZIP은 디스크에 스트리밍되며, 메모리에 머무는 원본은 최대
    ARCHIVE_DOWNLOAD_CONCURRENCY개다. 반환: (임시파일 경로, 실제 담긴 파일 수).
    """
    supabase = get_supabase()
    rows: list[dict] = []
    for i in range(0, len(manifest_photo_ids), 500):
        chunk = manifest_photo_ids[i : i + 500]
        res = (
            supabase.table("photos")
            .select("id, number, r2_original_url, original_filename")
            .in_("id", chunk)
            .execute()
        )
        rows.extend(res.data or [])
    by_id = {r["id"]: r for r in rows}

    entries: list[tuple[dict, str]] = []
    for pid in manifest_photo_ids:
        row = by_id.get(pid)
        if not row or not row.get("r2_original_url"):
            logger.warning("[archive] photo %s missing/no original — skipped from zip", pid)
            continue
        original_ref = row["r2_original_url"]
        # 신규 원본 보존 경로는 DB에 R2 key를 직접 저장한다. 레거시 압축본은
        # 공개 URL로 저장돼 있어 두 형태를 모두 읽는다.
        key = original_ref if original_ref.startswith("originals/") else r2_key_from_url(original_ref)
        entries.append((row, key))

    fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="archive_part_")
    os.close(fd)
    count = 0
    try:
        used_names: set[str] = set()
        # 다음 몇 장을 미리 요청하되, ZIP 기록은 entries 순서를 지켜 기존 결과와 동일하다.
        with ThreadPoolExecutor(max_workers=ARCHIVE_DOWNLOAD_CONCURRENCY) as prefetch, \
             zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zf:
            futures: dict[int, Future[bytes]] = {}
            next_to_schedule = 0
            while next_to_schedule < min(ARCHIVE_DOWNLOAD_CONCURRENCY, len(entries)):
                futures[next_to_schedule] = prefetch.submit(get_r2_object_bytes_sync, entries[next_to_schedule][1])
                next_to_schedule += 1
            for index, (row, _key) in enumerate(entries):
                data = futures.pop(index).result()
                if next_to_schedule < len(entries):
                    futures[next_to_schedule] = prefetch.submit(get_r2_object_bytes_sync, entries[next_to_schedule][1])
                    next_to_schedule += 1
                arcname = _sanitize_arcname(row.get("original_filename") or "photo.jpg")
                # 같은 파일명이 여러 장이면 ZIP 엔트리 충돌을 막되, 일반적인 경우에는
                # 작가가 등록한 파일명을 한 글자도 바꾸지 않는다.
                if arcname in used_names:
                    stem, ext = os.path.splitext(arcname)
                    suffix = 2
                    candidate = f"{stem} ({suffix}){ext}"
                    while candidate in used_names:
                        suffix += 1
                        candidate = f"{stem} ({suffix}){ext}"
                    arcname = candidate
                used_names.add(arcname)
                zf.writestr(arcname, data)
                count += 1
                del data
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path, count


async def _process_archive_part(part: dict) -> None:
    """original_archive_parts 행 1개: ZIP 빌드 → R2 업로드 → HEAD verify → 완료 처리."""
    part_id: str = part["id"]
    project_id: str = part["project_id"]
    attempts: int = part["attempts"]
    max_attempts: int = part["max_attempts"]
    manifest: list[str] = part.get("manifest") or []
    r2_key: str = part["r2_key"]

    supabase = get_supabase()
    loop = asyncio.get_event_loop()

    def _fail(reason: str) -> None:
        permanent = attempts >= max_attempts
        try:
            supabase.rpc("fail_archive_part", {
                "p_part_id": part_id,
                "p_project_id": project_id,
                "p_last_error": reason,
                "p_permanent": permanent,
            }).execute()
        except Exception as db_err:
            logger.exception("fail_archive_part RPC failed for part %s: %s", part_id, db_err)

    def _part_is_still_processing_sync() -> bool:
        """전체 삭제로 취소된 파트는 ZIP 업로드/완료 처리 전에 중단한다."""
        result = (
            supabase.table("original_archive_parts")
            .select("id")
            .eq("id", part_id)
            .eq("status", "processing")
            .limit(1)
            .execute()
        )
        return bool(result.data)

    async def _part_is_still_processing() -> bool:
        return await loop.run_in_executor(_executor, _part_is_still_processing_sync)

    tmp_path: Optional[str] = None
    try:
        tmp_path, count = await loop.run_in_executor(_executor, _download_and_zip_sync, manifest)
        logger.info("[archive] zip built part=%s files=%d path=%s", part_id, count, tmp_path)
    except Exception as e:
        logger.exception("[archive] zip build failed for part %s: %s", part_id, e)
        _fail(f"zip build failed: {e}")
        return

    try:
        # 작가가 preparing 단계에서 전체 삭제한 경우, 이미 내려받은 ZIP도 업로드하지 않는다.
        if not await _part_is_still_processing():
            logger.info("[archive] part=%s cancelled before upload", part_id)
            return

        try:
            await loop.run_in_executor(_executor, upload_local_file_to_r2, r2_key, tmp_path, "application/zip")
        except Exception as e:
            logger.exception("[archive] R2 upload failed for part %s: %s", part_id, e)
            _fail(f"R2 upload failed: {e}")
            return

        # 업로드 중 취소된 아주 짧은 경합도 R2 ZIP을 바로 회수한다.
        if not await _part_is_still_processing():
            logger.info("[archive] part=%s cancelled after upload", part_id)
            await loop.run_in_executor(_executor, delete_r2_objects, [r2_key])
            return
    except Exception as e:
        logger.exception("[archive] cancellation check failed for part %s: %s", part_id, e)
        _fail(f"cancellation check failed: {e}")
        return
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        gc.collect()

    try:
        await loop.run_in_executor(_executor, head_r2_object_sync, r2_key)
    except Exception as e:
        logger.exception("[archive] HEAD verify failed for part %s key %s: %s", part_id, r2_key, e)
        _fail(f"HEAD verify failed: {e}")
        return

    try:
        if not await _part_is_still_processing():
            logger.info("[archive] part=%s cancelled before completion", part_id)
            await loop.run_in_executor(_executor, delete_r2_objects, [r2_key])
            return
    except Exception as e:
        logger.exception("[archive] cancellation check failed for part %s: %s", part_id, e)
        _fail(f"cancellation check failed: {e}")
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        supabase.rpc("complete_archive_part", {
            "p_part_id": part_id,
            "p_project_id": project_id,
            "p_completed_at": now_iso,
        }).execute()
    except Exception as e:
        logger.exception("[archive] complete_archive_part failed for part %s: %s", part_id, e)
        _fail(f"DB completion failed: {e}")
        return

    # 고객 링크가 ZIP 완료 전 먼저 열렸다면, 다운로드 30일 기산은 실제 원본 준비 완료
    # 시점부터 시작한다. DB 마이그레이션 적용 전에도 이 보완 경로로 누락을 막는다.
    try:
        supabase.table("projects").update({
            "original_download_started_at": now_iso,
        }).eq("id", project_id).eq("status", "selecting").eq(
            "original_archive_status", "ready"
        ).is_("original_download_started_at", "null").execute()
    except Exception as e:
        logger.exception("[archive] download window start failed for project %s: %s", project_id, e)

    logger.info("[archive] completed part=%s project=%s key=%s", part_id, project_id, r2_key)


async def original_archive_worker() -> None:
    """startup 시 실행되는 단일 폴링 루프.
    매 사이클: (1) pending 프로젝트 claim → 파트 최초 생성  (2) pending 파트 claim → zip 빌드.
    재시도로 되돌려진 파트도 (2)의 status='pending' 폴링에 자연히 포함되므로 별도 루프 불필요."""
    logger.info(
        "original_archive_worker started (concurrency=%d, part_max_bytes=%d)",
        ARCHIVE_BUILD_CONCURRENCY, ARCHIVE_PART_MAX_BYTES,
    )
    while True:
        try:
            supabase = get_supabase()

            claimed_r = supabase.rpc(
                "claim_original_archive_builds", {"p_limit": ARCHIVE_BUILD_CONCURRENCY}
            ).execute()
            claimed_projects = claimed_r.data or []
            for project in claimed_projects:
                await _create_parts_for_claimed_project(project)

            parts_r = supabase.rpc(
                "claim_original_archive_parts", {"p_limit": ARCHIVE_BUILD_CONCURRENCY}
            ).execute()
            parts = parts_r.data or []
            if parts:
                logger.info("[archive] claimed %d part(s)", len(parts))
                await asyncio.gather(*[_process_archive_part(p) for p in parts], return_exceptions=True)
        except Exception as e:
            logger.exception("original_archive_worker cycle error: %s", e)
        await asyncio.sleep(5)


def _bin_pack_delivery(entries: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_size = 0
    for entry in entries:
        size = int(entry.get("byte_size") or _FALLBACK_PHOTO_BYTES)
        if current and current_size + size > ARCHIVE_PART_MAX_BYTES:
            groups.append(current)
            current, current_size = [], 0
        current.append(entry)
        current_size += size
    if current:
        groups.append(current)
    return groups


async def _create_final_delivery_parts(archive: dict) -> None:
    """검토 시작 시 고정된 manifest를 파트로 나눈다. 이후 V2 업로드/교체와 무관하다."""
    archive_id = archive["id"]
    project_id = archive["project_id"]
    manifest = archive.get("manifest") or []
    supabase = get_supabase()
    if not manifest:
        supabase.table("final_delivery_archives").update({
            "status": "failed", "last_error": "empty manifest", "processing_started_at": None,
        }).eq("id", archive_id).eq("status", "processing").execute()
        return
    rows = []
    for idx, group in enumerate(_bin_pack_delivery(manifest), start=1):
        rows.append({
            "archive_id": archive_id,
            "project_id": project_id,
            "part_number": idx,
            "r2_key": f"versions/delivery-archives/{project_id}/{archive_id}/part-{idx}.zip",
            "manifest": group,
            "file_count": len(group),
            "byte_size": sum(int(item.get("byte_size") or 0) for item in group),
            "status": "pending",
        })
    try:
        supabase.table("final_delivery_archive_parts").insert(rows).execute()
    except Exception as e:
        logger.exception("[final archive] part insert failed archive=%s: %s", archive_id, e)
        supabase.table("final_delivery_archives").update({
            "status": "failed", "last_error": str(e), "processing_started_at": None,
        }).eq("id", archive_id).eq("status", "processing").execute()


def _download_and_zip_delivery_sync(manifest: list[dict]) -> tuple[str, int]:
    entries = [(entry, str(entry.get("key") or "")) for entry in manifest if entry.get("key")]
    fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="final_delivery_")
    os.close(fd)
    count = 0
    try:
        used_names: set[str] = set()
        with ThreadPoolExecutor(max_workers=ARCHIVE_DOWNLOAD_CONCURRENCY) as prefetch, \
             zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zf:
            futures: dict[int, Future[bytes]] = {}
            next_to_schedule = 0
            while next_to_schedule < min(ARCHIVE_DOWNLOAD_CONCURRENCY, len(entries)):
                futures[next_to_schedule] = prefetch.submit(get_r2_object_bytes_sync, entries[next_to_schedule][1])
                next_to_schedule += 1
            for index, (entry, _key) in enumerate(entries):
                data = futures.pop(index).result()
                if next_to_schedule < len(entries):
                    futures[next_to_schedule] = prefetch.submit(get_r2_object_bytes_sync, entries[next_to_schedule][1])
                    next_to_schedule += 1
                arcname = _sanitize_arcname(entry.get("filename") or "photo.jpg")
                if arcname in used_names:
                    stem, ext = os.path.splitext(arcname)
                    suffix = 2
                    while f"{stem} ({suffix}){ext}" in used_names:
                        suffix += 1
                    arcname = f"{stem} ({suffix}){ext}"
                used_names.add(arcname)
                zf.writestr(arcname, data)
                count += 1
                del data
    except Exception:
        try: os.remove(tmp_path)
        except OSError: pass
        raise
    return tmp_path, count


async def _process_final_delivery_part(part: dict) -> None:
    part_id, archive_id = part["id"], part["archive_id"]
    r2_key = part["r2_key"]
    supabase = get_supabase()
    loop = asyncio.get_event_loop()

    def fail(reason: str) -> None:
        supabase.rpc("fail_final_delivery_archive_part", {
            "p_part_id": part_id, "p_archive_id": archive_id,
            "p_last_error": reason, "p_permanent": part["attempts"] >= part["max_attempts"],
        }).execute()

    def active() -> bool:
        result = (supabase.table("final_delivery_archive_parts").select("id,final_delivery_archives!inner(status)")
                  .eq("id", part_id).eq("status", "processing")
                  .eq("final_delivery_archives.status", "processing").limit(1).execute())
        return bool(result.data)

    tmp_path: Optional[str] = None
    try:
        tmp_path, count = await loop.run_in_executor(_executor, _download_and_zip_delivery_sync, part.get("manifest") or [])
        if count != int(part.get("file_count") or 0) or not await loop.run_in_executor(_executor, active):
            if count != int(part.get("file_count") or 0): fail("manifest file count mismatch")
            return
        await loop.run_in_executor(_executor, upload_local_file_to_r2, r2_key, tmp_path, "application/zip")
        await loop.run_in_executor(_executor, head_r2_object_sync, r2_key)
        if not await loop.run_in_executor(_executor, active):
            await loop.run_in_executor(_executor, delete_r2_objects, [r2_key])
            return
        supabase.rpc("complete_final_delivery_archive_part", {
            "p_part_id": part_id, "p_archive_id": archive_id,
        }).execute()
    except Exception as e:
        logger.exception("[final archive] part failed id=%s: %s", part_id, e)
        try: fail(str(e))
        except Exception: logger.exception("[final archive] failure update failed id=%s", part_id)
    finally:
        if tmp_path:
            try: os.remove(tmp_path)
            except OSError: pass
        gc.collect()


async def final_delivery_archive_worker() -> None:
    logger.info("final_delivery_archive_worker started")
    while True:
        try:
            supabase = get_supabase()
            archives = supabase.rpc("claim_final_delivery_archives", {
                "p_limit": ARCHIVE_BUILD_CONCURRENCY,
            }).execute().data or []
            for archive in archives:
                await _create_final_delivery_parts(archive)
            parts = supabase.rpc("claim_final_delivery_archive_parts", {
                "p_limit": ARCHIVE_BUILD_CONCURRENCY,
            }).execute().data or []
            if parts:
                await asyncio.gather(*[_process_final_delivery_part(part) for part in parts], return_exceptions=True)
        except Exception as e:
            # DB migration보다 BE가 먼저 배포된 짧은 구간도 기존 워커를 방해하지 않는다.
            logger.debug("final_delivery_archive_worker cycle skipped: %s", e)
        await asyncio.sleep(5)


async def _cleanup_expired_archive_parts() -> None:
    """다운로드 만료(30일) + 유예(7일) = 37일 경과한 프로젝트의 완료된 아카이브 ZIP을 R2에서 삭제.
    삭제는 멱등(존재하지 않는 key 삭제도 에러 없이 통과) — 실패 시 deleted_at을 남기지 않아
    다음 30분 주기 스윕이 자연히 재시도한다."""
    supabase = get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ARCHIVE_RETENTION_DAYS + ARCHIVE_GRACE_DAYS)).isoformat()
    projects_r = (
        supabase.table("projects")
        .select("id")
        .lt("original_download_started_at", cutoff)
        .execute()
    )
    project_ids = [p["id"] for p in projects_r.data or []]
    if not project_ids:
        return

    loop = asyncio.get_event_loop()
    deleted_total = 0
    for i in range(0, len(project_ids), 100):
        chunk = project_ids[i : i + 100]
        parts_r = (
            supabase.table("original_archive_parts")
            .select("id, r2_key")
            .in_("project_id", chunk)
            .eq("status", "completed")
            .is_("deleted_at", "null")
            .execute()
        )
        for part in (parts_r.data or []):
            try:
                await loop.run_in_executor(_executor, delete_r2_objects, [part["r2_key"]])
                supabase.table("original_archive_parts").update({
                    "deleted_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", part["id"]).execute()
                deleted_total += 1
            except Exception as e:
                logger.warning(
                    "[archive cleanup] delete failed for part %s (retry next sweep): %s", part["id"], e
                )
    if deleted_total:
        logger.info("[archive cleanup] deleted %d expired archive part(s)", deleted_total)


async def _cleanup_final_delivery_archives() -> None:
    """재보정으로 폐기된 후보와 최종 납품 37일 경과 ZIP을 정리한다."""
    supabase = get_supabase()
    obsolete_r = supabase.table("final_delivery_archives").select("id").eq("status", "obsolete").execute()
    archive_ids = {row["id"] for row in (obsolete_r.data or [])}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ARCHIVE_RETENTION_DAYS + ARCHIVE_GRACE_DAYS)).isoformat()
    projects_r = (supabase.table("projects").select("active_final_delivery_archive_id")
                  .lt("delivered_at", cutoff).execute())
    archive_ids.update(
        row["active_final_delivery_archive_id"] for row in (projects_r.data or [])
        if row.get("active_final_delivery_archive_id")
    )
    if not archive_ids:
        return
    loop = asyncio.get_event_loop()
    for start in range(0, len(archive_ids), 100):
        chunk = list(archive_ids)[start:start + 100]
        parts_r = (supabase.table("final_delivery_archive_parts").select("id,r2_key")
                   .in_("archive_id", chunk).eq("status", "completed").is_("deleted_at", "null").execute())
        for part in (parts_r.data or []):
            try:
                await loop.run_in_executor(_executor, delete_r2_objects, [part["r2_key"]])
                supabase.table("final_delivery_archive_parts").update({
                    "deleted_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", part["id"]).execute()
            except Exception as e:
                logger.warning("[final archive cleanup] part=%s: %s", part["id"], e)


async def archive_sweep_worker() -> None:
    """30분마다: 아카이브 고착 복구(프로젝트 단위 + 파트 단위) + 만료 아카이브 정리."""
    logger.info("archive_sweep_worker started")
    while True:
        await asyncio.sleep(1800)
        try:
            supabase = get_supabase()
            r1 = supabase.rpc("recover_stuck_original_archive_builds", {"p_stuck_minutes": 15}).execute()
            if r1.data:
                logger.info("[archive sweep] recovered %d stuck build(s) (no parts yet)", r1.data)
            # part 하나의 다운로드+ZIP+업로드는 파트 크기(ARCHIVE_PART_MAX_BYTES)에 비례해
            # 길어질 수 있어, 빌드 claim(15분)보다 여유 있는 임계값을 둔다.
            r2 = supabase.rpc("recover_stuck_original_archive_parts", {"p_stuck_minutes": 45}).execute()
            if r2.data:
                logger.info("[archive sweep] recovered %d stuck part(s)", r2.data)
            supabase.rpc("recover_stuck_final_delivery_archives", {"p_stuck_minutes": 15}).execute()
            supabase.rpc("recover_stuck_final_delivery_parts", {"p_stuck_minutes": 45}).execute()
        except Exception as e:
            logger.exception("archive_sweep_worker recovery error: %s", e)

        try:
            await _cleanup_expired_archive_parts()
            await _cleanup_final_delivery_archives()
        except Exception as e:
            logger.exception("archive_sweep_worker cleanup error: %s", e)
