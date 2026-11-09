"""按请求日检查盘后数据。所有阶段共用同一个 SQLite 读快照。"""
from __future__ import annotations

import json
import subprocess
from datetime import date
from functools import lru_cache
from pathlib import Path

from .backtest import TIER_LINE_RE, build_symbol_index, parse_tiers, tier_limits
from .benchmarks import INDEX_CODES
from .tzutil import now

PERIODS = (5, 10, 20, 60)
VERSION = "2026-11-09.1"


@lru_cache(maxsize=1)
def code_version():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=2, check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unrecorded"


def stage(actual_date, expected, available, **details):
    missing = None if expected is None else max(0, expected - available)
    return dict(actual_date=actual_date, expected=expected, available=available,
                missing=missing, ready=(actual_date is not None and missing == 0), **details)


def _bars(store, day):
    conn = store.conn
    actual = conn.execute("SELECT MAX(date) FROM bars WHERE date<=?", (day,)).fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM bars WHERE date=? AND close>0 AND volume>0",
                     (day,)).fetchone()[0]
    row = conn.execute(
        "SELECT params,finished_at FROM runs WHERE trade_date=? AND mode='spot_coverage' "
        "ORDER BY run_id DESC LIMIT 1", (day,)
    ).fetchone()
    info = json.loads(row[0]) if row else {}
    observed = info.get("active")
    expected = info.get("universe")
    closed = info.get("no_trade", 0)
    usable = min(n, observed) if observed is not None else n
    result = stage(actual, expected, usable + closed, active_bars=n, no_trade=closed,
                   coverage_source=info.get("source"), observed_at=row[1] if row else None,
                   source_version=info.get("code_version"), reason=None)
    result["ready"] = bool(actual == day and n and result["ready"] and n >= (observed or 0))
    if not result["ready"]:
        result["reason"] = ("awaiting_market_bars" if not n else "unverified_snapshot_coverage"
                            if not row else "missing_or_invalid_quotes")
    return result


def _indicators(conn, day):
    # 每只股票仅取截至请求日最近 60 根有效收盘，避免老股的超长序列影响轮询。
    query = """
        SELECT b.secid,
          (SELECT COUNT(*) FROM (SELECT 1 FROM bars h WHERE h.secid=b.secid
           AND h.date<=b.date AND h.close>0 AND h.volume>0 ORDER BY h.date DESC LIMIT 60)) AS n,
          i.macd_diff,i.macd_dea,i.kdj_k,i.kdj_d,i.boll_mid,i.boll_lower,
          d.s_macd_golden,d.s_kdj_golden,d.s_boll_break_lower,
          i.ma5,i.ma10,i.ma20,i.ma60,d.ma5 AS c5,d.ma10 AS c10,d.ma20 AS c20,d.ma60 AS c60
        FROM bars b LEFT JOIN indicators i ON i.secid=b.secid AND i.date=b.date
        LEFT JOIN daily_signals d ON d.secid=b.secid AND d.date=b.date
        WHERE b.date=? AND b.volume>0 AND b.close>0
    """
    fields = {f"ma{p}": {"eligible": 0, "indicator_filled": 0, "cache_filled": 0,
                         "matched": 0, "insufficient_history": 0} for p in PERIODS}
    total = good = technical_missing = 0
    for row in conn.execute(query, (day,)):
        total += 1
        valid = all(row[k] is not None for k in
                    ("macd_diff","macd_dea","kdj_k","kdj_d","boll_mid","boll_lower",
                     "s_macd_golden","s_kdj_golden","s_boll_break_lower"))
        technical_missing += int(not valid)
        for p in PERIODS:
            f = fields[f"ma{p}"]
            if row["n"] < p:
                f["insufficient_history"] += 1
                continue
            f["eligible"] += 1
            i, c = row[f"ma{p}"], row[f"c{p}"]
            f["indicator_filled"] += int(i is not None)
            f["cache_filled"] += int(c is not None)
            match = i is not None and c is not None and abs(i-c) <= 1e-8 * max(1.0, abs(i))
            f["matched"] += int(match)
            valid = valid and match
        good += int(valid)
    actual = conn.execute("SELECT MAX(date) FROM indicators WHERE date<=?", (day,)).fetchone()[0]
    result = stage(actual, total, good, moving_averages=fields,
                   missing_technical_or_signal_cache=technical_missing)
    result["ready"] = total > 0 and actual == day and good == total
    result["reason"] = None if result["ready"] else (
        "no_active_bars" if not total else "missing_indicators_or_cache"
    )
    return result


