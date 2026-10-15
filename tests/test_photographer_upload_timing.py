import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.routers import upload


class PhotographerUploadTimingTests(unittest.TestCase):
    def test_photo_timing_log_contains_every_field(self):
        timing = {
            "start": time.perf_counter() - 0.1,
            "ok": 2,
            "rejected": 1,
            "bytes": 2_097_152,
            "auth_ms": 0.001,
            "db_pre_ms": 0.002,
            "process_ms": 0.003,
            "resize": [0.004, 0.005],
            "r2": [0.006],
            "db_insert_ms": 0.007,
            "db_post_ms": 0.008,
            "presign_ms": 0.009,
        }

        with self.assertLogs(upload.logger, level="INFO") as logged:
            upload._log_photographer_upload_timing("project-1", 3, True, timing)

        line = logged.output[0]
        for field in (
            "project=project-1", "files=3", "ok=2", "rejected=1", "mb=2.0",
            "include_original=True", "auth_ms=", "db_pre_ms=", "process_ms=",
            "resize_max_ms=", "r2_max_ms=", "db_insert_ms=", "db_post_ms=",
            "presign_ms=", "total_ms=",
        ):
            self.assertIn(field, line)

    def test_confirm_timing_log_contains_every_field(self):
        timing = {
            "start": time.perf_counter() - 0.1,
            "r2_head_ms": 0.01,
            "rpc_ms": 0.02,
        }
        with self.assertLogs(upload.logger, level="INFO") as logged:
            upload._log_original_confirm_timing("project-2", timing)
        line = logged.output[0]
        for field in ("project=project-2", "r2_head_ms=", "rpc_ms=", "total_ms="):
            self.assertIn(field, line)

    def test_photo_timing_warns_after_fifteen_seconds(self):
        timing = {
            "start": 0.0, "ok": 0, "rejected": 0, "bytes": 0,
            "auth_ms": 0.0, "db_pre_ms": 0.0, "process_ms": 0.0,
            "resize": [], "r2": [], "db_insert_ms": 0.0, "db_post_ms": 0.0,
            "presign_ms": 0.0,
        }
        with patch.object(upload.time, "perf_counter", return_value=16.0), self.assertLogs(
            upload.logger, level="WARNING"
        ) as logged:
            upload._log_photographer_upload_timing("project-1", 0, False, timing)
        self.assertIn("WARNING", logged.output[0])

    def test_logger_failure_is_swallowed(self):
        photo_timing = {
            "start": time.perf_counter(), "ok": 0, "rejected": 0, "bytes": 0,
            "auth_ms": 0.0, "db_pre_ms": 0.0, "process_ms": 0.0,
            "resize": [], "r2": [], "db_insert_ms": 0.0, "db_post_ms": 0.0,
            "presign_ms": 0.0,
        }
        with patch.object(upload.logger, "info", side_effect=RuntimeError("log failed")):
            upload._log_photographer_upload_timing("project-1", 0, False, photo_timing)

    def test_logger_failure_does_not_change_upload_response(self):
        expected = {"uploaded": 1, "rejected": []}
        with patch.object(
            upload, "_upload_photos_impl", new=AsyncMock(return_value=expected)
        ), patch.object(upload.logger, "info", side_effect=RuntimeError("log failed")):
            result = asyncio.run(upload.upload_photos(
                "project-1", [object()], False, False,
                [], [], [], [], [], [], [], [], [], (uuid4(), time.perf_counter()),
            ))
        self.assertEqual(result, expected)


    def test_timing_includes_photographer_auth_dependency(self):
        # 동기 의존성(작가 확인)은 스레드풀에서 돈다 — 그 시간이 total_ms에 들어가야 한다.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.dependencies import get_current_photographer

        def slow_photographer():
            time.sleep(0.3)
            return uuid4()

        app = FastAPI()
        app.include_router(upload.router)
        app.dependency_overrides[get_current_photographer] = slow_photographer
        path = next(route.path for route in upload.router.routes if route.path.endswith("/photos"))
        with patch.object(upload, "_upload_photos_impl", new=AsyncMock(return_value={"uploaded": 0, "rejected": []})), \
                self.assertLogs(upload.logger, level="INFO") as logged:
            response = TestClient(app).post(path, data={"project_id": "project-1"}, files={"files": ("a.jpg", b"x", "image/jpeg")})
        self.assertEqual(response.status_code, 200)
        total_ms = int(next(part for part in logged.output[-1].split() if part.startswith("total_ms=")).split("=")[1])
        self.assertGreaterEqual(total_ms, 300)


if __name__ == "__main__":
    unittest.main()
