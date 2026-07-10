"""编排：拉取 -> 解析 -> 归一化去重 -> (可选)验证 -> 产出。"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .deep_probe import DeepProbeStatus, probe_all_deep
from .emit import to_m3u, to_meta_json, to_txt
from .fetch import fetch_all
from .models import Channel, Stream
from .normalize import build_channels, is_chinese_channel
from .parse import parse_content
from .probe import ProbeResult, probe_all
from .rank import build_stable_channels
from .state import HealthState

logger = logging.getLogger(__name__)


@dataclass
class PipelineStats:
    upstreams_total: int = 0
    upstreams_ok: int = 0
    streams_parsed: int = 0
    channels: int = 0
    streams_final: int = 0
    dropped_by_probe: int = 0
    stable_channels: int = 0
    stable_streams: int = 0
    deep_pass: int = 0
    deep_soft_fail: int = 0
    deep_hard_fail: int = 0
    deep_unsupported: int = 0
    generation: str = ""


async def run_pipeline(
    config_dir: Path,
    output_dir: Path,
    state_path: Path,
    *,
    do_probe: bool = True,
    do_deep_probe: bool = True,
    probe_timeout: int = 8,
    network_vantage: str = "local",
) -> PipelineStats:
    cfg = Config.load(config_dir)
    stats = PipelineStats(
        upstreams_total=len(cfg.upstreams),
        generation=uuid.uuid4().hex,
    )
    logger.info("加载 %d 个上游源", stats.upstreams_total)

    # 1) 拉取
    contents = await fetch_all(cfg.upstreams)
    stats.upstreams_ok = len(contents)
    logger.info("成功拉取 %d/%d 个上游", stats.upstreams_ok, stats.upstreams_total)

    # 2) 解析
    all_streams: list[Stream] = []
    for url, text in contents.items():
        parsed = parse_content(text, source=url)
        logger.info("解析 %d 条流: %s", len(parsed), url)
        all_streams.extend(parsed)
    stats.streams_parsed = len(all_streams)

    # 3) 归一化 + 去重 + 分组 + 排序
    all_channels = build_channels(all_streams, cfg)
    stats.channels = len(all_channels)

    state = HealthState.load(state_path)
    stable_channels: list[Channel] = []

    # 4) 快筛 + 深验：all 宽松，stable 正向准入
    if do_probe:
        all_channels, stable_channels = await _validate_and_filter(
            all_channels,
            state,
            cfg,
            probe_timeout,
            do_deep_probe,
            stats,
        )
        state.save(state_path)

    stats.channels = len(all_channels)
    stats.streams_final = sum(len(ch.streams) for ch in all_channels)
    stats.stable_channels = len(stable_channels)
    stats.stable_streams = sum(len(ch.streams) for ch in stable_channels)

    # 5) 产出
    write_outputs(
        all_channels,
        stable_channels,
        state,
        output_dir,
        generation=stats.generation,
        network_vantage=network_vantage,
    )
    logger.info(
        "完成：候选 %d 频道 / %d 流，稳定 %d 频道 / %d 流（候选池丢弃 %d 条连续硬失败）",
        stats.channels,
        stats.streams_final,
        stats.stable_channels,
        stats.stable_streams,
        stats.dropped_by_probe,
    )
    return stats


async def _validate_and_filter(
    channels: list[Channel],
    state: HealthState,
    cfg: Config,
    probe_timeout: int,
    do_deep_probe: bool,
    stats: PipelineStats,
) -> tuple[list[Channel], list[Channel]]:
    flat = [st for ch in channels for st in ch.streams]
    results = await probe_all(flat, timeout=probe_timeout)

    alive_keys = {st.state_key() for st in flat}
    for st in flat:
        state_key = st.state_key()
        state.set_source_count(state_key, len(st.sources))
        state.apply_fast_result(state_key, results[st.dedup_key()], cfg.validation)

    if do_deep_probe:
        deep_candidates = [
            stream for stream in flat if results[stream.dedup_key()] == ProbeResult.OK
        ]
        logger.info("进入 FFmpeg 深度验证: %d 条", len(deep_candidates))
        deep_results = await probe_all_deep(deep_candidates, cfg.validation)
        for stream in deep_candidates:
            result = deep_results[stream.state_key()]
            state.apply_deep_result(stream.state_key(), result, cfg.validation)
            if result.status == DeepProbeStatus.PASS:
                stats.deep_pass += 1
            elif result.status == DeepProbeStatus.SOFT_FAIL:
                stats.deep_soft_fail += 1
            elif result.status == DeepProbeStatus.HARD_FAIL:
                stats.deep_hard_fail += 1
            else:
                stats.deep_unsupported += 1

    filtered: list[Channel] = []
    for ch in channels:
        kept = [st for st in ch.streams if not state.should_drop(st.state_key())]
        stats.dropped_by_probe += len(ch.streams) - len(kept)
        if kept:
            ch.streams = kept
            filtered.append(ch)

    state.prune_stale(alive_keys)
    stable = (
        build_stable_channels(
            filtered,
            state,
            max_streams_per_channel=cfg.validation.stable_max_per_channel,
        )
        if do_deep_probe
        else []
    )
    return filtered, stable


def write_outputs(
    all_channels: list[Channel],
    stable_channels: list[Channel],
    state: HealthState,
    output_dir: Path,
    *,
    generation: str,
    network_vantage: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "all.m3u": to_m3u(all_channels),
        "all.txt": to_txt(all_channels),
        "stable.m3u": to_m3u(stable_channels),
        "stable.txt": to_txt(stable_channels),
    }

    cn = [c for c in stable_channels if _is_cn_channel(c)]
    global_ = [c for c in stable_channels if not _is_cn_channel(c)]
    files["cn.m3u"] = to_m3u(cn)
    files["global.m3u"] = to_m3u(global_)
    files["meta.json"] = to_meta_json(
        all_channels,
        stable_channels,
        state,
        generation=generation,
        network_vantage=network_vantage,
    )
    for name, content in files.items():
        (output_dir / name).write_text(content, encoding="utf-8")

    state.generation = generation
    state_path = output_dir / ".state" / "health.json"
    state.save(state_path)
    manifest_files = {
        name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest() for name in files
    }
    manifest_files[".state/health.json"] = hashlib.sha256(state_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "generation": generation,
        "files": manifest_files,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


#: 明确归入国内 / 国际的分组
_CN_GROUPS = {"央视", "卫视", "港澳台"}


def _is_cn_channel(ch: Channel) -> bool:
    """cn/global 归属：先看分组，再对"其他"这类含混分组用汉字启发式兜底。"""
    if ch.group in _CN_GROUPS:
        return True
    if ch.group == "国际":
        return False
    return is_chinese_channel(ch.name)
