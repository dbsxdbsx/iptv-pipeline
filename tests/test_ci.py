import json

import pytest

from iptv_pipeline.artifacts import write_deep_results
from iptv_pipeline.ci import (
    _enforce_quality_gate,
    _load_deep_result_shards,
    _load_previous_state,
    render_upstream_summary,
    write_upstream_report,
)
from iptv_pipeline.config import VALIDATION_SCOPE, Config, ValidationConfig
from iptv_pipeline.deep_probe import DeepProbeResult, DeepProbeStatus
from iptv_pipeline.models import Channel, Stream
from iptv_pipeline.pipeline import write_outputs
from iptv_pipeline.state import HealthState


def _config(**validation_overrides) -> Config:
    return Config(
        upstreams=[],
        alias_to_canonical={},
        canonical_names=[],
        blacklist=[],
        group_rules=[],
        default_group="其他",
        validation=ValidationConfig(**validation_overrides),
    )


def _stable_channels(count: int) -> tuple[list[Channel], HealthState]:
    state = HealthState()
    state.ensure_validation_scope(VALIDATION_SCOPE)
    channels: list[Channel] = []
    for index in range(count):
        stream = Stream(
            url=f"https://media{index}.example/live.m3u8",
            name=f"Channel {index}",
            raw_name=f"Channel {index}",
        )
        state.apply_deep_result(
            stream.state_key(),
            DeepProbeResult(
                DeepProbeStatus.PASS,
                "decoded",
                checked_at=1000.0,
                decoded_frames=10,
            ),
            ValidationConfig(),
        )
        channels.append(Channel(name=stream.name, streams=[stream]))
    return channels, state


def test_upstream_report_names_the_dead_sources(tmp_path):
    """死上游不让这一轮失败，所以必须被点名报出来。

    只记 logger.warning 的后果是实测过的：11 个源里死了 3 个，产出与质量门禁全部正常。
    """
    report_path = tmp_path / "upstream_report.json"
    report = write_upstream_report(
        report_path,
        configured=["https://a.example/x.m3u", "https://b.example/y.m3u", "http://dead/z.m3u"],
        fetched={"https://a.example/x.m3u", "https://b.example/y.m3u"},
    )
    assert report == {"total": 3, "ok": 2, "failed": ["http://dead/z.m3u"]}
    assert json.loads(report_path.read_text(encoding="utf-8")) == report

    summary = (tmp_path / "upstream_report.md").read_text(encoding="utf-8")
    assert "2/3" in summary
    assert "http://dead/z.m3u" in summary, "死上游必须在摘要里点名，只报个数等于还要去翻日志"


def test_upstream_report_is_written_even_when_all_healthy(tmp_path):
    """两份都必须无条件落盘：workflow 那步在文件缺失时静默跳过，缺文件等于悄悄失去这层观测。"""
    report_path = tmp_path / "upstream_report.json"
    report = write_upstream_report(
        report_path,
        configured=["https://a.example/x.m3u"],
        fetched={"https://a.example/x.m3u"},
    )
    assert report == {"total": 1, "ok": 1, "failed": []}
    assert report_path.exists()
    summary = (tmp_path / "upstream_report.md").read_text(encoding="utf-8")
    assert summary.strip() == "### 上游存活：1/1"


def test_upstream_summary_renders_valid_markdown_list():
    """摘要贴进 Step Summary 前后都不许缺行尾换行，否则会和相邻小节粘成一行。"""
    summary = render_upstream_summary({"total": 5, "ok": 3, "failed": ["http://a/1", "http://b/2"]})
    assert summary.endswith("\n")
    lines = summary.splitlines()
    assert lines[0].startswith("### ")
    assert lines[-2:] == ["- `http://a/1`", "- `http://b/2`"]


def test_quality_gate_accepts_healthy_first_generation(tmp_path):
    stable, state = _stable_channels(3)
    _enforce_quality_gate(
        stable,
        state,
        _config(minimum_stable_channels=3),
        tmp_path / "missing-meta.json",
    )


