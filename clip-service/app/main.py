"""유사컷(burst shot) CLIP 분석 서비스. 기존 photo-selection-be의 app/ 패키지와 완전히 독립적으로 동작한다.

CLIP 모델은 서버 시작 시 미리 로드하지 않고, 분석 요청이 들어와 compute_embeddings()가
처음 호출될 때 lazy하게 로드된다(clip_model._ensure_loaded). Railway Sleep으로 유휴 시
컨테이너가 내려가는 걸 전제로, 깨어난 직후 아무 요청도 없으면 모델도 메모리에 올라가지
않도록 하기 위함이다."""
import logging
from datetime import datetime, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app import analyzer, gemini_analyzer, matcher, state
from app import gemini_state
from app.auth import verify_internal_token
from app.config import (
    GEMINI_EMBEDDING_DIMENSION,
    GEMINI_EMBEDDING_MODEL,
    GEMINI_SIMILARITY_THRESHOLD,
)
from app.db import get_supabase
from app.memlog import log_rss

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
log_rss("boot")

app = FastAPI(title="photo-selection clip-service")


class AnalyzeRequest(BaseModel):
    project_id: str


class AnalyzeGeminiRequest(BaseModel):
    project_id: str
    limit: int | None = None  # number 순 앞 N장만 분석 (POC 비용 통제용, 예: 50/100)
    force: bool = False  # True면 이미 저장된 임베딩도 재계산


class MatchRetouchResult(BaseModel):
    photo_id: str
    filename: str
    similarity: float
    type: str  # "clip" | "clip_low"


class MatchRetouchResponse(BaseModel):
    matches: list[MatchRetouchResult]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze", status_code=202, dependencies=[Depends(verify_internal_token)])
def analyze(req: AnalyzeRequest, background_tasks: BackgroundTasks):
    project_id = req.project_id
    supabase = get_supabase()

    project_r = (
        supabase.table("projects")
        .select("id, clip_analysis_status")
        .eq("id", project_id)
        .limit(1)
        .execute()
    )
    if not project_r.data:
        raise HTTPException(status_code=404, detail="Project not found")

    current_status = project_r.data[0].get("clip_analysis_status")
    if current_status == "processing" or state.is_in_flight(project_id):
        raise HTTPException(status_code=409, detail="Analysis already in progress")

    if not state.try_start(project_id):
        raise HTTPException(status_code=409, detail="Analysis already in progress")

    (
        supabase.table("projects")
        .update(
            {
                "clip_analysis_status": "processing",
                "clip_analysis_started_at": datetime.now(timezone.utc).isoformat(),
                "clip_analysis_error": None,
            }
        )
        .eq("id", project_id)
        .execute()
    )

    background_tasks.add_task(analyzer.run, project_id)
    return {"status": "processing"}


@app.post("/match-retouch", dependencies=[Depends(verify_internal_token)])
async def match_retouch(
    project_id: str = Form(...),
    photo_ids: str = Form(...),
    files: list[UploadFile] = File(...),
) -> MatchRetouchResponse:
    pid_list = [p.strip() for p in photo_ids.split(",") if p.strip()]
    if not pid_list or not files:
        return MatchRetouchResponse(matches=[])

    retouch_files = [(f.filename or f"file_{i}", await f.read()) for i, f in enumerate(files)]
    supabase = get_supabase()
    matches = await matcher.match_retouch(supabase, project_id, pid_list, retouch_files)
    return MatchRetouchResponse(matches=[MatchRetouchResult(**m) for m in matches])


@app.delete("/analyze/{project_id}", status_code=200, dependencies=[Depends(verify_internal_token)])
def cancel_analyze(project_id: str):
    supabase = get_supabase()
    project_r = (
        supabase.table("projects")
        .select("id, clip_analysis_status")
        .eq("id", project_id)
        .limit(1)
        .execute()
    )
    if not project_r.data:
        raise HTTPException(status_code=404, detail="Project not found")

    if state.is_in_flight(project_id):
        state.request_cancel(project_id)

    (
        supabase.table("projects")
        .update({"clip_analysis_status": None, "clip_analysis_error": None})
        .eq("id", project_id)
        .execute()
    )
    return {"status": "cancelled"}


