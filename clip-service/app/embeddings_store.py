"""photos.clip_embedding 영속화 — analyzer.py(분석 파이프라인)와 matcher.py(보정본 매칭) 양쪽에서 공유."""
import logging
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


def persist_embeddings(supabase, id_vector_pairs: List[Tuple[str, Optional[np.ndarray]]]) -> None:
    """photo_id별로 계산된 임베딩을 photos.clip_embedding에 저장. 실패한 항목은 로그만 남기고 계속한다."""
    for photo_id, vec in id_vector_pairs:
        if vec is None:
            continue
        try:
            supabase.table("photos").update({"clip_embedding": vec.tolist()}).eq("id", photo_id).execute()
        except Exception as e:
            logger.warning("failed to persist clip_embedding for photo_id=%s: %s", photo_id, e)
