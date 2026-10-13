"""箱体震荡 + 换手健康（做 T 备选池）测试。"""

import pandas as pd
import pytest
from helpers import bar, make_cfg, trading_dates

from emscan.box import compute_box_features, scan_box
from emscan.cli import main, open_store

WINDOW = 40


def _series(
    secid, closes, turnover=3.0, amplitude=3.0, pct_chg=0.0, amount=1.0e8, end="2026-10-09"
):
    dates = trading_dates(len(closes), end=end)
    rows = []
    for date, close in zip(dates, closes):
        item = bar(secid, date, 100000.0, close=float(close), pct_chg=pct_chg, amount=amount)
        item["turnover"] = turnover
        item["amplitude"] = amplitude
        rows.append(item)
    return rows, dates


def _zigzag(n=WINDOW, low=10.0, high=10.8):
    """在 10~10.8 之间来回（箱体高度 8%）。"""
    return [low + (high - low) * (0.5 + 0.5 * (-1) ** i) for i in range(n)]


def test_box_detects_range_bound_with_healthy_turnover():
    rows, dates = _series("1.600001", _zigzag())
    features = compute_box_features(pd.DataFrame(rows))
    assert len(features) == 1
    row = features.iloc[0]
    assert row["s_box_range"] == 1
    assert row["box_days"] == WINDOW
    assert row["box_low"] == pytest.approx(10.0)
    assert row["box_high"] == pytest.approx(10.8)
    assert row["box_width"] == pytest.approx(0.08, abs=1e-6)
    assert 0 <= row["box_pos"] <= 1
    assert row["box_avg_turnover"] == pytest.approx(3.0)
    assert row["box_avg_amplitude"] == pytest.approx(3.0)


@pytest.mark.parametrize(
    "closes,turnover,amplitude,pct_chg,reason",
    [
        # 单边上涨 10 -> 20：净涨 100%，不是箱体
        ([10.0 + 0.25 * i for i in range(WINDOW)], 3.0, 3.0, 0.0, "趋势上涨"),
        # 单边下跌
        ([20.0 - 0.25 * i for i in range(WINDOW)], 3.0, 3.0, 0.0, "趋势下跌"),
        # 箱体太窄（10.00~10.05，高度 0.5%）：做不出差价
        ([10.0 + 0.05 * (i % 2) for i in range(WINDOW)], 3.0, 3.0, 0.0, "箱体过窄"),
        # 换手太低：流动性差
        (_zigzag(), 0.2, 3.0, 0.0, "换手过低"),
        # 换手太高：妖股博弈
        (_zigzag(), 25.0, 3.0, 0.0, "换手过高"),
        # 振幅太小：日内没波动，做不了 T
        (_zigzag(), 3.0, 0.8, 0.0, "振幅过小"),
        # 窗口里涨停太多（连板票）
        (_zigzag(), 3.0, 3.0, 10.0, "连续涨停"),
    ],
)
def test_box_rejects_bad_candidates(closes, turnover, amplitude, pct_chg, reason):
    rows, _ = _series("1.600001", closes, turnover=turnover, amplitude=amplitude, pct_chg=pct_chg)
    features = compute_box_features(pd.DataFrame(rows))
    assert features.iloc[0]["s_box_range"] == 0, reason


def test_box_rejects_st_and_short_history():
    rows, _ = _series("1.600001", _zigzag())
    frame = pd.DataFrame(rows)
    # ST 直接排除
    assert compute_box_features(frame, st_secids=["1.600001"]).iloc[0]["s_box_range"] == 0
    # 有效交易日不足
    short = frame.tail(10)
    features = compute_box_features(short)
    assert features.iloc[0]["s_box_range"] == 0 and features.iloc[0]["box_days"] == 10


def test_box_accepts_values_exactly_on_the_threshold():
    """阈值正好卡边界（浮点会算出 0.059999…）不能误杀：10.0~10.6 就是 6% 的箱子。"""
    closes = [10.0 + (0.6 if i % 2 else 0.0) for i in range(WINDOW)]
    rows, _ = _series("1.600001", closes, turnover=1.0, amplitude=2.0)
    row = compute_box_features(pd.DataFrame(rows)).iloc[0]
    assert row["box_width"] == pytest.approx(0.06, abs=1e-6)
    assert row["s_box_range"] == 1


def test_scan_box_joins_symbols_and_filters(tmp_path):
    cfg = make_cfg(tmp_path)
    store = open_store(cfg)
    good, _ = _series("1.600001", _zigzag())
    bad, _ = _series("0.300002", [10.0 + 0.25 * i for i in range(WINDOW)])
    store.upsert_bars(good + bad)
    store.upsert_symbols(
        [
            {
                "secid": "1.600001",
                "code": "600001",
                "name": "箱体票",
                "market": 1,
                "board": "sh_main",
            },
            {
                "secid": "0.300002",
                "code": "300002",
                "name": "趋势票",
                "market": 0,
                "board": "sz_gem",
            },
        ]
    )
    hits = scan_box(store, cfg)
    assert list(hits["code"]) == ["600001"]
    assert hits.iloc[0]["name"] == "箱体票"
    store.close()


def test_box_command_writes_cache(tmp_path):
    """`emscan box` 出报告，并把箱体特征写进每日信号缓存（网页第五个按钮就靠它）。"""
    store_path = tmp_path / "market.sqlite"
    cfg = make_cfg(
        tmp_path,
        {
            "store": {
                "path": str(store_path),
                "reports_dir": str(tmp_path / "reports"),
                "logs_dir": str(tmp_path / "logs"),
            }
        },
    )
    store = open_store(cfg)
    good, dates = _series("1.600001", _zigzag())
    store.upsert_bars(good)
    store.upsert_symbols(
        [
            {
                "secid": "1.600001",
                "code": "600001",
                "name": "箱体票",
                "market": 1,
                "board": "sh_main",
            }
        ]
    )
    store.close()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "store:\n"
        f"  path: {store_path}\n"
        f"  reports_dir: {tmp_path / 'reports'}\n"
        f"  logs_dir: {tmp_path / 'logs'}\n",
        encoding="utf-8",
    )
    assert main(["--config", str(config_path), "box"]) == 0
    assert (tmp_path / "reports" / f"box_{dates[-1]}.csv").exists()

    store = open_store(cfg)
    frame = store.load_daily_signals(start=dates[-1], end=dates[-1], join_symbols=True)
    store.close()
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["s_box_range"] == 1
    assert row["box_width"] == pytest.approx(0.08, abs=1e-6)
    assert "box_range" in str(row["signals"])
