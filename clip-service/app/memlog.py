"""임시 계측: Railway Sleep 동작 여부 및 CLIP lazy-load 전환 효과를 RSS로 확인하기 위한 로깅.
검증 끝나면 삭제할 코드다."""
import logging
import os

import psutil

logger = logging.getLogger(__name__)


def log_rss(tag: str) -> None:
    rss_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    logger.info("RSS[%s]=%.1fMB", tag, rss_mb)
