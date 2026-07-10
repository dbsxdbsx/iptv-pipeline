"""配置加载：upstreams / aliases / blacklist / groups。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

VALIDATION_SCOPE = "ffmpeg-gstreamer-headerless-v1"


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
    match: list[str]
    priority_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ValidationConfig:
    fast_timeout_seconds: int = 8
    deep_timeout_seconds: int = 15
    decode_seconds: int = 4
    deep_concurrency: int = 4
    gstreamer_timeout_seconds: int = 12
    require_gstreamer: bool = True
    stable_max_per_channel: int = 2
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
    group_rules: list[GroupRule]
    default_group: str
    validation: ValidationConfig = field(default_factory=ValidationConfig)

    @classmethod
    def load(cls, config_dir: Path) -> Config:
        upstreams = load_lines(config_dir / "upstreams.txt")
        blacklist = [kw.lower() for kw in load_lines(config_dir / "blacklist.txt")]

        alias_to_canonical, canonical_names = _load_aliases(config_dir / "aliases.json")
        group_rules, default_group = _load_groups(config_dir / "groups.json")
        validation = _load_validation(config_dir / "validation.json")

        return cls(
            upstreams=upstreams,
            alias_to_canonical=alias_to_canonical,
            canonical_names=canonical_names,
            blacklist=blacklist,
            group_rules=group_rules,
            default_group=default_group,
            validation=validation,
        )


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


def _load_groups(path: Path) -> tuple[list[GroupRule], str]:
    if not path.exists():
        return [], "其他"
    data = json.loads(path.read_text(encoding="utf-8"))
    default_group = data.get("default_group", "其他")
    order = data.get("order", [])
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
            )
        )
    return rules, default_group


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
        stable_max_per_channel=max(1, min(5, int(data.get("stable_max_per_channel", 2)))),
        grace_hours=max(0, int(data.get("grace_hours", 12))),
        grace_rounds=max(0, int(data.get("grace_rounds", 2))),
        minimum_stable_channels=max(1, int(data.get("minimum_stable_channels", 100))),
        maximum_drop_ratio=max(0.0, min(1.0, float(data.get("maximum_drop_ratio", 0.25)))),
    )
