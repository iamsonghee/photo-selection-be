"""셀프 고객 사진용 Gemini 분석. 모델 호출 코드는 작가 분석과 공유하고 저장소만 분리한다.

실행 공통 규칙: 실행 내내 heartbeat(`_heartbeat`)로 진행 시각을 남기고, Gemini를 부르기 전·결과를 쓰기 직전에
아직 현재 실행인지 확인한다(`_ensure_running` — 멈춘 것으로 닫혀 대체된 실행은 비용을 더 쓰지 않고 멈춘다).
사진을 내려받는 실행은 BATCH_PHOTOS장씩 내려받기 → 판정 → 저장하고, 프로세스당 CUSTOMER_AI_HEAVY_RUNS개까지만 동시에 돈다.
"""
import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, Optional

import numpy as np
from google.genai import types
from pydantic import BaseModel

from app.config import (CUSTOMER_AI_HEAVY_RUNS, GEMINI_EMBEDDING_DIMENSION, GEMINI_EMBEDDING_MODEL,
                        GEMINI_EMBEDDING_VERSION, GEMINI_FLASH_MODEL, GEMINI_FLEX_TIMEOUT_SECONDS,
                        GEMINI_QUALITY_PROMPT_VERSION, GEMINI_QUALITY_TIMEOUT_SECONDS)
from app.db import get_supabase
from app.downloader import download_all
from app.gemini_client import embed_images, get_client
from app.gemini_quality_client import UNKNOWN_PLACE, _build_usage, assess_images, customer_service_tier, sum_usage
from app.grouping import (SHOT_ANCHOR_MARGIN, SHOT_MAX, SHOT_MAX_GAP_SECONDS, SHOT_MIN, SHOT_PERCENTILE, group_shots)
from app.scenes import (CLOSE_GAP_SECONDS, CONTENT_CUT_SIMILARITY, CONTENT_MAX_SCENES,
                        CONTENT_MIN_SCENE_PHOTOS, CONTENT_WINDOW, MIN_PHOTOS_FOR_SCENES,
                        MIN_SCENE_PHOTOS, PLACE_MIN_RUN, PLACE_WINDOW, SCENE_SETTINGS,
                        CONTENT_VISIBLE_MAX, CONTENT_VISIBLE_MIN, CONTENT_VISIBLE_TARGET,
                        scene_gap, scene_taken_at, size_content_sections, split_by_content, split_by_place, split_scenes)

logger = logging.getLogger(__name__)
OTHER_SCENE = "기타 장면"
# 장소를 알 수 없는 이름 — 작으면 이웃 장면에 붙인다(`absorb_placeless`). "클로즈업·디테일"은 FE 홈스냅 카탈로그 이름과 같아야 한다.
PLACELESS_SCENES = {OTHER_SCENE, "클로즈업·디테일"}
# 이보다 적고 붙을 이웃의 절반보다 작은 장소 없는 장면만 이웃에 붙인다 — 큰 디테일 컷 묶음은 따로 둔다(2026-10-05 홈스냅:
# 워밍업 디테일 컷 10장 → 거실 116장). 이웃 비율 조건이 없으면 사진이 적은 프로젝트(30장)에서 15장짜리 장면이 통째로 옆 장소에 먹힌다.
PLACELESS_ABSORB_PHOTOS = 20
# 셀프 고객 판정은 인물 구성(people)까지 묻는 별도 프롬프트라 버전을 따로 둔다 — 작가 판정 캐시와 섞이지 않게.
CUSTOMER_QUALITY_PROMPT_VERSION = f"{GEMINI_QUALITY_PROMPT_VERSION}-people"
# 장소 기준 장면(홈스냅)에서 실행 settings에 남기는 값 — 시간 기준 장면과 검수 채점에서 구분.
# 내용 기준 장면(촬영 시각 없음) 묘사: 이름 목록에서 고르면 웨딩 촬영은 실내가 전부 "스튜디오"가 됐다(2,249장에서 30개 중 24개).
# 작은 구간의 배경·의상 묘사는 큰 구간의 분리 후보를 고를 때 쓴다. 앞 표현을 넘겨야 같은 세트를 같은 말로 적는다. 대표 사진은 이름 고르기(3장)보다
# 많이 본다 — 3장으로는 어두운 유리창 너머 브라운 수트를 검정 턱시도로 읽었다.
DESCRIBE_PROMPT_VERSION = "describe-v6-general-props"
DESCRIBE_SAMPLE_PHOTOS = 5
SECTION_PROMPT_VERSION = "sections-v3-outfit-continuity"
SECTION_SAMPLE_PHOTOS = 2
# 내용 기준 장면(촬영 시각 없음)에서 실행 settings에 남기는 값.
CONTENT_SCENE_SETTINGS = {"boundary": "content", "contentWindow": CONTENT_WINDOW, "contentCutSimilarity": CONTENT_CUT_SIMILARITY,
                          "contentMinScenePhotos": CONTENT_MIN_SCENE_PHOTOS, "contentMaxScenes": CONTENT_MAX_SCENES,
                          "embeddingModel": GEMINI_EMBEDDING_MODEL, "namePromptVersion": DESCRIBE_PROMPT_VERSION,
                          "sampleCount": DESCRIBE_SAMPLE_PHOTOS, "sectionPromptVersion": SECTION_PROMPT_VERSION,
                          "sectionSampleCount": SECTION_SAMPLE_PHOTOS, "visibleTarget": CONTENT_VISIBLE_TARGET,
                          "visibleMax": CONTENT_VISIBLE_MAX, "visibleMinSplit": CONTENT_VISIBLE_MIN,
                          "visibleGrouping": "adaptive-anchor-gap"}
PLACE_SCENE_SETTINGS = {"boundary": "place", "placeWindow": PLACE_WINDOW, "placeMinRun": PLACE_MIN_RUN, "hardCutSeconds": CLOSE_GAP_SECONDS}


