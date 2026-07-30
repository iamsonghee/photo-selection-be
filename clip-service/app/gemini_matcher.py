"""보정본 업로드 시 파일명 매칭(exact/fuzzy)에 실패한 잔여 항목을 Gemini 임베딩 유사도로
매칭한다. matcher.py(OpenCLIP)를 대체하는 2단계(운영 전환) 구현.

matcher.py와 동일한 lazy-backfill 패턴을 따르되, 캐시 저장소는 photos.clip_embedding
(컬럼 1개, 버전 구분 없음) 대신 gemini_embeddings 테이블(model+dimension+version까지 함께
확인)을 사용한다 — 이미 유사컷 분석(gemini_analyzer.py)이 쓰던 캐시를 그대로 재사용한다.

임계값은 1단계 실측(2026-07-30, 실제 웨딩/로모그래피 프로젝트 10쌍 + 합성 편집 20건)으로
산정했다 — OpenCLIP의 0.85/0.60을 그대로 쓰지 않는다(점수 분포가 다름, config.py 주석 참고).
top1-top2 margin 기반 강등/거부는 아직 넣지 않았다 — 실측에서 margin이 오매칭 방지에
그대로 쓰기엔 애매하다는 결과가 나와 별도 설계가 필요하다고 판단, 이번 전환 범위에서 제외.
"""
import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.config import (
    GEMINI_EMBEDDING_DIMENSION,
    GEMINI_EMBEDDING_MODEL,
    GEMINI_EMBEDDING_VERSION,
    GEMINI_MATCH_AUTO_THRESHOLD,
    GEMINI_MATCH_LOW_THRESHOLD,
)
from app.downloader import download_all
from app.gemini_client import embed_images
from app.gemini_embeddings_store import (
    extract_object_key,
    fetch_embeddings_by_photo_id,
    get_cached_photo_ids,
    persist_embeddings,
)
from app.grouping import _cosine

logger = logging.getLogger(__name__)


async def get_or_compute_original_embeddings(
    supabase, project_id: str, photo_ids: List[str]
) -> Dict[str, np.ndarray]:
    """지정한 원본 사진들의 Gemini 임베딩을 반환한다.

    gemini_embeddings에 현재 설정(model+dimension+version, + 가능하면 source_object_key)과
    일치하는 캐시가 있으면 재사용하고, 없는 사진만 새로 다운로드·임베딩 계산 후 캐시에
    저장(lazy backfill)한다 — 모델 버전이 다른 임베딩을 섞어 쓰는 일은 gemini_embeddings_store의
    조회 조건(model+dimension+version 정확히 일치)이 이미 차단한다.
    """
    if not photo_ids:
        return {}

    rows = (
        supabase.table("photos")
        .select("id, r2_thumb_url")
        .eq("project_id", project_id)
        .in_("id", photo_ids)
        .execute()
        .data
        or []
    )
    url_by_id = {r["id"]: r["r2_thumb_url"] for r in rows}
    object_key_by_id = {pid: extract_object_key(url) for pid, url in url_by_id.items()}

    cached_ids = get_cached_photo_ids(
        supabase,
        project_id,
        GEMINI_EMBEDDING_MODEL,
        GEMINI_EMBEDDING_DIMENSION,
        GEMINI_EMBEDDING_VERSION,
        object_key_by_id,
    )

    result: Dict[str, np.ndarray] = {}
    if cached_ids:
        result.update(
            fetch_embeddings_by_photo_id(
                supabase,
                project_id,
                GEMINI_EMBEDDING_MODEL,
                GEMINI_EMBEDDING_DIMENSION,
                GEMINI_EMBEDDING_VERSION,
                list(cached_ids),
            )
        )

    missing_ids = [pid for pid in url_by_id if pid not in result]
    if missing_ids:
        urls = [url_by_id[pid] for pid in missing_ids]
        images = await download_all(urls)
        vecs, _usages = await embed_images(images)

        to_persist: List[Tuple[str, Optional[np.ndarray], Optional[str]]] = []
        for pid, vec in zip(missing_ids, vecs):
            if vec is not None:
                result[pid] = vec
            to_persist.append((pid, vec, object_key_by_id.get(pid)))

        persist_embeddings(
            supabase,
            project_id,
            GEMINI_EMBEDDING_MODEL,
            GEMINI_EMBEDDING_DIMENSION,
            GEMINI_EMBEDDING_VERSION,
            to_persist,
        )

    return result


async def compute_retouch_embeddings(
    file_bytes_list: List[bytes],
) -> List[Optional[np.ndarray]]:
    """업로드된 보정본 파일들의 Gemini 임베딩을 계산한다. 1회성 매칭용이라 캐시하지 않는다."""
    if not file_bytes_list:
        return []
    vecs, _usages = await embed_images(file_bytes_list)
    return vecs


async def match_retouch_gemini(
    supabase,
    project_id: str,
    photo_ids: List[str],
    retouch_files: List[Tuple[str, bytes]],
) -> List[dict]:
    """matcher.match_retouch와 동일한 시그니처/반환 형식 — main.py에서 그대로 교체 가능하게 맞춤.
    반환 각 항목의 type은 "gemini" | "gemini_low" (기존 "clip" | "clip_low"를 대체)."""
    if not photo_ids or not retouch_files:
        return []

    embedding_by_id = await get_or_compute_original_embeddings(supabase, project_id, photo_ids)
    if not embedding_by_id:
        return []

    file_names = [name for name, _ in retouch_files]
    file_bytes = [data for _, data in retouch_files]
    file_vecs = await compute_retouch_embeddings(file_bytes)

    file_meta = [
        {"filename": name, "embedding": vec}
        for name, vec in zip(file_names, file_vecs)
        if vec is not None
    ]
    if not file_meta:
        return []

    photo_meta = [{"photo_id": pid, "embedding": vec} for pid, vec in embedding_by_id.items()]

    similarity_matrix = [
        [float(_cosine(f["embedding"], p["embedding"])) for p in photo_meta]
        for f in file_meta
    ]

    return _greedy_assign(similarity_matrix, file_meta, photo_meta)


def _greedy_assign(
    similarity_matrix: List[List[float]],
    file_meta: List[dict],
    photo_meta: List[dict],
) -> List[dict]:
    """matcher.py의 _greedy_assign과 동일한 알고리즘(전역 유사도 내림차순 + claimed set으로
    1:1 보장) — 임계값과 type 라벨만 Gemini용으로 교체."""
    pairs = []
    for fi, row in enumerate(similarity_matrix):
        for pi, sim in enumerate(row):
            pairs.append((sim, fi, pi))
    pairs.sort(key=lambda x: x[0], reverse=True)

    claimed_files: set[int] = set()
    claimed_photos: set[int] = set()
    results: List[dict] = []
    for sim, fi, pi in pairs:
        if sim < GEMINI_MATCH_LOW_THRESHOLD:
            break  # 내림차순 정렬이므로 이후 전부 기준 미달
        if fi in claimed_files or pi in claimed_photos:
            continue
        claimed_files.add(fi)
        claimed_photos.add(pi)
        results.append(
            {
                "photo_id": photo_meta[pi]["photo_id"],
                "filename": file_meta[fi]["filename"],
                "similarity": round(sim, 4),
                "type": "gemini" if sim >= GEMINI_MATCH_AUTO_THRESHOLD else "gemini_low",
            }
        )
    return results
