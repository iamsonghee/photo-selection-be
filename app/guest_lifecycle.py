"""Durable guest ZIP parts and deletion. Run only after the guest completion migration."""
import asyncio
import logging
import os
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone

from app.archive import _sanitize_arcname
from app.database import get_supabase
from app.storage import get_r2_client, R2_BUCKET_NAME

logger = logging.getLogger(__name__)
POLL_SECONDS = 5
SWEEP_SECONDS = 300
# Presigned PUTs live for one hour. Also allow an in-flight PUT to finish.
DELETE_GRACE_HOURS = 2


def delete_prefix(prefix):
    """S3 may return HTTP 200 with per-object failures. Never erase DB evidence then."""
    if not R2_BUCKET_NAME:
        raise RuntimeError("R2 bucket is not configured")
    client = get_r2_client()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            result = client.delete_objects(Bucket=R2_BUCKET_NAME, Delete={"Objects": objects})
            if result.get("Errors"):
                raise RuntimeError("R2 deletion incomplete")


def build_part(part):
    db = get_supabase()
    client = get_r2_client()
    archive = db.table("guest_archives").select("album_id").eq("id", part["archive_id"]).single().execute().data
    album_id = archive["album_id"]
    key = f'guest-albums/{album_id}/archives/{part["id"]}/{part["lease_token"]}.zip'
    fd, path = tempfile.mkstemp(prefix="guest_", suffix=".zip")
    os.close(fd)
    done = 0
    last = 0.0

    def progress(force=False):
        nonlocal last
        if not force and time.monotonic() - last < 2:
            return
        album = db.table("guest_albums").select("deleting_at").eq("id", album_id).single().execute().data
        if album["deleting_at"]:
            raise RuntimeError("Album is deleting")
        result = db.table("guest_archive_parts").update({"processed_bytes": done, "lease_until": (datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat()}).eq("id", part["id"]).eq("lease_token", part["lease_token"]).eq("status", "processing").execute()
        if not result.data:
            raise RuntimeError("Archive lease lost")
        last = time.monotonic()

    try:
        progress(True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive_file:
            for entry in part["manifest"]:
                # The UUID avoids duplicate names and zip traversal; the folder preserves original/reduced status.
                name = f'{"원본" if entry["is_original"] else "줄인 사진"}/{entry["id"]}_{_sanitize_arcname(entry["filename"])}'
                response = client.get_object(Bucket=R2_BUCKET_NAME, Key=entry["key"])
                body = response["Body"]
                count = 0
                try:
                    with archive_file.open(name, "w", force_zip64=True) as dest:
                        while chunk := body.read(1024 * 1024):
                            dest.write(chunk)
                            count += len(chunk)
                            done += len(chunk)
                            progress()
                finally:
                    body.close()
                if count != entry["size"]:
                    raise RuntimeError("Source file size mismatch")
        progress(True)
        client.upload_file(path, R2_BUCKET_NAME, key, ExtraArgs={"ContentType": "application/zip"}, Callback=lambda _n: progress())
        progress(True)
        result = db.table("guest_archive_parts").update({"status": "ready", "object_key": key, "processed_bytes": done, "lease_until": None}).eq("id", part["id"]).eq("lease_token", part["lease_token"]).eq("status", "processing").execute()
        if not result.data:
            raise RuntimeError("Archive lease lost")
    except Exception:
        # Unique attempt keys fence off a slow/crashed worker from the next worker.
        delete_prefix(key)
        raise
    finally:
        os.unlink(path)


def sweep():
    db = get_supabase()
    db.rpc("mark_guest_cleanup", {}).execute()
    cutoff = (datetime.now(timezone.utc)-timedelta(hours=DELETE_GRACE_HOURS)).isoformat()
    # Every successful deletion removes rows, so repeated bounded batches never skip rows.
    albums = db.table("guest_albums").select("id").lt("deleting_at", cutoff).limit(100).execute().data or []
    for album in albums:
        try:
            active = db.table("guest_archive_parts").select("id,guest_archives!inner(album_id)").eq("guest_archives.album_id", album["id"]).eq("status", "processing").gt("lease_until", datetime.now(timezone.utc).isoformat()).limit(1).execute().data
            if active:
                continue
            delete_prefix(f'guest-albums/{album["id"]}/')
            db.table("guest_albums").delete().eq("id", album["id"]).execute()
        except Exception:
            logger.exception("Guest album cleanup failed: %s", album["id"])
    rows = db.table("guest_media").select("id,album_id").lt("cleanup_at", cutoff).eq("status", "pending").limit(100).execute().data or []
    for row in rows:
        try:
            delete_prefix(f'guest-albums/{row["album_id"]}/{row["id"]}/')
            db.table("guest_media").delete().eq("id", row["id"]).eq("status", "pending").execute()
        except Exception:
            logger.exception("Guest pending cleanup failed: %s", row["id"])


async def guest_archive_worker():
    while True:
        part = None
        try:
            result = await asyncio.to_thread(lambda: get_supabase().rpc("claim_guest_archive_part", {}).execute())
            if result.data:
                part = result.data[0]
                await asyncio.to_thread(build_part, part)
                continue
        except Exception:
            logger.exception("Guest archive failed")
            if part:
                try:
                    await asyncio.to_thread(lambda: get_supabase().table("guest_archive_parts").update({"status": "failed", "error": "ZIP generation failed", "lease_until": None}).eq("id", part["id"]).eq("lease_token", part["lease_token"]).execute())
                except Exception:
                    logger.exception("Guest archive failure could not be recorded")
        await asyncio.sleep(POLL_SECONDS)


async def guest_cleanup_worker():
    while True:
        try:
            await asyncio.to_thread(sweep)
        except Exception:
            logger.exception("Guest sweep failed")
        await asyncio.sleep(SWEEP_SECONDS)
