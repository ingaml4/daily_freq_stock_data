"""Independent frozen-cohort sizing experiment; does not change the O/R report."""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from . import entry_minute_study as minute, execution, strategy_execution
from .entry_minute_report import read_candidates, attach_capture_config
from .entry_study import market_sessions, SHANGHAI
from .opportunity_archive import _atomic_write
from .position_sizing import Policy, VERSION, size_order

MODES = ("all_in", "fixed", "risk", "risk_first")


def _themes(record):
    # Every declared mainline counts; SW2 is only an industry proxy, labelled below.
    keys = [x["key"] for x in record.get("mainlines", [])]
    return sorted(set(keys or record.get("sector_membership", {}).keys()))


def _theme_time(record):
    times = [minute._stamp(record["generated_at"])]
    if not record.get("mainlines"):
        for member in record.get("sector_membership", {}).values():
            stamp = member.get("observed_at")
            if not stamp:
                return None
            try:
                parsed = datetime.fromisoformat(stamp)
            except (ValueError, TypeError):
                return None
            times.append(parsed.replace(tzinfo=SHANGHAI) if parsed.utcoffset() is None else parsed)
    return max(times).isoformat()


def compare(records, prices, days, as_of, *, capital=240000.0, policy=None):
    """One frozen signal-day cohort. Allocate at entry cost against starting capital.

    This deliberately has no equity compounding or intraday mark inference. All
    sizing modes see the same one-lot-qualified quote and original signal floors.
    The full size is bounded by that displayed quote; no search for a later fill.
    """
    policy = policy or Policy()
    if not records or len({r["date"] for r in records}) != 1:
        raise ValueError("exactly one non-empty frozen signal-day cohort required")
    if isinstance(capital, bool) or not isinstance(capital, (float, int)) or not 0 < capital < 1e12:
        raise ValueError("invalid research capital")
    as_of = minute._stamp(as_of)
    day = records[0]["date"]
    unique = {}
    for r in sorted(records, key=lambda r: (r["secid"], r["mode"])):
        unique.setdefault(r["secid"], r)
    rows = list(unique.values())
    opts = execution.settings({})
    result = {
        "version": VERSION, "research_only": True, "promoted": False,
        "signal_date": day, "as_of": as_of.isoformat(), "initial_capital": capital,
        "candidate_count": len(records), "unique_securities": len(rows),
        "overlapping_modes_removed": len(records) - len(rows),
        "policy": asdict(policy), "costs": opts,
        "allocation_basis": "starting_capital_and_entry_cost_exposures_single_cohort",
        "entry_basis": "same_first_one_lot_qualified_quote_per_O_R_arm",
        "theme_basis": "frozen_mainlines_else_frozen_SW2_industry_proxy",
        "limitations": ["book_quotes_do_not_prove_actual_fills", "one_cohort_does_not_validate_parameters",
                        "six_percent_stress_is_a_scenario_not_a_loss_ceiling",
                        "SW2_industry_does_not_capture_all_correlated_themes",
                        "intraday_correlation_and_live_broker_balances_unavailable",
                        "risk_first_has_no_automatic_adds_without_separate_evidence"],
        "arms": {},
    }
    for arm in ("O", "R"):
        arm_result = {}
        queued = sorted(rows, key=lambda r: ((r["orders"][arm].get("buy") or {}).get("at", ""), r["secid"]))
        for mode in MODES:
            cash, exposure, ledger = capital, [], []
            for row in queued:
                original = row["orders"][arm]
                item = {"candidate_key": row["candidate_key"], "name": row["name"],
                        "themes": _themes(row), "entry_status": original["entry_status"],
                        "entry_reason": original.get("reason"), "sizing": None, "order": None}
                ledger.append(item)
                if not original.get("buy"):
                    continue
                order = deepcopy(original)
                buy = order["buy"]
                at = buy["at"]
                if minute._stamp(at) > as_of:
                    raise ValueError("future buy evidence")
                # Quantity probe must be explicit to prevent silently selecting only 100k fills.
                if row.get("quantity_basis") != "one_lot_probe_for_separate_sizing_research":
                    raise ValueError("requires independent one-lot entry probe")
                candidate = {
                    "secid": row["secid"], "entry_price": buy["price"], "risk_price": row["risk"],
                    "at": at, "risk_available_at": row["generated_at"],
                    "themes_available_at": _theme_time(row), "themes": _themes(row),
                    "ask_shares": buy["evidence"]["ask_shares"], "execution_verified": True,
                }
                account = {"kind": "research", "as_of": at, "equity": capital,
                           "monthly_peak_equity": capital, "cash_available": cash,
                           "cash_basis": "net_of_pending", "positions": exposure, "pending_orders": []}
                sizing = size_order(candidate, account, mode=mode, policy=policy, costs=opts)
                item["sizing"] = sizing
                if not sizing["shares"]:
                    continue
                qty = sizing["shares"]
                order["buy"] = {**buy, **execution._cost(buy["price"], qty, "buy", opts)}
                for field in ("sell", "net_return", "net_pnl", "cost", "total_cost"):
                    order[field] = None
                order["eligible_for_validation"] = False
                for field in ("path_gaps", "problem_date", "exit_attempts"):
                    order.pop(field, None)
                minute._exit(order, days, days.index(day), strategy_execution._Bars(prices[row["secid"]]),
                             opts, execution._board(row["secid"])[0], as_of)
                item["order"] = order
                cash = sizing["remaining_cash"]
                exposure.append({"id": row["secid"], "secid": row["secid"], "themes": _themes(row),
                                 "market_value": order["buy"]["gross"], "planned_loss": sizing["planned_loss"],
                                 "stress_loss": sizing["stress_loss"]})
            arm_result[mode] = _metrics(ledger, prices, days, day, as_of, capital, exposure)
        result["arms"][arm] = arm_result
    return result


