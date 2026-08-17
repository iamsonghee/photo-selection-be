"""사진 업로드 재시도는 동일한 R2 객체 키를 재사용해야 한다."""
import asyncio
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from app.routers import upload


class UploadIdempotencyTest(unittest.TestCase):
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

        with patch.object(upload, "_make_thumb_and_preview_sync", return_value=(b"thumb", b"preview")), patch.object(
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


if __name__ == "__main__":
    unittest.main()
