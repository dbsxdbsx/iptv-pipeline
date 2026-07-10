"""产出 m3u / txt 文件。"""

from __future__ import annotations

from .models import Channel

#: 客户端可读的 EPG 地址（写入 m3u 头，供播放器自动加载节目单）
DEFAULT_EPG_URL = "https://epg.112114.xyz/pp.xml"


def to_m3u(channels: list[Channel], epg_url: str = DEFAULT_EPG_URL) -> str:
    """生成标准 M3U：带 group-title / tvg-id / tvg-logo，同频道多流依次列出。"""
    lines = [f'#EXTM3U x-tvg-url="{epg_url}"']
    for ch in channels:
        for st in ch.streams:
            attrs = [f'tvg-name="{ch.name}"']
            if ch.tvg_id:
                attrs.append(f'tvg-id="{ch.tvg_id}"')
            if ch.logo:
                attrs.append(f'tvg-logo="{ch.logo}"')
            attrs.append(f'group-title="{ch.group}"')
            lines.append(f"#EXTINF:-1 {' '.join(attrs)},{ch.name}")
            lines.append(st.url)
    return "\n".join(lines) + "\n"


def to_txt(channels: list[Channel]) -> str:
    """生成 TXT(#genre#)：按分组输出，同频道多流合并为一行 URL1#URL2。"""
    lines: list[str] = []
    current_group: str | None = None
    for ch in channels:
        if ch.group != current_group:
            current_group = ch.group
            lines.append(f"{current_group},#genre#")
        urls = "#".join(st.url for st in ch.streams)
        lines.append(f"{ch.name},{urls}")
    return "\n".join(lines) + "\n"
