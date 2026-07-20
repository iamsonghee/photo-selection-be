"""분석 파이프라인 오케스트레이션: DB 조회 -> 다운로드 -> 임베딩 -> 그룹핑 -> DB 기록."""
import asyncio
import logging
from datetime import datetime, timezone

import numpy as np

from app import state
from app.clip_model import compute_embeddings
from app.config import (
    BLUR_VARIANCE_THRESHOLD,
    CLIP_SIMILARITY_THRESHOLD,
    EYE_AR_THRESHOLD,
    MAX_CONCURRENT_PROJECTS,
)
from app.db import get_supabase
from app.downloader import download_all
from app.embeddings_store import persist_embeddings
from app.eyes import compute_eye_flags
from app.grouping import group_by_similarity
from app.quality import compute_blur_flags, compute_quality_scores, pick_best_index
from app.quality_store import persist_quality_flags

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

    # 배치 경계 스티칭: 증분 분석은 target_rows(신규 사진)끼리만 비교하므로, 직전 배치의
    # 마지막 사진(경계)과 이번 배치 첫 사진이 실제로는 연속 촬영본이어도 어느 실행에서도
    # 비교되지 않아 영구히 그룹화되지 않는 문제가 있었다. 경계 사진의 임베딩은 이전 분석 때
    # 이미 저장돼 있으므로(persist_embeddings), 재다운로드/재계산 없이 그대로 비교에 포함한다.
    boundary_row: dict | None = None
    boundary_embedding: np.ndarray | None = None
    if can_incremental:
        target_rows = [r for r in rows if r["number"] > last_number]
        if not target_rows:
            logger.info(
                "project_id=%s: no new photos since last analysis (number<=%s), skipping",
                project_id, last_number,
            )
            _update_clip_progress(supabase, project_id, current_max_number)
            return

        boundary_r = (
            supabase.table("photos")
            .select("id, r2_thumb_url, clip_embedding, similarity_group_id")
            .eq("project_id", project_id)
            .eq("number", last_number)
            .limit(1)
            .execute()
        )
        boundary_data = (boundary_r.data or [None])[0]
        if boundary_data and boundary_data.get("clip_embedding"):
            boundary_row = boundary_data
            boundary_embedding = np.array(boundary_data["clip_embedding"], dtype=np.float32)
    else:
        target_rows = rows

    urls = [r["r2_thumb_url"] for r in target_rows]
    loop = asyncio.get_event_loop()
    images = await download_all(urls)

    # 흔들림/눈감음 경고 배지: 그룹핑 결과(싱글톤 포함 여부)와 무관하게 이번에 분석 대상이 된
    # 모든 사진에 대해 계산·저장한다. 아래 그룹핑 로직처럼 "그룹이 없으면 조기 return"에
    # 걸리지 않도록 반드시 그 이전에 실행해야 한다.
    blur_flags = await loop.run_in_executor(None, compute_blur_flags, images, BLUR_VARIANCE_THRESHOLD)
    eye_flags = await loop.run_in_executor(None, compute_eye_flags, images, EYE_AR_THRESHOLD)
    quality_flags = [
        (blur_variance, is_blurry, face_detected, eyes_closed)
        for (blur_variance, is_blurry), (face_detected, eyes_closed) in zip(blur_flags, eye_flags)
    ]
    persist_quality_flags(
        supabase, list(zip([r["id"] for r in target_rows], quality_flags))
    )

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

    # 경계 사진이 있으면 인덱스 0에 붙여서 함께 그룹핑하고, 결과에서 경계가 포함된 연결
    # 요소만 따로 떼어내 병합/신규생성 케이스로 처리한다. grouping.py 자체는 변경 없음 —
    # "인덱스 0이 경계"라는 의미를 몰라도 인접 비교 + union-find 결과만 돌려주면 된다.
    boundary_offset = 1 if boundary_embedding is not None else 0
    grouping_input = ([boundary_embedding] + full_embeddings) if boundary_offset else full_embeddings
    raw_groups = group_by_similarity(grouping_input, CLIP_SIMILARITY_THRESHOLD)

    new_groups: list[list[int]] = []
    boundary_new_members: list[int] | None = None
    for members in raw_groups:
        if boundary_offset and 0 in members:
            boundary_new_members = [m - boundary_offset for m in members if m != 0]
        else:
            new_groups.append([m - boundary_offset for m in members])

    if not new_groups and not boundary_new_members:
        logger.info("project_id=%s: no similarity groups found", project_id)
        _update_clip_progress(supabase, project_id, current_max_number)
        return

    quality_scores = await loop.run_in_executor(None, compute_quality_scores, images)

    for member_indices in new_groups:
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

    if boundary_new_members:
        await _stitch_boundary_group(
            supabase, project_id, boundary_row, boundary_new_members, target_rows,
            full_embeddings, quality_scores, loop,
        )

    _update_clip_progress(supabase, project_id, current_max_number)


