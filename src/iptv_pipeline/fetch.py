"""并发拉取上游成品列表。"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


async def _fetch_one(
    session: aiohttp.ClientSession, url: str, timeout: int
) -> tuple[str, str | None]:
    """返回 (url, 内容)；失败返回 (url, None)。"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                logger.warning("上游返回 %s: %s", resp.status, url)
                return url, None
            text = await resp.text(errors="replace")
            logger.info("已拉取 %d 字符: %s", len(text), url)
            return url, text
    except Exception as exc:  # noqa: BLE001 - 上游千奇百怪，统一降级
        logger.warning("上游拉取失败 (%s): %s", type(exc).__name__, url)
        return url, None


async def fetch_all(
    urls: list[str], timeout: int = DEFAULT_TIMEOUT, concurrency: int = 8
) -> dict[str, str]:
    """并发拉取所有上游，返回 {url: 内容}（仅含成功项）。"""
    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": DEFAULT_UA}

    async with aiohttp.ClientSession(headers=headers) as session:

        async def _guarded(u: str) -> tuple[str, str | None]:
            async with sem:
                return await _fetch_one(session, u, timeout)

        results = await asyncio.gather(*(_guarded(u) for u in urls))

    return {url: content for url, content in results if content is not None}
