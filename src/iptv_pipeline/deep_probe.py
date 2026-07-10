"""FFprobe 元数据、FFmpeg 短时解码与 GStreamer 兼容门禁。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from .config import ValidationConfig
from .models import Stream
from .safety import sanitize_headers, supports_deep_probe

logger = logging.getLogger(__name__)

_DEFAULT_UA = "okhttp/3.12.0"
_ALLOWED_PROTOCOLS = "http,https,tcp,tls,crypto"
_MIN_DECODED_FRAMES = 2
_FRAME_RE = re.compile(r"^frame=(\d+)$", re.MULTILINE)
_GSTREAMER_VIDEO_RE = re.compile(
    r"^\s*video(?:\s+#\d+)?\s*:",
    re.IGNORECASE | re.MULTILINE,
)


class DeepProbeStatus(str, Enum):
    PASS = "pass"
    SOFT_FAIL = "soft_fail"
    HARD_FAIL = "hard_fail"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class DeepProbeResult:
    status: DeepProbeStatus
    reason: str
    checked_at: float
    latency_ms: int = 0
    codec: str = ""
    width: int = 0
    height: int = 0
    duration_seconds: float | None = None
    decoded_frames: int = 0
    freeze_detected: bool = False
    gstreamer_compatible: bool | None = None
    gstreamer_reason: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> DeepProbeResult:
        return cls(
            status=DeepProbeStatus(data["status"]),
            reason=str(data.get("reason", "")),
            checked_at=float(data.get("checked_at", 0.0)),
            latency_ms=int(data.get("latency_ms", 0)),
            codec=str(data.get("codec", "")),
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            duration_seconds=(
                None if data.get("duration_seconds") is None else float(data["duration_seconds"])
            ),
            decoded_frames=int(data.get("decoded_frames", 0)),
            freeze_detected=bool(data.get("freeze_detected", False)),
            gstreamer_compatible=(
                None
                if data.get("gstreamer_compatible") is None
                else bool(data["gstreamer_compatible"])
            ),
            gstreamer_reason=str(data.get("gstreamer_reason", "")),
        )


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _input_options(stream: Stream, timeout_seconds: int) -> list[str]:
    headers = sanitize_headers(stream.headers)
    user_agent = headers.pop("User-Agent", _DEFAULT_UA)
    options = [
        "-rw_timeout",
        str(timeout_seconds * 1_000_000),
        "-protocol_whitelist",
        _ALLOWED_PROTOCOLS,
        "-user_agent",
        user_agent,
    ]
    if headers:
        serialized = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        options.extend(["-headers", serialized])
    return options


async def _run_process(command: list[str], timeout_seconds: int) -> _ProcessResult:
    process_options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_options,
        )
    except FileNotFoundError:
        return _ProcessResult(127, "", "binary_not_found")

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        await _terminate_process_tree(process)
        return _ProcessResult(-1, "", "timeout", timed_out=True)
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_process_tree(process))
        raise

    return _ProcessResult(
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """有界终止媒体工具及其子进程，避免超时后卡在 pipe 清理。"""
    tree_killed = False
    if os.name == "nt":
        killer: asyncio.subprocess.Process | None = None
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(killer.communicate(), timeout=5)
            except TimeoutError:
                killer.kill()
                await asyncio.wait_for(killer.communicate(), timeout=2)
            tree_killed = killer.returncode == 0
        except (OSError, TimeoutError):
            if killer is not None and killer.returncode is None:
                try:
                    killer.kill()
                    await asyncio.wait_for(killer.communicate(), timeout=2)
                except (ProcessLookupError, TimeoutError):
                    pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            tree_killed = True
        except ProcessLookupError:
            tree_killed = process.returncode is not None

    if not tree_killed and process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass

    try:
        await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError:
        logger.warning("媒体探测进程未在 kill 后及时退出: pid=%d", process.pid)


def _failure_status(stderr: str) -> tuple[DeepProbeStatus, str]:
    text = stderr.lower()
    soft_markers = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "temporary failure in name resolution",
        "resource temporarily unavailable",
        "service unavailable",
        "connection reset",
        "network is unreachable",
        "name or service not known",
        "no address associated",
        "connection refused",
        "server returned 429",
        "too many requests",
        "server returned 5",
        "http error 5",
        "i/o error",
    )
    if any(marker in text for marker in soft_markers):
        return DeepProbeStatus.SOFT_FAIL, "network_timeout"
    hard_markers = (
        "403 forbidden",
        "401 unauthorized",
        "404 not found",
        "410 gone",
        "400 bad request",
        "server returned 4",
        "invalid data found",
        "protocol not found",
        "unsupported codec",
        "decoder not found",
        "missing plugin",
        "no suitable plugins",
    )
    if any(marker in text for marker in hard_markers):
        return DeepProbeStatus.HARD_FAIL, "media_or_http_error"
    return DeepProbeStatus.SOFT_FAIL, "transient_or_unknown_failure"


def _gstreamer_failure_status(output: str) -> tuple[DeepProbeStatus, str]:
    """FFmpeg 已通过后，GStreamer 未知失败默认视为兼容性硬失败。"""
    text = output.lower()
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
        return DeepProbeStatus.SOFT_FAIL, "network_timeout"
    return DeepProbeStatus.HARD_FAIL, "incompatible_or_media_error"


def _parse_probe_json(stdout: str) -> tuple[dict, str | None]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {}, "invalid_probe_json"
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return {}, "missing_streams"
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    if not videos:
        return {}, "no_video_stream"

    video = videos[0]
    format_info = payload.get("format")
    if not isinstance(format_info, dict):
        format_info = {}
    duration: float | None = None
    try:
        raw_duration = format_info.get("duration")
        if raw_duration not in (None, "N/A"):
            duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = None

    return {
        "codec": str(video.get("codec_name") or ""),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "duration_seconds": duration,
        "format_name": str(format_info.get("format_name") or ""),
    }, None


def _is_finite_vod(stream: Stream, metadata: dict) -> bool:
    duration = metadata.get("duration_seconds")
    if stream.url.lower().split("?", 1)[0].endswith(".mp4"):
        return True
    return isinstance(duration, (int, float)) and 0 < duration < 12 * 60 * 60


async def _probe_gstreamer(
    stream: Stream,
    config: ValidationConfig,
) -> tuple[DeepProbeStatus, str, bool | None]:
    if not config.require_gstreamer:
        return DeepProbeStatus.PASS, "gstreamer_disabled", None
    if sanitize_headers(stream.headers):
        # gst-discoverer CLI 无法安全注入 HLS 子请求头；App playbin 会注入，避免假阴性。
        return DeepProbeStatus.PASS, "gstreamer_skipped_custom_headers", None

    gstreamer_binary = _find_gstreamer_discoverer()
    if gstreamer_binary is None:
        return DeepProbeStatus.UNSUPPORTED, "gstreamer_missing", None
    command = [
        gstreamer_binary,
        "--verbose",
        f"--timeout={config.gstreamer_timeout_seconds}",
        stream.url,
    ]
    result = await _run_process(command, config.gstreamer_timeout_seconds + 5)
    if result.timed_out:
        return DeepProbeStatus.SOFT_FAIL, "gstreamer_timeout", False
    if result.returncode == 127:
        return DeepProbeStatus.UNSUPPORTED, "gstreamer_missing", None
    combined = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        status, reason = _gstreamer_failure_status(combined)
        return status, f"gstreamer_{reason}", False
    lowered = combined.lower()
    if "missing plugin" in lowered or "missing-plugin" in lowered:
        return DeepProbeStatus.HARD_FAIL, "gstreamer_missing_plugin", False
    if _GSTREAMER_VIDEO_RE.search(combined) is None:
        return DeepProbeStatus.HARD_FAIL, "gstreamer_no_video", False
    return DeepProbeStatus.PASS, "gstreamer_discovered", True


def _find_gstreamer_discoverer() -> str | None:
    discovered = shutil.which("gst-discoverer-1.0")
    if discovered:
        return discovered
    root = os.environ.get("GSTREAMER_1_0_ROOT_MSVC_X86_64")
    if not root:
        return None
    candidate = Path(root) / "bin" / "gst-discoverer-1.0.exe"
    return str(candidate) if candidate.is_file() else None


async def probe_stream(stream: Stream, config: ValidationConfig) -> DeepProbeResult:
    checked_at = time.time()
    started = time.monotonic()
    if not supports_deep_probe(stream.url) or stream.is_ipv6:
        return DeepProbeResult(
            DeepProbeStatus.UNSUPPORTED,
            "unsupported_protocol_or_ipv6",
            checked_at,
        )

    input_options = _input_options(stream, config.deep_timeout_seconds)
    ffprobe_command = [
        "ffprobe",
        "-v",
        "error",
        *input_options,
        "-show_entries",
        "stream=codec_type,codec_name,width,height:format=format_name,duration",
        "-of",
        "json",
        stream.url,
    ]
    probe = await _run_process(ffprobe_command, config.deep_timeout_seconds)
    if probe.timed_out:
        return DeepProbeResult(
            DeepProbeStatus.SOFT_FAIL,
            "ffprobe_timeout",
            checked_at,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    if probe.returncode == 127:
        return DeepProbeResult(
            DeepProbeStatus.UNSUPPORTED,
            "ffprobe_missing",
            checked_at,
        )
    if probe.returncode != 0:
        status, reason = _failure_status(probe.stderr)
        return DeepProbeResult(
            status,
            reason,
            checked_at,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    metadata, parse_error = _parse_probe_json(probe.stdout)
    if parse_error:
        return DeepProbeResult(
            DeepProbeStatus.HARD_FAIL,
            parse_error,
            checked_at,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    if _is_finite_vod(stream, metadata):
        return DeepProbeResult(
            DeepProbeStatus.HARD_FAIL,
            "finite_vod",
            checked_at,
            latency_ms=int((time.monotonic() - started) * 1000),
            codec=metadata["codec"],
            width=metadata["width"],
            height=metadata["height"],
            duration_seconds=metadata["duration_seconds"],
        )

    ffmpeg_command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "info",
        *input_options,
        "-i",
        stream.url,
        "-map",
        "0:v:0",
        "-an",
        "-t",
        str(config.decode_seconds),
        "-vf",
        "freezedetect=n=-60dB:d=3",
        "-progress",
        "pipe:1",
        "-f",
        "null",
        "-",
    ]
    decode = await _run_process(ffmpeg_command, config.deep_timeout_seconds)
    latency_ms = int((time.monotonic() - started) * 1000)
    if decode.timed_out:
        return DeepProbeResult(
            DeepProbeStatus.SOFT_FAIL,
            "ffmpeg_timeout",
            checked_at,
            latency_ms=latency_ms,
            codec=metadata["codec"],
            width=metadata["width"],
            height=metadata["height"],
            duration_seconds=metadata["duration_seconds"],
        )
    if decode.returncode == 127:
        return DeepProbeResult(
            DeepProbeStatus.UNSUPPORTED,
            "ffmpeg_missing",
            checked_at,
        )
    if decode.returncode != 0:
        status, reason = _failure_status(decode.stderr)
        return DeepProbeResult(
            status,
            reason,
            checked_at,
            latency_ms=latency_ms,
            codec=metadata["codec"],
            width=metadata["width"],
            height=metadata["height"],
            duration_seconds=metadata["duration_seconds"],
        )

    frames = max((int(value) for value in _FRAME_RE.findall(decode.stdout)), default=0)
    if frames < _MIN_DECODED_FRAMES:
        return DeepProbeResult(
            DeepProbeStatus.HARD_FAIL,
            "insufficient_video_frames",
            checked_at,
            latency_ms=latency_ms,
            codec=metadata["codec"],
            width=metadata["width"],
            height=metadata["height"],
            duration_seconds=metadata["duration_seconds"],
            decoded_frames=frames,
        )
    gstreamer_status, gstreamer_reason, gstreamer_compatible = await _probe_gstreamer(
        stream, config
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    if gstreamer_status != DeepProbeStatus.PASS:
        return DeepProbeResult(
            gstreamer_status,
            gstreamer_reason,
            checked_at,
            latency_ms=latency_ms,
            codec=metadata["codec"],
            width=metadata["width"],
            height=metadata["height"],
            duration_seconds=metadata["duration_seconds"],
            decoded_frames=frames,
            freeze_detected="freeze_start" in decode.stderr.lower(),
            gstreamer_compatible=gstreamer_compatible,
            gstreamer_reason=gstreamer_reason,
        )
    return DeepProbeResult(
        DeepProbeStatus.PASS,
        (
            "decoded_and_gstreamer_checked"
            if gstreamer_compatible is True
            else "decoded_gstreamer_not_applicable"
        ),
        checked_at,
        latency_ms=latency_ms,
        codec=metadata["codec"],
        width=metadata["width"],
        height=metadata["height"],
        duration_seconds=metadata["duration_seconds"],
        decoded_frames=frames,
        freeze_detected="freeze_start" in decode.stderr.lower(),
        gstreamer_compatible=gstreamer_compatible,
        gstreamer_reason=gstreamer_reason,
    )


async def probe_all_deep(
    streams: list[Stream],
    config: ValidationConfig,
) -> dict[str, DeepProbeResult]:
    """有界并发深验，返回 ``state_key -> result``。"""
    semaphore = asyncio.Semaphore(config.deep_concurrency)
    completed = 0
    total = len(streams)

    async def guarded(stream: Stream) -> tuple[str, DeepProbeResult]:
        nonlocal completed
        async with semaphore:
            result = await probe_stream(stream, config)
        completed += 1
        if completed % 100 == 0 or completed == total:
            logger.info("深度验证进度: %d/%d", completed, total)
        return stream.state_key(), result

    pairs = await asyncio.gather(*(guarded(stream) for stream in streams))
    return dict(pairs)
