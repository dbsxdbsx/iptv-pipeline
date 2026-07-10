"""归一化：频道名规范化、黑名单过滤、跨源去重、分组、IPv6 标记。"""

from __future__ import annotations

import re
import unicodedata

from .config import Config
from .models import Channel, Stream
from .safety import is_safe_stream_url

# 归一化时移除的噪声词（画质标签等），避免 "CCTV1HD" 与 "CCTV1" 被当成两个频道。
_CJK_QUALITY_TOKENS = ("高清", "超清", "标清", "蓝光")
_ASCII_QUALITY_SUFFIX_RE = re.compile(
    r"(?:[\s\-_]*(?:sd|hd|fhd|uhd|(?:360|480|576|720|1080|1440|2160)[pi]?))+$",
    re.IGNORECASE,
)
_QUALITY_BRACKET_RE = re.compile(
    r"[\(\[（【]\s*(?:sd|hd|fhd|uhd|4k|8k|"
    r"(?:360|480|576|720|1080|1440|2160)[pi]?|hevc|h\.?26[45])\s*[\)\]）】]",
    re.IGNORECASE,
)
_IPV6_RE = re.compile(r"\[[0-9a-fA-F:]+\]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def normalize_key(name: str) -> str:
    """把频道名归一成匹配用的 key：小写、去空白/分隔符/噪声词、全角转半角。"""
    s = unicodedata.normalize("NFKC", name).strip().lower()
    s = _QUALITY_BRACKET_RE.sub("", s)
    for token in _CJK_QUALITY_TOKENS:
        s = s.replace(token, "")
    s = _ASCII_QUALITY_SUFFIX_RE.sub("", s)
    # 去掉空格与常见分隔符（保留 + 号，如 CCTV5+）
    s = re.sub(r"[\s\-_·.,、|/\\()\[\]{}（）【】]", "", s)
    return s


def is_ipv6_url(url: str) -> bool:
    """URL 的 host 部分是否为 IPv6 字面量（[...] 形式）。"""
    return bool(_IPV6_RE.search(url))


def is_chinese_channel(name: str) -> bool:
    """频道名是否含中日韩汉字。用于 cn/global 产物拆分的轻量启发式：
    绝大多数国内频道名带汉字，国际频道名为拉丁字母（CGTN 等中国外宣频道归入 global 亦合理）。
    """
    return bool(_CJK_RE.search(name))


def canonicalize_name(raw_name: str, cfg: Config) -> str:
    """把原始频道名映射到规范名；命中别名表则用规范名，否则原样返回（去空白）。"""
    key = normalize_key(raw_name)
    return cfg.alias_to_canonical.get(key, raw_name.strip())


def is_blacklisted(stream: Stream, cfg: Config) -> bool:
    hay = f"{stream.raw_name} {stream.url}".lower()
    return any(kw in hay for kw in cfg.blacklist)


def assign_group(name: str, cfg: Config) -> str:
    key = normalize_key(name)
    for rule in cfg.group_rules:
        # 规范名在该组优先列表内直接归入
        if name in rule.priority_names:
            return rule.name
        if any(m in key or m in name.lower() for m in rule.match):
            return rule.name
    return cfg.default_group


def build_channels(streams: list[Stream], cfg: Config) -> list[Channel]:
    """核心聚合：过滤 -> 规范化 -> 跨源去重 -> 按频道聚合 -> 分组 -> 排序。"""
    channels: dict[str, Channel] = {}
    seen_streams: dict[str, Stream] = {}

    for st in streams:
        if is_blacklisted(st, cfg) or not is_safe_stream_url(st.url):
            continue

        st.name = canonicalize_name(st.raw_name, cfg)
        st.is_ipv6 = is_ipv6_url(st.url)

        dk = st.dedup_key()
        existing = seen_streams.get(dk)
        if existing is not None:
            existing.merge_provenance(st)
            continue
        seen_streams[dk] = st

        ch = channels.get(st.name)
        if ch is None:
            ch = Channel(name=st.name, group=assign_group(st.name, cfg))
            channels[st.name] = ch
        ch.streams.append(st)
        # 频道级 logo / tvg_id 取第一个非空
        if not ch.logo and st.logo:
            ch.logo = st.logo
        if not ch.tvg_id and st.tvg_id:
            ch.tvg_id = st.tvg_id

    return _sort_channels(list(channels.values()), cfg)


def _sort_channels(channels: list[Channel], cfg: Config) -> list[Channel]:
    """按分组顺序 + 组内优先名顺序 + 自然序排序。"""
    group_order = {rule.name: i for i, rule in enumerate(cfg.group_rules)}
    default_idx = len(group_order)

    priority_index: dict[str, int] = {}
    for rule in cfg.group_rules:
        for i, pname in enumerate(rule.priority_names):
            priority_index[pname] = i

    def sort_key(ch: Channel) -> tuple:
        g_idx = group_order.get(ch.group, default_idx)
        p_idx = priority_index.get(ch.name, 10_000)
        return (g_idx, p_idx, _natural_key(ch.name))

    return sorted(channels, key=sort_key)


def _natural_key(name: str) -> list:
    """自然排序：让 CCTV-2 排在 CCTV-10 前面。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]
