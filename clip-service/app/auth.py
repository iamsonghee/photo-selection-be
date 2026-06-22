"""서버 간 호출 인증. 브라우저에 노출되지 않는 공유 시크릿 헤더 검증."""
from fastapi import Header, HTTPException

from app.config import INTERNAL_TOKEN


def verify_internal_token(x_internal_token: str = Header(default="")) -> None:
    if not INTERNAL_TOKEN:
        raise HTTPException(status_code=503, detail="CLIP_INTERNAL_TOKEN not configured")
    if x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid internal token")
