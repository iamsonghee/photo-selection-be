"""Early PUT authorization/fallback and cleanup must fail closed on uncertain references."""
import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from app.routers import upload
from app import original_upload_reservations as cleanup


class OriginalReservationTest(unittest.TestCase):
    def setUp(self):
        self.photographer = uuid4()
        self.body = upload.OriginalUploadReservationRequest(
            project_id=uuid4(), client_upload_id=uuid4(), filename="photo.jpg",
            content_type="image/jpeg", file_size=2000, last_modified=123,
        )
        self.db = MagicMock()

    def call(self):
        with patch.object(upload, "get_supabase", return_value=self.db), patch.object(upload, "get_max_photos_per_project", return_value=500):
            return asyncio.run(upload.presign_original_upload(self.body, self.photographer))

    def test_issues_url_only_after_owned_reservation(self):
        self.db.rpc.return_value.execute.return_value.data = {"source_key": "originals/source/key.jpg"}
        with patch.object(upload, "generate_presigned_put_url", return_value="signed") as sign:
            result = self.call()
        self.assertEqual(result["url"], "signed")
        args = self.db.rpc.call_args.args[1]
        self.assertEqual(args["p_photographer_id"], str(self.photographer))
        self.assertEqual(args["p_client_upload_id"], str(self.body.client_upload_id))
        self.assertEqual(args["p_limit"], 500)
        sign.assert_called_once()

    def test_existing_photo_does_not_get_early_overwrite_url(self):
        self.db.rpc.return_value.execute.return_value.data = {"deferred": True}
        with patch.object(upload, "generate_presigned_put_url") as sign:
            self.assertEqual(self.call(), {"deferred": True})
        sign.assert_not_called()

    def test_missing_migration_never_signs_an_untracked_object(self):
        self.db.rpc.return_value.execute.side_effect = RuntimeError("PGRST202 function not found")
        with patch.object(upload, "generate_presigned_put_url") as sign:
            with self.assertRaises(HTTPException) as raised:
                self.call()
        self.assertEqual(raised.exception.status_code, 503)
        sign.assert_not_called()

    def test_denied_reservation_never_signs(self):
        for reason, status in [("not_found", 404), ("not_allowed", 403), ("limit", 403), ("metadata_mismatch", 409), ("cleaning", 409)]:
            self.db.rpc.return_value.execute.side_effect = RuntimeError("original_reservation_" + reason)
            with patch.object(upload, "generate_presigned_put_url") as sign:
                with self.assertRaises(HTTPException) as raised:
                    self.call()
            self.assertEqual(raised.exception.status_code, status)
            sign.assert_not_called()

    def test_heic_does_not_create_reservation(self):
        self.body.content_type = "image/heic"
        with self.assertRaises(HTTPException):
            self.call()
        self.db.rpc.assert_not_called()

    def test_expired_reservation_stops_preview_before_image_processing(self):
        query = MagicMock()
        for method in ("select", "eq", "limit"):
            getattr(query, method).return_value = query
        query.execute.return_value.data = [{"id": str(self.body.project_id), "status": "preparing"}]
        self.db.table.return_value = query
        self.db.rpc.return_value.execute.side_effect = RuntimeError("original_reservation_unavailable")
        file = UploadFile(filename="preview.jpg", file=io.BytesIO(b"test"), headers=Headers({"content-type": "image/jpeg"}))
        with patch.object(upload, "get_supabase", return_value=self.db), patch.object(upload, "_process_one") as process:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(upload.upload_photos(
                    project_id=str(self.body.project_id), files=[file], include_original=True, early_original_upload=True,
                    original_filenames=[self.body.filename], original_file_sizes=[self.body.file_size],
                    original_last_modifieds=[self.body.last_modified], original_content_types=[self.body.content_type],
                    source_widths=[100], source_heights=[100], client_upload_ids=[str(self.body.client_upload_id)],
                    photographer_id=self.photographer,
                ))
        self.assertEqual(raised.exception.status_code, 409)
        process.assert_not_called()
        self.assertEqual(self.db.rpc.call_args.args[0], "renew_original_upload_reservation")

    def test_cleanup_preserves_linked_and_uncertain_objects(self):
        project, client = str(uuid4()), str(uuid4())
        row = {"project_id": project, "client_upload_id": client,
               "source_key": f"originals/source/{project}/{client.replace('-', '')}.jpg", "cleanup_token": str(uuid4())}
        self.db.rpc.return_value.execute.return_value.data = [row]
        query = MagicMock()
        query.select.return_value = query
        query.eq.return_value = query
        query.limit.return_value = query
        query.delete.return_value = query
        self.db.table.return_value = query
        for data in [[{"id": "linked"}], []]:
            query.execute.return_value = SimpleNamespace(data=data)
            with patch.object(cleanup, "get_supabase", return_value=self.db), patch.object(cleanup, "get_r2_client") as r2:
                self.assertEqual(cleanup.cleanup_expired_original_uploads(), 1)
                self.assertEqual(r2.return_value.delete_object.call_count, 0 if data else 1)
        query.execute.side_effect = RuntimeError("DB unavailable")
        with patch.object(cleanup, "get_supabase", return_value=self.db), patch.object(cleanup, "get_r2_client") as r2:
            self.assertEqual(cleanup.cleanup_expired_original_uploads(), 0)
            r2.assert_not_called()

    def test_cleanup_deletion_error_keeps_ledger_for_retry(self):
        project, client = str(uuid4()), str(uuid4())
        self.db.rpc.return_value.execute.return_value.data = [{"project_id": project, "client_upload_id": client,
            "source_key": f"originals/source/{project}/{client.replace('-', '')}.png", "cleanup_token": str(uuid4())}]
        query = MagicMock()
        query.select.return_value = query
        query.eq.return_value = query
        query.limit.return_value = query
        query.execute.return_value.data = []
        self.db.table.return_value = query
        with patch.object(cleanup, "get_supabase", return_value=self.db), patch.object(cleanup, "get_r2_client") as r2:
            r2.return_value.delete_object.side_effect = RuntimeError("R2 unavailable")
            self.assertEqual(cleanup.cleanup_expired_original_uploads(), 0)
        query.delete.assert_not_called()