def test_quality_gate_warns_but_publishes_when_grace_ratio_high(tmp_path, caplog):
    import logging

    from iptv_pipeline.state import TIER_GRACE

    stable, state = _stable_channels(9)
    grace_stream = Stream(
        url="https://grace.example/live.m3u8",
        name="Grace Channel",
        raw_name="Grace Channel",
    )
    state.entries[grace_stream.state_key()] = {
        "tier": TIER_GRACE,
        "grace_rounds": 1,
        "last_deep_ok": 1000.0,
        "deep_successes": 1,
    }
    stable.append(Channel(name=grace_stream.name, streams=[grace_stream]))

    # 1/10 = 10% 不触发；再加一条 GRACE 变成 2/11 ≈ 18%
    grace_stream_2 = Stream(
        url="https://grace2.example/live.m3u8",
        name="Grace Channel 2",
        raw_name="Grace Channel 2",
    )
    state.entries[grace_stream_2.state_key()] = {
        "tier": TIER_GRACE,
        "grace_rounds": 1,
        "last_deep_ok": 1000.0,
        "deep_successes": 1,
    }
    stable.append(Channel(name=grace_stream_2.name, streams=[grace_stream_2]))

    with caplog.at_level(logging.WARNING):
        _enforce_quality_gate(
            stable,
            state,
            _config(minimum_stable_channels=1),
            tmp_path / "missing-meta.json",
        )

    assert any("GRACE 占比偏高但仍继续发布" in record.message for record in caplog.records)


