"""管道内部数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Stream:
    """一条具体的直播流（同一频道可有多条）。"""

    url: str
    #: 归一化后的规范频道名，例如 "CCTV-1"
    name: str
    #: 原始频道名（上游里写的名字），用于调试与别名补充
    raw_name: str = ""
    #: 台标 URL（m3u tvg-logo）
    logo: str = ""
    #: EPG 频道 id（m3u tvg-id）
    tvg_id: str = ""
    #: 来源上游 URL（哪个上游提供了这条流）
    source: str = ""
    #: 是否 IPv6 流（GitHub Actions 无法验证，需标记直通）
    is_ipv6: bool = False

    def dedup_key(self) -> str:
        """跨源去重键：同名 + 同 URL 视为重复。"""
        return f"{self.name}\t{self.url}"


@dataclass
class Channel:
    """一个逻辑频道，聚合了来自多个上游的多条流。"""

    name: str
    group: str = "其他"
    logo: str = ""
    tvg_id: str = ""
    streams: list[Stream] = field(default_factory=list)
