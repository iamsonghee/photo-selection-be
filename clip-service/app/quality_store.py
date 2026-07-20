"""photos.blur_variance/is_blurry/face_detected/eyes_closed 영속화 — embeddings_store.py와 동일 패턴."""
import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

QualityFlags = Tuple[Optional[float], Optional[bool], Optional[bool], Optional[bool]]
"""(blur_variance, is_blurry, face_detected, eyes_closed)"""


def persist_quality_flags(supabase, id_flags_pairs: List[Tuple[str, QualityFlags]]) -> None:
    """photo_id별로 계산된 품질 플래그를 photos 테이블에 저장.
    전부 None(디코딩 실패)인 항목은 건너뛴다. 실패한 항목은 로그만 남기고 계속한다."""
    for photo_id, (blur_variance, is_blurry, face_detected, eyes_closed) in id_flags_pairs:
        if blur_variance is None and face_detected is None:
            continue
        try:
            supabase.table("photos").update(
                {
                    "blur_variance": blur_variance,
                    "is_blurry": is_blurry,
                    "face_detected": face_detected,
                    "eyes_closed": eyes_closed,
                }
            ).eq("id", photo_id).execute()
        except Exception as e:
            logger.warning("failed to persist quality flags for photo_id=%s: %s", photo_id, e)