def place_list(names: Optional[list[str]]) -> Optional[list[str]]:
    """장면 이름 목록 중 장소인 것만(클로즈업·디테일 같은 장소 없는 이름은 뺀다 — 장소를 모르면 "알 수 없음"으로 받는다)."""
    places = [name for name in names or [] if name not in PLACELESS_SCENES]
    return places or None


def quality_prompt_version(place_names: Optional[list[str]] = None) -> str:
    """장소도 묻는 판정은 장소 목록마다 버전이 다르다 — 목록이 바뀌면 다시 판정한다(같은 목록이면 캐시 재사용)."""
    if not place_names:
        return CUSTOMER_QUALITY_PROMPT_VERSION
    digest = hashlib.sha1(json.dumps(place_names, ensure_ascii=False).encode()).hexdigest()[:8]
    return f"{CUSTOMER_QUALITY_PROMPT_VERSION}-place-{digest}"
SCENE_SAMPLE_PHOTOS = 3
# 장면 이름 프롬프트 버전 — 프롬프트·응답 형식을 바꾸면 올린다(실행 settings에 남아 검수 채점에서 구분).
SCENE_NAME_PROMPT_VERSION = "v2-confident"
PAGE_ROWS = 1000  # PostgREST 최대 행 수 — 넘는 조회는 나눠 읽는다(셀프 고객 한도 3,000장)
# 임베딩은 3,072차원일 때 한 행이 JSON 약 62KB(지금 기본 768차원은 약 16KB)라 1,000행이면 요청 하나가 약 62MB — Postgres가 이 JSON을 쿼리 하나 안에서
# 만들다 운영 DB가 20분 멈췄다(2026-10-05, 1,285장 재정리). 한 요청 약 6MB로 나눠 읽는다.
EMBEDDING_PAGE_ROWS = 100
BATCH_PHOTOS = 40  # 한 번에 내려받아 판정·저장하는 사진 수(메모리: 1200px 미리보기 40장 ≈ 10MB)
HEARTBEAT_SECONDS = 60

# 진행 갱신(updated_at)이 이만큼 없으면 멈춘 실행(서비스 재시작 등으로 프로세스가 사라진 것). 살아 있는 실행은
# heartbeat가 HEARTBEAT_SECONDS마다 갱신하므로 Gemini 응답이 느리거나 동시 실행 자리를 기다려도 닫히지 않는다.
STALE_RUN_SECONDS = 10 * 60


class _Superseded(Exception):
    """이 실행이 더는 현재 실행이 아님(멈춘 것으로 닫힘) — 결과를 쓰지 않고 조용히 멈춘다."""


_heavy_running = 0


@asynccontextmanager
async def _heavy_slot():
    """사진을 내려받는 실행의 프로세스 동시 실행 수 제한(CUSTOMER_AI_HEAVY_RUNS). 자리가 날 때까지 기다린다.
    # ponytail: 2초 폴링·순서 보장 없음 — 대기가 길어지면 asyncio.Condition 대기열로."""
    global _heavy_running
    while _heavy_running >= CUSTOMER_AI_HEAVY_RUNS:
        await asyncio.sleep(2)
    _heavy_running += 1
    try:
        yield
    finally:
        _heavy_running -= 1


@asynccontextmanager
async def _heartbeat(db, run_id: str):
    """실행이 살아 있는 동안 HEARTBEAT_SECONDS마다 updated_at을 갱신한다(진행 중인 실행만)."""
    async def beat():
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                db.table("customer_ai_runs").update({"updated_at": _now()}).eq("id", run_id).eq("status", "processing").execute()
            except Exception as exc:  # 기록 실패는 다음 주기에 다시
                logger.warning("heartbeat failed: %s", exc)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_stale(run: dict, now: datetime) -> bool:
    """진행 중인데 마지막 진행 갱신 뒤 STALE_RUN_SECONDS 넘게 멈춘 실행(서비스 재시작 등). 갱신 기록 전 실행은 시작 시각 기준."""
    last = run.get("updated_at") or run.get("started_at")
    if run.get("status") != "processing" or not last:
        return False
    last_at = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    return (now - last_at).total_seconds() > STALE_RUN_SECONDS


def _ensure_running(db, run_id: str):
    """이 실행이 아직 현재 실행인지 — 멈춘 것으로 닫혀 새 실행이 시작됐으면 Gemini 호출·결과 쓰기 전에 멈춘다."""
    rows = db.table("customer_ai_runs").select("status").eq("id", run_id).limit(1).execute().data or []
    if not rows or rows[0]["status"] != "processing":
        raise _Superseded()


def _all_rows(query, page_rows: int = PAGE_ROWS) -> list[dict]:
    """query(): 매번 새 조회(정렬 포함)를 만드는 함수. page_rows씩 끝까지 읽는다."""
    rows: list[dict] = []
    while True:
        page = query().range(len(rows), len(rows) + page_rows - 1).execute().data or []
        rows += page
        if len(page) < page_rows:
            return rows


def _progress(db, run_id: str, total: int, start: int, settings: Optional[dict] = None, every: int = 5):
    """진행 수 기록: 시작 시 전체·재사용 수(와 실행 설정)를 쓰고, 이후 every 이상 늘 때마다(그리고 마지막에) 처리 수를 갱신하는 콜백을 돌려준다."""
    count = {"done": start, "written": start}
    db.table("customer_ai_runs").update({
        "image_count": total, "processed_count": start, "updated_at": _now(),
        **({"settings": settings} if settings is not None else {}),
    }).eq("id", run_id).execute()

    def tick(step: int = 1):
        count["done"] += step
        if count["done"] - count["written"] >= every or count["done"] >= total:
            count["written"] = count["done"]
            try:
                db.table("customer_ai_runs").update({"processed_count": count["done"], "updated_at": _now()}).eq("id", run_id).execute()
            except Exception as exc:  # 진행 표시는 실패해도 분석은 계속한다
                logger.warning("progress update failed: %s", exc)
    return tick


