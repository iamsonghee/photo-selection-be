import asyncio

import httpx
from google.genai import errors

from app.gemini_client import is_retryable, retry_delay


def test_only_transient_errors_are_retried():
    assert is_retryable(errors.ClientError(429, {}))
    assert is_retryable(errors.ServerError(503, {}))
    assert is_retryable(asyncio.TimeoutError())
    assert is_retryable(httpx.ConnectError("down"))
    # 잘못된 요청·인증·이미지 오류는 다시 보내도 같다 — 재시도하면 비용만 늘어난다.
    assert not is_retryable(errors.ClientError(400, {}))
    assert not is_retryable(errors.ClientError(403, {}))
    assert not is_retryable(ValueError("bad"))


def test_retry_waits_as_long_as_the_server_asks():
    from types import SimpleNamespace
    busy = errors.ClientError(429, {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}]}})
    assert retry_delay(busy, 0) == 17
    with_header = errors.ClientError(429, {}, SimpleNamespace(headers={"retry-after": "9"}))
    assert retry_delay(with_header, 0) == 9
    assert retry_delay(errors.ServerError(503, {}), 2, base=5.0) == 20  # 알려 준 값이 없으면 base × 2^attempt
    assert retry_delay(errors.ClientError(429, {}, SimpleNamespace(headers={"retry-after": "3600"})), 0) == 60  # 상한


def _count_calls(monkeypatch, outcome, flex=False):
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
    monkeypatch.setattr(quality, "_SUPPORTS_SERVICE_TIER", flex)
    monkeypatch.setattr(quality, "GEMINI_CUSTOMER_QUALITY_SERVICE_TIER", "flex" if flex else "standard")
    monkeypatch.setattr(quality.types, "GenerateContentConfig", lambda **kwargs: kwargs)
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    stats = {}
    try:
        asyncio.run(quality._assess_one(client, b"x", "image/jpeg", customer=True, stats=stats))
    except Exception:
        pass
    assert stats["attempts"] == len(calls)  # 보낸 요청 수를 그대로 센다
    return len(calls)


def test_bad_json_is_asked_again_once_and_client_errors_not_at_all(monkeypatch):
    assert _count_calls(monkeypatch, "{}") == 2
    assert _count_calls(monkeypatch, errors.ClientError(400, {})) == 1
    assert _count_calls(monkeypatch, errors.ServerError(500, {})) == 3


def test_flex_timeouts_are_not_sent_again(monkeypatch):
    # Flex는 이미 오래 기다린 뒤라 타임아웃을 다시 보내지 않는다(표준은 재시도).
    assert _count_calls(monkeypatch, asyncio.TimeoutError(), flex=True) == 1
    assert _count_calls(monkeypatch, asyncio.TimeoutError(), flex=False) == 3
