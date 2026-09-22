import asyncio
import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials
from starlette.datastructures import Headers

from app.routers import customer_upload


class CustomerUploadTest(unittest.TestCase):
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

    def test_rejects_entire_over_limit_request_before_processing(self):
        files = [
            UploadFile(filename=name, file=io.BytesIO(b"jpeg"), headers=Headers({"content-type": "image/jpeg"}))
            for name in ("first.jpg", "over-limit.jpg")
        ]
        with patch.object(customer_upload, "get_supabase", return_value=MagicMock()), patch.object(
            customer_upload,
            "_authorize_customer_project",
            return_value={"photo_count": customer_upload.MAX_PHOTOS_PER_CUSTOMER_PROJECT - 1},
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
            customer_upload, "_authorize_customer_project", return_value={"photo_count": 1999}
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