def _done(db, run_id, total, processed, failed, error=None, usage: Optional[dict] = None):
    # 진행 중일 때만 닫는다 — 멈춘 것으로 이미 닫힌 실행이 늦게 끝나 failed를 completed로 되돌리지 않게.
    db.table("customer_ai_runs").update({
        "status": "failed" if error else "completed", "image_count": total,
        "processed_count": processed, "failed_count": failed, "error": error,
        "completed_at": _now(), "updated_at": _now(),
        **({"usage": usage} if usage is not None else {}),
    }).eq("id", run_id).eq("status", "processing").execute()


def capture_order(rows: list[dict]) -> list[dict]:
    """촬영 시각순(없으면 뒤로, 같으면 업로드 순). 유사컷은 인접한 사진끼리만 비교하므로
    업로드 순서가 아니라 실제로 연달아 찍은 순서로 늘어놓아야 연속 촬영을 놓치지 않는다.
    파일 수정 시각은 촬영 시각이 아니라 뺀다 — 보정본을 한꺼번에 내보내면 같은 초 안에서 순서가 섞인다(업로드 = 파일명 순서가 맞다)."""
    return sorted(rows, key=lambda row: (scene_taken_at(row) is None, scene_taken_at(row) or "", row["order_index"]))


def _shot_time(row: dict) -> Optional[float]:
    value = scene_taken_at(row)
    try:
        return datetime.fromisoformat(value.replace("Z", "")).timestamp() if value else None
    except ValueError:
        return None


async def _embeddings(db, run_id: str, project_id: str, rows: list[dict], tick=None) -> list[Optional[np.ndarray]]:
    """사진별 임베딩(rows 순서). 이미 저장된 사진은 재사용하고 새 사진만 BATCH_PHOTOS장씩 Gemini로 계산해 바로 저장한다
    (중간에 멈춰도 계산한 만큼은 남아 다음 실행이 재사용)."""
    stored = {
        row["photo_id"]: np.asarray(row["embedding"], dtype=np.float64)
        for row in _all_rows(lambda: db.table("customer_ai_embeddings").select("photo_id,embedding")
                             .eq("project_id", project_id).eq("model", GEMINI_EMBEDDING_MODEL)
                             .eq("dimension", GEMINI_EMBEDDING_DIMENSION).eq("version", GEMINI_EMBEDDING_VERSION)
                             .order("photo_id"), page_rows=EMBEDDING_PAGE_ROWS)
    }
    missing = [row for row in rows if row["id"] not in stored]
    if tick:
        tick(len(rows) - len(missing))
    for start in range(0, len(missing), BATCH_PHOTOS):
        batch = missing[start:start + BATCH_PHOTOS]
        _ensure_running(db, run_id)
        vectors, _ = await embed_images(await download_all([row["thumb_url"] for row in batch]), on_each=tick)
        payload = [{"project_id": project_id, "photo_id": row["id"],
                    "model": GEMINI_EMBEDDING_MODEL, "dimension": GEMINI_EMBEDDING_DIMENSION,
                    "version": GEMINI_EMBEDDING_VERSION, "embedding": vector.tolist()}
                   for row, vector in zip(batch, vectors) if vector is not None]
        _ensure_running(db, run_id)
        if payload:
            db.table("customer_ai_embeddings").upsert(
                payload, on_conflict="project_id,photo_id,model,dimension,version").execute()
        stored.update({row["id"]: vector for row, vector in zip(batch, vectors) if vector is not None})
    return [stored.get(row["id"]) for row in rows]


class _SceneName(BaseModel):
    name: str
    confident: bool


async def _name_scene(client, images: list[bytes], names: list[str], usages: Optional[list[dict]] = None) -> Optional[str]:
    """한 장면의 대표 사진들을 보고 촬영 종류별 장면 목록 중 하나를 고른다. 맞는 게 없거나 확신이 없으면 기타 장면,
    호출이 실패하면 None(이름만 못 붙인 것 — 장면은 그대로 쓰고 기타 장면으로 저장, 실패 수로 센다)."""
    prompt = (
        "다음 사진들은 한 촬영의 같은 장면에서 연달아 찍은 사진입니다. 아래 장면 목록 중 이 장면에 가장 맞는 이름을 "
        f"정확히 하나 고르세요. 맞는 것이 없으면 \"{OTHER_SCENE}\"을 고르세요. 사진만 보고 목록 중 하나라고 분명히 "
        "말할 수 있을 때만 confident를 true로, 두 이름 이상이 그럴듯하거나 애매하면 false로 하세요.\n장면 목록: " + ", ".join(names)
    )
    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_FLASH_MODEL,
            contents=[prompt, *[types.Part.from_bytes(data=image, mime_type="image/jpeg") for image in images]],
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_SceneName, temperature=0),
        )
        if usages is not None and (usage := _build_usage(response)):
            usages.append(usage)
        result = _SceneName.model_validate_json(response.text)
        name = result.name.strip()
        return name if result.confident and name in names else OTHER_SCENE
    except Exception as exc:
        logger.warning("scene naming failed: %s", exc)
        return None


class _SceneLook(BaseModel):
    place: str
    outfits: list[str]


