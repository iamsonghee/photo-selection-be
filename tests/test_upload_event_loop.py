import asyncio
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.routers import upload


class UploadEventLoopTests(unittest.TestCase):
    def test_db_wait_does_not_block_concurrent_confirms(self):
        # DB 조회(동기 0.3초)를 기다리는 동안 다른 요청이 멈추면 두 요청이 0.6초 넘게 줄을 선다.
        def slow_owned_job(*_args, **_kwargs):
            time.sleep(0.3)
            return {"id": "job-1", "status": "completed", "r2_source_key": "k", "project_id": "project-1"}

        async def two_confirms():
            started = time.perf_counter()
            results = await asyncio.gather(
                upload.confirm_original_upload("job-1", (uuid4(), time.perf_counter())),
                upload.confirm_original_upload("job-2", (uuid4(), time.perf_counter())),
            )
            return results, time.perf_counter() - started

        with patch.object(upload, "get_supabase", return_value=object()), \
                patch.object(upload, "require_owned_job", side_effect=slow_owned_job):
            results, elapsed = asyncio.run(two_confirms())
        self.assertEqual(results, [{"ok": True}, {"ok": True}])
        self.assertLess(elapsed, 0.5)


if __name__ == "__main__":
    unittest.main()