def _metrics(ledger, prices, days, signal_day, as_of, capital, exposure):
    entered = [x for x in ledger if x["order"]]
    entry_complete = all(x["entry_status"] not in ("unavailable", "pending_entry", "excluded")
                         and not (x["sizing"] and x["sizing"]["status"] == "unavailable") for x in ledger)
    nav, cash, held, complete = [], capital, {}, True
    worst_gap, streak, max_streak = None, 0, 0
    closed, skipped = [], Counter()
    for item in ledger:
        sizing = item["sizing"]
        if sizing and not sizing["shares"]:
            skipped.update(sizing["reasons"])
    for day in days:
        if day <= signal_day or day > as_of.date().isoformat() or (day == as_of.date().isoformat() and as_of.hour < 15):
            continue
        for item in entered:
            order = item["order"]
            if order["buy"]["date"] == day:
                held[order["secid"]] = item
                cash -= order["buy"]["cash"]
        value = cash
        for sid, item in list(held.items()):
            order = item["order"]
            bars = strategy_execution._Bars(prices[sid])
            bar = bars.get(day)
            prev = bars.get(days[days.index(day) - 1])
            reason, _ = execution._session(bar, prev, execution._board(sid)[0])
            if reason or order.get("path_gaps"):
                complete = False
            elif day > order["buy"]["date"]:
                gap = bar["open"] / prev["close"] - 1
                worst_gap = gap if worst_gap is None else min(worst_gap, gap)
            sell = order.get("sell")
            if sell and sell["date"] == day:
                cash += sell["cash"]
                value += sell["cash"]
                closed.append(item)
                streak = streak + 1 if order["net_pnl"] < 0 else 0
                max_streak = max(max_streak, streak)
                del held[sid]
            elif not reason:
                value += bar["close"] * order["buy"]["shares"]
        nav.append({"date": day, "equity": execution._money(value) if complete else None,
                    "cash": execution._money(cash), "positions": len(held),
                    "capital_utilization": (value - cash) / value if complete and value > 0 else None})
    peak, drawdown = capital, 0.0
    for point in nav:
        if point["equity"] is not None:
            peak = max(peak, point["equity"])
            drawdown = max(drawdown, 1 - point["equity"] / peak)
    eligible = sum(x["sizing"] is not None for x in ledger)
    theme_values = defaultdict(float)
    for row in exposure:
        for theme in row["themes"]:
            theme_values[theme] += row["market_value"]
    for item in closed:
        item["net_R"] = item["order"]["net_pnl"] / item["sizing"]["planned_loss"]
    target = days[days.index(signal_day) + 5] if days.index(signal_day) + 5 < len(days) else None
    target_reached = target is not None and bool(nav) and nav[-1]["date"] >= target
    mature = target_reached and len(closed) == len(entered) and complete and entry_complete
    total = nav[-1]["equity"] if nav else None
    return {
        "candidate_count": len(ledger), "quote_eligible_count": eligible, "sized_count": len(entered),
        "completed_count": len(closed), "pending_or_unverifiable_exit_count": len(entered) - len(closed),
        "sizing_rejection_rate": (eligible - len(entered)) / eligible if eligible else None,
        "sizing_reasons": dict(skipped), "entry_data_complete": entry_complete,
        "valuation_complete": complete and bool(nav), "outcomes_mature": mature,
        "net_return_to_date": total / capital - 1 if total is not None and complete and entry_complete else None,
        "mature_net_return": total / capital - 1 if mature and total is not None else None,
        "max_drawdown": drawdown if complete and entry_complete and nav else None,
        "worst_observed_overnight_gap": worst_gap if complete else None,
        "max_consecutive_closed_losses": max_streak if closed else None,
        "loss_streak_order": "exit_day_then_entry_receipt_then_security",
        "net_R": [x["net_R"] for x in closed],
        "fees_closed": execution._money(sum(x["order"]["total_cost"] for x in closed)),
        "entry_cost_utilization": sum(x["market_value"] for x in exposure) / capital,
        "entry_planned_loss": sum(x["planned_loss"] for x in exposure),
        "entry_correlated_stress_loss": sum(x["stress_loss"] for x in exposure),
        "entry_security_HHI_on_equity": sum((x["market_value"] / capital) ** 2 for x in exposure),
        "entry_theme_weights": {k: v / capital for k, v in theme_values.items()},
        "estimated_return_correlation": None,
        "adds": {"executed": 0, "status": "needs_distinct_later_signal_and_book_evidence"},
        "daily_equity": nav, "ledger": ledger,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("db", "archive", "capture-config", "events", "out"):
        parser.add_argument("--" + key, required=True, type=Path)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    parser.add_argument("--capital", type=float, default=240000)
    args = parser.parse_args()
    if args.as_of.utcoffset() is None or args.as_of > datetime.now(SHANGHAI):
        parser.error("as-of must have a timezone and must not be in the future")
    day, candidates, archive_info = read_candidates(args.archive, args.archive_sha256)
    entry_day, config_sha = attach_capture_config(args.capture_config, day, args.archive_sha256, candidates)
    # Caller supplies an immutable captured snapshot, never truncates a running producer.
    raw = args.events.read_bytes()
    events = minute.read_events(raw.decode("utf-8-sig").splitlines())
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        end = max(args.as_of.astimezone(SHANGHAI).date(), date.fromisoformat(day) + timedelta(days=20))
        days = market_sessions(conn, (date.fromisoformat(day) - timedelta(days=20)).isoformat(), end.isoformat())
        if days[days.index(day) + 1] != entry_day:
            raise ValueError("entry date disagrees with cached exchange calendar")
        prices = defaultdict(dict)
        records = []
        for candidate in candidates:
            sid = candidate["secid"]
            candidate["first_seen"] = conn.execute("SELECT MIN(date) FROM bars WHERE secid=? AND close>0 AND date<=?", (sid, day)).fetchone()[0]
            for bar in conn.execute("SELECT * FROM bars WHERE secid=? AND date>=? AND date<=?", (sid, day, end.isoformat())):
                prices[sid][bar["date"]] = dict(bar)
            records.append(minute.replay_candidate(candidate, events, days, prices[sid], as_of=args.as_of, quantity_probe=True))
        result = compare(records, prices, days, args.as_of, capital=args.capital)
    result.update(archive_sha256=args.archive_sha256, capture_config_sha256=config_sha,
                  events_sha256=hashlib.sha256(raw).hexdigest(), archive=archive_info)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(args.out, encoded)
    print(json.dumps({"out": str(args.out), "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                      "candidate_count": result["candidate_count"]}))


if __name__ == "__main__":
    main()
