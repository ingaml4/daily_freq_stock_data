"""箱体震荡 + 换手健康（做 T 备选池）。

做 T（高抛低吸）想要的不是趋势股，而是**在一个箱体里来回磨、且换手健康**的票：

* **箱体**：最近 ``window`` 个交易日（默认 40 ≈ 2 个月）里，收盘价只在
  ``[箱底, 箱顶]`` 之间来回；窗口相对高度落在 ``[min_width, max_width]``
  （太窄做不出差价，太宽就是过山车不是箱体）；
* **不是趋势**：窗口内净涨跌幅 ``|net_chg| <= max_net_chg``，且 MA5 与 MA20 靠得近
  （``|MA5-MA20|/MA20 <= max_ma_gap``）——均线缠绕才是震荡；
* **换手健康**：窗口内日均换手率在 ``[min_avg_turnover, max_avg_turnover]`` 之间
  （太低流动性差、太高是妖股博弈），且换手率稳定（变异系数 ``<= max_turnover_cv``）；
* **有振幅可做**：窗口内日均振幅在 ``[min_avg_amplitude, max_avg_amplitude]``；
* **别太刺激**：窗口内 ``|涨跌幅| >= 9.5%`` 的天数不超过 ``max_extreme_days``（连板票不适合做 T）；
* **流动性**：当日成交额 >= ``min_amount``；ST/停牌、有效交易日不足的直接排除。

输出指标（写进 ``daily_signals`` 缓存，网页/命令行共用）：
``box_high/box_low/box_width/box_pos/box_net_chg/box_ma_gap/box_avg_turnover/
box_turnover_cv/box_avg_amplitude/box_extreme_days/box_days`` + 标记 ``s_box_range``。
``box_pos`` 是当日收盘在箱体里的位置（0=贴箱底、1=贴箱顶），做 T 视角下越靠箱底越有吸引力。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .store import Store

log = logging.getLogger("emscan")

DEFAULT_WINDOW = 40

# 浮点容差：阈值正好卡在边界时（例如箱体高度正好 6.000% 算出 0.05999999999999996）别误杀
EPS = 1e-9

# 参与计算的行情列（按列裁剪，省内存）
BOX_BAR_COLUMNS = ("close", "high", "low", "turnover", "amount", "amplitude", "pct_chg", "volume")

# 写进每日缓存的列
BOX_COLUMNS: tuple = (
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
) -> pd.DataFrame:
    """按股票算箱体特征 + ``s_box_range`` 标记。

    ``bars`` 是长表（secid/date/close/...），返回一只票一行（列见 ``BOX_COLUMNS``）。
    """
    conf = cfg.get if cfg is not None else (lambda key, default=None: default)
    win = int(window or conf("box.window", DEFAULT_WINDOW) or DEFAULT_WINDOW)
    min_width = float(conf("box.min_width", 0.06))
    max_width = float(conf("box.max_width", 0.35))
    max_net = abs(float(conf("box.max_net_chg", 0.12)))
    max_gap = abs(float(conf("box.max_ma_gap", 0.04)))
    min_turnover = float(conf("box.min_avg_turnover", 1.0))
    max_turnover = float(conf("box.max_avg_turnover", 15.0))
    max_cv = float(conf("box.max_turnover_cv", 1.0))
    min_amp = float(conf("box.min_avg_amplitude", 2.0))
    max_amp = float(conf("box.max_avg_amplitude", 9.0))
    max_extreme = int(conf("box.max_extreme_days", 2))
    min_amount = float(conf("box.min_amount", 30_000_000))
    min_valid = int(conf("box.min_valid_days", max(5, win - 5)))
    exclude_st = bool(conf("universe.exclude_st", True))
    st = set(st_secids or [])

    if bars is None or bars.empty:
        return pd.DataFrame(columns=["secid"] + list(BOX_COLUMNS))

    frame = bars.copy()
    frame["date"] = frame["date"].astype(str)
    for column in ("close", "high", "low", "turnover", "amount", "amplitude", "pct_chg", "volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        else:
            frame[column] = np.nan
    frame = frame[frame["volume"].fillna(0) > 0]  # 停牌日不参与
    frame = frame.sort_values(["secid", "date"])

    rows: List[Dict[str, Any]] = []
    for secid, group in frame.groupby("secid", sort=False):
        tail = group.tail(win)
        days = int(len(tail))
        row: Dict[str, Any] = {"secid": str(secid), "box_days": days, "s_box_range": 0}
        for column in BOX_COLUMNS:
            row.setdefault(column, None)
        if days < min_valid:
            rows.append(row)
            continue

        close = tail["close"].to_numpy(dtype=float)
        turnover = tail["turnover"].to_numpy(dtype=float)
        amplitude = tail["amplitude"].to_numpy(dtype=float)
        pct = tail["pct_chg"].to_numpy(dtype=float)
        amount = _f(tail["amount"].iloc[-1])
        last = _f(close[-1])

        box_high, box_low = float(np.nanmax(close)), float(np.nanmin(close))
        width = None if not box_low else (box_high - box_low) / box_low
        position = None
        if width is not None and box_high > box_low and last is not None:
            position = (last - box_low) / (box_high - box_low)
        first = _f(close[0])
        net_chg = (last / first - 1.0) if (first and last) else None
        ma5 = float(np.nanmean(close[-5:])) if days >= 5 else None
        ma20 = float(np.nanmean(close[-20:])) if days >= 20 else None
        ma_gap = abs(ma5 - ma20) / ma20 if (ma5 is not None and ma20) else None
        avg_turnover = float(np.nanmean(turnover)) if np.isfinite(turnover).any() else None
        turnover_cv = None
        if avg_turnover:
            turnover_cv = float(np.nanstd(turnover) / avg_turnover)
        avg_amp = float(np.nanmean(amplitude)) if np.isfinite(amplitude).any() else None
        extremes = int(np.sum(np.abs(pct) >= 9.5))

        row.update(
            {
                "box_high": box_high,
                "box_low": box_low,
                "box_width": width,
                "box_pos": position,
                "box_net_chg": net_chg,
                "box_ma_gap": ma_gap,
                "box_avg_turnover": avg_turnover,
                "box_turnover_cv": turnover_cv,
                "box_avg_amplitude": avg_amp,
                "box_extreme_days": extremes,
            }
        )
        row["s_box_range"] = int(
            _between(width, min_width, max_width)
            and net_chg is not None
            and abs(net_chg) <= max_net + EPS
            and ma_gap is not None
            and ma_gap <= max_gap + EPS
            and _between(avg_turnover, min_turnover, max_turnover)
            and turnover_cv is not None
            and turnover_cv <= max_cv + EPS
            and _between(avg_amp, min_amp, max_amp)
            and extremes <= max_extreme
            and (amount is None or amount >= min_amount - EPS)
            and not (exclude_st and str(secid) in st)
        )
        rows.append(row)
    return pd.DataFrame(rows)


def scan_box(
    store: Store,
    cfg=None,
    trade_date: Optional[str] = None,
    min_bars: Optional[int] = None,
) -> pd.DataFrame:
    """从库里读最近行情 -> 算箱体特征（一只票一行，含未命中的）。"""
    conf = cfg.get if cfg is not None else (lambda key, default=None: default)
    window = int(conf("box.window", DEFAULT_WINDOW) or DEFAULT_WINDOW)
    calendar_days = int(window * 2 + 20)
    bars = store.load_recent_bars(calendar_days=calendar_days, columns=BOX_BAR_COLUMNS)
    if bars is not None and not bars.empty and trade_date:
        bars = bars[bars["date"].astype(str) <= str(trade_date)]
    symbols = store.symbols_df()
    st_secids: List[str] = []
    if symbols is not None and not symbols.empty and "is_st" in symbols.columns:
        st_secids = [str(s) for s in symbols.loc[symbols["is_st"] == 1, "secid"].tolist()]
    features = compute_box_features(bars, st_secids=st_secids, cfg=cfg, window=window)
    if features.empty:
        return features
    frame = features[features["s_box_range"] == 1].copy()
    if frame.empty:
        return frame
    if symbols is not None and not symbols.empty:
        meta = symbols.drop_duplicates(subset=["secid"]).set_index("secid")
        for column in ("code", "name", "board"):
            frame[column] = meta[column].reindex(frame["secid"]).to_numpy()
    return frame
