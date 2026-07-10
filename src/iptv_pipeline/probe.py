"""v1 有效性快筛：aiohttp 并发探测流可达性。

约束（已核实的工程事实）：
- GitHub Actions 托管 runner 不支持 IPv6 出网，IPv6 流一律跳过验证并直通保留。
- runner 在海外，大陆源易误判 -> 区分硬失败/软失败，只有硬失败才计入删除计数。
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from urllib.parse import urljoin, urlsplit

import aiohttp

from .models import Stream
from .safety import resolves_to_public_addresses, sanitize_headers

logger = logging.getLogger(__name__)

_DEFAULT_UA = "okhttp/3.12.0"
_SNIFF_BYTES = 4096
_MAX_REDIRECTS = 5


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
        headers = sanitize_headers(stream.headers)
        headers.setdefault("User-Agent", _DEFAULT_UA)
        current_url = stream.url
        deadline = asyncio.get_running_loop().time() + timeout
        for _ in range(_MAX_REDIRECTS + 1):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return ProbeResult.SOFT_FAIL
            if not await resolves_to_public_addresses(current_url):
                return ProbeResult.HARD_FAIL
            async with session.get(
                current_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=remaining),
                allow_redirects=False,
            ) as resp:
                if 300 <= resp.status < 400:
                    location = resp.headers.get("Location")
                    if not location:
                        return ProbeResult.HARD_FAIL
                    current_url = urljoin(current_url, location)
                    continue
                status_result = _classify_status(resp.status)
                if status_result is not None:
                    return status_result
                try:
                    payload = await resp.content.read(_SNIFF_BYTES)
                except Exception:  # noqa: BLE001
                    return ProbeResult.SOFT_FAIL
                return _classify_payload(
                    current_url,
                    resp.headers.get("Content-Type", ""),
                    payload,
                )
        return ProbeResult.HARD_FAIL
    except aiohttp.ClientConnectorError:
        return ProbeResult.SOFT_FAIL
    except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
        return ProbeResult.SOFT_FAIL
    except aiohttp.ClientResponseError:
        return ProbeResult.SOFT_FAIL
    except Exception:  # noqa: BLE001 - 其余归为软失败，宁可保留
        return ProbeResult.SOFT_FAIL


def _classify_status(status: int) -> ProbeResult | None:
    if status < 400:
        return None
    if status in {408, 425, 429} or status >= 500:
        return ProbeResult.SOFT_FAIL
    return ProbeResult.HARD_FAIL


def _classify_payload(url: str, content_type: str, payload: bytes) -> ProbeResult:
    if not payload:
        return ProbeResult.SOFT_FAIL
    content_type = content_type.lower()
    text = payload.decode("utf-8", errors="ignore").lstrip()
    lowered = text.lower()
    if (
        "text/html" in content_type
        or lowered.startswith(("<!doctype html", "<html"))
        or ("application/json" in content_type and lowered.startswith(("{", "[")))
    ):
        return ProbeResult.HARD_FAIL

    path = urlsplit(url).path.lower()
    looks_like_hls = path.endswith((".m3u", ".m3u8")) or "#EXTM3U" in text.upper()
    if looks_like_hls:
        upper = text.upper()
        if "#EXTM3U" not in upper or "#EXT-X-ENDLIST" in upper:
            return ProbeResult.HARD_FAIL
    return ProbeResult.OK


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
