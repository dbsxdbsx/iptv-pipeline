"""管道内部数据模型。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256


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
    #: 同一条流可能被多个上游重复收录；用于置信度与诊断
    sources: list[str] = field(default_factory=list)
    #: 播放该线路所需的公开、安全 HTTP 头
    headers: dict[str, str] = field(default_factory=dict)
    #: 是否 IPv6 流（GitHub Actions 无法验证，需标记直通）
    is_ipv6: bool = False

    def __post_init__(self) -> None:
        if self.source and self.source not in self.sources:
            self.sources.append(self.source)

    def dedup_key(self) -> str:
        """跨源去重键：同名、URL 与请求头都相同才视为重复。"""
        headers = json.dumps(
            self.headers,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"{self.name}\t{self.url}\t{headers}"

    def state_key(self) -> str:
        """稳定、定长的健康状态键，避免把 URL token 写进 JSON 对象键。"""
        return sha256(self.dedup_key().encode("utf-8")).hexdigest()

    def merge_provenance(self, other: Stream) -> None:
        """合并重复流的来源与可选元数据。"""
        for source in other.sources:
            if source and source not in self.sources:
                self.sources.append(source)
        if not self.logo and other.logo:
            self.logo = other.logo
        if not self.tvg_id and other.tvg_id:
            self.tvg_id = other.tvg_id


@dataclass
class Channel:
    """一个逻辑频道，聚合了来自多个上游的多条流。"""

    name: str
    group: str = "其他"
    logo: str = ""
    tvg_id: str = ""
    streams: list[Stream] = field(default_factory=list)
