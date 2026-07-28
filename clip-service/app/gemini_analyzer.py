"""Gemini 임베딩 분석 파이프라인: DB 조회 -> 다운로드 -> Gemini 임베딩 -> DB 기록.
OpenCLIP 파이프라인(app/analyzer.py)과 완전히 독립 — 이쪽이 실패해도 OpenCLIP 분석에 영향 없음.

블러/눈감음 판정, 대표컷 화질 선정은 POC 범위 밖(analyzer.py와 달리 없음).
그룹핑은 이미지를 새로 분석하지 않고 저장된 임베딩으로 즉시 재계산할 수 있도록
compute_groups()를 별도로 분리했다 — threshold 실험이 Gemini API 재호출 없이 가능하다.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from app import gemini_state as state
from app.config import (
    GEMINI_EMBEDDING_DIMENSION,
    GEMINI_EMBEDDING_MODEL,
    GEMINI_IMAGE_PRICE_USD,
)
from app.db import get_supabase
from app.downloader import download_all
from app.gemini_client import GeminiNotConfigured, embed_images
from app.gemini_embeddings_store import (
    fetch_embeddings_by_photo_id,
    get_existing_photo_ids,
    persist_embeddings,
)
from app.grouping import group_by_similarity
from app.memlog import log_perf, log_rss

logger = logging.getLogger(__name__)

# OpenCLIP(MAX_CONCURRENT_PROJECTS)과는 별개의 동시성 한도 — 서로의 가드를 공유하지 않는다.
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
            logger.info("gemini analysis cancelled project_id=%s run_id=%s", project_id, run_id)
            (
                supabase.table("gemini_analysis_runs")
                .update({"status": "failed", "error": "cancelled", "completed_at": _now_iso()})
                .eq("id", run_id)
                .execute()
            )
        except GeminiNotConfigured as e:
            logger.error("gemini analysis not configured project_id=%s: %s", project_id, e)
            (
                supabase.table("gemini_analysis_runs")
                .update({"status": "failed", "error": str(e), "completed_at": _now_iso()})
                .eq("id", run_id)
                .execute()
            )
        except Exception as e:
            logger.exception("gemini analysis failed project_id=%s run_id=%s: %s", project_id, run_id, e)
            (
                supabase.table("gemini_analysis_runs")
                .update({"status": "failed", "error": str(e)[:500], "completed_at": _now_iso()})
                .eq("id", run_id)
                .execute()
            )
        finally:
            total = time.perf_counter() - t_pipe
            state.finish(project_id)
            state.clear_cancel(project_id)
            log_rss(f"gemini_analyze_done:{project_id}")
            log_perf("gemini_analyze_done", elapsed_sec=total, total_sec=total)


async def _run_pipeline(
    supabase, run_id: str, project_id: str, limit: Optional[int], force: bool, t_pipe: float
) -> None:
    photos_r = (
        supabase.table("photos")
        .select("id, number, r2_thumb_url")
        .eq("project_id", project_id)
        .order("number")
        .execute()
    )
    rows = photos_r.data or []
    rows = [r for r in rows if r.get("r2_thumb_url")]
    if limit is not None:
        rows = rows[:limit]

    if len(rows) < 2:
        _finish_run(supabase, run_id, image_count=len(rows), processed=0, failed=0, cost=0.0, t_pipe=t_pipe)
        return

    photo_ids_all = [r["id"] for r in rows]

    # 동일 이미지에 대한 불필요한 중복 호출 방지 — force가 아니면 이미 저장된 임베딩은 재사용
    already: set[str] = set()
    if not force:
        already = get_existing_photo_ids(supabase, project_id, GEMINI_EMBEDDING_MODEL, photo_ids_all)
    target_rows = [r for r in rows if r["id"] not in already]

    if state.is_cancel_requested(project_id):
        raise _Cancelled()

    n_new = len(target_rows)
    n_emb_ok = 0
    e_fail = 0
    usages: list[dict] = []

    if n_new > 0:
        urls = [r["r2_thumb_url"] for r in target_rows]
        log_perf("gemini_download_start", n=n_new, total_sec=time.perf_counter() - t_pipe)
        t = time.perf_counter()
        images = await download_all(urls)
        n_dl_ok = sum(1 for img in images if img is not None)
        log_perf(
            "gemini_download_done", elapsed_sec=time.perf_counter() - t,
            n=n_new, success_count=n_dl_ok, failure_count=n_new - n_dl_ok,
            total_sec=time.perf_counter() - t_pipe,
        )

        if state.is_cancel_requested(project_id):
            raise _Cancelled()

        log_perf("gemini_embed_start", n=n_dl_ok, total_sec=time.perf_counter() - t_pipe)
        t = time.perf_counter()
        embeddings, usages = await embed_images(images)
        n_emb_ok = sum(1 for e in embeddings if e is not None)
        log_perf(
            "gemini_embed_done", elapsed_sec=time.perf_counter() - t,
            n=n_dl_ok, success_count=n_emb_ok, failure_count=n_dl_ok - n_emb_ok,
            total_sec=time.perf_counter() - t_pipe,
        )

        if state.is_cancel_requested(project_id):
            raise _Cancelled()

        log_perf("gemini_db_start", n=n_emb_ok, total_sec=time.perf_counter() - t_pipe)
        t = time.perf_counter()
        e_ok, e_fail, _ = persist_embeddings(
            supabase, project_id, GEMINI_EMBEDDING_MODEL, GEMINI_EMBEDDING_DIMENSION,
            list(zip([r["id"] for r in target_rows], embeddings)),
        )
        log_perf(
            "gemini_db_done", elapsed_sec=time.perf_counter() - t,
            n=e_ok + e_fail, success_count=e_ok, failure_count=e_fail,
            total_sec=time.perf_counter() - t_pipe,
        )

    failed_count = n_new - n_emb_ok if n_new > 0 else 0
    estimated_cost = round(n_new * GEMINI_IMAGE_PRICE_USD, 6)
    usage_metadata = {"sample_count": len(usages), "samples": usages[:5]} if usages else None

    _finish_run(
        supabase, run_id,
        image_count=len(rows),
        processed=len(already) + n_emb_ok,
        failed=failed_count,
        cost=estimated_cost,
        t_pipe=t_pipe,
        usage_metadata=usage_metadata,
    )


def _finish_run(
    supabase, run_id: str, *, image_count: int, processed: int, failed: int, cost: float,
    t_pipe: float, usage_metadata: Optional[dict] = None,
) -> None:
    (
        supabase.table("gemini_analysis_runs")
        .update(
            {
                "status": "completed",
                "image_count": image_count,
                "processed_count": processed,
                "failed_count": failed,
                "estimated_cost_usd": cost,
                "usage_metadata": usage_metadata,
                "completed_at": _now_iso(),
                "duration_ms": int((time.perf_counter() - t_pipe) * 1000),
            }
        )
        .eq("id", run_id)
        .execute()
    )


def compute_groups(supabase, project_id: str, threshold: float) -> dict:
    """저장된 임베딩으로 그룹핑만 재계산 — Gemini API를 다시 호출하지 않는다.
    threshold를 바꿔가며 여러 번 호출해도 추가 비용이 발생하지 않는다."""
    photos_r = (
        supabase.table("photos")
        .select("id, number")
        .eq("project_id", project_id)
        .order("number")
        .execute()
    )
    rows = photos_r.data or []
    photo_ids = [r["id"] for r in rows]
    emb_map = fetch_embeddings_by_photo_id(supabase, project_id, GEMINI_EMBEDDING_MODEL, photo_ids)
    if not emb_map:
        return {"groups": [], "analyzed_count": 0, "threshold": threshold}

    ordered = [(r["id"], emb_map.get(r["id"])) for r in rows]
    embeddings = [e for _, e in ordered]
    raw_groups = group_by_similarity(embeddings, threshold)

    groups = []
    for members in raw_groups:
        photo_id_list = [ordered[i][0] for i in members]
        vectors = [embeddings[i] for i in members]
        groups.append(
            {
                "photo_ids": photo_id_list,
                # POC 범위에서는 화질 기반 대표컷 선정을 하지 않음 — number 순 첫 사진을 대표로 표시
                "representative_photo_id": photo_id_list[0],
                "photo_count": len(photo_id_list),
                "avg_similarity": _avg_pairwise_similarity(vectors),
            }
        )

    return {"groups": groups, "analyzed_count": len(emb_map), "threshold": threshold}


def _avg_pairwise_similarity(vectors) -> float:
    if len(vectors) < 2:
        return 1.0
    sims = [float(np.dot(vectors[i], vectors[i + 1])) for i in range(len(vectors) - 1)]
    return round(sum(sims) / len(sims), 4)
