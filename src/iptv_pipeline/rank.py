"""严格源线路排序与频道裁剪。"""

from __future__ import annotations

from .models import Channel, Stream
from .state import TIER_GRACE, TIER_PASS, HealthState


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


def build_stable_channels(
    channels: list[Channel],
    state: HealthState,
    max_streams_per_channel: int,
) -> list[Channel]:
    """仅保留正向准入线路，每频道限制为最优 N 条。"""
    stable: list[Channel] = []
    for channel in channels:
        eligible = [
            stream for stream in channel.streams if state.is_stable_eligible(stream.state_key())
        ]
        eligible.sort(key=lambda stream: stream_rank_key(stream, state))
        kept = eligible[:max_streams_per_channel]
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
