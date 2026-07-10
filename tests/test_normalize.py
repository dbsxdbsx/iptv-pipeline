"""归一化测试：规范化 key、别名归并、去重、分组、IPv6、排序。"""

from iptv_pipeline.config import Config, GroupRule
from iptv_pipeline.models import Stream
from iptv_pipeline.normalize import (
    build_channels,
    is_chinese_channel,
    is_ipv6_url,
    normalize_key,
)


def _cfg() -> Config:
    return Config(
        upstreams=[],
        alias_to_canonical={
            normalize_key("CCTV1"): "CCTV-1",
            normalize_key("CCTV-1"): "CCTV-1",
            normalize_key("央视1台"): "CCTV-1",
            normalize_key("CCTV5+"): "CCTV-5+",
            normalize_key("湖南卫视"): "湖南卫视",
        },
        canonical_names=["CCTV-1", "CCTV-5+", "湖南卫视"],
        blacklist=["成人", "测试"],
        group_rules=[
            GroupRule(name="央视", match=["cctv", "央视"], priority_names=["CCTV-1", "CCTV-5+"]),
            GroupRule(name="卫视", match=["卫视"], priority_names=["湖南卫视"]),
        ],
        default_group="其他",
    )


def test_normalize_key_strips_noise_and_separators():
    assert normalize_key("CCTV-1 高清") == normalize_key("cctv1")
    assert normalize_key("CCTV　1") == normalize_key("CCTV1")  # 全角空格
    assert normalize_key("CCTV-5+") == "cctv5+"  # 保留 +
    assert normalize_key("CCTV-7 (720p)") == normalize_key("CCTV7")
    assert normalize_key("东方卫视（2160P）") == normalize_key("东方卫视")


def test_alias_merges_variants_into_one_channel():
    streams = [
        Stream(url="http://a/1", name="", raw_name="CCTV1"),
        Stream(url="http://b/1", name="", raw_name="央视1台"),
        Stream(url="http://c/1", name="", raw_name="CCTV-1 高清"),
    ]
    channels = build_channels(streams, _cfg())
    assert len(channels) == 1
    assert channels[0].name == "CCTV-1"
    assert len(channels[0].streams) == 3


def test_dedup_same_name_same_url():
    streams = [
        Stream(url="http://a/1", name="", raw_name="CCTV1"),
        Stream(url="http://a/1", name="", raw_name="CCTV-1"),  # 归一后同名同url
    ]
    channels = build_channels(streams, _cfg())
    assert len(channels[0].streams) == 1


def test_dedup_merges_upstream_provenance():
    streams = [
        Stream(url="http://a.example/1", name="", raw_name="CCTV1", source="source-a"),
        Stream(url="http://a.example/1", name="", raw_name="CCTV-1", source="source-b"),
    ]
    channels = build_channels(streams, _cfg())

    assert channels[0].streams[0].sources == ["source-a", "source-b"]


def test_same_url_with_different_headers_is_not_deduplicated():
    streams = [
        Stream(
            url="http://a.example/1",
            name="",
            raw_name="CCTV1",
            headers={"Referer": "https://a.example/"},
        ),
        Stream(
            url="http://a.example/1",
            name="",
            raw_name="CCTV-1",
            headers={"Referer": "https://b.example/"},
        ),
    ]
    channels = build_channels(streams, _cfg())

    assert len(channels[0].streams) == 2


def test_blacklist_filters_stream():
    streams = [
        Stream(url="http://a/1", name="", raw_name="成人频道"),
        Stream(url="http://b/1", name="", raw_name="CCTV1"),
    ]
    channels = build_channels(streams, _cfg())
    names = {c.name for c in channels}
    assert "成人频道" not in names
    assert "CCTV-1" in names


def test_grouping_and_default():
    streams = [
        Stream(url="http://a/1", name="", raw_name="CCTV1"),
        Stream(url="http://b/1", name="", raw_name="湖南卫视"),
        Stream(url="http://c/1", name="", raw_name="某小众台"),
    ]
    channels = build_channels(streams, _cfg())
    by_name = {c.name: c.group for c in channels}
    assert by_name["CCTV-1"] == "央视"
    assert by_name["湖南卫视"] == "卫视"
    assert by_name["某小众台"] == "其他"


def test_sort_priority_and_natural_order():
    streams = [
        Stream(url="http://a/2", name="", raw_name="CCTV2"),  # 不在别名表，原样
        Stream(url="http://a/10", name="", raw_name="CCTV10"),
        Stream(url="http://a/1", name="", raw_name="CCTV1"),
    ]
    channels = build_channels(streams, _cfg())
    order = [c.name for c in channels]
    # CCTV-1 是 priority_name 排最前；CCTV2 应在 CCTV10 前（自然序）
    assert order[0] == "CCTV-1"
    assert order.index("CCTV2") < order.index("CCTV10")


def test_ipv6_detection_and_marking():
    assert is_ipv6_url("http://[2606:4700::1111]:80/live.m3u8")
    assert not is_ipv6_url("http://1.2.3.4:80/live.m3u8")
    streams = [Stream(url="http://[2606:4700::1111]/1", name="", raw_name="CCTV1")]
    channels = build_channels(streams, _cfg())
    assert channels[0].streams[0].is_ipv6 is True


def test_private_and_multicast_hosts_are_filtered():
    streams = [
        Stream(url="http://127.0.0.1/live.m3u8", name="", raw_name="CCTV1"),
        Stream(url="rtp://239.0.0.1:5000", name="", raw_name="CCTV1"),
        Stream(url="https://public.example/live.m3u8", name="", raw_name="CCTV1"),
    ]
    channels = build_channels(streams, _cfg())

    assert [stream.url for stream in channels[0].streams] == ["https://public.example/live.m3u8"]


def test_is_chinese_channel():
    # 纯汉字启发式：检测 CJK 字符。CCTV-1 无汉字 -> False（分组归属由 pipeline 另行处理）
    assert is_chinese_channel("湖南卫视")
    assert is_chinese_channel("CCTV1综合")
    assert not is_chinese_channel("CCTV-1")
    assert not is_chinese_channel("CNN")
    assert not is_chinese_channel("BBC News")
