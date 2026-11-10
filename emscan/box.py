"""放量突破箱体上沿。箱体边界只使用信号日前的市场窗口。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from pathlib import Path

from .config import Config, DEFAULTS
from .scan import scan
from .store import Store

log = logging.getLogger("emscan")

DEFAULT_WINDOW = 40
BOX_RULE_VERSION = "volume_breakout_v1"

# 浮点容差：阈值正好卡在边界时（例如箱体高度正好 6.000% 算出 0.05999999999999996）别误杀
EPS = 1e-9

# 参与计算的行情列（按列裁剪，省内存）
BOX_BAR_COLUMNS = (
    "close",
    "high",
    "low",
    "turnover",
    "amount",
    "amplitude",
    "pct_chg",
    "volume",
    "float_mv",
    "volume_ratio",
)

# 写进每日缓存的列
BOX_COLUMNS: tuple = (
    "box_rule_version",
    "box_breakout_pct",
    "box_high",
    "box_low",
    "box_width",
    "box_pos",
    "box_net_chg",
    "box_ma_gap",
    "box_avg_turnover",
    "box_turnover_cv",
    "box_avg_amplitude",
    "box_extreme_days",
    "box_days",
    "s_box_range",
)


def _between(value: Optional[float], low: float, high: float) -> bool:
    """``low <= value <= high``（带浮点容差）。"""
    return value is not None and low - EPS <= value <= high + EPS


def _f(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def compute_box_features(
    bars: pd.DataFrame,
    st_secids: Optional[Sequence[str]] = None,
    cfg=None,
    window: Optional[int] = None,
    trade_date: Optional[str] = None,
    market_days: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    """返回每只股票的历史箱体和当日突破。旧标记名 s_box_range 保持兼容。

    D日前最多window个市场交易日构成箱体，不能用D日新高抬高箱顶。
    market_days由Store提供；纯计算调用省略时使用输入中全部日期的并集。
    """
    config = cfg if cfg is not None else Config(DEFAULTS, Path("."))
    conf = config.get
    win = int(window or conf("box.window", DEFAULT_WINDOW) or DEFAULT_WINDOW)
    min_valid = int(conf("box.min_valid_days", max(5, win - 5)))
    columns = ["secid"] + list(BOX_COLUMNS) + ["vol_ratio_5", "vol_ratio_20", "vol_z60"]
    if bars is None or bars.empty:
        return pd.DataFrame(columns=columns)
    frame = bars.copy()
    frame["date"] = frame["date"].astype(str)
    day = str(trade_date or frame["date"].max())
    frame = frame[frame["date"] <= day]
    calendar = sorted(
        {
            str(d)
            for d in (market_days if market_days is not None else frame["date"])
            if str(d) <= day
        }
    )
    preceding = [d for d in calendar if d < day][-win:]
    previous_day = preceding[-1] if preceding else None
    for column in BOX_BAR_COLUMNS:
        frame[column] = (
            pd.to_numeric(frame.get(column), errors="coerce").replace([np.inf, -np.inf], np.nan)
            if column in frame
            else np.nan
        )
    active = frame[(frame["volume"] > 0) & (frame["close"] > 0)].sort_values(["secid", "date"])
    if active.empty or not (active["date"] == day).any():
        return pd.DataFrame(columns=columns)
    # 原放量扫描统一执行：比较期均量排除D日，阈值和样本门槛与放量视图一致。
    metrics = scan(active, None, config, trade_date=day, apply_universe_filter=False).all_metrics
    volume_by_sid = {} if metrics is None else metrics.set_index("secid").to_dict("index")
    excluded = set(st_secids or [])
    rows = []
    for secid, group in active.groupby("secid", sort=False):
        today = group[group["date"] == day]
        history = group[group["date"].isin(preceding)]
        row = dict.fromkeys(BOX_COLUMNS)
        row.update(
            secid=str(secid),
            box_rule_version=BOX_RULE_VERSION,
            box_days=len(history),
            s_box_range=0,
        )
        volume = volume_by_sid.get(secid, {})
        for key in ("vol_ratio_5", "vol_ratio_20", "vol_z60"):
            row[key] = volume.get(key)
        rows.append(row)
        if today.empty or len(history) < min_valid:
            continue
        # 复牌前的最后报价不能与D拼成连续突破，历史窗口也不向更早日期借样本。
        if history["date"].iloc[-1] != previous_day:
            continue
        price = history[["high", "low", "close"]]
        if not np.isfinite(price.to_numpy()).all() or (price <= 0).any().any():
            continue
        high = float(history["high"].max())
        low = float(history["low"].min())
        last = float(today["close"].iloc[-1])
        width = (high - low) / low
        close = history["close"].to_numpy(dtype=float)
        net = close[-1] / close[0] - 1
        ma5, ma20 = np.mean(close[-5:]), np.mean(close[-20:])
        ma_gap = abs(ma5 - ma20) / ma20
        turnover = history["turnover"].to_numpy(dtype=float)
        amplitude = history["amplitude"].to_numpy(dtype=float)
        pct = history["pct_chg"].to_numpy(dtype=float)
        avg_turnover = float(np.nanmean(turnover)) if np.isfinite(turnover).all() else None
        avg_amp = float(np.nanmean(amplitude)) if np.isfinite(amplitude).all() else None
        cv = float(np.std(turnover) / avg_turnover) if avg_turnover else None
        # 创业板按20%制度的极端日阈值，主板按10%制度，保留原配置可调幅度。
        extreme_threshold = 19.5 if str(secid).startswith(("0.300", "0.301")) else 9.5
        extremes = int(np.count_nonzero(np.abs(pct) >= extreme_threshold))
        row.update(
            box_high=high,
            box_low=low,
            box_width=width,
            box_pos=(last - low) / (high - low) if high > low else None,
            box_breakout_pct=last / high - 1,
            box_net_chg=net,
            box_ma_gap=ma_gap,
            box_avg_turnover=avg_turnover,
            box_turnover_cv=cv,
            box_avg_amplitude=avg_amp,
            box_extreme_days=extremes,
        )
        amount = _f(today["amount"].iloc[-1])
        row["s_box_range"] = int(
            _between(width, float(conf("box.min_width", 0.06)), float(conf("box.max_width", 0.35)))
            and abs(net) <= abs(float(conf("box.max_net_chg", 0.12))) + EPS
            and ma_gap <= abs(float(conf("box.max_ma_gap", 0.04))) + EPS
            and _between(
                avg_turnover,
                float(conf("box.min_avg_turnover", 1)),
                float(conf("box.max_avg_turnover", 15)),
            )
            and cv is not None
            and cv <= float(conf("box.max_turnover_cv", 1)) + EPS
            and _between(
                avg_amp,
                float(conf("box.min_avg_amplitude", 2)),
                float(conf("box.max_avg_amplitude", 9)),
            )
            and extremes <= int(conf("box.max_extreme_days", 2))
            and amount is not None
            and amount >= float(conf("box.min_amount", 30000000))
            and last > high + EPS * max(1.0, high)
            and volume.get("s_vol_spike") == 1
            and not (bool(conf("universe.exclude_st", True)) and str(secid) in excluded)
            and not str(secid).startswith(("1.688", "1.689"))
        )
    return pd.DataFrame(rows, columns=columns)


def box_features(
    store: Store,
    cfg=None,
    trade_date: Optional[str] = None,
) -> pd.DataFrame:
    """从库里读最近行情 -> 算箱体特征（一只票一行，含未命中的）。"""
    conf = cfg.get if cfg is not None else (lambda key, default=None: default)
    window = int(conf("box.window", DEFAULT_WINDOW) or DEFAULT_WINDOW)
    calendar_days = int(max(window, int(conf("scan.baseline_window", 60))) * 2 + 30)
    trade_date = trade_date or store.latest_date()
    bars = store.load_recent_bars(
        calendar_days, BOX_BAR_COLUMNS, end=trade_date, require_active=True
    )
    symbols = store.symbols_df()
    st_secids: List[str] = []
    if symbols is not None and not symbols.empty and "is_st" in symbols.columns:
        st_secids = [str(s) for s in symbols.loc[symbols["is_st"] == 1, "secid"].tolist()]
    features = compute_box_features(
        bars,
        st_secids=st_secids,
        cfg=cfg,
        window=window,
        trade_date=trade_date,
        market_days=store.all_trading_days(),
    )
    if features.empty:
        return features
    latest = bars[bars["date"] == trade_date]
    features = features.merge(
        latest[["secid", "close", "volume", "amount", "pct_chg", "turnover"]],
        on="secid",
        how="inner",
    )
    if not symbols.empty:
        features = features.merge(
            symbols[["secid", "code", "name", "board"]], on="secid", how="left"
        )
    return features


def scan_box(store: Store, cfg=None, trade_date=None, min_bars=None) -> pd.DataFrame:
    features = box_features(store, cfg, trade_date)
    return features[features["s_box_range"] == 1].copy() if not features.empty else features
