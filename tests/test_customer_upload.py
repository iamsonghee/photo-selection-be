import asyncio
import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import UploadFile
from starlette.datastructures import Headers

from app.routers import customer_upload


class CustomerUploadTest(unittest.TestCase):
    def test_reports_limit_and_processing_rejections(self):
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
        ):
            result = asyncio.run(customer_upload.upload_customer_photos("project", files, None, None))

        self.assertEqual(result["uploaded"], 0)
        self.assertCountEqual(result["rejected"], ["first.jpg", "over-limit.jpg"])


if __name__ == "__main__":
    unittest.main()
