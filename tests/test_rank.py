from iptv_pipeline.config import ValidationConfig
from iptv_pipeline.deep_probe import DeepProbeResult, DeepProbeStatus
from iptv_pipeline.models import Channel, Stream
from iptv_pipeline.rank import build_stable_channels, select_diverse_streams
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


def test_stable_keeps_up_to_five_diverse_hosts():
    streams = [
        Stream(
            url=f"https://host{index}.example/live.m3u8",
            name="CCTV-5",
            raw_name="CCTV-5",
            source="source-a",
        )
        for index in range(6)
    ]
    # 同 host 的更差线路，不应挤掉其它 host
    same_host_slow = Stream(
        url="https://host0.example/backup.m3u8",
        name="CCTV-5",
        raw_name="CCTV-5",
        source="source-b",
    )
    state = HealthState()
    for index, stream in enumerate(streams):
        _pass(state, stream, latency_ms=100 + index * 100)
    _pass(state, same_host_slow, latency_ms=9000)

    stable = build_stable_channels(
        [Channel(name="CCTV-5", group="央视", streams=[*streams, same_host_slow])],
        state,
        max_streams_per_channel=5,
    )

    assert len(stable) == 1
    kept_urls = [stream.url for stream in stable[0].streams]
    assert len(kept_urls) == 5
    assert same_host_slow.url not in kept_urls
    assert streams[0].url in kept_urls
    assert streams[4].url in kept_urls
    assert streams[5].url not in kept_urls


def test_select_diverse_fills_same_host_when_needed():
    a1 = Stream(url="https://a.example/1.m3u8", name="Ch", raw_name="Ch")
    a2 = Stream(url="https://a.example/2.m3u8", name="Ch", raw_name="Ch")
    b1 = Stream(url="https://b.example/1.m3u8", name="Ch", raw_name="Ch")
    kept = select_diverse_streams([a1, b1, a2], max_streams_per_channel=3)
    assert kept == [a1, b1, a2]


def test_channel_without_pass_or_grace_is_not_visible():
    stream = Stream(
        url="https://unknown.example/live.m3u8",
        name="Unknown",
        raw_name="Unknown",
    )
    stable = build_stable_channels(
        [Channel(name="Unknown", streams=[stream])],
        HealthState(),
        max_streams_per_channel=5,
    )
    assert stable == []
