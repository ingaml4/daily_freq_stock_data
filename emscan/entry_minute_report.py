"""Read an immutable opportunity archive, captured JSONL evidence and a SQLite snapshot."""

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import entry_minute_study
from .entry_study import MODES, SHANGHAI, _cash, _is_dated_archive, market_sessions
from .opportunity_archive import _atomic_write


def read_candidates(path, expected_sha256):
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256.lower():
        raise ValueError("机会归档 SHA256 不匹配")
    data = json.loads(raw)
    day = data["date"]
    if data.get("schema_version") != "1.0" or not _is_dated_archive(data, day):
        raise ValueError("需要同日收盘后保存的机会归档")
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("归档日期无效")
    rows = []
    keys = set()
    for candidate in data["candidates"]:
        for mode in candidate["modes"]:
            key = (day, candidate["secid"], mode["id"])
            if mode["id"] not in MODES or key in keys:
                raise ValueError("归档候选模式无效或键重复")
            keys.add(key)
            rows.append({"date": day, "secid": candidate["secid"], "symbol": candidate["symbol"],
                         "name": candidate["name"], "mode": mode["id"], "trigger": mode["trigger"],
                         "risk": mode["risk"], "generated_at": data["generated_at"],
                         "signal_bar": candidate.get("bar"),
                         "mainlines": candidate.get("mainlines", [])})
    return day, rows, {"generated_at": data["generated_at"], "code_version": data.get("code_version"),
                       "coverage": data.get("coverage"), "mainline": data.get("mainline")}


def attach_capture_config(path, day, archive_sha, candidates):
    raw = path.read_bytes()
    config = json.loads(raw)
    if config.get("source_sha256") != archive_sha or config.get("source_date") != day:
        raise ValueError("采集配置引用的机会归档不一致")
    rows = {(row["ticker"], row["group"]): row for row in config["candidates"]}
    if len(rows) != len(config["candidates"]) or set(rows) != {(r["symbol"], r["mode"]) for r in candidates}:
        raise ValueError("采集配置的候选键不完整或重复")
    for candidate in candidates:
        row = rows[candidate["symbol"], candidate["mode"]]
        if (row["trigger"], row["risk"]) != (candidate["trigger"], candidate["risk"]):
            raise ValueError("采集配置的触发或风险位与正式归档不一致")
        if row.get("previous_close") is not None and row["previous_close"] != (candidate["signal_bar"] or {}).get("close"):
            raise ValueError("采集配置昨收与正式归档不一致")
        candidate["sector_membership"] = {}
        for key in row.get("boards", []):
            board = config["boards"][key]
            members = set(board["members"])
            if candidate["symbol"] not in members or len(members) != len(board["members"]):
                raise ValueError("采集配置的板块成员缺少对应候选或存在重复")
            candidate["sector_membership"][key] = {
                "membership_version": board["membership_version"], "expected": len(members),
                "source": board.get("source"), "observed_at": board.get("observed_at"),
            }
    return config["trade_date"], hashlib.sha256(raw).hexdigest()


def replay_archive(conn, day, candidates, events, as_of, *, configured_entry_day=None):
    cutoff = as_of.astimezone(SHANGHAI)
    start = (date.fromisoformat(day) - timedelta(days=20)).isoformat()
    # Keep the real next trading date and D+5 even in a before-open rehearsal.
    calendar_end = max(cutoff.date(), date.fromisoformat(day) + timedelta(days=20)).isoformat()
    days = market_sessions(conn, start, calendar_end)
    if day not in days or days.index(day) + 5 >= len(days):
        raise ValueError("交易日历缺少信号日、入场日或计划退出日")
    day_index = days.index(day)
    entry_day, target = days[day_index + 1], days[day_index + 5]
    if configured_entry_day is not None and configured_entry_day != entry_day:
        raise ValueError("采集配置日期与交易日历 D+1 不一致")
    complete_through = cutoff.date() if cutoff.hour >= 15 else cutoff.date() - timedelta(days=1)
    prices = defaultdict(dict)
    first_seen = {}
    for secid in sorted({row["secid"] for row in candidates}):
        first_seen[secid] = conn.execute(
            "SELECT MIN(date) FROM bars WHERE secid=? AND close>0 AND date<=?", (secid, day)
        ).fetchone()[0]
        for bar in conn.execute("SELECT * FROM bars WHERE secid=? AND date BETWEEN ? AND ?",
                                (secid, day, complete_through.isoformat())):
            prices[secid][bar["date"]] = dict(bar)
    records = [entry_minute_study.replay_candidate(
        {**row, "first_seen": first_seen[row["secid"]]}, events, days, prices[row["secid"]], as_of=cutoff
    ) for row in candidates]
    mature = target <= complete_through.isoformat()
    report = {
        "version": entry_minute_study.VERSION, "signal_date": day, "entry_date": entry_day,
        "target_date": target, "as_of": cutoff.isoformat(), "candidate_count": len(records),
        "planned_exit_reached": mature,
        "outcomes": entry_minute_study.summarize(records),
        "by_mode": {mode: entry_minute_study.summarize([r for r in records if r["mode"] == mode]) for mode in MODES},
        "cash_mature": _cash(records, prices, [d for d in days if d <= complete_through.isoformat()], day,
                             priority="observed_entry_time")
        if mature and records else None,
    }
    return report, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--capture-config", type=Path)
    parser.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.as_of.utcoffset() is None or args.as_of > datetime.now(SHANGHAI):
        parser.error("--as-of requires a timezone and must not be in the future")
    day, candidates, archive_info = read_candidates(args.archive, args.archive_sha256)
    configured_day, config_digest = None, None
    if args.capture_config:
        configured_day, config_digest = attach_capture_config(
            args.capture_config, day, args.archive_sha256.lower(), candidates
        )
    events_digest = hashlib.sha256()

    def decoded_lines(stream):
        for index, line in enumerate(stream):
            events_digest.update(line)
            yield line.decode("utf-8-sig" if index == 0 else "utf-8")

    with args.events.open("rb") as stream:
        events = entry_minute_study.read_events(decoded_lines(stream))
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        report, orders = replay_archive(conn, day, candidates, events, args.as_of, configured_entry_day=configured_day)
    report["archive_sha256"] = args.archive_sha256.lower()
    report["archive"] = archive_info
    report["capture_config_sha256"] = config_digest
    report["events_sha256"] = events_digest.hexdigest()
    orders_bytes = (json.dumps(orders, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf8")
    report["orders_sha256"] = hashlib.sha256(orders_bytes).hexdigest()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, payload in (("orders.json", orders), ("report.json", report)):
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        _atomic_write(args.out / name, encoded)
        print(name, hashlib.sha256(encoded.encode("utf8")).hexdigest())


if __name__ == "__main__":
    main()
