"""复盘梯队跟踪回测：名单解析、落库、1/5/10/20 交易日收益与超额。"""

import pandas as pd
import pytest
from helpers import make_cfg

from emscan import backtest as bt
from emscan.store import Store

NEW_FORMAT_DOC = """## 一、今日全景
…复盘正文…

## 七、跟踪名单（机器可读，格式一个字都不要改）

TIER1: 301591 肯特股份 | 603344 星德胜（放量+反转） | 000001 平安银行
TIER2: 600519 贵州茅台 | 999999 不存在股份
"""

OLD_FORMAT_DOC = """## 四、多信号共振
强弱排序：第一梯队为肯特股份、星德胜、标榜股份；第二梯队为亚香股份、洪都航空。
"""


def _store(tmp_path):
    store = Store(tmp_path / "market.sqlite").init_schema()
    store.upsert_symbols(
        [
            {"secid": "0.301591", "code": "301591", "name": "肯特股份", "board": "sz_gem"},
            {"secid": "1.603344", "code": "603344", "name": "星德胜", "board": "sh_main"},
            {"secid": "0.000001", "code": "000001", "name": "平安银行", "board": "sz_main"},
            {"secid": "1.600519", "code": "600519", "name": "贵州茅台", "board": "sh_main"},
            {"secid": "0.301220", "code": "301220", "name": "亚香股份", "board": "sz_gem"},
            {"secid": "1.600316", "code": "600316", "name": "洪都航空", "board": "sh_main"},
            {"secid": "0.301181", "code": "301181", "name": "标榜股份", "board": "sz_gem"},
        ]
    )
    return store


def test_parse_tier_lines_prefers_machine_readable_block(tmp_path):
    store = _store(tmp_path)
    parsed = bt.parse_tiers(NEW_FORMAT_DOC, bt.build_symbol_index(store))
    assert parsed["format"] == "tier-line"
    assert [p["code"] for p in parsed["picks"][1]] == ["301591", "603344", "000001"]
    assert [p["code"] for p in parsed["picks"][2]] == ["600519"]
    # 理由写在括号里也能取出来
    assert parsed["picks"][1][1]["reason"] == "放量+反转"
    # AI 编的代码不会静默消失
    assert [u["code"] for u in parsed["unmatched"]] == ["999999"]
    store.close()


def test_parse_tiers_falls_back_to_prose(tmp_path):
    """老格式（没有 TIER 行）也能从"第一梯队为…"里捞出来，历史复盘不至于空白。"""
    store = _store(tmp_path)
    parsed = bt.parse_tiers(OLD_FORMAT_DOC, bt.build_symbol_index(store))
    assert parsed["format"] == "prose"
    assert [p["name"] for p in parsed["picks"][1]] == ["肯特股份", "星德胜", "标榜股份"]
    assert [p["name"] for p in parsed["picks"][2]] == ["亚香股份", "洪都航空"]
    # 老格式里的说明文字不算"AI 写错"，不要拿去报警刷屏
    assert parsed["unmatched"] == []
    store.close()


def test_parse_tiers_truncates_to_configured_limits(tmp_path):
    """提示词只是要求：程序按 tiers.max_tier1/2 硬截断。"""
    store = _store(tmp_path)
    doc = (
        "TIER1: 301591 肯特股份 | 603344 星德胜 | 000001 平安银行 | "
        "600519 贵州茅台 | 301220 亚香股份\n"
    )
    parsed = bt.parse_tiers(doc, bt.build_symbol_index(store), limits={1: 3, 2: 2})
    assert [p["code"] for p in parsed["picks"][1]] == ["301591", "603344", "000001"]
    assert parsed["truncated"][1] == 2
    store.close()


