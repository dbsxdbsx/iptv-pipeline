"""v1 有效性快筛：aiohttp 并发探测流可达性。

约束（已核实的工程事实）：
- GitHub Actions 托管 runner 不支持 IPv6 出网，IPv6 流一律跳过验证并直通保留。
- runner 在海外，大陆源易误判 -> 区分硬失败/软失败，只有硬失败才计入删除计数。
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum

import aiohttp

from .models import Stream

logger = logging.getLogger(__name__)


class ProbeResult(str, Enum):
    OK = "ok"
    SOFT_FAIL = "soft_fail"  # 超时/网络抖动，海外 runner 视角不可靠，不计入删除
    HARD_FAIL = "hard_fail"  # DNS 失败 / 拒连 / 4xx / 5xx，较可信
    SKIPPED = "skipped"  # IPv6，无法验证，直通


async def _probe_one(session: aiohttp.ClientSession, stream: Stream, timeout: int) -> ProbeResult:
    if stream.is_ipv6:
        return ProbeResult.SKIPPED
    if not stream.url.lower().startswith(("http://", "https://")):
        # rtmp/rtp 等非 HTTP 流无法用 aiohttp 探测，直通保留
        return ProbeResult.SKIPPED

    try:
        # 只取响应头 + 首字节，尽量轻
        async with session.get(
            stream.url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                return ProbeResult.HARD_FAIL
            try:
                await resp.content.read(1)
            except Exception:  # noqa: BLE001
                return ProbeResult.SOFT_FAIL
            return ProbeResult.OK
    except aiohttp.ClientConnectorError:
        return ProbeResult.HARD_FAIL  # DNS/拒连
    except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
        return ProbeResult.SOFT_FAIL
    except aiohttp.ClientResponseError:
        return ProbeResult.HARD_FAIL
    except Exception:  # noqa: BLE001 - 其余归为软失败，宁可保留
        return ProbeResult.SOFT_FAIL


async def probe_all(
    streams: list[Stream], timeout: int = 8, concurrency: int = 50
) -> dict[str, ProbeResult]:
    """并发探测，返回 {dedup_key: ProbeResult}。"""
    sem = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, force_close=True)

    async with aiohttp.ClientSession(connector=connector) as session:

        async def _guarded(st: Stream) -> tuple[str, ProbeResult]:
            async with sem:
                return st.dedup_key(), await _probe_one(session, st, timeout)

        pairs = await asyncio.gather(*(_guarded(st) for st in streams))

    results = dict(pairs)
    _log_summary(results)
    return results


def _log_summary(results: dict[str, ProbeResult]) -> None:
    counts: dict[ProbeResult, int] = {}
    for r in results.values():
        counts[r] = counts.get(r, 0) + 1
    logger.info(
        "探测结果: OK=%d SOFT=%d HARD=%d SKIP(IPv6/非HTTP)=%d",
        counts.get(ProbeResult.OK, 0),
        counts.get(ProbeResult.SOFT_FAIL, 0),
        counts.get(ProbeResult.HARD_FAIL, 0),
        counts.get(ProbeResult.SKIPPED, 0),
    )
