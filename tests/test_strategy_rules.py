import numpy as np
import pandas as pd
import pytest

from emscan import signals
from emscan.indicators import compute_frame
from emscan.strategy_rules import _sectors, strategy_candidates


DAYS = pd.bdate_range("2025-01-02", periods=128).strftime("%Y-%m-%d").tolist()
CODE = "1.600001"


def frame(prices, code=CODE):
    close = np.asarray(prices, dtype=float)
    return pd.DataFrame(
        {
            "date": DAYS[: len(close)],
            "secid": code,
            "open": close - 0.04,
            "high": close + 0.1,
            "low": close - 0.15,
            "close": close,
            "volume": 1000.0,
            "amount": 1e8,
            "turnover": 1.0,
            "pct_chg": pd.Series(close).pct_change() * 100,
            "source": "spot",
        }
    )


def trend_sample():
    data = frame(list(np.linspace(10, 14, 119)) + [15.0, 15.5, 16.0])
    data.loc[119, ["volume", "pct_chg"]] = [2100, 7.0]
    data.loc[119, "high"] = data.loc[119, "close"] + 0.025
    return data


def pick(data, **kwargs):
    return strategy_candidates(data, {CODE: "样本"}, **kwargs)


def test_trend_breakout_and_baseline_original_scan_filters():
    data = trend_sample()
    day = DAYS[119]
    got = pick(data, start=day, asof=day).set_index("strategy")
    row = got.loc["trend_breakout"]
    assert row.trigger_price == pytest.approx(data.iloc[99:119].high.max())
    assert row.invalidation_price == pytest.approx(data.iloc[114:119].low.min())
    assert row.invalidation_basis == "spot"
    assert row.history_valid_bars == 120 and row.point_in_time
    assert row.indicator_asof == day and row.indicator_basis == "spot"
    assert "放量上攻" in got.loc["baseline_core", "reason"]
    assert pd.isna(got.loc["baseline_core", "invalidation_price"])
    changed = data.copy()
    changed.loc[114, "source"] = "kline"
    changed.loc[119, "turnover"] = 0.49
    got = pick(changed, asof=day)
    assert got.query("strategy == 'trend_breakout'").iloc[-1].invalidation_basis == "qfq_or_mixed"
    assert set(got.indicator_basis) == {"mixed"}
    assert ~got.reason.str.contains("放量上攻").any()
    changed.loc[119, "volume"] = 1499
    assert "trend_breakout" not in set(pick(changed, asof=day).strategy)


def test_indicator_baseline_uses_original_rule_masks_and_score():
    data = frame(11 + np.cos(np.arange(127) * 0.9))
    actual = pick(data)
    ind = compute_frame(data)
    ind["close"] = data.close
    flags = pd.DataFrame(
        {
            key: signals._rule_mask(key, ind, ind.shift(), ind.index)
            for key in ("macd_golden", "kdj_golden", "boll_break_lower")
        }
    )
    scores = signals._score(
        pd.DataFrame(
            {
                "rule_count": flags.sum(axis=1),
                "boll_pos": (data.close - ind.boll_lower) / (ind.boll_upper - ind.boll_lower),
                "amount": data.amount,
            }
        )
    )
    expected = flags.any(axis=1) & (flags.index >= 119)
    assert list(actual.query("strategy=='baseline_core'").date) == list(data.date[expected])
    assert list(actual.query("strategy=='baseline_core'").score) == list(scores[expected])


def test_reversal_first_confirmation_known_stop_prefix_and_mixed():
    data = frame([10.0] * 119 + [8.0, 8.15, 9.3, 9.8, 10.0, 10.1])
    data.loc[119, "high"] = 8.4
    data.loc[120, ["low", "source"]] = [7.4, "kline"]
    got = pick(data)
    setups = got.query("strategy=='reversal_confirmed'")
    assert list(setups.date) == [DAYS[121]]
    row = setups.iloc[0]
    assert row.trigger_price == pytest.approx(8.25) and row.invalidation_price == pytest.approx(7.4)
    assert row.invalidation_basis == "qfq_or_mixed"
    cutoff = DAYS[121]
    sliced = pick(data.iloc[:122], start=DAYS[120])
    pd.testing.assert_frame_equal(
        sliced, got.loc[got.date.between(DAYS[120], cutoff)].reset_index(drop=True)
    )
    changed = data.copy()
    changed.loc[122:, ["low", "high"]] = [1.0, 500.0]
    pd.testing.assert_frame_equal(sliced, pick(changed, start=DAYS[120], asof=cutoff))


