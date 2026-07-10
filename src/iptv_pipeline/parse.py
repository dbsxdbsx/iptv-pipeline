"""解析上游内容：自动识别 M3U 与 TXT(#genre#) 两种格式。

产出未归一化的 Stream 列表（name 暂用 raw_name，后续 normalize 阶段再规范化）。
"""

from __future__ import annotations

import re

from .models import Stream

_EXTINF_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


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


def parse_m3u(text: str, source: str) -> list[Stream]:
    streams: list[Stream] = []
    pending: dict[str, str] | None = None

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
        elif line.startswith("#"):
            # #EXTM3U / #EXTVLCOPT / #KODIPROP 等，忽略（v1 不透传自定义头）
            continue
        else:
            name = (pending or {}).get("name", "").strip()
            if name:
                streams.append(
                    Stream(
                        url=line,
                        name=name,
                        raw_name=name,
                        logo=(pending or {}).get("logo", ""),
                        tvg_id=(pending or {}).get("tvg_id", ""),
                        source=source,
                    )
                )
            pending = None

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
        for url in url_part.split("#"):
            url = url.strip()
            if url.lower().startswith(("http://", "https://", "rtmp://", "rtp://")):
                streams.append(Stream(url=url, name=name, raw_name=name, source=source))

    return streams
