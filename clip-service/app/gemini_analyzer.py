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
    GEMINI_EMBEDDING_VERSION,
    GEMINI_FLASH_MODEL,
    GEMINI_IMAGE_PRICE_USD,
    GEMINI_QUALITY_PROMPT_VERSION,
    GEMINI_SIMILARITY_THRESHOLD,
)
from app.db import get_supabase
from app.downloader import download_all
from app.gemini_client import GeminiNotConfigured, embed_images
from app.gemini_embeddings_store import (
    extract_object_key,
    fetch_embeddings_by_photo_id,
    get_cached_photo_ids,
    persist_embeddings,
)
from app.gemini_quality_store import fetch_quality_by_photo_id
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
            # 베타 화면(작가 그리드)과 고객 갤러리가 모두 읽는 photo_groups/similarity_group_id를
            # 항상 베타 기본 threshold로 최신화한다 — 관리자 POC의 threshold 슬라이더 실험과는
            # 무관(그쪽은 compute_groups()를 직접 호출할 뿐 여기로 쓰지 않는다).
            sync_groups_to_db(supabase, project_id, GEMINI_SIMILARITY_THRESHOLD)
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

    photo_id_to_key = {r["id"]: extract_object_key(r.get("r2_thumb_url")) for r in rows}

    # 동일 이미지에 대한 불필요한 중복 호출 방지 — force가 아니면 이미 저장된 임베딩은 재사용.
    # model+dimension+embedding_version이 전부 일치하고(설정 변경 시 자동으로 재분석 대상이 됨),
    # object key까지 확인 가능하면 실제 파일이 같은지도 함께 검증한다.
    already: set[str] = set()
    if not force:
        already = get_cached_photo_ids(
            supabase, project_id, GEMINI_EMBEDDING_MODEL, GEMINI_EMBEDDING_DIMENSION,
            GEMINI_EMBEDDING_VERSION, photo_id_to_key,
        )
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
            GEMINI_EMBEDDING_VERSION,
            [
                (r["id"], vec, photo_id_to_key.get(r["id"]))
                for r, vec in zip(target_rows, embeddings)
            ],
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


def compute_groups(supabase, project_id: str, threshold: float, include_quality: bool = False) -> dict:
    """저장된 임베딩으로 그룹핑만 재계산 — Gemini API를 다시 호출하지 않는다.
    threshold를 바꿔가며 여러 번 호출해도 추가 비용이 발생하지 않는다.

    include_quality=True일 때만 저장된 Gemini Flash 품질 판정을 곁들여 품질 반영 추천 이미지
    (recommended_photo_id)를 계산한다 — 이 결합도 순수 조회+계산이라 Flash API를 호출하지 않는다.
    기본값 False: 베타 경로(작가 화면/고객 갤러리에 반영되는 sync_groups_to_db 등)는 이 값을 넘기지
    않아 품질 데이터를 아예 조회하지 않으며, recommended_photo_id는 항상 medoid 대표 이미지
    (representative_photo_id)와 동일하게 나온다 — "품질 분석 결과를 대표 이미지 선정에 반영하지
    않는다"는 베타 요구사항을 코드 구조로 보장한다. 관리자 POC(GeminiAnalysisPanel)만
    include_quality=True를 명시적으로 넘기며, 그 경로는 Next.js 프록시 라우트의 isAdminEmail
    세션 검증을 통과해야만 도달 가능하다(clip-service 자체는 사용자 신원 개념이 없음)."""
    photos_r = (
        supabase.table("photos")
        .select("id, number, is_blurry, face_detected, eyes_closed")
        .eq("project_id", project_id)
        .order("number")
        .execute()
    )
    rows = photos_r.data or []
    photo_ids = [r["id"] for r in rows]
    emb_map = fetch_embeddings_by_photo_id(
        supabase, project_id, GEMINI_EMBEDDING_MODEL, GEMINI_EMBEDDING_DIMENSION,
        GEMINI_EMBEDDING_VERSION, photo_ids,
    )
    if not emb_map:
        return {"groups": [], "analyzed_count": 0, "quality_analyzed_count": 0, "threshold": threshold}

    ordered = [(r["id"], emb_map.get(r["id"])) for r in rows]
    embeddings = [e for _, e in ordered]
    raw_groups = group_by_similarity(embeddings, threshold)
    legacy_by_id = {r["id"]: r for r in rows}

    grouped_photo_ids = [ordered[i][0] for members in raw_groups for i in members]
    quality_map = (
        fetch_quality_by_photo_id(
            supabase, project_id, GEMINI_FLASH_MODEL, GEMINI_QUALITY_PROMPT_VERSION, grouped_photo_ids
        )
        if include_quality and grouped_photo_ids
        else {}
    )

    groups = []
    for members in raw_groups:
        photo_id_list = [ordered[i][0] for i in members]
        vectors = [embeddings[i] for i in members]
        similarity_rank = _avg_similarity_per_index(vectors)
        rep_idx = int(np.argmax(similarity_rank))
        representative_photo_id = photo_id_list[rep_idx]

        rec_photo_id, rec_tier, rec_reason, quality_by_photo = _recommend_with_quality(
            photo_id_list, similarity_rank, quality_map, representative_photo_id, legacy_by_id
        )

        groups.append(
            {
                "photo_ids": photo_id_list,
                "representative_photo_id": representative_photo_id,
                "recommended_photo_id": rec_photo_id,
                "recommendation_tier": rec_tier,
                "recommendation_reason": rec_reason,
                "photo_count": len(photo_id_list),
                "avg_similarity": _avg_pairwise_similarity(vectors),
                "quality_by_photo": quality_by_photo,
            }
        )

    return {
        "groups": groups,
        "analyzed_count": len(emb_map),
        "quality_analyzed_count": len(quality_map),
        "threshold": threshold,
    }


