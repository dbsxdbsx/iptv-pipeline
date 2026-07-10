"""不可信上游输入的 URL 与 HTTP 头安全边界。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

# 公共产物只透传不会携带账号凭据的请求头。
_PUBLIC_HEADER_NAMES = {
    "user-agent": "User-Agent",
    "referer": "Referer",
    "referrer": "Referer",
    "origin": "Origin",
    "accept": "Accept",
    "accept-language": "Accept-Language",
    "accept-encoding": "Accept-Encoding",
}

_STREAM_SCHEMES = {"http", "https", "rtmp", "rtp"}
_DEEP_PROBE_SCHEMES = {"http", "https"}


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    """规范并过滤可安全公开的请求头，拒绝 CR/LF 注入。"""
    safe: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = _PUBLIC_HEADER_NAMES.get(raw_name.strip().lower())
        value = str(raw_value).strip()
        if name is None or not value or "\r" in value or "\n" in value:
            continue
        safe[name] = value
    return safe


def is_safe_stream_url(url: str) -> bool:
    """是否为可公开处理的流 URL。

    域名留到网络层解析；这里先拒绝本机、私网、链路本地、组播及保留 IP 字面量，
    避免公开聚合器成为 SSRF 或局域网地址分发器。
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in _STREAM_SCHEMES or not parsed.hostname:
        return False

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return _is_public_address(address)


async def resolves_to_public_addresses(url: str) -> bool:
    """解析顶层 URL 的全部 A/AAAA；任一非公网结果都拒绝。

    该检查与 CI 网络层 egress 防火墙共同使用，降低 DNS rebinding 风险。
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if not is_safe_stream_url(url) or not parsed.hostname:
        return False
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        return False
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                parsed.hostname,
                port,
                type=socket.SOCK_STREAM,
            ),
            timeout=5,
        )
    except (OSError, TimeoutError, UnicodeError):
        return False
    addresses = {
        info[4][0].split("%", 1)[0] for info in infos if info and len(info) >= 5 and info[4]
    }
    if not addresses:
        return False
    try:
        return all(_is_public_address(ipaddress.ip_address(value)) for value in addresses)
    except ValueError:
        return False


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_global and not (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_private
        or address.is_reserved
        or address.is_unspecified
    )


def supports_deep_probe(url: str) -> bool:
    """GitHub 托管 runner 主链允许深验的协议。"""
    try:
        return urlsplit(url).scheme.lower() in _DEEP_PROBE_SCHEMES
    except ValueError:
        return False


def redact_url(url: str) -> str:
    """日志中隐藏 query/fragment，避免泄露公开链接携带的临时 token。"""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
