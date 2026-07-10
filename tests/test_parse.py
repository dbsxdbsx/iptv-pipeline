"""解析器测试：M3U / TXT 格式识别与字段提取。"""

from iptv_pipeline.parse import parse_content, parse_m3u, parse_txt

M3U_SAMPLE = """#EXTM3U x-tvg-url="http://epg.example/e.xml"
#EXTINF:-1 tvg-id="CCTV1" tvg-name="CCTV-1" tvg-logo="http://logo/cctv1.png" group-title="央视",CCTV-1 综合
http://a.example/cctv1.m3u8
#EXTINF:-1 group-title="卫视",湖南卫视
http://b.example/hunan.m3u8
#EXTVLCOPT:http-user-agent=Mozilla
#EXTINF:-1,无URL频道
"""

TXT_SAMPLE = """央视,#genre#
CCTV-1,http://a.example/1.m3u8#http://b.example/1.m3u8
CCTV-2,http://a.example/2.m3u8
卫视,#genre#
湖南卫视,http://a.example/hunan.m3u8
坏行没有逗号
"""


def test_parse_m3u_extracts_attrs_and_name():
    streams = parse_m3u(M3U_SAMPLE, source="s1")
    assert len(streams) == 2  # 第三个 EXTINF 无 URL，跳过
    first = streams[0]
    assert first.name == "CCTV-1 综合"
    assert first.url == "http://a.example/cctv1.m3u8"
    assert first.logo == "http://logo/cctv1.png"
    assert first.tvg_id == "CCTV1"
    assert first.source == "s1"


def test_parse_m3u_ignores_directive_lines():
    streams = parse_m3u(M3U_SAMPLE, source="s1")
    # #EXTVLCOPT 不应被当作 URL
    urls = [s.url for s in streams]
    assert all(u.startswith("http") for u in urls)


def test_parse_txt_multi_url_split():
    streams = parse_txt(TXT_SAMPLE, source="s2")
    cctv1 = [s for s in streams if s.name == "CCTV-1"]
    assert len(cctv1) == 2  # 一行两个 URL 拆成两条流
    assert {s.url for s in cctv1} == {
        "http://a.example/1.m3u8",
        "http://b.example/1.m3u8",
    }


def test_parse_txt_skips_genre_and_malformed():
    streams = parse_txt(TXT_SAMPLE, source="s2")
    names = {s.name for s in streams}
    assert "央视" not in names  # #genre# 行不产出频道
    assert "坏行没有逗号" not in names


def test_parse_content_auto_detects_format():
    assert len(parse_content(M3U_SAMPLE, "s")) == 2
    assert len(parse_content(TXT_SAMPLE, "s")) == 4


def test_parse_m3u_preserves_safe_headers_and_rejects_credentials():
    content = """#EXTM3U
#EXTINF:-1 http-user-agent="AttrUA" group-title="国际",Header Test
#EXTVLCOPT:http-referrer=https://example.com/
#EXTHTTP:{"Origin":"https://example.com","Cookie":"private=1","Authorization":"Bearer x"}
https://media.example/live.m3u8
"""
    streams = parse_m3u(content, source="headers")

    assert len(streams) == 1
    assert streams[0].headers == {
        "User-Agent": "AttrUA",
        "Referer": "https://example.com/",
        "Origin": "https://example.com",
    }


def test_parse_m3u_pipe_headers_override_directives():
    content = """#EXTM3U
#EXTINF:-1,Pipe Header
#EXTVLCOPT:http-user-agent=OldUA
https://media.example/live.m3u8|User-Agent=NewUA&Referer=https%3A%2F%2Fref.example%2F
"""
    stream = parse_m3u(content, source="pipe")[0]

    assert stream.url == "https://media.example/live.m3u8"
    assert stream.headers == {
        "User-Agent": "NewUA",
        "Referer": "https://ref.example/",
    }


def test_parse_header_rejects_newline_injection():
    content = """#EXTM3U
#EXTINF:-1,Unsafe Header
#EXTHTTP:{"User-Agent":"safe\\r\\nX-Evil: injected"}
https://media.example/live.m3u8
"""
    stream = parse_m3u(content, source="unsafe")[0]
    assert stream.headers == {}
