from iptv_pipeline.probe import ProbeResult, _classify_payload, _classify_status


def test_fast_probe_rejects_html_and_json_error_pages():
    assert (
        _classify_payload(
            "https://media.example/live.php",
            "text/html",
            b"<!doctype html><title>upstream error</title>",
        )
        == ProbeResult.HARD_FAIL
    )
    assert (
        _classify_payload(
            "https://media.example/live.php",
            "application/json",
            b'{"error":"token expired"}',
        )
        == ProbeResult.HARD_FAIL
    )


def test_fast_probe_accepts_live_hls_and_rejects_vod_hls():
    live = b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nsegment.ts\n"
    vod = live + b"#EXT-X-ENDLIST\n"

    assert (
        _classify_payload(
            "https://media.example/live.m3u8",
            "application/vnd.apple.mpegurl",
            live,
        )
        == ProbeResult.OK
    )
    assert (
        _classify_payload(
            "https://media.example/vod.m3u8",
            "application/vnd.apple.mpegurl",
            vod,
        )
        == ProbeResult.HARD_FAIL
    )


def test_fast_probe_rejects_empty_or_invalid_m3u8():
    assert (
        _classify_payload(
            "https://media.example/live.m3u8",
            "application/octet-stream",
            b"",
        )
        == ProbeResult.SOFT_FAIL
    )
    assert (
        _classify_payload(
            "https://media.example/live.m3u8",
            "text/plain",
            b"not a playlist",
        )
        == ProbeResult.HARD_FAIL
    )


def test_transient_http_statuses_are_soft_failures():
    assert _classify_status(429) == ProbeResult.SOFT_FAIL
    assert _classify_status(503) == ProbeResult.SOFT_FAIL
    assert _classify_status(404) == ProbeResult.HARD_FAIL
    assert _classify_status(200) is None
