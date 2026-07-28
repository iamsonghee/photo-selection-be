"""Gemini 멀티모달 임베딩 API 래퍼 (POC 전용, OpenCLIP 파이프라인과 완전히 독립).

이미지 1장당 1회 embed_content 호출 → 임베딩 1개. 동시성 제한(세마포어),
제한된 재시도(exponential backoff), 요청 timeout을 적용한다.
API 키와 이미지 바이트, 임베딩 값은 절대 로그에 남기지 않는다.
"""
import asyncio
import logging
from typing import List, Optional

import numpy as np
from google import genai
from google.genai import types

from app.config import (
    GEMINI_API_KEY,
    GEMINI_CONCURRENCY,
    GEMINI_EMBEDDING_DIMENSION,
    GEMINI_EMBEDDING_MODEL,
    GEMINI_MAX_RETRIES,
    GEMINI_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

_client: Optional[genai.Client] = None
_client_lock = asyncio.Lock()


class GeminiNotConfigured(Exception):
    pass


async def get_client() -> genai.Client:
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            if not GEMINI_API_KEY:
                raise GeminiNotConfigured("GEMINI_API_KEY not configured")
            _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def _normalize(vec: np.ndarray) -> np.ndarray:
    """output_dimensionality로 절단(Matryoshka)된 임베딩은 단위 벡터가 아닐 수 있어
    grouping.py의 코사인 유사도(정규화 벡터 내적 가정)와 맞추기 위해 명시적으로 정규화한다."""
    norm = np.linalg.norm(vec)
    if norm == 0:
        return vec
    return vec / norm


def _extract_usage(response) -> Optional[dict]:
    """SDK 응답에 사용량 정보가 있으면 그대로 기록(추정치보다 우선), 없으면 None."""
    usage = getattr(response, "usage_metadata", None) or getattr(response, "metadata", None)
    if usage is None:
        return None
    try:
        if hasattr(usage, "model_dump"):
            return usage.model_dump()
        return dict(usage)
    except Exception:
        return None


async def _embed_one(client: genai.Client, image_bytes: bytes, mime_type: str):
    last_exc: Optional[Exception] = None
    for attempt in range(GEMINI_MAX_RETRIES + 1):
        try:
            response = await asyncio.wait_for(
                client.aio.models.embed_content(
                    model=GEMINI_EMBEDDING_MODEL,
                    contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime_type)],
                    config=types.EmbedContentConfig(
                        output_dimensionality=GEMINI_EMBEDDING_DIMENSION
                    ),
                ),
                timeout=GEMINI_TIMEOUT_SECONDS,
            )
            vec = _normalize(np.array(response.embeddings[0].values, dtype=np.float32))
            return vec, _extract_usage(response)
        except Exception as e:
            last_exc = e
            if attempt < GEMINI_MAX_RETRIES:
                await asyncio.sleep(2**attempt)
                continue
    raise last_exc  # type: ignore[misc]


async def embed_images(
    images: List[Optional[bytes]],
) -> tuple[List[Optional[np.ndarray]], List[dict]]:
    """순서를 보존하며 이미지별 임베딩 계산. 다운로드 실패(None) 또는 임베딩 실패 항목은 None.
    반환: (임베딩 리스트, 실제 usage_metadata 수집분 리스트)."""
    client = await get_client()
    sem = asyncio.Semaphore(GEMINI_CONCURRENCY)
    usages: List[dict] = []

    async def _run(idx: int, img: Optional[bytes]) -> Optional[np.ndarray]:
        if img is None:
            return None
        async with sem:
            try:
                vec, usage = await _embed_one(client, img, "image/jpeg")
                if usage:
                    usages.append(usage)
                return vec
            except Exception as e:
                logger.warning("gemini embedding failed for image index=%d: %s", idx, e)
                return None

    embeddings = await asyncio.gather(*[_run(i, img) for i, img in enumerate(images)])
    return list(embeddings), usages
