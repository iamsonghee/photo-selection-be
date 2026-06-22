"""open_clip 모델 lazy-load 싱글톤 + 임베딩 계산."""
import io
import logging
import threading
from typing import List

import numpy as np
import torch
from PIL import Image

from app.config import CLIP_MODEL_NAME, CLIP_MODEL_PRETRAINED, EMBEDDING_BATCH_SIZE

logger = logging.getLogger(__name__)

_model = None
_preprocess = None
_load_lock = threading.Lock()


def _ensure_loaded():
    global _model, _preprocess
    if _model is not None:
        return
    with _load_lock:
        if _model is not None:
            return
        import open_clip

        logger.info("Loading CLIP model %s (%s)...", CLIP_MODEL_NAME, CLIP_MODEL_PRETRAINED)
        model, _, preprocess = open_clip.create_model_and_transforms(
            CLIP_MODEL_NAME, pretrained=CLIP_MODEL_PRETRAINED
        )
        model.eval()
        torch.set_grad_enabled(False)
        _model = model
        _preprocess = preprocess
        logger.info("CLIP model loaded.")


def warm_up() -> None:
    """서버 시작 시 모델을 미리 로드해 첫 분석 요청의 지연을 없앤다."""
    _ensure_loaded()


def compute_embeddings(images: List[bytes]) -> List[np.ndarray]:
    """이미지 바이트 리스트 -> L2 정규화된 임베딩 벡터 리스트 (순서 보존).
    디코딩 실패한 이미지는 None으로 채워 반환한다."""
    _ensure_loaded()
    assert _model is not None and _preprocess is not None

    results: List[np.ndarray | None] = [None] * len(images)
    decoded: list[tuple[int, "torch.Tensor"]] = []
    for idx, raw in enumerate(images):
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            decoded.append((idx, _preprocess(img)))
        except Exception as e:
            logger.warning("image decode failed at index %s: %s", idx, e)

    for start in range(0, len(decoded), EMBEDDING_BATCH_SIZE):
        chunk = decoded[start : start + EMBEDDING_BATCH_SIZE]
        batch = torch.stack([t for _, t in chunk])
        with torch.no_grad():
            feats = _model.encode_image(batch)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        feats_np = feats.cpu().numpy()
        for (idx, _), vec in zip(chunk, feats_np):
            results[idx] = vec

    return results
