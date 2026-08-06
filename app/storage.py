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


def upload_local_file_to_r2(key: str, file_path: str, content_type: str) -> Optional[str]:
    """로컬 파일 경로에서 R2로 업로드(boto3 upload_file — 내부적으로 청크 단위 전송,
    zip 아카이브처럼 큰 파일을 메모리에 통째로 올리지 않고 업로드할 때 사용)."""
    if not R2_BUCKET_NAME:
        raise ValueError("R2_BUCKET_NAME must be set in .env")
    client = get_r2_client()
    client.upload_file(file_path, R2_BUCKET_NAME, key, ExtraArgs={"ContentType": content_type})
    if R2_PUBLIC_URL:
        base = R2_PUBLIC_URL.rstrip("/")
        return f"{base}/{key}"
    return None


def delete_r2_objects(keys: list[str]) -> int:
    """R2 버킷에서 지정한 key 목록 삭제. 삭제한 객체 수 반환."""
    if not R2_BUCKET_NAME or not keys:
        return 0
    client = get_r2_client()
    # S3/R2 DeleteObjects API는 요청 하나당 최대 1,000개 key만 받는다. 프로젝트 전체
    # 삭제(썸네일·미리보기·납품 원본 포함)는 이보다 많을 수 있으므로 안전하게 나눈다.
    for start in range(0, len(keys), 1000):
        objects = [{"Key": k} for k in keys[start : start + 1000]]
        client.delete_objects(Bucket=R2_BUCKET_NAME, Delete={"Objects": objects})
    return len(keys)


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
    re.compile(rf"^originals/{_UUID}/{_HEX32}\.jpg$"),
    re.compile(rf"^originals/source/{_UUID}/{_HEX32}\.(jpg|jpeg|heic|heif|png|webp)$"),
    re.compile(rf"^originals/archives/{_UUID}/part-\d+\.zip$"),
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


def generate_presigned_urls_batch(
    keys: list[str],
    expires: int = PRESIGN_EXPIRES_SECONDS,
    dispositions: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """key 목록에 대해 presigned GET URL을 일괄 생성합니다.
    dispositions: {key: Content-Disposition 헤더 문자열} — 지정된 key만 다운로드 시
    표시/저장 파일명을 강제한다(R2 객체 key 자체는 변경하지 않음).
    반환: { key: presigned_url }
    """
    if not R2_BUCKET_NAME:
        raise ValueError("R2_BUCKET_NAME must be set in .env")
    client = get_r2_client()
    dispositions = dispositions or {}
    result: dict[str, str] = {}
    for key in keys:
        params: dict = {"Bucket": R2_BUCKET_NAME, "Key": key}
        disposition = dispositions.get(key)
        if disposition:
            params["ResponseContentDisposition"] = disposition
        result[key] = client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires,
        )
    return result


# ─── 다운로드 파일명(Content-Disposition) 안전 처리 ──────────────────────────

_FILENAME_UNSAFE_RE = re.compile(r'[/\\\x00-\x1f"]')
_FILENAME_MAX_LEN = 80


def sanitize_filename_component(raw: str) -> str:
    """Content-Disposition에 안전하게 넣을 수 있도록 파일명 구성요소를 정리한다.
    슬래시/백슬래시/제어문자/따옴표 제거, 연속 공백 축소, 길이 제한."""
    cleaned = _FILENAME_UNSAFE_RE.sub("", raw or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = "download"
    return cleaned[:_FILENAME_MAX_LEN]


def build_content_disposition(display_name: str) -> str:
    """RFC 5987 filename*(UTF-8, 한글 등 유지) + ASCII fallback filename 둘 다 포함하는
    Content-Disposition 헤더 값을 만든다."""
    safe = sanitize_filename_component(display_name)
    encoded = _urlparse.quote(safe, safe="")
    # 일부 모바일 브라우저는 filename*보다 ASCII filename을 우선한다. 원본 사진에도
    # download.zip을 넣으면 JPEG가 ZIP으로 저장되므로 fallback의 확장자를 보존한다.
    match = re.search(r"\.[A-Za-z0-9]{1,10}$", safe)
    extension = match.group(0) if match else ""
    ascii_base = re.sub(r"[^\x20-\x7E]", "", safe[:len(safe) - len(extension)]).strip()
    fallback = f"{ascii_base}{extension}" if ascii_base else f"download{extension}"
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def generate_presigned_put_url(key: str, content_type: str, expires: int = PRESIGN_EXPIRES_SECONDS) -> str:
    """브라우저가 R2에 직접 PUT할 수 있는 presigned URL 생성.
    content_type은 브라우저 PUT 요청의 Content-Type 헤더와 정확히 일치해야 한다.
    """
    if not R2_BUCKET_NAME:
        raise ValueError("R2_BUCKET_NAME must be set in .env")
    client = get_r2_client()
    return client.generate_presigned_url(
        "put_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
    )


def get_r2_object_bytes_sync(key: str) -> bytes:
    """R2 object를 다운로드해 bytes로 반환. 미존재 시 KeyError 발생."""
    if not R2_BUCKET_NAME:
        raise ValueError("R2_BUCKET_NAME must be set in .env")
    from botocore.exceptions import ClientError
    client = get_r2_client()
    try:
        response = client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return response["Body"].read()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "403"):
            raise KeyError(f"R2 key not found: {key}")
        raise
    except Exception as exc:
        err_str = str(exc)
        if "NoSuchKey" in err_str or "404" in err_str:
            raise KeyError(f"R2 key not found: {key}")
        raise


def head_r2_object_sync(key: str) -> int:
    """R2 object의 ContentLength 반환. 미존재 또는 0-byte 시 KeyError 발생."""
    if not R2_BUCKET_NAME:
        raise ValueError("R2_BUCKET_NAME must be set in .env")
    from botocore.exceptions import ClientError
    client = get_r2_client()
    try:
        response = client.head_object(Bucket=R2_BUCKET_NAME, Key=key)
        size = response.get("ContentLength", 0)
        if size == 0:
            raise KeyError(f"R2 object is empty: {key}")
        return size
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "403"):
            raise KeyError(f"R2 key not found: {key}")
        raise


# ─── GCS ─────────────────────────────────────────────────────────────────────

def upload_to_gcs(key: str, body: bytes, content_type: str) -> str:
    """GCS 버킷에 업로드. 반환: gcs URI 또는 공개 URL (구성에 따름)."""
    bucket = get_gcs_bucket()
    blob = bucket.blob(key)
    blob.upload_from_string(body, content_type=content_type)
    return f"gs://{GCS_BUCKET_NAME}/{key}"
