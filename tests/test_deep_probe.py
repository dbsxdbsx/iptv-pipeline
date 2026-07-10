import asyncio
import sys
import time

import pytest

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
    assert result.gstreamer_compatible is None
    assert result.reason == "decoded_gstreamer_not_applicable"
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


def test_gstreamer_is_required_for_headerless_streams(monkeypatch):
    results = iter(
        [
            deep_probe._ProcessResult(
                0,
                '{"streams":[{"codec_type":"video","codec_name":"h264"}],'
                '"format":{"format_name":"hls"}}',
                "",
            ),
            deep_probe._ProcessResult(
                0,
                "frame=1\nframe=12\nprogress=end\n",
                "",
            ),
            deep_probe._ProcessResult(
                0,
                "Properties:\n  container #0: application/x-hls\n",
                "",
            ),
        ]
    )

    async def fake_run(command: list[str], timeout_seconds: int):
        return next(results)

    monkeypatch.setattr(deep_probe, "_run_process", fake_run)
    monkeypatch.setattr(deep_probe, "_find_gstreamer_discoverer", lambda: "gst-discoverer-1.0")
    result = asyncio.run(
        probe_stream(
            Stream(
                url="https://media.example/live.m3u8",
                name="GStreamer Gate",
                raw_name="GStreamer Gate",
            ),
            _config(),
        )
    )

    assert result.status == DeepProbeStatus.HARD_FAIL
    assert result.reason == "gstreamer_no_video"
    assert result.gstreamer_compatible is False


def test_gstreamer_rejects_success_exit_with_missing_plugin(monkeypatch):
    async def fake_run(command: list[str], timeout_seconds: int):
        return deep_probe._ProcessResult(
            0,
            "Missing plugins:\n  H.266 decoder\n  video #1: video/x-h266",
            "",
        )

    monkeypatch.setattr(deep_probe, "_run_process", fake_run)
    monkeypatch.setattr(deep_probe, "_find_gstreamer_discoverer", lambda: "gst-discoverer-1.0")
    status, reason, compatible = asyncio.run(
        deep_probe._probe_gstreamer(
            Stream(
                url="https://media.example/live.m3u8",
                name="Missing plugin",
            ),
            _config(),
        )
    )

    assert status == DeepProbeStatus.HARD_FAIL
    assert reason == "gstreamer_missing_plugin"
    assert compatible is False


def test_gstreamer_unknown_errors_are_hard_but_network_errors_are_soft():
    assert deep_probe._gstreamer_failure_status("not-negotiated") == (
        DeepProbeStatus.HARD_FAIL,
        "incompatible_or_media_error",
    )
    assert deep_probe._gstreamer_failure_status("Server returned 503 Service Unavailable") == (
        DeepProbeStatus.SOFT_FAIL,
        "network_timeout",
    )
    for message in (
        "Connection refused",
        "Could not resolve server name",
        "Name or service not known",
        "TLS connection terminated: unexpected EOF",
    ):
        assert deep_probe._gstreamer_failure_status(message) == (
            DeepProbeStatus.SOFT_FAIL,
            "network_timeout",
        )


def test_gstreamer_video_output_matches_linux_and_windows_formats():
    assert deep_probe._GSTREAMER_VIDEO_RE.search("      video #1: H.264")
    assert deep_probe._GSTREAMER_VIDEO_RE.search("    video: video/x-vp8, width=854")
    assert deep_probe._GSTREAMER_VIDEO_RE.search("video #12: H.265")
    assert deep_probe._GSTREAMER_VIDEO_RE.search("audio #1: AAC") is None


def test_media_process_timeout_is_strictly_bounded():
    started = time.monotonic()
    result = asyncio.run(
        deep_probe._run_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout_seconds=1,
        )
    )

    assert result.timed_out
    assert time.monotonic() - started < 8


def test_media_process_cancellation_runs_bounded_cleanup():
    async def cancel_probe() -> None:
        task = asyncio.create_task(
            deep_probe._run_process(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout_seconds=30,
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    started = time.monotonic()
    asyncio.run(cancel_probe())
    assert time.monotonic() - started < 8
