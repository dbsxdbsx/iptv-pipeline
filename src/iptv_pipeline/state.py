"""跨轮次健康状态：实现"宽松删除"。

只有连续多轮硬失败的流才会被剔除，避免海外 runner 单次误判就误删优质源。
状态文件随仓库提交（体积小），在 CI 多次运行间保持连续性。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .probe import ProbeResult

#: 连续硬失败达到该次数则从产出中剔除
HARD_FAIL_LIMIT = 3


@dataclass
class HealthState:
    #: dedup_key -> {"hard_streak": int, "last_ok": float, "last_seen": float}
    entries: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> HealthState:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(entries=data.get("entries", {}))
        except (json.JSONDecodeError, OSError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": time.time(), "entries": self.entries}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def update(self, dedup_key: str, result: ProbeResult) -> None:
        now = time.time()
        entry = self.entries.setdefault(
            dedup_key, {"hard_streak": 0, "last_ok": 0.0, "last_seen": 0.0}
        )
        entry["last_seen"] = now
        if result == ProbeResult.OK:
            entry["hard_streak"] = 0
            entry["last_ok"] = now
        elif result == ProbeResult.HARD_FAIL:
            entry["hard_streak"] = entry.get("hard_streak", 0) + 1
        # SOFT_FAIL / SKIPPED：不加也不减，维持现状（宽松）

    def should_drop(self, dedup_key: str) -> bool:
        entry = self.entries.get(dedup_key)
        if not entry:
            return False
        return entry.get("hard_streak", 0) >= HARD_FAIL_LIMIT

    def prune_stale(self, alive_keys: set[str]) -> None:
        """清理已不在任何上游出现的历史条目，防止状态文件无限膨胀。"""
        self.entries = {k: v for k, v in self.entries.items() if k in alive_keys}
