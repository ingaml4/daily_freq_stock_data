"""放量扫描算法测试：命中、边界剔除、停牌处理、稳健Z分支、分类与排序。"""

import numpy as np
import pandas as pd
import pytest
from helpers import bar, flat_history, make_cfg, symbols_frame, trading_dates

from emscan.scan import SCAN_COLUMNS, SIGNAL_DOWN, SIGNAL_FLAT, SIGNAL_UP, scan

BASE = 1_000_000.0  # 基准成交量（手）
DATES = trading_dates(80, end="2026-10-09")
TRADE_DATE = DATES[-1]
HIST = DATES[:-1]


def _symbol(secid, name, board="sh_main", is_st=0):
    return {
        "secid": secid,
        "code": secid.split(".")[-1],
        "name": name,
        "market": int(secid.split(".")[0]),
        "board": board,
        "is_st": is_st,
    }


def build_market():
    """构造一个包含各类样本的迷你市场。"""
    bars = []
    syms = []

    # 1) 真放量上涨（应命中）
    bars += flat_history("1.600001", HIST, BASE)
    bars.append(bar("1.600001", TRADE_DATE, 5 * BASE, pct_chg=5.0))
    syms.append(_symbol("1.600001", "放量上攻"))

    # 2) 平时正常波动（不应命中）
    bars += flat_history("1.600002", HIST, BASE)
    bars.append(bar("1.600002", TRADE_DATE, 1.2 * BASE, pct_chg=1.0))
    syms.append(_symbol("1.600002", "正常波动"))

    # 3) 放量下跌（应命中，形态=放量下跌）
    bars += flat_history("1.600003", HIST, BASE)
    bars.append(bar("1.600003", TRADE_DATE, 4 * BASE, pct_chg=-6.0))
    syms.append(_symbol("1.600003", "放量下跌"))

    # 4) 放量滞涨（应命中，形态=放量滞涨）
    bars += flat_history("1.600004", HIST, BASE)
    bars.append(bar("1.600004", TRADE_DATE, 4 * BASE, pct_chg=0.2))
    syms.append(_symbol("1.600004", "放量滞涨"))

    # 5) ST 股放量（应被剔除）
    bars += flat_history("1.600005", HIST, BASE)
    bars.append(bar("1.600005", TRADE_DATE, 5 * BASE, pct_chg=4.9))
    syms.append(_symbol("1.600005", "ST测试", is_st=1))

    # 6) 新股：历史只有 10 根K线（应被剔除）
    short = HIST[-10:]
    bars += flat_history("1.600006", short, BASE)
    bars.append(bar("1.600006", TRADE_DATE, 6 * BASE, pct_chg=5.0))
    syms.append(_symbol("1.600006", "次新股"))

    # 7) 当日停牌（成交量为 0，应被剔除）
    bars += flat_history("1.600007", HIST, BASE)
    bars.append(bar("1.600007", TRADE_DATE, 0.0, pct_chg=0.0))
    syms.append(_symbol("1.600007", "停牌股"))

    # 8) 小成交额（放量 20 倍也过不了 5000 万门槛，应被剔除）
    tiny = 1000.0
    bars += flat_history("1.600008", HIST, tiny, close=10.0)
    bars.append(bar("1.600008", TRADE_DATE, 20 * tiny, pct_chg=3.0))
    syms.append(_symbol("1.600008", "小成交额"))

    # 9) 历史里有停牌日：最后 3 个交易日成交量为 0，今天复牌放量 2.5 倍
    rows = flat_history("1.600009", HIST, BASE)
    for row in rows[-3:]:
        row["volume"] = 0.0
        row["amount"] = 0.0
    bars += rows
    bars.append(bar("1.600009", TRADE_DATE, 2.5 * BASE, pct_chg=2.0))
    syms.append(_symbol("1.600009", "停牌复牌"))

    return pd.DataFrame(bars), symbols_frame(syms)


def scan_market(cfg, frame, symbols, trade_date=TRADE_DATE):
    return scan(frame, symbols, cfg, trade_date=trade_date)


def test_hits_spikes_and_ignores_normal_volume(tmp_path):
    cfg = make_cfg(tmp_path)
    frame, symbols = build_market()
    result = scan_market(cfg, frame, symbols)
    hits = set(result.hits["secid"])
    assert "1.600001" in hits
    assert "1.600003" in hits
    assert "1.600004" in hits
    assert "1.600002" not in hits  # 正常波动不该被误报
    assert result.stats["universe"] == 9
    assert result.stats["candidates"] == len(result.hits)


