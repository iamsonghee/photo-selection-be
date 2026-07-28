"""gemini_embeddings 테이블 영속화. photos/clip_embedding과 완전히 분리된 별도 테이블 —
threshold를 바꿔가며 재그룹핑할 때 Gemini API를 다시 호출하지 않기 위한 캐시 역할도 겸한다."""
import logging
import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.config import DB_UPSERT_BATCH_SIZE

logger = logging.getLogger(__name__)


def get_existing_photo_ids(supabase, project_id: str, model: str, photo_ids: List[str]) -> set[str]:
    """이미 임베딩이 저장된 photo_id 집합 — 동일 이미지에 대한 불필요한 Gemini 재호출 방지."""
    if not photo_ids:
        return set()
    r = (
        supabase.table("gemini_embeddings")
        .select("photo_id")
        .eq("project_id", project_id)
        .eq("embedding_model", model)
        .in_("photo_id", photo_ids)
        .execute()
    )
    return {row["photo_id"] for row in (r.data or [])}


def persist_embeddings(
    supabase,
    project_id: str,
    model: str,
    dimension: int,
    id_vector_pairs: List[Tuple[str, Optional[np.ndarray]]],
) -> Tuple[int, int, int]:
    """photo_id별로 계산된 임베딩을 gemini_embeddings에 배치 upsert.

    반환: (success_count, fail_count, batch_count)
    """
    rows = [
        {
            "project_id": project_id,
            "photo_id": photo_id,
            "embedding_model": model,
            "embedding": vec.tolist(),
            "dimension": dimension,
        }
        for photo_id, vec in id_vector_pairs
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
            supabase.table("gemini_embeddings").upsert(
                batch, on_conflict="project_id,photo_id,embedding_model"
            ).execute()
            success += len(batch)
        except Exception as e:
            fail += len(batch)
            logger.warning(
                "gemini persist_embeddings batch %d/%d failed (%d rows): %s",
                i + 1, n_batches, len(batch), e,
            )

    return success, fail, n_batches


def fetch_embeddings_by_photo_id(
    supabase, project_id: str, model: str, photo_ids: List[str]
) -> Dict[str, np.ndarray]:
    """지정한 photo_id들의 임베딩을 {photo_id: vector} 형태로 조회 (재그룹핑용)."""
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
            .in_("photo_id", chunk)
            .execute()
        )
        for row in r.data or []:
            result[row["photo_id"]] = np.array(row["embedding"], dtype=np.float32)
    return result
