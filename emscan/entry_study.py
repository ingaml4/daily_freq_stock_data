"""Offline matched opening versus daily-confirmation entry experiment."""

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean, median

from . import execution, opportunity_archive, strategy_execution
from .readiness import code_version
from .strategy_review import _statistics, portfolio

VERSION = "abc-entry-timing-v1"
ARMS = {"O": "open", "R": "daily_confirmation"}
MODES = ("A", "B", "C")
NO_FILL = {"entry_expired", "entry_invalidated", "confirmation_not_met", "confirmation_lost"}
SHANGHAI = strategy_execution.SHANGHAI


class Snapshot:
    def __init__(self, conn, path):
        self.conn = conn
        self.path = path


def market_sessions(conn, start, end):
    days = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        row = conn.execute("SELECT value FROM meta WHERE key=?", (f"mairui_calendar:{year}",)).fetchone()
        annual = json.loads(row[0])["days"] if row else []
        if len(annual) < 220 or len(set(annual)) != len(annual):
            raise ValueError(f"缺少可核验的 {year} 年完整交易日历")
        days.extend(d for d in annual if start <= d <= end)
    return sorted(days)


def _is_dated_archive(payload, day):
    if payload["provenance"]["mode"] != "archived":
        return False
    try:
        stamp = datetime.fromisoformat(payload["generated_at"])
    except ValueError:
        return False
    if stamp.utcoffset() is None:
        return False
    local = stamp.astimezone(SHANGHAI)
    return local.date().isoformat() == day and local.time() >= time(15)


def _pairs(records):
    counts = Counter()
    comparable = []
    skipped, only_r, matched_policy = [], [], []
    for row in records:
        o, r = row["orders"]["O"], row["orders"]["R"]
        o_closed, r_closed = (x["eligible_for_validation"] for x in (o, r))
        counts["both_entries"] += bool(o["buy"] and r["buy"])
        counts["only_O_entry"] += bool(o["buy"] and not r["buy"])
        counts["only_R_entry"] += bool(r["buy"] and not o["buy"])
        counts["neither_entry"] += not (o["buy"] or r["buy"])
        if o_closed and r_closed:
            comparable.append((o["net_return"], r["net_return"]))
        elif o_closed and r["status"] in NO_FILL:
            counts["only_O_closed_other_known_no_fill"] += 1
            skipped.append(o["net_return"])
            sign = "loss_skipped" if o["net_return"] < 0 else "gain_missed" if o["net_return"] > 0 else "flat_skipped"
            counts[sign] += 1
        elif r_closed and o["status"] in NO_FILL:
            counts["only_R_closed_other_known_no_fill"] += 1
            only_r.append(r["net_return"])
        elif o_closed or r_closed:
            counts["one_closed_other_unresolved"] += 1
        if (o_closed or o["status"] in NO_FILL) and (r_closed or r["status"] in NO_FILL):
            matched_policy.append(((r["net_return"] if r_closed else 0) - (o["net_return"] if o_closed else 0)))
    a = [o for o, _ in comparable]
    b = [r for _, r in comparable]
    diff = [r - o for o, r in comparable]
    return {**counts, "both_closed": len(diff), "O_mean": mean(a) if a else None,
            "R_mean": mean(b) if b else None, "R_minus_O_mean": mean(diff) if diff else None,
            "R_minus_O_median": median(diff) if diff else None,
            "only_O_mean_when_R_known_no_fill": mean(skipped) if skipped else None,
            "only_R_mean_when_O_known_no_fill": mean(only_r) if only_r else None,
            "both_outcomes_known": len(matched_policy),
            "matched_policy_R_minus_O_mean_cash_for_known_no_fill": mean(matched_policy) if matched_policy else None}


def _stats(records):
    stats = {"candidates": len(records), "paired": _pairs(records)}
    for arm in ARMS:
        orders = [row["orders"][arm] for row in records]
        stats[arm] = _statistics(orders)
        stats[arm]["entries"] = sum(bool(order["buy"]) for order in orders)
        stats[arm]["known_no_entry"] = sum(order["status"] in NO_FILL for order in orders)
        stats[arm]["path_gap_orders"] = sum(bool(order.get("path_gaps")) for order in orders)
        stats[arm]["reason_counts"] = dict(Counter(order["reason"] for order in orders))
    return stats


def _cash(records, prices, days, start, key="orders", priority="secid_asc"):
    # Choose the same representative in both arms, before looking at their outcomes.
    ordered = sorted(records, key=lambda r: (r["date"], r["secid"], MODES.index(r["mode"])))
    unique = {}
    for row in ordered:
        unique.setdefault((row["date"], row["secid"]), row)
    queue = list(unique.values())
    if priority == "secid_desc":
        queue.sort(key=lambda r: r["secid"], reverse=True)
    elif priority == "signal_amount_desc":
        queue.sort(key=lambda r: -(prices[r["secid"]].get(r["date"], {}).get("amount") or 0))
    books = {arm: portfolio([r[key][arm] for r in queue], prices, days, start)
             for arm in ARMS}
    return {"unique_candidates": len(unique), "overlapping_modes_removed": len(records) - len(unique),
            "same_day_priority": priority, "representative_mode_priority": list(MODES), "arms": books}