def test_boundary_samples_are_filtered_out(tmp_path):
    cfg = make_cfg(tmp_path)
    frame, symbols = build_market()
    hits = set(scan_market(cfg, frame, symbols).hits["secid"])
    assert "1.600005" not in hits  # ST
    assert "1.600006" not in hits  # 新股历史不足
    assert "1.600007" not in hits  # 当日停牌
    assert "1.600008" not in hits  # 成交额太小


def test_st_filter_can_be_disabled(tmp_path):
    cfg = make_cfg(tmp_path, {"universe": {"exclude_st": False}})
    frame, symbols = build_market()
    hits = set(scan_market(cfg, frame, symbols).hits["secid"])
    assert "1.600005" in hits  # 关掉 ST 过滤后就会被算进来


def test_suspension_days_are_excluded_from_baseline(tmp_path):
    cfg = make_cfg(tmp_path)
    frame, symbols = build_market()
    hits = scan_market(cfg, frame, symbols).hits.set_index("secid")
    ratio = float(hits.loc["1.600009", "vol_ratio_5"])
    # 基准只算有成交的 2 天（≈1.0 倍基准量），今天 2.5 倍 → 量比≈2.5
    assert 2.2 < ratio < 2.9, ratio
    # valid_bars 统计的是"基准窗口(默认60个交易日)内的有效交易日数"，停牌那 3 天不算
    assert float(hits.loc["1.600009", "valid_bars"]) == 60 - 3


def test_zero_volume_days_would_inflate_ratio_if_not_excluded(tmp_path):
    """反向验证：如果不剔除零成交日，量比会被放大到 6 倍以上。"""
    cfg = make_cfg(tmp_path)
    frame, symbols = build_market()
    hits = scan_market(cfg, frame, symbols).hits.set_index("secid")
    ratio = float(hits.loc["1.600009", "vol_ratio_5"])
    assert ratio < 3.0  # 未剔除时会算成 2.5/0.385≈6.5


def test_signal_classification_and_score_order(tmp_path):
    cfg = make_cfg(tmp_path)
    frame, symbols = build_market()
    hits = scan_market(cfg, frame, symbols).hits.set_index("secid")
    assert hits.loc["1.600001", "signal"] == SIGNAL_UP
    assert hits.loc["1.600003", "signal"] == SIGNAL_DOWN
    assert hits.loc["1.600004", "signal"] == SIGNAL_FLAT
    scores = hits["score"].tolist()
    assert scores == sorted(scores, reverse=True)
    assert hits.loc["1.600001", "score"] > hits.loc["1.600004", "score"]


def test_z_score_branch_fires_when_ratio_rule_does_not(tmp_path):
    """当20日量比远低于阈值时，稳健Z分数分支应该独立触发命中。"""
    dates = trading_dates(80, end="2026-10-09")
    hist, trade_date = dates[:-1], dates[-1]
    secid = "1.600100"
    bars = []
    for date in hist[:59]:  # 长时间极小量（多数派 → 中位数=极小量，MAD=0）
        bars.append(bar(secid, date, 0.1 * BASE))
    for date in hist[59:74]:  # 中段 15 天巨量（把 20 日均值抬高）
        bars.append(bar(secid, date, 20 * BASE))
    for date in hist[74:]:  # 最近 5 天又回到极小量
        bars.append(bar(secid, date, 0.1 * BASE))
    bars.append(bar(secid, trade_date, 0.5 * BASE, pct_chg=1.0))

    symbols = symbols_frame([_symbol(secid, "厚尾放量")])
    cfg = make_cfg(
        tmp_path, {"scan": {"vol_ratio_5_min": 2.0, "vol_ratio_20_min": 0.5, "z_min": 3.0}}
    )
    hits = scan(pd.DataFrame(bars), symbols, cfg, trade_date=trade_date).hits.set_index("secid")
    assert secid in hits.index
    assert float(hits.loc[secid, "vol_ratio_5"]) > 2.0
    assert float(hits.loc[secid, "vol_ratio_20"]) < 0.5  # 20日量比不满足
    assert float(hits.loc[secid, "vol_z60"]) >= 3.0

    # 关掉 Z 分支后，同一只票不再命中
    cfg_no_z = make_cfg(
        tmp_path, {"scan": {"vol_ratio_5_min": 2.0, "vol_ratio_20_min": 0.5, "z_min": 999.0}}
    )
    no_hit = scan(pd.DataFrame(bars), symbols, cfg_no_z, trade_date=trade_date).hits
    assert secid not in set(no_hit["secid"])


