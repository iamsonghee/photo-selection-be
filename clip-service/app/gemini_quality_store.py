"""gemini_quality_assessments 테이블 영속화. photos 테이블과 완전히 분리된 별도 저장소 —
동일 model+prompt_version 재요청 시 Gemini API를 다시 호출하지 않기 위한 캐시 역할도 겸한다."""
import logging
import math
from typing import Dict, List, Optional, Tuple

from app.config import DB_UPSERT_BATCH_SIZE
from app.gemini_quality_client import PhotoQualityAssessment

logger = logging.getLogger(__name__)


def get_existing_photo_ids(
    supabase, project_id: str, model: str, prompt_version: str, photo_ids: List[str]
) -> set[str]:
    """이미 같은 model+prompt_version으로 판정된 photo_id 집합 — 불필요한 재호출 방지."""
    if not photo_ids:
        return set()
    r = (
        supabase.table("gemini_quality_assessments")
        .select("photo_id")
        .eq("project_id", project_id)
        .eq("model", model)
        .eq("prompt_version", prompt_version)
        .in_("photo_id", photo_ids)
        .execute()
    )
    return {row["photo_id"] for row in (r.data or [])}


def persist_assessments(
    supabase,
    project_id: str,
    model: str,
    prompt_version: str,
    id_assessment_pairs: List[Tuple[str, Optional[PhotoQualityAssessment]]],
) -> Tuple[int, int, int]:
    """photo_id별 판정 결과를 gemini_quality_assessments에 배치 upsert.
    반환: (success_count, fail_count, batch_count)"""
    rows = [
        {
            "project_id": project_id,
            "photo_id": photo_id,
            "model": model,
            "prompt_version": prompt_version,
            "eyes_closed": a.eyes_closed.value,
            "blur_or_shake": a.blur_or_shake.value,
            "focus_issue": a.focus_issue.value,
            "face_occluded": a.face_occluded.value,
            "primary_subject_detected": a.primary_subject_detected,
            "notes": a.notes,
            "raw_response": a.model_dump(mode="json"),
        }
        for photo_id, a in id_assessment_pairs
        if a is not None
    ]
    if not rows:
        return 0, 0, 0

    n = len(rows)
    n_batches = math.ceil(n / DB_UPSERT_BATCH_SIZE)
    success = 0
    fail = 0

    for i in range(n_batches):
        batch = rows[i * DB_UPSERT_BATCH_SIZE : (i + 1) * DB_UPSERT_BATCH_SIZE]
        try:
            supabase.table("gemini_quality_assessments").upsert(
                batch, on_conflict="project_id,photo_id,model,prompt_version"
            ).execute()
            success += len(batch)
        except Exception as e:
            fail += len(batch)
            logger.warning(
                "gemini persist_assessments batch %d/%d failed (%d rows): %s",
                i + 1, n_batches, len(batch), e,
            )

    return success, fail, n_batches


def fetch_quality_by_photo_id(
    supabase, project_id: str, model: str, prompt_version: str, photo_ids: List[str]
) -> Dict[str, dict]:
    """지정한 photo_id들의 최신 품질 판정을 {photo_id: {...}} 형태로 조회 (그룹 응답 결합용)."""
    if not photo_ids:
        return {}
    result: Dict[str, dict] = {}
    for i in range(0, len(photo_ids), DB_UPSERT_BATCH_SIZE):
        chunk = photo_ids[i : i + DB_UPSERT_BATCH_SIZE]
        r = (
            supabase.table("gemini_quality_assessments")
            .select("photo_id, eyes_closed, blur_or_shake, focus_issue, face_occluded, primary_subject_detected")
            .eq("project_id", project_id)
            .eq("model", model)
            .eq("prompt_version", prompt_version)
            .in_("photo_id", chunk)
            .execute()
        )
        for row in r.data or []:
            result[row["photo_id"]] = row
    return result