async def _stitch_boundary_group(
    supabase, project_id: str, boundary_row: dict, new_member_indices: list[int],
    target_rows: list[dict], full_embeddings: list, quality_scores: list, loop,
) -> None:
    """배치 경계를 넘어 새로 연결된 사진들을 처리.
    경계 사진이 이미 그룹에 속해 있었으면 그 그룹에 편입(대표컷 유지), 아니었으면
    경계 사진을 포함한 신규 그룹을 만든다(대표컷은 이번에 새로 선정)."""
    new_photo_ids = [target_rows[i]["id"] for i in new_member_indices]
    existing_group_id = boundary_row.get("similarity_group_id")

    if existing_group_id:
        group_r = (
            supabase.table("photo_groups")
            .select("photo_count")
            .eq("id", existing_group_id)
            .limit(1)
            .execute()
        )
        current_count = (group_r.data or [{"photo_count": 1}])[0].get("photo_count", 1)
        (
            supabase.table("photos")
            .update({"similarity_group_id": existing_group_id})
            .in_("id", new_photo_ids)
            .execute()
        )
        (
            supabase.table("photo_groups")
            .update({"photo_count": current_count + len(new_photo_ids)})
            .eq("id", existing_group_id)
            .execute()
        )
        return

    # 경계 사진이 그룹에 속한 적 없던 싱글톤 — 경계 사진 포함 신규 그룹 생성.
    # 대표컷 선정에 필요한 화질 점수는 이번 분석 대상(target_rows)에 없으므로,
    # 이번에 한해 경계 사진 썸네일 1장만 온디맨드로 다운로드해 계산한다.
    boundary_images = await download_all([boundary_row["r2_thumb_url"]])
    boundary_score = (await loop.run_in_executor(None, compute_quality_scores, boundary_images))[0]

    all_photo_ids = [boundary_row["id"]] + new_photo_ids
    member_scores = [boundary_score] + [quality_scores[i] for i in new_member_indices]
    vectors = [np.array(boundary_row["clip_embedding"], dtype=np.float32)] + [
        full_embeddings[i] for i in new_member_indices
    ]
    avg_sim = _avg_pairwise_similarity(vectors)
    representative_photo_id = all_photo_ids[pick_best_index(member_scores)]

    group_r = (
        supabase.table("photo_groups")
        .insert(
            {
                "project_id": project_id,
                "representative_photo_id": representative_photo_id,
                "photo_count": len(all_photo_ids),
                "avg_similarity": avg_sim,
            }
        )
        .execute()
    )
    group_id = group_r.data[0]["id"]

    (
        supabase.table("photos")
        .update({"similarity_group_id": group_id})
        .in_("id", all_photo_ids)
        .execute()
    )


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
    if len(vectors) < 2:
        return 1.0
    sims = []
    for i in range(len(vectors) - 1):
        sims.append(float(np.dot(vectors[i], vectors[i + 1])))
    return round(sum(sims) / len(sims), 4)
