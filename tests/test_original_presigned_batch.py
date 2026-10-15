import asyncio
import io
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import UploadFile
from starlette.datastructures import Headers

from app.routers import upload

PROJECT = str(uuid4())


def _db(jobs):
    tables = {name: MagicMock() for name in ("photos", "projects", "original_jobs", "admin_audit_logs")}
    photos = tables["photos"]
    photos.select.return_value.eq.return_value.in_.return_value.execute.return_value.data = []  # 재전송 아님
    photos.select.return_value.eq.return_value.execute.return_value.count = 3
    tables["original_jobs"].select.return_value.in_.return_value.execute.return_value.data = jobs
    db = MagicMock()
    db.table.side_effect = lambda name: tables[name]
    return db


class OriginalPresignedBatchTests(unittest.TestCase):
    def test_each_presigned_item_carries_its_client_upload_id(self):
        # 여러 장을 한 요청으로 보내면 FE는 client_upload_id로 사진과 원본 주소를 짝짓는다.
        ids = [str(uuid4()) for _ in range(3)]
        files = [UploadFile(filename=f"p{i}.jpg", file=io.BytesIO(b"jpeg"), headers=Headers({"content-type": "image/jpeg"})) for i in range(3)]
        processed = [("t", "p", 10, {"source_key": f"src/{i}.jpg", "photo_hex": f"h{i}", "content_type": "image/jpeg"}, 100, 100) for i in range(3)]
        inserted = [{"id": f"photo-{i}"} for i in range(3)]
        jobs = [{"id": f"job-{i}", "photo_id": f"photo-{i}", "r2_source_key": f"src/{i}.jpg", "source_content_type": "image/jpeg"} for i in range(3)]

        def presign(key, *_args):
            if key == "src/1.jpg":
                raise RuntimeError("presign failed")  # 가운데 사진만 실패 → 항목이 빠져도 나머지 짝은 그대로여야 한다
            return f"https://r2/{key}"

        with patch.object(upload, "get_supabase", return_value=_db(jobs)), \
                patch.object(upload, "require_owned_project", return_value={"id": PROJECT, "status": "preparing"}), \
                patch.object(upload, "_count_photos", return_value=0), \
                patch.object(upload, "get_max_photos_per_project", return_value=None), \
                patch.object(upload, "_process_one", new=AsyncMock(side_effect=processed)), \
                patch.object(upload, "_insert_photos_with_numbers", return_value=inserted), \
                patch.object(upload, "generate_presigned_put_url", side_effect=presign):
            result = asyncio.run(upload.upload_photos(
                PROJECT, files, True, False,
                [f"p{i}.jpg" for i in range(3)], [100] * 3, [1] * 3, ["image/jpeg"] * 3,
                [100] * 3, [100] * 3, ids, [], [], (uuid4(), time.perf_counter()),
            ))

        items = {item["client_upload_id"]: item for item in result["original_presigned"]}
        self.assertEqual(set(items), {ids[0], ids[2]})
        self.assertEqual(items[ids[0]]["job_id"], "job-0")
        self.assertEqual(items[ids[2]]["job_id"], "job-2")
        self.assertEqual(items[ids[2]]["source_key"], "src/2.jpg")


if __name__ == "__main__":
    unittest.main()
