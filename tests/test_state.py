"""状态机测试：宽松删除逻辑。"""

import json
from pathlib import Path

import pytest

from iptv_pipeline.config import ValidationConfig
from iptv_pipeline.deep_probe import DeepProbeResult, DeepProbeStatus
from iptv_pipeline.probe import ProbeResult
from iptv_pipeline.state import (
    HARD_FAIL_LIMIT,
    TIER_GRACE,
    TIER_PASS,
    TIER_REJECT,
    TIER_UNVERIFIED,
    HealthState,
)


def test_hard_fail_streak_triggers_drop():
    st = HealthState()
    key = "CCTV-1\thttp://x/1"
    for _ in range(HARD_FAIL_LIMIT):
        st.update(key, ProbeResult.HARD_FAIL)
    assert st.should_drop(key)


def test_ok_resets_streak():
    st = HealthState()
    key = "CCTV-1\thttp://x/1"
    st.update(key, ProbeResult.HARD_FAIL)
    st.update(key, ProbeResult.HARD_FAIL)
    st.update(key, ProbeResult.OK)  # 恢复
    st.update(key, ProbeResult.HARD_FAIL)
    assert not st.should_drop(key)  # streak 被重置，只有 1 次


def test_soft_fail_does_not_count():
    st = HealthState()
    key = "CCTV-1\thttp://x/1"
    for _ in range(HARD_FAIL_LIMIT + 2):
        st.update(key, ProbeResult.SOFT_FAIL)
    assert not st.should_drop(key)


def test_skipped_ipv6_never_dropped():
    st = HealthState()
    key = "CCTV-1\thttp://[::1]/1"
    for _ in range(HARD_FAIL_LIMIT + 2):
        st.update(key, ProbeResult.SKIPPED)
    assert not st.should_drop(key)


def test_prune_stale_removes_absent_keys():
    st = HealthState()
    st.update("a\thttp://x/1", ProbeResult.OK)
    st.update("b\thttp://x/2", ProbeResult.OK)
    st.prune_stale({"a\thttp://x/1"})
    assert "a\thttp://x/1" in st.entries
    assert "b\thttp://x/2" not in st.entries


def test_save_and_load_roundtrip(tmp_path: Path):
    p = tmp_path / "health.json"
    st = HealthState(generation="g1")
    st.update("a\thttp://x/1", ProbeResult.HARD_FAIL)
    st.save(p)
    loaded = HealthState.load(p)
    assert loaded.entries["a\thttp://x/1"]["hard_streak"] == 1
    assert loaded.generation == "g1"


def test_load_missing_file_returns_empty(tmp_path: Path):
    assert HealthState.load(tmp_path / "nope.json").entries == {}


def test_strict_load_rejects_missing_or_corrupt_state(tmp_path: Path):
    with pytest.raises(ValueError):
        HealthState.load(tmp_path / "missing.json", strict=True)
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        HealthState.load(corrupt, strict=True)


def test_stable_requires_positive_deep_pass():
    state = HealthState()
    config = ValidationConfig()
    key = "sha256-key"

    state.apply_fast_result(key, ProbeResult.OK, config)
    assert state.stable_tier(key) == TIER_UNVERIFIED
    assert not state.is_stable_eligible(key)

    state.apply_deep_result(
        key,
        DeepProbeResult(
            DeepProbeStatus.PASS,
            "decoded",
            checked_at=1000.0,
            decoded_frames=10,
        ),
        config,
    )
    assert state.stable_tier(key) == TIER_PASS
    assert state.is_stable_eligible(key)


def test_soft_failure_has_bounded_grace(monkeypatch):
    state = HealthState()
    config = ValidationConfig(grace_hours=12, grace_rounds=2)
    key = "sha256-key"
    monkeypatch.setattr("iptv_pipeline.state.time.time", lambda: 1000.0)
    state.apply_deep_result(
        key,
        DeepProbeResult(DeepProbeStatus.PASS, "decoded", checked_at=1000.0),
        config,
    )

    monkeypatch.setattr("iptv_pipeline.state.time.time", lambda: 1100.0)
    for expected in (TIER_GRACE, TIER_GRACE, TIER_UNVERIFIED):
        state.apply_deep_result(
            key,
            DeepProbeResult(
                DeepProbeStatus.SOFT_FAIL,
                "ffmpeg_timeout",
                checked_at=1100.0,
            ),
            config,
        )
        assert state.stable_tier(key) == expected


def test_hard_failure_immediately_removes_stable_entry():
    state = HealthState()
    config = ValidationConfig()
    key = "sha256-key"
    state.apply_deep_result(
        key,
        DeepProbeResult(DeepProbeStatus.PASS, "decoded", checked_at=1000.0),
        config,
    )
    state.apply_deep_result(
        key,
        DeepProbeResult(
            DeepProbeStatus.HARD_FAIL,
            "media_or_http_error",
            checked_at=1100.0,
        ),
        config,
    )

    assert state.stable_tier(key) == TIER_REJECT
    assert not state.is_stable_eligible(key)
