"""配置加载：upstreams / aliases / blacklist / groups。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

VALIDATION_SCOPE = "ffmpeg5.1.9-gstreamer1.22-headerless-only-v1"  # 与 Dockerfile 同步


def _strip_inline_comment(line: str) -> str:
    """去掉行尾 ' #注释'（要求 # 前有空白），保留 URL 中的 #。"""
    for i in range(1, len(line)):
        if line[i] == "#" and line[i - 1].isspace():
            return line[:i]
    return line


def load_lines(path: Path) -> list[str]:
    """读取行式配置：忽略空行与 # 注释行，去除行尾附注。"""
    if not path.exists():
        return []
    result: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _strip_inline_comment(raw).strip()
        if not line or line.startswith("#"):
            continue
        result.append(line)
    return result


@dataclass
class GroupRule:
    name: str
    #: 频道名关键字（归一化后做子串匹配），第一级判定依据
    match: list[str]
    priority_names: list[str] = field(default_factory=list)
    #: 上游分组名精确匹配集合（归一化后的 key），第二级判定依据
    upstream: set[str] = field(default_factory=set)
    #: 上游分组名子串匹配（如省份名，可覆盖「☘️四川频道」这类带装饰的变体）
    upstream_match: list[str] = field(default_factory=list)
    #: cn / global / auto，决定 cn.m3u 与 global.m3u 的归属
    scope: str = "auto"


@dataclass(frozen=True)
class ValidationConfig:
    fast_timeout_seconds: int = 8
    deep_timeout_seconds: int = 15
    decode_seconds: int = 4
    deep_concurrency: int = 4
    gstreamer_timeout_seconds: int = 12
    require_gstreamer: bool = True
    stable_max_per_channel: int = 5
    grace_hours: int = 12
    grace_rounds: int = 2
    minimum_stable_channels: int = 100
    maximum_drop_ratio: float = 0.25


@dataclass
class Config:
    upstreams: list[str]
    #: 别名 -> 规范名 的展开映射（已做归一化 key）
    alias_to_canonical: dict[str, str]
    #: 规范名列表（保序，用于产出排序参考）
    canonical_names: list[str]
    blacklist: list[str]
    #: 按判定优先级排列（groups.json 的 order）：同一频道命中多条规则时靠前者胜出
    group_rules: list[GroupRule]
    default_group: str
    #: 按展示顺序排列的分组名（groups.json 的 display_order，缺省时同 order）。
    #: 判定优先级要「题材优先于地域」，展示顺序要「常看的排前面」，两者结论不同，
    #: 合成一个字段就必然牺牲一边。
    display_order: list[str] = field(default_factory=list)
    #: 境外兜底分组：前两级都没命中且频道名不含汉字时归入。空串表示关闭该级。
    foreign_group: str = ""
    #: 台标 URL 前缀重写表 (旧前缀, 新前缀)，保序，先命中先用
    logo_rewrites: list[tuple[str, str]] = field(default_factory=list)
    validation: ValidationConfig = field(default_factory=ValidationConfig)

    @classmethod
    def load(cls, config_dir: Path) -> Config:
        upstreams = load_lines(config_dir / "upstreams.txt")
        blacklist = [kw.lower() for kw in load_lines(config_dir / "blacklist.txt")]

        alias_to_canonical, canonical_names = _load_aliases(config_dir / "aliases.json")
        groups = _load_groups(config_dir / "groups.json")
        validation = _load_validation(config_dir / "validation.json")
        logo_rewrites = _load_logo_rewrites(config_dir / "logo_rewrites.json")

        return cls(
            upstreams=upstreams,
            alias_to_canonical=alias_to_canonical,
            canonical_names=canonical_names,
            blacklist=blacklist,
            group_rules=groups.rules,
            default_group=groups.default_group,
            display_order=groups.display_order,
            foreign_group=groups.foreign_group,
            logo_rewrites=logo_rewrites,
            validation=validation,
        )

    def group_scope(self, group_name: str) -> str:
        """查分组的 cn/global 归属；未登记的分组（含 default_group）返回 auto。"""
        for rule in self.group_rules:
            if rule.name == group_name:
                return rule.scope
        return "auto"

    def group_display_index(self, group_name: str) -> int:
        """查分组的展示序号；未登记的分组排到所有已登记分组之后。"""
        names = self.display_order or [rule.name for rule in self.group_rules]
        try:
            return names.index(group_name)
        except ValueError:
            return len(names)


