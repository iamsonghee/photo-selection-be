"""분석 파이프라인 오케스트레이션: DB 조회 -> 다운로드 -> 임베딩 -> 그룹핑 -> DB 기록."""
import asyncio
import logging
from datetime import datetime, timezone

from app import state
from app.clip_model import compute_embeddings
from app.config import CLIP_SIMILARITY_THRESHOLD, MAX_CONCURRENT_PROJECTS
from app.db import get_supabase
from app.downloader import download_all
from app.grouping import group_by_similarity

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

    urls = [r["r2_thumb_url"] for r in rows]
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

    groups = group_by_similarity(full_embeddings, CLIP_SIMILARITY_THRESHOLD)
    if not groups:
        logger.info("project_id=%s: no similarity groups found", project_id)
        return

    for member_indices in groups:
        photo_ids = [rows[i]["id"] for i in member_indices]
        vectors = [full_embeddings[i] for i in member_indices]
        avg_sim = _avg_pairwise_similarity(vectors)

        group_r = (
            supabase.table("photo_groups")
            .insert(
                {
                    "project_id": project_id,
                    "representative_photo_id": photo_ids[0],
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


def _avg_pairwise_similarity(vectors) -> float:
    import numpy as np

    if len(vectors) < 2:
        return 1.0
    sims = []
    for i in range(len(vectors) - 1):
        sims.append(float(np.dot(vectors[i], vectors[i + 1])))
    return round(sum(sims) / len(sims), 4)
