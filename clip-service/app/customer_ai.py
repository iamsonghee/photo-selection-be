"""셀프 고객 사진용 Gemini 분석. 모델 호출 코드는 작가 분석과 공유하고 저장소만 분리한다."""
from datetime import datetime, timezone

import logging
from typing import Optional

import numpy as np
from google.genai import types
from pydantic import BaseModel

from app.config import (GEMINI_EMBEDDING_DIMENSION, GEMINI_EMBEDDING_MODEL,
                        GEMINI_EMBEDDING_VERSION, GEMINI_FLASH_MODEL,
                        GEMINI_QUALITY_PROMPT_VERSION, GEMINI_SIMILARITY_THRESHOLD)
from app.db import get_supabase
from app.downloader import download_all
from app.gemini_client import embed_images, get_client
from app.gemini_quality_client import assess_images
from app.grouping import group_by_similarity
from app.scenes import CLOSE_GAP_SECONDS, SCENE_SETTINGS, scene_gap, scene_taken_at, split_scenes

logger = logging.getLogger(__name__)
OTHER_SCENE = "기타 장면"
# 셀프 고객 판정은 인물 구성(people)까지 묻는 별도 프롬프트라 버전을 따로 둔다 — 작가 판정 캐시와 섞이지 않게.
CUSTOMER_QUALITY_PROMPT_VERSION = f"{GEMINI_QUALITY_PROMPT_VERSION}-people"
SCENE_SAMPLE_PHOTOS = 3
# 장면 이름 프롬프트 버전 — 프롬프트·응답 형식을 바꾸면 올린다(실행 settings에 남아 검수 채점에서 구분).
SCENE_NAME_PROMPT_VERSION = "v2-confident"
PAGE_ROWS = 1000  # PostgREST 최대 행 수 — 넘는 조회는 나눠 읽는다(셀프 고객 한도 2,000장)


# 진행 갱신(updated_at)이 이만큼 없으면 멈춘 실행. 진행 수는 5장마다·장면마다 갱신되고, 그 사이 가장 긴 구간은
# 전체 사진 내려받기(동시 다운로드라 2,000장도 몇 분)라 10분이면 정상 실행을 닫지 않는다.
STALE_RUN_SECONDS = 10 * 60


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


def _still_running(db, run_id: str) -> bool:
    """이 실행이 아직 현재 실행인지 — 멈춘 것으로 닫혀 새 실행이 시작됐으면 결과를 쓰지 않는다."""
    rows = db.table("customer_ai_runs").select("status").eq("id", run_id).limit(1).execute().data or []
    return bool(rows) and rows[0]["status"] == "processing"


def _all_rows(query) -> list[dict]:
    """query(): 매번 새 조회(정렬 포함)를 만드는 함수. PAGE_ROWS씩 끝까지 읽는다."""
    rows: list[dict] = []
    while True:
        page = query().range(len(rows), len(rows) + PAGE_ROWS - 1).execute().data or []
        rows += page
        if len(page) < PAGE_ROWS:
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


def _done(db, run_id, total, processed, failed, error=None):
    # 진행 중일 때만 닫는다 — 멈춘 것으로 이미 닫힌 실행이 늦게 끝나 failed를 completed로 되돌리지 않게.
    db.table("customer_ai_runs").update({
        "status": "failed" if error else "completed", "image_count": total,
        "processed_count": processed, "failed_count": failed, "error": error,
        "completed_at": _now(), "updated_at": _now(),
    }).eq("id", run_id).eq("status", "processing").execute()


def capture_order(rows: list[dict]) -> list[dict]:
    """촬영 시각순(없으면 뒤로, 같으면 업로드 순). 유사컷은 인접한 사진끼리만 비교하므로
    업로드 순서가 아니라 실제로 연달아 찍은 순서로 늘어놓아야 연속 촬영을 놓치지 않는다."""
    return sorted(rows, key=lambda row: (row.get("taken_at") is None, row.get("taken_at") or "", row["order_index"]))


