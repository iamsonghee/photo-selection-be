from datetime import datetime, timedelta, timezone

from app.customer_ai import STALE_RUN_SECONDS, is_stale

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _run(status, minutes_ago):
    return {"status": status, "started_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}


def test_only_long_running_processing_runs_are_stale():
    assert is_stale(_run("processing", STALE_RUN_SECONDS / 60 + 1), NOW)
    assert not is_stale(_run("processing", 5), NOW)
    assert not is_stale(_run("completed", 600), NOW)
    assert not is_stale({"status": "processing", "started_at": None}, NOW)
