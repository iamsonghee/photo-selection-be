"""분석 파이프라인 오케스트레이션: DB 조회 -> 다운로드 -> 임베딩 -> 그룹핑 -> DB 기록."""
import asyncio
import logging
from datetime import datetime, timezone

from app import state
from app.clip_model import compute_embeddings
from app.config import CLIP_SIMILARITY_THRESHOLD, MAX_CONCURRENT_PROJECTS
from app.db import get_supabase
from app.downloader import download_all
from app.embeddings_store import persist_embeddings
from app.grouping import group_by_similarity
from app.quality import compute_quality_scores, pick_best_index

logger = logging.getLogger(__name__)

_project_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROJECTS)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run(project_id: str) -> None:
    """백그라운드 작업 진입점. 실패해도 예외를 삼키고 DB에 상태를 기록한다."""
    async with _project_semaphore:
        supabase = get_supabase()
        try:
            await _run_pipeline(supabase, project_id)
            (
                supabase.table("projects")
                .update(
                    {
                        "clip_analysis_status": "completed",
                        "clip_analysis_completed_at": _now_iso(),
                        "clip_analysis_error": None,
                    }
                )
                .eq("id", project_id)
                .execute()
            )
        except Exception as e:
            logger.exception("clip analysis failed for project_id=%s: %s", project_id, e)
            (
                supabase.table("projects")
                .update(
                    {
                        "clip_analysis_status": "failed",
                        "clip_analysis_completed_at": _now_iso(),
                        "clip_analysis_error": str(e)[:500],
                    }
                )
                .eq("id", project_id)
                .execute()
            )
        finally:
            state.finish(project_id)


async def _run_pipeline(supabase, project_id: str) -> None:
    project_r = (
        supabase.table("projects")
        .select("clip_analysis_last_number, clip_analysis_threshold")
        .eq("id", project_id)
        .limit(1)
        .execute()
    )
    project_row = (project_r.data or [{}])[0]
    last_number = project_row.get("clip_analysis_last_number")
    last_threshold = project_row.get("clip_analysis_threshold")

    photos_r = (
        supabase.table("photos")
        .select("id, number, r2_thumb_url")
        .eq("project_id", project_id)
        .order("number")
        .execute()
    )
    rows = photos_r.data or []
    rows = [r for r in rows if r.get("r2_thumb_url")]
    if len(rows) < 2:
        logger.info("project_id=%s has fewer than 2 photos with thumbnails, skipping", project_id)
        return

    current_max_number = rows[-1]["number"]

    # 증분 분석: 이전 분석 기준점(last_number)이 있고, 그 기준 사진이 삭제되지 않았고,
    # threshold도 그대로일 때만 새로 추가된 사진만 본다. 하나라도 어긋나면 안전하게 전체 재분석.
    threshold_changed = (
        last_threshold is not None and abs(float(last_threshold) - CLIP_SIMILARITY_THRESHOLD) > 1e-9
    )
    boundary_exists = last_number is not None and any(r["number"] == last_number for r in rows)
    can_incremental = last_number is not None and boundary_exists and not threshold_changed

    if can_incremental:
        target_rows = [r for r in rows if r["number"] > last_number]
        if not target_rows:
            logger.info(
                "project_id=%s: no new photos since last analysis (number<=%s), skipping",
                project_id, last_number,
            )
            _update_clip_progress(supabase, project_id, current_max_number)
            return
    else:
        target_rows = rows

    urls = [r["r2_thumb_url"] for r in target_rows]
    loop = asyncio.get_event_loop()
    images = await download_all(urls)

    embeddings = await loop.run_in_executor(
        None, compute_embeddings, [img for img in images if img is not None]
    )

    # download_all과 동일 순서 유지: None(다운로드 실패)을 임베딩 리스트에도 None으로 복원
    full_embeddings = []
    emb_iter = iter(embeddings)
    for img in images:
        full_embeddings.append(next(emb_iter) if img is not None else None)

    # 그룹 결과와 무관하게(싱글톤 포함) 분석 대상이 된 모든 사진의 임베딩을 영속화 —
    # 추후 보정본 CLIP 매칭(matcher.py)이 재계산 없이 재사용할 수 있도록.
    persist_embeddings(supabase, list(zip([r["id"] for r in target_rows], full_embeddings)))

    groups = group_by_similarity(full_embeddings, CLIP_SIMILARITY_THRESHOLD)
    if not groups:
        logger.info("project_id=%s: no similarity groups found", project_id)
        _update_clip_progress(supabase, project_id, current_max_number)
        return

    quality_scores = await loop.run_in_executor(None, compute_quality_scores, images)

    for member_indices in groups:
        photo_ids = [target_rows[i]["id"] for i in member_indices]
        vectors = [full_embeddings[i] for i in member_indices]
        avg_sim = _avg_pairwise_similarity(vectors)

        member_scores = [quality_scores[i] for i in member_indices]
        representative_photo_id = photo_ids[pick_best_index(member_scores)]

        group_r = (
            supabase.table("photo_groups")
            .insert(
                {
                    "project_id": project_id,
                    "representative_photo_id": representative_photo_id,
                    "photo_count": len(photo_ids),
                    "avg_similarity": avg_sim,
                }
            )
            .execute()
        )
        group_id = group_r.data[0]["id"]

        (
            supabase.table("photos")
            .update({"similarity_group_id": group_id})
            .in_("id", photo_ids)
            .execute()
        )

    _update_clip_progress(supabase, project_id, current_max_number)


def _update_clip_progress(supabase, project_id: str, max_number: int) -> None:
    """이번 분석이 다룬 범위를 기록 — 다음 실행이 어디서부터 증분으로 봐야 하는지 판단하는 기준점."""
    (
        supabase.table("projects")
        .update(
            {
                "clip_analysis_last_number": max_number,
                "clip_analysis_threshold": CLIP_SIMILARITY_THRESHOLD,
            }
        )
        .eq("id", project_id)
        .execute()
    )


def _avg_pairwise_similarity(vectors) -> float:
    import numpy as np

    if len(vectors) < 2:
        return 1.0
    sims = []
    for i in range(len(vectors) - 1):
        sims.append(float(np.dot(vectors[i], vectors[i + 1])))
    return round(sum(sims) / len(sims), 4)
