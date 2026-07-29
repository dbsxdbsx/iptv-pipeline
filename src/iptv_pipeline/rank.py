"""严格源线路排序与频道裁剪。"""

from __future__ import annotations

from urllib.parse import urlsplit

from .models import Channel, Stream
from .state import TIER_GRACE, TIER_PASS, HealthState


def stream_host(url: str) -> str:
    """提取用于多样性去重的 host（小写，去尾点）。"""
    try:
        hostname = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return hostname.rstrip(".").lower()


def stream_rank_key(stream: Stream, state: HealthState) -> tuple:
    entry = state.entries.get(stream.state_key(), {})
    tier = entry.get("tier")
    tier_rank = {TIER_PASS: 0, TIER_GRACE: 1}.get(tier, 9)
    confidence = state.confidence(stream.state_key())
    latency = int(entry.get("latency_ms", 2_147_483_647) or 2_147_483_647)
    deep_successes = int(entry.get("deep_successes", 0))
    return (
        tier_rank,
        -confidence,
        -deep_successes,
        latency,
        -len(stream.sources),
        stream.url,
    )


def select_diverse_streams(
    eligible: list[Stream],
    max_streams_per_channel: int,
) -> list[Stream]:
    """在已按质量排序的候选中，优先保留不同 host，再回填同 host 优质线。

    PASS 已排在 GRACE 之前，因此前几名会自然偏向 PASS；GRACE 只在
    PASS 不足时补位。
    """
    if max_streams_per_channel <= 0 or not eligible:
        return []
    if len(eligible) <= max_streams_per_channel:
        return list(eligible)

    kept: list[Stream] = []
    seen_hosts: set[str] = set()
    for stream in eligible:
        host = stream_host(stream.url)
        if host and host in seen_hosts:
            continue
        kept.append(stream)
        if host:
            seen_hosts.add(host)
        if len(kept) >= max_streams_per_channel:
            return kept

    kept_ids = {id(stream) for stream in kept}
    for stream in eligible:
        if id(stream) in kept_ids:
            continue
        kept.append(stream)
        if len(kept) >= max_streams_per_channel:
            break
    return kept


def build_stable_channels(
    channels: list[Channel],
    state: HealthState,
    max_streams_per_channel: int,
) -> list[Channel]:
    """仅保留正向准入线路，每频道限制为最优 N 条（优先 host 多样）。"""
    stable: list[Channel] = []
    for channel in channels:
        eligible = [
            stream for stream in channel.streams if state.is_stable_eligible(stream.state_key())
        ]
        eligible.sort(key=lambda stream: stream_rank_key(stream, state))
        kept = select_diverse_streams(eligible, max_streams_per_channel)
        if not kept:
            continue
        stable.append(
            Channel(
                name=channel.name,
                group=channel.group,
                logo=channel.logo,
                tvg_id=channel.tvg_id,
                streams=kept,
            )
        )
    return stable
