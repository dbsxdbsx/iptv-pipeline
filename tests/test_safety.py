from iptv_pipeline.safety import (
    is_safe_stream_url,
    redact_url,
    sanitize_headers,
    supports_deep_probe,
)


def test_sanitize_headers_uses_public_allowlist():
    assert sanitize_headers(
        {
            "user-agent": "Player/1.0",
            "Referrer": "https://example.com/",
            "Cookie": "secret=1",
            "Authorization": "Bearer token",
            "Origin": "https://example.com\r\nX-Evil: yes",
        }
    ) == {
        "User-Agent": "Player/1.0",
        "Referer": "https://example.com/",
    }


def test_safe_stream_url_blocks_local_and_reserved_targets():
    assert is_safe_stream_url("https://media.example/live.m3u8")
    assert not is_safe_stream_url("http://localhost/live")
    assert not is_safe_stream_url("http://127.0.0.1/live")
    assert not is_safe_stream_url("http://169.254.169.254/latest/meta-data")
    assert not is_safe_stream_url("rtp://239.1.1.1:5000")
    assert not is_safe_stream_url("file:///etc/passwd")


def test_deep_probe_protocol_and_log_redaction():
    assert supports_deep_probe("https://media.example/live.m3u8")
    assert not supports_deep_probe("rtmp://media.example/live")
    assert (
        redact_url("https://media.example/live.m3u8?token=secret#fragment")
        == "https://media.example/live.m3u8"
    )
