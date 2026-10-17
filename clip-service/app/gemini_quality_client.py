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
from typing import Callable, Literal, Optional

from google.genai import types
from pydantic import BaseModel, ValidationError, create_model

from app.config import (
    GEMINI_CUSTOMER_QUALITY_SERVICE_TIER,
    GEMINI_FLEX_TIMEOUT_SECONDS,
    GEMINI_FLASH_MODEL,
    GEMINI_QUALITY_CONCURRENCY,
    GEMINI_QUALITY_MAX_RETRIES,
    GEMINI_QUALITY_TIMEOUT_SECONDS,
    GEMINI_STAGE_TIMEOUT_SECONDS,
)
from app.gemini_client import get_client, is_retryable, retry_delay

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


class PeopleKind(str, Enum):
    SOLO = "solo"  # 한 사람 단독(돌잔치면 아기 독사진)
    FAMILY = "family"  # 가족·커플 등 가까운 소수(2~6명)
    GROUP = "group"  # 하객·단체 등 여러 사람
    NONE = "none"  # 인물 없음(공간·소품·상차림 디테일)


class CustomerPhotoAssessment(PhotoQualityAssessment):
    """셀프 고객 전용 — 품질 판정에 인물 구성을 더한다(장면 안에서 아기 단독·가족 등으로 걸러 보기용).
    작가 판정(PhotoQualityAssessment)은 바꾸지 않는다: 프롬프트가 바뀌면 작가 쪽 저장된 판정이 모두 무효가 된다."""
    people: PeopleKind


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

_CUSTOMER_PROMPT = _PROMPT.replace("주어진 JSON 스키마 형식으로만 응답하세요.", """- people: 사진의 인물 구성을 하나로 고르세요.
  "solo" = 한 사람이 단독으로 주인공(다른 사람은 손·뒷모습 정도만), "family" = 가족·커플 등 2~6명이 함께,
  "group" = 하객·단체 등 7명 이상이거나 여러 무리가 함께, "none" = 사람이 없거나 공간·소품·음식 디테일이 주인공.

주어진 JSON 스키마 형식으로만 응답하세요.""")


UNKNOWN_PLACE = "알 수 없음"


def customer_schema(place_names: Optional[list[str]] = None):
    """셀프 고객 판정 스키마·프롬프트. place_names(홈스냅)가 있으면 같은 호출에서 사진마다 찍은 장소(목록 중 하나 또는 UNKNOWN_PLACE)도
    묻는다 — 사진을 한 번만 보낸다(장소 목록 기준 입력 토큰 +약 9%). 웨딩 본식 단계는 여기서 묻지 않는다(`judge_stages`)."""
    if not place_names:
        return CustomerPhotoAssessment, _CUSTOMER_PROMPT
    places = [*place_names, UNKNOWN_PLACE]
    schema = create_model("CustomerPlaceAssessment", __base__=CustomerPhotoAssessment, place=(Literal[tuple(places)], ...))
    end = "주어진 JSON 스키마 형식으로만 응답하세요."
    field = f"""- place: 이 사진을 찍은 장소를 배경을 보고 목록 중 하나로 고르세요. 배경이 거의 안 보여 장소를 알 수 없으면 "{UNKNOWN_PLACE}".
  목록: {", ".join(places)}"""
    return schema, _CUSTOMER_PROMPT.replace(end, f"{field}\n\n{end}")