def test_sync_tier_picks_honours_limits(tmp_path):
    cfg = make_cfg(tmp_path, {"tiers": {"max_tier1": 2, "max_tier2": 1}})
    store = _store(tmp_path)
    store.save_summary(
        {
            "date": "2026-10-14",
            "engine": "deepseek-flash",
            "content": NEW_FORMAT_DOC,
            "digest": "{}",
        }
    )
    stats = bt.sync_tier_picks(store, cfg)
    assert stats["tier1"] == 2 and stats["tier2"] == 1
    assert stats["truncated"]["tier1"] == 1
    picks = store.load_tier_picks()
    assert len(picks[picks["tier"] == 1]) == 2
    store.close()


def _seed_history(store, days, closes):
    """写一段日线：``closes[secid] = [(date, open, close), …]``。"""
    rows = []
    for secid, series in closes.items():
        for date, open_price, close in series:
            rows.append(
                {
                    "secid": secid,
                    "date": date,
                    "open": open_price,
                    "high": max(open_price, close) * 1.01,
                    "low": min(open_price, close) * 0.99,
                    "close": close,
                    "volume": 1000.0,
                    "amount": 1e7,
                }
            )
    store.upsert_bars(rows)
    return rows


def test_evaluate_computes_returns_excess_and_tracking_state(tmp_path):
    """T+1 开盘买入 → 1/5/10/20 交易日收益；没走完的显示跟踪中；超额对标全市场等权。"""
    store = _store(tmp_path)
    days = [f"2026-10-{d:02d}" for d in range(1, 29)]
    # 肯特股份：每天都涨 1%；平安银行：每天都跌 1%（用来当"基准不如"的对照）
    _seed_history(
        store,
        days,
        {
            "0.301591": [
                (d, 10.0 * (1.01**i), 10.0 * (1.01 ** (i + 1))) for i, d in enumerate(days)
            ],
            "0.000001": [
                (d, 10.0 * (0.99**i), 10.0 * (0.99 ** (i + 1))) for i, d in enumerate(days)
            ],
        },
    )
    store.save_tier_picks(
        [
            {
                "date": days[0],
                "tier": 1,
                "secid": "0.301591",
                "code": "301591",
                "name": "肯特股份",
            },
            {
                "date": days[0],
                "tier": 2,
                "secid": "0.000001",
                "code": "000001",
                "name": "平安银行",
            },
        ]
    )
    cfg = make_cfg(tmp_path, {"backtest": {"entry": "next_open", "horizons": [1, 5, 10, 20]}})
    result = bt.evaluate(store, cfg)
    first = [r for r in result["rows"] if r["code"] == "301591"][0]
    assert first["status"] == "已完成"
    assert first["entry_date"] == days[1]  # T+1
    assert first["entry"] == pytest.approx(10.0 * 1.01)  # T+1 开盘
    assert first["rets"][1] == pytest.approx(0.01, abs=1e-3)  # 买入当天收盘 +1%
    assert first["rets"][20] == pytest.approx(1.01**20 - 1, abs=1e-3)
    assert first["max_gain"] > first["rets"][1]
    assert first["excess"][20] > 0  # 同期基准（都是跌的票）明显更差

    # 只看 1 个持有期、且持有期超过本地交易日 → 跟踪中，收益留空
    cfg_partial = make_cfg(tmp_path, {"backtest": {"entry": "next_open", "horizons": [20]}})
    partial = bt.evaluate(
        store,
        cfg_partial,
        pd.DataFrame(
            [
                {
                    "date": days[-1],
                    "tier": 1,
                    "secid": "0.301591",
                    "code": "301591",
                    "name": "肯特股份",
                    "reason": "",
                    "source": "summary",
                }
            ]
        ),
    )
    row = partial["rows"][0]
    assert row["status"] == "待入场" and row["rets"] == {}
    store.close()


