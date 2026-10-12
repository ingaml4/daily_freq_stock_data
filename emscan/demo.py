"""合成一段"假行情"，用来在无网络环境里端到端验证整条流水线。

生成的数据刻意包含几类边界样本：

* 真正的放量（应命中）
* ST 股放量（应被剔除）
* 上市不足 min_bars 天的新股放量（应被剔除）
* 当日停牌（应被剔除）
* 成交额过小的放量（应被剔除）
* 温和放量 1.6 倍（低于 2.0 阈值，不应命中）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .store import Store

log = logging.getLogger(__name__)

BOARD_CYCLE = ["sh_main", "sz_main", "sz_gem", "sh_star"]
BOARD_PREFIX = {
    "sh_main": ("600", 1),
    "sz_main": ("000", 0),
    "sz_gem": ("300", 0),
    "sh_star": ("688", 1),
}


def synthesize(
    store: Store,
    n_symbols: int = 300,
    days: int = 220,
    n_up: int = 8,
    n_down: int = 2,
    n_flat: int = 1,
    seed: int = 11,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """生成合成行情并写入 store，返回注入样本的清单。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(
        end=pd.Timestamp(end_date) if end_date else pd.Timestamp.today().normalize(), periods=days
    )
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    trade_date = date_strs[-1]

    symbols: List[Dict[str, Any]] = []
    bars: List[Dict[str, Any]] = []

    special = {
        "st_spike": 0,
        "short_history_spike": 1,
        "suspended_spike": 2,
        "tiny_amount_spike": 3,
    }
    targets: List[str] = []
    noise: List[str] = []

    def make_code(index: int, board: str) -> str:
        prefix, _ = BOARD_PREFIX[board]
        return f"{prefix}{index + 1:03d}"

    for index in range(n_symbols):
        board = BOARD_CYCLE[index % len(BOARD_CYCLE)]
        prefix, market = BOARD_PREFIX[board]
        code = make_code(index, board)
        secid = f"{market}.{code}"
        if index == special["st_spike"]:
            name = f"ST测试{code[-3:]}"
        else:
            name = f"测试{code[-3:]}"

        history_len = days
        if index == special["short_history_spike"]:
            history_len = 8  # 上市不足 min_bars 个交易日

        base_volume = float(np.exp(rng.normal(12.0, 0.8)))  # 单位：手
        if index == special["tiny_amount_spike"]:
            base_volume = 300.0  # 成交额永远过不了门槛
        price = float(np.exp(rng.normal(2.7, 0.4)))  # 约 5~50 元
        # 流通股本按成交量量级生成，使换手率落在合理区间（约 1%~30%）
        float_shares = base_volume * 100.0 * float(np.exp(rng.normal(3.0, 0.5)))

        # 当日放量倍数
        multiplier = float(np.clip(np.exp(rng.normal(0.0, 0.22)), 0.4, 1.9))
        last_pct = float(np.clip(rng.normal(0.0, 1.5), -6, 6))
        if index in (
            special["st_spike"],
            special["short_history_spike"],
            special["suspended_spike"],
            special["tiny_amount_spike"],
        ):
            multiplier = 0.0  # 稍后按类别单独设置
        elif len(targets) < n_up:
            multiplier = float(rng.uniform(3.0, 12.0))
            last_pct = float(rng.uniform(3.0, 9.8))
            targets.append(secid)
        elif len(targets) < n_up + n_down:
            multiplier = float(rng.uniform(3.0, 8.0))
            last_pct = float(rng.uniform(-9.0, -3.0))
            targets.append(secid)
        elif len(targets) < n_up + n_down + n_flat:
            multiplier = float(rng.uniform(2.6, 5.0))
            last_pct = float(rng.uniform(-0.8, 0.8))
            targets.append(secid)
        elif len(noise) < 6:
            multiplier = 1.6  # 温和放量，不应命中
            last_pct = float(rng.uniform(1.0, 3.0))
            noise.append(secid)

        if index == special["st_spike"]:
            multiplier, last_pct = 5.0, 4.9
        if index == special["short_history_spike"]:
            multiplier, last_pct = 6.0, 5.0
        if index == special["suspended_spike"]:
            multiplier, last_pct = 0.0, 0.0
        if index == special["tiny_amount_spike"]:
            multiplier, last_pct = 20.0, 3.0

        symbols.append(
            {
                "secid": secid,
                "code": code,
                "name": name,
                "market": market,
                "board": board,
                "is_st": 1 if index == special["st_spike"] else 0,
            }
        )

        start = len(date_strs) - history_len
        for offset, date in enumerate(date_strs[start:]):
            is_last = offset == (history_len - 1)
            close = price * float(np.exp(rng.normal(0.0, 0.005) * offset))
            if is_last:
                volume = base_volume * multiplier
                pct_chg = last_pct
            else:
                volume = base_volume * float(np.clip(np.exp(rng.normal(0.0, 0.22)), 0.4, 1.9))
                pct_chg = float(np.clip(rng.normal(0.0, 1.5), -6, 6))
                close = close * (1 + pct_chg / 100.0)
            amount = volume * 100.0 * close
            bars.append(
                {
                    "secid": secid,
                    "date": date,
                    "open": round(close * (1 - pct_chg / 200), 2),
                    "high": round(close * 1.01, 2),
                    "low": round(close * 0.99, 2),
                    "close": round(close, 2),
                    "volume": round(volume, 2),
                    "amount": round(amount, 2),
                    "amplitude": round(abs(pct_chg) + 1.0, 2),
                    "pct_chg": round(pct_chg, 2),
                    "chg": round(close * pct_chg / 100.0, 2),
                    "turnover": round(volume * 100.0 / float_shares * 100.0, 4),
                    "float_mv": round(close * float_shares, 2),
                    "volume_ratio": None,
                    "source": "demo",
                }
            )

    store.init_schema()
    store.upsert_symbols(symbols)
    for i in range(0, len(bars), 20000):
        store.upsert_bars(bars[i : i + 20000])

    return {
        "trade_date": trade_date,
        "dates": date_strs,
        "symbols": len(symbols),
        "bars": len(bars),
        "targets": targets,
        "noise": noise,
        "st_spike": symbols[special["st_spike"]]["secid"],
        "short_history_spike": symbols[special["short_history_spike"]]["secid"],
        "suspended_spike": symbols[special["suspended_spike"]]["secid"],
        "tiny_amount_spike": symbols[special["tiny_amount_spike"]]["secid"],
    }
