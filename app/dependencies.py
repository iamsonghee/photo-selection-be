from uuid import UUID

import logging
import os
import jwt
import json
import httpx
from typing import Dict, List, Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.algorithms import ECAlgorithm

from app.database import get_supabase

bearer_scheme = HTTPBearer(auto_error=True)
logger = logging.getLogger(__name__)
SUPABASE_URL = os.getenv("SUPABASE_URL")
_jwks_cache: Optional[List[Dict]] = None


def get_jwks() -> List[Dict]:
    global _jwks_cache
    if _jwks_cache:
        return _jwks_cache
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL is not configured")
    url = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
    res = httpx.get(url, timeout=10)
    res.raise_for_status()
    data = res.json()
    keys = data.get("keys")
    if not isinstance(keys, list) or not keys:
        raise RuntimeError("JWKS response missing keys")
    _jwks_cache = keys
    return _jwks_cache


def get_current_photographer(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> UUID:
    """Authorization 헤더의 Supabase JWT를 JWKS로 검증하고 photographer_id를 반환."""
    token = credentials.credentials
    if not SUPABASE_URL:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SUPABASE_URL is not configured",
        )

    diag: Dict[str, object] = {
        "kid": None,
        "jwks_keys": 0,
        "supabase_url": SUPABASE_URL,
    }
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        diag["kid"] = kid

        keys = get_jwks()
        diag["jwks_keys"] = len(keys)
        jwk = next((k for k in keys if k.get("kid") == kid), None) if kid else None
        if not jwk:
            jwk = keys[0]

        public_key = ECAlgorithm.from_jwk(json.dumps(jwk))

        payload = jwt.decode(token, public_key, algorithms=["ES256"], audience="authenticated")
        auth_user_id = payload.get("sub")
        if not auth_user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            )
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        ) from e
    except jwt.InvalidTokenError as e:
        logger.exception("JWT InvalidTokenError", extra={"diag": diag})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        ) from e
    except httpx.HTTPError as e:
        logger.exception("JWKS fetch error", extra={"diag": diag})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="인증 서버(JWKS) 연결 실패",
        ) from e
    except RuntimeError as e:
        logger.exception("Auth config/JWKS shape error", extra={"diag": diag})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("JWT 검증 catch-all", extra={"diag": diag})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"인증 처리 중 오류: {type(e).__name__}",
        ) from e

    client = get_supabase()

    # photographers 테이블: auth_id = Supabase Auth user id
    r = (
        client.table("photographers")
        .select("id")
        .eq("auth_id", auth_user_id)
        .limit(1)
        .execute()
    )
    if not r.data:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Photographer not found",
        )
    return UUID(r.data[0]["id"])
