import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, ClientOptions, create_client

# 백엔드 패키지 기준 .env 로드 (실행 경로와 무관하게 photo-selection-be/.env 사용)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)
load_dotenv()  # 현재 작업 디렉터리 .env도 병합

SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_SECRET_KEY")
    or os.getenv("SUPABASE_PUBLISHABLE_KEY")
)

# 요청이 무한 대기하지 않도록 타임아웃 설정 (초)
POSTGREST_TIMEOUT = 15


def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SECRET_KEY/PUBLISHABLE_KEY) must be set in .env"
        )
    options = ClientOptions(postgrest_client_timeout=POSTGREST_TIMEOUT)
    return create_client(SUPABASE_URL, SUPABASE_KEY, options)


supabase: Optional[Client] = None


def init_supabase() -> Client:
    global supabase
    supabase = get_supabase()
    return supabase