async def _describe_scene(client, images: list[bytes], places: list[str], outfits: list[str],
                          usages: Optional[list[dict]] = None) -> Optional[dict]:
    """한 장면의 대표 사진들을 보고 {"place", "outfits"}를 적는다. 호출이 실패하면 None."""
    prompt = (
        "다음 사진들은 한 촬영의 같은 장면에서 연달아 찍은 사진입니다. 고객이 장면을 구분할 수 있게 짧게 적으세요.\n"
        "- place: 이 장면만의 배경·세트를 눈에 띄는 색·재질·소품으로 8자 안팎. 예: '주황 나무 계단', '유리 천장 방', '주황 격자문', "
        "'하트 풍선', '야외 정원', '흰 커튼 창가'. '흰 벽'처럼 어디에나 있는 표현은 다른 특징이 정말 없을 때만.\n"
        "- outfits: 보이는 주인공의 의상을 사람마다 색+종류 6자 안팎으로, 최대 2명. 신부·여성 먼저, 신랑·남성 다음. "
        "예: ['흰 드레스', '검정 턱시도'], ['한복'], 한 사람만 보이면 그 사람만. 사람이 없으면 [].\n"
        "배경뿐 아니라 주요 소품과 연출까지 앞 장면과 같을 때만 아래 장소 표현을 그대로(띄어쓰기까지) 다시 쓰세요. "
        "같은 배경이어도 하트 풍선·하트 티셔츠·꽃잎·리본·부케 같은 소품이 바뀌면 반드시 그 소품으로 새 place를 쓰세요. "
        "눈에 띄는 소품이 하나라도 있으면 흰 벽·흰 커튼 같은 일반 배경보다 소품을 우선하세요. 대표 사진에 보이지 않는 기존 표현은 쓰지 말고, "
        "인물 한 명의 작은 장신구보다 여러 대표 사진에 공통으로 보이는 특징을 고르세요. "
        "의상도 같다고 확실할 때만 기존 표현을 그대로 쓰세요.\n"
        f"이미 쓴 장소: {', '.join(places) or '없음'}\n이미 쓴 의상: {', '.join(outfits) or '없음'}"
    )
    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_FLASH_MODEL,
            contents=[prompt, *[types.Part.from_bytes(data=image, mime_type="image/jpeg") for image in images]],
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_SceneLook, temperature=0),
        )
        if usages is not None and (usage := _build_usage(response)):
            usages.append(usage)
        look = _SceneLook.model_validate_json(response.text)
        place = look.place.strip()
        return {"place": place, "outfits": [o.strip() for o in look.outfits if o.strip()][:2]} if place else None
    except Exception as exc:
        logger.warning("scene describing failed: %s", exc)
        return None


class _ContentSection(BaseModel):
    start: int
    name: str
    outfits: list[str]


class _ContentSections(BaseModel):
    basis: Literal["outfit", "activity"]
    sections: list[_ContentSection]


async def _plan_content_sections(client, scenes, descriptions, shoot_type, flagged, usages=None, check_running=None):
    """전체 구간의 대표 사진으로 큰 촬영 흐름을 정한다. 크기 분할은 size_content_sections에서 한다."""
    samples = [pick_samples(scene, flagged, SECTION_SAMPLE_PHOTOS) for scene in scenes]
    urls = [photo["preview_url"] for sample in samples for photo in sample]
    images = []
    for start in range(0, len(urls), BATCH_PHOTOS):
        if check_running:
            check_running()
        images.extend(await download_all(urls[start:start + BATCH_PHOTOS]))
    contents = [
        "아래 구간들은 한 촬영의 시간순 연속 구간입니다. 전체 촬영 흐름을 보고 고객이 사진을 고르기 좋은 큰 구간으로 묶으세요.\n"
        "스튜디오·연출 촬영은 주인공 의상 조합이 확실히 바뀔 때 큰 구간을 나누세요. 같은 의상의 배경·소품·포즈 변화, "
        "커플/단독 전환, 부케·반지 디테일은 같은 큰 구간입니다. 어두운 조명으로 색이 달라 보이는 의상은 전후 사진을 함께 보고 판단하세요.\n"
        "행사·생활 촬영은 의상보다 활동·행사 단계·장소 이동을 우선하세요. 촬영 유형만으로 스튜디오라고 단정하지 말고 사진을 보세요.\n"
        "basis는 의상 중심 연출 촬영이면 outfit, 행사·생활의 활동 중심이면 activity입니다. "
        "outfit일 때 반지·부케 같은 인물 없는 디테일 구간을 독립 sections로 만들지 말고 같은 촬영의 앞뒤 의상 구간에 포함하세요.\n"
        "각 section의 outfits에는 주인공별 의상 식별 표현을 적고 동일한 의상에는 전체 구간에서 정확히 같은 표현을 쓰세요. "
        "다른 드레스라면 색이 같아도 실루엣·재질의 뚜렷한 차이를 표현에 포함하세요. 한 사람이 안 보이는 것은 의상 변화가 아닙니다. "
        "같은 드레스의 신부 단독과 커플 구간, 같은 의상의 실내와 야외·야간은 한 section으로 묶으세요. activity의 outfits는 []여도 됩니다.\n"
        "의상이나 활동의 확실한 변화는 짧아도 보존하되 근거 없이 잘게 나누지 마세요. 같은 의상이 나중에 돌아와도 "
        "중간 구간을 건너뛰어 합치지 마세요. 장수와 크기에 따른 분리는 이후 별도로 하므로 여기서는 의미 있는 큰 흐름만 정하세요.\n"
        "기존 묘사는 오판할 수 있으니 사진을 우선하세요. 이름은 큰 구간 전체를 대표하는 짧은 한국어(의상 조합 또는 활동·장소)로 적으세요.\n"
        "sections는 {start: 시작 구간 번호(0부터), name: 이름, outfits: 의상 식별 표현 목록}입니다. 첫 start는 0, 이후는 엄격한 오름차순이고 "
        "각 구간은 다음 start 직전까지 포함합니다. 구간 번호를 빠뜨리거나 순서를 바꾸지 마세요.\n"
        f"촬영 유형: {shoot_type or '미지정'}, 전체 구간 수: {len(scenes)}"
    ]
    offset = 0
    for index, (sample, description) in enumerate(zip(samples, descriptions)):
        available = [image for image in images[offset:offset + len(sample)] if image]
        offset += len(sample)
        if not available:
            raise ValueError(f"content section {index} has no representative image")
        contents.append(f"구간 {index}: 기존 묘사 {json.dumps(description, ensure_ascii=False)}")
        contents.extend(types.Part.from_bytes(data=image, mime_type="image/jpeg") for image in available)
    if check_running:
        check_running()
    response = await client.aio.models.generate_content(
        model=GEMINI_FLASH_MODEL, contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_ContentSections, temperature=0))
    if usages is not None and (usage := _build_usage(response)):
        usages.append(usage)
    return _ContentSections.model_validate_json(response.text).model_dump()


