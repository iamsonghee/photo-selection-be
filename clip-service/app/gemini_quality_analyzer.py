"""Gemini Flash 품질 판정 파이프라인: DB 조회 -> 다운로드 -> Gemini Flash 판정 -> DB 기록.
Gemini Embedding(app/gemini_analyzer.py)·OpenCLIP(app/analyzer.py)과 완전히 독립 —
이 파이프라인이 실패해도 다른 두 분석에는 영향이 없다.

이미지 입력은 r2_preview_url(1200px)을 사용한다 — 눈감음/흔들림/초점처럼 미묘한 시각 신호는
Embedding에 쓰는 r2_thumb_url(300px)보다 해상도가 높을수록 판정 정확도가 오르고, 이미 존재하는
자산이라 추가 저장 비용이 없다(기존 OpenCV/MediaPipe는 300px 기준이라 직접 비교 시 해상도가
다르다는 점을 UI에서 명시한다 — compute_groups_with_quality 참고).
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from app import gemini_quality_state as state
from app.config import (
    GEMINI_FLASH_INPUT_PRICE_PER_1M,
    GEMINI_FLASH_MODEL,
    GEMINI_FLASH_OUTPUT_PRICE_PER_1M,
    GEMINI_QUALITY_PROMPT_VERSION,
)
from app.db import get_supabase
from app.downloader import download_all
from app.gemini_client import GeminiNotConfigured
from app.gemini_quality_client import assess_images
from app.gemini_quality_store import get_existing_photo_ids, persist_assessments
from app.memlog import log_perf, log_rss

logger = logging.getLogger(__name__)

# OpenCLIP/Gemini Embedding과는 별개의 동시성 한도 — 서로의 가드를 공유하지 않는다.
_project_semaphore = asyncio.Semaphore(2)


class _Cancelled(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run(run_id: str, project_id: str, limit: Optional[int], force: bool) -> None:
    """백그라운드 작업 진입점. 실패해도 예외를 삼키고 run 레코드에 상태를 기록한다."""
    async with _project_semaphore:
        supabase = get_supabase()
        t_pipe = time.perf_counter()
        try:
            await _run_pipeline(supabase, run_id, project_id, limit, force, t_pipe)
        except _Cancelled:
            logger.info("gemini quality analysis cancelled project_id=%s run_id=%s", project_id, run_id)
            (
                supabase.table("gemini_quality_runs")
                .update({"status": "failed", "error": "cancelled", "completed_at": _now_iso()})
                .eq("id", run_id)
                .execute()
            )
        except GeminiNotConfigured as e:
            logger.error("gemini quality analysis not configured project_id=%s: %s", project_id, e)
            (
                supabase.table("gemini_quality_runs")
                .update({"status": "failed", "error": str(e), "completed_at": _now_iso()})
                .eq("id", run_id)
                .execute()
            )
        except Exception as e:
            logger.exception(
                "gemini quality analysis failed project_id=%s run_id=%s: %s", project_id, run_id, e
            )
            (
                supabase.table("gemini_quality_runs")
                .update({"status": "failed", "error": str(e)[:500], "completed_at": _now_iso()})
                .eq("id", run_id)
                .execute()
            )
        finally:
            total = time.perf_counter() - t_pipe
            state.finish(project_id)
            state.clear_cancel(project_id)
            log_rss(f"gemini_quality_done:{project_id}")
            log_perf("gemini_quality_done", elapsed_sec=total, total_sec=total)


async def _run_pipeline(
    supabase, run_id: str, project_id: str, limit: Optional[int], force: bool, t_pipe: float
) -> None:
    photos_r = (
        supabase.table("photos")
        .select("id, number, r2_preview_url")
        .eq("project_id", project_id)
        .order("number")
        .execute()
    )
    rows = photos_r.data or []
    rows = [r for r in rows if r.get("r2_preview_url")]
    if limit is not None:
        rows = rows[:limit]

    if not rows:
        _finish_run(
            supabase, run_id, image_count=0, processed=0, failed=0, reused=0, cost=0.0, t_pipe=t_pipe
        )
        return

    photo_ids_all = [r["id"] for r in rows]

    # 동일 사진에 대한 불필요한 중복 호출 방지 — force가 아니면 같은 model+prompt_version 결과 재사용
    already: set[str] = set()
    if not force:
        already = get_existing_photo_ids(
            supabase, project_id, GEMINI_FLASH_MODEL, GEMINI_QUALITY_PROMPT_VERSION, photo_ids_all
        )
    target_rows = [r for r in rows if r["id"] not in already]

    if state.is_cancel_requested(project_id):
        raise _Cancelled()

    n_new = len(target_rows)
    n_ok = 0
    usages: list[dict] = []

    if n_new > 0:
        urls = [r["r2_preview_url"] for r in target_rows]
        log_perf("gemini_quality_download_start", n=n_new, total_sec=time.perf_counter() - t_pipe)
        t = time.perf_counter()
        images = await download_all(urls)
        n_dl_ok = sum(1 for img in images if img is not None)
        log_perf(
            "gemini_quality_download_done", elapsed_sec=time.perf_counter() - t,
            n=n_new, success_count=n_dl_ok, failure_count=n_new - n_dl_ok,
            total_sec=time.perf_counter() - t_pipe,
        )

        if state.is_cancel_requested(project_id):
            raise _Cancelled()

        log_perf("gemini_quality_assess_start", n=n_dl_ok, total_sec=time.perf_counter() - t_pipe)
        t = time.perf_counter()
        assessments, usages = await assess_images(images)
        n_ok = sum(1 for a in assessments if a is not None)
        log_perf(
            "gemini_quality_assess_done", elapsed_sec=time.perf_counter() - t,
            n=n_dl_ok, success_count=n_ok, failure_count=n_dl_ok - n_ok,
            total_sec=time.perf_counter() - t_pipe,
        )

        if state.is_cancel_requested(project_id):
            raise _Cancelled()

        log_perf("gemini_quality_db_start", n=n_ok, total_sec=time.perf_counter() - t_pipe)
        t = time.perf_counter()
        e_ok, e_fail, _ = persist_assessments(
            supabase, project_id, GEMINI_FLASH_MODEL, GEMINI_QUALITY_PROMPT_VERSION,
            list(zip([r["id"] for r in target_rows], assessments)),
        )
        log_perf(
            "gemini_quality_db_done", elapsed_sec=time.perf_counter() - t,
            n=e_ok + e_fail, success_count=e_ok, failure_count=e_fail,
            total_sec=time.perf_counter() - t_pipe,
        )

    failed_count = n_new - n_ok if n_new > 0 else 0
    cost = _compute_cost(usages)
    usage_metadata = _summarize_usage(usages) if usages else None

    _finish_run(
        supabase, run_id,
        image_count=len(rows),
        processed=len(already) + n_ok,
        failed=failed_count,
        reused=len(already),
        cost=cost,
        t_pipe=t_pipe,
        usage_metadata=usage_metadata,
    )


def _compute_cost(usages: list[dict]) -> float:
    total_in = sum((u.get("prompt_token_count") or 0) for u in usages)
    total_out = sum((u.get("candidates_token_count") or 0) for u in usages)
    return round(
        total_in / 1_000_000 * GEMINI_FLASH_INPUT_PRICE_PER_1M
        + total_out / 1_000_000 * GEMINI_FLASH_OUTPUT_PRICE_PER_1M,
        6,
    )


def _summarize_usage(usages: list[dict]) -> dict:
    return {
        "call_count": len(usages),
        "total_prompt_tokens": sum((u.get("prompt_token_count") or 0) for u in usages),
        "total_candidates_tokens": sum((u.get("candidates_token_count") or 0) for u in usages),
    }


def _finish_run(
    supabase, run_id: str, *, image_count: int, processed: int, failed: int, reused: int,
    cost: float, t_pipe: float, usage_metadata: Optional[dict] = None,
) -> None:
    (
        supabase.table("gemini_quality_runs")
        .update(
            {
                "status": "completed",
                "image_count": image_count,
                "processed_count": processed,
                "failed_count": failed,
                "reused_count": reused,
                "estimated_cost_usd": cost,
                "usage_metadata": usage_metadata,
                "completed_at": _now_iso(),
                "duration_ms": int((time.perf_counter() - t_pipe) * 1000),
            }
        )
        .eq("id", run_id)
        .execute()
    )
