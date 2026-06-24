"""OpenCV 기반 이미지 품질 점수(블러 + 노출) 계산. 그룹 내 대표컷 자동 선정에 사용."""
import io
import logging
from typing import List, Optional

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


def _score_one(image_bytes: bytes) -> Optional[float]:
    """블러(라플라시안 분산)와 노출(중간 밝기 근접도)을 결합한 점수. 높을수록 좋음.
    디코딩 실패 시 None."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        gray = np.asarray(img)
    except Exception as e:
        logger.warning("quality score: image decode failed: %s", e)
        return None

    blur_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    # 분산 스케일이 장면마다 크게 달라 로그로 압축
    blur_component = float(np.log1p(blur_var))

    mean_brightness = float(gray.mean())
    # 중간 밝기(128)에서 멀어질수록 감점되는 0~1 노출 점수 (과/저露광 페널티)
    exposure_score = max(0.0, 1.0 - abs(mean_brightness - 128.0) / 128.0)

    return blur_component * exposure_score


def compute_quality_scores(images: List[Optional[bytes]]) -> List[Optional[float]]:
    """이미지 바이트 리스트 -> 품질 점수 리스트 (순서 보존).
    None(다운로드/디코딩 실패)은 None으로 반환된다."""
    return [_score_one(img) if img is not None else None for img in images]


def pick_best_index(scores: List[Optional[float]]) -> int:
    """품질 점수가 가장 높은 인덱스를 반환. 모두 None이면 0(첫 번째)을 반환."""
    best_idx = 0
    best_score = float("-inf")
    for i, s in enumerate(scores):
        if s is not None and s > best_score:
            best_score = s
            best_idx = i
    return best_idx