async def _embeddings(db, project_id: str, rows: list[dict], tick=None) -> list[Optional[np.ndarray]]:
    """사진별 임베딩(rows 순서). 이미 저장된 사진은 재사용하고 새 사진만 Gemini로 계산해 저장한다."""
    stored = {
        row["photo_id"]: np.asarray(row["embedding"], dtype=np.float64)
        for row in _all_rows(lambda: db.table("customer_ai_embeddings").select("photo_id,embedding")
                             .eq("project_id", project_id).eq("model", GEMINI_EMBEDDING_MODEL)
                             .eq("dimension", GEMINI_EMBEDDING_DIMENSION).eq("version", GEMINI_EMBEDDING_VERSION)
                             .order("photo_id"))
    }
    missing = [row for row in rows if row["id"] not in stored]
    if tick:
        tick(len(rows) - len(missing))
    if missing:
        vectors, _ = await embed_images(await download_all([row["thumb_url"] for row in missing]), on_each=tick)
        payload = [{"project_id": project_id, "photo_id": row["id"],
                    "model": GEMINI_EMBEDDING_MODEL, "dimension": GEMINI_EMBEDDING_DIMENSION,
                    "version": GEMINI_EMBEDDING_VERSION, "embedding": vector.tolist()}
                   for row, vector in zip(missing, vectors) if vector is not None]
        for start in range(0, len(payload), 100):
            db.table("customer_ai_embeddings").upsert(
                payload[start:start + 100],
                on_conflict="project_id,photo_id,model,dimension,version").execute()
        stored.update({row["id"]: vector for row, vector in zip(missing, vectors) if vector is not None})
    return [stored.get(row["id"]) for row in rows]


class _SceneName(BaseModel):
    name: str
    confident: bool


async def _name_scene(client, images: list[bytes], names: list[str]) -> Optional[str]:
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
        result = _SceneName.model_validate_json(response.text)
        name = result.name.strip()
        return name if result.confident and name in names else OTHER_SCENE
    except Exception as exc:
        logger.warning("scene naming failed: %s", exc)
        return None


def merge_same_named(scenes: list[list[dict]], names: list[Optional[str]]) -> tuple[list[list[dict]], list[Optional[str]]]:
    """바로 붙은 장면의 이름이 같고 사이 공백이 짧으면(CLOSE_GAP 미만) 한 장면으로 합친다 — 시간 공백이 한 장면을
    잘못 나눈 경우(예: 하객, 하객). 공백이 길면 이름이 같아도 다른 시점이라 그대로 둔다(이름 오판이 시간 경계를 없애지 않게).
    이름 없는 장면·기타 장면은 합치지 않는다."""
    merged: list[list[dict]] = []
    merged_names: list[Optional[str]] = []
    for scene, name in zip(scenes, names):
        if (name not in (None, OTHER_SCENE) and merged_names and merged_names[-1] == name
                and scene_gap(merged[-1], scene) < CLOSE_GAP_SECONDS):
            merged[-1] = merged[-1] + scene
        else:
            merged.append(scene)
            merged_names.append(name)
    return merged, merged_names


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
    rows = _all_rows(lambda: db.table("customer_quality_assessments").select("photo_id,eyes_closed,blur_or_shake,focus_issue")
                     .eq("project_id", project_id).eq("prompt_version", CUSTOMER_QUALITY_PROMPT_VERSION).order("photo_id"))
    return {row["photo_id"] for row in rows
            if "likely" in (row.get("eyes_closed"), row.get("blur_or_shake"), row.get("focus_issue"))}


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


async def run_scene(run_id: str, project_id: str, scene_names: Optional[list[str]] = None):
    """장면 정리: 촬영 시각 공백으로 나누고(이름 목록이 있으면 장면당 대표 사진 몇 장으로 이름을 붙여) 저장한다.
    전체 사진 임베딩이 필요 없어 유사컷 분석과 따로 돈다. 진행 수는 이름 붙일 장면 수 기준.
    이름 붙이기(Gemini·다운로드)를 다 끝낸 뒤에 기존 장면을 바꾼다 — 도중에 서비스가 재시작돼도 기존 장면은 남는다.
    나눌 근거가 없으면(사진이 적거나 촬영 시각 대부분이 없으면) 장면을 지운다."""
    db = get_supabase()
    settings = {**SCENE_SETTINGS, "catalog": scene_names or [], "nameModel": GEMINI_FLASH_MODEL,
                "namePromptVersion": SCENE_NAME_PROMPT_VERSION, "sampleSelection": "quality+similarity-dedupe"}
    try:
        rows = capture_order(_all_rows(lambda: db.table("customer_photos")
                                       .select("id,order_index,preview_url,taken_at,taken_at_source,similarity_group_id")
                                       .eq("project_id", project_id).order("id")))
        scenes = split_scenes(rows) or []
        names: list[Optional[str]] = [None] * len(scenes)
        to_name = [index for index, scene in enumerate(scenes) if scene_names and scene_taken_at(scene[0])]
        tick = _progress(db, run_id, len(to_name), 0, settings=settings, every=1)
        failed = 0
        if to_name:
            client = await get_client()
            flagged = _flagged_photos(db, project_id)
            for index in to_name:
                sample = pick_samples(scenes[index], flagged)
                images = [image for image in await download_all([photo["preview_url"] for photo in sample]) if image]
                name = await _name_scene(client, images, scene_names) if images else None
                failed += name is None
                names[index] = name or OTHER_SCENE
                tick()
            scenes, names = merge_same_named(scenes, names)
        if not _still_running(db, run_id):
            return
        _replace_scenes(db, project_id, scenes, names)
        _done(db, run_id, len(to_name), len(to_name) - failed, failed)
    except Exception as exc:
        _done(db, run_id, 0, 0, 0, str(exc)[:500])


