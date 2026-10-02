import asyncio

import httpx
from google.genai import errors

from app.gemini_client import is_retryable


def test_only_transient_errors_are_retried():
    assert is_retryable(errors.ClientError(429, {}))
    assert is_retryable(errors.ServerError(503, {}))
    assert is_retryable(asyncio.TimeoutError())
    assert is_retryable(httpx.ConnectError("down"))
    # 잘못된 요청·인증·이미지 오류는 다시 보내도 같다 — 재시도하면 비용만 늘어난다.
    assert not is_retryable(errors.ClientError(400, {}))
    assert not is_retryable(errors.ClientError(403, {}))
    assert not is_retryable(ValueError("bad"))


def _count_calls(monkeypatch, outcome):
    """_assess_one이 Gemini를 몇 번 부르는지(대기 없이)."""
    from types import SimpleNamespace
    from app import gemini_quality_client as quality

    calls = []

    async def generate_content(**kwargs):
        calls.append(1)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome, usage_metadata=None)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(quality.asyncio, "sleep", no_sleep)
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    try:
        asyncio.run(quality._assess_one(client, b"x", "image/jpeg", customer=True))
    except Exception:
        pass
    return len(calls)


def test_bad_json_is_asked_again_once_and_client_errors_not_at_all(monkeypatch):
    assert _count_calls(monkeypatch, "{}") == 2
    assert _count_calls(monkeypatch, errors.ClientError(400, {})) == 1
    assert _count_calls(monkeypatch, errors.ServerError(500, {})) == 3
