from iptv_pipeline.config import ValidationConfig
from iptv_pipeline.deep_probe import DeepProbeResult, DeepProbeStatus
from iptv_pipeline.models import Channel, Stream
from iptv_pipeline.rank import build_stable_channels
from iptv_pipeline.state import HealthState


def _pass(
    state: HealthState,
    stream: Stream,
    *,
    latency_ms: int,
    checked_at: float = 1000.0,
) -> None:
    state.apply_deep_result(
        stream.state_key(),
        DeepProbeResult(
            DeepProbeStatus.PASS,
            "decoded",
            checked_at=checked_at,
            latency_ms=latency_ms,
            decoded_frames=10,
        ),
        ValidationConfig(),
    )


def test_stable_keeps_only_eligible_best_routes():
    fast = Stream(
        url="https://fast.example/live.m3u8",
        name="CCTV-1",
        raw_name="CCTV-1",
        source="source-a",
    )
    slow = Stream(
        url="https://slow.example/live.m3u8",
        name="CCTV-1",
        raw_name="CCTV-1",
        source="source-a",
    )
    backup = Stream(
        url="https://backup.example/live.m3u8",
        name="CCTV-1",
        raw_name="CCTV-1",
        source="source-a",
    )
    rejected = Stream(
        url="https://bad.example/live.m3u8",
        name="CCTV-1",
        raw_name="CCTV-1",
    )
    state = HealthState()
    _pass(state, slow, latency_ms=5000)
    _pass(state, fast, latency_ms=500)
    _pass(state, backup, latency_ms=2500)
    state.apply_deep_result(
        rejected.state_key(),
        DeepProbeResult(
            DeepProbeStatus.HARD_FAIL,
            "decode_failed",
            checked_at=1000.0,
        ),
        ValidationConfig(),
    )

    stable = build_stable_channels(
        [Channel(name="CCTV-1", group="央视", streams=[slow, rejected, backup, fast])],
        state,
        max_streams_per_channel=2,
    )

    assert len(stable) == 1
    assert [stream.url for stream in stable[0].streams] == [
        fast.url,
        backup.url,
    ]


def test_channel_without_pass_or_grace_is_not_visible():
    stream = Stream(
        url="https://unknown.example/live.m3u8",
        name="Unknown",
        raw_name="Unknown",
    )
    stable = build_stable_channels(
        [Channel(name="Unknown", streams=[stream])],
        HealthState(),
        max_streams_per_channel=2,
    )
    assert stable == []