def test_universe_and_warmup_exclude_missing_suspended_and_invalid_bars():
    codes = [CODE, "1.688001", "0.002001", "0.300001", "1.900101", "1.600002", "0.399001"]
    data = pd.concat([trend_sample().assign(secid=c) for c in codes], ignore_index=True)
    names = {c: ("ST公司" if c == "0.002001" else c) for c in codes if c != "1.600002"}
    data.loc[(data.secid == CODE) & (data.date == DAYS[44]), "volume"] = 0
    actual = strategy_candidates(data, names, asof=DAYS[119])
    assert set(actual.secid) == {"0.300001"}
    with pytest.raises(ValueError, match="重复"):
        pick(pd.concat([trend_sample(), trend_sample().iloc[[-1]]], ignore_index=True))


def test_sector_equal_weight_loo_and_daily_cross_section_for_stock_rank():
    tags = {f"id{i}": sector for i, sector in enumerate(["甲"] * 4 + ["乙"] * 4 + ["丙"] * 2)}
    meta = pd.Series(tags, name="sector").to_frame()
    returns = pd.DataFrame(
        {
            "date": ["d"] * 10,
            "secid": list(tags),
            "r5": [12, 2, 2, 2, 4, 4, 4, 4, 1, 1],
            "r20": [17, 3, 3, 3, 2, 2, 2, 2, 1, 1],
        }
    )
    picks = pd.DataFrame({"date": ["d"] * 4, "secid": ["id0", "id1", "id8", "missing"]})
    got = _sectors(picks, returns, meta)
    row = got.iloc[0]
    assert row.sector_return_5d == 2 and row.relative_strength_5d == 10
    assert row.sector_return_20d == 3 and row.relative_strength_20d == 14
    assert row.sector_percentile_5d == 0 and row.sector_percentile_20d == 1
    assert row.relative_percentile_5d == row.relative_percentile_20d == 1
    assert row.sector_rank_score == 0.75 and row.rank_quality == "exploratory_current_membership"
    assert row.peer_count_5d == 3 and row.peer_coverage_5d == 1
    assert got.iloc[1].sector_return_5d == pytest.approx(16 / 3)
    assert (
        pd.isna(got.iloc[2].sector_rank_score)
        and got.iloc[2].rank_quality == "insufficient_peers_or_coverage"
    )
    assert pd.isna(got.iloc[3].sector_rank_score) and got.iloc[3].rank_quality == "missing_sector"
    returns.loc[1, "r20"] = np.nan
    broken = _sectors(picks, returns, meta)
    assert broken.iloc[0].peer_count_5d == 2 and pd.isna(broken.iloc[0].sector_return_5d)
    assert broken.iloc[1].rank_quality == "missing_stock_return"


def test_sector_no_return_for_suspension_inside_market_window():
    codes = [f"1.60000{i}" for i in range(1, 10)]
    tags = {c: ("甲" if i < 5 else "乙") for i, c in enumerate(codes)}
    data = pd.concat([trend_sample().assign(secid=c) for c in codes], ignore_index=True)
    # 仍有120根有效日线并触发突破，但20日窗口内缺一天，因此不可行业排序。
    data.loc[data.date == DAYS[121], ["close", "high", "open", "low", "volume", "pct_chg"]] = [
        18,
        18.02,
        17.5,
        17.0,
        2500,
        12,
    ]
    data.loc[(data.secid == CODE) & (data.date == DAYS[110]), "volume"] = 0
    got = strategy_candidates(data, {c: c for c in codes}, tags, start=DAYS[121])
    assert got.loc[got.secid == CODE, "rank_quality"].eq("missing_stock_return").all()
    assert not got.loc[got.secid == CODE].empty
    assert got.loc[got.secid == codes[1], "peer_count_20d"].eq(3).all()
