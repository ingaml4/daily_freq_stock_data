"""低位缩量十字星（超跌企稳的观察信号）。

用 A 股技术分析里比较常见的口径，四个条件同时满足才算命中：

（当前默认口径）**大幅回撤 + 当天突然缩量 + 当日止跌**，三条同时满足即命中：

1. **大幅回撤**：距最近 ``drawdown_window``（默认 120）个交易日**高点**回撤 >= ``max_drawdown_from_high``（默认 20%）；
   （原来那套"60 日区间位置"改成可选，``use_range_position: true`` 才启用）
2. **突然缩量**：当日量 <= 之前 ``shrink_window``（默认 5）个有效交易日均量 × ``shrink_ratio``（默认 0.6）；
3. **当日止跌**：涨跌幅在 ``[min_pct_chg, max_pct_chg]`` 之间（默认 -1.5% ~ +3%，**微红、微跌都算止跌**，
   大涨不算——那是启动不是止跌），且收盘不在当日最低（``(收-低)/(高-低) >= close_off_low_ratio``，默认 0.35）。

形态（小实体/长下影）默认**不作为必选条件**（``require_shape: false``），需要时再打开。

（旧的严格口径，供参考）
1. **十字星形态**：实体极小——``|收-开|/前收 <= body_ratio``（默认 0.3%），
   并且有**上下影线**（``high > max(开,收)``、``low < min(开,收)``），
   影线长度至少是实体的 ``shadow_body_multiple`` 倍（默认 2 倍），
   也就是"多空打成平手、盘中来回拉锯"；
2. **低位**：收盘价处于最近 ``position_window``（默认 60）个交易日区间的下沿
   （``位置 = (收-min)/(max-min) <= max_position``，默认 0.30），
   或者距 60 日高点已回撤 ``max_drawdown_from_high``（默认 -20%）以上；
3. **缩量**：当日成交量 <= 之前 ``shrink_window``（默认 5）个有效交易日均量的
   ``shrink_ratio``（默认 0.6）倍——"地量"是低位十字星的关键；
4. **可交易/流动性**：当日成交额 >= ``min_amount``、换手率 <= ``max_turnover``、
   历史K线足够（``min_bars``）、非 ST。

输出（写进 ``daily_signals`` 缓存，网页/命令行共用）：
``doji_body``（实体占前收比例）、``doji_upper``/``doji_lower``（上下影线占前收比例）、
``doji_pos``（60 日区间位置 0~1）、``doji_drawdown``（距 60 日高点涨跌幅）、
``doji_vol_ratio``（当日量/前 5 日均量）+ 标记 ``s_low_doji``。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .store import Store

log = logging.getLogger("emscan")

DOJI_BAR_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "amount",
    "pct_chg",
)

DOJI_COLUMNS: tuple = (
    "doji_body",
    "doji_stop_pos",
    "doji_upper",
    "doji_lower",
    "doji_pos",
    "doji_drawdown",
    "doji_vol_ratio",
    "s_low_doji",
)


def _f(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def compute_doji_features(
    bars: pd.DataFrame,
    st_secids: Optional[Sequence[str]] = None,
    cfg=None,
) -> pd.DataFrame:
    """逐只票算"低位缩量十字星"特征 + ``s_low_doji`` 标记。"""
    conf = cfg.get if cfg is not None else (lambda key, default=None: default)
    body_ratio = float(conf("doji.body_ratio", 0.015))
    lower_shadow_ratio = float(conf("doji.lower_shadow_ratio", 0.01))
    require_shape = bool(conf("doji.require_shape", False))
    window = int(conf("doji.drawdown_window", 120) or 120)
    use_position = bool(conf("doji.use_range_position", False))
    min_pct = float(conf("doji.min_pct_chg", -1.5))
    max_pct = float(conf("doji.max_pct_chg", 3.0))
    close_off_low_ratio = float(conf("doji.close_off_low_ratio", 0.35))
    max_position = float(conf("doji.max_position", 0.30))
    max_drawdown = float(conf("doji.max_drawdown_from_high", -0.20))
    shrink_window = int(conf("doji.shrink_window", 5) or 5)
    shrink_ratio = float(conf("doji.shrink_ratio", 0.6))
    max_turnover = float(conf("doji.max_turnover", 5.0))
    min_amount = float(conf("doji.min_amount", 20_000_000))
    min_bars = int(conf("doji.min_bars", 45))
    exclude_st = bool(conf("universe.exclude_st", True))
    st = set(st_secids or [])

    if bars is None or bars.empty:
        return pd.DataFrame(columns=["secid"] + list(DOJI_COLUMNS))

    frame = bars.copy()
    frame["date"] = frame["date"].astype(str)
    for column in DOJI_BAR_COLUMNS:
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
    frame = frame[frame["volume"].fillna(0) > 0].sort_values(["secid", "date"])

    rows: List[Dict[str, Any]] = []
    for secid, group in frame.groupby("secid", sort=False):
        tail = group.tail(window)
        row: Dict[str, Any] = {"secid": str(secid), "s_low_doji": 0}
        for column in DOJI_COLUMNS:
            row.setdefault(column, None)
        # 要求整体历史够长（min_bars），并且窗口里至少 20 根用于判"低位"
        if len(group) < min_bars or len(tail) < min(20, window):
            rows.append(row)
            continue

        last = tail.iloc[-1]
        open_, high, low, close = (
            _f(last["open"]),
            _f(last["high"]),
            _f(last["low"]),
            _f(last["close"]),
        )
        prev_close = _f(tail["close"].iloc[-2]) if len(tail) >= 2 else None
        volume = _f(last["volume"])
        base = prev_close or open_
        if None in (open_, high, low, close) or not base or not high or not low:
            rows.append(row)
            continue

        body = abs(close - open_)
        upper = high - max(open_, close)
        lower = min(open_, close) - low
        row["doji_body"] = body / base
        row["doji_upper"] = upper / base
        row["doji_lower"] = lower / base
        row["doji_stop_pos"] = ((close - low) / (high - low)) if high > low else None

        # 低位：收盘在 60 日区间下沿，或距 60 日高点回撤够深
        rng_high, rng_low = float(np.nanmax(tail["high"])), float(np.nanmin(tail["low"]))
        span = rng_high - rng_low
        position = (close - rng_low) / span if span > 0 else None
        drawdown = (close / rng_high - 1.0) if rng_high else None
        row["doji_pos"] = position
        row["doji_drawdown"] = drawdown

        # 缩量：当日量 / 之前 N 个有效交易日均量
        prior = tail["volume"].iloc[-(shrink_window + 1) : -1]
        prior_mean = float(prior.mean()) if len(prior) else None
        vol_ratio = (volume / prior_mean) if (volume is not None and prior_mean) else None
        row["doji_vol_ratio"] = vol_ratio

        # 形态（默认不强制）：小实体 或 明显下影线
        shape_ok = (row["doji_body"] <= body_ratio) or (lower / base >= lower_shadow_ratio)
        # 低位：默认只看"距高点大幅回撤"；可选再叠加"区间位置"
        deep = drawdown is not None and drawdown <= max_drawdown
        low_ok = deep and (not use_position or (position is not None and position <= max_position))
        shrink_ok = vol_ratio is not None and vol_ratio <= shrink_ratio
        pct = _f(last["pct_chg"])
        # 止跌：微红/微跌都算，但大涨不算；且收盘别贴着当日最低
        stop_ok = (
            pct is not None
            and min_pct <= pct <= max_pct
            and row["doji_stop_pos"] is not None
            and row["doji_stop_pos"] >= close_off_low_ratio
        )
        turnover = _f(last["turnover"])
        amount = _f(last["amount"])
        row["s_low_doji"] = int(
            (shape_ok or not require_shape)
            and low_ok
            and shrink_ok
            and stop_ok
            and (turnover is None or turnover <= max_turnover)
            and (amount is None or amount >= min_amount)
            and not (exclude_st and str(secid) in st)
        )
        rows.append(row)
    return pd.DataFrame(rows)


def doji_params(cfg) -> Dict[str, float]:
    """报告里要展示的口径（顺手把窗口参数也带上）。"""
    keys = {
        "回撤窗口": ("doji.drawdown_window", 120),
        "高点回撤阈值": ("doji.max_drawdown_from_high", -0.20),
        "当日涨跌幅区间": ("doji.min_pct_chg", -1.5),
        "收盘位置下限": ("doji.close_off_low_ratio", 0.35),
        "缩量窗口": ("doji.shrink_window", 5),
        "缩量倍数": ("doji.shrink_ratio", 0.6),
        "换手率上限%": ("doji.max_turnover", 5.0),
    }
    return {name: cfg.get(key, default) for name, (key, default) in keys.items()}


def scan_doji(
    store: Store,
    cfg=None,
    trade_date: Optional[str] = None,
    with_symbols: bool = True,
) -> pd.DataFrame:
    """从库里读最近行情 -> 算十字星特征；默认只返回命中的（带代码/名称/板块）。"""
    conf = cfg.get if cfg is not None else (lambda key, default=None: default)
    window = int(conf("doji.position_window", 60) or 60)
    bars = store.load_recent_bars(calendar_days=window * 2 + 30, columns=DOJI_BAR_COLUMNS)
    if bars is not None and not bars.empty and trade_date:
        bars = bars[bars["date"].astype(str) <= str(trade_date)]
    symbols = store.symbols_df()
    st: List[str] = []
    if symbols is not None and not symbols.empty and "is_st" in symbols.columns:
        st = [str(s) for s in symbols.loc[symbols["is_st"] == 1, "secid"].tolist()]
    features = compute_doji_features(bars, st_secids=st, cfg=cfg)
    if features.empty:
        return features
    if with_symbols:
        latest = bars[bars["date"].astype(str) == str(trade_date)] if trade_date else bars
        keep = [
            c for c in ("close", "pct_chg", "volume", "amount", "turnover") if c in latest.columns
        ]
        if keep:
            day = latest[["secid"] + keep].drop_duplicates(subset=["secid"])
            features = features.merge(day, on="secid", how="left")
    return features
