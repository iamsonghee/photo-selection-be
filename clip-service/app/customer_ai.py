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
from app.scenes import split_scenes

logger = logging.getLogger(__name__)
OTHER_SCENE = "기타 장면"
SCENE_SAMPLE_PHOTOS = 3


def _progress(db, run_id: str, total: int, start: int):
    """진행 수 기록: 시작 시 전체·재사용 장수를 쓰고, 이후 5장마다(그리고 마지막에) 처리 장수를 갱신하는 콜백을 돌려준다."""
    count = {"done": start}
    db.table("customer_ai_runs").update({"image_count": total, "processed_count": start}).eq("id", run_id).execute()

    def tick():
        count["done"] += 1
        if count["done"] % 5 == 0 or count["done"] >= total:
            try:
                db.table("customer_ai_runs").update({"processed_count": count["done"]}).eq("id", run_id).execute()
            except Exception as exc:  # 진행 표시는 실패해도 분석은 계속한다
                logger.warning("progress update failed: %s", exc)
    return tick


def _done(db, run_id, total, processed, failed, error=None):
    db.table("customer_ai_runs").update({
        "status": "failed" if error else "completed", "image_count": total,
        "processed_count": processed, "failed_count": failed, "error": error,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", run_id).execute()


def capture_order(rows: list[dict]) -> list[dict]:
    """촬영 시각순(없으면 뒤로, 같으면 업로드 순). 유사컷은 인접한 사진끼리만 비교하므로
    업로드 순서가 아니라 실제로 연달아 찍은 순서로 늘어놓아야 연속 촬영을 놓치지 않는다."""
    return sorted(rows, key=lambda row: (row.get("taken_at") is None, row.get("taken_at") or "", row["order_index"]))


async def _embeddings(db, project_id: str, rows: list[dict], run_id: Optional[str] = None) -> list[Optional[np.ndarray]]:
    """사진별 임베딩(rows 순서). 이미 저장된 사진은 재사용하고 새 사진만 Gemini로 계산해 저장한다."""
    stored = {
        row["photo_id"]: np.asarray(row["embedding"], dtype=np.float64)
        for row in (db.table("customer_ai_embeddings").select("photo_id,embedding")
                    .eq("project_id", project_id).eq("model", GEMINI_EMBEDDING_MODEL)
                    .eq("dimension", GEMINI_EMBEDDING_DIMENSION).eq("version", GEMINI_EMBEDDING_VERSION)
                    .execute()).data or []
    }
    missing = [row for row in rows if row["id"] not in stored]
    tick = _progress(db, run_id, len(rows), len(rows) - len(missing)) if run_id else None
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


async def _name_scene(client, images: list[bytes], names: list[str]) -> str:
    """한 장면의 대표 사진들을 보고 촬영 종류별 장면 목록 중 하나를 고른다. 맞는 게 없거나 실패하면 기타 장면."""
    prompt = (
        "다음 사진들은 한 촬영의 같은 장면에서 연달아 찍은 사진입니다. 아래 장면 목록 중 이 장면에 가장 맞는 이름을 "
        f"정확히 하나 고르세요. 맞는 것이 없으면 \"{OTHER_SCENE}\"을 고르세요.\n장면 목록: " + ", ".join(names)
    )
    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_FLASH_MODEL,
            contents=[prompt, *[types.Part.from_bytes(data=image, mime_type="image/jpeg") for image in images]],
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=_SceneName, temperature=0),
        )
        name = _SceneName.model_validate_json(response.text).name.strip()
        return name if name in names else OTHER_SCENE
    except Exception as exc:  # 이름만 못 붙인 것 — 장면은 그대로 쓴다.
        logger.warning("scene naming failed: %s", exc)
        return OTHER_SCENE


