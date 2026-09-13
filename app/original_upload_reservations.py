"""Early upload ledger maintenance; only expired, unlinked source objects may be deleted."""
import asyncio
import logging
import re

from app.database import get_supabase
from app.storage import get_r2_client, R2_BUCKET_NAME

logger = logging.getLogger(__name__)
RESERVATION_SWEEP_SECONDS = 1800


def cleanup_expired_original_uploads() -> int:
    db = get_supabase()
    rows = db.rpc("claim_expired_original_uploads", {"p_limit": 50}).execute().data or []
    cleaned = 0
    for row in rows:
        try:
            key = row["source_key"]
            expected = f"originals/source/{row['project_id']}/{str(row['client_upload_id']).replace('-', '')}"
            if not re.fullmatch(re.escape(expected) + r"\.(jpg|png|webp)", key):
                logger.error("Invalid original reservation key; leaving reservation for inspection")
                continue
            # Do not treat a database error as an absent reference. The lease blocks late
            # early-upload /photos requests while this check + deletion runs.
            jobs = db.table("original_jobs").select("id").eq("r2_source_key", key).limit(1).execute().data
            photos = db.table("photos").select("id").eq("project_id", row["project_id"]).eq("client_upload_id", row["client_upload_id"]).limit(1).execute().data
            if not jobs and not photos:
                # Single-object API raises on failure; a multi-delete can return per-key Errors.
                get_r2_client().delete_object(Bucket=R2_BUCKET_NAME, Key=key)
            db.table("original_upload_reservations").delete().eq("project_id", row["project_id"]).eq("client_upload_id", row["client_upload_id"]).eq("cleanup_token", row["cleanup_token"]).execute()
            cleaned += 1
        except Exception:
            logger.exception("Original reservation cleanup deferred")
    return cleaned


async def original_reservation_sweep_worker() -> None:
    while True:
        try:
            await asyncio.get_running_loop().run_in_executor(None, cleanup_expired_original_uploads)
        except Exception:
            # Safe during rolling deploy: no early URLs are issued before the RPC exists.
            logger.warning("Original reservation sweep unavailable", exc_info=True)
        await asyncio.sleep(RESERVATION_SWEEP_SECONDS)
