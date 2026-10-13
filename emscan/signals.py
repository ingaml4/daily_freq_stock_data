"""指标信号筛选：MACD / KDJ 金叉死叉、布林上下轨、均线金叉（纯计算，不联网，可单测）。

输入是已落库的两张表：

* ``bars``        —— 当日价量（收盘、涨跌、成交额、换手率、最低价…）
* ``indicators``  —— 麦蕊的日频 MACD / KDJ / BOLL / MA（见 ``emscan/mairui.py``）

输出一张"当日命中哪些信号"的表，用来筛"刚进入金叉""刚踩到布林下轨"这类形态。

规则一览（``config.yaml`` 的 ``signals.rules`` 里挑，默认取前三个）：

| 规则名 | 含义 | 需要的指标 |
| --- | --- | --- |
| ``macd_golden`` | MACD 金叉：DIFF 上穿 DEA | macd |
| ``macd_dead`` | MACD 死叉：DIFF 下穿 DEA | macd |
| ``kdj_golden`` | KDJ 金叉：K 上穿 D | kdj |
| ``kdj_dead`` | KDJ 死叉：K 下穿 D | kdj |
| ``ma_golden`` | MA5 上穿 MA10 | ma |
| ``ma_dead`` | MA5 下穿 MA10 | ma |
| ``boll_lower`` | 触及布林下轨（当日最低价 ≤ 下轨） | boll |
| ``boll_break_lower`` | 收在布林下轨之下（收盘 ≤ 下轨） | boll |
| ``boll_upper`` | 触及布林上轨（当日最高价 ≥ 上轨） | boll |
| ``boll_break_upper`` | 收在布林上轨之上（收盘 ≥ 上轨） | boll |

``signals.match`` 决定多个规则之间是"或"（``any``，默认）还是"且"（``all``）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# 规则名 -> (中文说明, 依赖的指标列)
RULE_SPECS: Dict[str, Dict[str, Any]] = {
    "macd_golden": {"label": "MACD金叉", "needs": ("macd_diff", "macd_dea")},
    "macd_dead": {"label": "MACD死叉", "needs": ("macd_diff", "macd_dea")},
    "kdj_golden": {"label": "KDJ金叉", "needs": ("kdj_k", "kdj_d")},
    "kdj_dead": {"label": "KDJ死叉", "needs": ("kdj_k", "kdj_d")},
    "ma_golden": {"label": "MA金叉", "needs": ("ma5", "ma10")},
    "ma_dead": {"label": "MA死叉", "needs": ("ma5", "ma10")},
    "boll_lower": {"label": "触及下轨", "needs": ("boll_lower",)},
    "boll_break_lower": {"label": "跌破下轨", "needs": ("boll_lower",)},
    "boll_upper": {"label": "触及上轨", "needs": ("boll_upper",)},
    "boll_break_upper": {"label": "突破上轨", "needs": ("boll_upper",)},
}

DEFAULT_RULES = ["macd_golden", "kdj_golden", "boll_lower"]

# 报告里展示的列
SIGNAL_COLUMNS: List[str] = [
    "secid",
    "code",
    "name",
    "board",
    "close",
    "pct_chg",
    "amount_yi",
    "signals",
    "signals_cn",
    "rule_count",
    "boll_pos",
    "boll_lower",
    "boll_mid",
    "boll_upper",
    "macd_diff",
    "macd_dea",
    "macd_bar",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "score",
]

# 筛选信号需要的行情列（比放量扫描多要 low/high，用来判断"触及上/下轨"）
SIGNAL_BAR_COLUMNS: tuple = (
    "secid",
    "date",
    "close",
    "high",
    "low",
    "volume",
    "amount",
    "pct_chg",
    "turnover",
)

# 规则名 -> daily_signals 里的标记列
RULE_FLAG_COLUMNS: Dict[str, str] = {
    "macd_golden": "s_macd_golden",
    "macd_dead": "s_macd_dead",
    "kdj_golden": "s_kdj_golden",
    "kdj_dead": "s_kdj_dead",
    "ma_golden": "s_ma_golden",
    "ma_dead": "s_ma_dead",
    "boll_lower": "s_boll_lower",
    "boll_break_lower": "s_boll_break_lower",
    "boll_upper": "s_boll_upper",
    "boll_break_upper": "s_boll_break_upper",
}

# 标记列 -> 中文名（报告、网页、命令行都用同一份，避免各处自己拼）
FLAG_LABELS: Dict[str, str] = {
    "s_vol_spike": "突然放量",
    "s_macd_golden": "MACD金叉",
    "s_kdj_golden": "KDJ金叉",
    "s_boll_break_lower": "BOLL下轨突破",
    "s_macd_dead": "MACD死叉",
    "s_kdj_dead": "KDJ死叉",
    "s_ma_golden": "MA金叉",
    "s_ma_dead": "MA死叉",
    "s_boll_lower": "触及下轨",
    "s_boll_upper": "触及上轨",
    "s_boll_break_upper": "突破上轨",
}

# 写进 daily_signals 缓存的列（指标那部分）
DAILY_INDICATOR_COLUMNS: tuple = (
    "secid",
    "date",
    "close",
    "pct_chg",
    "amount",
    "turnover",
    "volume",
    "boll_pos",
    "macd_diff",
    "macd_dea",
    "macd_bar",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "boll_upper",
    "boll_mid",
    "boll_lower",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
) + tuple(RULE_FLAG_COLUMNS.values())

SIGNAL_LABELS: Dict[str, str] = {
    "code": "代码",
    "name": "名称",
    "board": "板块",
    "trade_date": "交易日",
    "close": "收盘价",
    "pct_chg": "涨跌幅%",
    "turnover": "换手率%",
    "amount": "成交额(元)",
    "amount_yi": "成交额(亿)",
    "signals": "信号",
    "signals_cn": "命中信号",
    "rule_count": "信号数",
    "boll_pos": "布林位置",
    "boll_lower": "布林下轨",
    "boll_mid": "布林中轨",
    "boll_upper": "布林上轨",
    "macd_diff": "DIFF",
    "macd_dea": "DEA",
    "macd_bar": "MACD柱",
    "kdj_k": "K",
    "kdj_d": "D",
    "kdj_j": "J",
    "ma5": "MA5",
    "ma10": "MA10",
    "ma20": "MA20",
    "ma60": "MA60",
    "score": "评分",
}

SIGNAL_FORMATS: Dict[str, str] = {
    "close": "{:.2f}",
    "pct_chg": "{:+.2f}",
    "turnover": "{:.2f}",
    "amount_yi": "{:.2f}",
    "boll_pos": "{:.2f}",
    "boll_lower": "{:.2f}",
    "boll_mid": "{:.2f}",
    "boll_upper": "{:.2f}",
    "macd_diff": "{:.3f}",
    "macd_dea": "{:.3f}",
    "macd_bar": "{:.3f}",
    "kdj_k": "{:.1f}",
    "kdj_d": "{:.1f}",
    "kdj_j": "{:.1f}",
    "ma5": "{:.2f}",
    "ma10": "{:.2f}",
    "ma20": "{:.2f}",
    "ma60": "{:.2f}",
    "score": "{:.2f}",
}


@dataclass
class SignalResult:
    trade_date: str
    hits: pd.DataFrame
    stats: Dict[str, Any] = field(default_factory=dict)
    # 全市场信号（不只命中报告里挑出来的那些）：写每日信号缓存用
    all_metrics: Optional[pd.DataFrame] = None

    def __len__(self) -> int:
        return len(self.hits)


def normalize_rules(rules: Optional[Sequence[str]]) -> List[str]:
    """把配置里的规则名规范化：去空、去重、忽略不认识的名字（带警告）。"""
    picked: List[str] = []
    for rule in rules or []:
        name = str(rule or "").strip().lower()
        if not name:
            continue
        if name not in RULE_SPECS:
            log.warning("忽略未知信号规则：%s（可选 %s）", name, ", ".join(RULE_SPECS))
            continue
        if name not in picked:
            picked.append(name)
    return picked or list(DEFAULT_RULES)


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def _cross(
    fast: pd.Series, slow: pd.Series, prev_fast: pd.Series, prev_slow: pd.Series
) -> pd.Series:
    """fast 上穿 slow：今天 fast > slow，且昨天 fast <= slow。"""
    now = (fast > slow) & prev_fast.notna() & prev_slow.notna() & fast.notna() & slow.notna()
    before = prev_fast <= prev_slow
    return (now & before.fillna(False)).fillna(False)


def _rule_mask(
    rule: str,
    today: pd.DataFrame,
    prev: pd.DataFrame,
    index: pd.Index,
) -> pd.Series:
    """算单条规则在当日是否命中（返回与 ``index`` 对齐的布尔 Series）。"""
    if rule == "macd_golden":
        return _cross(
            _numeric(today, "macd_diff").reindex(index),
            _numeric(today, "macd_dea").reindex(index),
            _numeric(prev, "macd_diff").reindex(index),
            _numeric(prev, "macd_dea").reindex(index),
        )
    if rule == "macd_dead":
        return _cross(
            _numeric(today, "macd_dea").reindex(index),
            _numeric(today, "macd_diff").reindex(index),
            _numeric(prev, "macd_dea").reindex(index),
            _numeric(prev, "macd_diff").reindex(index),
        )
    if rule == "kdj_golden":
        return _cross(
            _numeric(today, "kdj_k").reindex(index),
            _numeric(today, "kdj_d").reindex(index),
            _numeric(prev, "kdj_k").reindex(index),
            _numeric(prev, "kdj_d").reindex(index),
        )
    if rule == "kdj_dead":
        return _cross(
            _numeric(today, "kdj_d").reindex(index),
            _numeric(today, "kdj_k").reindex(index),
            _numeric(prev, "kdj_d").reindex(index),
            _numeric(prev, "kdj_k").reindex(index),
        )
    if rule == "ma_golden":
        return _cross(
            _numeric(today, "ma5").reindex(index),
            _numeric(today, "ma10").reindex(index),
            _numeric(prev, "ma5").reindex(index),
            _numeric(prev, "ma10").reindex(index),
        )
    if rule == "ma_dead":
        return _cross(
            _numeric(today, "ma10").reindex(index),
            _numeric(today, "ma5").reindex(index),
            _numeric(prev, "ma10").reindex(index),
            _numeric(prev, "ma5").reindex(index),
        )
    if rule in ("boll_lower", "boll_break_lower"):
        lower = _numeric(today, "boll_lower").reindex(index)
        price = (
            _numeric(today, "low").reindex(index)
            if rule == "boll_lower"
            else _numeric(today, "close").reindex(index)
        )
        return ((price <= lower) & lower.notna() & price.notna()).fillna(False)
    if rule in ("boll_upper", "boll_break_upper"):
        upper = _numeric(today, "boll_upper").reindex(index)
        price = (
            _numeric(today, "high").reindex(index)
            if rule == "boll_upper"
            else _numeric(today, "close").reindex(index)
        )
        return ((price >= upper) & upper.notna() & price.notna()).fillna(False)
    raise ValueError(f"未知信号规则：{rule}")


def detect(
    bars: pd.DataFrame,
    indicators: pd.DataFrame,
    symbols: Optional[pd.DataFrame],
    cfg,
    trade_date: Optional[str] = None,
    rules: Optional[Sequence[str]] = None,
    apply_universe_filter: bool = True,
) -> SignalResult:
    """在本地数据上筛选"当日命中指定指标信号"的股票。

    参数
    ----
    bars
        日线行情（至少 secid/date/close/volume/amount）；
    indicators
        日频指标（secid/date + 各指标列），至少覆盖当日与上一个交易日；
    symbols
        股票基础信息（secid/code/name/board/is_st），用于过滤与展示；
    trade_date
        要筛选的交易日，默认取指标表里最大的日期。
    """
    active_rules = normalize_rules(rules if rules is not None else cfg.get("signals.rules"))
    top_n = int(cfg.get("signals.top_n", 200) or 200)
    match_mode = str(cfg.get("signals.match", "any") or "any").strip().lower()
    min_amount = float(cfg.get("signals.min_amount", 0) or 0)
    exclude_st = bool(cfg.get("universe.exclude_st", True))
    exclude_suspended = bool(cfg.get("universe.exclude_suspended_today", True))
    boards = cfg.get("universe.boards") or []

    if indicators is None or indicators.empty:
        return SignalResult(
            trade_date or "",
            pd.DataFrame(columns=SIGNAL_COLUMNS),
            {"reason": "no_indicators"},
            pd.DataFrame(columns=DAILY_INDICATOR_COLUMNS),
        )

    indicators = indicators.copy()
    indicators["date"] = indicators["date"].astype(str)
    for column in indicators.columns:
        if column not in ("secid", "date", "source"):
            indicators[column] = pd.to_numeric(indicators[column], errors="coerce")

    trade_date = str(trade_date or indicators["date"].max())
    available = sorted(set(indicators["date"]))
    if trade_date not in available:
        # 指标当天可能还没更新（文档说明日线指标 15:35 开始更新），退回最近一个有数据的日子
        latest = max((d for d in available if d <= trade_date), default=None)
        if latest is None:
            raise ValueError(f"{trade_date} 及其之前没有任何指标数据，先跑 indicators/update")
        log.warning("指标表里没有 %s 的数据，改用最近一天 %s", trade_date, latest)
        trade_date = latest
    prev_date = max((d for d in available if d < trade_date), default=None)

    today_ind = indicators[indicators["date"] == trade_date].drop_duplicates(subset=["secid"])
    prev_ind = (
        indicators[indicators["date"] == prev_date].drop_duplicates(subset=["secid"])
        if prev_date
        else today_ind.head(0)
    )

    if bars is None or bars.empty:
        return SignalResult(
            trade_date,
            pd.DataFrame(columns=SIGNAL_COLUMNS),
            {"reason": "no_bars"},
            pd.DataFrame(columns=DAILY_INDICATOR_COLUMNS),
        )
    bars = bars.copy()
    bars["date"] = bars["date"].astype(str)
    today_bars = bars[bars["date"] == trade_date].drop_duplicates(subset=["secid"])
    if today_bars.empty:
        raise ValueError(f"{trade_date} 在本地行情里没有数据，无法筛选（先跑 update/backfill）")
    today_bars = today_bars.set_index("secid")

    index = today_bars.index
    today_ind = today_ind.set_index("secid").reindex(index)
    prev_ind = prev_ind.set_index("secid").reindex(index)

    today = today_bars.join(today_ind, how="left", rsuffix="_ind")
    today["low"] = pd.to_numeric(today.get("low"), errors="coerce")
    today["high"] = pd.to_numeric(today.get("high"), errors="coerce")
    prev = prev_ind

    # 全部规则都算一遍：报告只挑 active_rules，但每日信号缓存里留全套标记，
    # 以后想按别的规则（或组合）筛选时不用重算。
    mask_frame = pd.DataFrame(
        {rule: _rule_mask(rule, today, prev, index) for rule in RULE_SPECS}, index=index
    )
    active_mask = mask_frame[active_rules]
    hit_count = active_mask.sum(axis=1)
    if match_mode == "all":
        matched = hit_count >= len(active_rules)
    else:
        matched = hit_count >= 1

    # ---- 全市场：指标数值 + 全套信号标记（写每日信号缓存用）
    full = today.copy()
    full["trade_date"] = trade_date
    full["signals"] = [
        ",".join([rule for rule in active_rules if bool(active_mask.loc[secid, rule])])
        for secid in full.index
    ]
    full["signals_cn"] = [
        "、".join(
            RULE_SPECS[rule]["label"] for rule in active_rules if bool(active_mask.loc[secid, rule])
        )
        for secid in full.index
    ]
    full["rule_count"] = hit_count.astype(int)
    for rule, column in RULE_FLAG_COLUMNS.items():
        full[column] = mask_frame[rule].astype(int)
    full["amount"] = pd.to_numeric(full.get("amount"), errors="coerce")
    full["amount_yi"] = full["amount"] / 1e8
    full["pct_chg"] = pd.to_numeric(full.get("pct_chg"), errors="coerce")
    full["turnover"] = pd.to_numeric(full.get("turnover"), errors="coerce")
    upper = pd.to_numeric(full.get("boll_upper"), errors="coerce")
    lower = pd.to_numeric(full.get("boll_lower"), errors="coerce")
    close = pd.to_numeric(full.get("close"), errors="coerce")
    width = (upper - lower).replace(0, np.nan)
    full["boll_pos"] = ((close - lower) / width).replace([np.inf, -np.inf], np.nan)

    # ---- 股票基础信息 + 基础过滤（ST / 停牌 / 成交额门槛 / 板块）
    if symbols is not None and not symbols.empty:
        meta = symbols.drop_duplicates(subset=["secid"]).set_index("secid")
        for column, default in (("code", None), ("name", ""), ("board", np.nan), ("is_st", 0)):
            if column in meta.columns:
                full[column] = meta[column].reindex(full.index)
            elif column not in full.columns:
                full[column] = default
    else:
        full["code"] = full.index.str.split(".").str[-1]
        full["name"] = ""
        full["board"] = np.nan
        full["is_st"] = 0
    full["name"] = full["name"].fillna("").astype(str)
    full["is_st"] = pd.to_numeric(full.get("is_st"), errors="coerce").fillna(0).astype(int)
    volume = pd.to_numeric(full.get("volume"), errors="coerce").fillna(0.0)

    keep = pd.Series(True, index=full.index)
    if exclude_suspended:
        keep &= volume > 0
    if min_amount > 0:
        keep &= full["amount"].fillna(0.0) >= min_amount
    if exclude_st:
        keep &= (full["is_st"] == 0) & ~full["name"].str.contains("ST|退", regex=True, na=False)
    if apply_universe_filter and boards:
        keep &= full["board"].isin(list(boards)) | full["board"].isna()

    # 缓存里存全市场（含 ST/停牌），过滤只作用在报告上
    all_out = full.reset_index()
    for column in DAILY_INDICATOR_COLUMNS:
        if column not in all_out.columns:
            all_out[column] = np.nan
    all_metrics = all_out[list(DAILY_INDICATOR_COLUMNS)]

    hits = full[keep & matched].copy()
    stats = _stats(trade_date, index, active_rules, match_mode, int(len(hits)), prev_date)
    if hits.empty:
        result = pd.DataFrame(columns=SIGNAL_COLUMNS)
        result.attrs["stats"] = stats
        return SignalResult(trade_date, result, stats, all_metrics)

    hits["score"] = _score(hits)
    hits = hits.sort_values(
        ["rule_count", "boll_pos", "amount"], ascending=[False, True, False]
    ).head(top_n)
    for column in SIGNAL_COLUMNS:
        if column == "secid" or column in hits.columns:
            continue  # secid 是指标索引，稍后 reset_index() 会补成列
        hits[column] = np.nan
    result = hits.reset_index()[SIGNAL_COLUMNS]
    result.attrs["stats"] = stats
    return SignalResult(trade_date, result, stats, all_metrics)


def _stats(
    trade_date: str,
    index: pd.Index,
    rules: Sequence[str],
    match_mode: str,
    hits: int,
    prev_date: Optional[str],
) -> Dict[str, Any]:
    return {
        "trade_date": trade_date,
        "prev_trade_date": prev_date,
        "universe": int(len(index)),
        "hits": int(hits),
        "rules": list(rules),
        "match": match_mode,
    }


def _score(frame: pd.DataFrame) -> pd.Series:
    """排序分：命中规则越多越靠前，其次看布林位置（越贴下轨越靠前）与成交额。"""
    count = pd.to_numeric(frame.get("rule_count"), errors="coerce").fillna(0.0)
    position = pd.to_numeric(frame.get("boll_pos"), errors="coerce").clip(-1.0, 2.0)
    amount = pd.to_numeric(frame.get("amount"), errors="coerce").fillna(0.0)
    score = 3.0 * count + (1.0 - position.fillna(1.0)) + np.log10(amount / 1e8 + 1.0)
    return pd.Series(np.round(score.to_numpy(dtype=float), 3), index=frame.index)


def build_summary(result: SignalResult, cfg) -> str:
    stats = result.stats or {}
    labels = " + ".join(RULE_SPECS[rule]["label"] for rule in stats.get("rules", []))
    match_cn = "同时满足" if stats.get("match") == "all" else "任一满足"
    return "\n".join(
        [
            f"{cfg.get('signals.title', 'A股日频指标信号')} | 交易日 {result.trade_date}",
            f"参与筛选 {stats.get('universe', '-')} 只 / 命中 {len(result.hits)} 只",
            f"信号规则：{labels or '-'}（{match_cn}）；上一个交易日 {stats.get('prev_trade_date') or '-'}",
        ]
    )
