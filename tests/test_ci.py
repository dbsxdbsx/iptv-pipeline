import json

import pytest

from iptv_pipeline.artifacts import write_deep_results
from iptv_pipeline.ci import (
    _enforce_quality_gate,
    _load_deep_result_shards,
    _load_previous_state,
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


def test_quality_gate_accepts_healthy_first_generation(tmp_path):
    stable, state = _stable_channels(3)
    _enforce_quality_gate(
        stable,
        state,
        _config(minimum_stable_channels=3),
        tmp_path / "missing-meta.json",
    )


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
