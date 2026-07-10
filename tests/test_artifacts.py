from iptv_pipeline.artifacts import (
    read_candidate_bundle,
    read_deep_results,
    write_candidate_bundle,
    write_deep_results,
)
from iptv_pipeline.deep_probe import DeepProbeResult, DeepProbeStatus
from iptv_pipeline.models import Channel, Stream
from iptv_pipeline.probe import ProbeResult


def test_candidate_and_deep_result_artifacts_roundtrip(tmp_path):
    stream = Stream(
        url="https://media.example/live.m3u8",
        name="Demo",
        raw_name="Demo HD",
        source="source-a",
        headers={"Referer": "https://example.com/"},
    )
    bundle = tmp_path / "candidate.json"
    write_candidate_bundle(
        bundle,
        generation="g1",
        channels=[Channel(name="Demo", streams=[stream])],
        fast_results={stream.state_key(): ProbeResult.OK},
    )

    generation, channels, fast = read_candidate_bundle(bundle)
    restored = channels[0].streams[0]
    assert generation == "g1"
    assert restored.headers == stream.headers
    assert restored.sources == ["source-a"]
    assert fast[restored.state_key()] == ProbeResult.OK

    result_path = tmp_path / "deep-results-0.json"
    write_deep_results(
        result_path,
        generation="g1",
        shard_index=0,
        shard_count=1,
        results={
            stream.state_key(): DeepProbeResult(
                DeepProbeStatus.PASS,
                "decoded",
                checked_at=1000.0,
                decoded_frames=12,
            )
        },
    )
    shard = read_deep_results(result_path, "g1")
    assert shard.shard_index == 0
    assert shard.shard_count == 1
    assert shard.results[stream.state_key()].status == DeepProbeStatus.PASS