def _timing_diagnostics(mature, maturity_dates, prices, days, start):
    records_by_day = defaultdict(list)
    for row in mature:
        records_by_day[row["date"]].append(row)
    spreads = {day: _pairs(records_by_day[day])["matched_policy_R_minus_O_mean_cash_for_known_no_fill"]
               for day in maturity_dates}
    split = len(maturity_dates) // 2
    bands = {"all": maturity_dates, "first_half": maturity_dates[:split], "second_half": maturity_dates[split:]}
    by_window = {}
    for label, window in bands.items():
        values = [spreads[d] for d in window if spreads[d] is not None]
        by_window[label] = {"signal_days": len(window), "days_with_known_pairs": len(values),
                            "mean_R_minus_O_equal_signal_day": mean(values) if values else None}
    return {"daily_policy_difference": {"by_day": spreads, "windows": by_window,
                                        "basis": "known outcomes only, known no fill counts as cash"},
            "queue_priority_sensitivity": {priority: _cash(mature, prices, days, start, priority=priority)
                                           for priority in ("secid_desc", "signal_amount_desc")}}


def _minute_audit(folder, records):
    wanted = {(r["entry_session"], r["symbol"]) for r in records if r["entry_session"]}
    observed = set()
    files = []
    if folder:
        for path in sorted(folder.glob("intraday-frames-*.jsonl")):
            day = path.name[16:26]
            symbols = {symbol for d, symbol in wanted if d == day}
            if not symbols:
                continue
            files.append(path.name)
            with path.open(encoding="utf-8-sig") as stream:
                for line in stream:
                    packet = json.loads(line)
                    for frame in packet["frames"]:
                        if frame["symbol"] in symbols and frame.get("bars"):
                            observed.add((day, frame["symbol"]))
    return {"status": "insufficient_for_matched_intraday_replay", "entry_symbol_days": len(wanted),
            "any_minute_frame_upper_bound": len(observed), "files": files,
            "matched_entry_days_with_frames": [list(x) for x in sorted(observed)],
            "reason": "Partial watchlist capture; complete adjacent minutes and comparable opening/next executable quote evidence not established."}