def merge_same_named(scenes: list[list[dict]], names: list[Optional[str]]) -> tuple[list[list[dict]], list[Optional[str]]]:
    """바로 붙은 장면의 이름이 같고 사이 공백이 짧으면(CLOSE_GAP 미만) 한 장면으로 합친다 — 시간 공백이 한 장면을
    잘못 나눈 경우(예: 하객, 하객). 공백이 길어도 한쪽이 작은 장면(MIN_SCENE_PHOTOS 미만)이면 합친다 — 작은 장면은
    시간 공백 때문에만 따로 남은 것이라 같은 이름이면 따로 둘 근거가 없다. 둘 다 크고 공백이 길면 다른 시점이라 그대로 둔다
    (이름 오판이 큰 시간 경계를 없애지 않게). 이름 없는 장면·기타 장면은 합치지 않는다."""
    merged: list[list[dict]] = []
    merged_names: list[Optional[str]] = []
    for scene, name in zip(scenes, names):
        if (name not in (None, OTHER_SCENE) and merged_names and merged_names[-1] == name
                and (scene_gap(merged[-1], scene) < CLOSE_GAP_SECONDS
                     or min(len(merged[-1]), len(scene)) < MIN_SCENE_PHOTOS)):
            merged[-1] = merged[-1] + scene
        else:
            merged.append(scene)
            merged_names.append(name)
    return merged, merged_names


def absorb_placeless(scenes: list[list[dict]], names: list[Optional[str]]) -> tuple[list[list[dict]], list[Optional[str]]]:
    """장소 없는 작은 장면(기타 장면·클로즈업, PLACELESS_ABSORB_PHOTOS 미만이고 이웃의 절반 미만)을 공백이 더 짧은 이웃 장면(CLOSE_GAP 미만)에
    붙이고 이웃 이름을 따른다 — 배경 없는 디테일 컷은 사진만으로 장소를 못 고르지만(같은 사진에 답이 흔들림) 바로 앞뒤에서
    이어 찍은 것이라 그 장소다. 이웃도 장소 없는 장면이거나 이름이 없으면(촬영 시각 없는 장면) 붙이지 않는다."""
    scenes, names = list(scenes), list(names)
    absorbed = True
    while absorbed:
        absorbed = False
        for i, (scene, name) in enumerate(zip(scenes, names)):
            if name not in PLACELESS_SCENES or len(scene) >= PLACELESS_ABSORB_PHOTOS:
                continue
            gaps = [(scene_gap(scenes[j], scene) if j < i else scene_gap(scene, scenes[j]), j) for j in (i - 1, i + 1)
                    if 0 <= j < len(scenes) and names[j] is not None and names[j] not in PLACELESS_SCENES
                    and len(scene) * 2 < len(scenes[j])]
            gaps = [item for item in gaps if item[0] < CLOSE_GAP_SECONDS]
            if not gaps:
                continue
            j = min(gaps)[1]
            lo = min(i, j)
            scenes[lo:lo + 2] = [scenes[lo] + scenes[lo + 1]]
            names[lo:lo + 2] = [names[j]]
            absorbed = True
            break
    return scenes, names


def number_repeated(names: list[Optional[str]]) -> list[Optional[str]]:
    """같은 이름이 (합치지 않고) 두 번 이상 남으면 순서대로 번호를 붙인다 — "야외", "야외" → "야외 1", "야외 2".
    순서 정보는 모델이 아니라 여기서 붙인다(모델은 사진으로 보이는 이름만 고른다). 기타 장면·이름 없음은 그대로."""
    repeated = {name for name in names if name not in (None, OTHER_SCENE) and names.count(name) > 1}
    seen: dict[str, int] = {}
    numbered = []
    for name in names:
        if name in repeated:
            seen[name] = seen.get(name, 0) + 1
            name = f"{name} {seen[name]}"
        numbered.append(name)
    return numbered


