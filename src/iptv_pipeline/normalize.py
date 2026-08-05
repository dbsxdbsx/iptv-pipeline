"""归一化：频道名规范化、黑名单过滤、跨源去重、分组、IPv6 标记。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

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
# 上游分组名的装饰字符（emoji、标点、空白）。CJK 与拉丁字母数字都属于 \w，会被保留。
_GROUP_DECORATION_RE = re.compile(r"\W+", re.UNICODE)


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


def rewrite_logo_url(url: str, cfg: Config) -> str:
    """把失效图床上的台标地址改道到可达镜像（见 config/logo_rewrites.json）。

    改道必须发生在去重之前：``merge_provenance`` 与频道级 logo 都是「取第一个非空」，
    等到那时候再改就得改好几处，而 artifacts 里落下的仍是旧地址。
    """
    if not url:
        return url
    for src, dst in cfg.logo_rewrites:
        if url.startswith(src):
            return dst + url[len(src) :]
    return url


def normalize_group_key(raw: str) -> str:
    """把上游分组名归一成匹配用的 key：全角转半角、小写、去 emoji 与标点空白。

    上游爱给分组名加装饰（`☘️四川频道`），不清掉就匹配不上。
    """
    s = unicodedata.normalize("NFKC", raw).strip().lower()
    return _GROUP_DECORATION_RE.sub("", s)


def split_upstream_groups(raw: str) -> list[str]:
    """拆分上游分组值。iptv-org 用分号承载多标签，如 `Culture;Documentary;Travel`。"""
    if not raw:
        return []
    return [key for key in (normalize_group_key(part) for part in raw.split(";")) if key]


def assign_group(name: str, raw_groups: Iterable[str], cfg: Config) -> str:
    """三级判定：频道名关键字 → 上游分组映射 → 境外兜底 → 默认组。

    关键字必须优先于上游分组：我们的规则针对频道名、置信度高，而上游分组粒度不一，
    CCTV-5 在某些上游里被塞进「咪咕赛事」，上游优先会把它从央视拽进体育，
    而用户找 CCTV-5 只会去央视找。上游分组的价值在于补关键字覆盖不到的召回
    ——地方台与点播剧集频道没有任何可靠的名字特征，只有上游分组认得它们。
    """
    key = normalize_key(name)
    lowered = name.lower()
    for rule in cfg.group_rules:
        # 规范名在该组优先列表内直接归入
        if name in rule.priority_names:
            return rule.name
        if any(m in key or m in lowered for m in rule.match):
            return rule.name

    tokens = {token for raw in raw_groups for token in split_upstream_groups(raw)}
    if tokens:
        for rule in cfg.group_rules:
            if tokens & rule.upstream:
                return rule.name
            if any(m in token for token in tokens for m in rule.upstream_match):
                return rule.name

    # 境外兜底：走到这里说明既没有名字特征也没有可信上游分组。不含汉字的绝大多数是
    # iptv-org 全球库里的小语种台，归入国际比堆在「其他」里更有用。
    if cfg.foreign_group and not is_chinese_channel(name):
        return cfg.foreign_group
    return cfg.default_group


def build_channels(streams: list[Stream], cfg: Config) -> list[Channel]:
    """核心聚合：过滤 -> 规范化 -> 跨源去重 -> 按频道聚合 -> 分组 -> 排序。"""
    channels: dict[str, Channel] = {}
    seen_streams: dict[str, Stream] = {}

    for st in streams:
        if is_blacklisted(st, cfg) or not is_safe_stream_url(st.url):
            continue

        st.name = canonicalize_name(st.raw_name, cfg)
        st.logo = rewrite_logo_url(st.logo, cfg)
        st.is_ipv6 = is_ipv6_url(st.url)

        dk = st.dedup_key()
        existing = seen_streams.get(dk)
        if existing is not None:
            existing.merge_provenance(st)
            continue
        seen_streams[dk] = st

        ch = channels.get(st.name)
        if ch is None:
            ch = Channel(name=st.name, group=cfg.default_group)
            channels[st.name] = ch
        ch.streams.append(st)
        # 频道级 logo / tvg_id 取第一个非空
        if not ch.logo and st.logo:
            ch.logo = st.logo
        if not ch.tvg_id and st.tvg_id:
            ch.tvg_id = st.tvg_id

    # 分组必须等所有流都归到频道之后再判：同一频道来自多个上游，各家给的分组名不同，
    # 只看第一条流会丢掉后来那条才带的分组信息（部分上游整份没有 group-title）。
    for ch in channels.values():
        ch.group = assign_group(ch.name, (st.raw_group for st in ch.streams), cfg)

    return _sort_channels(list(channels.values()), cfg)


def _sort_channels(channels: list[Channel], cfg: Config) -> list[Channel]:
    """按分组展示顺序 + 组内优先名顺序 + 自然序排序。

    这里用展示顺序而不是判定优先级：判定要「题材优先于地域」（想看体育的用户会点体育，
    地方台是没有明确题材的残余桶），展示要「常看的排前面」，两者结论不同。
    """

    priority_index: dict[str, int] = {}
    for rule in cfg.group_rules:
        for i, pname in enumerate(rule.priority_names):
            priority_index[pname] = i

    def sort_key(ch: Channel) -> tuple:
        p_idx = priority_index.get(ch.name, 10_000)
        return (cfg.group_display_index(ch.group), p_idx, _natural_key(ch.name))

    return sorted(channels, key=sort_key)


def _natural_key(name: str) -> list:
    """自然排序：让 CCTV-2 排在 CCTV-10 前面。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]
