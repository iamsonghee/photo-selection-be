import io
import os
import unittest
import zipfile
from unittest.mock import MagicMock, patch

from app import guest_lifecycle as lifecycle
from app.routers import guest_upload
from fastapi import HTTPException


class GuestLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.r2 = MagicMock()
        self.db.table.side_effect = self.table
        self.parts = MagicMock()
        self.parts.update.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{"id": "part"}]
        self.part = {"id": "part", "archive_id": "archive", "lease_token": "lease", "manifest": [
            {"id": "a", "key": "a", "filename": "../same.jpg", "is_original": True, "size": 3},
            {"id": "b", "key": "b", "filename": "same.jpg", "is_original": False, "size": 3},
        ]}
        self.r2.get_object.side_effect = lambda **kwargs: {"Body": io.BytesIO(b"abc")}
        self.r2.get_paginator.return_value.paginate.return_value = []
        self.patches = [patch.object(lifecycle, "get_supabase", return_value=self.db), patch.object(lifecycle, "get_r2_client", return_value=self.r2), patch.object(lifecycle, "R2_BUCKET_NAME", "local")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def table(self, name):
        if name == "guest_archive_parts":
            return self.parts
        query = MagicMock()
        query.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {"album_id": "album"} if name == "guest_archives" else {"deleting_at": None}
        return query

    def test_zip_streams_original_and_reduced_and_cleans_temp(self):
        captured = []
        def upload(path, *_args, **_kwargs):
            with zipfile.ZipFile(path) as archive:
                captured.extend(archive.namelist())
                for name in archive.namelist():
                    self.assertEqual(archive.read(name), b"abc")
        self.r2.upload_file.side_effect = upload
        lifecycle.build_part(self.part)
        self.assertEqual(captured, ["원본/a_.._same.jpg", "줄인 사진/b_same.jpg"])
        self.assertFalse(os.path.exists(self.r2.upload_file.call_args.args[0]))
        self.assertEqual(self.parts.update.call_args.args[0]["status"], "ready")

    def test_corrupt_source_never_publishes_zip(self):
        self.part["manifest"][0]["size"] = 4
        with self.assertRaisesRegex(RuntimeError, "size mismatch"):
            lifecycle.build_part(self.part)
        self.r2.upload_file.assert_not_called()
        self.assertFalse(any(c.args[0].get("status") == "ready" for c in self.parts.update.call_args_list))

    def test_lost_lease_never_uploads(self):
        self.parts.update.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
        with self.assertRaisesRegex(RuntimeError, "lease lost"):
            lifecycle.build_part(self.part)
        self.r2.upload_file.assert_not_called()

    def test_partial_r2_delete_is_failure(self):
        self.r2.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": "guest-albums/a/original"}]}]
        self.r2.delete_objects.return_value = {"Errors": [{"Key": "guest-albums/a/original", "Code": "AccessDenied"}]}
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            lifecycle.delete_prefix("guest-albums/a/")

    def test_archive_url_requires_secret_and_exact_archive_path(self):
        with patch.object(guest_upload, "INTERNAL_PRESIGN_SECRET", "secret"):
            for auth, key in [("Bearer wrong", "anything"), ("Bearer secret", "guest-albums/../../other")]:
                with self.assertRaises(HTTPException):
                    guest_upload.archive_urls(guest_upload.KeysBody(keys=[key]), authorization=auth)


if __name__ == "__main__":
    unittest.main()
