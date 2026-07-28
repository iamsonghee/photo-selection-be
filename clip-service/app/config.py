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

# ── Gemini Embedding POC (OpenCLIP 파이프라인과 완전히 독립) ──────────────────
# 서버 전용 키. 프론트엔드에는 절대 노출되지 않는다(clip-service는 내부망/서버간 호출만 받음).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2")
# 128~3072 가변(Matryoshka). 차원별 가격 차이가 없어 기본값은 최대 품질인 3072.
GEMINI_EMBEDDING_DIMENSION = _env_int("GEMINI_EMBEDDING_DIMENSION", 3072, 128, 3072)
# OpenCLIP(0.92)과 점수 분포가 다르므로 그대로 쓰지 않는다 — 실사용 테스트로 0.96에서 적당한
# 그룹핑을 확인(2026-07-28). 초기값일 뿐, UI 슬라이더에서 API 재호출 없이 재조정 가능.
GEMINI_SIMILARITY_THRESHOLD = _env_float("GEMINI_SIMILARITY_THRESHOLD", 0.96, 0.5, 0.999)
GEMINI_CONCURRENCY = _env_int("GEMINI_CONCURRENCY", 4, 1, 16)
GEMINI_MAX_RETRIES = _env_int("GEMINI_MAX_RETRIES", 2, 0, 5)
GEMINI_TIMEOUT_SECONDS = _env_float("GEMINI_TIMEOUT_SECONDS", 30.0, 5.0, 120.0)
# 이미지 1장당 표준 가격(USD). 배치 API(50% 할인)는 POC 범위 밖 — 실제 값은 변경될 수 있으므로
# 이 한 곳에서만 관리하고 코드 곳곳에 하드코딩하지 않는다.
GEMINI_IMAGE_PRICE_USD = _env_float("GEMINI_IMAGE_PRICE_USD", 0.00012, 0.0, 1.0)

# ── Gemini Flash 품질 판정 POC (Embedding과도 완전히 독립, GEMINI_API_KEY만 공유) ──────
# 2026-10-16 종료 예정인 2.5 계열은 피하고 현재 GA인 3.5 계열 중 가장 비용 효율적인 모델 채택.
# 필요 시 코드 변경 없이 gemini-3.5-flash 등으로 교체 가능하도록 env로 분리.
GEMINI_FLASH_MODEL = os.getenv("GEMINI_FLASH_MODEL", "gemini-3.5-flash-lite")
# 프롬프트/판정 기준이 바뀌면 이 값을 올린다 — gemini_quality_assessments의 UNIQUE 키에 포함되어
# 기존 버전 결과를 덮어쓰지 않고 새 버전으로 나란히 쌓이게 한다.
GEMINI_QUALITY_PROMPT_VERSION = os.getenv("GEMINI_QUALITY_PROMPT_VERSION", "v1")
GEMINI_QUALITY_CONCURRENCY = _env_int("GEMINI_QUALITY_CONCURRENCY", 4, 1, 16)
GEMINI_QUALITY_MAX_RETRIES = _env_int("GEMINI_QUALITY_MAX_RETRIES", 2, 0, 5)
GEMINI_QUALITY_TIMEOUT_SECONDS = _env_float("GEMINI_QUALITY_TIMEOUT_SECONDS", 30.0, 5.0, 120.0)
# gemini-3.5-flash-lite 표준가(1M 토큰당 USD). 실제 비용은 usage_metadata 실사용량으로 계산하므로
# 이 값은 참고용 단가일 뿐이며, 한 곳에서만 관리해 코드 곳곳에 하드코딩하지 않는다.
GEMINI_FLASH_INPUT_PRICE_PER_1M = _env_float("GEMINI_FLASH_INPUT_PRICE_PER_1M", 0.30, 0.0, 100.0)
GEMINI_FLASH_OUTPUT_PRICE_PER_1M = _env_float("GEMINI_FLASH_OUTPUT_PRICE_PER_1M", 2.50, 0.0, 100.0)