def test_quality_gate_rejects_large_regression(tmp_path):
    stable, state = _stable_channels(7)
    previous_meta = tmp_path / "meta.json"
    previous_meta.write_text(
        json.dumps(
            {
                "quality_scope": VALIDATION_SCOPE,
                "stats": {"channels_stable": 10},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="质量门禁失败"):
        _enforce_quality_gate(
            stable,
            state,
            _config(minimum_stable_channels=1, maximum_drop_ratio=0.25),
            previous_meta,
        )


def test_quality_gate_warns_but_publishes_when_backup_channels_drop(tmp_path, caplog):
    """双线路掉 25% 不得卡死整轮发布。

    2026-08-30 起连续红叉：总台数还在（甚至更多），只是双线路从 105 掉到 56。
    硬拦后 output 基线不再更新，定时任务就对着同一份 105 每 6 小时再失败一次。
    """
    import logging

    stable, state = _stable_channels(20)
    for channel in stable[:8]:
        extra = Stream(
            url=f"{channel.streams[0].url}?backup=1",
            name=channel.name,
            raw_name=channel.name,
        )
        state.apply_deep_result(
            extra.state_key(),
            DeepProbeResult(
                DeepProbeStatus.PASS,
                "decoded",
                checked_at=1000.0,
                decoded_frames=10,
            ),
            ValidationConfig(),
        )
        channel.streams.append(extra)

    previous_meta = tmp_path / "meta.json"
    previous_meta.write_text(
        json.dumps(
            {
                "quality_scope": VALIDATION_SCOPE,
                "stats": {
                    "channels_stable": 20,
                    "streams_stable": 30,
                    "channels_with_backup": 16,
                },
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        _enforce_quality_gate(
            stable,
            state,
            _config(minimum_stable_channels=1, maximum_drop_ratio=0.25),
            previous_meta,
        )

    assert any("双线路频道从 16 降至 8" in record.message for record in caplog.records)


def test_quality_gate_rejects_route_count_regression(tmp_path):
    stable, state = _stable_channels(10)
    previous_meta = tmp_path / "meta.json"
    previous_meta.write_text(
        json.dumps(
            {
                "quality_scope": VALIDATION_SCOPE,
                "stats": {
                    "channels_stable": 10,
                    "streams_stable": 20,
                    "channels_with_backup": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="stable 线路"):
        _enforce_quality_gate(
            stable,
            state,
            _config(minimum_stable_channels=1, maximum_drop_ratio=0.25),
            previous_meta,
        )


def test_quality_scope_migration_requires_explicit_approval(tmp_path):
    stable, state = _stable_channels(3)
    previous_meta = tmp_path / "meta.json"
    previous_meta.write_text(
        json.dumps(
            {
                "quality_scope": "ffmpeg-only-v1",
                "stats": {
                    "channels_stable": 10,
                    "streams_stable": 10,
                    "channels_with_backup": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="验证范围"):
        _enforce_quality_gate(
            stable,
            state,
            _config(minimum_stable_channels=1, maximum_drop_ratio=0.25),
            previous_meta,
        )

    _enforce_quality_gate(
        stable,
        state,
        _config(minimum_stable_channels=1, maximum_drop_ratio=0.25),
        previous_meta,
        approve_quality_scope_migration=True,
    )


def test_stable_baseline_reset_skips_drop_ratio(tmp_path):
    stable, state = _stable_channels(3)
    previous_meta = tmp_path / "meta.json"
    previous_meta.write_text(
        json.dumps(
            {
                "quality_scope": VALIDATION_SCOPE,
                "stats": {
                    "channels_stable": 100,
                    "streams_stable": 120,
                    "channels_with_backup": 40,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="stable 频道从"):
        _enforce_quality_gate(
            stable,
            state,
            _config(minimum_stable_channels=1, maximum_drop_ratio=0.25),
            previous_meta,
        )

    _enforce_quality_gate(
        stable,
        state,
        _config(minimum_stable_channels=1, maximum_drop_ratio=0.25),
        previous_meta,
        approve_stable_baseline_reset=True,
    )


def test_deep_result_shards_require_complete_indexes_and_correct_ownership(
    tmp_path,
):
    result = DeepProbeResult(
        DeepProbeStatus.PASS,
        "decoded",
        checked_at=1000.0,
        decoded_frames=10,
    )
    shard_zero_key = "00000000" + "0" * 56
    shard_one_key = "00000001" + "0" * 56
    shard_zero = tmp_path / "deep-results-0.json"
    shard_one = tmp_path / "deep-results-1.json"
    write_deep_results(
        shard_zero,
        generation="g1",
        shard_index=0,
        shard_count=2,
        results={shard_zero_key: result},
    )
    write_deep_results(
        shard_one,
        generation="g1",
        shard_index=1,
        shard_count=2,
        results={shard_one_key: result},
    )

    merged = _load_deep_result_shards(
        [shard_zero, shard_one],
        "g1",
        expected_shard_count=2,
    )
    assert set(merged) == {shard_zero_key, shard_one_key}

    with pytest.raises(RuntimeError, match="分片集合不完整"):
        _load_deep_result_shards(
            [shard_zero],
            "g1",
            expected_shard_count=2,
        )

    write_deep_results(
        shard_zero,
        generation="g1",
        shard_index=0,
        shard_count=2,
        results={shard_one_key: result},
    )
    with pytest.raises(RuntimeError, match="归属错误"):
        _load_deep_result_shards(
            [shard_zero, shard_one],
            "g1",
            expected_shard_count=2,
        )


def test_deep_result_shards_reject_duplicate_index_and_wrong_count(tmp_path):
    result_path = tmp_path / "deep-results-0.json"
    write_deep_results(
        result_path,
        generation="g1",
        shard_index=0,
        shard_count=1,
        results={},
    )

    with pytest.raises(RuntimeError, match="分片总数不一致"):
        _load_deep_result_shards(
            [result_path],
            "g1",
            expected_shard_count=2,
        )

    duplicate_dir = tmp_path / "duplicate"
    duplicate_path = duplicate_dir / "deep-results-0.json"
    write_deep_results(
        duplicate_path,
        generation="g1",
        shard_index=0,
        shard_count=1,
        results={},
    )
    with pytest.raises(RuntimeError, match="index 重复"):
        _load_deep_result_shards(
            [result_path, duplicate_path],
            "g1",
            expected_shard_count=1,
        )


def test_previous_generation_and_hashes_must_match(tmp_path):
    stable, state = _stable_channels(1)
    output = tmp_path / "output"
    write_outputs(
        stable,
        stable,
        state,
        output,
        cfg=_config(),
        generation="g1",
        network_vantage="test",
    )

    loaded = _load_previous_state(
        output / ".state" / "health.json",
        output / "meta.json",
        output / "manifest.json",
        has_previous=True,
    )
    assert loaded.generation == "g1"

    (output / "meta.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="generation"):
        _load_previous_state(
            output / ".state" / "health.json",
            output / "meta.json",
            output / "manifest.json",
            has_previous=True,
        )
