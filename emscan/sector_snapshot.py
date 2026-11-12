"""One-shot Tencent sector collector and artifact-only reader."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import tempfile
from datetime import datetime, time
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .request_cache import daily_lock

TZ = ZoneInfo("Asia/Shanghai")
SYMBOL = re.compile(r"^(SH|SZ|BJ)[0-9]{6}$")


def _now():
    return datetime.now(TZ)


def _session(now):
    if now.weekday() >= 5:
        return "weekend_closed"
    t = now.time().replace(tzinfo=None)
    if t < time(9, 15): return "preopen"
    if t < time(9, 30): return "auction"
    if t <= time(11, 30): return "morning"
    if t < time(13): return "lunch_break"
    if t <= time(15): return "afternoon"
    return "closed"


def _read_membership(membership):
    if isinstance(membership, (str, Path)):
        with open(membership, encoding="utf-8-sig") as f:
            membership = json.load(f)
    if isinstance(membership, list):
        membership = {"sectors": {"explicit": {"members": membership}}}
    if not isinstance(membership, dict) or not isinstance(membership.get("sectors"), dict):
        raise ValueError("membership requires sectors mapping or explicit member list")
    sectors = {}
    for name, spec in membership["sectors"].items():
        if isinstance(spec, list): spec = {"members": spec}
        members = spec.get("members")
        if not isinstance(members, list): raise ValueError("sector members must be a list")
        normalized, seen = [], set()
        for item in members:
            item = {"symbol": item} if isinstance(item, str) else dict(item)
            symbol = str(item.get("symbol", "")).upper()
            if not SYMBOL.fullmatch(symbol): raise ValueError(f"invalid symbol: {symbol!r}")
            if symbol in seen: raise ValueError(f"duplicate symbol in sector {name}: {symbol}")
            seen.add(symbol)
            normalized.append({**item, "symbol": symbol})
        expected = spec.get("expected_count")
        if expected is not None and (type(expected) is not int or expected < len(normalized)):
            raise ValueError("expected_count must be an integer >= listed member count")
        sectors[name] = {"members": normalized, "source": spec.get("source", membership.get("source")),
                         "observed_at": spec.get("observed_at", membership.get("observed_at")),
                         "constituents_date": spec.get("constituents_date"),
                         "verified": spec.get("verified", False) is True, "expected_count": expected}
    return sectors


def _download(symbols):
    url = "https://qt.gtimg.cn/q=" + ",".join(s.lower() for s in symbols)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    with urlopen(req, timeout=12) as response:
        return response.read().decode("gb18030", errors="replace")


def _parse(raw):
    result = {}
    for match in re.finditer(r'v_(sh|sz|bj)([0-9]{6})="([^"\r\n]*)"', raw, re.I):
        symbol = (match.group(1) + match.group(2)).upper()
        fields = match.group(3).split("~")
        if len(fields) < 31 or not fields[3]: continue
        try:
            source_at = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=TZ).isoformat()
        except ValueError:
            source_at = None
        def number(i):
            try:
                value = float(fields[i])
                return value if math.isfinite(value) else None
            except (ValueError, IndexError): return None
        price = number(3)
        if price is None or price <= 0: continue
        lots = number(36)
        if lots is not None and lots < 0: lots = None
        try:
            amount = float(fields[35].split("/")[2])
            if not math.isfinite(amount) or amount < 0: amount = None
        except (ValueError, IndexError): amount = None
        currency = "USD" if symbol.startswith("SH900") else "HKD" if symbol.startswith("SZ200") else "CNY"
        result[symbol] = {"name": fields[1], "price": number(3), "price_currency": currency,
                          "previous_close": number(4),
                          "open": number(5), "volume_lots": lots,
                          "volume_shares": lots * 100 if lots is not None else None,
                          "amount_yuan": amount if currency == "CNY" else None,
                          "amount_reported": amount, "amount_currency_verified": currency == "CNY",
                          "change_pct": number(32),
                          "source_at": source_at, "raw_fields": fields}
    return result


def _quality(row, now, max_age_seconds):
    if row.get("excluded_no_permission"): return "excluded_no_permission"
    if row.get("quote") is None: return "missing"
    price = row["quote"].get("price")
    if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0: return "invalid_price"
    stamp = row["quote"].get("source_at")
    if not stamp: return "source_time_missing"
    try: age = (now - datetime.fromisoformat(stamp)).total_seconds()
    except (ValueError, TypeError): return "source_time_invalid"
    if age < -5: return "source_time_future"
    if age > max_age_seconds: return "stale"
    return "fresh"


def _comparison(previous, current):
    """Compare only consecutive source observations in one trading session."""
    if not previous or not current: return None
    try:
        a = datetime.fromisoformat(previous["source_at"])
        b = datetime.fromisoformat(current["source_at"])
        seconds = (b - a).total_seconds()
        if a.date() != b.date() or seconds <= 0 or _session(a) != _session(b): return None
        if _session(a) not in ("auction", "morning", "afternoon"): return None
        old_price, price = previous["price"], current["price"]
        if any(not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in (old_price, price)): return None
    except (ValueError, TypeError, KeyError): return None
    def increment(key):
        old, new = previous.get(key), current.get(key)
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (old, new)): return None
        return new - old if new >= old else None
    return {"window_seconds": seconds, "price_change_pct": (price / old_price - 1) * 100,
            "volume_increment_shares": increment("volume_shares"),
            "amount_increment_yuan": increment("amount_yuan")}


def _summarize(sector):
    rows = sector["members"]
    quality = [r["freshness"] for r in rows]
    sector["coverage"] = {"listed": len(rows), "returned": sum(r.get("quote") is not None for r in rows),
                          "monitored": sum(not r.get("excluded_no_permission") for r in rows),
                          **{state: quality.count(state) for state in
                             ("fresh", "missing", "stale", "invalid_price", "source_time_missing", "source_time_invalid", "source_time_future", "excluded_no_permission")}}
    changes = [r["quote"].get("change_pct") for r in rows if r["freshness"] == "fresh"]
    changes = [x for x in changes if isinstance(x, (int, float)) and math.isfinite(x)]
    sector["breadth"] = {"basis": "fresh_quotes_with_change_pct", "count": len(changes),
                         "up": sum(x > 0 for x in changes), "down": sum(x < 0 for x in changes),
                         "unchanged": sum(x == 0 for x in changes),
                         "change_median_pct": statistics.median(changes) if changes else None}


def collect(membership, output, *, fetch=None, now=None, max_age_seconds=90, calendar_verified=False):
    """Collect each unique member once in batches <=100, then atomically publish."""
    if max_age_seconds <= 0: raise ValueError("max_age_seconds must be positive")
    sectors = _read_membership(membership)
    target = Path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with daily_lock(target) as acquired:
        if not acquired:
            raise RuntimeError(f"sector snapshot collector already running for {target}")
        previous = {}
        try:
            with target.open(encoding="utf-8") as f: old = json.load(f)
            for sector in old["sectors"].values():
                for row in sector["members"]: previous[row["symbol"]] = row.get("quote")
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            pass
        symbols = list(dict.fromkeys(m["symbol"] for s in sectors.values() for m in s["members"]
                                    if not m["symbol"].startswith("SH688")))
        quotes, batch_records = {}, []
        fetch = fetch or _download
        for start in range(0, len(symbols), 100):
            batch = symbols[start:start+100]
            try:
                raw = fetch(batch)
                received = (now or _now()).isoformat()
                parsed = _parse(raw)
                for symbol in batch:
                    quotes[symbol] = {"quote": parsed.get(symbol), "received_at": received,
                                      "error": None if symbol in parsed else "no_usable_quote"}
                batch_records.append({"symbols": batch, "received_at": received, "error": None})
            except Exception as exc:
                received = (now or _now()).isoformat()
                for symbol in batch: quotes[symbol] = {"quote": None, "received_at": received, "error": str(exc)}
                batch_records.append({"symbols": batch, "received_at": received, "error": str(exc)})
        generated = now or _now()
        result = {"schema_version": 1, "provider": "Tencent qt.gtimg.cn", "generated_at": generated.isoformat(),
                  "max_age_seconds": max_age_seconds, "session": _session(generated),
                  "calendar_verified": calendar_verified, "batches": batch_records, "sectors": {}}
        for name, spec in sectors.items():
            rows = []
            for member in spec["members"]:
                symbol = member["symbol"]
                excluded = symbol.startswith("SH688")
                row = {**member, **quotes.get(symbol, {"quote": None, "received_at": None, "error": "excluded_no_permission"}),
                       "excluded_no_permission": excluded}
                row["freshness"] = _quality(row, generated, max_age_seconds)
                row["comparison"] = _comparison(previous.get(symbol), row["quote"])
                row["action_eligible"] = False if symbol.startswith("SH688") else None
                row["permission_status"] = "excluded_no_permission" if excluded else "unknown"
                rows.append(row)
            count = spec["expected_count"]
            result["sectors"][name] = {k: spec[k] for k in ("source", "observed_at", "constituents_date", "verified", "expected_count")}
            result["sectors"][name].update({"listed_count": len(rows), "unidentified_gap_count": None if count is None else count-len(rows), "members": rows})
            _summarize(result["sectors"][name])
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp") as f:
            temp = f.name
            json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        try: os.replace(temp, target)
        finally:
            if os.path.exists(temp): os.unlink(temp)
        return result
def build_sector_snapshot_payload(path, *, now=None):
    """Read artifact only and recompute freshness; never call upstream."""
    with open(path, encoding="utf-8") as f: result = json.load(f)
    now = now or _now()
    result["read_at"] = now.isoformat()
    result["session"] = _session(now)
    for sector in result["sectors"].values():
        for row in sector["members"]:
            row["freshness"] = _quality(row, now, result["max_age_seconds"])
        _summarize(sector)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--membership", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-age-seconds", type=int, default=90)
    parser.add_argument("--market-hours", action="store_true")
    parser.add_argument("--calendar-db")
    args = parser.parse_args()
    current = _now()
    calendar_verified = False
    if args.market_hours:
        if _session(current) not in ("morning", "afternoon"):
            print(json.dumps({"skipped": "outside_continuous_session"}))
            return 0
        if not args.calendar_db:
            parser.error("--market-hours requires --calendar-db")
        import sqlite3
        with sqlite3.connect(Path(args.calendar_db).resolve().as_uri() + "?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (f"mairui_calendar:{current.year}",)).fetchone()
        days = json.loads(row[0]).get("days", []) if row else []
        if current.date().isoformat() not in days:
            print(json.dumps({"skipped": "not_in_cached_exchange_calendar"}))
            return 0
        calendar_verified = True
    result = collect(args.membership, args.output, max_age_seconds=args.max_age_seconds,
                     calendar_verified=calendar_verified)
    print(json.dumps({"generated_at": result["generated_at"], "batches": len(result["batches"]),
                      "errors": sum(bool(b["error"]) for b in result["batches"])}, ensure_ascii=False))
    return 1 if any(b["error"] for b in result["batches"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
