import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from app import customer_ai


def _quality_run(monkeypatch, photo_count, status_after=None, fail_query=False):
    """_run_quality를 DB·Gemini 없이 돌린다. status_after[n] = n번째 현재 실행 확인부터의 상태."""
    calls = {"download": [], "assess": 0, "ensure": 0}
    done = {}
    photos = [{"id": f"p{i}", "order_index": i, "preview_url": f"u{i}"} for i in range(photo_count)]
    queries = iter([photos, [{"photo_id": "p0"}]])  # 사진 목록, 이미 판정된 사진(p0)

    def all_rows(query):
        if fail_query:
            raise RuntimeError("db down")
        return next(queries)

    def ensure(db, run_id):
        calls["ensure"] += 1
        if status_after and calls["ensure"] > status_after:
            raise customer_ai._Superseded()

    async def download(urls):
        calls["download"].append(len(urls))
        return [b"x" for _ in urls]

    async def assess(images, on_each=None, customer=False):
        calls["assess"] += 1
        value = SimpleNamespace(eyes_closed=SimpleNamespace(value="ok"), blur_or_shake=SimpleNamespace(value="ok"),
                                focus_issue=SimpleNamespace(value="ok"), face_occluded=SimpleNamespace(value="ok"),
                                primary_subject_detected=True, notes=None, model_dump=lambda mode: {})
        return [value for _ in images], [{"prompt_token_count": 10}]

    db = MagicMock()
    monkeypatch.setattr(customer_ai, "_all_rows", all_rows)
    monkeypatch.setattr(customer_ai, "_ensure_running", ensure)
    monkeypatch.setattr(customer_ai, "download_all", download)
    monkeypatch.setattr(customer_ai, "assess_images", assess)
    monkeypatch.setattr(customer_ai, "_progress", lambda *args, **kwargs: (lambda step=1: None))
    monkeypatch.setattr(customer_ai, "_done", lambda db, run_id, total, processed, failed, error=None, usage=None: done.update(
        total=total, processed=processed, failed=failed, error=error))
    asyncio.run(customer_ai._run_quality(db, "run", "project"))
    return calls, done, db


def test_quality_downloads_judges_and_saves_in_batches(monkeypatch):
    calls, done, db = _quality_run(monkeypatch, 91)  # p0은 이미 판정 → 90장을 40·40·10장으로
    assert calls["download"] == [40, 40, 10]
    assert calls["assess"] == 3
    assert done == {"total": 91, "processed": 91, "failed": 0, "error": None}
    upserts = db.table.return_value.upsert.call_args_list
    assert [len(call.args[0]) for call in upserts] == [40, 40, 10]
    # 사진마다 현재 판정 한 행만 — 다른 프롬프트 버전·다른 모델 판정을 지운다.
    neq = db.table.return_value.delete.return_value.eq.return_value.neq.call_args_list
    assert {call.args[0] for call in neq} == {"prompt_version", "model"}


def test_superseded_quality_run_stops_before_spending_more(monkeypatch):
    # 첫 배치 판정 뒤 저장 직전 확인(2번째)부터 대체됨 → 저장도, 다음 배치 호출도 하지 않고 실행 기록도 건드리지 않는다.
    calls, done, db = _quality_run(monkeypatch, 91, status_after=1)
    assert calls["assess"] == 1
    assert not db.table.return_value.upsert.called
    assert done == {}


def test_quality_query_failure_closes_the_run(monkeypatch):
    _, done, _ = _quality_run(monkeypatch, 10, fail_query=True)
    assert done["error"] == "db down"


def test_heavy_runs_wait_for_a_free_slot(monkeypatch):
    monkeypatch.setattr(customer_ai, "CUSTOMER_AI_HEAVY_RUNS", 2)
    monkeypatch.setattr(customer_ai.asyncio, "sleep", _fast_sleep)
    running, peak = [0], [0]

    async def job():
        async with customer_ai._heavy_slot():
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            await _REAL_SLEEP(0.01)
            running[0] -= 1

    async def main():
        await asyncio.gather(*[job() for _ in range(5)])

    asyncio.run(main())
    assert peak[0] == 2
    assert customer_ai._heavy_running == 0


def test_heartbeat_marks_the_run_alive_while_working(monkeypatch):
    monkeypatch.setattr(customer_ai, "HEARTBEAT_SECONDS", 0.01)
    db = MagicMock()

    async def main():
        async with customer_ai._heartbeat(db, "run"):
            await asyncio.sleep(0.05)

    asyncio.run(main())
    updates = [call.args[0] for call in db.table.return_value.update.call_args_list]
    assert updates and all(set(update) == {"updated_at"} for update in updates)


_REAL_SLEEP = asyncio.sleep


async def _fast_sleep(_seconds):
    await _REAL_SLEEP(0.001)