def _benchmarks(conn, day):
    by_code = {r[0]: r[1] for r in conn.execute(
        "SELECT code, MAX(date) FROM benchmark_bars WHERE date<=? GROUP BY code", (day,)
    )}
    dates = {dm[:6]: by_code.get(dm[:6]) for dm in INDEX_CODES}
    missing = [k for k, d in dates.items() if d != day]
    return stage(max((d for d in dates.values() if d), default=None), len(dates),
                 len(dates) - len(missing), dates=dates, missing_codes=missing,
                 reason="missing_same_day_benchmark" if missing else None)


def _tiers(store, cfg, day):
    record = store.get_summary(day) or {}
    conn = store.conn
    requested = set((r[0], r[1]) for r in conn.execute(
        "SELECT tier,secid FROM tier_picks WHERE date=? AND source='summary'", (day,)
    ))
    actual = conn.execute("SELECT MAX(date) FROM tier_picks WHERE date<=? AND source='summary'",
                          (day,)).fetchone()[0]
    content = record.get("content") or ""
    engine = record.get("engine") or ""
    explicit = [m for line in content.splitlines() if (m := TIER_LINE_RE.match(line))]
    parsed = parse_tiers(content, build_symbol_index(store), limits=tier_limits(cfg)) if content else {}
    picks = {(k, r["secid"]) for k, vals in parsed.get("picks", {}).items() for r in vals}
    # 空名单也可完整：须两档都明确写「空」，普通正文缺榜仍属未完成。
    empty_labels = {
        int(m.group(1)) if m.group(1) else {"一": 1, "二": 2}[m.group(2)]
        for m in explicit if m.string[m.end():].strip().strip("（）()。 ") in ("空", "无", "暂无")
    }
    expected = len(picks) + len(parsed.get("unmatched", ()))
    empty = empty_labels == {1, 2}
    covered_tiers = {k for k, _ in picks} | empty_labels
    result = stage(day if empty else actual, expected if content and (picks or explicit) else None,
                   len(requested & picks), generated_at=record.get("created_at"),
                   summary_date=day if content else None, engine=engine or None,
                   unexpected=len(requested - picks), explicitly_empty=empty)
    result["ready"] = bool(engine and not engine.startswith("rule") and covered_tiers == {1, 2}
                           and result["missing"] == 0 and requested == picks)
    result["reason"] = None if result["ready"] else (
        "summary_not_published" if not content else "rule_fallback" if engine.startswith("rule")
        else "tier_list_missing_or_incomplete"
    )
    return result


def report(store, cfg, day):
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("date must be YYYY-MM-DD")
    clock = now()
    conn = store.conn
    conn.execute("SAVEPOINT readiness_read")
    try:
        stages = {"market": _bars(store, day), "indicators": _indicators(conn, day),
                  "benchmarks": _benchmarks(conn, day), "tiers": _tiers(store, cfg, day)}
        from .tier_reasons import readiness as reason_coverage

        evidence = reason_coverage(conn, day)
        start = conn.execute(
            "SELECT run_id,params,started_at FROM runs WHERE trade_date=? AND mode='run_started' "
            "ORDER BY run_id DESC LIMIT 1", (day,)
        ).fetchone()
        run = {"phase": "legacy_or_not_started", "timestamp": None, "generation": None,
               "source_version": None, "exit_code": None}
        if start:
            run.update(phase="run_started", generation=start["run_id"], timestamp=start["started_at"],
                       source_version=json.loads(start["params"]).get("code_version"))
            for end in conn.execute(
                "SELECT finished_at,params FROM runs WHERE run_id>? AND trade_date=? "
                "AND mode='run_finished' ORDER BY run_id DESC", (start["run_id"], day)
            ):
                params = json.loads(end["params"])
                if params.get("generation") == start["run_id"]:
                    run.update(phase="run_finished", timestamp=end["finished_at"],
                               exit_code=params.get("exit_code"))
                    break
        complete = all(v["ready"] for v in stages.values())
        evidence_ready = stages["tiers"]["ready"] and evidence["complete"] == evidence["expected"]
        ready = complete and evidence_ready and run["phase"] != "run_started" and run["exit_code"] in (None, 0)
        today = clock.date().isoformat()
        in_window = day == today and clock.strftime("%H:%M") < "18:00"
        waiting = day > today or (in_window and run["phase"] != "run_finished" and
                                  stages["tiers"]["reason"] != "rule_fallback")
        state = "waiting" if waiting and (not ready or run["phase"] == "run_started") else (
            "ready" if ready else "degraded")
        return {"date": day, "checked_at": clock.isoformat(), "state": state,
                "ready": ready and state == "ready", "data_ready": complete,
                "stages": stages, "tier_reasons": evidence, "run": run,
                "entry_evidence_complete": evidence_ready,
                "readiness_version": VERSION, "reader_code_version": code_version(),
                "cache_version": store.data_version(), "schema_version": conn.execute("PRAGMA user_version").fetchone()[0]}
    finally:
        conn.execute("RELEASE readiness_read")
