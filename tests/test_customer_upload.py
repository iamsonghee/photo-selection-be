import asyncio
import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials
from starlette.datastructures import Headers

from app.routers import customer_upload


class CustomerUploadTest(unittest.TestCase):
    def test_taken_at_keeps_valid_values_in_file_order(self):
        parsed = customer_upload._parse_taken_at('["2026-10-03T11:02:45", null, "bad", 5]', 5)
        self.assertEqual(parsed, ["2026-10-03T11:02:45", None, None, None, None])

    def test_upload_timing_warns_only_when_slow(self):
        marks = {"start": 0.0, "authorized": 0.2, "checked": 0.3, "processed": 1.3, "end": 1.5}
        timings = {"resize": [0.1, 0.4], "r2": [0.3]}
        with self.assertLogs(customer_upload.logger, level="INFO") as fast:
            customer_upload._log_upload_timing("p1", 2, 2, 0, 2_097_152, marks, timings)
        self.assertEqual(fast.records[0].levelname, "INFO")
        self.assertIn("resize_max_ms=400 r2_max_ms=300 db_ms=200 total_ms=1500", fast.output[0])
        slow = {**marks, "end": customer_upload.SLOW_UPLOAD_SECONDS + 1}
        with self.assertLogs(customer_upload.logger, level="INFO") as logged:
            customer_upload._log_upload_timing("p1", 2, 2, 0, 0, slow, {"resize": [], "r2": []})
        self.assertEqual(logged.records[0].levelname, "WARNING")

    def test_taken_at_ignores_missing_or_invalid_payload(self):
        self.assertEqual(customer_upload._parse_taken_at(None, 2), [None, None])
        self.assertEqual(customer_upload._parse_taken_at("not json", 1), [None])
        self.assertEqual(customer_upload._parse_taken_at('{"a": 1}', 1), [None])
        self.assertEqual(customer_upload._parse_taken_at('["2026-10-03T11:02:45", "2026-10-03T11:02:46"]', 1), ["2026-10-03T11:02:45"])

    def test_exported_project_rejects_photo_changes(self):
        with self.assertRaises(HTTPException) as raised:
            customer_upload._require_photo_set_mutable({"exported": True})
        self.assertEqual(raised.exception.status_code, 409)

    def test_reopened_project_still_rejects_photo_changes(self):
        with self.assertRaises(HTTPException) as raised:
            customer_upload._require_photo_set_mutable({"exported": False, "delivery_count": 1})
        self.assertEqual(raised.exception.status_code, 409)

    def test_share_token_cannot_manage_photos(self):
        with patch.object(
            customer_upload,
            "_get_customer_project",
            return_value={"id": "project-1", "owner_id": "owner-1", "photo_count": 0},
        ):
            with self.assertRaises(HTTPException) as raised:
                customer_upload._authorize_customer_project(MagicMock(), "project-1", None, "old-share-token")
        self.assertEqual(raised.exception.status_code, 403)

    def test_rejects_entire_request_when_other_projects_use_account_limit(self):
        files = [
            UploadFile(filename=name, file=io.BytesIO(b"jpeg"), headers=Headers({"content-type": "image/jpeg"}))
            for name in ("first.jpg", "over-limit.jpg")
        ]
        with patch.object(customer_upload, "get_supabase", return_value=MagicMock()), patch.object(
            customer_upload,
            "_authorize_customer_project",
            return_value={"owner_id": "owner-1", "photo_count": 0},
        ), patch.object(
            customer_upload,
            "_get_customer_account_photo_count",
            return_value=customer_upload.MAX_PHOTOS_PER_CUSTOMER_ACCOUNT - 1,
        ), patch.object(
            customer_upload,
            "_process_one_customer_photo",
            new=AsyncMock(return_value=None),
        ) as process:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(customer_upload.upload_customer_photos("project", files, None, None))
            process.assert_not_awaited()
        self.assertEqual(raised.exception.detail["error"], "limit_exceeded")
        self.assertEqual(raised.exception.detail["remaining"], 1)

    def test_processing_failure_is_reported_within_limit(self):
        file = UploadFile(filename="failed.jpg", file=io.BytesIO(b"jpeg"), headers=Headers({"content-type": "image/jpeg"}))
        with patch.object(customer_upload, "get_supabase", return_value=MagicMock()), patch.object(
            customer_upload, "_authorize_customer_project", return_value={"owner_id": "owner-1", "photo_count": 1999}
        ), patch.object(
            customer_upload, "_get_customer_account_photo_count", return_value=1999
        ), patch.object(customer_upload, "_process_one_customer_photo", new=AsyncMock(return_value=None)):
            result = asyncio.run(customer_upload.upload_customer_photos("project", [file], None, None))
        self.assertEqual(result["uploaded"], 0)
        self.assertEqual(result["rejected"], ["failed.jpg"])

    def test_project_delete_cascades_and_cleans_r2(self):
        supabase = MagicMock()
        photos = MagicMock()
        photos.select.return_value.eq.return_value.execute.return_value.data = [{"id": "photo-1"}]
        versions = MagicMock()
        versions.select.return_value.in_.return_value.execute.return_value.data = [{"id": "version-1"}]
        projects = MagicMock()
        supabase.table.side_effect = lambda name: {
            "customer_photos": photos,
            "customer_photo_versions": versions,
            "customer_projects": projects,
        }[name]
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token")

        with patch.object(customer_upload, "get_supabase", return_value=supabase), patch.object(
            customer_upload, "_get_customer_project", return_value={"id": "project-1", "owner_id": "owner-1"}
        ), patch.object(customer_upload, "verify_supabase_jwt", return_value="owner-1"), patch.object(
            customer_upload, "delete_r2_objects"
        ) as delete_r2:
            result = asyncio.run(customer_upload.delete_customer_project("project-1", credentials))

        self.assertEqual(result, {"deleted": True})
        projects.delete.return_value.eq.assert_called_once_with("id", "project-1")
        delete_r2.assert_called_once_with([
            "customer-photos/project-1/photo-1_thumb.jpg",
            "customer-photos/project-1/photo-1_preview.jpg",
            "customer-photos/project-1/retouched/version-1_thumb.jpg",
            "customer-photos/project-1/retouched/version-1_preview.jpg",
        ])


if __name__ == "__main__":
    unittest.main()
