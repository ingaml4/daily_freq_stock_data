"""报告渲染测试（中文对齐、文件输出）。"""

import pandas as pd
from helpers import bar, flat_history, make_cfg, symbols_frame, trading_dates

from emscan.report import CONSOLE_COLUMNS, build_summary, display_width, render_table, write_files
from emscan.scan import ScanResult, scan


def _hit_frame():
    return pd.DataFrame(
        [
            {
                "secid": "1.600001",
                "code": "600001",
                "name": "贵州茅台",
                "board": "sh_main",
                "trade_date": "2026-10-09",
                "close": 1680.5,
                "pct_chg": 5.6,
                "amount": 5.3e9,
                "amount_yi": 53.0,
                "turnover": 1.23,
                "volume": 320000.0,
                "vol_ratio_5": 5.32,
                "vol_ratio_20": 3.1,
                "vol_ratio_60": 2.8,
                "vol_z60": 7.4,
                "spot_volume_ratio": None,
                "valid_bars": 60,
                "signal": "放量上攻",
                "score": 12.3,
            },
            {
                "secid": "0.300001",
                "code": "300001",
                "name": "ST测试",
                "board": "sz_gem",
                "trade_date": "2026-10-09",
                "close": 8.1,
                "pct_chg": -6.2,
                "amount": 1.2e8,
                "amount_yi": 1.2,
                "turnover": 0.9,
                "volume": 150000.0,
                "vol_ratio_5": 4.0,
                "vol_ratio_20": 2.2,
                "vol_ratio_60": 2.0,
                "vol_z60": 5.0,
                "spot_volume_ratio": None,
                "valid_bars": 59,
                "signal": "放量下跌",
                "score": 9.1,
            },
        ]
    )


def test_render_table_keeps_lines_aligned():
    text = render_table(_hit_frame())
    widths = {display_width(line) for line in text.splitlines()}
    assert len(widths) == 1, text
    assert "放量上攻" in text
    assert "沪主板" in text
    assert "7.4" in text


def test_render_table_handles_empty_and_missing_columns():
    assert render_table(pd.DataFrame()) == "（无命中）"
    frame = _hit_frame().copy()
    frame.loc[0, "vol_ratio_60"] = None
    text = render_table(frame)
    assert "-" in text
    partial = render_table(frame, columns=tuple(CONSOLE_COLUMNS) + ("不存在的列",))
    assert "代码" in partial


def test_build_summary_and_write_files(tmp_path):
    dates = trading_dates(70)
    secid = "1.600001"
    bars = flat_history(secid, dates[:-1], 1_000_000.0)
    bars.append(bar(secid, dates[-1], 5_000_000.0, pct_chg=6.0))
    symbols = symbols_frame(
        [
            {
                "secid": secid,
                "code": "600001",
                "name": "测试股",
                "market": 1,
                "board": "sh_main",
                "is_st": 0,
            }
        ]
    )
    cfg = make_cfg(tmp_path, {"store": {"reports_dir": str(tmp_path / "reports")}})
    result = scan(pd.DataFrame(bars), symbols, cfg, trade_date=dates[-1])

    summary = build_summary(result, cfg)
    assert dates[-1] in summary
    assert "命中" in summary

    files = write_files(result, cfg)
    names = sorted(p.name for p in files)
    assert names == [f"volscan_{dates[-1]}.csv", f"volscan_{dates[-1]}.xlsx"]
    assert all(p.exists() and p.stat().st_size > 0 for p in files)

    empty = ScanResult(dates[-1], pd.DataFrame(), {"universe": 0, "scanned": 0})
    assert "命中 0 只" in build_summary(empty, cfg)
