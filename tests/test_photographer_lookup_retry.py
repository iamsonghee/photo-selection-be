"""Cloudflare 등 프록시가 HTML 에러 페이지를 반환해 postgrest가 'JSON could not be
generated' APIError를 던지는 경우도 재시도 대상이어야 한다 — 응답을 아예 못 받는
httpx.TransportError만 재시도하던 기존 범위로는 이 케이스를 놓쳐 로그인 요청이 그대로
실패했다."""
import unittest
from unittest.mock import MagicMock

from postgrest.exceptions import APIError

from app.dependencies import _select_photographer_with_retry


def _gateway_html_error(status_code: int = 400) -> APIError:
    return APIError({
        "message": "JSON could not be generated",
        "code": status_code,
        "hint": "Refer to full message for details",
        "details": "b'<html>...cloudflare...</html>'",
    })


def _real_postgrest_error() -> APIError:
    return APIError({
        "message": "relation \"photographers\" does not exist",
        "code": "42P01",
        "hint": None,
        "details": None,
    })


def _client_failing_then_succeeding(*exceptions):
    query = MagicMock()
    query.select.return_value = query
    query.eq.return_value = query
    query.limit.return_value = query
    query.execute.side_effect = [*exceptions, "ok"]
    client = MagicMock()
    client.table.return_value = query
    return client


class PhotographerLookupRetryTest(unittest.TestCase):
    def test_retries_on_non_json_gateway_error_and_succeeds(self):
        client = _client_failing_then_succeeding(_gateway_html_error(), _gateway_html_error())
        result = _select_photographer_with_retry(client, "user-1")
        self.assertEqual(result, "ok")

    def test_does_not_retry_real_postgrest_error(self):
        client = _client_failing_then_succeeding(_real_postgrest_error())
        with self.assertRaises(APIError):
            _select_photographer_with_retry(client, "user-1")
        client.table.return_value.execute.assert_called_once()

    def test_exhausting_retries_raises_503(self):
        from fastapi import HTTPException

        client = MagicMock()
        query = MagicMock()
        query.select.return_value = query
        query.eq.return_value = query
        query.limit.return_value = query
        query.execute.side_effect = _gateway_html_error()
        client.table.return_value = query
        with self.assertRaises(HTTPException) as ctx:
            _select_photographer_with_retry(client, "user-1")
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
