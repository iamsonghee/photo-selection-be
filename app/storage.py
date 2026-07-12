"""
GCS + Cloudflare R2 (S3 호환) 스토리지 클라이언트.
"""
import json
import os
import re
import threading
import time
import urllib.parse as _urlparse
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _get_gcs_credentials_json() -> str:
    raw = os.getenv("GCS_CREDENTIALS_JSON") or ""
    return raw.replace("\\n", "\n") if raw else ""


# GCS
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
GCS_CREDENTIALS_JSON = _get_gcs_credentials_json()

# R2 (S3 호환)
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")


def get_gcs_client():
    """Google Cloud Storage 클라이언트 반환. GCS_* 설정이 있을 때만 사용 가능."""
    if not GCS_BUCKET_NAME or not GCS_CREDENTIALS_JSON:
        raise ValueError("GCS_BUCKET_NAME and GCS_CREDENTIALS_JSON must be set in .env")
    from google.cloud import storage

    creds_info = json.loads(GCS_CREDENTIALS_JSON)
    client = storage.Client.from_service_account_info(creds_info)
    return client


def get_gcs_bucket():
    """GCS 버킷 인스턴스 반환."""
    client = get_gcs_client()
    return client.bucket(GCS_BUCKET_NAME)


_r2_client = None
_r2_client_lock = threading.Lock()


def get_r2_client():
    """Cloudflare R2용 boto3 S3 호환 클라이언트 — 프로세스 내 singleton.
    boto3 S3 client는 thread-safe(concurrent put_object / generate_presigned_url 가능)."""
    global _r2_client
    if _r2_client is None:
        with _r2_client_lock:
            if _r2_client is None:
                if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY]):
                    raise ValueError(
                        "R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY must be set in .env"
                    )
                import boto3
                from botocore.config import Config

                endpoint = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
                _r2_client = boto3.client(
                    "s3",
                    endpoint_url=endpoint,
                    aws_access_key_id=R2_ACCESS_KEY_ID,
                    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                    config=Config(signature_version="s3v4"),
                    region_name="auto",
                )
    return _r2_client


def upload_to_r2(
    key: str, body: bytes, content_type: str, cache_control: Optional[str] = None
) -> Optional[str]:
    """
    R2 버킷에 업로드. 성공 시 공개 URL 반환 (R2_PUBLIC_URL 설정 시).

    cache_control: 지정하면 객체 응답에 Cache-Control 헤더로 저장된다. 같은 key가
    나중에 다른 내용으로 덮어써질 수 있는 경로(보정본 재업로드 등)에는 절대 넘기지
    말 것 — 원본 사진 썸네일/프리뷰처럼 매 업로드마다 새 랜덤 key를 쓰는(즉 같은
    key가 재사용되지 않는) 경로에서만 안전하다.
    """
    if not R2_BUCKET_NAME:
        raise ValueError("R2_BUCKET_NAME must be set in .env")
    client = get_r2_client()
    extra: dict = {}
    if cache_control:
        extra["CacheControl"] = cache_control
    client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=body,
        ContentType=content_type,
        **extra,
    )
    if R2_PUBLIC_URL:
        base = R2_PUBLIC_URL.rstrip("/")
        return f"{base}/{key}"
    return None


def delete_r2_objects(keys: list[str]) -> int:
    """R2 버킷에서 지정한 key 목록 삭제. 삭제한 객체 수 반환."""
    if not R2_BUCKET_NAME or not keys:
        return 0
    client = get_r2_client()
    objects = [{"Key": k} for k in keys]
    client.delete_objects(Bucket=R2_BUCKET_NAME, Delete={"Objects": objects})
    return len(objects)


def delete_r2_objects_by_prefix(prefix: str) -> int:
    """
    R2 버킷에서 prefix로 시작하는 모든 객체 삭제. 삭제한 객체 수 반환.
    (프로젝트 삭제 시 photos/{photographer_id}/{project_id}/, versions/{project_id}/ 정리용)
    """
    if not R2_BUCKET_NAME:
        return 0
    client = get_r2_client()
    deleted = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix):
        contents = page.get("Contents") or []
        if not contents:
            continue
        keys = [{"Key": obj["Key"]} for obj in contents]
        client.delete_objects(Bucket=R2_BUCKET_NAME, Delete={"Objects": keys})
        deleted += len(keys)
    return deleted


# ─── R2 Key 추출 / 검증 / Presign ────────────────────────────────────────────

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_HEX32 = r"[0-9a-f]{32}"
_SAFE = r"[\w\-.]+"

_ALLOWED_KEY_PATTERNS = [
    re.compile(rf"^photos/{_UUID}/{_UUID}/{_HEX32}_(thumb|preview)\.jpg$"),
    re.compile(rf"^versions/{_UUID}/v\d+/{_UUID}_{_SAFE}$"),
    re.compile(rf"^versions/{_UUID}/v\d+/{_UUID}_thumb\.jpg$"),
]

PRESIGN_EXPIRES_SECONDS = 3600


def _r2_allowed_hostname() -> str:
    """R2_PUBLIC_URL 환경변수에서 허용 hostname 추출."""
    raw = (R2_PUBLIC_URL or "").rstrip("/")
    if not raw:
        return ""
    return _urlparse.urlparse(raw).netloc


def r2_key_from_url(url: str) -> str:
    """R2 공개 URL에서 object key를 추출합니다.
    허용 도메인 whitelist, URL decoding, 빈 key 방어 포함.
    """
    try:
        parsed = _urlparse.urlparse(url)
    except Exception as exc:
        raise ValueError(f"Invalid URL: {url!r}") from exc

    allowed = _r2_allowed_hostname()
    if allowed and parsed.netloc != allowed:
        raise ValueError(f"R2 domain not allowed: {parsed.netloc!r} (expected {allowed!r})")

    key = _urlparse.unquote(parsed.path).lstrip("/")
    if not key:
        raise ValueError(f"Empty key from URL: {url!r}")
    return key


def validate_r2_key(key: str) -> bool:
    """알려진 R2 key 패턴 중 하나와 일치하는지 검증합니다."""
    return any(p.match(key) for p in _ALLOWED_KEY_PATTERNS)


def generate_presigned_urls_batch(keys: list[str], expires: int = PRESIGN_EXPIRES_SECONDS) -> dict[str, str]:
    """key 목록에 대해 presigned GET URL을 일괄 생성합니다.
    반환: { key: presigned_url }
    """
    if not R2_BUCKET_NAME:
        raise ValueError("R2_BUCKET_NAME must be set in .env")
    client = get_r2_client()
    result: dict[str, str] = {}
    for key in keys:
        result[key] = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET_NAME, "Key": key},
            ExpiresIn=expires,
        )
    return result


# ─── GCS ─────────────────────────────────────────────────────────────────────

def upload_to_gcs(key: str, body: bytes, content_type: str) -> str:
    """GCS 버킷에 업로드. 반환: gcs URI 또는 공개 URL (구성에 따름)."""
    bucket = get_gcs_bucket()
    blob = bucket.blob(key)
    blob.upload_from_string(body, content_type=content_type)
    return f"gs://{GCS_BUCKET_NAME}/{key}"
