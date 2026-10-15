import io
import logging
import unittest
from unittest.mock import patch

import app.main  # noqa: F401 — 로그 설정이 import 시점에 적용된다


class AppLoggingTests(unittest.TestCase):
    def test_app_info_logs_reach_stdout(self):
        # 업로드 단계별 시간 로그는 INFO다. 기본값(WARNING)이면 운영 로그에서 사라진다.
        handler = logging.getLogger("app").handlers[0]
        stream = io.StringIO()
        with patch.object(handler, "stream", stream):
            logging.getLogger("app.routers.upload").info("photographer upload timing total_ms=1")
        self.assertIn("photographer upload timing total_ms=1", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
