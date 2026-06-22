"""R2 공개 URL에서 썸네일 이미지를 비동기로 내려받는다 (인증 불필요한 GET)."""
import asyncio
import logging
from typing import List, Optional

import httpx

from app.config import DOWNLOAD_CONCURRENCY

logger = logging.getLogger(__name__)


async def download_all(urls: List[str]) -> List[Optional[bytes]]:
    """순서를 보존하며 다운로드. 실패한 항목은 None."""
    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    async with httpx.AsyncClient(timeout=20.0) as client:

        async def _fetch(url: str) -> Optional[bytes]:
            async with sem:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    return resp.content
                except Exception as e:
                    logger.warning("download failed for %s: %s", url, e)
                    return None

        return await asyncio.gather(*[_fetch(u) for u in urls])
