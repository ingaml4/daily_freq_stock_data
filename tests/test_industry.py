"""行业/概念归属：麦蕊板块接口的解析、落库、按周刷新，以及复盘里的行业分布。"""

import pandas as pd
from helpers import make_cfg

from emscan import industry as industry_mod
from emscan import summary as summary_mod
from emscan.mairui import parse_tag_members, parse_tag_tree, parse_tags_of_stock
from emscan.store import Store

# 真实报文片段（用公开演示证书抓下来的，字段与生产一致）
TREE_SAMPLE = [
    {"name": "A股-申万行业-煤炭", "code": "sw_mt", "level": 2, "pname": "A股-申万行业", "isleaf": 1},
    {"name": "A股-申万行业", "code": "swhy", "level": 1, "pname": "A股", "isleaf": 0},
    {"name": "A股-申万二级-股份制银行Ⅱ", "code": "sw2_480300", "isleaf": 1},
    {"name": "A股-热门概念-长期破净", "code": "chgn_701320", "isleaf": 1},
    {"name": "A股-概念板块-融资融券", "code": "gn_rzrq", "isleaf": 1},
]

MEMBERS_SAMPLE = [
    {"dm": "601857", "mc": "中国石油", "jys": "sh"},
    {"dm": "000001", "mc": "平安银行", "jys": "sz"},
    {"dm": "920088", "mc": "科力股份", "jys": "bj"},
    {"dm": "", "mc": "脏数据", "jys": "sh"},
]


def test_parse_tag_tree_filters_kinds_and_leaves():
    """只留叶子节点；按类别前缀过滤（sw/sw2 能分开，不会被"sw"吞掉"sw2"）。"""
    assert [row["code"] for row in parse_tag_tree(TREE_SAMPLE)] == [
        "sw_mt",
        "sw2_480300",
        "chgn_701320",
        "gn_rzrq",
    ]
    level1 = parse_tag_tree(TREE_SAMPLE, ["sw"])
    assert [row["code"] for row in level1] == ["sw_mt"]
    assert level1[0]["name"] == "煤炭"  # "A股-申万行业-煤炭" -> "煤炭"
    assert [row["kind"] for row in parse_tag_tree(TREE_SAMPLE, ["sw2", "gn"])] == ["sw2", "gn"]


def test_parse_tag_members_and_stock_tags():
    members = parse_tag_members(MEMBERS_SAMPLE)
    assert [m["secid"] for m in members] == ["1.601857", "0.000001", "2.920088"]
    tags = parse_tags_of_stock(
        [
            {"code": "sw_yx", "name": "A股-申万行业-银行"},
            {"code": "chgn_701320", "name": "A股-热门概念-长期破净"},
        ]
    )
    assert [(t["kind"], t["name"]) for t in tags] == [("sw", "银行"), ("chgn", "长期破净")]


def test_store_symbol_tags_roundtrip(tmp_path):
    store = Store(tmp_path / "market.sqlite").init_schema()
    assert store.replace_symbol_tags(
        "sw", [{"secid": "1.600519", "code": "sw_spyl", "name": "食品饮料"}]
    ) == 1
    # 整类覆盖：第二次写入会把旧数据清掉（源数据每周整棵换）
    store.replace_symbol_tags("sw", [{"secid": "1.600519", "code": "sw_yh", "name": "银行"}])
    frame = store.load_symbol_tags(kind="sw")
    assert list(frame["name"]) == ["银行"]
    assert store.symbol_tag_names("sw") == {"sw_yh": "银行"}
    assert store.symbol_tag_stats()["sw"] == {"nodes": 1, "stocks": 1}
    store.close()


def test_sync_tags_writes_membership_and_refresh_window(tmp_path, monkeypatch):
    """按节点批量同步：sw 两个节点 -> 落到 symbol_tags；7 天内不重复拉。"""
    cfg = make_cfg(tmp_path)
    store = Store(tmp_path / "market.sqlite").init_schema()

    class Client:
        def __init__(self):
            self.tree_calls = 0

        def fetch_tag_tree(self, kinds=None):
            self.tree_calls += 1
            return parse_tag_tree(TREE_SAMPLE, kinds)

        def fetch_tag_members_many(self, nodes, progress=None, max_errors=50):
            return (
                {
                    "sw_mt": [{"secid": "1.601857"}, {"secid": "2.920088"}],
                    "sw2_480300": [{"secid": "0.000001"}],
                },
                [],
            )

    client = Client()
    written = industry_mod.sync_tags(store, client, cfg)
    assert written == {"sw": 2, "sw2": 1}
    assert store.symbol_tag_stats()["sw"] == {"nodes": 1, "stocks": 2}
    assert industry_mod.describe(store).startswith("申万一级行业 1 个节点/2 只")
    # 刷新窗口内不再请求
    assert industry_mod.sync_tags(store, client, cfg) == {}
    assert client.tree_calls == 1
    # 强制刷新就再来一遍
    assert industry_mod.sync_tags(store, client, cfg, force=True) == {"sw": 2, "sw2": 1}
    assert client.tree_calls == 2
    store.close()


