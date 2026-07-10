"""状态机测试：宽松删除逻辑。"""

from pathlib import Path

from iptv_pipeline.probe import ProbeResult
from iptv_pipeline.state import HARD_FAIL_LIMIT, HealthState


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
    st = HealthState()
    st.update("a\thttp://x/1", ProbeResult.HARD_FAIL)
    st.save(p)
    loaded = HealthState.load(p)
    assert loaded.entries["a\thttp://x/1"]["hard_streak"] == 1


def test_load_missing_file_returns_empty(tmp_path: Path):
    assert HealthState.load(tmp_path / "nope.json").entries == {}