# 단계 판정(웨딩 본식): 흔들림 판정과 따로, 촬영 순서로 이어진 사진 STAGE_BATCH_PHOTOS장을 한 호출에 보여 주고 사진마다 단계를 받는다.
# 앞뒤 흐름을 봐야 맞히는 사진(노래를 듣는 신부 = 축가, 버진로드 행진 사이 키스 = 행진, 아버지가 신랑에게 인계 = 신부 입장)을 한 장만 보고는
# 예식으로 읽었다. 2026-10-15 짱쭌 본식 1,285장(잠정 정답): 흔들림 호출에 얹은 한 장 판정 84.4% → 10장 묶음 89.3%(다듬은 뒤 93.5 → 94.7%,
# 장면 경계 ±3장 정밀도 67 → 90%). 묶음끼리 STAGE_BATCH_OVERLAP장 겹쳐 앞 묶음 끝을 맥락으로만 다시 본다(라벨은 앞 묶음 것).
# 해상도는 낮음(장당 약 280토큰): 기본 해상도(85.7%, $0.60)보다 정확하고 싸다 — 1,285장 약 $0.20, 3,439장 약 $0.55(표준 단가).
# 구간 대표 사진만 한 번에 보여 주는 방식은 85.4%로 낮았고 구간 141개짜리 응답은 개수를 못 맞췄다. 판단 기준 문구를 더 늘린 v3(하객 유무·
# 앉은 신부)는 두 프로젝트 모두 낮아졌다(94.7 → 93.4%, 81.0 → 76.7%).
# 문구·묶음 크기·해상도를 바꾸면 버전을 올린다(판정 캐시 버전에 들어가 전체 재판정).
STAGE_PROMPT_VERSION = "stage-v2-context10-low"
STAGE_BATCH_PHOTOS = 10
STAGE_BATCH_OVERLAP = 2
STAGE_MEDIA_RESOLUTION = types.MediaResolution.MEDIA_RESOLUTION_LOW
# 응답 개수 제한(minItems·maxItems)은 이 개수 이하에서만 건다 — 56처럼 크면 Gemini가 400(INVALID_ARGUMENT)으로 거절한다.
_SCHEMA_MAX_ITEMS = 20

_STAGE_PROMPT = """아래 사진 {n}장은 한 결혼식 본식을 시간 순서대로 이어 찍은 사진입니다(사진 1이 가장 먼저).
사진마다 촬영 단계를 아래 목록에서 하나씩 고르세요. 한 장만 보지 말고 앞뒤 사진의 흐름을 함께 보세요 — 예를 들어 노래하는 사람 앞뒤의
신부 단독 컷은 축가를 듣는 장면이고, 버진로드를 걸어 나가는 사진 사이의 키스 클로즈업은 행진입니다. 어느 단계에나 나올 수 있는
하객 얼굴·손·소품 클로즈업은 앞뒤 사진의 단계를 따르세요. 정말 알 수 없을 때만 "{unknown}".
단계(이름: 판단 기준):
{lines}
stages에는 사진 1부터 차례로 정확히 {n}개를 적으세요."""


class _StageList(BaseModel):
    stages: list[str]


def stage_prompt(count: int, stage_names: list[str], hints: Optional[dict[str, str]] = None) -> str:
    hints = hints or {}
    lines = "\n".join(f"- {name}: {hints[name]}" if hints.get(name) else f"- {name}" for name in stage_names)
    return _STAGE_PROMPT.format(n=count, unknown=UNKNOWN_PLACE, lines=lines)


def stage_schema(count: int, stage_names: list[str]) -> types.Schema:
    stages = types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING, enum=[*stage_names, UNKNOWN_PLACE]),
                          **({"min_items": count, "max_items": count} if count <= _SCHEMA_MAX_ITEMS else {}))
    return types.Schema(type=types.Type.OBJECT, properties={"stages": stages}, required=["stages"])


def stage_windows(count: int, context: int = 0) -> list[tuple[int, int, int]]:
    """사진 count장(앞 context장은 맥락으로만)을 나눈 묶음 [(시작, 끝(미포함), 이 묶음에서 라벨을 쓰는 첫 위치)].
    묶음끼리 STAGE_BATCH_OVERLAP장 겹치고, 겹친 앞부분(첫 묶음은 맥락 사진)은 라벨을 쓰지 않는다."""
    windows: list[tuple[int, int, int]] = []
    start = 0
    while count > context:
        end = min(count, start + STAGE_BATCH_PHOTOS)
        windows.append((start, end, context if start == 0 else STAGE_BATCH_OVERLAP))
        if end == count:
            break
        start = end - STAGE_BATCH_OVERLAP
    return windows


# ponytail: 운영(Python 3.11)의 SDK는 service_tier를 받지만 로컬 Python 3.9용 SDK(1.47)에는 없다 — 없으면 표준으로 보낸다.
_SUPPORTS_SERVICE_TIER = "service_tier" in types.GenerateContentConfig.model_fields


def customer_service_tier() -> str:
    """셀프 고객 판정에 실제로 쓰는 서비스 티어(실행 settings 기록용)."""
    return GEMINI_CUSTOMER_QUALITY_SERVICE_TIER if _SUPPORTS_SERVICE_TIER else "standard"


