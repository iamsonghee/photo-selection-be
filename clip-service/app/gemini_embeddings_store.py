"""gemini_embeddings 테이블 영속화. photos/clip_embedding과 완전히 분리된 별도 저장소 —
threshold를 바꿔가며 재그룹핑할 때 Gemini API를 다시 호출하지 않기 위한 캐시 역할도 겸한다.

캐시 판별은 project_id+photo_id+model 만으로는 부족하다 — dimension/embedding_version이
바뀌면 예전 설정의 벡터를 잘못 재사용할 수 있고(그룹 계산에 서로 다른 차원이 섞이는 버그로
이어짐), 방어적으로 R2 object key까지 함께 확인해 실제 파일이 바뀐 경우도 잡아낸다."""
import logging
import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.config import DB_UPSERT_BATCH_SIZE, R2_PUBLIC_URL

logger = logging.getLogger(__name__)

_UNIQUE_KEY = "project_id,photo_id,embedding_model,dimension,embedding_version"


def extract_object_key(url: Optional[str]) -> Optional[str]:
    """r2_thumb_url에서 R2_PUBLIC_URL 접두사를 제거한 순수 R2 object key.
    R2_PUBLIC_URL 미설정이거나 접두사가 안 맞으면 None(호출부가 "확인 불가"로 안전 처리)."""
    if not url or not R2_PUBLIC_URL:
        return None
    base = R2_PUBLIC_URL.rstrip("/") + "/"
    return url[len(base):] if url.startswith(base) else None


def get_cached_photo_ids(
    supabase,
    project_id: str,
    model: str,
    dimension: int,
    version: str,
    photo_id_to_object_key: Dict[str, Optional[str]],
) -> set[str]:
    """현재 설정(model+dimension+version)에 저장된 임베딩이 있는 photo_id만 "캐시 히트"로
    인정한다. object key까지 알 수 있는 경우(R2_PUBLIC_URL 설정됨 + 기존 행에도 기록되어 있음)엔
    실제 파일이 같은지도 함께 확인한다 — 어느 한쪽이라도 key를 모르면(과거 행이라 NULL이거나
    R2_PUBLIC_URL 미설정) 설정 일치만으로 안전하게 캐시 인정(과도한 재분석 방지)."""
    photo_ids = list(photo_id_to_object_key.keys())
    if not photo_ids:
        return set()
    cached: set[str] = set()
    for i in range(0, len(photo_ids), DB_UPSERT_BATCH_SIZE):
        chunk = photo_ids[i : i + DB_UPSERT_BATCH_SIZE]
        r = (
            supabase.table("gemini_embeddings")
            .select("photo_id, source_object_key")
            .eq("project_id", project_id)
            .eq("embedding_model", model)
            .eq("dimension", dimension)
            .eq("embedding_version", version)
            .in_("photo_id", chunk)
            .execute()
        )
        for row in r.data or []:
            pid = row["photo_id"]
            stored_key = row.get("source_object_key")
            expected_key = photo_id_to_object_key.get(pid)
            if stored_key is None or expected_key is None or stored_key == expected_key:
                cached.add(pid)
    return cached


def persist_embeddings(
    supabase,
    project_id: str,
    model: str,
    dimension: int,
    version: str,
    id_vector_key_triples: List[Tuple[str, Optional[np.ndarray], Optional[str]]],
) -> Tuple[int, int, int]:
    """photo_id별로 계산된 임베딩을 gemini_embeddings에 배치 upsert.
    id_vector_key_triples: (photo_id, embedding 벡터, source_object_key) 튜플 목록.

    반환: (success_count, fail_count, batch_count)
    """
    rows = [
        {
            "project_id": project_id,
            "photo_id": photo_id,
            "embedding_model": model,
            "embedding": vec.tolist(),
            "dimension": dimension,
            "embedding_version": version,
            "source_object_key": object_key,
        }
        for photo_id, vec, object_key in id_vector_key_triples
        if vec is not None
    ]
    if not rows:
        return 0, 0, 0

    n = len(rows)
    n_batches = math.ceil(n / DB_UPSERT_BATCH_SIZE)
    success = 0
    fail = 0

    for i in range(n_batches):
        batch = rows[i * DB_UPSERT_BATCH_SIZE : (i + 1) * DB_UPSERT_BATCH_SIZE]
        try:
            supabase.table("gemini_embeddings").upsert(batch, on_conflict=_UNIQUE_KEY).execute()
            success += len(batch)
        except Exception as e:
            fail += len(batch)
            logger.warning(
                "gemini persist_embeddings batch %d/%d failed (%d rows): %s",
                i + 1, n_batches, len(batch), e,
            )

    return success, fail, n_batches


def fetch_embeddings_by_photo_id(
    supabase, project_id: str, model: str, dimension: int, version: str, photo_ids: List[str]
) -> Dict[str, np.ndarray]:
    """지정한 photo_id들의 임베딩을 {photo_id: vector} 형태로 조회 (재그룹핑용).
    dimension/version까지 필터링해 서로 다른 설정의 벡터가 섞이는 것을 원천 차단한다."""
    if not photo_ids:
        return {}
    result: Dict[str, np.ndarray] = {}
    for i in range(0, len(photo_ids), DB_UPSERT_BATCH_SIZE):
        chunk = photo_ids[i : i + DB_UPSERT_BATCH_SIZE]
        r = (
            supabase.table("gemini_embeddings")
            .select("photo_id, embedding")
            .eq("project_id", project_id)
            .eq("embedding_model", model)
            .eq("dimension", dimension)
            .eq("embedding_version", version)
            .in_("photo_id", chunk)
            .execute()
        )
        for row in r.data or []:
            result[row["photo_id"]] = np.array(row["embedding"], dtype=np.float32)
    return result
