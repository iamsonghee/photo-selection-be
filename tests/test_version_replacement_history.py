"""보정본 교체는 기존 행 UPSERT가 아니라 이력 보존 RPC를 사용해야 한다."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from app.routers import upload


PROJECT_ID = "18dddd67-0aec-47b9-80cd-4d5e4c2539c2"
PHOTO_ID = "aff806d5-0fe8-4f49-9df1-9568b47feabb"


class _Query:
    def __init__(self, table: str):
        self.table = table
        self.selected = ""

    def select(self, columns: str):
        self.selected = columns
        return self

    def eq(self, *_args):
        return self

    def in_(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        if self.table == "projects":
            return SimpleNamespace(data=[{"id": PROJECT_ID, "status": "editing_v2"}])
        if self.table == "photos":
            return SimpleNamespace(data=[{"id": PHOTO_ID}])
        if self.table == "photo_versions" and self.selected == "version":
            return SimpleNamespace(data=[{"version": 1}, {"version": 2}])
        if self.table == "photo_versions":
            return SimpleNamespace(data=[{"id": "current-v2", "photo_id": PHOTO_ID}])
        if self.table == "version_reviews":
            return SimpleNamespace(data=[])
        raise AssertionError(f"unexpected table query: {self.table} ({self.selected})")


class _Supabase:
    def __init__(self):
        self.rpc_name = None
        self.rpc_params = None

    def table(self, name: str):
        return _Query(name)

    def rpc(self, name: str, params: dict):
        self.rpc_name = name
        self.rpc_params = params
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))


class _UploadFile:
    filename = "replacement.jpg"
    content_type = "image/jpeg"

    async def read(self):
        return b"preview-input"


class VersionReplacementHistoryTest(unittest.TestCase):
    def test_v2_replacement_uses_history_rpc(self):
        supabase = _Supabase()
        delivery_key = f"versions/{PROJECT_ID}/delivery/v2/{PHOTO_ID}_{'a' * 32}.jpg"

        async def fake_process(*_args, **_kwargs):
            return (
                f"https://assets.test/versions/{PROJECT_ID}/v2/new.jpg",
                f"https://assets.test/versions/{PROJECT_ID}/v2/new_thumb.jpg",
                PHOTO_ID,
                1234,
                "replacement.jpg",
            )

        async def run():
            return await upload.upload_versions(
                project_id=PROJECT_ID,
                version=2,
                photo_ids=PHOTO_ID,
                delivery_metadata=json.dumps([{
                    "photo_id": PHOTO_ID,
                    "key": delivery_key,
                    "filename": "replacement.jpg",
                    "content_type": "image/jpeg",
                    "byte_size": 4096,
                }]),
                files=[_UploadFile()],
                photographer_id=UUID("00000000-0000-0000-0000-000000000001"),
            )

        with patch.object(upload, "get_supabase", return_value=supabase), patch.object(
            upload, "_head_r2_object_sync", return_value=4096
        ), patch.object(upload, "_process_one_version", side_effect=fake_process):
            result = asyncio.run(run())

        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(supabase.rpc_name, "replace_photo_versions_with_history")
        self.assertEqual(supabase.rpc_params["p_rows"][0]["photo_id"], PHOTO_ID)
        self.assertEqual(supabase.rpc_params["p_rows"][0]["version"], 2)


if __name__ == "__main__":
    unittest.main()
