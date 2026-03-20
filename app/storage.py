"""
GCS + Cloudflare R2 (S3 호환) 스토리지 클라이언트.
"""
import json
import os
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


def get_r2_client():
    """Cloudflare R2용 boto3 S3 호환 클라이언트 반환."""
    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY]):
        raise ValueError(
            "R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY must be set in .env"
        )
    import boto3
    from botocore.config import Config

    endpoint = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def upload_to_r2(key: str, body: bytes, content_type: str) -> Optional[str]:
    """
    R2 버킷에 업로드. 성공 시 공개 URL 반환 (R2_PUBLIC_URL 설정 시).
    """
    if not R2_BUCKET_NAME:
        raise ValueError("R2_BUCKET_NAME must be set in .env")
    client = get_r2_client()
    client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=body,
        ContentType=content_type,
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


def upload_to_gcs(key: str, body: bytes, content_type: str) -> str:
    """GCS 버킷에 업로드. 반환: gcs URI 또는 공개 URL (구성에 따름)."""
    bucket = get_gcs_bucket()
    blob = bucket.blob(key)
    blob.upload_from_string(body, content_type=content_type)
    return f"gs://{GCS_BUCKET_NAME}/{key}"