def _avg_similarity_per_index(vectors: list[np.ndarray]) -> np.ndarray:
    """그룹 내 각 이미지가 나머지 이미지들과 갖는 평균 코사인 유사도(정규화 벡터 내적).
    medoid 선정과 tier 내부 정렬(그룹 대표성) 양쪽에서 재사용한다."""
    n = len(vectors)
    if n == 1:
        return np.array([1.0])
    mat = np.stack(vectors)  # (n, d), 이미 L2-정규화된 벡터
    sim = mat @ mat.T  # (n, n) 코사인 유사도 행렬
    np.fill_diagonal(sim, 0.0)
    return sim.sum(axis=1) / (n - 1)


def _avg_pairwise_similarity(vectors) -> float:
    if len(vectors) < 2:
        return 1.0
    sims = [float(np.dot(vectors[i], vectors[i + 1])) for i in range(len(vectors) - 1)]
    return round(sum(sims) / len(sims), 4)


_QUALITY_AXES = ("eyes_closed", "blur_or_shake", "focus_issue", "face_occluded")
_AXIS_LABEL_KO = {
    "eyes_closed": "눈 감음",
    "blur_or_shake": "흔들림",
    "focus_issue": "초점",
    "face_occluded": "얼굴 판정",
}


def _issue_count(q: dict) -> int:
    """LIKELY=2점, POSSIBLE=1점, UNKNOWN/OK=0점 — "판정 불가"를 불량으로 취급하지 않기 위해
    UNKNOWN은 점수에 포함하지 않는다."""
    total = 0
    for axis in _QUALITY_AXES:
        level = q.get(axis)
        if level == "likely":
            total += 2
        elif level == "possible":
            total += 1
    return total


def _has_likely(q: dict) -> bool:
    return any(q.get(axis) == "likely" for axis in _QUALITY_AXES)


def _has_signal(q: dict) -> bool:
    """4축이 전부 UNKNOWN이면 실질적으로 아무 정보도 얻지 못한 것 — "이슈 없음(ok)"과
    구분해야 한다(전부 UNKNOWN인데 issue_count가 0이라고 해서 "품질 이상 없음"이라고 부르면 안 됨)."""
    return any(q.get(axis) != "unknown" for axis in _QUALITY_AXES)


