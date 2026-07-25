"""photos.clip_embedding 영속화 — analyzer.py(분석 파이프라인)와 matcher.py(보정본 매칭) 양쪽에서 공유."""
import logging
import math
from typing import List, Optional, Tuple

import numpy as np

from app.config import DB_UPSERT_BATCH_SIZE

logger = logging.getLogger(__name__)


def persist_embeddings(
    supabase,
    id_vector_pairs: List[Tuple[str, Optional[np.ndarray]]],
) -> Tuple[int, int, int]:
    """photo_id별로 계산된 임베딩을 photos.clip_embedding에 배치 upsert.

    upsert on_conflict="id": 기존 row의 clip_embedding만 갱신하며 다른 컬럼은 건드리지 않는다.
    id_vector_pairs의 photo_id는 반드시 이미 photos 테이블에 존재해야 한다(분석 시작 시
    SELECT로 조회한 ID를 그대로 사용하므로 이 조건은 항상 성립한다).

    반환: (success_count, fail_count, batch_count)
    """
    rows = [
        {"id": photo_id, "clip_embedding": vec.tolist()}
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
            supabase.table("photos").upsert(batch, on_conflict="id").execute()
            success += len(batch)
        except Exception as e:
            fail += len(batch)
            logger.warning(
                "persist_embeddings batch %d/%d failed (%d rows): %s",
                i + 1, n_batches, len(batch), e,
            )

    return success, fail, n_batches
