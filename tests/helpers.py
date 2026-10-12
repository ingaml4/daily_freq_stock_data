"""测试共用的构造工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd

from emscan.config import DEFAULTS, Config, deep_merge


def make_cfg(tmp_path: Path, overrides: Optional[Dict] = None) -> Config:
    data = deep_merge(DEFAULTS, overrides or {})
    return Config(data, root=Path(tmp_path))


def trading_dates(periods: int = 80, end: str = "2026-10-09") -> List[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(end=end, periods=periods)]


def bar(
    secid: str,
    date: str,
    volume: float,
    close: float = 10.0,
    pct_chg: float = 0.0,
    amount: Optional[float] = None,
    turnover: float = 2.0,
    float_mv: Optional[float] = None,
) -> Dict:
    return {
        "secid": secid,
        "date": date,
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": volume,
        "amount": volume * 100.0 * close if amount is None else amount,
        "amplitude": 2.0,
        "pct_chg": pct_chg,
        "chg": 0.0,
        "turnover": turnover,
        "float_mv": float_mv,
        "volume_ratio": None,
        "source": "test",
    }


def flat_history(
    secid: str,
    dates: Sequence[str],
    base_volume: float,
    close: float = 10.0,
    turnover: float = 2.0,
    wiggle: float = 0.05,
) -> List[Dict]:
    """生成一段基准期成交量（带极小波动，避免 MAD=0 干扰对量比的断言）。"""
    rows = []
    for i, date in enumerate(dates):
        factor = 1.0 + wiggle * (((i % 5) - 2) / 2.0)
        rows.append(bar(secid, date, base_volume * factor, close=close, turnover=turnover))
    return rows


def symbols_frame(entries: Sequence[Dict]) -> pd.DataFrame:
    return pd.DataFrame(list(entries))
