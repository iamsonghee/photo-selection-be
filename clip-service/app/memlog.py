"""임시 계측: Railway Sleep 동작 여부 및 CLIP lazy-load 전환 효과를 RSS로 확인하기 위한 로깅.
검증 끝나면 삭제할 코드다."""
import logging
import os

import psutil

logger = logging.getLogger(__name__)


def _rss_mb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def log_rss(tag: str) -> None:
    logger.info("RSS[%s]=%.1fMB", tag, _rss_mb())


def log_perf(tag: str, elapsed_sec: float | None = None, **kwargs) -> None:
    """단계별 성능 계측 로그.

    형식: [PERF] tag=<tag> [elapsed_sec=<n>] rss_mb=<n> [key=value ...]

    elapsed_sec: 해당 단계 자체 소요시간. _start 태그에는 생략.
    total_sec: pipeline 시작 시각 기준 누적 시간 (kwargs로 전달).
    n: 처리 대상 건수.
    success_count / failure_count / batch_count: 결과 통계.
    """
    rss = _rss_mb()
    parts = [f"tag={tag}"]
    if elapsed_sec is not None:
        parts.append(f"elapsed_sec={elapsed_sec:.1f}")
    parts.append(f"rss_mb={rss:.1f}")
    for k, v in kwargs.items():
        if v is None:
            continue
        if isinstance(v, float):
            parts.append(f"{k}={v:.1f}")
        else:
            parts.append(f"{k}={v}")
    logger.info("[PERF] %s", " ".join(parts))
