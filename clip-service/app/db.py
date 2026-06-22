"""Supabase 클라이언트. 기존 백엔드(photo-selection-be/app/database.py)와 동일 패턴, 독립 인스턴스."""
from supabase import Client, ClientOptions, create_client

from app.config import POSTGREST_TIMEOUT, SUPABASE_KEY, SUPABASE_URL


def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SECRET_KEY) must be set in .env"
        )
    options = ClientOptions(postgrest_client_timeout=POSTGREST_TIMEOUT)
    return create_client(SUPABASE_URL, SUPABASE_KEY, options)
