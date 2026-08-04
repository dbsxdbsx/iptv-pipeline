"""真实 config/ 目录的一致性守卫。

这里断言的都是「错了不会报错、只会静默产出错分组」的那类问题：
配置漂移不抛异常、不进日志，只体现在用户侧侧栏分类变得没用。
"""

from __future__ import annotations

import json
from pathlib import Path

from iptv_pipeline.config import Config, load_lines
from iptv_pipeline.models import Channel
from iptv_pipeline.normalize import assign_group, normalize_group_key, rewrite_logo_url
from iptv_pipeline.pipeline import _is_cn_channel

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _groups_json() -> dict:
    return json.loads((CONFIG_DIR / "groups.json").read_text(encoding="utf-8"))


def test_order_and_display_order_cover_the_same_groups():
    """两个顺序表必须含相同分组名。

    漂移的后果是静默的：display_order 缺了某个桶，group_display_index 会把它排到
    所有已登记分组之后，产物里那一整组频道被甩到末尾，而没有任何报错。
    """
    data = _groups_json()
    assert set(data["order"]) == set(data["display_order"])


def test_every_declared_group_is_in_order():
    """groups 里定义了但 order 里没列的桶是死配置：_load_groups 只遍历 order。"""
    data = _groups_json()
    declared = set(data["groups"])
    ordered = set(data["order"])
    assert declared - ordered == set(), "定义了却没进 order 的分组不会生效"
    # default_group 不需要规则体，foreign_group 必须有
    assert data["foreign_group"] in declared


def test_local_station_precedence_is_after_topical_groups():
    """地方台的关键字是行政区名，必须排在题材桶之后。

    放前面会把「山东体育」「四川妇女儿童」从体育 / 少儿里抢走 —— 实测这样做会让
    体育少 6 个频道、少儿少 10 个，而分布表看上去依然「合理」。
    """
    order = _groups_json()["order"]
    local_idx = order.index("地方台")
    for topical in ("体育", "少儿", "纪录", "影视剧集", "音乐", "春晚"):
        assert order.index(topical) < local_idx, f"{topical} 必须排在地方台之前"


def test_central_and_satellite_precede_local_station():
    """「山东卫视」必须先被卫视接走，否则会因为含「山东」落入地方台。"""
    order = _groups_json()["order"]
    assert order.index("央视") < order.index("地方台")
    assert order.index("卫视") < order.index("地方台")


def test_every_group_declares_a_scope():
    """漏写 scope 会静默落到 auto，走汉字启发式。

    对「地方台」这种全中文名的桶看不出差别，但对「影视剧集」里的 CHC / NewTV 这类
    拉丁名频道，auto 会把它们判成境外扔进 global.m3u，而 cn.m3u 的错不会有任何报错。
    """
    data = _groups_json()
    for name, rule in data["groups"].items():
        assert rule.get("scope") in {"cn", "global"}, f"{name} 未声明 scope"


def test_cn_global_split_follows_config_scope():
    """cn/global 归属必须由配置驱动，不能是代码里的写死名单。

    写死时每加一个分组桶都要记得同步改判定函数，忘了就会把整组判成境外。
    """
    cfg = Config.load(CONFIG_DIR)
    assert _is_cn_channel(Channel(name="东丰", group="地方台"), cfg)
    assert _is_cn_channel(Channel(name="CHC动作电影HD", group="影视剧集"), cfg)
    assert not _is_cn_channel(Channel(name="CNN", group="国际"), cfg)
    # 「其他」未登记 scope，退回汉字启发式
    assert _is_cn_channel(Channel(name="华数4K", group="其他"), cfg)
    assert not _is_cn_channel(Channel(name="GOOD TV CH1", group="其他"), cfg)


def test_upstream_tokens_are_stored_normalized():
    """upstream 集合在加载时就要归一化，否则带装饰或全角的上游分组名永远匹配不上。"""
    cfg = Config.load(CONFIG_DIR)
    for rule in cfg.group_rules:
        for token in rule.upstream:
            assert token == normalize_group_key(token), f"{rule.name} 的 {token!r} 未归一化"


def test_logo_rewrites_load_and_cover_the_dead_fanmingming_cdn():
    """live.fanmingming.cn 是国内台标的事实标准、被大量上游内嵌引用。

    它 2026-08 挂掉时影响 129 个已发布频道，而这些地址来自上游 m3u 字符串，
    停用上游解决不了——必须在归一化阶段改道。
    """
    cfg = Config.load(CONFIG_DIR)
    assert cfg.logo_rewrites, "台标重写表为空"
    rewritten = rewrite_logo_url("https://live.fanmingming.cn/tv/CCTV1.png", cfg)
    assert rewritten != "https://live.fanmingming.cn/tv/CCTV1.png"
    assert rewritten.endswith("/tv/CCTV1.png"), "只许换前缀，换掉路径会把台标全部打成 404"


def test_no_upstream_points_at_a_host_we_rewrite_away():
    """已知失效到需要改道的 host，不该同时还挂在上游列表里等着超时。

    这正是上一批的实况：fanmingming 的 CDN 既是死上游、又是死台标源，
    而两处都只是静默失败，各自看起来都不像问题。
    """
    cfg = Config.load(CONFIG_DIR)
    upstreams = load_lines(CONFIG_DIR / "upstreams.txt")
    for dead_prefix, _ in cfg.logo_rewrites:
        host = dead_prefix.split("//", 1)[-1].rstrip("/")
        for url in upstreams:
            assert host not in url, f"上游 {url} 仍指向已判失效的 {host}"


def test_real_config_classifies_representative_channels():
    """拿实测数据里真出现过的频道名做端到端抽查。

    每一条都对应一次实机踩坑，不是构造的例子。
    """
    cfg = Config.load(CONFIG_DIR)
    cases = [
        # (频道名, 上游分组, 期望分组)
        ("CCTV-1", ["央视频道"], "央视"),
        ("CCTV-5", ["咪咕赛事"], "央视"),  # 关键字必须压过上游题材分组
        ("湖南卫视", ["卫视频道"], "卫视"),
        ("北京衛視 (1080p) [Geo-blocked]", ["General"], "卫视"),  # 繁体
        ("翡翠台", ["港澳台频道"], "港澳台"),
        ("EBC News (東森新聞台) (1080p)", ["News"], "港澳台"),  # 繁体 + 拉丁缩写
        ("东莞新闻综合", ["地方频道"], "地方台"),
        ("东丰", ["☘️吉林频道"], "地方台"),  # 名字无线索，只有上游分组认得
        ("山东农科 (406p) [Geo-blocked]", ["Science"], "地方台"),  # 行政区名在频道名里
        ("房山电视台 (576p)", ["General"], "地方台"),
        ("山东体育", ["地方频道"], "体育"),  # 题材优先于地域
        ("三国演义", ["埋堆堆频道"], "影视剧集"),
        ("七龙珠", ["动画频道"], "少儿"),
        ("求索动物", ["数字频道"], "纪录"),
        ("2024年春晚", ["历年春晚"], "春晚"),
        ("CNN", ["News"], "国际"),
        ("24 Kanal (720p)", ["General"], "国际"),  # 境外兜底
        ("312 Кино (406p)", ["Movies"], "国际"),
    ]
    for name, raw_groups, expected in cases:
        assert assign_group(name, raw_groups, cfg) == expected, name
