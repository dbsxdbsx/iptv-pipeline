"""投递 manifest（schema v2 endpoints）守卫。"""

from __future__ import annotations

import json
from pathlib import Path

from iptv_pipeline.config import (
    VALIDATION_SCOPE,
    Config,
    DeliveryConfig,
    ValidationConfig,
)
from iptv_pipeline.deep_probe import DeepProbeResult, DeepProbeStatus
from iptv_pipeline.models import Channel, Stream
from iptv_pipeline.pipeline import write_outputs
from iptv_pipeline.state import HealthState

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def test_delivery_json_loads_ordered_endpoints():
    cfg = Config.load(CONFIG_DIR)
    assert cfg.delivery.playlist_endpoints
    assert cfg.delivery.manifest_endpoints
    assert cfg.delivery.playlist_endpoints[0].endswith("stable.m3u")
    assert cfg.delivery.manifest_endpoints[0].endswith("manifest.json")
    # jsDelivr 必须作为第二镜像，GitHub raw 被墙时才能回退
    assert any("jsdelivr" in u for u in cfg.delivery.playlist_endpoints)


def test_write_outputs_embeds_delivery_endpoints(tmp_path):
    stream = Stream(
        url="https://media.example/live.m3u8",
        name="Demo",
        raw_name="Demo",
    )
    state = HealthState()
    state.ensure_validation_scope(VALIDATION_SCOPE)
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
    channels = [Channel(name="Demo", streams=[stream])]
    cfg = Config(
        upstreams=[],
        alias_to_canonical={},
        canonical_names=[],
        blacklist=[],
        group_rules=[],
        default_group="其他",
        delivery=DeliveryConfig(
            playlist_endpoints=[
                "https://cdn.example/stable.m3u",
                "https://raw.githubusercontent.com/dbsxdbsx/iptv-pipeline/output/stable.m3u",
            ],
            manifest_endpoints=[
                "https://cdn.example/manifest.json",
            ],
        ),
    )

    write_outputs(
        channels,
        channels,
        state,
        tmp_path / "output",
        cfg=cfg,
        generation="gen-delivery",
        network_vantage="test",
    )

    manifest = json.loads((tmp_path / "output" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["generation"] == "gen-delivery"
    assert manifest["endpoints"][0] == "https://cdn.example/stable.m3u"
    assert manifest["manifest_endpoints"] == ["https://cdn.example/manifest.json"]
    assert "stable.m3u" in manifest["files"]


def test_write_outputs_defaults_raw_when_delivery_empty(tmp_path):
    stream = Stream(
        url="https://media.example/live.m3u8",
        name="Demo",
        raw_name="Demo",
    )
    state = HealthState()
    state.ensure_validation_scope(VALIDATION_SCOPE)
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
    channels = [Channel(name="Demo", streams=[stream])]
    cfg = Config(
        upstreams=[],
        alias_to_canonical={},
        canonical_names=[],
        blacklist=[],
        group_rules=[],
        default_group="其他",
    )

    write_outputs(
        channels,
        channels,
        state,
        tmp_path / "output",
        cfg=cfg,
        generation="g-default",
        network_vantage="test",
    )
    manifest = json.loads((tmp_path / "output" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["endpoints"] == [
        "https://raw.githubusercontent.com/dbsxdbsx/iptv-pipeline/output/stable.m3u",
    ]
