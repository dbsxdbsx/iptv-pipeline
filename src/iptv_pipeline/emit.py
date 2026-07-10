"""产出 m3u / txt 文件。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .models import Channel
from .safety import sanitize_headers
from .state import HealthState

#: 客户端可读的 EPG 地址（写入 m3u 头，供播放器自动加载节目单）
DEFAULT_EPG_URL = "https://epg.112114.xyz/pp.xml"


def _escape_attr(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


def to_m3u(channels: list[Channel], epg_url: str = DEFAULT_EPG_URL) -> str:
    """生成标准 M3U：带 group-title / tvg-id / tvg-logo，同频道多流依次列出。"""
    lines = [f'#EXTM3U x-tvg-url="{_escape_attr(epg_url)}"']
    for ch in channels:
        for st in ch.streams:
            attrs = [f'tvg-name="{_escape_attr(ch.name)}"']
            if ch.tvg_id:
                attrs.append(f'tvg-id="{_escape_attr(ch.tvg_id)}"')
            if ch.logo:
                attrs.append(f'tvg-logo="{_escape_attr(ch.logo)}"')
            attrs.append(f'group-title="{_escape_attr(ch.group)}"')
            lines.append(f"#EXTINF:-1 {' '.join(attrs)},{_escape_attr(ch.name)}")
            headers = sanitize_headers(st.headers)
            if headers:
                lines.append(
                    "#EXTHTTP:"
                    + json.dumps(
                        headers,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
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


def to_meta_json(
    all_channels: list[Channel],
    stable_channels: list[Channel],
    state: HealthState,
    *,
    generation: str,
    network_vantage: str,
) -> str:
    stable_ranks = {
        stream.state_key(): rank
        for channel in stable_channels
        for rank, stream in enumerate(channel.streams, start=1)
    }
    streams: list[dict] = []
    alias_candidates: dict[str, set[str]] = {}
    for channel in all_channels:
        for stream in channel.streams:
            state_key = stream.state_key()
            entry = state.entries.get(state_key, {})
            if stream.raw_name and stream.raw_name != channel.name:
                alias_candidates.setdefault(channel.name, set()).add(stream.raw_name)
            streams.append(
                {
                    "id": state_key,
                    "name": channel.name,
                    "raw_name": stream.raw_name,
                    "group": channel.group,
                    "url": stream.url,
                    "headers": sanitize_headers(stream.headers),
                    "sources": stream.sources,
                    "tier": entry.get("tier", "unverified"),
                    "reason": entry.get("deep_reason", ""),
                    "rank_in_channel": stable_ranks.get(state_key),
                    "confidence": round(state.confidence(state_key), 4),
                    "last_verified_at": _iso_time(entry.get("last_deep_ok")),
                    "last_checked_at": _iso_time(entry.get("last_deep_checked")),
                    "latency_ms": int(entry.get("latency_ms", 0) or 0),
                    "codec": entry.get("codec", ""),
                    "width": int(entry.get("width", 0) or 0),
                    "height": int(entry.get("height", 0) or 0),
                    "decoded_frames": int(entry.get("decoded_frames", 0) or 0),
                    "freeze_detected": bool(entry.get("freeze_detected", False)),
                    "is_ipv6": stream.is_ipv6,
                }
            )

    payload = {
        "schema_version": 1,
        "generation": generation,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "network_vantage": network_vantage,
        "quality_scope": "ffmpeg_decodable_from_runner",
        "stats": {
            "channels_all": len(all_channels),
            "streams_all": sum(len(channel.streams) for channel in all_channels),
            "channels_stable": len(stable_channels),
            "streams_stable": sum(len(channel.streams) for channel in stable_channels),
            "channels_with_backup": sum(len(channel.streams) >= 2 for channel in stable_channels),
            "pass_stable": sum(
                state.stable_tier(stream.state_key()) == "pass"
                for channel in stable_channels
                for stream in channel.streams
            ),
            "grace_stable": sum(
                state.stable_tier(stream.state_key()) == "grace"
                for channel in stable_channels
                for stream in channel.streams
            ),
            "pass": sum(entry.get("tier") == "pass" for entry in state.entries.values()),
            "grace": sum(entry.get("tier") == "grace" for entry in state.entries.values()),
        },
        "alias_candidates": {
            name: sorted(raw_names) for name, raw_names in sorted(alias_candidates.items())
        },
        "streams": streams,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _iso_time(value: object) -> str | None:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
