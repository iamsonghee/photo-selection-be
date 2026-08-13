"""원본 업로드 종료 검증은 R2를 조회하지 않고 DB 상태만 집계해야 한다."""
import asyncio
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.routers import upload


class _Response:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.with_count = False

    def select(self, *_args, **kwargs):
        self.with_count = kwargs.get("count") == "exact"
        return self

    def eq(self, key, value):
        self.filters.append(lambda row: row.get(key) == value)
        return self

    def in_(self, key, values):
        self.filters.append(lambda row: row.get(key) in values)
        return self

    def limit(self, _limit):
        return self

    def execute(self):
        data = [row for row in self.rows if all(predicate(row) for predicate in self.filters)]
        return _Response(data, len(data) if self.with_count else None)


class _Supabase:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


class OriginalUploadFinalizeTest(unittest.TestCase):
    def _run(self, photo_statuses, job_count):
        project_id = "project-1"
        photographer_id = uuid4()
        db = _Supabase({
            "projects": [{"id": project_id, "photographer_id": str(photographer_id)}],
            "photos": [
                {"id": f"photo-{index}", "project_id": project_id, "original_status": status}
                for index, status in enumerate(photo_statuses)
            ],
            "original_jobs": [
                {"id": f"job-{index}", "project_id": project_id}
                for index in range(job_count)
            ],
        })
        with patch.object(upload, "get_supabase", return_value=db), patch.object(
            upload, "_head_r2_object_sync", side_effect=AssertionError("finalize must not call R2 HEAD")
        ):
            return asyncio.run(upload.finalize_original_upload(project_id, photographer_id))

    def test_pending_and_processing_are_accepted_without_waiting_for_worker(self):
        result = self._run(["pending", "processing", "completed"], 3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["accepted"], 3)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["incomplete"], 0)

    def test_awaiting_upload_and_missing_job_block_completion(self):
        result = self._run(["completed", "awaiting_upload", None], 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["incomplete"], 2)
        self.assertEqual(result["missing_jobs"], 1)


if __name__ == "__main__":
    unittest.main()
