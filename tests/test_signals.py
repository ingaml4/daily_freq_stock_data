"""指标信号筛选测试：金叉/死叉、布林上下轨、过滤与报告输出。"""

import pandas as pd
from helpers import bar, make_cfg, symbols_frame, trading_dates

from emscan.report import render_table, write_files
from emscan.signals import (
    RULE_SPECS,
    SIGNAL_FORMATS,
    SIGNAL_LABELS,
    build_summary,
    detect,
    normalize_rules,
)

DATES = trading_dates(6, end="2026-10-09")
TRADE_DATE = DATES[-1]
PREV = DATES[-2]


def _symbol(secid, name, board="sh_main", is_st=0):
    return {
        "secid": secid,
        "code": secid.split(".")[-1],
        "name": name,
        "market": int(secid.split(".")[0]),
        "board": board,
        "is_st": is_st,
    }


def _bars(secid, volume=1000.0, close=10.0, low=None, high=None, pct_chg=0.0):
    rows = []
    for date in DATES[:-1]:
        rows.append(bar(secid, date, volume, close=close))
    last = bar(secid, TRADE_DATE, volume, close=close, pct_chg=pct_chg)
    last["low"] = close * 0.98 if low is None else low
    last["high"] = close * 1.02 if high is None else high
    rows.append(last)
    return rows


def _ind(secid, date, **values):
    row = {"secid": secid, "date": date}
    row.update(values)
    return row


def build_market():
    """三只票：金叉+踩下轨 / 无信号 / 金叉但 ST。"""
    bars = []
    indicators = []
    symbols = []

    # A：昨天 DIFF/DEA 死叉状态，今天金叉；且收盘踩到布林下轨
    secid = "1.600001"
    bars += _bars(secid, close=10.0, low=9.5, high=10.4, pct_chg=-3.0)
    indicators += [
        _ind(
            secid,
            PREV,
            macd_diff=-0.20,
            macd_dea=-0.10,
            macd_bar=-0.20,
            kdj_k=20.0,
            kdj_d=30.0,
            boll_upper=11.5,
            boll_mid=10.8,
            boll_lower=10.1,
        ),
        _ind(
            secid,
            TRADE_DATE,
            macd_diff=-0.05,
            macd_dea=-0.08,
            macd_bar=0.06,
            kdj_k=45.0,
            kdj_d=38.0,
            kdj_j=59.0,
            boll_upper=11.2,
            boll_mid=10.6,
            boll_lower=10.0,
        ),
    ]
    symbols.append(_symbol(secid, "金叉踩轨"))

    # B：没有任何交叉，也没有触轨
    secid = "0.300002"
    bars += _bars(secid, close=20.0, low=19.8, high=20.2)
    indicators += [
        _ind(
            secid,
            PREV,
            macd_diff=0.5,
            macd_dea=0.3,
            macd_bar=0.4,
            kdj_k=60.0,
            kdj_d=55.0,
            boll_upper=23.0,
            boll_mid=20.5,
            boll_lower=18.0,
        ),
        _ind(
            secid,
            TRADE_DATE,
            macd_diff=0.6,
            macd_dea=0.4,
            macd_bar=0.4,
            kdj_k=65.0,
            kdj_d=60.0,
            boll_upper=23.2,
            boll_mid=20.8,
            boll_lower=18.4,
        ),
    ]
    symbols.append(_symbol(secid, "普通股", board="sz_gem"))

    # C：金叉但 ST（默认剔除）
    secid = "1.600003"
    bars += _bars(secid, close=5.0, low=4.2, high=5.1)
    indicators += [
        _ind(
            secid,
            PREV,
            macd_diff=-0.30,
            macd_dea=-0.12,
            kdj_k=10.0,
            kdj_d=25.0,
            boll_upper=6.0,
            boll_mid=5.2,
            boll_lower=4.4,
        ),
        _ind(
            secid,
            TRADE_DATE,
            macd_diff=-0.05,
            macd_dea=-0.10,
            kdj_k=40.0,
            kdj_d=30.0,
            boll_upper=5.8,
            boll_mid=5.1,
            boll_lower=4.3,
        ),
    ]
    symbols.append(_symbol(secid, "ST金叉", is_st=1))

    # D：金叉但当日停牌（成交量为 0，默认剔除）
    secid = "1.600004"
    bars += _bars(secid, close=8.0, low=7.5, high=8.2)
    bars[-1]["volume"] = 0.0
    indicators += [
        _ind(
            secid,
            PREV,
            macd_diff=-0.30,
            macd_dea=-0.12,
            kdj_k=10.0,
            kdj_d=25.0,
            boll_upper=9.0,
            boll_mid=8.4,
            boll_lower=7.9,
        ),
        _ind(
            secid,
            TRADE_DATE,
            macd_diff=-0.05,
            macd_dea=-0.10,
            kdj_k=40.0,
            kdj_d=30.0,
            boll_upper=8.9,
            boll_mid=8.3,
            boll_lower=7.8,
        ),
    ]
    symbols.append(_symbol(secid, "停牌金叉"))

    return pd.DataFrame(bars), pd.DataFrame(indicators), symbols_frame(symbols)


