"""MediaPipe Face Mesh 기반 눈감음 검출. quality.py의 블러 판정과는 별도 모듈로 분리 —
mediapipe는 새 의존성이자 새 장애 지점이라, 이 모듈의 초기화/추론이 실패해도
블러 스코어링은 영향받지 않도록 격리한다(main.py의 CLIP warm_up() 방어 패턴과 동일)."""
import io
import logging
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# 표준 MediaPipe Face Mesh 눈 랜드마크 인덱스 (좌우 각 6점: 좌/우 끝 + 위/아래 2쌍)
_LEFT_EYE = [33, 160, 158, 133, 153, 144]
_RIGHT_EYE = [362, 385, 387, 263, 373, 380]

_face_mesh = None
_init_failed = False


def _get_face_mesh():
    """지연 초기화. 실패하면 이후 호출에서 재시도하지 않고 (None, None)만 반환."""
    global _face_mesh, _init_failed
    if _face_mesh is not None or _init_failed:
        return _face_mesh
    try:
        import mediapipe as mp

        _face_mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=True,
            max_num_faces=10,
            refine_landmarks=False,
            min_detection_confidence=0.5,
        )
    except Exception as e:
        logger.warning("mediapipe FaceMesh 초기화 실패, 눈감음 검출 비활성화: %s", e)
        _init_failed = True
    return _face_mesh


def _eye_aspect_ratio(landmarks, indices, width: int, height: int) -> float:
    pts = [(landmarks[i].x * width, landmarks[i].y * height) for i in indices]
    vertical = (
        float(np.hypot(pts[1][0] - pts[5][0], pts[1][1] - pts[5][1]))
        + float(np.hypot(pts[2][0] - pts[4][0], pts[2][1] - pts[4][1]))
    ) / 2.0
    horizontal = float(np.hypot(pts[0][0] - pts[3][0], pts[0][1] - pts[3][1]))
    return vertical / horizontal if horizontal else 0.0


def _score_one(image_bytes: bytes, ear_threshold: float) -> Tuple[Optional[bool], Optional[bool]]:
    """-> (face_detected, eyes_closed).
    얼굴 미검출: (False, None). 디코딩/추론 실패: (None, None)."""
    face_mesh = _get_face_mesh()
    if face_mesh is None:
        return None, None

    try:
        img = np.asarray(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
    except Exception as e:
        logger.warning("eye detection: image decode failed: %s", e)
        return None, None

    try:
        result = face_mesh.process(img)
    except Exception as e:
        logger.warning("eye detection: mediapipe processing failed: %s", e)
        return None, None

    if not result.multi_face_landmarks:
        return False, None

    height, width = img.shape[:2]
    # 단체 사진에서 한 명이라도 눈을 감았으면 재촬영 후보로 보고 경고
    any_closed = False
    for face in result.multi_face_landmarks:
        lm = face.landmark
        left = _eye_aspect_ratio(lm, _LEFT_EYE, width, height)
        right = _eye_aspect_ratio(lm, _RIGHT_EYE, width, height)
        if (left + right) / 2.0 < ear_threshold:
            any_closed = True
            break

    return True, any_closed


def compute_eye_flags(
    images: List[Optional[bytes]], ear_threshold: float
) -> List[Tuple[Optional[bool], Optional[bool]]]:
    """이미지 바이트 리스트 -> (face_detected, eyes_closed) 튜플 리스트 (순서 보존)."""
    return [
        _score_one(img, ear_threshold) if img is not None else (None, None)
        for img in images
    ]