def test_industry_sync_is_skippable(tmp_path):
    cfg = make_cfg(tmp_path, {"industry": {"enabled": False}})
    store = Store(tmp_path / "market.sqlite").init_schema()
    assert industry_mod.sync_tags(store, object(), cfg) == {}
    store.close()


def test_digest_reports_industry_distribution(tmp_path):
    """复盘摘要要带"哪个行业扎堆"：每只票的行业 + 每个按钮的行业分布 + 全部命中汇总。"""
    from emscan.summary import build_digest

    cfg = make_cfg(tmp_path)
    rows = [
        dict(
            secid="1.600001",
            code="600001",
            name="甲",
            board="sh_main",
            pct_chg=6.0,
            amount=5e8,
            turnover=3.0,
            s_vol_spike=1,
            s_kdj_golden=1,
            kdj_j=80.0,
        ),
        dict(
            secid="1.600002",
            code="600002",
            name="乙",
            board="sh_main",
            pct_chg=5.0,
            amount=4e8,
            turnover=3.0,
            s_vol_spike=1,
            s_kdj_golden=0,
            kdj_j=30.0,
        ),
        dict(
            secid="0.300003",
            code="300003",
            name="丙",
            board="sz_gem",
            pct_chg=4.0,
            amount=3e8,
            turnover=3.0,
            s_vol_spike=0,
            s_kdj_golden=1,
            kdj_j=70.0,
        ),
    ]
    frame = pd.DataFrame(rows)

    class StoreStub:
        def load_daily_signals(self, **_kwargs):
            return frame

        def load_symbol_tags(self, kind=None):
            assert kind == "sw"
            return pd.DataFrame(
                [
                    {"secid": "1.600001", "kind": "sw", "code": "sw_dz", "name": "电子"},
                    {"secid": "1.600002", "kind": "sw", "code": "sw_dz", "name": "电子"},
                    {"secid": "0.300003", "kind": "sw", "code": "sw_jx", "name": "机械设备"},
                ]
            )

    digest = build_digest(StoreStub(), cfg, "2026-10-14")
    assert digest["行业口径"] == "申万一级行业"
    assert digest["行业分布"]["放量大涨"] == {"电子": 2}
    assert digest["行业分布"]["KDJ金叉"] == {"电子": 1, "机械设备": 1}
    assert digest["行业分布_全部命中"] == {"电子": 2, "机械设备": 1}
    assert [row["行业"] for row in digest["views"]["放量大涨"]] == ["电子", "电子"]
    # 规则版（不调 AI）也要把行业分布写出来
    text = summary_mod.rule_summary(digest)
    assert "行业分布（申万一级行业）" in text and "电子 2只" in text
    # 没有行业归属的股票算"未分类"，不能静默丢掉
    digest_missing = build_digest(
        StoreStub(), make_cfg(tmp_path, {"industry": {"kinds": ["gn"]}}), "2026-10-14"
    )
    assert "行业分布" not in digest_missing or digest_missing["行业分布"] == {}


def test_web_shows_industry_column_only_when_available(tmp_path):
    """网页有行业归属就多一列（可排序），没有就不显示空列。"""
    from emscan.web import build_context, render_html

    store = Store(tmp_path / "market.sqlite").init_schema()
    store.upsert_symbols(
        [{"secid": "1.600519", "code": "600519", "name": "贵州茅台", "board": "sh_main"}]
    )
    store.conn.execute(
        "INSERT INTO daily_signals (secid, date, pct_chg, amount, turnover, volume, s_macd_golden) "
        "VALUES ('1.600519', ?, 3.2, 5e9, 1.1, 1000, 1)",
        (str(store.latest_date() or "2026-10-14"),),
    )
    store.conn.commit()
    date = str(store.latest_date() or "2026-10-14")
    page = render_html(build_context(store, view="macd_golden", date=date))
    assert 'data-sort="industry"' not in page and "食品饮料" not in page
    store.replace_symbol_tags("sw", [{"secid": "1.600519", "code": "sw_spyl", "name": "食品饮料"}])
    page = render_html(build_context(store, view="macd_golden", date=date))
    assert 'data-sort="industry"' in page and "食品饮料" in page
    # 表头可以点（纯前端排序），行业列随数据出现
    assert "行业" in page
    store.close()
