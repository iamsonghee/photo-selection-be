"""Opt-in tests against tests/support/guest_local_server.py, never production credentials."""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

BASE = "http://127.0.0.1:55441"
OWNER = "11111111-1111-4111-8111-111111111111"


@unittest.skipUnless(os.environ.get("GUEST_LOCAL_E2E") == "1", "requires disposable local DB")
class GuestLocalIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.client = httpx.Client(base_url=BASE, timeout=15)
        self.album = str(uuid4())
        self.submission = str(uuid4())
        self.call("POST", "guest_albums", {"id": self.album, "owner_id": OWNER, "name": "local", "wedding_date": "2099-01-01", "upload_token": str(uuid4())})
        self.call("POST", "guest_submissions", {"id": self.submission, "album_id": self.album, "device_hash": "test", "name": "test"})

    def call(self, method, path, body=None):
        response = self.client.request(method, "/rest/v1/"+path, json=body)
        response.raise_for_status()
        return response

    def media(self, size=10):
        uid = str(uuid4())
        return {"id": uid, "album_id": self.album, "submission_id": self.submission, "kind": "photo", "filename": "test.jpg", "content_type": "image/jpeg", "size_bytes": size, "reserved_bytes": size, "original_key": f"guest-albums/{self.album}/{uid}/original"}

    def tearDown(self):
        self.call("PATCH", "app_settings?id=eq.1", {"guest_album_max_files": 5000, "guest_album_max_mb": 20480, "guest_archive_part_mb": 1024})
        self.call("POST", "rpc/guest_owner_action", {"p_album": self.album, "p_owner": OWNER, "p_action": "delete"})
        self.client.post(f"/_test/age-delete/{self.album}").raise_for_status()
        self.client.post("/_test/sweep").raise_for_status()
        self.client.close()

    def test_parallel_upload_reservations_cannot_exceed_limit(self):
        self.call("PATCH", "app_settings?id=eq.1", {"guest_album_max_files": 1})
        async def race():
            async with httpx.AsyncClient(base_url=BASE) as client:
                return await asyncio.gather(*[client.post("/rest/v1/guest_media", json=self.media()) for _ in range(8)])
        responses = asyncio.run(race())
        self.assertEqual(sum(r.status_code == 201 for r in responses), 1)
        self.assertTrue(all(r.status_code == 201 or "guest_file_limit" in r.text for r in responses))

    def test_pending_cleanup_removes_all_keys_after_grace(self):
        media = self.media(3)
        self.call("POST", "guest_media", media)
        for part in ["original", "thumb", "preview"]:
            response = self.client.put(f'/storage/guest-albums/{self.album}/{media["id"]}/{part}?size=3&type=image/jpeg', content=b"abc", headers={"Content-Type": "image/jpeg"})
            response.raise_for_status()
        self.call("PATCH", f'guest_media?id=eq.{media["id"]}', {"last_presigned_at": (datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()})
        self.client.post("/_test/sweep").raise_for_status()
        row = self.call("GET", f'guest_media?id=eq.{media["id"]}').json()[0]
        self.assertIsNotNone(row["cleanup_at"])
        self.assertEqual(self.client.get(f'/storage/{media["original_key"]}').status_code, 200)
        self.call("PATCH", f'guest_media?id=eq.{media["id"]}', {"cleanup_at": (datetime.now(timezone.utc)-timedelta(hours=3)).isoformat()})
        result = self.client.post("/_test/sweep").json()
        self.assertFalse(any(media["id"] in k for k in result["keys"]))
        self.assertEqual(self.call("GET", f'guest_media?id=eq.{media["id"]}').json(), [])

    def test_archive_splits_and_preserves_snapshot(self):
        self.call("PATCH", "app_settings?id=eq.1", {"guest_archive_part_mb": 1})
        media = [self.media(700000), self.media(700000)]
        for row in media:
            self.call("POST", "guest_media", row)
            self.call("PATCH", f'guest_media?id=eq.{row["id"]}', {"status": "ready"})
        self.call("PATCH", f"guest_albums?id=eq.{self.album}", {"closed_at": datetime.now(timezone.utc).isoformat()})
        for row in media:
            self.call("POST", "rpc/guest_owner_action", {"p_album": self.album, "p_owner": OWNER, "p_action": "select", "p_media": row["id"], "p_selected": True})
        job = self.call("POST", "rpc/guest_owner_action", {"p_album": self.album, "p_owner": OWNER, "p_action": "archive"}).json()["archiveId"]
        parts = self.call("GET", f"guest_archive_parts?archive_id=eq.{job}").json()
        self.assertEqual(len(parts), 2)
        self.call("POST", "rpc/guest_owner_action", {"p_album": self.album, "p_owner": OWNER, "p_action": "select", "p_media": media[0]["id"], "p_selected": False})
        self.assertEqual(sum(len(p["manifest"]) for p in self.call("GET", f"guest_archive_parts?archive_id=eq.{job}").json()), 2)


if __name__ == "__main__":
    unittest.main()
