"""解析上游内容：自动识别 M3U 与 TXT(#genre#) 两种格式。

产出未归一化的 Stream 列表（name 暂用 raw_name，后续 normalize 阶段再规范化）。
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl

from .models import Stream
from .safety import sanitize_headers

_EXTINF_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_ATTR_HEADER_NAMES = {
    "http-user-agent": "User-Agent",
    "http-referrer": "Referer",
    "http-referer": "Referer",
    "http-origin": "Origin",
}


def parse_content(text: str, source: str) -> list[Stream]:
    """按内容特征分派到 m3u / txt 解析器。"""
    head = text.lstrip()[:512].lower()
    if "#extm3u" in head or "#extinf" in head:
        return parse_m3u(text, source)
    return parse_txt(text, source)


def _extract_attrs(extinf_line: str) -> dict[str, str]:
    """从 #EXTINF 行提取 tvg-* / group-title 等属性。"""
    return {k.lower(): v for k, v in _EXTINF_ATTR_RE.findall(extinf_line)}


def _display_name(extinf_line: str) -> str:
    """取 #EXTINF 行最后一个逗号之后的显示名。"""
    if "," in extinf_line:
        return extinf_line.rsplit(",", 1)[1].strip()
    return ""


def _headers_from_attrs(attrs: dict[str, str]) -> dict[str, str]:
    return sanitize_headers(
        {canonical: attrs[key] for key, canonical in _ATTR_HEADER_NAMES.items() if key in attrs}
    )


def _headers_from_pairs(value: str) -> dict[str, str]:
    """解析 ``User-Agent=x&Referer=y`` 一类头参数。"""
    return sanitize_headers({key: val for key, val in parse_qsl(value, keep_blank_values=False)})


def _headers_from_directive(line: str) -> dict[str, str]:
    if line.startswith("#EXTHTTP:"):
        try:
            payload = json.loads(line.removeprefix("#EXTHTTP:").strip())
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return sanitize_headers({str(key): str(value) for key, value in payload.items()})

    if line.startswith("#EXTVLCOPT:"):
        option = line.removeprefix("#EXTVLCOPT:").strip()
    elif line.startswith("#KODIPROP:"):
        option = line.removeprefix("#KODIPROP:").strip()
    else:
        return {}

    key, separator, value = option.partition("=")
    if not separator:
        return {}
    key = key.strip().lower()
    value = value.strip()
    if key in _ATTR_HEADER_NAMES:
        return sanitize_headers({_ATTR_HEADER_NAMES[key]: value})
    if key in {
        "inputstream.adaptive.stream_headers",
        "inputstream.adaptive.manifest_headers",
    }:
        return _headers_from_pairs(value)
    return {}


def _split_url_headers(value: str) -> tuple[str, dict[str, str]]:
    """拆分 ``URL|User-Agent=...&Referer=...`` 形式。"""
    url, separator, options = value.partition("|")
    if not separator:
        return value, {}
    return url.strip(), _headers_from_pairs(options)


def parse_m3u(text: str, source: str) -> list[Stream]:
    streams: list[Stream] = []
    pending: dict[str, str] | None = None
    pending_headers: dict[str, str] = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs = _extract_attrs(line)
            pending = {
                "name": _display_name(line) or attrs.get("tvg-name", ""),
                "logo": attrs.get("tvg-logo", ""),
                "tvg_id": attrs.get("tvg-id", ""),
            }
            pending_headers.update(_headers_from_attrs(attrs))
        elif line.startswith(("#EXTVLCOPT:", "#KODIPROP:", "#EXTHTTP:")):
            pending_headers.update(_headers_from_directive(line))
        elif line.startswith("#"):
            # #EXTM3U 与其他不影响播放的指令
            continue
        else:
            name = (pending or {}).get("name", "").strip()
            if name:
                url, pipe_headers = _split_url_headers(line)
                stream_headers = {**pending_headers, **pipe_headers}
                streams.append(
                    Stream(
                        url=url,
                        name=name,
                        raw_name=name,
                        logo=(pending or {}).get("logo", ""),
                        tvg_id=(pending or {}).get("tvg_id", ""),
                        source=source,
                        headers=sanitize_headers(stream_headers),
                    )
                )
            pending = None
            pending_headers = {}

    return streams


def parse_txt(text: str, source: str) -> list[Stream]:
    """解析 TXT：'分组名,#genre#' 定义分组；'频道名,URL1#URL2' 定义频道（可多 URL）。"""
    streams: list[Stream] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "," not in line:
            continue
        name_part, url_part = line.split(",", 1)
        name = name_part.strip()
        url_part = url_part.strip()

        # 分组分隔行，如 "央视,#genre#"
        if url_part.lower() == "#genre#":
            continue
        if not name or not url_part:
            continue

        # 一行多 URL，用 # 分隔
        for raw_url in url_part.split("#"):
            raw_url = raw_url.strip()
            url, headers = _split_url_headers(raw_url)
            if url.lower().startswith(("http://", "https://", "rtmp://", "rtp://")):
                streams.append(
                    Stream(
                        url=url,
                        name=name,
                        raw_name=name,
                        source=source,
                        headers=headers,
                    )
                )

    return streams