def run(store, start, end, asof, minute_dir=None):
    warmup = (date.fromisoformat(start) - timedelta(days=20)).isoformat()
    days = market_sessions(store.conn, warmup, asof)
    if not days or start > end or end > asof:
        raise ValueError("交易日期区间无效")
    signals, coverage = [], []
    opts = dict(execution.DEFAULTS, entry_wait=1)
    stamp = datetime.combine(date.fromisoformat(asof), time(23, 59), SHANGHAI)
    for idx, day in enumerate(days):
        if day < start:
            continue
        if day > end:
            break
        payload = opportunity_archive.report(store, day)
        archived = _is_dated_archive(payload, day)
        if not archived and payload["provenance"]["mode"] == "archived":
            payload = opportunity_archive._payload(store, day, "recomputed")
        origin = "dated_eod_archive" if archived else "reconstructed_from_current_database"
        main = payload["mainline"]
        market = payload["coverage"]["market"] or {}
        counts = Counter(mode["id"] for candidate in payload["candidates"] for mode in candidate["modes"])
        due = days[idx + 5] if idx + 5 < len(days) else None
        coverage.append({"date": day, "origin": origin, "target_date": due,
                         "market_ready": market.get("ready"), "market_missing": market.get("missing"),
                         "A_evaluable": bool(main["publication_ready"]),
                         "B_evaluable_at_least_one_line_day": bool(main["publication_ready"] or main["previous_publication_ready"]),
                         "B_both_line_days_evaluable": bool(main["publication_ready"] and main["previous_publication_ready"]),
                         "mainline": main, "counts": {mode: counts[mode] for mode in MODES}})
        print(f"{day} {origin} {dict(counts)}", file=sys.stderr, flush=True)
        for candidate in payload["candidates"]:
            for mode in candidate["modes"]:
                signals.append({"date": day, "secid": candidate["secid"], "symbol": candidate["symbol"],
                                "name": candidate["name"], "mode": mode["id"], "origin": origin,
                                "trigger": mode["trigger"], "risk": mode["risk"], "mode_basis": mode["basis"],
                                "history": candidate["history"], "signal_bar": candidate["bar"],
                                "mainlines": candidate["mainlines"], "macd": candidate["macd"],
                                "entry_session": days[idx + 1] if idx + 1 < len(days) else None,
                                "target_date": due, "generated_at": payload["generated_at"] if archived else None})

    prices = defaultdict(dict)
    first = {}
    for sid in sorted({s["secid"] for s in signals}):
        first[sid] = store.conn.execute("SELECT MIN(date) FROM bars WHERE secid=? AND close>0", (sid,)).fetchone()[0]
        for bar in store.conn.execute("SELECT * FROM bars WHERE secid=? AND date BETWEEN ? AND ?", (sid, start, asof)):
            prices[sid][bar["date"]] = dict(bar)
    for row in signals:
        order = dict(row, strategy=row["mode"], point_in_time=row["generated_at"] is None,
                     first_seen=first[row["secid"]], invalidation_price=row["risk"], confirmation_price=row["trigger"])
        row["orders"] = {arm: strategy_execution.simulate_candidate(
            order, days, prices[row["secid"]], opts=opts, entry_policy=policy, as_of=stamp
        ) for arm, policy in ARMS.items()}
        if row["target_date"]:
            row["structure"] = {arm: strategy_execution.simulate_candidate(
                order, days, prices[row["secid"]], opts=opts, entry_policy=policy, as_of=stamp,
                structure_sensitivity=True
            ) for arm, policy in ARMS.items()}

    mature = [r for r in signals if r["target_date"]]
    maturity_dates = [r["date"] for r in coverage if r["target_date"]]
    report = {"version": VERSION, "code_version": code_version(), "generated_at": datetime.now(timezone.utc).isoformat(),
              "start": start, "end": end, "asof": asof, "calendar": "cached_mairui_exchange_calendar",
              "policy": {"entry_O": "D+1 open only", "entry_R": "D+1 open and close above risk, close strictly above trigger, D+2 open at or above trigger",
                         "exit": "D+5 close, then next executable close; ordinary A-share T+1",
                         "costs": opts, "holdout": None, "scope": "research_daily_proxy",
                         "structure_sensitivity": "after entry close below frozen risk, sell next executable open; D+5 close deadline otherwise"},
              "coverage": coverage, "mature_signal_sessions": len(maturity_dates),
              "mature_signal_end": maturity_dates[-1] if maturity_dates else None,
              "all_candidates": _stats(signals), "mature_candidates": _stats(mature),
              "by_mode": {mode: _stats([r for r in mature if r["mode"] == mode]) for mode in MODES},
              "by_month": {month: _stats([r for r in mature if r["date"].startswith(month)])
                           for month in sorted({d[:7] for d in maturity_dates})},
              "by_day": {d: _stats([r for r in signals if r["date"] == d]) for d in [c["date"] for c in coverage]},
              "by_origin": {origin: {"all": _stats([r for r in signals if r["origin"] == origin]),
                                     "mature": _stats([r for r in mature if r["origin"] == origin])}
                            for origin in ("dated_eod_archive", "reconstructed_from_current_database")},
              "cash_mature": _cash(mature, prices, days, start),
              "cash_by_mode": {mode: _cash([r for r in mature if r["mode"] == mode], prices, days, start)
                               for mode in MODES},
              "structure_sensitivity_cash": _cash(mature, prices, days, start, key="structure"),
              "minute_coverage": _minute_audit(minute_dir, signals),
              "formation_source_counts": dict(Counter(",".join(r["history"].get("price_sources", ["archive_unrecorded"])) for r in mature)),
              **_timing_diagnostics(mature, maturity_dates, prices, days, start),
              "limitations": ["当前库证券资格、ST及历史复权回补可能带来生存者偏差及混合价格口径；重建信号并非历史实收。",
                              "未复权日线开盘和收盘仅供撮合代理；同日成交量及涨跌停核查不证明真实成交。",
                              "缺日线时延后退出尝试，若后续参考昨收仍无法核验，路径不可判定；缺口持仓估值和收益只能作条件性展示。",
                              "当前主线和昨日主线按历史成分版本判断；样本未到期或主线缺数时不把零笔当作策略表现。",
                              "日线代理不识别盘中站稳、回踩顺序或首个可成交报价；分钟归档范围见覆盖审计。"]}
    return report, signals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("start", "end", "asof"):
        parser.add_argument(f"--{flag}", required=True, type=date.fromisoformat)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--minute-dir", type=Path)
    args = parser.parse_args()
    if args.asof > datetime.now(SHANGHAI).date() or (
        args.asof == datetime.now(SHANGHAI).date() and datetime.now(SHANGHAI).time() < time(15)
    ):
        parser.error("--asof must be a completed Shanghai session")
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        report, orders = run(Snapshot(conn, args.db), str(args.start), str(args.end), str(args.asof), args.minute_dir)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, value in (("orders.json", orders), ("report.json", report)):
        content = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf8")
        opportunity_archive._atomic_write(args.out / name, content.decode("utf8") + "\n")
        print(name, hashlib.sha256(content + b"\n").hexdigest())


if __name__ == "__main__":
    main()
