"""本地计算日频技术指标（MACD / KDJ / BOLL / MA）。

为什么要有这个：麦蕊的指标接口是"**一只股票、一种指标、一次请求**"，
全市场 5200 只 × 3 种 ≈ 1.6 万次/天，按基础档 300 次/分钟要跑近一小时 —— 慢在限频，不在机器。
而这些指标本来就只是日线的函数，本地用日线算一遍是**毫秒级**的事：

* 增量：每天只需算"新出现的那个交易日"（几千行、几秒）；
* 回补：历史指标可以随时按日线重算，不必再花接口配额；
* 口径：MACD/KDJ/BOLL/MA 都是标准公式（见下），与行情软件一致；
  与麦蕊返回值的差异最多出现在 EMA/KDJ 的起始值与软件小数位上（不影响金叉/触轨这类判断）。

公式（与通达信/同花顺一致）::

    MACD: EMA12, EMA26 -> DIF = EMA12-EMA26, DEA = EMA9(DIF), MACD柱 = 2*(DIF-DEA)
    KDJ : RSV = (C - LLV(L,9)) / (HHV(H,9) - LLV(L,9)) * 100
          K = 2/3*K' + 1/3*RSV, D = 2/3*D' + 1/3*K, J = 3K - 2D（K/D 初值 50）
    BOLL: MID = MA(C,20), UPPER/LOWER = MID ± 2*STD(C,20)（总体标准差）
    MA  : MA5 / MA10 / MA20 / MA60

想继续用麦蕊的指标（口径以券商为准），把 ``indicators.source`` 设成 ``api`` 即可。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .store import INDICATOR_COLUMNS, Store

log = logging.getLogger("emscan")

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
KDJ_N = 9
BOLL_N = 20
BOLL_K = 2.0
MA_PERIODS = (5, 10, 20, 60)

# 算指标需要的"预热"自然日：最长的 MA60 要 60 个交易日，EMA26 递推约 100 个交易日收敛，
# 200 自然日 ≈ 137 个交易日，足够；再多只是白读数据
WARMUP_CALENDAR_DAYS = 200

# 本地计算只用得到这三列，读库时按列裁剪（内存与 IO 都省一半以上）
BAR_INPUT_COLUMNS = ("close", "high", "low")


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def compute_frame(bars: pd.DataFrame) -> pd.DataFrame:
    """输入单只股票的日线（按日期升序，含 date/close/high/low），输出各指标列。"""
    close = pd.to_numeric(bars["close"], errors="coerce")
    high = pd.to_numeric(bars["high"], errors="coerce")
    low = pd.to_numeric(bars["low"], errors="coerce")

    # ---- MACD
    dif = _ema(close, MACD_FAST) - _ema(close, MACD_SLOW)
    dea = _ema(dif, MACD_SIGNAL)

    # ---- KDJ
    lowest = low.rolling(KDJ_N, min_periods=1).min()
    highest = high.rolling(KDJ_N, min_periods=1).max()
    span = (highest - lowest).replace(0, np.nan)
    rsv = ((close - lowest) / span * 100).fillna(50.0)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    j = 3 * k - 2 * d

    # ---- BOLL
    mid = close.rolling(BOLL_N, min_periods=1).mean()
    std = close.rolling(BOLL_N, min_periods=1).std(ddof=0).fillna(0.0)

    out = pd.DataFrame(
        {
            "date": bars["date"].astype(str).to_numpy(),
            "macd_diff": dif.to_numpy(dtype=float),
            "macd_dea": dea.to_numpy(dtype=float),
            "macd_bar": (2 * (dif - dea)).to_numpy(dtype=float),
            "kdj_k": k.to_numpy(dtype=float),
            "kdj_d": d.to_numpy(dtype=float),
            "kdj_j": j.to_numpy(dtype=float),
            "boll_upper": (mid + BOLL_K * std).to_numpy(dtype=float),
            "boll_mid": mid.to_numpy(dtype=float),
            "boll_lower": (mid - BOLL_K * std).to_numpy(dtype=float),
        }
    )
    for period in MA_PERIODS:
        out["ma%d" % period] = close.rolling(period, min_periods=1).mean().to_numpy(dtype=float)
    return out


def _clean(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (float("inf"), float("-inf")) else number


def compute_and_store(
    store: Store,
    secids: Sequence[str],
    cfg=None,
    dates: Optional[Sequence[str]] = None,
    kinds: Sequence[str] = ("macd", "kdj", "boll", "ma"),
    chunk: int = 400,
    progress=None,
) -> int:
    """本地算好指标写进 ``indicators`` 表；返回写入行数。

    * ``dates`` 给定 -> 只写这些交易日（每天增量用这个）；
      不给则写日线里所有日期（首次回补/重算历史）。
    * 会多读一段"预热"日线，保证 MA60/EMA26 这类递推从足够早的地方开始。
    """
    if not secids:
        return 0
    target = sorted({str(d) for d in dates}) if dates else None
    start = None
    if target:
        start = (pd.Timestamp(target[0]) - pd.Timedelta(days=WARMUP_CALENDAR_DAYS)).strftime(
            "%Y-%m-%d"
        )
    step = max(1, int(chunk))
    written = 0
    total = len(secids)
    for offset in range(0, total, step):
        group = list(secids[offset : offset + step])
        bars = store.load_bars(start=start, secids=group, columns=BAR_INPUT_COLUMNS)
        if bars.empty:
            continue
        bars["date"] = bars["date"].astype(str)
        bars = bars.sort_values(["secid", "date"])
        rows: Dict[str, List[Dict[str, Any]]] = {kind: [] for kind in kinds}
        for secid, frame in bars.groupby("secid", sort=False):
            computed = compute_frame(frame)
            if target is not None:
                computed = computed[computed["date"].isin(target)]
            for record in computed.to_dict("records"):
                date = record.pop("date")
                payload = {"secid": secid, "date": date, "source": "local"}
                for column in record:
                    payload[column] = _clean(record[column])
                # 四种指标各写一次（列不重叠，COALESCE 合并到同一行）
                for kind in kinds:
                    if kind in INDICATOR_COLUMNS:
                        rows[kind].append(payload)
        for kind in kinds:
            if rows[kind]:
                written += store.upsert_indicators(kind, rows[kind])
        if progress:
            progress(min(offset + step, total), total, "本地计算")
    return written