def _confidence_bucket(has_data: bool, q: Optional[dict]) -> int:
    """추천 우선순위: 0=검증된 이상없음 > 1=경미한 의심 > 2=판정불가/미분석 > 3=명확한 의심.
    "판정 불가·미분석"은 "이슈 없음"보다는 낮지만 "명확한 의심"보다는 우선하도록 별도 버킷으로 분리한다
    — 그래야 미분석 사진이 검증된 clean 사진과 동률로 취급되지 않고, 검증된 minor 이슈 사진에도 밀리지 않는다."""
    if not has_data or q is None or not _has_signal(q):
        return 2
    if _has_likely(q):
        return 3
    if _issue_count(q) > 0:
        return 1
    return 0


def _worst_axis(q: dict) -> Optional[str]:
    order = {"likely": 2, "possible": 1}
    worst_axis = None
    worst_score = 0
    for axis in _QUALITY_AXES:
        score = order.get(q.get(axis), 0)
        if score > worst_score:
            worst_score = score
            worst_axis = axis
    return worst_axis


def _recommend_with_quality(
    photo_id_list: list[str],
    similarity_rank: np.ndarray,
    quality_map: dict,
    representative_photo_id: str,
    legacy_by_id: dict,
) -> tuple[str, str, Optional[str], dict]:
    """단계형(tiered) 추천: 후보를 신뢰도 버킷(0=검증된 이상없음 > 1=경미한 의심 >
    2=판정불가/미분석 > 3=명확한 의심)으로 먼저 나누고, 같은 버킷 안에서만 issue_count →
    그룹 대표성(medoid 유사도) 순으로 비교한다. "판정불가/미분석"을 "이상없음"과 분리하는 이유는
    UNKNOWN·미분석 사진이 실제로는 아무것도 확인되지 않았는데 "품질 이슈가 없다"고 잘못 표시되는
    것을 막기 위함이다 — 그 사진이 최종 추천으로 뽑히더라도 사유 문구는 "판정 어려움/확인 필요"로
    나가야 한다. 품질 분석이 그룹 내 어디에도 없으면 기존 medoid 대표 이미지를 그대로 반환해
    이전 동작과 동일하게 유지한다(회귀 없음). 동일 입력에는 항상 동일 결과가 나오도록 무작위
    요소를 두지 않는다."""
    quality_by_photo: dict = {}
    candidates = []  # (photo_id, bucket, issue_count, has_data, similarity_rank)
    for idx, pid in enumerate(photo_id_list):
        q = quality_map.get(pid)
        legacy_row = legacy_by_id.get(pid) or {}
        has_data = q is not None
        if q:
            quality_by_photo[pid] = {
                "gemini": {
                    "eyes_closed": q.get("eyes_closed"),
                    "blur_or_shake": q.get("blur_or_shake"),
                    "focus_issue": q.get("focus_issue"),
                    "face_occluded": q.get("face_occluded"),
                    "model": GEMINI_FLASH_MODEL,
                    "prompt_version": GEMINI_QUALITY_PROMPT_VERSION,
                },
                "legacy": {
                    "is_blurry": legacy_row.get("is_blurry"),
                    "face_detected": legacy_row.get("face_detected"),
                    "eyes_closed": legacy_row.get("eyes_closed"),
                },
            }
        bucket = _confidence_bucket(has_data, q)
        issue_count = _issue_count(q) if q else 0
        candidates.append((pid, bucket, issue_count, has_data, float(similarity_rank[idx])))

    if not any(c[3] for c in candidates):
        return representative_photo_id, "unavailable", None, quality_by_photo

    best = min(
        candidates,
        key=lambda c: (c[1], c[2], -c[4], photo_id_list.index(c[0])),
    )
    best_photo_id, bucket, _issue_count_val, _has_data, _rank = best

    if bucket == 0:
        tier = "clean"
        reason = "품질 이슈가 발견되지 않고 그룹 대표성이 높은 이미지를 추천했습니다."
    elif bucket == 1:
        tier = "minor"
        axis = _worst_axis(quality_map.get(best_photo_id) or {})
        label = _AXIS_LABEL_KO.get(axis, "품질")
        reason = f"{label} 의심이 낮고 그룹 대표성이 높은 이미지를 우선 추천했습니다."
    elif bucket == 2:
        tier = "unknown"
        reason = "품질 판정 정보가 충분하지 않아(미분석 또는 판정 어려움) 그룹 대표성이 높은 이미지를 추천했습니다 · 품질 확인 필요"
    else:
        tier = "major"
        reason = "그룹 내 모든 이미지에 확인 항목이 있어 상대적으로 안정적인 이미지를 추천했습니다 · 확인 필요"

    return best_photo_id, tier, reason, quality_by_photo


