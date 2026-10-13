"""放量检测算法（纯计算，不联网，可单测）。

核心思路
--------
1. 对每只股票，取"今日成交量"与"之前 N 个有效交易日平均成交量"的比值（量比），
   N 默认取 5/10/20/60；停牌等成交量=0 的交易日不算作基准（否则会人为放大比值）。
2. 再用稳健统计量兜底：对 log(成交量) 取中位数 + MAD，算今日的 Z 分数，
   这样即使某只票平时成交量极低、偶发放量，也能被识别出来（对异常值不敏感）。
3. 硬性门槛过滤噪声：成交额下限、换手率下限、最少历史K线数、剔除 ST/退市、
   当日停牌不参与。
4. 命中规则（可配）::

       vol_ratio_5 >= 2.0  且  (vol_ratio_20 >= 1.5  或  vol_z60 >= 3.0)

5. 命中后按 "放量上攻 / 放量下跌 / 放量滞涨 / 放量震荡" 分类，并给出综合排序分。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

SIGNAL_UP = "放量上攻"
SIGNAL_DOWN = "放量下跌"
SIGNAL_FLAT = "放量滞涨"
SIGNAL_CHOP = "放量震荡"

# 扫描真正用到的列：读取时只取这些，行情库再大也不会把内存吃满
SCAN_COLUMNS = (
    "secid",
    "date",
    "volume",
    "close",
    "pct_chg",
    "amount",
    "turnover",
    "float_mv",
    "volume_ratio",
)


@dataclass
class ScanResult:
    trade_date: str
    hits: pd.DataFrame
    stats: Dict[str, Any] = field(default_factory=dict)
    # 全市场指标（不只命中）：写"每日信号缓存"用，扫描时顺手算好，避免重复计算
    all_metrics: Optional[pd.DataFrame] = None

    def __len__(self) -> int:
        return len(self.hits)


# 写进 daily_signals 缓存的列（放量那部分）
DAILY_VOLUME_COLUMNS: tuple = (
    "secid",
    "date",
    "close",
    "pct_chg",
    "volume",
    "amount",
    "turnover",
    "vol_ratio_5",
    "vol_ratio_10",
    "vol_ratio_20",
    "vol_ratio_60",
    "vol_z60",
    "spot_volume_ratio",
    "vol_signal",
    "vol_score",
    "s_vol_spike",
)


def result_columns(windows: Sequence[int], baseline_window: int) -> List[str]:
    cols = [
        "secid",
        "code",
        "name",
        "board",
        "trade_date",
        "close",
        "pct_chg",
        "amount",
        "amount_yi",
        "turnover",
        "volume",
    ]
    cols += [f"vol_ratio_{int(n)}" for n in windows]
    cols += [f"vol_z{int(baseline_window)}", "spot_volume_ratio", "valid_bars", "signal", "score"]
    return cols


def _empty_result(windows: Sequence[int], baseline_window: int) -> pd.DataFrame:
    return pd.DataFrame(columns=result_columns(windows, baseline_window))


def _window_list(cfg) -> List[int]:
    windows = {int(n) for n in cfg.get("scan.windows", [5, 10, 20, 60])}
    # 命中规则依赖这两个窗口，即使用户没配也补上
    windows.update({5, 20})
    return sorted(w for w in windows if w > 0)


def scan(
    bars: pd.DataFrame,
    symbols: Optional[pd.DataFrame],
    cfg,
    trade_date: Optional[str] = None,
    apply_universe_filter: bool = True,
) -> ScanResult:
    """在本地行情上做一次全市场放量扫描。

    参数
    ----
    bars
        长表，至少包含 secid/date/volume，其余价格与换手率字段缺失会自动填 0/NaN。
    symbols
        股票基础信息（secid/code/name/board/is_st），用于 ST 过滤和报告展示。
    trade_date
        要扫描的交易日；默认取 bars 里最大的日期（即最新一个交易日）。
    """
    windows = _window_list(cfg)
    min_bars_cfg = int(cfg.get("universe.min_bars", 25))
    baseline_window = max(int(cfg.get("scan.baseline_window", 60)), min_bars_cfg)
    min_amount = float(cfg.get("scan.min_amount", 0) or 0)
    min_turnover = float(cfg.get("scan.min_turnover", 0) or 0)
    v5_min = float(cfg.get("scan.vol_ratio_5_min", 2.0))
    v20_min = float(cfg.get("scan.vol_ratio_20_min", 1.5))
    z_min = float(cfg.get("scan.z_min", 3.0))
    min_valid_in_window = int(cfg.get("scan.min_valid_in_window", 2) or 2)
    top_n = int(cfg.get("scan.top_n", 50))
    exclude_st = bool(cfg.get("universe.exclude_st", True))
    exclude_suspended = bool(cfg.get("universe.exclude_suspended_today", True))
    boards = cfg.get("universe.boards") or []

    columns = result_columns(windows, baseline_window)
    if bars is None or bars.empty:
        return ScanResult(
            trade_date or "", _empty_result(windows, baseline_window), {"reason": "no_data"}
        )

    bars = bars.copy()
    bars["date"] = bars["date"].astype(str)
    bars["volume"] = pd.to_numeric(bars["volume"], errors="coerce").fillna(0.0)
    for col in ("close", "amount", "turnover", "pct_chg", "float_mv", "volume_ratio"):
        if col in bars.columns:
            bars[col] = pd.to_numeric(bars[col], errors="coerce")

    trade_date = str(trade_date or bars["date"].max())
    today = bars[bars["date"] == trade_date]
    if today.empty:
        raise ValueError(f"{trade_date} 在本地行情里没有数据，无法扫描（先跑 update/backfill）")
    hist = bars[bars["date"] < trade_date]

    today = today.drop_duplicates(subset=["secid"]).set_index("secid")
    secids = pd.Index(today.index)

    # ---- 历史成交量矩阵（行=交易日，列=股票），只保留真的有成交的交易日
    hist = hist[hist["secid"].isin(secids)]
    hist = hist[hist["volume"] > 0]
    if hist.empty:
        return ScanResult(
            trade_date,
            _empty_result(windows, baseline_window),
            {"reason": "no_history", "universe": int(len(secids))},
        )
    vol = hist.pivot_table(index="date", columns="secid", values="volume", aggfunc="last")
    vol = vol.sort_index().tail(baseline_window).reindex(columns=secids)

    # ---- 当日指标
    res = pd.DataFrame(index=secids)
    res.index.name = "secid"
    res["trade_date"] = trade_date
    for col in ("close", "pct_chg", "amount", "turnover", "float_mv", "volume_ratio"):
        res[col] = today[col] if col in today.columns else np.nan
    res["volume"] = pd.to_numeric(today["volume"], errors="coerce").astype(float)
    res["valid_bars"] = vol.notna().sum(axis=0).astype(float)

    today_vol = res["volume"]

    # ---- 量比：今日 / 之前 N 个有效交易日均值
    for n in windows:
        window = vol.tail(n)
        count = window.notna().sum(axis=0)
        mean = window.mean(axis=0, skipna=True)
        # 窗口内有效交易日（非停牌）太少时不给结论：均值建立在 1 天数据上没有意义。
        # 默认要求至少 2 天，这样"停牌几天后复牌放量"仍能被识别（见 scan.min_valid_in_window）。
        mean = mean.where(count >= min(min_valid_in_window, n))
        ratio = today_vol / mean
        res[f"vol_ratio_{n}"] = ratio.replace([np.inf, -np.inf], np.nan)

    # ---- 稳健 Z 分数（log 成交量，中位数 + MAD）
    log_vol = np.log1p(vol.to_numpy(dtype=float))
    with np.errstate(all="ignore"):
        median = np.nanmedian(log_vol, axis=0)
        mad = np.nanmedian(np.abs(log_vol - median), axis=0)
        z = (np.log1p(today_vol.to_numpy(dtype=float)) - median) / (1.4826 * mad)
    zero_mad = ~(mad > 0)
    if zero_mad.any():
        # 基准期成交量完全一致（例如长期停牌后恢复）时用方向性下限代替除法
        higher = today_vol.to_numpy(dtype=float) > np.expm1(median)
        z = np.where(zero_mad, np.where(higher, 10.0, 0.0), z)
    res[f"vol_z{baseline_window}"] = z

    # ---- 股票基础信息 & 过滤条件
    if symbols is not None and not symbols.empty:
        meta = symbols.drop_duplicates(subset=["secid"]).set_index("secid")
        res["code"] = (
            meta["code"].reindex(res.index) if "code" in meta else res.index.str.split(".").str[-1]
        )
        res["name"] = meta["name"].reindex(res.index) if "name" in meta else ""
        res["board"] = meta["board"].reindex(res.index) if "board" in meta else np.nan
        res["is_st"] = meta["is_st"].reindex(res.index) if "is_st" in meta else 0
    else:
        res["code"] = res.index.str.split(".").str[-1]
        res["name"] = ""
        res["board"] = np.nan
        res["is_st"] = 0

    res["name"] = res["name"].fillna("").astype(str)
    res["is_st"] = res["is_st"].fillna(0).astype(int)
    res["amount"] = res["amount"].fillna(0.0)
    # 换手率缺失时用 成交额/流通市值 估一个，保证门槛判断不会因为字段缺失而放行
    turnover = res["turnover"]
    fallback = res["amount"] / res["float_mv"].replace(0, np.nan) * 100.0
    res["turnover"] = turnover.where(turnover.notna(), fallback).fillna(0.0)

    eligible = res["valid_bars"] >= min_bars_cfg
    if exclude_suspended:
        eligible &= res["volume"] > 0
    eligible &= res["amount"] >= min_amount
    eligible &= res["turnover"] >= min_turnover
    if exclude_st:
        name = res["name"]
        eligible &= (res["is_st"] == 0) & ~name.str.contains("ST|退", regex=True, na=False)
    if apply_universe_filter and boards:
        eligible &= res["board"].isin(list(boards)) | res["board"].isna()

    vr5 = res["vol_ratio_5"]
    vr20 = res["vol_ratio_20"]
    zcol = res[f"vol_z{baseline_window}"]
    rule = (vr5 >= v5_min) & ((vr20 >= v20_min) | (zcol >= z_min)).fillna(False)
    rule = rule.fillna(False) & vr5.notna()

    # ---- 全市场指标（给每日信号缓存）：命中与否都留一份，方便以后按别的阈值再筛
    selected = eligible & rule
    full = res.copy()
    full["date"] = trade_date  # 放量指标是按"当天"算的：缓存行必须带交易日
    full["s_vol_spike"] = selected.astype(int)
    full["vol_signal"] = np.where(full["s_vol_spike"] > 0, _classify(full["pct_chg"]), None)
    full["vol_score"] = np.where(
        full["s_vol_spike"] > 0, _score(full, baseline_window).to_numpy(dtype=float), np.nan
    )
    full_out = full.reset_index()
    for column in DAILY_VOLUME_COLUMNS:
        if column not in full_out.columns:
            full_out[column] = np.nan
    all_metrics = full_out[list(DAILY_VOLUME_COLUMNS)]

    hits = res[selected].copy()
    stats: Dict[str, Any] = {
        "trade_date": trade_date,
        "universe": int(len(secids)),
        "scanned": int(eligible.sum()),
        "candidates": int((eligible & rule).sum()),
        "thresholds": {
            "vol_ratio_5_min": v5_min,
            "vol_ratio_20_min": v20_min,
            "z_min": z_min,
            "min_amount": min_amount,
            "min_turnover": min_turnover,
            "min_bars": min_bars_cfg,
        },
    }
    if hits.empty:
        result = _empty_result(windows, baseline_window)
        result.attrs["stats"] = stats
        return ScanResult(trade_date, result, stats, all_metrics)

    hits["amount_yi"] = hits["amount"] / 1e8
    hits["signal"] = _classify(hits["pct_chg"])
    hits["score"] = _score(hits, baseline_window)
    # 行情快照自带的"量比"字段，留一份便于和自算量比交叉验证
    hits["spot_volume_ratio"] = hits["volume_ratio"]
    hits = hits.sort_values(["score", "amount"], ascending=False).head(top_n)

    for col in columns:
        # secid 是指标索引，稍后 reset_index() 会补成列
        if col == "secid" or col in hits.columns:
            continue
        hits[col] = np.nan
    result = hits.reset_index()[columns]
    result.attrs["stats"] = stats
    return ScanResult(trade_date, result, stats, all_metrics)


def _classify(pct_chg: pd.Series) -> np.ndarray:
    """按涨跌幅给放量形态分类。"""
    pct = pd.to_numeric(pct_chg, errors="coerce").to_numpy(dtype=float)
    conditions = [
        pct >= 3.0,
        pct <= -3.0,
        np.abs(pct) < 1.0,
    ]
    choices = [SIGNAL_UP, SIGNAL_DOWN, SIGNAL_FLAT]
    return np.select(conditions, choices, default=SIGNAL_CHOP)


def _score(frame: pd.DataFrame, baseline_window: int) -> pd.Series:
    """综合排序分：量比为主，兼顾成交额、换手率与统计显著性。"""
    vr5 = np.clip(
        pd.to_numeric(frame.get("vol_ratio_5"), errors="coerce").to_numpy(float), 1.0, None
    )
    vr20 = np.clip(
        pd.to_numeric(frame.get("vol_ratio_20"), errors="coerce").to_numpy(float), 1.0, None
    )
    z = pd.to_numeric(frame.get(f"vol_z{baseline_window}"), errors="coerce").to_numpy(float)
    amount = np.clip(pd.to_numeric(frame.get("amount"), errors="coerce").to_numpy(float), 1.0, None)
    turnover = np.clip(
        pd.to_numeric(frame.get("turnover"), errors="coerce").to_numpy(float), 0.0, 30.0
    )
    score = (
        2.0 * np.log2(vr5)
        + 1.0 * np.log2(vr20)
        + 0.6 * np.minimum(np.nan_to_num(z, nan=0.0), 10.0)
        + 1.0 * np.log10(amount / 1e8 + 1.0)
        + 0.05 * np.nan_to_num(turnover, nan=0.0)
    )
    return pd.Series(np.round(score, 3), index=frame.index)