def test_evaluate_close_entry_and_aggregates(tmp_path):
    """T 日收盘买入口径（entry=close），以及按梯队/按日期的汇总。"""
    store = _store(tmp_path)
    days = [f"2026-09-{d:02d}" for d in range(1, 29)]
    _seed_history(
        store,
        days,
        {
            "0.301591": [(d, 10.0 + i, 10.0 + i + 1) for i, d in enumerate(days)],
            "1.600519": [(d, 100.0, 100.0 - i) for i, d in enumerate(days)],
        },
    )
    store.save_tier_picks(
        [
            {"date": days[2], "tier": 1, "secid": "0.301591", "code": "301591", "name": "肯特股份"},
            {"date": days[2], "tier": 2, "secid": "1.600519", "code": "600519", "name": "贵州茅台"},
        ]
    )
    cfg = make_cfg(tmp_path, {"backtest": {"entry": "close", "horizons": [1, 5]}})
    result = bt.evaluate(store, cfg)
    first = [r for r in result["rows"] if r["code"] == "301591"][0]
    assert first["entry_date"] == days[2]  # T 日收盘买入
    assert first["entry"] == pytest.approx(13.0)
    assert first["rets"][1] == pytest.approx(14.0 / 13.0 - 1, abs=1e-4)
    # 汇总：第一梯队均值为正、第二梯队为负
    assert result["by_tier"]["1"]["1"]["avg"] > 0
    assert result["by_tier"]["2"]["1"]["avg"] < 0
    assert result["by_tier"]["1"]["picks"] == 1
    assert result["by_date"][0]["date"] == days[2]
    assert result["by_date"][0]["tier_label"] == "第一梯队"
    store.close()


def test_sync_tier_picks_from_stored_summaries(tmp_path, monkeypatch):
    """复盘落库之后能自动/手动解析成梯队名单；已有名单不重复解析（除非 --force）。"""
    store = _store(tmp_path)
    store.save_summary(
        {"date": "2026-10-14", "engine": "deepseek-flash", "content": NEW_FORMAT_DOC}
    )
    store.save_summary({"date": "2026-10-13", "engine": "rule", "content": "规则版，没有梯队名单"})
    stats = bt.sync_tier_picks(store, make_cfg(tmp_path))
    assert stats["dates"] == 1 and stats["tier1"] == 3 and stats["tier2"] == 1
    assert store.tier_dates() == ["2026-10-14"]
    picks = store.load_tier_picks()
    assert set(picks["tier"]) == {1, 2}
    # 再跑一次：已解析的跳过
    assert bt.sync_tier_picks(store, make_cfg(tmp_path))["dates"] == 0
    store.close()


def test_web_tiers_page_and_api(tmp_path):
    """网页 /tiers 能打开并展示梯队汇总、明细；没有名单时给提示而不是报错。"""
    from emscan.web import make_handler, render_tiers_page

    cfg = make_cfg(tmp_path)
    store = _store(tmp_path)
    days = [f"2026-08-{d:02d}" for d in range(1, 29)]
    _seed_history(
        store,
        days,
        {"0.301591": [(d, 10.0 + i, 10.0 + i + 1) for i, d in enumerate(days)]},
    )
    store.save_tier_picks(
        [{"date": days[0], "tier": 1, "secid": "0.301591", "code": "301591", "name": "肯特股份"}]
    )
    result = bt.report(store, cfg, days=30)
    page = render_tiers_page(result, title="测试", days=30, entry="next_open")
    assert "梯队回测" in page
    assert "① 按梯队汇总" in page and "肯特股份" in page
    assert "https://www.xueqiu.com/S/SZ301591" in page
    assert "/tiers.csv?days=30" in page

    empty = render_tiers_page(
        {"rows": [], "horizons": [1, 5], "by_tier": {}, "by_date": []}, title="测试"
    )
    assert "还没有梯队名单" in empty

    # make_handler 要接受 backtest_cfg（回归：漏传会让 /tiers 直接 500）
    handler_cls = make_handler(lambda: store, backtest_cfg=cfg)
    assert handler_cls is not None
    store.close()
