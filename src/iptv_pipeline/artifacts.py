"""CI 分片之间传递候选频道与深验结果的稳定 JSON 契约。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .deep_probe import DeepProbeResult
from .models import Channel, Stream
from .probe import ProbeResult

ARTIFACT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DeepResultShard:
    shard_index: int
    shard_count: int
    results: dict[str, DeepProbeResult]


def write_candidate_bundle(
    path: Path,
    *,
    generation: str,
    channels: list[Channel],
    fast_results: dict[str, ProbeResult],
) -> None:
    payload = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "generation": generation,
        "channels": [_channel_to_dict(channel) for channel in channels],
        "fast_results": {state_key: result.value for state_key, result in fast_results.items()},
    }
    _write_json(path, payload)


def read_candidate_bundle(
    path: Path,
) -> tuple[str, list[Channel], dict[str, ProbeResult]]:
    payload = _read_json(path)
    _require_schema(payload)
    channels = [_channel_from_dict(item) for item in payload.get("channels", [])]
    fast_results = {
        str(state_key): ProbeResult(value)
        for state_key, value in payload.get("fast_results", {}).items()
    }
    return str(payload["generation"]), channels, fast_results


def write_deep_results(
    path: Path,
    *,
    generation: str,
    shard_index: int,
    shard_count: int,
    results: dict[str, DeepProbeResult],
) -> None:
    payload = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "generation": generation,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "results": {state_key: result.to_dict() for state_key, result in results.items()},
    }
    _write_json(path, payload)


def read_deep_results(path: Path, expected_generation: str) -> DeepResultShard:
    payload = _read_json(path)
    _require_schema(payload)
    if payload.get("generation") != expected_generation:
        raise ValueError(f"深验分片 generation 不一致: {path}")
    shard_index = payload.get("shard_index")
    shard_count = payload.get("shard_count")
    if (
        not isinstance(shard_index, int)
        or isinstance(shard_index, bool)
        or not isinstance(shard_count, int)
        or isinstance(shard_count, bool)
        or shard_count <= 0
        or not 0 <= shard_index < shard_count
    ):
        raise ValueError(f"深验分片元数据无效: {path}")
    raw_results = payload.get("results")
    if not isinstance(raw_results, dict):
        raise ValueError(f"深验分片 results 无效: {path}")
    results = {
        str(state_key): DeepProbeResult.from_dict(result)
        for state_key, result in raw_results.items()
    }
    return DeepResultShard(
        shard_index=shard_index,
        shard_count=shard_count,
        results=results,
    )


def _stream_to_dict(stream: Stream) -> dict:
    return {
        "url": stream.url,
        "name": stream.name,
        "raw_name": stream.raw_name,
        "logo": stream.logo,
        "tvg_id": stream.tvg_id,
        "source": stream.source,
        "sources": stream.sources,
        "headers": stream.headers,
        "is_ipv6": stream.is_ipv6,
    }


def _stream_from_dict(data: dict) -> Stream:
    return Stream(
        url=str(data["url"]),
        name=str(data["name"]),
        raw_name=str(data.get("raw_name", "")),
        logo=str(data.get("logo", "")),
        tvg_id=str(data.get("tvg_id", "")),
        source=str(data.get("source", "")),
        sources=[str(value) for value in data.get("sources", [])],
        headers={str(name): str(value) for name, value in data.get("headers", {}).items()},
        is_ipv6=bool(data.get("is_ipv6", False)),
    )


def _channel_to_dict(channel: Channel) -> dict:
    return {
        "name": channel.name,
        "group": channel.group,
        "logo": channel.logo,
        "tvg_id": channel.tvg_id,
        "streams": [_stream_to_dict(stream) for stream in channel.streams],
    }


def _channel_from_dict(data: dict) -> Channel:
    return Channel(
        name=str(data["name"]),
        group=str(data.get("group", "其他")),
        logo=str(data.get("logo", "")),
        tvg_id=str(data.get("tvg_id", "")),
        streams=[_stream_from_dict(item) for item in data.get("streams", [])],
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"无效 JSON artifact: {path}")
    return payload


def _require_schema(payload: dict) -> None:
    if payload.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("不支持的 artifact schema")
