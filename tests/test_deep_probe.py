import asyncio

from iptv_pipeline import deep_probe
from iptv_pipeline.config import ValidationConfig
from iptv_pipeline.deep_probe import DeepProbeStatus, probe_stream
from iptv_pipeline.models import Stream


def _config() -> ValidationConfig:
    return ValidationConfig(deep_timeout_seconds=10, decode_seconds=3)


def test_deep_probe_requires_metadata_and_decoded_frames(monkeypatch):
    calls: list[list[str]] = []
    results = iter(
        [
            deep_probe._ProcessResult(
                0,
                '{"streams":[{"codec_type":"video","codec_name":"h264",'
                '"width":1280,"height":720}],"format":{"format_name":"hls"}}',
                "",
            ),
            deep_probe._ProcessResult(
                0,
                "frame=1\nframe=18\nprogress=end\n",
                "[freezedetect] freeze_start: 0",
            ),
        ]
    )

    async def fake_run(command: list[str], timeout_seconds: int):
        calls.append(command)
        assert timeout_seconds == 10
        return next(results)

    monkeypatch.setattr(deep_probe, "_run_process", fake_run)
    stream = Stream(
        url="https://media.example/live.m3u8",
        name="Demo",
        raw_name="Demo",
        headers={
            "User-Agent": "DemoPlayer/1.0",
            "Referer": "https://example.com/",
            "Cookie": "not-public",
        },
    )

    result = asyncio.run(probe_stream(stream, _config()))

    assert result.status == DeepProbeStatus.PASS
    assert result.codec == "h264"
    assert result.decoded_frames == 18
    assert result.freeze_detected
    commands = "\n".join(" ".join(command) for command in calls)
    assert "DemoPlayer/1.0" in commands
    assert "Referer: https://example.com/" in commands
    assert "not-public" not in commands


def test_deep_probe_rejects_finite_mp4_before_decode(monkeypatch):
    calls = 0

    async def fake_run(command: list[str], timeout_seconds: int):
        nonlocal calls
        calls += 1
        return deep_probe._ProcessResult(
            0,
            '{"streams":[{"codec_type":"video","codec_name":"h264"}],'
            '"format":{"format_name":"mov,mp4","duration":"120.0"}}',
            "",
        )

    monkeypatch.setattr(deep_probe, "_run_process", fake_run)
    result = asyncio.run(
        probe_stream(
            Stream(
                url="https://media.example/recording.mp4",
                name="Recording",
                raw_name="Recording",
            ),
            _config(),
        )
    )

    assert result.status == DeepProbeStatus.HARD_FAIL
    assert result.reason == "finite_vod"
    assert calls == 1


def test_deep_probe_timeout_is_soft_failure(monkeypatch):
    async def fake_run(command: list[str], timeout_seconds: int):
        return deep_probe._ProcessResult(-1, "", "timeout", timed_out=True)

    monkeypatch.setattr(deep_probe, "_run_process", fake_run)
    result = asyncio.run(
        probe_stream(
            Stream(
                url="https://media.example/live.m3u8",
                name="Timeout",
                raw_name="Timeout",
            ),
            _config(),
        )
    )

    assert result.status == DeepProbeStatus.SOFT_FAIL
    assert result.reason == "ffprobe_timeout"


def test_ipv6_is_not_admitted_without_supported_vantage():
    result = asyncio.run(
        probe_stream(
            Stream(
                url="https://[2606:4700::1111]/live.m3u8",
                name="IPv6",
                raw_name="IPv6",
                is_ipv6=True,
            ),
            _config(),
        )
    )
    assert result.status == DeepProbeStatus.UNSUPPORTED


def test_deep_probe_classifies_transient_server_errors_as_soft():
    assert deep_probe._failure_status("Server returned 503 Service Unavailable") == (
        DeepProbeStatus.SOFT_FAIL,
        "network_timeout",
    )
    assert deep_probe._failure_status("HTTP error 404 Not Found") == (
        DeepProbeStatus.HARD_FAIL,
        "media_or_http_error",
    )
    assert deep_probe._failure_status("Unclassified transport failure") == (
        DeepProbeStatus.SOFT_FAIL,
        "transient_or_unknown_failure",
    )
