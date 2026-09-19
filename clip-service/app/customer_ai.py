"""셀프 고객 사진용 Gemini 분석. 모델 호출 코드는 작가 분석과 공유하고 저장소만 분리한다."""
from datetime import datetime, timezone

from app.config import (GEMINI_EMBEDDING_DIMENSION, GEMINI_EMBEDDING_MODEL,
                        GEMINI_EMBEDDING_VERSION, GEMINI_FLASH_MODEL,
                        GEMINI_QUALITY_PROMPT_VERSION, GEMINI_SIMILARITY_THRESHOLD)
from app.db import get_supabase
from app.downloader import download_all
from app.gemini_client import embed_images
from app.gemini_quality_client import assess_images
from app.grouping import group_by_similarity


def _done(db, run_id, total, processed, failed, error=None):
    db.table("customer_ai_runs").update({
        "status": "failed" if error else "completed", "image_count": total,
        "processed_count": processed, "failed_count": failed, "error": error,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", run_id).execute()


async def run_similarity(run_id: str, project_id: str):
    db = get_supabase()
    rows = (db.table("customer_photos").select("id,order_index,thumb_url")
            .eq("project_id", project_id).order("order_index").execute()).data or []
    try:
        images = await download_all([row["thumb_url"] for row in rows])
        vectors, _ = await embed_images(images)
        payload = [{"project_id": project_id, "photo_id": row["id"],
                    "model": GEMINI_EMBEDDING_MODEL, "dimension": GEMINI_EMBEDDING_DIMENSION,
                    "version": GEMINI_EMBEDDING_VERSION, "embedding": vector.tolist()}
                   for row, vector in zip(rows, vectors) if vector is not None]
        for start in range(0, len(payload), 100):
            db.table("customer_ai_embeddings").upsert(
                payload[start:start + 100],
                on_conflict="project_id,photo_id,model,dimension,version").execute()

        db.table("customer_photos").update({"similarity_group_id": None}).eq("project_id", project_id).execute()
        db.table("customer_photo_groups").delete().eq("project_id", project_id).execute()
        for members in group_by_similarity(vectors, GEMINI_SIMILARITY_THRESHOLD):
            ids = [rows[index]["id"] for index in members]
            group = db.table("customer_photo_groups").insert({
                "project_id": project_id, "representative_photo_id": ids[0], "photo_count": len(ids)
            }).execute().data[0]
            db.table("customer_photos").update({"similarity_group_id": group["id"]}).in_("id", ids).execute()
        _done(db, run_id, len(rows), len(payload), len(rows) - len(payload))
    except Exception as exc:
        _done(db, run_id, len(rows), 0, len(rows), str(exc)[:500])


async def run_quality(run_id: str, project_id: str):
    db = get_supabase()
    rows = (db.table("customer_photos").select("id,order_index,preview_url")
            .eq("project_id", project_id).order("order_index").execute()).data or []
    try:
        images = await download_all([row["preview_url"] for row in rows])
        assessments, _ = await assess_images(images)
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
        _done(db, run_id, len(rows), len(payload), len(rows) - len(payload))
    except Exception as exc:
        _done(db, run_id, len(rows), 0, len(rows), str(exc)[:500])