def _build_usage(response) -> Optional[dict]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    return {
        "prompt_token_count": getattr(usage, "prompt_token_count", None),
        "candidates_token_count": getattr(usage, "candidates_token_count", None),
        # thinking 토큰은 candidates에 포함되지 않지만 출력 단가로 과금된다 — 따로 세지 않으면 비용이 안 보인다.
        "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
        "total_token_count": getattr(usage, "total_token_count", None),
    }


def sum_usage(usages: list[dict], stats: Optional[dict] = None) -> dict:
    """호출별 usage 합계(실행 기록용). 출력 비용 = candidates + thoughts. calls는 성공해 usage를 받은 응답 수,
    attempts·failed_attempts는 실제로 보낸 요청 수와 그중 실패 수(실패·타임아웃 요청도 과금될 수 있어 따로 센다)."""
    total = lambda key: sum(usage.get(key) or 0 for usage in usages)  # noqa: E731
    return {
        "calls": len(usages),
        **({"attempts": stats.get("attempts", 0), "failed_attempts": stats.get("failed_attempts", 0)} if stats is not None else {}),
        "prompt_tokens": total("prompt_token_count"),
        "output_tokens": total("candidates_token_count"),
        "thinking_tokens": total("thoughts_token_count"),
        "total_tokens": total("total_token_count"),
    }


async def _assess_one(client, image_bytes: bytes, mime_type: str, customer: bool = False, stats: Optional[dict] = None,
                      place_names: Optional[list[str]] = None):
    schema, prompt = customer_schema(place_names) if customer else (PhotoQualityAssessment, _PROMPT)
    tier = {"service_tier": GEMINI_CUSTOMER_QUALITY_SERVICE_TIER} if customer and _SUPPORTS_SERVICE_TIER else {}
    flex = tier.get("service_tier") == "flex"
    stats = stats if stats is not None else {}
    last_exc: Optional[Exception] = None
    for attempt in range(GEMINI_QUALITY_MAX_RETRIES + 1):
        stats["attempts"] = stats.get("attempts", 0) + 1
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=GEMINI_FLASH_MODEL,
                    contents=[
                        prompt,
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=schema,
                        temperature=0,
                        **tier,
                    ),
                ),
                timeout=GEMINI_FLEX_TIMEOUT_SECONDS if flex else GEMINI_QUALITY_TIMEOUT_SECONDS,
            )
            assessment = schema.model_validate_json(response.text)
            return assessment, _build_usage(response)
        except Exception as e:
            last_exc = e
            stats["failed_attempts"] = stats.get("failed_attempts", 0) + 1
            # 일시적 오류만 재시도. 응답 JSON이 스키마에 안 맞으면 한 번만 다시 묻는다(같은 요청을 계속 반복하지 않는다).
            # Flex 타임아웃은 이미 오래 기다린 것이라 다시 보내지 않는다. Flex 혼잡(429)은 더 길게 기다린다.
            retry = (is_retryable(e) and not (flex and isinstance(e, asyncio.TimeoutError))) \
                or (isinstance(e, ValidationError) and attempt == 0)
            if attempt < GEMINI_QUALITY_MAX_RETRIES and retry:
                await asyncio.sleep(retry_delay(e, attempt, base=5.0 if flex else 1.0))
                continue
            raise
    raise last_exc  # type: ignore[misc]


async def assess_images(
    images: list[Optional[bytes]],
    on_each: Optional[Callable[[], None]] = None,
    customer: bool = False,
    stats: Optional[dict] = None,
    place_names: Optional[list[str]] = None,
) -> tuple[list[Optional[PhotoQualityAssessment]], list[dict]]:
    """순서를 보존하며 이미지별 품질 판정. 다운로드 실패(None) 또는 판정 실패 항목은 None.
    반환: (판정 리스트, 실제 usage_metadata 리스트)."""
    client = await get_client()
    sem = asyncio.Semaphore(GEMINI_QUALITY_CONCURRENCY)
    usages: list[dict] = []

    async def _run(idx: int, img: Optional[bytes]) -> Optional[PhotoQualityAssessment]:
        if img is None:
            if on_each:
                on_each()
            return None
        async with sem:
            try:
                assessment, usage = await _assess_one(client, img, "image/jpeg", customer, stats, place_names)
                if usage:
                    usages.append(usage)
                return assessment
            except Exception as e:
                logger.warning("gemini quality assessment failed for image index=%d: %s", idx, e)
                return None
            finally:
                if on_each:
                    on_each()

    results = await asyncio.gather(*[_run(i, img) for i, img in enumerate(images)])
    return list(results), usages


