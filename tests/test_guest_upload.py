"""하객 업로드 내부 API는 내부 비밀값과 하객 앨범 키만 허용하고, PUT 서명에 크기를 넣어야 한다."""
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import storage
from app.routers import guest_upload

ALBUM = "11111111-1111-4111-8111-111111111111"
MEDIA = "22222222-2222-4222-8222-222222222222"
KEY = f"guest-albums/{ALBUM}/{MEDIA}/original"


class _R2Client:
    def __init__(self):
        self.params = []

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.params.append(Params)
        return f"https://r2.test/{Params['Key']}"


class GuestUploadTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(guest_upload, "INTERNAL_PRESIGN_SECRET", "secret")
        patcher.start()
        self.addCleanup(patcher.stop)

    def body(self, key=KEY, content_type="video/quicktime"):
        return guest_upload.PresignPutBody(items=[{"key": key, "content_type": content_type, "content_length": 1234}])

    def test_rejects_without_secret(self):
        with self.assertRaises(HTTPException) as ctx:
            guest_upload.presign_put(self.body(), authorization="Bearer wrong")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_rejects_keys_outside_guest_albums(self):
        for key in [f"photos/{ALBUM}/{MEDIA}/original", f"guest-albums/{ALBUM}/{MEDIA}/../x", f"guest-albums/{ALBUM}/{MEDIA}/original.mov"]:
            with self.assertRaises(HTTPException) as ctx:
                guest_upload.presign_put(self.body(key), authorization="Bearer secret")
            self.assertEqual(ctx.exception.status_code, 400)

    def test_rejects_non_media_content_type(self):
        with self.assertRaises(HTTPException) as ctx:
            guest_upload.presign_put(self.body(content_type="text/html"), authorization="Bearer secret")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_signs_content_length(self):
        client = _R2Client()
        with patch.object(storage, "R2_BUCKET_NAME", "bucket"), patch.object(storage, "get_r2_client", return_value=client):
            result = guest_upload.presign_put(self.body(), authorization="Bearer secret")
        self.assertEqual(result["urls"][KEY], f"https://r2.test/{KEY}")
        self.assertEqual(client.params[0]["ContentLength"], 1234)
        self.assertEqual(client.params[0]["ContentType"], "video/quicktime")


if __name__ == "__main__":
    unittest.main()
