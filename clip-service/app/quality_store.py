"""photos.blur_variance/is_blurry/face_detected/eyes_closed 영속화 — embeddings_store.py와 동일 패턴."""
import logging
import math
from typing import List, Optional, Tuple

from app.config import DB_UPSERT_BATCH_SIZE

logger = logging.getLogger(__name__)

QualityFlags = Tuple[Optional[float], Optional[bool], Optional[bool], Optional[bool]]
"""(blur_variance, is_blurry, face_detected, eyes_closed)"""


def persist_quality_flags(
    supabase,
    id_flags_pairs: List[Tuple[str, QualityFlags]],
) -> Tuple[int, int, int]:
    """photo_id별로 계산된 품질 플래그를 photos 테이블에 배치 upsert.

    전부 None(디코딩 실패)인 항목은 기존 동작대로 건너뛴다.
    upsert on_conflict="id": blur_variance/is_blurry/face_detected/eyes_closed만 갱신하며
    다른 컬럼은 건드리지 않는다.

    반환: (success_count, fail_count, batch_count)
    """
    rows = []
    for photo_id, (blur_variance, is_blurry, face_detected, eyes_closed) in id_flags_pairs:
        if blur_variance is None and face_detected is None:
            continue
        rows.append({
            "id": photo_id,
            "blur_variance": blur_variance,
            "is_blurry": is_blurry,
            "face_detected": face_detected,
            "eyes_closed": eyes_closed,
        })

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
                "persist_quality_flags batch %d/%d failed (%d rows): %s",
                i + 1, n_batches, len(batch), e,
            )

    return success, fail, n_batches
