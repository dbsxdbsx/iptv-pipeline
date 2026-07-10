import json

from iptv_pipeline.config import ValidationConfig
from iptv_pipeline.deep_probe import DeepProbeResult, DeepProbeStatus
from iptv_pipeline.emit import to_m3u, to_meta_json
from iptv_pipeline.models import Channel, Stream
from iptv_pipeline.parse import parse_m3u
from iptv_pipeline.state import HealthState


def test_m3u_roundtrip_preserves_safe_headers():
    channels = [
        Channel(
            name="Header Channel",
            group="国际",
            streams=[
                Stream(
                    url="https://media.example/live.m3u8",
                    name="Header Channel",
                    raw_name="Header Channel",
                    headers={
                        "User-Agent": "Player/1.0",
                        "Referer": "https://example.com/",
                        "Cookie": "must-not-leak=1",
                    },
                )
            ],
        )
    ]

    output = to_m3u(channels)
    parsed = parse_m3u(output, source="roundtrip")

    assert "Cookie" not in output
    assert parsed[0].headers == {
        "User-Agent": "Player/1.0",
        "Referer": "https://example.com/",
    }


def test_m3u_attributes_cannot_inject_lines():
    channels = [
        Channel(
            name='Bad"\n#EXTINF:-1,Injected',
            group="Other",
            streams=[
                Stream(
                    url="https://media.example/live.m3u8",
                    name="Bad",
                    raw_name="Bad",
                )
            ],
        )
    ]

    output = to_m3u(channels)
    assert sum(line.startswith("#EXTINF:") for line in output.splitlines()) == 1


def test_meta_generation_matches_stable_rank_and_omits_credentials():
    stream = Stream(
        url="https://media.example/live.m3u8?token=public-upstream-token",
        name="Demo",
        raw_name="Demo HD",
        source="source-a",
        headers={"User-Agent": "Player/1.0", "Cookie": "secret=1"},
    )
    channel = Channel(name="Demo", group="国际", streams=[stream])
    state = HealthState()
    state.apply_deep_result(
        stream.state_key(),
        DeepProbeResult(
            DeepProbeStatus.PASS,
            "decoded",
            checked_at=1000.0,
            latency_ms=500,
            decoded_frames=12,
        ),
        ValidationConfig(),
    )

    payload = json.loads(
        to_meta_json(
            [channel],
            [channel],
            state,
            generation="generation-1",
            network_vantage="test",
        )
    )

    assert payload["generation"] == "generation-1"
    assert payload["stats"]["channels_stable"] == 1
    assert payload["streams"][0]["rank_in_channel"] == 1
    assert payload["streams"][0]["headers"] == {"User-Agent": "Player/1.0"}