def _load_aliases(path: Path) -> tuple[dict[str, str], list[str]]:
    from .normalize import normalize_key

    if not path.exists():
        return {}, []
    data = json.loads(path.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    canonical_names: list[str] = []
    for canonical, aliases in data.items():
        if canonical.startswith("_"):
            continue
        canonical_names.append(canonical)
        # 规范名自身也是别名
        mapping[normalize_key(canonical)] = canonical
        for alias in aliases:
            mapping[normalize_key(alias)] = canonical
    return mapping, canonical_names


@dataclass(frozen=True)
class GroupConfig:
    rules: list[GroupRule]
    default_group: str
    foreign_group: str
    display_order: list[str]


def _load_groups(path: Path) -> GroupConfig:
    from .normalize import normalize_group_key

    if not path.exists():
        return GroupConfig(rules=[], default_group="其他", foreign_group="", display_order=[])
    data = json.loads(path.read_text(encoding="utf-8"))
    default_group = data.get("default_group", "其他")
    foreign_group = data.get("foreign_group", "")
    order = data.get("order", [])
    display_order = data.get("display_order", []) or list(order)
    groups_data = data.get("groups", {})

    rules: list[GroupRule] = []
    for name in order:
        if name not in groups_data:
            continue
        g = groups_data[name]
        rules.append(
            GroupRule(
                name=name,
                match=[m.lower() for m in g.get("match", [])],
                priority_names=g.get("priority_names", []),
                upstream={
                    key for key in (normalize_group_key(u) for u in g.get("upstream", [])) if key
                },
                upstream_match=[
                    key
                    for key in (normalize_group_key(u) for u in g.get("upstream_match", []))
                    if key
                ],
                scope=str(g.get("scope", "auto")),
            )
        )
    return GroupConfig(
        rules=rules,
        default_group=default_group,
        foreign_group=foreign_group,
        display_order=display_order,
    )


def _load_logo_rewrites(path: Path) -> list[tuple[str, str]]:
    """读台标前缀重写表。保序返回，让「先命中先用」可预测。"""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rewrites = data.get("rewrites", {})
    return [
        (str(src), str(dst))
        for src, dst in rewrites.items()
        if src and dst and not src.startswith("_")
    ]


def _load_validation(path: Path) -> ValidationConfig:
    if not path.exists():
        return ValidationConfig()
    data = json.loads(path.read_text(encoding="utf-8"))
    return ValidationConfig(
        fast_timeout_seconds=max(1, int(data.get("fast_timeout_seconds", 8))),
        deep_timeout_seconds=max(5, int(data.get("deep_timeout_seconds", 15))),
        decode_seconds=max(2, int(data.get("decode_seconds", 4))),
        deep_concurrency=max(1, min(8, int(data.get("deep_concurrency", 4)))),
        gstreamer_timeout_seconds=max(5, int(data.get("gstreamer_timeout_seconds", 12))),
        require_gstreamer=bool(data.get("require_gstreamer", True)),
        stable_max_per_channel=max(1, min(5, int(data.get("stable_max_per_channel", 5)))),
        grace_hours=max(0, int(data.get("grace_hours", 12))),
        grace_rounds=max(0, int(data.get("grace_rounds", 2))),
        minimum_stable_channels=max(1, int(data.get("minimum_stable_channels", 100))),
        maximum_drop_ratio=max(0.0, min(1.0, float(data.get("maximum_drop_ratio", 0.25)))),
    )
