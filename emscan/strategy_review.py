"""固定规则的离线比较和上线验收。所有行情来自已落库快照。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config
from .execution import settings
from .store import Store

VERSION = "short-term-review-v1"
POLICY = {
    "primary_horizon": 5,
    "max_candidates_per_day": 8,
    "warmup_valid_bars": 120,
    "development_fraction": 0.6,
    "embargo_sessions": 5,
    "minimum_test_dates": 60,
    "minimum_closed_orders": 100,
    "minimum_execution_coverage": 0.9,
    "bootstrap_repetitions": 5000,
    "bootstrap_block_sessions": 5,
    "bootstrap_seed": 20261110,
    "minimum_improvement_ci": 0.0,
    "require_point_in_time_universe_and_industries": True,
}
ARMS = {
    "baseline": (False, False, "fixed_5d"),
    "setups": (True, False, "fixed_5d"),
    "setups_sector": (True, True, "fixed_5d"),
    "setups_sector_exit": (True, True, "structure_time"),
}


def _statistics(orders):
    completed = [
        o
        for o in orders
        if o.get("eligible_for_validation") and o["status"] == "closed"
    ]
    values = [float(o["net_return"]) for o in completed]
    return {
        "selected": len(orders),
        "closed": len(values),
        "execution_coverage": len(values) / len(orders) if orders else None,
        "states": dict(Counter(o["status"] for o in orders)),
        "mean_net_return": float(np.mean(values)) if values else None,
        "median_net_return": float(np.median(values)) if values else None,
        "win_rate": float(np.mean(np.asarray(values) > 0)) if values else None,
        "worst_trade": min(values) if values else None,
        "mean_cost": (
            float(np.mean([o["cost"] for o in completed])) if completed else None
        ),
        "signal_dates_with_closed_orders": len({o["date"] for o in completed}),
    }


def _daily_spread(arms, start, end, market_dates):
    """按信号日等权比较共同可观察样本，整日重采样保留日内相关性。"""
    means = {}
    for name, orders in arms.items():
        rows = [
            o
            for o in orders
            if start <= o["date"] <= end
            and o.get("eligible_for_validation")
            and o["status"] == "closed"
        ]
        groups = {}
        for row in rows:
            groups.setdefault(row["date"], []).append(row["net_return"])
        means[name] = {d: float(np.mean(v)) for d, v in groups.items()}
    base = means["baseline"]
    result = {}
    for name in ARMS:
        if name == "baseline":
            continue
        dates = sorted(set(base) & set(means[name]))
        deltas = np.array([means[name][d] - base[d] for d in dates])
        interval = None
        if len(dates) >= 2:
            rng = np.random.default_rng(POLICY["bootstrap_seed"])
            # 连续市场日期的移动块保持重叠持仓影响。缺一侧成交的日期不得压缩为相邻日。
            grid = [d for d in market_dates if start <= d <= end]
            daily = np.array(
                [means[name].get(d, np.nan) - base.get(d, np.nan) for d in grid]
            )
            width = min(POLICY["bootstrap_block_sessions"], len(grid))
            starts = rng.integers(
                0,
                len(grid),
                size=(
                    POLICY["bootstrap_repetitions"],
                    (len(grid) + width - 1) // width,
                ),
            )
            indices = (starts[:, :, None] + np.arange(width)) % len(grid)
            sampled = daily[
                indices.reshape(POLICY["bootstrap_repetitions"], -1)[:, : len(grid)]
            ]
            sample = np.nanmean(sampled, axis=1)
            # 三个候选方案同时比较，使用 Bonferroni 调整的双侧区间。
            tail = 0.05 / (2 * (len(ARMS) - 1))
            sample = sample[np.isfinite(sample)]
            if len(sample) >= POLICY["bootstrap_repetitions"] * 0.95:
                interval = [float(x) for x in np.quantile(sample, [tail, 1 - tail])]
        result[name] = {
            "common_signal_dates": len(dates),
            "mean_daily_difference": float(deltas.mean()) if len(deltas) else None,
            "simultaneous_95pct_interval": interval,
            "unit": "decimal_return",
            "limitation": "conditional_on_comparable_closed_orders",
            "block_sessions": POLICY["bootstrap_block_sessions"],
        }
    return result


def portfolio(orders, prices, days, start, capital=800000.0):
    """共享现金的八槽资金回放。未来无法验证的持仓继续占用资金并标出估值缺口。"""
    cash = capital
    holdings = {}
    closed = []
    blocked = Counter()
    nav = []
    complete = True
    traded_notional = 0.0
    filled = 0
    planned = {}
    for order in orders:
        buy = order.get("buy")
        if order["date"] >= start and buy:
            planned.setdefault(buy["date"], []).append(order)
    for day in days:
        if day < start:
            continue

        def sell_due(phase):
            nonlocal cash, traded_notional
            for sid, order in list(holdings.items()):
                sell = order.get("sell") or {}
                if (
                    sell.get("date") == day
                    and sell.get("execution_phase", "close") == phase
                ):
                    cash += sell["cash"]
                    traded_notional += sell.get("gross", sell["cash"])
                    closed.append(order)
                    del holdings[sid]

        sell_due("open")
        for order in planned.get(day, []):
            sid, buy = order["secid"], order["buy"]
            if sid in holdings:
                blocked["already_held"] += 1
            elif len(holdings) >= 8 or buy["cash"] > cash + 1e-8:
                blocked["cash_or_slots"] += 1
            else:
                cash -= buy["cash"]
                holdings[sid] = order
                traded_notional += buy.get("gross", buy["cash"])
                filled += 1
        sell_due("close")
        value = cash
        for sid, order in holdings.items():
            bar = prices[sid].get(day) or {}
            valid_price = (
                isinstance(bar.get("close"), (float, int))
                and math.isfinite(bar["close"])
                and bar["close"] > 0
            )
            if (
                bar.get("source") != "spot"
                or not valid_price
                or order["status"] == "unavailable"
                or order.get("path_gaps")
            ):
                complete = False
                blocked["unverified_valuation_days"] += 1
            mark = (
                bar["close"] if valid_price else order["buy"]["price"]
            )
            value += mark * order["buy"]["shares"]
        nav.append(
            {
                "date": day,
                "equity": round(value, 2),
                "cash": round(cash, 2),
                "holdings": len(holdings),
            }
        )
    equity = np.array([capital] + [n["equity"] for n in nav])
    drawdowns = equity / np.maximum.accumulate(equity) - 1
    execution_complete = all(
        o["status"] != "unavailable" and not o.get("path_gaps")
        for o in orders if o["date"] >= start
    )
    return {
        "initial_capital": capital,
        "order_budget": 100000.0,
        "max_positions": 8,
        "net_return": float(equity[-1] / capital - 1),
        "max_drawdown": float(drawdowns.min()),
        "valuation_complete": complete,
        "execution_data_complete": execution_complete,
        "valid_for_comparison": complete and execution_complete,
        "completed_trades": len(closed),
        "filled_trades": filled,
        "gross_traded_notional": round(traded_notional, 2),
        "gross_turnover_on_initial_capital": traded_notional / capital,
        "open_positions": len(holdings),
        "blocked": dict(blocked),
        "daily_equity": nav,
        "valuation_basis": "close_mark_open_inventory_not_liquidated",
    }


def _portfolio_differences(portfolios):
    daily = {}
    for name, result in portfolios.items():
        equity = np.array(
            [result["initial_capital"]] + [r["equity"] for r in result["daily_equity"]]
        )
        daily[name] = np.diff(equity) / equity[:-1]
    output = {}
    base = daily["baseline"]
    for name, values in daily.items():
        if name == "baseline":
            continue
        differences = values - base
        interval = None
        if len(differences) >= 2:
            rng = np.random.default_rng(POLICY["bootstrap_seed"])
            width = min(POLICY["bootstrap_block_sessions"], len(differences))
            starts = rng.integers(
                0,
                len(differences),
                size=(
                    POLICY["bootstrap_repetitions"],
                    (len(differences) + width - 1) // width,
                ),
            )
            indices = (
                (starts[:, :, None] + np.arange(width)) % len(differences)
            ).reshape(POLICY["bootstrap_repetitions"], -1)
            sample = differences[indices[:, : len(differences)]].mean(axis=1)
            tail = 0.05 / (2 * (len(ARMS) - 1))
            interval = np.quantile(sample, [tail, 1 - tail]).tolist()
        output[name] = {
            "market_sessions": len(differences),
            "mean_daily_difference": (
                float(differences.mean()) if len(differences) else None
            ),
            "simultaneous_95pct_interval": interval,
            "block_sessions": POLICY["bootstrap_block_sessions"],
            "unit": "decimal_daily_portfolio_return",
        }
    return output


def promotion_decision(
    stats,
    differences,
    provenance,
    portfolio=None,
    baseline_portfolio=None,
    stress=None,
    baseline_stats=None,
    baseline_stress=None,
):
    """验收条件预先固定，缺证据或未证实改善时保持未批准。"""
    reasons = []
    if not provenance["point_in_time_universe"]:
        reasons.append("historical_universe_and_status_unverified")
    if not provenance["point_in_time_industries"]:
        reasons.append("historical_industry_membership_unverified")
    if not provenance.get("comparable_indicator_price_history"):
        reasons.append("indicator_price_basis_unverified")
    if stats["signal_dates_with_closed_orders"] < POLICY["minimum_test_dates"]:
        reasons.append("insufficient_out_of_sample_dates")
    if stats["closed"] < POLICY["minimum_closed_orders"]:
        reasons.append("insufficient_closed_orders")
    if (stats["execution_coverage"] or 0) < POLICY["minimum_execution_coverage"]:
        reasons.append("insufficient_execution_coverage")
    if not math.isfinite(stats.get("execution_coverage") or 0):
        reasons.append("invalid_execution_coverage")
    interval = differences.get("simultaneous_95pct_interval")
    if (
        not interval
        or not all(math.isfinite(v) for v in interval)
        or interval[0] <= POLICY["minimum_improvement_ci"]
    ):
        reasons.append("net_improvement_not_established")
    if (
        not baseline_stats
        or baseline_stats["signal_dates_with_closed_orders"]
        < POLICY["minimum_test_dates"]
        or baseline_stats["closed"] < POLICY["minimum_closed_orders"]
        or not math.isfinite(baseline_stats["execution_coverage"] or 0)
        or (baseline_stats["execution_coverage"] or 0)
        < POLICY["minimum_execution_coverage"]
    ):
        reasons.append("baseline_evidence_insufficient")
    valid_portfolios = all(
        p
        and p.get("valid_for_comparison")
        and all(math.isfinite(p[k]) for k in ("max_drawdown", "net_return"))
        for p in (portfolio, baseline_portfolio)
    )
    if not valid_portfolios:
        reasons.append("portfolio_drawdown_validation_pending")
    elif portfolio["max_drawdown"] < baseline_portfolio["max_drawdown"] - 0.02:
        reasons.append("portfolio_drawdown_deteriorated")
    elif portfolio["net_return"] <= baseline_portfolio["net_return"]:
        reasons.append("portfolio_net_return_not_improved")
    if (
        not stress
        or not baseline_stress
        or not stress.get("valid_for_comparison")
        or not baseline_stress.get("valid_for_comparison")
        or not all(
            math.isfinite(p.get("net_return", float("nan")))
            for p in (stress, baseline_stress)
        )
        or stress["net_return"] <= max(0, baseline_stress["net_return"])
    ):
        reasons.append("double_slippage_stress_not_passed")
    return {"approved": not reasons, "reasons": reasons}


def _select(frame, name):
    setups, sector, _ = ARMS[name]
    mask = frame["strategy"].isin(
        ["trend_breakout", "reversal_confirmed"] if setups else ["baseline_core"]
    )
    rows = frame.loc[mask].copy()
    columns = (
        ["date"]
        + (["sector_rank_score"] if sector else [])
        + ["score", "secid", "strategy"]
    )
    ascending = [True] + ([False] if sector else []) + [False, True, True]
    rows = rows.sort_values(
        columns, ascending=ascending, na_position="last"
    ).drop_duplicates(["date", "secid"])
    return rows.groupby("date", sort=False).head(POLICY["max_candidates_per_day"])


def _published_reference(store, cfg):
    from .backtest import evaluate

    picks = store.load_tier_picks()
    if picks.empty:
        return {"closed": 0, "role": "published_list_reference_only"}
    picks = picks[
        picks["created_at"].astype(str).str[:10].eq(picks["date"].astype(str))
    ]
    picks = picks[~picks["secid"].str.startswith(("1.688", "1.689"))]
    result = evaluate(store, cfg, picks)
    orders = []
    for row in result["rows"]:
        item = row["execution"]
        outcome = item["exits"].get(5, item)
        orders.append(
            {
                **outcome,
                "date": row["date"],
                "cost": outcome.get("total_cost"),
                "eligible_for_validation": outcome["status"] == "closed",
            }
        )
    return {
        **_statistics(orders),
        "role": "published_list_reference_only",
        "comparison_warning": "different_dates_and_ai_selection_not_paired_to_reconstructed_rules",
    }


def run(store, cfg):
    from .strategy_rules import strategy_candidates
    from .strategy_execution import simulate_candidate
    from .readiness import code_version

    conn = store.conn
    end = store.latest_signal_date()
    if not end:
        raise ValueError("缺少已完成的每日信号日期")
    # 较早稀疏回补不作为全市场历史。预热和判定只使用当日及此前行情。
    covered_dates = [
        r[0]
        for r in conn.execute(
            "SELECT date FROM bars WHERE date<=? AND volume>0 AND close>0 GROUP BY date HAVING COUNT(*)>=5000 ORDER BY date",
            (end,),
        )
    ]
    if (
        len(covered_dates)
        <= POLICY["warmup_valid_bars"] + 2 * POLICY["primary_horizon"]
    ):
        raise ValueError("完整市场历史不足以分割回测")
    dates = [d for d in store.all_trading_days() if covered_dates[0] <= d <= end]
    columns = "secid,date,open,high,low,close,volume,amount,pct_chg,chg,source,turnover,float_mv,volume_ratio"
    bars = pd.read_sql_query(
        f"SELECT {columns} FROM bars WHERE date BETWEEN ? AND ? ORDER BY secid,date",
        conn,
        params=(dates[0], end),
    )
    for key in ("secid", "source", "date"):
        bars[key] = bars[key].astype("category")
    if "unknown" not in bars["source"].cat.categories:
        bars["source"] = bars["source"].cat.add_categories(["unknown"])
    symbols = store.symbols_df()
    tags = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT secid,MIN(name) FROM symbol_tags WHERE kind='sw' GROUP BY secid"
        )
    }
    day_index = {d: i for i, d in enumerate(dates)}
    signal_dates = [
        d
        for d in covered_dates[POLICY["warmup_valid_bars"] - 1 :]
        if day_index[d] + POLICY["primary_horizon"] + 1 < len(dates)
    ]
    split = int(len(signal_dates) * POLICY["development_fraction"])
    test_boundary = day_index[signal_dates[split]] + POLICY["embargo_sessions"]
    test_dates = [d for d in signal_dates if day_index[d] >= test_boundary]
    if not test_dates:
        raise ValueError("隔离训练边界后没有样本外信号日")
    frame = strategy_candidates(
        bars, symbols, tags, start=signal_dates[0], asof=end, cfg=cfg
    )
    frame = frame[frame["date"].isin(signal_dates)]
    # 配置、本次输入库版本和每个候选的D日依据一起保留，支持重跑核对。
    arms = {}
    stress_arms = {}
    opts = settings(cfg)
    stress_opts = {**opts, "slippage_bps": opts["slippage_bps"] * 2}
    series_columns = [
        "secid",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "source",
        "pct_chg",
        "chg",
    ]
    first_seen = (
        bars.groupby("secid", observed=True)["date"]
        .agg(lambda x: str(x.iloc[0]))
        .to_dict()
    )
    needed = set(frame["secid"])
    recent = bars[
        bars["secid"].isin(needed)
        & bars["date"].astype(str).ge(dates[POLICY["warmup_valid_bars"] - 7])
    ]
    grouped = {
        sid: {str(r["date"]): r for r in group.to_dict("records")}
        for sid, group in recent[series_columns].groupby(
            "secid", sort=False, observed=True
        )
    }
    del bars, recent
    name_by_sid = dict(zip(symbols["secid"], symbols["name"]))
    for name in ARMS:
        orders = []
        stressed = []
        for row in _select(frame, name).to_dict("records"):
            row = {k: v if pd.notna(v) else None for k, v in row.items()}
            row.update(
                name=name_by_sid.get(row["secid"], ""),
                first_seen=first_seen[row["secid"]],
                point_in_time=True,
                is_baseline=name == "baseline",
            )
            outcome = simulate_candidate(
                row,
                dates,
                grouped[row["secid"]],
                opts=opts,
                exit_policy=ARMS[name][2],
                time_exit=POLICY["primary_horizon"],
            )
            orders.append(
                {
                    **outcome,
                    "date": row["date"],
                    "secid": row["secid"],
                    "strategy": row["strategy"],
                    "signal": row,
                }
            )
            stress_order = simulate_candidate(
                row,
                dates,
                grouped[row["secid"]],
                opts=stress_opts,
                exit_policy=ARMS[name][2],
                time_exit=POLICY["primary_horizon"],
            )
            stressed.append(
                {
                    **stress_order,
                    "date": row["date"],
                    "secid": row["secid"],
                    "strategy": row["strategy"],
                }
            )
        arms[name] = orders
        stress_arms[name] = stressed
    provenance = {
        "point_in_time_universe": False,
        "point_in_time_industries": False,
        "comparable_indicator_price_history": False,
        "price_basis": "stored_qfq_history_plus_unadjusted_spot",
        "data_version": store.data_version(),
        "sector_tags_asof": store.get_meta("symbol_tags_updated"),
        "signal_provenance": "historical_rule_reconstruction",
    }
    spread = _daily_spread(arms, test_dates[0], signal_dates[-1], dates)
    portfolios = {
        name: portfolio(orders, grouped, dates, test_dates[0])
        for name, orders in arms.items()
    }
    stressed_portfolios = {
        name: portfolio(orders, grouped, dates, test_dates[0])
        for name, orders in stress_arms.items()
    }
    portfolio_spread = _portfolio_differences(portfolios)
    summary = {}
    for name, orders in arms.items():
        test = [r for r in orders if r["date"] in test_dates]
        summary[name] = {
            "all": _statistics(orders),
            "out_of_sample": _statistics(test),
            "out_of_sample_portfolio": portfolios[name],
            "double_slippage_portfolio": stressed_portfolios[name],
        }
        if name != "baseline":
            summary[name]["promotion"] = promotion_decision(
                summary[name]["out_of_sample"],
                portfolio_spread[name],
                provenance,
                portfolios[name],
                portfolios["baseline"],
                stressed_portfolios[name],
                _statistics([r for r in arms["baseline"] if r["date"] in test_dates]),
                stressed_portfolios["baseline"],
            )
    report = {
        "version": VERSION,
        "source_code_version": code_version(),
        "policy_sha256": hashlib.sha256(
            json.dumps(POLICY, sort_keys=True).encode()
        ).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asof": end,
        "policy": POLICY,
        "provenance": provenance,
        "coverage": {
            "market_sessions": len(dates),
            "signal_sessions": len(signal_dates),
            "start": signal_dates[0],
            "end": signal_dates[-1],
            "test_start": test_dates[0],
            "test_end": test_dates[-1],
            "test_sessions": len(test_dates),
        },
        "arms": summary,
        "out_of_sample_differences": spread,
        "portfolio_differences": portfolio_spread,
        "approved_strategies": [
            name
            for name in ARMS
            if name != "baseline" and summary[name]["promotion"]["approved"]
        ],
        "conclusion": "insufficient_evidence",
        "live_selection_changed": False,
        "execution_parameters": opts,
        "published_ai_reference": _published_reference(store, cfg),
        "comparison_unit": "independent_order_statistics_and_separate_800000_cny_shared_cash_portfolios",
        "limitations": [
            "current_surviving_universe",
            "current_industry_tags",
            "historical_price_basis_mixed",
            "unavailable_executions_are_censored_not_zero_returns",
            "overlapping_5day_holds",
            "short_out_of_sample",
        ],
    }
    return report, arms


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    cfg = Config.load()
    with Store(cfg.path("store.path")).init_schema() as store:
        result, orders = run(store, cfg)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, value in (("report.json", result), ("orders.json", orders)):
        payload = json.dumps(
            value, ensure_ascii=False, allow_nan=False, indent=2, default=str
        )
        target = args.out / name
        target.write_text(payload, encoding="utf8")
        print(name, hashlib.sha256(payload.encode()).hexdigest())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