def test_detect_finds_golden_cross_and_lower_band(tmp_path):
    cfg = make_cfg(tmp_path)
    bars, indicators, symbols = build_market()
    result = detect(bars, indicators, symbols, cfg, trade_date=TRADE_DATE)
    hits = result.hits
    assert list(hits["secid"]) == ["1.600001"]
    row = hits.iloc[0]
    assert row["code"] == "600001"
    assert "macd_golden" in row["signals"]
    assert "MACD金叉" in row["signals_cn"]
    assert "kdj_golden" in row["signals"]  # 昨天 K<D、今天 K>D
    assert "boll_break_lower" in row["signals"]  # 默认规则：收盘 10.0 <= 下轨 10.0（下轨突破）
    assert row["rule_count"] == 3
    assert row["signals_cn"] == "MACD金叉、KDJ金叉、跌破下轨"
    # 缓存里会把"触及下轨"也算出来（最低价 9.5 <= 10.0），供以后按别的规则筛选
    flags = result.all_metrics.set_index("secid").loc["1.600001"]
    assert flags["s_boll_lower"] == 1 and flags["s_boll_break_lower"] == 1
    assert flags["s_macd_golden"] == 1 and flags["s_kdj_golden"] == 1
    # 布林位置 = (收盘 - 下轨) / (上轨 - 下轨)
    assert row["boll_pos"] == (10.0 - 10.0) / (11.2 - 10.0)
    assert result.stats["rules"] == ["macd_golden", "kdj_golden", "boll_break_lower"]
    assert result.stats["universe"] == 4


def test_detect_respects_kdj_rule_and_match_all(tmp_path):
    cfg = make_cfg(tmp_path, {"signals": {"rules": ["kdj_golden"]}})
    bars, indicators, symbols = build_market()
    result = detect(bars, indicators, symbols, cfg, trade_date=TRADE_DATE)
    # A 与 C 都满足 KDJ 金叉，但 C 是 ST 被剔除
    assert list(result.hits["secid"]) == ["1.600001"]

    cfg_all = make_cfg(
        tmp_path, {"signals": {"rules": ["macd_golden", "kdj_golden"], "match": "all"}}
    )
    both = detect(bars, indicators, symbols, cfg_all, trade_date=TRADE_DATE)
    assert list(both.hits["secid"]) == ["1.600001"]  # A 同时金叉

    # 金叉 + 死叉不可能同时成立 -> all 模式下没有命中
    cfg_strict = make_cfg(
        tmp_path, {"signals": {"rules": ["macd_golden", "kdj_dead"], "match": "all"}}
    )
    strict = detect(bars, indicators, symbols, cfg_strict, trade_date=TRADE_DATE)
    assert strict.hits.empty


def test_detect_returns_empty_without_indicators(tmp_path):
    cfg = make_cfg(tmp_path)
    bars, _, symbols = build_market()
    result = detect(bars, pd.DataFrame(), symbols, cfg, trade_date=TRADE_DATE)
    assert result.hits.empty
    assert result.stats["reason"] == "no_indicators"


def test_detect_falls_back_to_latest_indicator_date(tmp_path):
    cfg = make_cfg(tmp_path)
    bars, indicators, symbols = build_market()
    result = detect(bars, indicators, symbols, cfg, trade_date="2026-10-10")
    assert result.trade_date == TRADE_DATE
    assert result.stats["prev_trade_date"] == PREV


def test_normalize_rules_ignores_unknown(tmp_path, caplog):
    assert normalize_rules(["MACD_GOLDEN", "macd_golden", " ", "not_a_rule"]) == ["macd_golden"]
    assert normalize_rules([]) == ["macd_golden", "kdj_golden", "boll_lower"]
    assert set(RULE_SPECS) >= {"macd_dead", "ma_golden", "boll_upper"}


def test_signal_report_rendering_and_files(tmp_path):
    cfg = make_cfg(
        tmp_path,
        {"store": {"reports_dir": str(tmp_path / "reports")}, "signals": {"top_n": 5}},
    )
    bars, indicators, symbols = build_market()
    result = detect(bars, indicators, symbols, cfg, trade_date=TRADE_DATE)

    summary = build_summary(result, cfg)
    assert TRADE_DATE in summary and "MACD金叉" in summary

    text = render_table(
        result.hits,
        columns=["code", "name", "close", "signals_cn", "boll_pos", "score"],
        labels=SIGNAL_LABELS,
        float_formats=SIGNAL_FORMATS,
    )
    assert "代码" in text and "命中信号" in text and "MACD金叉" in text
    from emscan.report import display_width

    widths = {display_width(line) for line in text.splitlines()}
    assert len(widths) == 1

    files = write_files(result, cfg, prefix="signals", sheet_name="指标信号")
    names = sorted(p.name for p in files)
    assert names == [f"signals_{TRADE_DATE}.csv", f"signals_{TRADE_DATE}.xlsx"]
    frame = pd.read_csv(tmp_path / "reports" / f"signals_{TRADE_DATE}.csv")
    assert {"signals", "boll_pos", "macd_diff"} <= set(frame.columns)
