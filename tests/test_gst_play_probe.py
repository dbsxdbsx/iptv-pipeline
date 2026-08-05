from iptv_pipeline.gst_play_probe import (
    PlaybinProbeResult,
    build_worker_command,
    choose_playbin_factory,
    parse_worker_output,
)


def test_choose_playbin_factory_prefers_playbin3_with_demux2():
    exists = {"playbin3", "hlsdemux2", "playbin"}
    assert (
        choose_playbin_factory(
            "https://cdn.example/live.m3u8",
            factory_exists=exists.__contains__,
        )
        == "playbin3"
    )


def test_choose_playbin_factory_falls_back_without_hlsdemux2():
    exists = {"playbin3", "playbin", "hlsdemux"}
    assert (
        choose_playbin_factory(
            "https://cdn.example/live.m3u8",
            factory_exists=exists.__contains__,
        )
        == "playbin"
    )


def test_choose_playbin_factory_progressive_uses_playbin():
    exists = {"playbin3", "hlsdemux2", "playbin"}
    assert (
        choose_playbin_factory(
            "https://cdn.example/live.ts",
            factory_exists=exists.__contains__,
        )
        == "playbin"
    )


def test_build_worker_command_includes_headers():
    command = build_worker_command(
        python_executable="python",
        url="https://cdn.example/a.m3u8",
        headers={"User-Agent": "Demo/1", "Referer": "https://ref.example/"},
        timeout_seconds=9,
    )
    assert command[:4] == ["python", "-m", "iptv_pipeline.gst_play_probe", "--url"]
    assert "https://cdn.example/a.m3u8" in command
    assert "--timeout" in command
    assert "9" in command
    joined = " ".join(command)
    assert "User-Agent: Demo/1" in joined
    assert "Referer: https://ref.example/" in joined


def test_parse_worker_output_pass_and_timeout():
    assert parse_worker_output(
        '{"status":"pass","reason":"gstreamer_playbin_video","factory":"playbin","video_buffers":3}',
        0,
        False,
    ) == PlaybinProbeResult("pass", "gstreamer_playbin_video", "playbin", 3)
    assert parse_worker_output("", -1, True) == PlaybinProbeResult("soft_fail", "gstreamer_timeout")


def test_parse_worker_output_uses_last_json_line():
    stdout = 'noise\n{"status":"hard_fail","reason":"gstreamer_no_video","factory":"playbin"}\n'
    parsed = parse_worker_output(stdout, 1, False)
    assert parsed.status == "hard_fail"
    assert parsed.reason == "gstreamer_no_video"
