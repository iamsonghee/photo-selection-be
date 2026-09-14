"""소유권 체크 통합(app/ownership.py) 후 각 엔드포인트가 기존과 동일한 404/403/성공
동작을 유지하는지 확인한다. 리팩터링 전 인라인 코드가 반환하던 status_code/detail을
그대로 재현하는지가 핵심 — 새 동작을 검증하는 게 아니라 회귀를 잡는 테스트다."""
import asyncio
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.ownership import require_owned_job, require_owned_project
from app.routers import projects, upload


class _Response:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self.filters.append(lambda row: row.get(key) == value)
        return self

    def limit(self, _n):
        return self

    def execute(self):
        data = [row for row in self.rows if all(p(row) for p in self.filters)]
        return _Response(data)


class _Supabase:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


PHOTOGRAPHER = uuid4()
OTHER_PHOTOGRAPHER = uuid4()
PROJECT_ID = "project-1"


def _db(job=None):
    tables = {
        "projects": [{"id": PROJECT_ID, "photographer_id": str(PHOTOGRAPHER), "status": "editing"}],
    }
    if job is not None:
        tables["original_jobs"] = [job]
    return _Supabase(tables)


class RequireOwnedProjectTest(unittest.TestCase):
    def test_owned_project_returns_row(self):
        row = require_owned_project(_db(), PROJECT_ID, PHOTOGRAPHER, select="id,status")
        self.assertEqual(row["id"], PROJECT_ID)

    def test_missing_or_wrong_owner_raises_404(self):
        with self.assertRaises(HTTPException) as ctx:
            require_owned_project(_db(), PROJECT_ID, OTHER_PHOTOGRAPHER)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "Project not found")

        with self.assertRaises(HTTPException) as ctx:
            require_owned_project(_db(), "no-such-project", PHOTOGRAPHER)
        self.assertEqual(ctx.exception.status_code, 404)


class RequireOwnedJobTest(unittest.TestCase):
    def test_owned_job_returns_row(self):
        db = _db(job={"id": "job-1", "project_id": PROJECT_ID, "status": "awaiting_upload"})
        job = require_owned_job(db, "job-1", PHOTOGRAPHER, select="id,status,project_id")
        self.assertEqual(job["status"], "awaiting_upload")

    def test_missing_job_raises_404(self):
        db = _db(job=None)
        with self.assertRaises(HTTPException) as ctx:
            require_owned_job(db, "no-such-job", PHOTOGRAPHER)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "job not found")

    def test_job_owned_by_other_photographer_raises_403(self):
        db = _db(job={"id": "job-1", "project_id": PROJECT_ID, "status": "awaiting_upload"})
        with self.assertRaises(HTTPException) as ctx:
            require_owned_job(db, "job-1", OTHER_PHOTOGRAPHER)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "forbidden")


class ProjectsRouterOwnershipTest(unittest.TestCase):
    def test_get_project_wrong_owner_404(self):
        with patch.object(projects, "get_supabase", return_value=_db()):
            with self.assertRaises(HTTPException) as ctx:
                projects.get_project(PROJECT_ID, OTHER_PHOTOGRAPHER)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_get_project_owner_returns_row(self):
        with patch.object(projects, "get_supabase", return_value=_db()):
            row = projects.get_project(PROJECT_ID, PHOTOGRAPHER)
        self.assertEqual(row["id"], PROJECT_ID)


class UploadRouterJobEndpointsOwnershipTest(unittest.TestCase):
    """confirm/recover/abandon/report-failure 4곳 모두 require_owned_job을 거치므로
    존재하지 않는 job(404)과 남의 job(403) 두 경로를 대표로 확인한다."""

    def _assert_404_and_403(self, coro_factory, job_row):
        # 존재하지 않는 job
        with patch.object(upload, "get_supabase", return_value=_db(job=None)):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(coro_factory())
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "job not found")

        # 존재하지만 다른 photographer 소유
        with patch.object(upload, "get_supabase", return_value=_db(job=job_row)):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(coro_factory(photographer_id=OTHER_PHOTOGRAPHER))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "forbidden")

    def test_confirm_original_upload(self):
        job_row = {"id": "job-1", "project_id": PROJECT_ID, "status": "completed", "r2_source_key": "k"}
        self._assert_404_and_403(
            lambda photographer_id=PHOTOGRAPHER: upload.confirm_original_upload("job-1", photographer_id),
            job_row,
        )

    def test_recover_original(self):
        job_row = {
            "id": "job-1", "project_id": PROJECT_ID, "status": "completed",
            "r2_source_key": "k", "source_content_type": "image/jpeg",
        }
        self._assert_404_and_403(
            lambda photographer_id=PHOTOGRAPHER: upload.recover_original("job-1", photographer_id),
            job_row,
        )

    def test_abandon_original(self):
        job_row = {"id": "job-1", "project_id": PROJECT_ID, "status": "completed", "photo_id": "photo-1"}
        self._assert_404_and_403(
            lambda photographer_id=PHOTOGRAPHER: upload.abandon_original("job-1", photographer_id),
            job_row,
        )

    def test_report_original_upload_failure(self):
        job_row = {"id": "job-1", "project_id": PROJECT_ID, "status": "completed"}
        self._assert_404_and_403(
            lambda photographer_id=PHOTOGRAPHER: upload.report_original_upload_failure(
                "job-1", "put_or_confirm", photographer_id
            ),
            job_row,
        )


class UploadRouterProjectEndpointsOwnershipTest(unittest.TestCase):
    """originals/finalize, originals/pending, versions/delivery/abandon 은
    require_owned_project(select 기본값 "id")를 그대로 쓴다 — 남의 프로젝트면 404."""

    def test_finalize_wrong_owner_404(self):
        with patch.object(upload, "get_supabase", return_value=_db()):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(upload.finalize_original_upload(PROJECT_ID, OTHER_PHOTOGRAPHER))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_get_pending_originals_wrong_owner_404(self):
        with patch.object(upload, "get_supabase", return_value=_db()):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(upload.get_pending_originals(PROJECT_ID, OTHER_PHOTOGRAPHER))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_abandon_delivery_versions_wrong_owner_404(self):
        payload = upload.DeliveryVersionAbandonRequest(
            project_id=UUID(int=0), version=1, keys=["versions/x/delivery/v1/a.jpg"]
        )
        with patch.object(upload, "get_supabase", return_value=_db()):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(upload.abandon_delivery_versions(payload, OTHER_PHOTOGRAPHER))
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
