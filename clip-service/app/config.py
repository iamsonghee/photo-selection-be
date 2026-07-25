"""환경변수 로드. 기존 백엔드(app/database.py)와 같은 패턴이지만 완전히 독립된 모듈."""
import os
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)
load_dotenv()


def _env_int(name: str, default: int, min_v: int, max_v: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(min_v, min(max_v, v))


def _env_float(name: str, default: float, min_v: float, max_v: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return max(min_v, min(max_v, v))


SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SECRET_KEY")

# 트리거 호출자(FE Next.js API route)와 공유하는 서버 간 시크릿
INTERNAL_TOKEN = os.getenv("CLIP_INTERNAL_TOKEN")

# 분석 파라미터
CLIP_SIMILARITY_THRESHOLD = _env_float("CLIP_SIMILARITY_THRESHOLD", 0.92, 0.5, 0.999)
# openai 사전학습 가중치는 quick_gelu 아키텍처로 학습됨 — 일반 ViT-B-32와 섞으면
# open_clip이 "QuickGELU mismatch" 경고를 내고 임베딩 품질이 떨어진다.
CLIP_MODEL_NAME = os.getenv("CLIP_MODEL_NAME", "ViT-B-32-quickgelu")
CLIP_MODEL_PRETRAINED = os.getenv("CLIP_MODEL_PRETRAINED", "openai")

DOWNLOAD_CONCURRENCY = _env_int("DOWNLOAD_CONCURRENCY", 12, 1, 32)
EMBEDDING_BATCH_SIZE = _env_int("EMBEDDING_BATCH_SIZE", 16, 1, 64)
MAX_CONCURRENT_PROJECTS = _env_int("MAX_CONCURRENT_PROJECTS", 1, 1, 4)
DB_UPSERT_BATCH_SIZE = _env_int("DB_UPSERT_BATCH_SIZE", 100, 1, 500)

# 흔들림/눈감음 경고 배지 임계값. 둘 다 300px 썸네일(r2_thumb_url) 기준으로 계산되므로
# 원본 해상도 튜토리얼의 통상값과 다를 수 있다 — placeholder이며 실사용 데이터로 재보정 필요.
BLUR_VARIANCE_THRESHOLD = _env_float("BLUR_VARIANCE_THRESHOLD", 80.0, 0.0, 10000.0)
EYE_AR_THRESHOLD = _env_float("EYE_AR_THRESHOLD", 0.21, 0.05, 0.5)

POSTGREST_TIMEOUT = 15