async def run_similarity(run_id: str, project_id: str):
    """유사컷 묶기(전체 사진 임베딩, 새 사진만 Gemini 호출)."""
    db = get_supabase()
    rows = []
    try:
        rows = capture_order(_all_rows(lambda: db.table("customer_photos")
                                       .select("id,order_index,thumb_url,taken_at")
                                       .eq("project_id", project_id).order("id")))
        tick = _progress(db, run_id, len(rows), 0, settings={
            "embeddingModel": GEMINI_EMBEDDING_MODEL, "embeddingDimension": GEMINI_EMBEDDING_DIMENSION,
            "embeddingVersion": GEMINI_EMBEDDING_VERSION, "similarityThreshold": GEMINI_SIMILARITY_THRESHOLD,
        })
        vectors = await _embeddings(db, project_id, rows, tick)
        if not _still_running(db, run_id):
            return
        db.table("customer_photos").update({"similarity_group_id": None}).eq("project_id", project_id).execute()
        db.table("customer_photo_groups").delete().eq("project_id", project_id).execute()
        for members in group_by_similarity(vectors, GEMINI_SIMILARITY_THRESHOLD):
            ids = [rows[index]["id"] for index in members]
            group = db.table("customer_photo_groups").insert({
                "project_id": project_id, "representative_photo_id": ids[0], "photo_count": len(ids)
            }).execute().data[0]
            db.table("customer_photos").update({"similarity_group_id": group["id"]}).in_("id", ids).execute()
        processed = sum(vector is not None for vector in vectors)
        _done(db, run_id, len(rows), processed, len(rows) - processed)
    except Exception as exc:
        _done(db, run_id, len(rows), 0, len(rows), str(exc)[:500])


async def run_quality(run_id: str, project_id: str):
    db = get_supabase()
    rows = _all_rows(lambda: db.table("customer_photos").select("id,order_index,preview_url")
                     .eq("project_id", project_id).order("order_index").order("id"))
    # 같은 모델·프롬프트 버전으로 이미 판정한 사진은 다시 부르지 않는다(사진을 추가하고 다시 정리할 때 새 사진만).
    done = {row["photo_id"] for row in _all_rows(lambda: db.table("customer_quality_assessments").select("photo_id")
            .eq("project_id", project_id).eq("model", GEMINI_FLASH_MODEL)
            .eq("prompt_version", CUSTOMER_QUALITY_PROMPT_VERSION).order("photo_id"))}
    reused = len([row for row in rows if row["id"] in done])
    rows = [row for row in rows if row["id"] not in done]
    try:
        tick = _progress(db, run_id, reused + len(rows), reused, settings={
            "model": GEMINI_FLASH_MODEL, "promptVersion": CUSTOMER_QUALITY_PROMPT_VERSION})
        images = await download_all([row["preview_url"] for row in rows])
        assessments, _ = await assess_images(images, on_each=tick, customer=True)
        payload = [{"project_id": project_id, "photo_id": row["id"], "model": GEMINI_FLASH_MODEL,
                    "prompt_version": CUSTOMER_QUALITY_PROMPT_VERSION,
                    "eyes_closed": value.eyes_closed.value, "blur_or_shake": value.blur_or_shake.value,
                    "focus_issue": value.focus_issue.value, "face_occluded": value.face_occluded.value,
                    "primary_subject_detected": value.primary_subject_detected, "notes": value.notes,
                    "raw_response": value.model_dump(mode="json")}
                   for row, value in zip(rows, assessments) if value is not None]
        for start in range(0, len(payload), 100):
            db.table("customer_quality_assessments").upsert(
                payload[start:start + 100],
                on_conflict="project_id,photo_id,model,prompt_version").execute()
        # 이전 프롬프트 판정은 지운다 — 프로젝트 조회가 버전 구분 없이 사진별 판정을 읽어서 남으면 섞인다.
        db.table("customer_quality_assessments").delete().eq("project_id", project_id) \
            .neq("prompt_version", CUSTOMER_QUALITY_PROMPT_VERSION).execute()
        _done(db, run_id, reused + len(rows), reused + len(payload), len(rows) - len(payload))
    except Exception as exc:
        _done(db, run_id, reused + len(rows), reused, len(rows), str(exc)[:500])
