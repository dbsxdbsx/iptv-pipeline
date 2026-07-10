"""GitHub Actions 的准备、分片深验与原子发布入口。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import shutil
import sys
import uuid
from pathlib import Path

from .artifacts import (
    read_candidate_bundle,
    read_deep_results,
    write_candidate_bundle,
    write_deep_results,
)
from .config import Config
from .deep_probe import probe_all_deep
from .fetch import fetch_all
from .models import Channel, Stream
from .normalize import build_channels
from .parse import parse_content
from .pipeline import write_outputs
from .probe import ProbeResult, probe_all
from .rank import build_stable_channels
from .safety import supports_deep_probe
from .state import TIER_GRACE, HealthState

logger = logging.getLogger(__name__)


async def prepare(config_dir: Path, bundle_path: Path) -> None:
    cfg = Config.load(config_dir)
    contents = await fetch_all(cfg.upstreams)
    if not contents:
        raise RuntimeError("没有任何上游拉取成功")
    streams: list[Stream] = []
    for source, content in contents.items():
        streams.extend(parse_content(content, source))
    channels = build_channels(streams, cfg)
    flat = [stream for channel in channels for stream in channel.streams]
    probe_results = await probe_all(
        flat,
        timeout=cfg.validation.fast_timeout_seconds,
    )
    fast_by_state = {stream.state_key(): probe_results[stream.dedup_key()] for stream in flat}
    generation = uuid.uuid4().hex
    write_candidate_bundle(
        bundle_path,
        generation=generation,
        channels=channels,
        fast_results=fast_by_state,
    )
    logger.info(
        "候选准备完成: generation=%s, %d 上游, %d 频道, %d 流",
        generation,
        len(contents),
        len(channels),
        len(flat),
    )


async def verify(
    config_dir: Path,
    bundle_path: Path,
    output_path: Path,
    *,
    shard_index: int,
    shard_count: int,
) -> None:
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        raise RuntimeError("ffprobe/ffmpeg 未安装")
    cfg = Config.load(config_dir)
    generation, channels, fast_results = read_candidate_bundle(bundle_path)
    candidates = [
        stream
        for channel in channels
        for stream in channel.streams
        if fast_results.get(stream.state_key()) == ProbeResult.OK
        and supports_deep_probe(stream.url)
        and not stream.is_ipv6
        and int(stream.state_key()[:8], 16) % shard_count == shard_index
    ]
    logger.info(
        "分片 %d/%d 开始深验 %d 条线路",
        shard_index + 1,
        shard_count,
        len(candidates),
    )
    results = await probe_all_deep(candidates, cfg.validation)
    write_deep_results(
        output_path,
        generation=generation,
        shard_index=shard_index,
        shard_count=shard_count,
        results=results,
    )


def publish(
    config_dir: Path,
    bundle_path: Path,
    results_dir: Path,
    state_path: Path,
    previous_meta_path: Path,
    previous_manifest_path: Path,
    base_output_sha_path: Path,
    previous_state_present_path: Path,
    output_dir: Path,
    *,
    network_vantage: str,
) -> None:
    cfg = Config.load(config_dir)
    generation, channels, fast_results = read_candidate_bundle(bundle_path)
    deep_results = {}
    result_files = sorted(results_dir.rglob("deep-results-*.json"))
    if not result_files:
        raise RuntimeError("未找到深验结果分片")
    for path in result_files:
        for state_key, result in read_deep_results(path, generation).items():
            if state_key in deep_results:
                raise RuntimeError(f"深验结果重复: {state_key}")
            deep_results[state_key] = result

    flat = [stream for channel in channels for stream in channel.streams]
    expected_deep = {
        stream.state_key()
        for stream in flat
        if fast_results.get(stream.state_key()) == ProbeResult.OK
        and supports_deep_probe(stream.url)
        and not stream.is_ipv6
    }
    missing = expected_deep - deep_results.keys()
    unexpected = deep_results.keys() - expected_deep
    if missing or unexpected:
        raise RuntimeError(f"深验分片不完整: missing={len(missing)}, unexpected={len(unexpected)}")

    base_output_sha = _read_base_output_sha(base_output_sha_path)
    previous_state_present = _read_previous_state_present(previous_state_present_path)
    state = _load_previous_state(
        state_path,
        previous_meta_path,
        previous_manifest_path,
        has_previous=bool(base_output_sha) and previous_state_present,
    )
    for stream in flat:
        state_key = stream.state_key()
        fast_result = fast_results[state_key]
        state.set_source_count(state_key, len(stream.sources))
        state.apply_fast_result(state_key, fast_result, cfg.validation)
        if state_key in deep_results:
            state.apply_deep_result(state_key, deep_results[state_key], cfg.validation)

    alive_keys = {stream.state_key() for stream in flat}
    state.prune_stale(alive_keys)
    broad = _filter_broad_channels(channels, state)
    stable = build_stable_channels(
        broad,
        state,
        max_streams_per_channel=cfg.validation.stable_max_per_channel,
    )
    _enforce_quality_gate(
        stable,
        state,
        cfg,
        previous_meta_path,
    )
    write_outputs(
        broad,
        stable,
        state,
        output_dir,
        generation=generation,
        network_vantage=network_vantage,
    )
    logger.info(
        "发布产物准备完成: generation=%s, stable=%d频道/%d线路",
        generation,
        len(stable),
        sum(len(channel.streams) for channel in stable),
    )


def _filter_broad_channels(
    channels: list[Channel],
    state: HealthState,
) -> list[Channel]:
    filtered: list[Channel] = []
    for channel in channels:
        streams = [
            stream for stream in channel.streams if not state.should_drop(stream.state_key())
        ]
        if not streams:
            continue
        filtered.append(
            Channel(
                name=channel.name,
                group=channel.group,
                logo=channel.logo,
                tvg_id=channel.tvg_id,
                streams=streams,
            )
        )
    return filtered


def _enforce_quality_gate(
    stable: list[Channel],
    state: HealthState,
    cfg: Config,
    previous_meta_path: Path,
) -> None:
    channel_count = len(stable)
    stream_keys = [stream.state_key() for channel in stable for stream in channel.streams]
    stream_count = len(stream_keys)
    if channel_count < cfg.validation.minimum_stable_channels:
        raise RuntimeError(
            "质量门禁失败: "
            f"stable 频道数 {channel_count} < {cfg.validation.minimum_stable_channels}"
        )
    grace_count = sum(state.stable_tier(key) == TIER_GRACE for key in stream_keys)
    if stream_count > 0 and grace_count / stream_count > 0.10:
        raise RuntimeError(f"质量门禁失败: GRACE 占比 {grace_count / stream_count:.1%} > 10%")

    previous_stats = _previous_stable_stats(previous_meta_path)
    previous_count = previous_stats["channels_stable"]
    if previous_count <= 0:
        return
    drop_ratio = max(0.0, (previous_count - channel_count) / previous_count)
    if drop_ratio > cfg.validation.maximum_drop_ratio:
        raise RuntimeError(
            "质量门禁失败: "
            f"stable 频道从 {previous_count} 降至 {channel_count}（-{drop_ratio:.1%}）"
        )
    previous_streams = previous_stats["streams_stable"]
    stream_drop_ratio = (
        max(0.0, (previous_streams - stream_count) / previous_streams)
        if previous_streams > 0
        else 0.0
    )
    if stream_drop_ratio > cfg.validation.maximum_drop_ratio:
        raise RuntimeError(
            "质量门禁失败: "
            f"stable 线路从 {previous_streams} 降至 {stream_count}"
            f"（-{stream_drop_ratio:.1%}）"
        )
    current_backup_channels = sum(len(channel.streams) >= 2 for channel in stable)
    previous_backup_channels = previous_stats["channels_with_backup"]
    backup_drop_ratio = (
        max(
            0.0,
            (previous_backup_channels - current_backup_channels) / previous_backup_channels,
        )
        if previous_backup_channels > 0
        else 0.0
    )
    if backup_drop_ratio > cfg.validation.maximum_drop_ratio:
        raise RuntimeError(
            "质量门禁失败: "
            f"双线路频道从 {previous_backup_channels} 降至 {current_backup_channels}"
            f"（-{backup_drop_ratio:.1%}）"
        )


def _previous_stable_stats(path: Path) -> dict[str, int]:
    empty = {
        "channels_stable": 0,
        "streams_stable": 0,
        "channels_with_backup": 0,
    }
    if not path.exists():
        return empty
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stats = payload.get("stats", {})
        return {
            key: int(stats.get(key, 0))
            for key in (
                "channels_stable",
                "streams_stable",
                "channels_with_backup",
            )
        }
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return empty


def _read_base_output_sha(path: Path) -> str:
    if not path.exists():
        raise RuntimeError("缺少 base-output-sha.txt")
    value = path.read_text(encoding="utf-8").strip()
    if value and (len(value) != 40 or any(c not in "0123456789abcdef" for c in value)):
        raise RuntimeError("base output SHA 格式无效")
    return value


def _read_previous_state_present(path: Path) -> bool:
    if not path.exists():
        raise RuntimeError("缺少 previous-state-present.txt")
    value = path.read_text(encoding="utf-8").strip()
    if value not in {"0", "1"}:
        raise RuntimeError("previous-state-present 标记无效")
    return value == "1"


def _load_previous_state(
    state_path: Path,
    meta_path: Path,
    manifest_path: Path,
    *,
    has_previous: bool,
) -> HealthState:
    if not has_previous:
        return HealthState()
    try:
        state = HealthState.load(state_path, strict=True)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise RuntimeError(f"上一代状态读取失败: {exc}") from exc
    generations = {
        state.generation,
        str(meta.get("generation", "")),
        str(manifest.get("generation", "")),
    }
    if "" in generations or len(generations) != 1:
        raise RuntimeError("上一代 state/meta/manifest generation 不一致")
    stats = meta.get("stats")
    if not isinstance(stats, dict):
        raise RuntimeError("上一代 meta.stats 缺失")
    for key in ("channels_stable", "streams_stable", "channels_with_backup"):
        if not isinstance(stats.get(key), int) or stats[key] < 0:
            raise RuntimeError(f"上一代 meta.stats.{key} 无效")
    expected_files = manifest.get("files", {})
    for relative_name, path in (
        (".state/health.json", state_path),
        ("meta.json", meta_path),
    ):
        expected_hash = expected_files.get(relative_name)
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if not expected_hash or expected_hash != actual_hash:
            raise RuntimeError(f"上一代文件哈希不一致: {relative_name}")
    return state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="iptv-pipeline CI 分阶段入口")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--config", default="config")
    prepare_parser.add_argument("--bundle", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--config", default="config")
    verify_parser.add_argument("--bundle", required=True)
    verify_parser.add_argument("--output", required=True)
    verify_parser.add_argument("--shard-index", type=int, required=True)
    verify_parser.add_argument("--shard-count", type=int, required=True)

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--config", default="config")
    publish_parser.add_argument("--bundle", required=True)
    publish_parser.add_argument("--results-dir", required=True)
    publish_parser.add_argument("--state", required=True)
    publish_parser.add_argument("--previous-meta", required=True)
    publish_parser.add_argument("--previous-manifest", required=True)
    publish_parser.add_argument("--base-output-sha", required=True)
    publish_parser.add_argument("--previous-state-present", required=True)
    publish_parser.add_argument("--output", required=True)
    publish_parser.add_argument("--network-vantage", default="github-hosted")
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            asyncio.run(prepare(Path(args.config), Path(args.bundle)))
        elif args.command == "verify":
            if args.shard_count <= 0 or not 0 <= args.shard_index < args.shard_count:
                raise ValueError("无效 shard 参数")
            asyncio.run(
                verify(
                    Path(args.config),
                    Path(args.bundle),
                    Path(args.output),
                    shard_index=args.shard_index,
                    shard_count=args.shard_count,
                )
            )
        else:
            publish(
                Path(args.config),
                Path(args.bundle),
                Path(args.results_dir),
                Path(args.state),
                Path(args.previous_meta),
                Path(args.previous_manifest),
                Path(args.base_output_sha),
                Path(args.previous_state_present),
                Path(args.output),
                network_vantage=args.network_vantage,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
