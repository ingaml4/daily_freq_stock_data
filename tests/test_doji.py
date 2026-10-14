"""低位缩量十字星测试。"""

import pandas as pd
import pytest
from helpers import bar, make_cfg, trading_dates

from emscan.doji import compute_doji_features
from emscan.cli import main, open_store


def _market(last_body=0.0005, last_vol=40000.0, last_low_f=0.98, trend=-0.15, last_pct=0.0):
    dates = trading_dates(70, end="2026-10-13")
    rows = []
    for i, d in enumerate(dates[:-1]):
        c = 20.0 + trend * i
        it = bar("1.600001", d, 100000.0, close=c)
        it.update(open=c * 1.002, high=c * 1.01, low=c * 0.99, turnover=2.0, amount=5e7)
        rows.append(it)
    c = 20.0 + trend * (len(dates) - 1)
    last = bar("1.600001", dates[-1], last_vol, close=c)
    last.update(
        open=c * (1 + last_body), high=c * 1.02, low=c * last_low_f, turnover=1.0, amount=4e7
    )
    rows.append(last)
    return pd.DataFrame(rows), dates


def test_low_shrinking_doji_is_detected():
    frame, _ = _market()
    row = compute_doji_features(frame).iloc[0]
    assert row["s_low_doji"] == 1
    assert row["doji_pos"] < 0.3 and row["doji_vol_ratio"] < 0.6


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"last_pct": -6.0}, "当日大跌不算止跌"),
        ({"last_pct": 6.0}, "缩量微红算止跌、但大涨（启动）不算"),
        ({"last_vol": 200000.0}, "放量而不是缩量"),
    ],
)
def test_not_a_low_shrinking_doji(kwargs, reason):
    frame, _ = _market(**kwargs)
    assert compute_doji_features(frame).iloc[0]["s_low_doji"] == 0, reason


def test_high_position_is_not_low(tmp_path):
    frame, _ = _market(trend=0.15)  # 上升趋势 -> 位置高
    assert compute_doji_features(frame).iloc[0]["s_low_doji"] == 0


def test_doji_command_writes_cache(tmp_path):
    store_path = tmp_path / "market.sqlite"
    cfg = make_cfg(
        tmp_path,
        {
            "store": {
                "path": str(store_path),
                "reports_dir": str(tmp_path / "r"),
                "logs_dir": str(tmp_path / "l"),
            }
        },
    )
    store = open_store(cfg)
    frame, dates = _market()
    store.upsert_bars(frame.to_dict("records"))
    store.upsert_symbols(
        [{"secid": "1.600001", "code": "600001", "name": "十字星", "market": 1, "board": "sh_main"}]
    )
    store.close()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "store:\n  path: %s\n  reports_dir: %s\n  logs_dir: %s\n"
        % (store_path, tmp_path / "r", tmp_path / "l"),
        encoding="utf-8",
    )
    assert main(["--config", str(cfg_file), "doji"]) == 0
    store = open_store(cfg)
    cache = store.load_daily_signals(start=dates[-1], end=dates[-1], join_symbols=True)
    store.close()
    assert len(cache) == 1 and cache.iloc[0]["s_low_doji"] == 1
    assert "low_doji" in str(cache.iloc[0]["signals"])
