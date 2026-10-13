"""本地指标计算测试：公式对拍 + 落库 + 与信号筛选联动。"""

import numpy as np
import pandas as pd
import pytest
from helpers import bar, make_cfg, trading_dates

from emscan.cli import open_store
from emscan.indicators import compute_and_store, compute_frame
from emscan.pipeline import SyncStats, sync_indicators
from emscan.signals import detect


def _bars_frame(closes, start="2026-01-05"):
    dates = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame(
        {
            "date": dates,
            "close": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
        }
    )


def test_compute_frame_matches_hand_math():
    closes = list(np.linspace(10.0, 20.0, 120))
    frame = _bars_frame(closes).set_index("date")
    out = compute_frame(_bars_frame(closes)).set_index("date")
    close = pd.Series(closes, index=frame.index)

    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    assert out["macd_diff"].iloc[-1] == pytest.approx(float(dif.iloc[-1]), abs=1e-9)
    assert out["macd_dea"].iloc[-1] == pytest.approx(float(dea.iloc[-1]), abs=1e-9)
    assert out["macd_bar"].iloc[-1] == pytest.approx(
        float(2 * (dif.iloc[-1] - dea.iloc[-1])), abs=1e-9
    )
    assert out["ma5"].iloc[-1] == pytest.approx(float(close.tail(5).mean()), abs=1e-9)
    assert out["ma20"].iloc[-1] == pytest.approx(float(close.tail(20).mean()), abs=1e-9)
    assert out["boll_mid"].iloc[-1] == pytest.approx(float(close.tail(20).mean()), abs=1e-9)
    assert out["boll_upper"].iloc[-1] == pytest.approx(
        float(close.tail(20).mean() + 2 * close.tail(20).std(ddof=0)), abs=1e-9
    )
    # KDJ 在 0~100 之间（J 可以越界），K/D 在合理区间
    assert 0 <= out["kdj_k"].iloc[-1] <= 100 and 0 <= out["kdj_d"].iloc[-1] <= 100


def test_compute_frame_flat_series_is_neutral():
    out = compute_frame(_bars_frame([10.0] * 60)).iloc[-1]
    assert out["macd_diff"] == 0 and out["macd_dea"] == 0 and out["macd_bar"] == 0
    assert out["boll_upper"] == out["boll_mid"] == out["boll_lower"] == 10.0
    assert out["kdj_k"] == 50.0 and out["kdj_d"] == 50.0


def test_compute_frame_flat_series_marks_nothing_odd():
    """横盘序列不产生金叉/跌破：K=D=50、上下轨=中轨=收盘价。"""
    out = compute_frame(_bars_frame([10.0] * 30)).iloc[-1]
    assert out["kdj_j"] == 50.0 and out["boll_lower"] == out["boll_upper"] == 10.0


def test_compute_and_store_only_writes_requested_dates(tmp_path):
    cfg = make_cfg(tmp_path)
    store = open_store(cfg)
    dates = trading_dates(40, end="2026-10-09")
    rows = [bar("1.600001", d, 1000.0, close=10.0 + i * 0.05) for i, d in enumerate(dates)]
    store.upsert_bars(rows)
    written = compute_and_store(store, ["1.600001"], cfg=cfg, dates=dates[-3:])
    assert written > 0
    frame = store.load_indicators()
    assert set(frame["date"]) == set(dates[-3:])
    assert set(frame["source"]) == {"local"}
    # 最后一行的 DIFF/DEA 是有限值（有预热数据，不是从 0 开始瞎算）
    last = frame[frame["date"] == dates[-1]].iloc[0]
    assert last["macd_diff"] != 0 and last["boll_mid"] > 0
    store.close()


def test_sync_indicators_local_needs_no_client(tmp_path):
    """source=local 时不碰接口：连 client 都可以是 None。"""
    cfg = make_cfg(tmp_path)
    store = open_store(cfg)
    dates = trading_dates(30, end="2026-10-09")
    store.upsert_bars([bar("1.600001", d, 1000.0, close=12.0) for d in dates])
    stats = SyncStats()
    written = sync_indicators(store, None, cfg, stats, ["1.600001"], dates, ["macd", "kdj", "boll"])
    assert written > 0
    assert store.indicator_dates_by_secid("macd")["1.600001"] == set(dates)
    assert stats.errors == []

    # 再跑一次：老股票只重算最近几天（daily 增量），写入量明显小于整窗口
    again = sync_indicators(store, None, cfg, stats, ["1.600001"], dates, ["macd", "kdj", "boll"])
    assert 0 < again < written
    store.close()


def test_local_indicators_drive_signals(tmp_path):
    """本地算出来的指标能直接喂给信号筛选（金叉/跌破下轨）。"""
    cfg = make_cfg(tmp_path)
    store = open_store(cfg)
    dates = trading_dates(60, end="2026-10-09")
    # 先横盘，最后一天放量砸穿下轨（close 8.0 / low 7.5，远低于 20 日均值 10 附近的下轨）
    closes = [10.0] * 58 + [10.0, 8.0]
    rows = []
    for date, close in zip(dates, closes):
        item = bar("1.600001", date, 1000.0, close=close)
        item["high"] = close * 1.02
        item["low"] = 7.5 if date == dates[-1] else close * 0.99
        rows.append(item)
    store.upsert_bars(rows)
    store.upsert_symbols(
        [
            {
                "secid": "1.600001",
                "code": "600001",
                "name": "测试",
                "market": 1,
                "board": "sh_main",
            }
        ]
    )
    sync_indicators(store, None, cfg, SyncStats(), ["1.600001"], dates, ["macd", "kdj", "boll"])
    bars = store.load_recent_bars(calendar_days=200, columns=("close", "high", "low", "volume"))
    result = detect(
        bars,
        store.load_indicators(),
        store.symbols_df(),
        cfg,
        trade_date=dates[-1],
        rules=["boll_lower"],
    )
    assert not result.hits.empty  # 最后一天最低价 7.5 明显低于下轨
    assert result.hits.iloc[0]["signals"] == "boll_lower"
    assert result.hits.iloc[0]["boll_pos"] < 0
    store.close()
