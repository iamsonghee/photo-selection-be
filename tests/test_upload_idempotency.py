"""사진 업로드 재시도는 동일한 R2 객체 키를 재사용해야 한다."""
import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from PIL import Image

from app.routers import upload
from fastapi import HTTPException


class UploadIdempotencyTest(unittest.TestCase):
    def test_new_photos_are_blocked_after_selection_but_replays_are_allowed(self):
        with self.assertRaises(HTTPException) as raised:
            upload._ensure_project_accepts_new_photos("selecting", 1)
        self.assertEqual(raised.exception.status_code, 409)
        upload._ensure_project_accepts_new_photos("selecting", 0)

    def test_approved_version_photo_ids_only_returns_approved_rows(self):
        versions_query = MagicMock()
        versions_query.select.return_value = versions_query
        versions_query.eq.return_value = versions_query
        versions_query.in_.return_value = versions_query
        versions_query.execute.return_value = SimpleNamespace(data=[
            {"id": "version-approved", "photo_id": "photo-approved"},
            {"id": "version-revision", "photo_id": "photo-revision"},
        ])
        reviews_query = MagicMock()
        reviews_query.select.return_value = reviews_query
        reviews_query.eq.return_value = reviews_query
        reviews_query.in_.return_value = reviews_query
        reviews_query.execute.return_value = SimpleNamespace(data=[
            {"photo_version_id": "version-approved"},
        ])
        supabase = MagicMock()
        supabase.table.side_effect = lambda name: (
            versions_query if name == "photo_versions" else reviews_query
        )

        locked = upload._approved_version_photo_ids(
            supabase,
            ["photo-approved", "photo-revision"],
            2,
        )

        self.assertEqual(locked, {"photo-approved"})

    def _run_process_one(self, client_upload_id: str):
        uploaded_keys: list[str] = []

        def fake_upload(key, _body, _content_type, _cache_control=None):
            uploaded_keys.append(key)
            return key

        async def run():
            loop = asyncio.get_running_loop()
            return await upload._process_one(
                loop,
                b"compressed-preview",
                0,
                "project-1",
                UUID("00000000-0000-0000-0000-000000000001"),
                True,
                "image/jpeg",
                client_upload_id,
            )

        with patch.object(
            upload,
            "_make_thumb_and_preview_sync",
            return_value=(b"thumb", b"preview", 2400, 1600),
        ), patch.object(
            upload, "_upload_to_r2_sync", side_effect=fake_upload
        ):
            result = asyncio.run(run())
        return result, sorted(uploaded_keys)

    def test_same_client_upload_id_reuses_preview_thumb_and_original_keys(self):
        client_upload_id = str(uuid4())
        first, first_keys = self._run_process_one(client_upload_id)
        second, second_keys = self._run_process_one(client_upload_id)

        self.assertEqual(first_keys, second_keys)
        self.assertEqual(first[3]["source_key"], second[3]["source_key"])

    def test_different_client_upload_ids_use_different_keys(self):
        first, first_keys = self._run_process_one(str(uuid4()))
        second, second_keys = self._run_process_one(str(uuid4()))

        self.assertNotEqual(first_keys, second_keys)
        self.assertNotEqual(first[3]["source_key"], second[3]["source_key"])

    def test_process_one_reuses_decoded_dimensions(self):
        result, _ = self._run_process_one(str(uuid4()))
        self.assertEqual(result[4:], (2400, 1600))

    def test_thumbnail_decode_reports_input_dimensions(self):
        source = Image.new("RGB", (96, 64), color="white")
        buffer = io.BytesIO()
        source.save(buffer, format="JPEG")
        source.close()

        thumb, preview, width, height = upload._make_thumb_and_preview_sync(buffer.getvalue())

        self.assertGreater(len(thumb), 0)
        self.assertGreater(len(preview), 0)
        self.assertEqual((width, height), (96, 64))


if __name__ == "__main__":
    unittest.main()
