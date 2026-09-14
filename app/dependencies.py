from uuid import UUID

import datetime
import logging
import os
import time
import jwt
import json
import httpx
from typing import Dict, List, Optional, Tuple
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.algorithms import ECAlgorithm
from postgrest.exceptions import APIError

from app.database import get_supabase

bearer_scheme = HTTPBearer(auto_error=True)
logger = logging.getLogger(__name__)
SUPABASE_URL = os.getenv("SUPABASE_URL")

# OPT-04: JWKS TTL 캐시 (1시간) — 무기한 캐시 → Supabase 키 교체 시 영구 실패 방지
_JWKS_TTL_SECONDS = 3600
_jwks_cache: Optional[Tuple[List[Dict], float]] = None  # (keys, fetched_at)


def get_jwks() -> List[Dict]:
    global _jwks_cache
    now = time.monotonic()
    if _jwks_cache is not None:
        keys, fetched_at = _jwks_cache
        if now - fetched_at < _JWKS_TTL_SECONDS:
            return keys
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL is not configured")
    url = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
    res = httpx.get(url, timeout=10)
    res.raise_for_status()
    data = res.json()
    keys = data.get("keys")
    if not isinstance(keys, list) or not keys:
        raise RuntimeError("JWKS response missing keys")
    _jwks_cache = (keys, now)
    return keys


#: 일시적 전송/게이트웨이 오류만 재시도한다 — 인증 실패·권한 오류 같은 "진짜 실패"는 그대로 올린다.
#  httpx.TransportError는 ConnectError(DNS 실패 Errno 8)·ReadError(Errno 35)·TimeoutException을
#  모두 포함하는 상위 클래스다.
#  postgrest.APIError 중 message가 "JSON could not be generated"인 것은 응답은 왔지만 body가
#  JSON이 아닌 경우(Cloudflare 등 앞단 프록시가 HTML 에러 페이지를 반환) — 진짜 Postgrest 에러는
#  항상 유효한 JSON이므로 이 메시지는 인프라 계층 장애로 보고 재시도 대상에 포함한다.
_PHOTOGRAPHER_LOOKUP_ATTEMPTS = 3
_PHOTOGRAPHER_LOOKUP_BACKOFF_SECONDS = 0.2
_NON_JSON_GATEWAY_ERROR_MESSAGE = "JSON could not be generated"


def _is_retryable_gateway_error(e: Exception) -> bool:
    return isinstance(e, APIError) and e.message == _NON_JSON_GATEWAY_ERROR_MESSAGE


def _select_photographer_with_retry(client, auth_user_id: str):
    """photographers 조회 — 전송/게이트웨이 계층 오류에 한해 짧은 backoff로 재시도.

    Supabase client는 프로세스 전역 싱글턴이고 HTTP/2로 소켓 하나를 공유하므로, 네트워크가
    잠깐 흔들리면 그 순간 진행 중이던 요청들이 함께 읽기 오류를 맞는다. 업로드처럼 요청이
    몰리는 흐름에서는 이 한 번의 흔들림이 곧바로 사진 유실로 이어진다.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, _PHOTOGRAPHER_LOOKUP_ATTEMPTS + 1):
        try:
            return (
                client.table("photographers")
                .select("id")
                .eq("auth_id", auth_user_id)
                .limit(1)
                .execute()
            )
        except httpx.TransportError as e:
            last_error = e
            logger.warning(
                "photographers 조회 전송 오류 — 재시도 %d/%d: %s",
                attempt, _PHOTOGRAPHER_LOOKUP_ATTEMPTS, type(e).__name__,
            )
            if attempt < _PHOTOGRAPHER_LOOKUP_ATTEMPTS:
                time.sleep(_PHOTOGRAPHER_LOOKUP_BACKOFF_SECONDS * attempt)
        except APIError as e:
            if not _is_retryable_gateway_error(e):
                raise
            last_error = e
            logger.warning(
                "photographers 조회 게이트웨이 오류(비-JSON 응답) — 재시도 %d/%d: code=%s",
                attempt, _PHOTOGRAPHER_LOOKUP_ATTEMPTS, e.code,
            )
            if attempt < _PHOTOGRAPHER_LOOKUP_ATTEMPTS:
                time.sleep(_PHOTOGRAPHER_LOOKUP_BACKOFF_SECONDS * attempt)

    # 여기까지 왔으면 재시도를 다 쓴 것이다. 500(서버 결함)이 아니라 503으로 알린다 —
    # 일시적 장애라는 뜻이고, 클라이언트 재시도 정책도 503을 재시도 대상으로 본다.
    #
    # ⚠️ detail 문구에 "인증/Token/JWKS/Unauthorized"를 넣지 말 것. 프론트가 503 중
    # 그 단어들이 들어간 응답은 **재시도하지 않고 즉시 실패 처리**한다(로그인 만료를 재시도로
    # 뭉개지 않으려는 장치, upload/page.tsx의 isAuthLikeDetail). 여기는 연결 문제이지
    # 인증 문제가 아니므로 그 단어를 피해야 재시도가 살아 있다.
    logger.exception("photographers 조회 실패(재시도 소진)", exc_info=last_error)
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="일시적인 연결 오류입니다. 잠시 후 다시 시도해주세요.",
    ) from last_error


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

        payload = jwt.decode(
            token, public_key, algorithms=["ES256"], audience="authenticated",
            leeway=datetime.timedelta(seconds=60),
        )
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
    #
    # 이 조회는 **모든 업로드 요청**이 통과하는 길목이라, 여기서 나는 일시적 네트워크 오류가
    # 그대로 500이 되면 사진 한 장이 통째로 유실된다(실측 2026-09-12: 40장 업로드 중
    # httpx.ReadError로 1장 실패 → 저장된 사진 39장). 끊김은 막을 수 없으니 재시도한다.
    r = _select_photographer_with_retry(client, auth_user_id)
    if not r.data:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Photographer not found",
        )
    return UUID(r.data[0]["id"])
