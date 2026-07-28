"""Gemini Flash 기반 사진 품질 판정 API 래퍼 (POC 전용, Gemini Embedding·OpenCLIP과 완전히 독립).

이미지 1장당 1회 generate_content 호출 → 구조화된 JSON 판정 1건. 동시성 제한(세마포어),
제한된 재시도(exponential backoff), 요청 timeout을 적용한다. 이 기능은 사진을 자동 삭제·숨김
처리하기 위한 것이 아니라 작가의 1차 검토를 돕는 보조 정보를 만드는 것뿐이다 — "판정하기 어려움"을
무리하게 정상/문제로 단정하지 않도록 프롬프트와 스키마를 설계했다.
API 키와 이미지 바이트, 판정 원문(raw_response)은 절대 로그에 남기지 않는다.
"""
import asyncio
import logging
from enum import Enum
from typing import Optional

from google.genai import types
from pydantic import BaseModel

from app.config import (
    GEMINI_FLASH_MODEL,
    GEMINI_QUALITY_CONCURRENCY,
    GEMINI_QUALITY_MAX_RETRIES,
    GEMINI_QUALITY_TIMEOUT_SECONDS,
)
from app.gemini_client import get_client

logger = logging.getLogger(__name__)


class QualityLevel(str, Enum):
    OK = "ok"  # 문제 없음
    POSSIBLE = "possible"  # 문제 가능성 있음
    LIKELY = "likely"  # 명확한 문제 의심
    UNKNOWN = "unknown"  # 판정하기 어려움(주요 인물 특정 불가 포함) — 불량으로 단정하지 않음


class PhotoQualityAssessment(BaseModel):
    eyes_closed: QualityLevel
    blur_or_shake: QualityLevel
    focus_issue: QualityLevel
    face_occluded: QualityLevel
    primary_subject_detected: bool
    notes: Optional[str] = None


_PROMPT = """당신은 사진작가의 1차 검토를 돕는 보조 도구입니다. 이 사진 1장을 보고 아래 4가지 항목을
각각 "ok"(문제 없음) / "possible"(문제 가능성 있음) / "likely"(명확한 문제 의심) / "unknown"(판정하기 어려움) 중 하나로 판정하세요.

- eyes_closed: 주요 인물이 눈을 감았거나 감은 것처럼 보이는지
- blur_or_shake: 카메라 또는 피사체 움직임으로 흔들려 보이는지
- focus_issue: 주요 인물에 초점이 맞지 않은 것으로 의심되는지
- face_occluded: 주요 인물의 얼굴이 가려졌거나(손/머리카락/물체 등) 각도상 판정이 어려운지

판단 기준:
- "주요 인물"은 사진에서 가장 크게 또는 중심에 나온 인물입니다. 단체사진에서 배경의 작게 나온
  인물 한 명의 눈 상태 때문에 전체를 문제로 판정하지 마세요.
- 주요 인물을 명확히 특정하기 어렵거나(예: 다수 인물이 비슷한 비중, 인물이 아주 작음, 뒷모습/실루엣만
  보임) 판정 근거가 불충분하면 해당 항목을 "unknown"으로 표시하세요. 확실하지 않은 경우 무리하게
  "ok"나 "likely"로 단정하지 마세요.
- 다음은 정상적인 경우이며 문제로 판정하지 마세요: 의도적인 아웃포커싱(배경만 흐림), 패닝/의도적
  움직임 표현, 웃어서 눈이 가늘어진 경우, 역광/저조도 자체(단, 그로 인해 실제로 판정이 어려우면
  해당 항목만 unknown).
- primary_subject_detected: 주요 인물을 하나로 특정할 수 있었으면 true, 어려웠으면 false.
- notes: 판정 근거를 한국어로 1문장 이내로 간단히(선택, 비워도 됨).

주어진 JSON 스키마 형식으로만 응답하세요."""


def _build_usage(response) -> Optional[dict]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    return {
        "prompt_token_count": getattr(usage, "prompt_token_count", None),
        "candidates_token_count": getattr(usage, "candidates_token_count", None),
        "total_token_count": getattr(usage, "total_token_count", None),
    }


async def _assess_one(client, image_bytes: bytes, mime_type: str):
    last_exc: Optional[Exception] = None
    for attempt in range(GEMINI_QUALITY_MAX_RETRIES + 1):
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=GEMINI_FLASH_MODEL,
                    contents=[
                        _PROMPT,
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=PhotoQualityAssessment,
                        temperature=0,
                    ),
                ),
                timeout=GEMINI_QUALITY_TIMEOUT_SECONDS,
            )
            assessment = PhotoQualityAssessment.model_validate_json(response.text)
            return assessment, _build_usage(response)
        except Exception as e:
            last_exc = e
            if attempt < GEMINI_QUALITY_MAX_RETRIES:
                await asyncio.sleep(2**attempt)
                continue
    raise last_exc  # type: ignore[misc]


async def assess_images(
    images: list[Optional[bytes]],
) -> tuple[list[Optional[PhotoQualityAssessment]], list[dict]]:
    """순서를 보존하며 이미지별 품질 판정. 다운로드 실패(None) 또는 판정 실패 항목은 None.
    반환: (판정 리스트, 실제 usage_metadata 리스트)."""
    client = await get_client()
    sem = asyncio.Semaphore(GEMINI_QUALITY_CONCURRENCY)
    usages: list[dict] = []

    async def _run(idx: int, img: Optional[bytes]) -> Optional[PhotoQualityAssessment]:
        if img is None:
            return None
        async with sem:
            try:
                assessment, usage = await _assess_one(client, img, "image/jpeg")
                if usage:
                    usages.append(usage)
                return assessment
            except Exception as e:
                logger.warning("gemini quality assessment failed for image index=%d: %s", idx, e)
                return None

    results = await asyncio.gather(*[_run(i, img) for i, img in enumerate(images)])
    return list(results), usages
