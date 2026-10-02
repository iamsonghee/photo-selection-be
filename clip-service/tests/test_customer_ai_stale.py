from datetime import datetime, timedelta, timezone

from app.customer_ai import STALE_RUN_SECONDS, is_stale

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
STALE_MINUTES = STALE_RUN_SECONDS / 60 + 1


def _ago(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat()


def test_stale_is_measured_from_last_progress_not_start():
    # 오래 걸리는 큰 분석도 진행 갱신이 계속되면 멈춘 게 아니다.
    assert not is_stale({"status": "processing", "started_at": _ago(120), "updated_at": _ago(1)}, NOW)
    assert is_stale({"status": "processing", "started_at": _ago(120), "updated_at": _ago(STALE_MINUTES)}, NOW)


def test_runs_without_progress_time_fall_back_to_start():
    assert is_stale({"status": "processing", "started_at": _ago(STALE_MINUTES)}, NOW)
    assert not is_stale({"status": "processing", "started_at": _ago(5)}, NOW)
    assert not is_stale({"status": "completed", "started_at": _ago(600)}, NOW)
    assert not is_stale({"status": "processing", "started_at": None}, NOW)
