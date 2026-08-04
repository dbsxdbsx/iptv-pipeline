"""归一化测试：规范化 key、别名归并、去重、分组、IPv6、排序。"""

from iptv_pipeline.config import Config, GroupRule
from iptv_pipeline.models import Stream
from iptv_pipeline.normalize import (
    assign_group,
    build_channels,
    is_chinese_channel,
    is_ipv6_url,
    normalize_group_key,
    normalize_key,
    rewrite_logo_url,
    split_upstream_groups,
)

_FMM_RAW = "https://raw.githubusercontent.com/fanmingming/live/main/"


def _cfg(**overrides) -> Config:
    defaults = dict(
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
            GroupRule(
                name="央视",
                match=["cctv", "央视"],
                priority_names=["CCTV-1", "CCTV-5+"],
                upstream={"央视频道"},
                scope="cn",
            ),
            GroupRule(name="卫视", match=["卫视"], priority_names=["湖南卫视"], scope="cn"),
            GroupRule(name="体育", match=["体育"], upstream={"咪咕赛事"}, scope="cn"),
            GroupRule(
                name="地方台",
                match=[],
                upstream={"地方频道"},
                upstream_match=["四川"],
                scope="cn",
            ),
            GroupRule(name="国际", match=["cnn"], scope="global"),
        ],
        default_group="其他",
        foreign_group="国际",
        logo_rewrites=[("https://live.fanmingming.cn/", _FMM_RAW)],
    )
    defaults.update(overrides)
    return Config(**defaults)


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


def test_normalize_group_key_strips_decoration():
    assert normalize_group_key("☘️四川频道") == "四川频道"
    assert normalize_group_key(" 卫视频道 ") == "卫视频道"
    assert normalize_group_key("央视IPV4") == "央视ipv4"
    assert normalize_group_key("4K8K频道") == "4k8k频道"


def test_split_upstream_groups_handles_multi_value():
    # iptv-org 用分号承载多标签
    assert split_upstream_groups("Culture;Documentary;Travel") == [
        "culture",
        "documentary",
        "travel",
    ]
    assert split_upstream_groups("") == []
    assert split_upstream_groups(";;") == []


def test_channel_name_keyword_beats_upstream_group():
    """CCTV-5 在部分上游里被塞进「咪咕赛事」，但用户找它只会去央视。

    这条顺序一旦反过来，央视会被上游的题材分组零散拆走，而产物里看不出任何异常。
    """
    assert assign_group("CCTV-5", ["咪咕赛事"], _cfg()) == "央视"


def test_upstream_group_recalls_what_keywords_cannot():
    """地方台没有可靠的名字特征，只有上游分组认得它们。"""
    cfg = _cfg()
    assert assign_group("东丰", ["地方频道"], cfg) == "地方台"
    # 带装饰的变体走 upstream_match 子串匹配
    assert assign_group("万荣综合", ["☘️四川频道"], cfg) == "地方台"


def test_foreign_fallback_only_applies_to_non_cjk_names():
    cfg = _cfg()
    # 无名字特征、无可信上游分组、不含汉字 -> 境外兜底
    assert assign_group("24 Kanal (720p)", ["General"], cfg) == "国际"
    # 含汉字则不得进国际：iptv-org 的英文分类故意不做映射，正是为了避免这种误判
    assert assign_group("和政电视台", ["General"], cfg) == "其他"


def test_foreign_fallback_can_be_disabled():
    cfg = _cfg(foreign_group="")
    assert assign_group("24 Kanal (720p)", ["General"], cfg) == "其他"


def test_group_uses_all_streams_not_just_the_first():
    """同一频道来自多个上游时，只看第一条流会丢掉后来那条才带的分组信息。

    内置源 bjzhou 整份都没有 group-title，它排在 upstreams 首位。
    """
    streams = [
        Stream(url="http://a/1", name="", raw_name="东丰", raw_group=""),
        Stream(url="http://b/1", name="", raw_name="东丰", raw_group="地方频道"),
    ]
    channels = build_channels(streams, _cfg())
    assert channels[0].group == "地方台"


def test_display_order_is_independent_of_precedence():
    """判定要「题材优先于地域」，展示要「常看的排前面」，两者结论不同。"""
    cfg = _cfg(display_order=["地方台", "央视", "卫视", "体育", "国际", "其他"])
    streams = [
        Stream(url="http://a/1", name="", raw_name="CCTV1"),
        Stream(url="http://b/1", name="", raw_name="东丰", raw_group="地方频道"),
    ]
    channels = build_channels(streams, cfg)
    # 地方台在判定顺序里靠后（题材优先），但展示时排在央视之前
    assert [c.group for c in channels] == ["地方台", "央视"]


def test_logo_rewrite_only_swaps_the_prefix():
    cfg = _cfg()
    assert (
        rewrite_logo_url("https://live.fanmingming.cn/tv/CCTV1.png", cfg)
        == f"{_FMM_RAW}tv/CCTV1.png"
    )
    # 文件名含中文与空格时不得被改动（这类台标占多数）
    assert (
        rewrite_logo_url("https://live.fanmingming.cn/tv/湖南卫视.png", cfg)
        == f"{_FMM_RAW}tv/湖南卫视.png"
    )


def test_logo_rewrite_leaves_other_hosts_alone():
    cfg = _cfg()
    for url in ("https://i.imgur.com/abc.png", "", "https://tb.zbds.top/tv/x.png"):
        assert rewrite_logo_url(url, cfg) == url


def test_logo_rewrite_happens_before_dedup():
    """改道必须早于去重：logo 是「取第一个非空」，晚了就得改好几处、且 artifacts 落旧值。"""
    streams = [
        Stream(
            url="http://a/1",
            name="",
            raw_name="CCTV1",
            logo="https://live.fanmingming.cn/tv/CCTV1.png",
        ),
    ]
    channels = build_channels(streams, _cfg())
    assert channels[0].logo == f"{_FMM_RAW}tv/CCTV1.png"
    assert channels[0].streams[0].logo == f"{_FMM_RAW}tv/CCTV1.png"


def test_logo_rewrite_is_noop_without_config():
    cfg = _cfg(logo_rewrites=[])
    url = "https://live.fanmingming.cn/tv/CCTV1.png"
    assert rewrite_logo_url(url, cfg) == url


def test_group_scope_lookup():
    cfg = _cfg()
    assert cfg.group_scope("央视") == "cn"
    assert cfg.group_scope("国际") == "global"
    assert cfg.group_scope("其他") == "auto"  # 未登记的分组


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
