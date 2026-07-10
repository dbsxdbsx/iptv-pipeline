import json

import pytest

from iptv_pipeline.ci import _enforce_quality_gate, _load_previous_state
from iptv_pipeline.config import Config, ValidationConfig
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
        json.dumps({"stats": {"channels_stable": 10}}),
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
                "stats": {
                    "channels_stable": 10,
                    "streams_stable": 20,
                    "channels_with_backup": 0,
                }
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
