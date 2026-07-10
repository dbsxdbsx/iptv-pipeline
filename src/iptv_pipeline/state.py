"""跨轮次健康状态：实现"宽松删除"。

``all.m3u`` 仍用连续硬失败避免误删；``stable.m3u`` 使用正向深验准入：
PASS 或受限 GRACE 才能出现，明确硬失败/解码失败立即退出 stable。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import ValidationConfig
from .deep_probe import DeepProbeResult, DeepProbeStatus
from .probe import ProbeResult

#: 连续硬失败达到该次数则从产出中剔除
HARD_FAIL_LIMIT = 3
STATE_SCHEMA_VERSION = 2

TIER_PASS = "pass"
TIER_GRACE = "grace"
TIER_REJECT = "reject"
TIER_UNVERIFIED = "unverified"


@dataclass
class HealthState:
    #: state_key -> 健康与深验信息
    entries: dict[str, dict] = field(default_factory=dict)
    generation: str = ""

    @classmethod
    def load(cls, path: Path, *, strict: bool = False) -> HealthState:
        if not path.exists():
            if strict:
                raise ValueError(f"状态文件不存在: {path}")
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("entries", {}), dict):
                raise ValueError(f"状态文件结构无效: {path}")
            if strict and data.get("schema_version") != STATE_SCHEMA_VERSION:
                raise ValueError(f"状态 schema 不兼容: {path}")
            return cls(
                entries=data.get("entries", {}),
                generation=str(data.get("generation", "")),
            )
        except (json.JSONDecodeError, OSError, ValueError):
            if strict:
                raise
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "generation": self.generation,
            "updated_at": time.time(),
            "entries": self.entries,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def update(self, dedup_key: str, result: ProbeResult) -> None:
        """兼容旧调用：更新宽松快筛状态。"""
        now = time.time()
        entry = self._entry(dedup_key)
        entry["last_seen"] = now
        entry["last_fast_status"] = result.value
        if result == ProbeResult.OK:
            entry["hard_streak"] = 0
            entry["last_ok"] = now
        elif result == ProbeResult.HARD_FAIL:
            entry["hard_streak"] = entry.get("hard_streak", 0) + 1
        # SOFT_FAIL / SKIPPED：不加也不减，维持现状（宽松）

    def apply_fast_result(
        self,
        state_key: str,
        result: ProbeResult,
        config: ValidationConfig,
    ) -> None:
        """快筛同时更新 broad 状态与 strict 准入状态。"""
        self.update(state_key, result)
        entry = self._entry(state_key)
        if result == ProbeResult.HARD_FAIL:
            entry["tier"] = TIER_REJECT
            entry["deep_reason"] = "fast_hard_fail"
            entry["grace_rounds"] = 0
        elif result == ProbeResult.SOFT_FAIL:
            self._apply_soft_failure(entry, config, "fast_soft_fail")
        elif result == ProbeResult.SKIPPED:
            entry["tier"] = TIER_UNVERIFIED
            entry["deep_reason"] = "fast_skipped"
            entry["grace_rounds"] = 0

    def apply_deep_result(
        self,
        state_key: str,
        result: DeepProbeResult,
        config: ValidationConfig,
    ) -> None:
        entry = self._entry(state_key)
        entry.update(
            {
                "last_seen": time.time(),
                "last_deep_checked": result.checked_at,
                "last_deep_status": result.status.value,
                "deep_reason": result.reason,
                "latency_ms": result.latency_ms,
                "codec": result.codec,
                "width": result.width,
                "height": result.height,
                "duration_seconds": result.duration_seconds,
                "decoded_frames": result.decoded_frames,
                "freeze_detected": result.freeze_detected,
            }
        )
        if result.status == DeepProbeStatus.PASS:
            entry["tier"] = TIER_PASS
            entry["last_deep_ok"] = result.checked_at
            entry["grace_rounds"] = 0
            entry["deep_successes"] = int(entry.get("deep_successes", 0)) + 1
            entry["freeze_streak"] = (
                int(entry.get("freeze_streak", 0)) + 1 if result.freeze_detected else 0
            )
        elif result.status == DeepProbeStatus.SOFT_FAIL:
            self._apply_soft_failure(entry, config, result.reason)
        elif result.status == DeepProbeStatus.HARD_FAIL:
            entry["tier"] = TIER_REJECT
            entry["grace_rounds"] = 0
        else:
            entry["tier"] = TIER_UNVERIFIED
            entry["grace_rounds"] = 0

    def stable_tier(self, state_key: str) -> str:
        return str(self.entries.get(state_key, {}).get("tier", TIER_UNVERIFIED))

    def is_stable_eligible(self, state_key: str) -> bool:
        return self.stable_tier(state_key) in {TIER_PASS, TIER_GRACE}

    def confidence(self, state_key: str) -> float:
        entry = self.entries.get(state_key, {})
        tier = entry.get("tier")
        if tier == TIER_PASS:
            freeze_penalty = min(int(entry.get("freeze_streak", 0)), 2) * 0.05
            source_score = min(int(entry.get("source_count", 1)), 3) * 0.02
            return max(0.0, min(1.0, 0.94 + source_score - freeze_penalty))
        if tier == TIER_GRACE:
            return 0.65
        return 0.0

    def should_drop(self, dedup_key: str) -> bool:
        entry = self.entries.get(dedup_key)
        if not entry:
            return False
        return entry.get("hard_streak", 0) >= HARD_FAIL_LIMIT

    def prune_stale(self, alive_keys: set[str]) -> None:
        """清理已不在任何上游出现的历史条目，防止状态文件无限膨胀。"""
        self.entries = {k: v for k, v in self.entries.items() if k in alive_keys}

    def set_source_count(self, state_key: str, count: int) -> None:
        self._entry(state_key)["source_count"] = max(1, count)

    def _entry(self, state_key: str) -> dict:
        return self.entries.setdefault(
            state_key,
            {
                "hard_streak": 0,
                "last_ok": 0.0,
                "last_seen": 0.0,
                "tier": TIER_UNVERIFIED,
                "grace_rounds": 0,
                "last_deep_ok": 0.0,
                "freeze_streak": 0,
                "deep_successes": 0,
            },
        )

    @staticmethod
    def _apply_soft_failure(
        entry: dict,
        config: ValidationConfig,
        reason: str,
    ) -> None:
        now = time.time()
        last_ok = float(entry.get("last_deep_ok", 0.0) or 0.0)
        grace_rounds = int(entry.get("grace_rounds", 0))
        grace_age = config.grace_hours * 60 * 60
        if last_ok > 0 and now - last_ok <= grace_age and grace_rounds < config.grace_rounds:
            entry["tier"] = TIER_GRACE
            entry["grace_rounds"] = grace_rounds + 1
        else:
            entry["tier"] = TIER_UNVERIFIED
            entry["grace_rounds"] = 0
        entry["deep_reason"] = reason
