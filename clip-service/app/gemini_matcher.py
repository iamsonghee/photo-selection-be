"""보정본 업로드 시 파일명 매칭(exact/fuzzy)에 실패한 잔여 항목을 Gemini 임베딩 유사도로
매칭하기 위한 임베딩 조회/생성 코드 — 1단계(실측)에서는 이 모듈이 main.py에 연결되지 않는다.

matcher.py(OpenCLIP)와 동일한 lazy-backfill 패턴을 따르되, 캐시 저장소는 photos.clip_embedding
(컬럼 1개, 버전 구분 없음) 대신 gemini_embeddings 테이블(model+dimension+version까지 함께
확인)을 사용한다 — 이미 유사컷 분석(gemini_analyzer.py)이 쓰던 캐시를 그대로 재사용한다.

2단계(운영 전환)에서 이 모듈에 _greedy_assign(1:1 배정)을 추가하고 main.py의
/match-retouch가 matcher.match_retouch 대신 이 모듈을 호출하도록 바꿀 예정이다.
"""
import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.config import GEMINI_EMBEDDING_DIMENSION, GEMINI_EMBEDDING_MODEL, GEMINI_EMBEDDING_VERSION
from app.downloader import download_all
from app.gemini_client import embed_images
from app.gemini_embeddings_store import (
    extract_object_key,
    fetch_embeddings_by_photo_id,
    get_cached_photo_ids,
    persist_embeddings,
)

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