def pick_samples(scene: list[dict], flagged: set[str], count: int = SCENE_SAMPLE_PHOTOS) -> list[dict]:
    """이름 붙일 대표 사진: 흔들림·눈 감음이 뚜렷한 사진은 빼고(다 빠지면 그대로), 같은 유사컷 묶음은 한 장만 남긴 뒤
    장면 앞·중간·뒤에서 고르게 고른다. 품질·유사컷 결과는 이미 있을 때만 쓴다(장면 이름이 그 분석을 기다리지 않게)."""
    candidates = [photo for photo in scene if photo["id"] not in flagged] or scene
    seen: set[str] = set()
    unique = []
    for photo in candidates:
        group = photo.get("similarity_group_id")
        if group in seen:
            continue
        if group:
            seen.add(group)
        unique.append(photo)
    step = max(1, len(unique) // count)
    return unique[step // 2::step][:count]


def _flagged_photos(db, project_id: str) -> set[str]:
    """이미 판정된 사진 중 흔들림·초점·눈 감음이 likely인 사진(대표 사진에서 뺀다)."""
    # 판정은 사진마다 현재 모델 한 행만 남는다(_run_quality가 정리) — 장소를 묻는 버전도 같은 값을 쓰도록 버전은 거르지 않는다.
    rows = _all_rows(lambda: db.table("customer_quality_assessments").select("photo_id,eyes_closed,blur_or_shake,focus_issue")
                     .eq("project_id", project_id).eq("model", GEMINI_FLASH_MODEL).order("photo_id"))
    return {row["photo_id"] for row in rows
            if "likely" in (row.get("eyes_closed"), row.get("blur_or_shake"), row.get("focus_issue"))}


def _place_scenes(db, project_id: str, place_names: Optional[list[str]], rows: Optional[list[dict]] = None):
    """흔들림 확인에서 사진마다 판정한 장소로 나눈 장면(이름 = 장소, 반복은 번호). 장소 판정이 없거나 부족하거나(흔들림 확인을
    껐거나 아직 진행 중) 지금 촬영 종류의 장소 목록과 맞지 않으면(촬영 종류를 바꾼 뒤) None — 시간 기준 장면을 쓴다."""
    place_names = place_list(place_names)
    if not place_names:
        return None
    labels = {row["photo_id"]: row.get("place") for row in _all_rows(lambda: db.table("customer_quality_assessments")
              .select("photo_id,place:raw_response->>place").eq("project_id", project_id)
              .eq("model", GEMINI_FLASH_MODEL).eq("prompt_version", quality_prompt_version(place_names)).order("photo_id"))}
    if rows is None:
        rows = _all_rows(lambda: db.table("customer_photos").select("id,order_index,taken_at,taken_at_source")
                         .eq("project_id", project_id).order("id"))
    placed = split_by_place(rows, labels, PLACELESS_SCENES | {UNKNOWN_PLACE}, OTHER_SCENE)
    return placed and (placed[0], number_repeated(placed[1]))


def scene_key(scene: list[dict]) -> str:
    """장면 사진 구성의 짧은 지문 — 다시 정리할 때 구성이 같은 장면은 이름을 다시 묻지 않는다(nameCache)."""
    return hashlib.sha1(",".join(sorted(photo["id"] for photo in scene)).encode()).hexdigest()[:16]


def _name_cache(db, project_id: str, settings: dict) -> dict[str, str]:
    """최근 완료한 장면 실행이 붙인 이름(장면 지문 → 이름). 이름 목록·모델·프롬프트가 같을 때만 쓴다(바뀌면 다시 묻는다)."""
    try:
        data = db.table("customer_ai_runs").select("settings").eq("project_id", project_id).eq("kind", "scene") \
            .eq("status", "completed").order("created_at", desc=True).limit(1).execute().data
        previous = data[0]["settings"] if isinstance(data, list) and data else None
    except Exception as exc:  # 캐시는 비용 절약일 뿐 — 못 읽으면 다시 묻는다
        logger.warning("scene name cache read failed: %s", exc)
        return {}
    if not isinstance(previous, dict) or any(previous.get(key) != settings[key] for key in ("catalog", "nameModel", "namePromptVersion")):
        return {}
    cache = previous.get("nameCache")
    return cache if isinstance(cache, dict) else {}


def _mark_place_scenes(db, project_id: str):
    """흔들림 확인이 장면을 장소 기준으로 바꿨음을 최근 장면 실행 settings에 남긴다 — 검수 화면이 장면을 만든 설정을 그 실행에서 읽는다."""
    latest = db.table("customer_ai_runs").select("id,settings").eq("project_id", project_id).eq("kind", "scene") \
        .eq("status", "completed").order("created_at", desc=True).limit(1).execute().data
    if latest:
        db.table("customer_ai_runs").update({"settings": {**(latest[0]["settings"] or {}), **PLACE_SCENE_SETTINGS}}) \
            .eq("id", latest[0]["id"]).execute()


def _replace_scenes(db, project_id: str, scenes: list[list[dict]], names: list[Optional[str]]):
    # ponytail: 지우기·넣기가 여러 REST 호출이라 그 사이(수 초)에 죽으면 장면이 일부만 남는다. 문제되면 한 트랜잭션 RPC로.
    db.table("customer_photos").update({"scene_id": None}).eq("project_id", project_id).execute()
    db.table("customer_scenes").delete().eq("project_id", project_id).execute()
    for index, scene in enumerate(scenes):
        saved = db.table("customer_scenes").insert({
            "project_id": project_id, "scene_index": index, "name": names[index],
            "start_at": scene_taken_at(scene[0]), "end_at": scene_taken_at(scene[-1]), "photo_count": len(scene),
        }).execute().data[0]
        ids = [photo["id"] for photo in scene]
        for start in range(0, len(ids), 200):
            db.table("customer_photos").update({"scene_id": saved["id"]}).in_("id", ids[start:start + 200]).execute()


async def run_scene(run_id: str, project_id: str, scene_names: Optional[list[str]] = None, gap_seconds: Optional[int] = None):
    db = get_supabase()
    async with _heartbeat(db, run_id):
        await _run_scene(db, run_id, project_id, scene_names, gap_seconds or SCENE_SETTINGS["gapSeconds"])


async def _run_scene(db, run_id: str, project_id: str, scene_names: Optional[list[str]], gap_seconds: int):
    """장면 정리: 촬영 시각이 있으면 공백으로, 없으면 내용 기반 큰 촬영 구간과 노출 카드 수로 나누어 저장한다.
    시각 없는 경로는 유사컷과 같은 임베딩을 재사용한다. 진행 수는 작은 구간을 묘사한 수 기준.
    이름 붙이기(Gemini·다운로드)를 다 끝낸 뒤에 기존 장면을 바꾼다 — 도중에 서비스가 재시작돼도 기존 장면은 남는다.
    나눌 근거가 없으면(사진이 적거나 촬영 시각 대부분이 없으면) 장면을 지운다."""
    settings = {**SCENE_SETTINGS, "gapSeconds": gap_seconds, "catalog": scene_names or [], "nameModel": GEMINI_FLASH_MODEL,
                "namePromptVersion": SCENE_NAME_PROMPT_VERSION, "sampleSelection": "quality+similarity-dedupe",
                "repeatedNames": "numbered", "absorbPlaceless": {"maxPhotos": PLACELESS_ABSORB_PHOTOS, "maxNeighborRatio": 0.5}}
    try:
        rows = capture_order(_all_rows(lambda: db.table("customer_photos")
                                       .select("id,order_index,preview_url,thumb_url,taken_at,taken_at_source,similarity_group_id")
                                       .eq("project_id", project_id).order("id")))
        placed = _place_scenes(db, project_id, scene_names, rows)
        if placed:  # 흔들림 확인에서 장소까지 판정했으면 그걸로 나누고 이름도 붙인다(Gemini 이름 호출 없음)
            _progress(db, run_id, 0, 0, settings={**settings, **PLACE_SCENE_SETTINGS}, every=1)
            _ensure_running(db, run_id)
            _replace_scenes(db, project_id, *placed)
            _done(db, run_id, 0, 0, 0, usage=sum_usage([]))
            return
        scenes = split_scenes(rows, gap_seconds)
        by_content = False
        if scenes is None and len(rows) >= MIN_PHOTOS_FOR_SCENES:
            # 촬영 시각이 없으면(보정본 등) 업로드 순서에서 사진 내용이 바뀌는 곳으로 나눈다. 임베딩은 유사컷과 같은 캐시라
            # 유사컷이 끝났으면 다시 계산하지 않는다. 없으면 내려받아 계산하므로 무거운 실행 자리를 기다린다.
            async with _heavy_slot():
                vectors = dict(zip((row["id"] for row in rows), await _embeddings(db, run_id, project_id, rows)))
                scenes = split_by_content(rows, [vectors[row["id"]] for row in rows])
            by_content = scenes is not None
            if by_content:
                settings.update(CONTENT_SCENE_SETTINGS)
                # 동시에 시작한 유사컷 작업의 저장 시점과 무관하게 같은 카드 수로 장면 크기를 정한다(로컬 계산만).
                rows = [{**row, "similarity_group_id": None} for row in rows]
                for members in group_shots([vectors[row["id"]] for row in rows], [_shot_time(row) for row in rows]):
                    for index in members:
                        rows[index]["similarity_group_id"] = rows[members[0]]["id"]
                scenes = split_by_content(rows, [vectors[row["id"]] for row in rows])
        scenes = scenes or []
        names: list[Optional[str]] = [None] * len(scenes)
        # 내용 기준 장면은 이름 목록 없이 장소·의상을 적으므로 촬영 종류와 상관없이 전부 묻는다.
        to_name = [index for index, scene in enumerate(scenes) if by_content or (scene_names and scene_taken_at(scene[0]))]
        descriptions: list[Optional[dict]] = [None] * len(scenes)
        tick = _progress(db, run_id, len(to_name), 0, settings=settings, every=1)
        failed = 0
        usages: list[dict] = []
        named: dict[str, str] = {}  # 이번에 이름을 받은(또는 캐시에서 쓴) 장면 지문 → 이름 — 다음 실행의 캐시
        if to_name:
            cache = _name_cache(db, project_id, settings)
            client = flagged = None
            for index in to_name:
                key = scene_key(scenes[index])
                if key in cache:  # 지난 정리와 사진 구성이 같은 장면은 Gemini를 다시 부르지 않는다
                    names[index] = named[key] = cache[key]
                    if by_content:
                        descriptions[index] = json.loads(cache[key])
                    tick()
                    continue
                _ensure_running(db, run_id)
                if client is None:
                    client, flagged = await get_client(), _flagged_photos(db, project_id)
                sample = pick_samples(scenes[index], flagged, DESCRIBE_SAMPLE_PHOTOS if by_content else SCENE_SAMPLE_PHOTOS)
                images = [image for image in await download_all([photo["preview_url"] for photo in sample]) if image]
                if by_content:  # 묘사는 캐시에 JSON 문자열로 남긴다(이름 캐시와 같은 자리)
                    used = [d for d in descriptions if d]
                    look = await _describe_scene(client, images, list(dict.fromkeys(d["place"] for d in used)),
                                                 list(dict.fromkeys(o for d in used for o in d["outfits"])), usages) if images else None
                    descriptions[index] = look
                    name = json.dumps(look, ensure_ascii=False) if look else None
                else:
                    name = await _name_scene(client, images, scene_names, usages) if images else None
                failed += name is None
                names[index] = name or OTHER_SCENE
                if name is not None:
                    named[key] = name
                tick()
            if by_content:  # 큰 촬영 흐름을 먼저 잡고 유사컷 표지 수가 많을 때만 내용 경계에서 나눈다.
                _ensure_running(db, run_id)
                client = client or await get_client()
                flagged = flagged if flagged is not None else _flagged_photos(db, project_id)
                project = db.table("customer_projects").select("shoot_type").eq("id", project_id).single().execute().data
                plan = await _plan_content_sections(client, scenes, descriptions, project.get("shoot_type"), flagged, usages,
                                                    lambda: _ensure_running(db, run_id))
                scenes, names = size_content_sections(scenes, descriptions, plan["sections"], vectors,
                                                     outfit_based=plan["basis"] == "outfit")
                settings["sectionPlan"] = plan
            else:
                scenes, names = merge_same_named(*absorb_placeless(scenes, names))
            names = number_repeated(names)
        _ensure_running(db, run_id)
        # 이름을 붙이는 사이 흔들림 확인(장소 판정)이 끝났으면 장소 기준 장면이 더 정확하다 — 그걸 저장한다.
        placed = _place_scenes(db, project_id, scene_names, rows)
        _replace_scenes(db, project_id, *(placed or (scenes, names)))
        db.table("customer_ai_runs").update({"settings": {**settings, **(PLACE_SCENE_SETTINGS if placed else {}), "nameCache": named}}) \
            .eq("id", run_id).execute()
        _done(db, run_id, len(to_name), len(to_name) - failed, failed, usage=sum_usage(usages))
    except _Superseded:
        return
    except Exception as exc:
        _done(db, run_id, 0, 0, 0, str(exc)[:500])


async def run_similarity(run_id: str, project_id: str):
    db = get_supabase()
    async with _heartbeat(db, run_id), _heavy_slot():
        await _run_similarity(db, run_id, project_id)


async def _run_similarity(db, run_id: str, project_id: str):
    """유사컷 묶기(전체 사진 임베딩, 새 사진만 Gemini 호출)."""
    rows = []
    try:
        rows = capture_order(_all_rows(lambda: db.table("customer_photos")
                                       .select("id,order_index,thumb_url,taken_at,taken_at_source")
                                       .eq("project_id", project_id).order("id")))
        tick = _progress(db, run_id, len(rows), 0, settings={
            "embeddingModel": GEMINI_EMBEDDING_MODEL, "embeddingDimension": GEMINI_EMBEDDING_DIMENSION,
            "embeddingVersion": GEMINI_EMBEDDING_VERSION, "grouping": "adaptive-anchor-gap",
            "shotPercentile": SHOT_PERCENTILE, "shotMin": SHOT_MIN, "shotMax": SHOT_MAX,
            "shotAnchorMargin": SHOT_ANCHOR_MARGIN, "shotMaxGapSeconds": SHOT_MAX_GAP_SECONDS,
        })
        vectors = await _embeddings(db, run_id, project_id, rows, tick)
        _ensure_running(db, run_id)
        db.table("customer_photos").update({"similarity_group_id": None}).eq("project_id", project_id).execute()
        db.table("customer_photo_groups").delete().eq("project_id", project_id).execute()
        for members in group_shots(vectors, [_shot_time(row) for row in rows]):
            ids = [rows[index]["id"] for index in members]
            group = db.table("customer_photo_groups").insert({
                "project_id": project_id, "representative_photo_id": ids[0], "photo_count": len(ids)
            }).execute().data[0]
            db.table("customer_photos").update({"similarity_group_id": group["id"]}).in_("id", ids).execute()
        processed = sum(vector is not None for vector in vectors)
        _done(db, run_id, len(rows), processed, len(rows) - processed)
    except _Superseded:
        return
    except Exception as exc:
        _done(db, run_id, len(rows), 0, len(rows), str(exc)[:500])


async def run_quality(run_id: str, project_id: str, place_names: Optional[list[str]] = None):
    db = get_supabase()
    async with _heartbeat(db, run_id), _heavy_slot():
        await _run_quality(db, run_id, project_id, place_names)


async def _run_quality(db, run_id: str, project_id: str, place_names: Optional[list[str]] = None):
    """흔들림·눈 감음·인물 구성 판정. 같은 모델·프롬프트 버전으로 이미 판정한 사진은 다시 부르지 않고(사진을 추가하고
    다시 정리할 때 새 사진만), 나머지를 BATCH_PHOTOS장씩 내려받아 판정하고 바로 저장한다.
    place_names(홈스냅)가 있으면 사진을 찍은 장소도 같은 호출에서 묻고, 끝나면 장면을 장소 기준으로 다시 나눈다 —
    장면 정리는 기다리지 않고 시간 기준 장면을 먼저 보여주고, 이 판정이 끝나면 더 정확한 장면으로 바뀐다."""
    place_names = place_list(place_names)
    version = quality_prompt_version(place_names)
    total = processed = 0
    usages: list[dict] = []
    stats: dict = {}  # 실제 보낸 요청·실패한 요청 수(재시도·타임아웃 포함)
    try:
        rows = _all_rows(lambda: db.table("customer_photos").select("id,order_index,preview_url")
                         .eq("project_id", project_id).order("order_index").order("id"))
        done = {row["photo_id"] for row in _all_rows(lambda: db.table("customer_quality_assessments").select("photo_id")
                .eq("project_id", project_id).eq("model", GEMINI_FLASH_MODEL)
                .eq("prompt_version", version).order("photo_id"))}
        pending = [row for row in rows if row["id"] not in done]
        total, processed = len(rows), len(rows) - len(pending)
        tick = _progress(db, run_id, total, processed, settings={
            "model": GEMINI_FLASH_MODEL, "promptVersion": version, "placeNames": place_names or [],
            "serviceTier": customer_service_tier(),
            "timeoutSeconds": GEMINI_FLEX_TIMEOUT_SECONDS if customer_service_tier() == "flex" else GEMINI_QUALITY_TIMEOUT_SECONDS,
            "image": "preview-1200", "batchPhotos": BATCH_PHOTOS})
        for start in range(0, len(pending), BATCH_PHOTOS):
            batch = pending[start:start + BATCH_PHOTOS]
            _ensure_running(db, run_id)
            images = await download_all([row["preview_url"] for row in batch])
            assessments, batch_usages = await assess_images(images, on_each=tick, customer=True, stats=stats, place_names=place_names)
            usages += batch_usages
            payload = [{"project_id": project_id, "photo_id": row["id"], "model": GEMINI_FLASH_MODEL,
                        "prompt_version": version,
                        "eyes_closed": value.eyes_closed.value, "blur_or_shake": value.blur_or_shake.value,
                        "focus_issue": value.focus_issue.value, "face_occluded": value.face_occluded.value,
                        "primary_subject_detected": value.primary_subject_detected, "notes": value.notes,
                        "raw_response": value.model_dump(mode="json")}
                       for row, value in zip(batch, assessments) if value is not None]
            _ensure_running(db, run_id)
            if payload:
                db.table("customer_quality_assessments").upsert(
                    payload, on_conflict="project_id,photo_id,model,prompt_version").execute()
            processed += len(payload)
        # 사진마다 현재 모델·프롬프트 판정 한 행만 남긴다 — 프로젝트 조회가 사진별로 한 행을 고르므로 남으면 섞인다.
        db.table("customer_quality_assessments").delete().eq("project_id", project_id) \
            .neq("prompt_version", version).execute()
        db.table("customer_quality_assessments").delete().eq("project_id", project_id) \
            .neq("model", GEMINI_FLASH_MODEL).execute()
        placed = _place_scenes(db, project_id, place_names)
        if placed:
            _ensure_running(db, run_id)
            _replace_scenes(db, project_id, *placed)
            _mark_place_scenes(db, project_id)
        _done(db, run_id, total, processed, total - processed, usage=sum_usage(usages, stats))
    except _Superseded:
        return
    except Exception as exc:
        _done(db, run_id, total, processed, total - processed, str(exc)[:500], usage=sum_usage(usages, stats))
