"""收盘时点的规则回放。元数据和行业归属来自当前快照，不能复原历史股票池。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pandas as pd

from . import signals
from .config import DEFAULTS, Config
from .indicators import compute_frame
from .scan import SIGNAL_UP, scan

MIN_BARS = 120
MIN_PEERS = 3
MIN_COVERAGE = 0.60
BASE_RULES = ("macd_golden", "kdj_golden", "boll_break_lower")
FLAG_LABELS = ("MACD金叉", "KDJ金叉", "收破布林下轨", "放量上攻")
STRATEGY_COLUMNS = [
    "date",
    "secid",
    "strategy",
    "score",
    "trigger_price",
    "invalidation_price",
    "indicator_asof",
    "reason",
    "history_valid_bars",
    "point_in_time",
    "indicator_basis",
    "invalidation_basis",
]
SECTOR_COLUMNS = [
    "sector",
    "sector_asof",
    "rank_quality",
    "sector_rank_score",
] + [
    f"{field}_{n}d"
    for n in (5, 20)
    for field in (
        "sector_return",
        "relative_strength",
        "relative_percentile",
        "sector_percentile",
        "peer_count",
        "peer_coverage",
    )
]
COLUMNS = STRATEGY_COLUMNS + SECTOR_COLUMNS
ELIGIBLE_SECID = r"(?:1\.60[0135]\d{3}|0\.(?:00[0123]|30[01])\d{3})"


def _metadata(symbols, tags):
    if isinstance(symbols, Mapping):
        symbols = pd.Series(symbols, name="name").rename_axis("secid").reset_index()
    meta = symbols.set_index("secid").copy()
    if not meta.index.is_unique:
        raise ValueError("重复的股票元数据 secid")
    name = meta["name"].fillna("").astype(str).str.strip()
    eligible = (
        meta.index.str.fullmatch(ELIGIBLE_SECID)
        & name.ne("")
        & ~name.str.contains("ST|退", case=False)
    )
    if "is_st" in meta:
        eligible &= pd.to_numeric(meta["is_st"], errors="coerce").eq(0)
    meta = meta[eligible].copy()
    if isinstance(tags, pd.DataFrame):
        tags = tags[["secid", "sector"]].drop_duplicates().set_index("secid")["sector"]
        if not tags.index.is_unique:
            raise ValueError("同一 secid 具有多个行业标签")
    if tags is not None:
        meta["sector"] = meta.index.map(tags)
    if "sector" not in meta:
        meta["sector"] = None
    meta["sector"] = meta["sector"].replace(r"^\s*$", np.nan, regex=True)
    return meta


def _volume_hits(bars, calendar, start, cfg):
    """直接用原 scan 的全市场 all_metrics，保留其换手过滤和60个市场日的停牌处理。"""
    cols = [
        c
        for c in (
            "date",
            "secid",
            "volume",
            "close",
            "turnover",
            "pct_chg",
            "float_mv",
            "amount",
            "volume_ratio",
        )
        if c in bars
    ]
    market = bars[cols].sort_values("date").reset_index(drop=True)
    boundaries = market["date"].searchsorted(calendar)
    hits = []
    lookback = max(int(cfg.get("scan.baseline_window", 60)), int(cfg.get("universe.min_bars", 25)))
    for i, day in enumerate(calendar):
        if i + 1 < MIN_BARS or (start and day < start):
            continue
        stop = boundaries[i + 1] if i + 1 < len(calendar) else len(market)
        sample = market.iloc[boundaries[max(i - lookback, 0)] : stop]
        if day != sample["date"].max():
            continue
        metrics = scan(sample, None, cfg, trade_date=day, apply_universe_filter=False).all_metrics
        if metrics is not None:
            hits.append(metrics.loc[metrics["vol_signal"] == SIGNAL_UP, ["secid", "date"]])
    if not hits:
        return pd.MultiIndex.from_arrays([[], []], names=["secid", "date"])
    return pd.MultiIndex.from_frame(pd.concat(hits, ignore_index=True))


def _one_stock(stock, market_positions, start):
    """输入一只股票的有效K线和全市场日序。所有滚动量均为向后看。"""
    stock = stock.reset_index(drop=True)
    size = len(stock)
    position = np.arange(size)
    close, high, low, volume = (stock[col] for col in ("close", "high", "low", "volume"))
    spot = stock["source"].eq("spot")
    # EMA/KDJ自首根有效数据开始；历史中出现不同源时保留这个限制。
    src = stock["source"].fillna("unknown")
    basis = np.where(src.ne(src.iloc[0]).cummax(), "mixed", src.iloc[0])
    eligible = position >= MIN_BARS - 1
    if start:
        eligible &= stock["date"].ge(start).to_numpy()
    market_index = stock["date"].map(market_positions)
    ind = compute_frame(stock)
    ind["close"] = close
    yesterday = ind.shift().where(market_index.diff().eq(1), axis=0)
    flags = pd.DataFrame(
        {key: signals._rule_mask(key, ind, yesterday, ind.index) for key in BASE_RULES}
    )
    flags["volume_up"] = stock["volume_up"].astype(bool)
    # 仅放量一项另有换手和成交额门槛，均在原 scan 中应用。
    count = flags.sum(axis=1)
    width = (ind["boll_upper"] - ind["boll_lower"]).replace(0, np.nan)
    score = signals._score(
        pd.DataFrame(
            {
                "rule_count": count,
                "boll_pos": (close - ind["boll_lower"]) / width,
                "amount": stock["amount"],
            }
        )
    )

    resistance = high.shift().rolling(20).max()
    vol_ratio = volume / volume.shift().rolling(20).mean()
    close_pos = (close - low) / (high - low).replace(0, np.nan)
    trend = (
        (close > resistance) & vol_ratio.ge(1.5) & close_pos.ge(0.7) & ind["ma20"].gt(ind["ma60"])
    )
    support = low.shift().rolling(5).min()
    support_spot = spot.shift().rolling(5).sum().eq(5)

    lower = ind["boll_lower"]
    breach = (position >= 19) & close.lt(lower)
    last_breach = pd.Series(np.where(breach, position, np.nan)).ffill()
    age = position - last_breach
    prev_high = high.shift()
    # 每个跌破事件只记录首次同时站回下轨、收破上个有效交易日高点的确认。
    confirm = age.between(1, 5) & close.ge(lower) & close.gt(prev_high)
    confirm &= confirm.groupby(last_breach).cumsum().eq(1)
    structure_low = low.groupby(last_breach).cummin()
    structure_spot = (~spot).groupby(last_breach).cummax().eq(False)

    records = []
    for i in np.flatnonzero(eligible & ((count > 0) | trend | confirm)):
        common = {
            "secid": stock.at[i, "secid"],
            "date": stock.at[i, "date"],
            "indicator_asof": stock.at[i, "date"],
            "point_in_time": True,
            "history_valid_bars": int(i + 1),
            "indicator_basis": basis[i],
        }
        if count.iat[i]:
            records.append(
                {
                    **common,
                    "strategy": "baseline_core",
                    "score": score.iat[i],
                    "reason": "、".join(
                        label for label, flag in zip(FLAG_LABELS, flags.iloc[i]) if flag
                    ),
                    "trigger_price": close.iat[i],
                    "invalidation_price": np.nan,
                    "invalidation_basis": None,
                }
            )
        if trend.iat[i]:
            records.append(
                {
                    **common,
                    "strategy": "trend_breakout",
                    "trigger_price": resistance.iat[i],
                    "score": 100 * (close.iat[i] / resistance.iat[i] - 1),
                    "invalidation_price": support.iat[i],
                    "invalidation_basis": "spot" if support_spot.iat[i] else "qfq_or_mixed",
                    "reason": f"收盘突破前20日高{resistance.iat[i]:.3f}，量比{vol_ratio.iat[i]:.2f}，收盘位置{close_pos.iat[i]:.2f}，MA20>MA60；失效位为此前5日最低价。",
                }
            )
        if confirm.iat[i]:
            j = int(last_breach.iat[i])
            records.append(
                {
                    **common,
                    "strategy": "reversal_confirmed",
                    "trigger_price": prev_high.iat[i],
                    "score": 100 * (close.iat[i] / prev_high.iat[i] - 1),
                    "invalidation_price": structure_low.iat[i],
                    "invalidation_basis": "spot" if structure_spot.iat[i] else "qfq_or_mixed",
                    "reason": f"{stock.at[j,'date']}收盘低于当时BOLL20下轨{lower.iat[j]:.3f}，今收回下轨并首次收破前高{prev_high.iat[i]:.3f}；失效位为跌破日至确认日最低价。",
                }
            )
    ret = stock[["date", "secid"]].copy()
    # 收益用库内单日涨跌幅复利：快照为供应商涨跌幅，历史日线为同次获取的收盘价计算值。
    pct = pd.to_numeric(stock["pct_chg"], errors="coerce")
    logs = np.log1p(pct.where(pct.gt(-100) & np.isfinite(pct)) / 100)
    for n in (5, 20):
        contiguous = market_index.diff(n - 1).eq(n - 1)
        ret[f"r{n}"] = (np.expm1(logs.rolling(n).sum()) * 100).where(contiguous)
    return records, ret.loc[eligible]


def _sectors(picks, returns, meta):
    picks = picks.copy()
    picks["sector"] = picks["secid"].map(meta["sector"])
    picks["sector_asof"] = "current_snapshot_membership"
    picks["rank_quality"] = "missing_sector"
    for col in SECTOR_COLUMNS[3:]:
        picks[col] = np.nan
    tagged = returns.assign(sector=returns["secid"].map(meta["sector"])).dropna(subset=["sector"])
    if tagged.empty:
        return picks
    sizes = meta["sector"].value_counts()
    # 两周期都要有效，用同一批成分，避免不同期限的排序因成分切换失真。
    members = tagged.dropna(subset=["r5", "r20"])
    table = (
        members.groupby(["date", "sector"])
        .agg(n=("secid", "size"), t5=("r5", "sum"), t20=("r20", "sum"))
        .reset_index()
    )
    table["size"] = table["sector"].map(sizes)
    comparable = table[
        table["n"].ge(MIN_PEERS) & table["n"].div(table["size"]).ge(MIN_COVERAGE)
    ].copy()
    for n in (5, 20):
        comparable[f"avg{n}"] = comparable[f"t{n}"] / comparable["n"]
    rows = (
        picks[["date", "secid", "sector"]]
        .merge(tagged, on=["date", "secid", "sector"], how="left", validate="many_to_one")
        .merge(table, on=["date", "sector"], how="left", validate="many_to_one")
    )
    has_own = rows[["r5", "r20"]].notna().all(axis=1)
    peers = rows["n"].fillna(0) - has_own.astype(int)
    cover = peers / (rows["sector"].map(sizes) - 1).replace(0, np.nan)
    enough = peers.ge(MIN_PEERS) & cover.ge(MIN_COVERAGE) & has_own
    picks.loc[rows["sector"].notna(), "rank_quality"] = "insufficient_peers_or_coverage"
    picks.loc[rows["sector"].notna() & ~has_own, "rank_quality"] = "missing_stock_return"
    picks.loc[enough, "rank_quality"] = "insufficient_comparable_sectors"
    # 个股超额的对照组包含当日全部可计算股票（先算全市场，再映射到候选）。
    universe = members.merge(table, on=["date", "sector"], validate="many_to_one")
    peers_all = universe["n"] - 1
    universe = universe[
        peers_all.ge(MIN_PEERS)
        & peers_all.div((universe["size"] - 1).replace(0, np.nan)).ge(MIN_COVERAGE)
    ].copy()
    comps = {day: g.set_index("sector") for day, g in comparable.groupby("date")}
    for n in (5, 20):
        sector_ret = ((rows[f"t{n}"] - rows[f"r{n}"]) / peers.replace(0, np.nan)).where(enough)
        picks[f"sector_return_{n}d"] = sector_ret
        picks[f"relative_strength_{n}d"] = rows[f"r{n}"] - sector_ret
        picks[f"peer_count_{n}d"] = peers.where(rows["sector"].notna())
        picks[f"peer_coverage_{n}d"] = cover
        universe["excess"] = universe[f"r{n}"] - (universe[f"t{n}"] - universe[f"r{n}"]) / (
            universe["n"] - 1
        )
        # 包含自己时取并列中点，除以 N-1，与行业百分位同为 0..1。
        gr = universe["excess"].round(10).groupby(universe["date"])
        rank = (gr.rank(method="average") - 1) / (gr.transform("size") - 1).replace(0, np.nan)
        ranks = pd.Series(
            rank.to_numpy(), index=pd.MultiIndex.from_frame(universe[["date", "secid"]])
        )
        picks[f"relative_percentile_{n}d"] = ranks.reindex(
            pd.MultiIndex.from_frame(rows[["date", "secid"]])
        ).to_numpy()
        # 目标行业先剔除自身，再与当日其他达标行业等权比较。并列取中间百分位。
        for (day, sector), group in rows[enough].groupby(["date", "sector"]):
            if day not in comps:
                continue
            others = np.sort(
                comps[day][f"avg{n}"].drop(index=sector, errors="ignore").to_numpy().round(10)
            )
            if len(others):
                values = sector_ret.loc[group.index].to_numpy().round(10)
                percentile = (
                    np.searchsorted(others, values, side="left")
                    + np.searchsorted(others, values, side="right")
                ) / (2.0 * len(others))
                picks.loc[group.index, f"sector_percentile_{n}d"] = percentile
    ranks = [f"{field}_percentile_{n}d" for field in ("sector", "relative") for n in (5, 20)]
    picks["sector_rank_score"] = picks[ranks].mean(axis=1, skipna=False)
    picks.loc[picks["sector_rank_score"].notna(), "rank_quality"] = "exploratory_current_membership"
    return picks


def strategy_candidates(bars, symbols, sector_by_secid=None, *, start=None, asof=None, cfg=None):
    """返回未截取Top N的三类候选，日期为ISO日历日，所有价格均为入参日线单位。

    收盘后计算，次日才可交易。start 仅限制输出，asof 截断所有计算输入。固定
    规则分为确认收盘相对触发价的百分比（baseline用原信号评分并计入放量）。
    sector_rank_score 平均5/20日行业强度与个股同行超额的四项同日百分位（0..1），未知留空。
    成分/股票名称/ST状态来自调用方当前快照，不代表历史上市或行业档案。
    """
    empty = pd.DataFrame(columns=COLUMNS)
    if bars.empty:
        return empty
    meta = _metadata(symbols, sector_by_secid)
    dates = bars["date"].astype(str)
    in_range = dates.le(str(asof)) if asof else pd.Series(True, index=bars.index)
    calendar = sorted(dates[in_range].unique())
    if len(calendar) < MIN_BARS:
        return empty
    work = bars.loc[in_range & bars["secid"].isin(meta.index)].copy()
    work["date"] = work["date"].astype(str)
    if work.duplicated(["secid", "date"]).any():
        raise ValueError("重复 secid/date 日线")
    if "listed_date" in meta:
        listed = work["secid"].map(meta["listed_date"])
        work = work[listed.isna() | work["date"].ge(listed.fillna("").astype(str))]
    for col in ("open", "high", "low", "close", "volume", "pct_chg", "amount"):
        work[col] = pd.to_numeric(work[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    prices = work[["open", "high", "low", "close"]]
    valid = (
        prices.notna().all(axis=1)
        & work["low"].gt(0)
        & work["volume"].gt(0)
        & work["high"].ge(prices.max(axis=1))
        & work["low"].le(prices.min(axis=1))
    )
    work = work[valid].sort_values(["secid", "date"])
    if work.empty:
        return empty
    if "source" not in work:
        work["source"] = "unknown"
    hits = _volume_hits(work, calendar, start, cfg or Config(DEFAULTS, Path(".")))
    work["volume_up"] = pd.MultiIndex.from_frame(work[["secid", "date"]]).isin(hits)
    market_positions = {day: i for i, day in enumerate(calendar)}
    all_records, all_returns = [], []
    for _, stock in work.groupby("secid", sort=False, observed=True):
        if len(stock) < MIN_BARS:
            continue
        records, returns = _one_stock(stock, market_positions, start)
        all_records.extend(records)
        if not returns.empty:
            all_returns.append(returns)
    if not all_records:
        return empty
    picks = _sectors(
        pd.DataFrame.from_records(all_records), pd.concat(all_returns, ignore_index=True), meta
    )
    return (
        picks[COLUMNS]
        .sort_values(["date", "score", "secid", "strategy"], ascending=[True, False, True, True])
        .reset_index(drop=True)
    )
