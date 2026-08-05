"""GStreamer playbin 短播探针：等价于 App 的 HTTP 头注入路径。

``gst-discoverer-1.0`` CLI 无法给 HLS 分片/子 playlist 注入 UA/Referer。
本模块用 playbin3（若可用且存在 demux2）或旧 playbin，挂
``element-setup`` + ``deep-element-added`` 向 ``souphttpsrc`` 注入公开头，
再以 fakesink 收到视频缓冲作为「可发现视频」判据。

设计为**独立子进程**入口（``python -m iptv_pipeline.gst_play_probe``），
避免与 asyncio 并发深验抢同一个 GLib 主循环。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

# 与 deep_probe / App 直播默认 UA 对齐
_DEFAULT_UA = "okhttp/3.12.0"
_EXTRA_HEADER_KEYS = (
    "Referer",
    "Origin",
    "Accept",
    "Accept-Language",
    "Accept-Encoding",
)


@dataclass(frozen=True)
class PlaybinProbeResult:
    status: str  # pass | soft_fail | hard_fail | unsupported
    reason: str
    factory: str = ""
    video_buffers: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def choose_playbin_factory(url: str, *, factory_exists) -> str:
    """与 App ``gstreamer_player`` 一致：自适应流优先 playbin3+demux2，否则 playbin。"""
    path = urlsplit(url).path.lower()
    demux2: str | None = None
    if path.endswith(".m3u8"):
        demux2 = "hlsdemux2"
    elif path.endswith(".mpd"):
        demux2 = "dashdemux2"
    if demux2 and factory_exists("playbin3") and factory_exists(demux2):
        return "playbin3"
    if factory_exists("playbin"):
        return "playbin"
    if factory_exists("playbin3"):
        return "playbin3"
    return ""


def build_worker_command(
    *,
    python_executable: str,
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
) -> list[str]:
    command = [
        python_executable,
        "-m",
        "iptv_pipeline.gst_play_probe",
        "--url",
        url,
        "--timeout",
        str(timeout_seconds),
    ]
    for name, value in headers.items():
        command.extend(["--header", f"{name}: {value}"])
    return command


def parse_worker_output(stdout: str, returncode: int, timed_out: bool) -> PlaybinProbeResult:
    if timed_out:
        return PlaybinProbeResult("soft_fail", "gstreamer_timeout")
    text = stdout.strip()
    if not text:
        if returncode == 127:
            return PlaybinProbeResult("unsupported", "gstreamer_playbin_probe_unavailable")
        return PlaybinProbeResult("hard_fail", "gstreamer_playbin_empty_output")
    try:
        payload = json.loads(text.splitlines()[-1])
    except json.JSONDecodeError:
        return PlaybinProbeResult("hard_fail", "gstreamer_playbin_invalid_json")
    status = str(payload.get("status", "hard_fail"))
    reason = str(payload.get("reason", "gstreamer_playbin_unknown"))
    if status not in {"pass", "soft_fail", "hard_fail", "unsupported"}:
        status = "hard_fail"
    return PlaybinProbeResult(
        status=status,
        reason=reason,
        factory=str(payload.get("factory", "")),
        video_buffers=int(payload.get("video_buffers", 0) or 0),
    )


def _parse_header_args(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in values:
        if ":" not in item:
            continue
        name, value = item.split(":", 1)
        name = name.strip()
        value = value.strip()
        if name and value and "\r" not in value and "\n" not in value:
            headers[name] = value
    return headers


def _origin_and_referer(url: str, headers: dict[str, str]) -> tuple[str, str]:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.hostname or ''}"
    if parsed.port and parsed.port not in (80, 443):
        origin = f"{origin}:{parsed.port}"
    referer = headers.get("Referer") or f"{origin}/"
    origin_v = headers.get("Origin") or origin
    return origin_v, referer


def _classify_bus_error(message: str) -> tuple[str, str]:
    text = message.lower()
    soft_markers = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "temporary failure in name resolution",
        "resource temporarily unavailable",
        "connection reset",
        "connection refused",
        "connection aborted",
        "connection closed",
        "connection terminated",
        "could not resolve server name",
        "could not resolve host",
        "failed to connect",
        "host is unreachable",
        "network is unreachable",
        "name or service not known",
        "no address associated",
        "socket closed",
        "unexpected eof",
        "too many requests",
        "server returned 429",
        "server returned 5",
        "http error 5",
        "service unavailable",
    )
    if any(marker in text for marker in soft_markers):
        return "soft_fail", "gstreamer_network_timeout"
    hard_markers = (
        "missing plugin",
        "missing-plugin",
        "no suitable plugins",
        "not-negotiated",
        "403",
        "401",
        "404",
        "410",
        "400",
        "server returned 4",
        "http error 4",
        "forbidden",
        "unauthorized",
        "not found",
    )
    if any(marker in text for marker in hard_markers):
        return "hard_fail", "gstreamer_incompatible_or_media_error"
    return "hard_fail", "gstreamer_incompatible_or_media_error"


def run_playbin_probe(
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
) -> PlaybinProbeResult:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst
    except Exception as exc:  # noqa: BLE001 - 子进程入口需把缺依赖收敛成 JSON
        return PlaybinProbeResult(
            "unsupported",
            f"gstreamer_playbin_probe_unavailable:{type(exc).__name__}",
        )

    Gst.init(None)

    def factory_exists(name: str) -> bool:
        return Gst.ElementFactory.find(name) is not None

    factory_name = choose_playbin_factory(url, factory_exists=factory_exists)
    if not factory_name:
        return PlaybinProbeResult("unsupported", "gstreamer_playbin_missing")

    playbin = Gst.ElementFactory.make(factory_name, factory_name)
    if playbin is None:
        return PlaybinProbeResult("unsupported", "gstreamer_playbin_create_failed")

    video_sink = Gst.ElementFactory.make("fakesink", "probe-video-sink")
    audio_sink = Gst.ElementFactory.make("fakesink", "probe-audio-sink")
    if video_sink is None or audio_sink is None:
        return PlaybinProbeResult("unsupported", "gstreamer_fakesink_missing")
    video_sink.set_property("sync", False)
    audio_sink.set_property("sync", False)
    video_sink.set_property("signal-handoffs", True)

    state = {"video_buffers": 0, "error": "", "eos": False}

    def on_handoff(_element, _buffer, _pad) -> None:
        state["video_buffers"] += 1

    video_sink.connect("handoff", on_handoff)

    playbin.set_property("uri", url)
    playbin.set_property("video-sink", video_sink)
    playbin.set_property("audio-sink", audio_sink)

    effective = dict(headers)
    if "User-Agent" not in effective:
        effective["User-Agent"] = _DEFAULT_UA
    origin_v, referer = _origin_and_referer(url, effective)

    def apply_http_config(element) -> None:
        factory = element.get_factory()
        if factory is None or factory.get_name() != "souphttpsrc":
            return
        if element.find_property("user-agent") is not None:
            element.set_property("user-agent", effective["User-Agent"])
        if element.find_property("extra-headers") is None:
            return
        builder = Gst.Structure.new_empty("extra-headers")
        builder.set_value("Referer", referer)
        builder.set_value("Origin", origin_v)
        for key in _EXTRA_HEADER_KEYS:
            if key in ("Referer", "Origin"):
                continue
            if key in effective:
                builder.set_value(key, effective[key])
        element.set_property("extra-headers", builder)

    def on_element_setup(_bin, element) -> None:
        apply_http_config(element)

    def on_deep_element_added(_bin, _sub_bin, element) -> None:
        apply_http_config(element)

    playbin.connect("element-setup", on_element_setup)
    # deep-element-added：兼容 hlsdemux / hlsdemux2 分片 souphttpsrc 创建路径差异
    playbin.connect("deep-element-added", on_deep_element_added)

    loop = GLib.MainLoop()
    bus = playbin.get_bus()
    bus.add_signal_watch()

    def on_bus(_bus, message) -> None:
        mtype = message.type
        if mtype == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            state["error"] = f"{err.message}; {debug or ''}".strip()
            loop.quit()
        elif mtype == Gst.MessageType.EOS:
            state["eos"] = True
            loop.quit()
        elif mtype == Gst.MessageType.ASYNC_DONE:
            if state["video_buffers"] > 0:
                loop.quit()

    bus.connect("message", on_bus)

    def on_timeout() -> bool:
        loop.quit()
        return False

    def poll_video() -> bool:
        if state["video_buffers"] > 0:
            loop.quit()
            return False
        return True

    GLib.timeout_add_seconds(max(1, timeout_seconds), on_timeout)
    GLib.timeout_add(200, poll_video)

    ret = playbin.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        playbin.set_state(Gst.State.NULL)
        return PlaybinProbeResult(
            "hard_fail",
            "gstreamer_state_change_failure",
            factory=factory_name,
        )

    try:
        loop.run()
    finally:
        bus.remove_signal_watch()
        playbin.set_state(Gst.State.NULL)

    if state["video_buffers"] > 0:
        return PlaybinProbeResult(
            "pass",
            "gstreamer_playbin_video",
            factory=factory_name,
            video_buffers=state["video_buffers"],
        )
    if state["error"]:
        status, reason = _classify_bus_error(state["error"])
        return PlaybinProbeResult(
            status,
            reason,
            factory=factory_name,
            video_buffers=0,
        )
    if state["eos"]:
        return PlaybinProbeResult(
            "hard_fail",
            "gstreamer_no_video",
            factory=factory_name,
        )
    return PlaybinProbeResult(
        "soft_fail",
        "gstreamer_timeout",
        factory=factory_name,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GStreamer playbin short probe")
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help='HTTP header as "Name: value"',
    )
    args = parser.parse_args(argv)
    headers = _parse_header_args(args.header)
    result = run_playbin_probe(args.url, headers, args.timeout)
    sys.stdout.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
