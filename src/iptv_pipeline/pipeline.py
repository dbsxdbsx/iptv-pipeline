"""编排：拉取 -> 解析 -> 归一化去重 -> (可选)验证 -> 产出。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .emit import to_m3u, to_txt
from .fetch import fetch_all
from .models import Channel, Stream
from .normalize import build_channels, is_chinese_channel
from .parse import parse_content
from .probe import probe_all
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


async def run_pipeline(
    config_dir: Path,
    output_dir: Path,
    state_path: Path,
    *,
    do_probe: bool = True,
    probe_timeout: int = 8,
) -> PipelineStats:
    cfg = Config.load(config_dir)
    stats = PipelineStats(upstreams_total=len(cfg.upstreams))
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
    channels = build_channels(all_streams, cfg)
    stats.channels = len(channels)

    # 4) 验证（v1）：宽松删除
    if do_probe:
        channels = await _probe_and_filter(channels, state_path, probe_timeout, stats)

    stats.streams_final = sum(len(ch.streams) for ch in channels)

    # 5) 产出
    _write_outputs(channels, output_dir)
    logger.info(
        "完成：%d 频道 / %d 流（丢弃 %d 条硬失败）",
        stats.channels,
        stats.streams_final,
        stats.dropped_by_probe,
    )
    return stats


async def _probe_and_filter(
    channels: list[Channel],
    state_path: Path,
    probe_timeout: int,
    stats: PipelineStats,
) -> list[Channel]:
    state = HealthState.load(state_path)
    flat = [st for ch in channels for st in ch.streams]
    results = await probe_all(flat, timeout=probe_timeout)

    alive_keys = {st.dedup_key() for st in flat}
    for st in flat:
        state.update(st.dedup_key(), results[st.dedup_key()])

    filtered: list[Channel] = []
    for ch in channels:
        kept = [st for st in ch.streams if not state.should_drop(st.dedup_key())]
        stats.dropped_by_probe += len(ch.streams) - len(kept)
        if kept:
            ch.streams = kept
            filtered.append(ch)

    state.prune_stale(alive_keys)
    state.save(state_path)
    return filtered


def _write_outputs(channels: list[Channel], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "all.m3u").write_text(to_m3u(channels), encoding="utf-8")
    (output_dir / "all.txt").write_text(to_txt(channels), encoding="utf-8")

    cn = [c for c in channels if _is_cn_channel(c)]
    global_ = [c for c in channels if not _is_cn_channel(c)]
    (output_dir / "cn.m3u").write_text(to_m3u(cn), encoding="utf-8")
    (output_dir / "global.m3u").write_text(to_m3u(global_), encoding="utf-8")


#: 明确归入国内 / 国际的分组
_CN_GROUPS = {"央视", "卫视", "港澳台"}


def _is_cn_channel(ch: Channel) -> bool:
    """cn/global 归属：先看分组，再对"其他"这类含混分组用汉字启发式兜底。"""
    if ch.group in _CN_GROUPS:
        return True
    if ch.group == "国际":
        return False
    return is_chinese_channel(ch.name)