def test_vol_ratio_gate_filters_mild_volume_increase(tmp_path):
    cfg = make_cfg(tmp_path)
    frame, symbols = build_market()
    hits = set(scan_market(cfg, frame, symbols).hits["secid"])
    # 1.2 倍成交额不足 2 倍量比，不应命中
    assert "1.600002" not in hits


def test_z_metric_math_is_robust(tmp_path):
    """对数成交量中位数+MAD 的 Z 分数：今天恰好等于中位数时接近 0。"""
    dates = trading_dates(70)
    secid = "1.600200"
    bars = [bar(secid, d, BASE * (1.0 + 0.3 * ((i % 7) - 3) / 3)) for i, d in enumerate(dates[:-1])]
    bars.append(bar(secid, dates[-1], BASE, pct_chg=0.0))
    symbols = symbols_frame([_symbol(secid, "中位附近")])
    cfg = make_cfg(
        tmp_path, {"scan": {"vol_ratio_5_min": 0.0, "vol_ratio_20_min": 0.0, "z_min": -99.0}}
    )
    result = scan(pd.DataFrame(bars), symbols, cfg, trade_date=dates[-1])
    z = float(result.hits.set_index("secid").loc[secid, "vol_z60"])
    assert abs(z) < 1.0


def test_turnover_fallback_uses_float_market_cap(tmp_path):
    """换手率字段缺失时，用 成交额/流通市值 估算，不让门槛被绕过。"""
    dates = trading_dates(80)
    secid = "0.300001"
    bars = flat_history(secid, dates[:-1], BASE)
    for row in bars:  # 清掉换手率，只留流通市值
        row["turnover"] = None
        row["float_mv"] = 1e10
    bars.append(bar(secid, dates[-1], 5 * BASE, pct_chg=4.0, turnover=None, float_mv=1e10))
    symbols = symbols_frame([_symbol(secid, "缺换手率", board="sz_gem")])
    cfg = make_cfg(tmp_path)
    hits = scan(pd.DataFrame(bars), symbols, cfg, trade_date=dates[-1]).hits.set_index("secid")
    assert secid in hits.index
    # 成交额 5e6 手 * 100 * 10 元 = 5e9 元 → 5e9/1e10 = 50%
    assert float(hits.loc[secid, "turnover"]) == pytest.approx(50.0, rel=1e-3)


def test_top_n_limits_output(tmp_path):
    cfg = make_cfg(tmp_path, {"scan": {"top_n": 2}})
    frame, symbols = build_market()
    result = scan_market(cfg, frame, symbols)
    assert len(result.hits) == 2
    assert result.stats["candidates"] >= 2


def test_empty_and_missing_inputs(tmp_path):
    cfg = make_cfg(tmp_path)
    symbols = symbols_frame([_symbol("1.600001", "空数据")])
    empty = scan(pd.DataFrame(columns=["secid", "date", "volume"]), symbols, cfg)
    assert empty.hits.empty
    assert empty.stats.get("reason") == "no_data"

    bars = pd.DataFrame([bar("1.600001", "2026-10-01", 1000.0)])
    with pytest.raises(ValueError):
        scan(bars, symbols, cfg, trade_date="2026-10-09")

    only_today = pd.DataFrame([bar("1.600001", "2026-10-09", 1000.0)])
    result = scan(only_today, symbols, cfg, trade_date="2026-10-09")
    assert result.hits.empty
    assert result.stats.get("reason") == "no_history"


def test_non_excel_symbol_without_metadata(tmp_path):
    """symbols 传 None 时也要能跑（用 secid 兜底当代码）。"""
    cfg = make_cfg(tmp_path)
    frame, _ = build_market()
    result = scan(frame, None, cfg, trade_date=TRADE_DATE)
    assert len(result.hits) > 0
    assert set(result.hits["code"]).issuperset({"600001"})
    assert np.isfinite(result.hits["score"]).all()


def test_scan_works_on_slim_column_projection(tmp_path):
    """扫描路径只读 SCAN_COLUMNS（无 open/high/low/source）时结果不变。"""
    cfg = make_cfg(tmp_path)
    frame, symbols = build_market()
    full = scan(frame, symbols, cfg, trade_date=TRADE_DATE).hits
    slim = scan(frame[list(SCAN_COLUMNS)], symbols, cfg, trade_date=TRADE_DATE).hits
    pd.testing.assert_frame_equal(full.reset_index(drop=True), slim.reset_index(drop=True))