async def _judge_stage_window(client, images: list[Optional[bytes]], stage_names: list[str], hints: Optional[dict[str, str]],
                              stats: dict) -> tuple[list[str], Optional[dict]]:
    """묶음 하나(이어 찍은 사진들)의 사진별 단계. 재시도는 `_assess_one`과 같은 규칙(응답 형식·개수가 틀리면 한 번만 다시 묻는다)."""
    contents: list = [stage_prompt(len(images), stage_names, hints)]
    for k, image in enumerate(images):
        contents.append(f"사진 {k + 1}")
        contents.append(types.Part.from_bytes(data=image, mime_type="image/jpeg") if image else "(이미지 없음)")
    tier = {"service_tier": GEMINI_CUSTOMER_QUALITY_SERVICE_TIER} if _SUPPORTS_SERVICE_TIER else {}
    flex = tier.get("service_tier") == "flex"
    allowed = {*stage_names, UNKNOWN_PLACE}
    last_exc: Optional[Exception] = None
    for attempt in range(GEMINI_QUALITY_MAX_RETRIES + 1):
        stats["attempts"] = stats.get("attempts", 0) + 1
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=GEMINI_FLASH_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=stage_schema(len(images), stage_names),
                        temperature=0,
                        media_resolution=STAGE_MEDIA_RESOLUTION,
                        **tier,
                    ),
                ),
                timeout=GEMINI_FLEX_TIMEOUT_SECONDS if flex else GEMINI_STAGE_TIMEOUT_SECONDS,
            )
            stages = _StageList.model_validate_json(response.text).stages
            if len(stages) != len(images) or not allowed.issuperset(stages):
                raise ValueError(f"stage answer has {len(stages)} items for {len(images)} photos or an unlisted stage")
            return stages, _build_usage(response)
        except Exception as e:
            last_exc = e
            stats["failed_attempts"] = stats.get("failed_attempts", 0) + 1
            retry = (is_retryable(e) and not (flex and isinstance(e, asyncio.TimeoutError))) \
                or (isinstance(e, (ValidationError, ValueError)) and attempt == 0)
            if attempt < GEMINI_QUALITY_MAX_RETRIES and retry:
                await asyncio.sleep(retry_delay(e, attempt, base=5.0 if flex else 1.0))
                continue
            raise
    raise last_exc  # type: ignore[misc]


async def judge_stages(images: list[Optional[bytes]], stage_names: list[str], hints: Optional[dict[str, str]] = None,
                       context: int = 0, stats: Optional[dict] = None) -> tuple[list[Optional[str]], list[dict]]:
    """촬영 순서로 놓인 사진들의 단계(`stage_windows` 묶음마다 한 호출). images 앞 context장은 앞 묶음 끝 사진 — 맥락으로만 보고 라벨은 돌려주지 않는다.
    반환: (images[context:]와 같은 순서의 단계 — 실패한 묶음의 사진은 None, 실제 usage 목록)."""
    client = await get_client()
    sem = asyncio.Semaphore(GEMINI_QUALITY_CONCURRENCY)
    stats = stats if stats is not None else {}
    labels: list[Optional[str]] = [None] * len(images)
    usages: list[dict] = []

    async def run(start: int, end: int, keep: int):
        async with sem:
            try:
                stages, usage = await _judge_stage_window(client, images[start:end], stage_names, hints, stats)
            except Exception as e:  # 묶음 하나가 실패해도 나머지로 다듬는다(그 사진들은 판정 없음 = 앞 단계를 따른다)
                logger.warning("gemini stage judgment failed for photos %d-%d: %s", start, end - 1, type(e).__name__)
                return
            if usage:
                usages.append(usage)
            labels[start + keep:end] = stages[keep:]

    await asyncio.gather(*[run(*window) for window in stage_windows(len(images), context)])
    return labels[context:], usages