def count_pending(supabase, project_id: str) -> dict:
    """베타 [AI 유사도 분석] 버튼의 상태 문구를 결정하기 위한 라이브 카운트.
    현재 활성(=photos 테이블에 남아있는) 사진 중 현재 설정(model+dimension+version)으로
    이미 분석된 수/대기 중인 수를 그 자리에서 계산한다 — Gemini API 호출 없음, 순수 조회."""
    photos_r = (
        supabase.table("photos")
        .select("id, r2_thumb_url")
        .eq("project_id", project_id)
        .execute()
    )
    rows = [r for r in (photos_r.data or []) if r.get("r2_thumb_url")]
    photo_id_to_key = {r["id"]: extract_object_key(r.get("r2_thumb_url")) for r in rows}
    cached = get_cached_photo_ids(
        supabase, project_id, GEMINI_EMBEDDING_MODEL, GEMINI_EMBEDDING_DIMENSION,
        GEMINI_EMBEDDING_VERSION, photo_id_to_key,
    )
    active = len(rows)
    return {
        "active_photo_count": active,
        "already_analyzed_count": len(cached),
        "pending_count": active - len(cached),
    }


def sync_groups_to_db(supabase, project_id: str, threshold: float) -> None:
    """작가 화면(업로드 그리드)과 고객 갤러리가 그대로 읽는 photo_groups/photos.similarity_group_id를
    Gemini 그룹핑 결과로 최신화한다. OpenCLIP처럼 배치 경계를 넘나드는 증분 스티칭 없이, 저장된
    임베딩으로 매번 전체를 다시 계산해(compute_groups, include_quality=False 고정) 통째로
    교체하는 방식 — Gemini는 이 재계산이 가벼워 항상 정합적인 스냅샷을 유지할 수 있다.

    이 프로젝트에 Gemini 임베딩이 하나도 없으면(OpenCLIP만 쓴 레거시 프로젝트) 아무것도 하지
    않고 즉시 반환한다 — 실수로 OpenCLIP의 photo_groups를 지우지 않기 위한 안전장치."""
    result = compute_groups(supabase, project_id, threshold, include_quality=False)
    if result["analyzed_count"] == 0:
        return

    # 1) 이 프로젝트의 similarity_group_id를 전부 NULL — 과거 OpenCLIP 그룹이 섞이지 않게 한다.
    supabase.table("photos").update({"similarity_group_id": None}).eq(
        "project_id", project_id
    ).execute()
    # 2) 기존 photo_groups 행 전부 삭제 후
    supabase.table("photo_groups").delete().eq("project_id", project_id).execute()
    # 3) 계산된 그룹으로 다시 채운다.
    for g in result["groups"]:
        group_r = (
            supabase.table("photo_groups")
            .insert(
                {
                    "project_id": project_id,
                    "representative_photo_id": g["representative_photo_id"],
                    "photo_count": g["photo_count"],
                    "avg_similarity": g["avg_similarity"],
                }
            )
            .execute()
        )
        group_id = group_r.data[0]["id"]
        supabase.table("photos").update({"similarity_group_id": group_id}).in_(
            "id", g["photo_ids"]
        ).execute()
