from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import get_supabase
from app.routers import projects, storage, upload

app = FastAPI(title="photo-selection-be")

import os

_extra = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
allow_origins = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
] + _extra

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
app.include_router(upload.router, prefix="/api/upload", tags=["upload"])
app.include_router(storage.router, prefix="/api/storage", tags=["storage"])


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/health/db")
def health_check_db():
    """Supabase 연결 확인: photographers 테이블 조회."""
    try:
        client = get_supabase()
        r = client.table("photographers").select("*").limit(1).execute()
        return {"status": "ok", "db": "connected", "photographers_sample": (r.data or [])}
    except Exception as e:
        return {"status": "error", "db": "disconnected", "message": str(e)}