@app.get("/analyze/{project_id}/status", dependencies=[Depends(verify_internal_token)])
def analyze_status(project_id: str):
    supabase = get_supabase()
    project_r = (
        supabase.table("projects")
        .select(
            "clip_analysis_status, clip_analysis_started_at, clip_analysis_completed_at, clip_analysis_error"
        )
        .eq("id", project_id)
        .limit(1)
        .execute()
    )
    if not project_r.data:
        raise HTTPException(status_code=404, detail="Project not found")
    return project_r.data[0]


# ── Gemini Embedding POC — OpenCLIP(/analyze) 라우트와 완전히 독립된 별도 엔드포인트 ──────


def _latest_gemini_run(supabase, project_id: str) -> dict | None:
    r = (
        supabase.table("gemini_analysis_runs")
        .select("*")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return (r.data or [None])[0]


@app.post("/analyze/gemini", status_code=202, dependencies=[Depends(verify_internal_token)])
def analyze_gemini(req: AnalyzeGeminiRequest, background_tasks: BackgroundTasks):
    project_id = req.project_id
    supabase = get_supabase()

    project_r = supabase.table("projects").select("id").eq("id", project_id).limit(1).execute()
    if not project_r.data:
        raise HTTPException(status_code=404, detail="Project not found")

    latest = _latest_gemini_run(supabase, project_id)
    if (latest and latest.get("status") == "processing") or gemini_state.is_in_flight(project_id):
        raise HTTPException(status_code=409, detail="Gemini analysis already in progress")

    if not gemini_state.try_start(project_id):
        raise HTTPException(status_code=409, detail="Gemini analysis already in progress")

    run_r = (
        supabase.table("gemini_analysis_runs")
        .insert(
            {
                "project_id": project_id,
                "status": "processing",
                "requested_image_limit": req.limit,
                "embedding_model": GEMINI_EMBEDDING_MODEL,
                "embedding_dimension": GEMINI_EMBEDDING_DIMENSION,
                "similarity_threshold": GEMINI_SIMILARITY_THRESHOLD,
            }
        )
        .execute()
    )
    run_id = run_r.data[0]["id"]

    background_tasks.add_task(gemini_analyzer.run, run_id, project_id, req.limit, req.force)
    return {"status": "processing", "run_id": run_id}


@app.delete("/analyze/gemini/{project_id}", status_code=200, dependencies=[Depends(verify_internal_token)])
def cancel_analyze_gemini(project_id: str):
    supabase = get_supabase()
    latest = _latest_gemini_run(supabase, project_id)
    if not latest:
        raise HTTPException(status_code=404, detail="No Gemini analysis run found")

    if gemini_state.is_in_flight(project_id):
        gemini_state.request_cancel(project_id)

    if latest["status"] == "processing":
        (
            supabase.table("gemini_analysis_runs")
            .update(
                {
                    "status": "failed",
                    "error": "cancelled",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", latest["id"])
            .execute()
        )
    return {"status": "cancelled"}


@app.get("/analyze/gemini/{project_id}/status", dependencies=[Depends(verify_internal_token)])
def analyze_gemini_status(project_id: str):
    supabase = get_supabase()
    project_r = supabase.table("projects").select("id").eq("id", project_id).limit(1).execute()
    if not project_r.data:
        raise HTTPException(status_code=404, detail="Project not found")

    latest = _latest_gemini_run(supabase, project_id)
    if not latest:
        return {"gemini_analysis_status": None}
    return {"gemini_analysis_status": latest["status"], "run": latest}


@app.get("/analyze/gemini/{project_id}/groups", dependencies=[Depends(verify_internal_token)])
def analyze_gemini_groups(project_id: str, threshold: float = GEMINI_SIMILARITY_THRESHOLD):
    supabase = get_supabase()
    result = gemini_analyzer.compute_groups(supabase, project_id, threshold)
    if result["analyzed_count"] == 0:
        raise HTTPException(status_code=400, detail="No completed Gemini analysis found for this project")
    return result