async def _save_scenes(db, project_id: str, rows: list[dict], scene_names: Optional[list[str]]):
    """촬영 시각 공백으로 장면을 나누고(이름 목록이 있으면 대표 사진으로 이름을 붙여) 저장한다. 나눌 근거가 없으면 장면을 지운다."""
    db.table("customer_photos").update({"scene_id": None}).eq("project_id", project_id).execute()
    db.table("customer_scenes").delete().eq("project_id", project_id).execute()
    scenes = split_scenes(rows)
    if not scenes:
        return
    names: list[Optional[str]] = [None] * len(scenes)
    if scene_names:
        client = await get_client()
        for index, scene in enumerate(scenes):
            if not scene[0].get("taken_at"):
                continue  # 촬영 시각 없는 사진 모음은 이름 없이 둔다
            step = max(1, len(scene) // SCENE_SAMPLE_PHOTOS)
            sample = scene[step // 2::step][:SCENE_SAMPLE_PHOTOS]
            images = [image for image in await download_all([photo["preview_url"] for photo in sample]) if image]
            if images:
                names[index] = await _name_scene(client, images, scene_names)
    for index, scene in enumerate(scenes):
        saved = db.table("customer_scenes").insert({
            "project_id": project_id, "scene_index": index, "name": names[index],
            "start_at": scene[0].get("taken_at"), "end_at": scene[-1].get("taken_at"), "photo_count": len(scene),
        }).execute().data[0]
        ids = [photo["id"] for photo in scene]
        for start in range(0, len(ids), 200):
            db.table("customer_photos").update({"scene_id": saved["id"]}).in_("id", ids[start:start + 200]).execute()


async def run_similarity(run_id: str, project_id: str, scene_names: Optional[list[str]] = None):
    """유사컷 묶기 + 장면. 같은 임베딩을 쓰므로 한 실행에서 이어서 한다(새 사진만 Gemini 호출)."""
    db = get_supabase()
    rows = capture_order((db.table("customer_photos").select("id,order_index,thumb_url,preview_url,taken_at")
                          .eq("project_id", project_id).execute()).data or [])
    try:
        vectors = await _embeddings(db, project_id, rows, run_id)
        db.table("customer_photos").update({"similarity_group_id": None}).eq("project_id", project_id).execute()
        db.table("customer_photo_groups").delete().eq("project_id", project_id).execute()
        for members in group_by_similarity(vectors, GEMINI_SIMILARITY_THRESHOLD):
            ids = [rows[index]["id"] for index in members]
            group = db.table("customer_photo_groups").insert({
                "project_id": project_id, "representative_photo_id": ids[0], "photo_count": len(ids)
            }).execute().data[0]
            db.table("customer_photos").update({"similarity_group_id": group["id"]}).in_("id", ids).execute()
        await _save_scenes(db, project_id, rows, scene_names)
        processed = sum(vector is not None for vector in vectors)
        _done(db, run_id, len(rows), processed, len(rows) - processed)
    except Exception as exc:
        _done(db, run_id, len(rows), 0, len(rows), str(exc)[:500])


async def run_quality(run_id: str, project_id: str):
    db = get_supabase()
    rows = (db.table("customer_photos").select("id,order_index,preview_url")
            .eq("project_id", project_id).order("order_index").execute()).data or []
    # 같은 모델·프롬프트 버전으로 이미 판정한 사진은 다시 부르지 않는다(사진을 추가하고 다시 정리할 때 새 사진만).
    done = {row["photo_id"] for row in (db.table("customer_quality_assessments").select("photo_id")
            .eq("project_id", project_id).eq("model", GEMINI_FLASH_MODEL)
            .eq("prompt_version", GEMINI_QUALITY_PROMPT_VERSION).execute()).data or []}
    reused = len([row for row in rows if row["id"] in done])
    rows = [row for row in rows if row["id"] not in done]
    try:
        tick = _progress(db, run_id, reused + len(rows), reused)
        images = await download_all([row["preview_url"] for row in rows])
        assessments, _ = await assess_images(images, on_each=tick)
        payload = [{"project_id": project_id, "photo_id": row["id"], "model": GEMINI_FLASH_MODEL,
                    "prompt_version": GEMINI_QUALITY_PROMPT_VERSION,
                    "eyes_closed": value.eyes_closed.value, "blur_or_shake": value.blur_or_shake.value,
                    "focus_issue": value.focus_issue.value, "face_occluded": value.face_occluded.value,
                    "primary_subject_detected": value.primary_subject_detected, "notes": value.notes,
                    "raw_response": value.model_dump(mode="json")}
                   for row, value in zip(rows, assessments) if value is not None]
        for start in range(0, len(payload), 100):
            db.table("customer_quality_assessments").upsert(
                payload[start:start + 100],
                on_conflict="project_id,photo_id,model,prompt_version").execute()
        _done(db, run_id, reused + len(rows), reused + len(payload), len(rows) - len(payload))
    except Exception as exc:
        _done(db, run_id, reused + len(rows), reused, len(rows), str(exc)[:500])
