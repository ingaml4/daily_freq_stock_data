"""Export server polls into versioned sampling evidence, keeping minutes separate."""

import argparse
from datetime import datetime, time, timedelta
import hashlib
import json
from pathlib import Path

from .entry_capture_backup import CN, atomic_json, interval


def convert(polls, day):
    date = datetime.strptime(day, "%Y-%m-%d").date()
    windows = [(datetime.combine(date, a, CN), datetime.combine(date, b, CN))
               for a, b in ((time(9, 30), time(11, 30)), (time(13), time(14, 57)))]
    previous, quotes, minutes = {}, [], []
    for raw in sorted(polls, key=lambda row: (row["received_at"], row["source"])):
        if raw.get("capture_schema") != 2 or raw["trade_date"] != day:
            raise ValueError("wrong backup schema or trade date")
        received, requested = (datetime.fromisoformat(raw[k]) for k in ("received_at", "requested_at"))
        if any(x.utcoffset() is None or x.date() != date for x in (received, requested)) or requested > received:
            raise ValueError("invalid backup receipt timestamps")
        source = raw["source"] + "/server"
        base = dict(schema_version=1, trade_date=day, source=source,
                    received_at=received.isoformat(), raw_ref=raw["raw_ref"])
        if raw["type"] == "minute_poll":
            for row in raw["minutes"]:
                minutes.append({**base, **row, "type": "minute"})
            continue
        for symbol in raw["requested_symbols"]:
            row = raw["quotes"].get(symbol)
            prior = previous.get((source, symbol))
            previous[source, symbol] = received
            window = next(((a, b) for a, b in windows if a <= received < b), None)
            if not window:
                continue
            start, end = window
            first = max(start, prior) if prior else start
            if first >= received:
                continue
            parsed = isinstance(row, dict)
            good = raw["quote_usability"].get(symbol) == "fresh_displayed_ask"
            fresh = False
            if parsed:
                source_at = datetime.fromisoformat(row["source_at"])
                fresh = (source_at.date() == date and start <= source_at <= received
                         and received - source_at <= timedelta(seconds=15))
            diagnostic = dict(sampling_coverage_version=2, symbol=symbol, scope="quote",
                              transport_ok=raw["transport_success"] is True, symbol_parsed=parsed,
                              quote_usable=good, current_fresh=fresh)
            late = prior is None and requested > start + timedelta(seconds=1)
            stalled = prior is not None and max(start, requested) - max(start, prior) > timedelta(seconds=max(15, interval(requested) + 5))
            if (late or stalled) and first < requested:
                quotes.append({**base, **diagnostic, "type": "coverage", "start": first.isoformat(),
                               "end": min(requested, end).isoformat(), "complete": False,
                               "reason": "observer_start_or_poll_gap"})
                first = max(first, requested)
            if first < received:
                quotes.append({**base, **diagnostic, "type": "coverage", "start": first.isoformat(),
                               "end": received.isoformat(), "complete": bool(raw["transport_success"] and parsed),
                               "reason": "scheduled_response_observed" if parsed else "missing_symbol_or_transport_failure"})
            if parsed:
                quotes.append({**base, **row, "symbol": symbol, "type": "quote"})
    return quotes, minutes


def write_lines(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join((json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode() for row in rows)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--quote-out", type=Path, required=True)
    parser.add_argument("--minute-out", type=Path, required=True)
    args = parser.parse_args()
    rows, inputs = [], {}
    for path in sorted(args.input.glob("polls-*.jsonl")):
        data = path.read_bytes()
        inputs[path.name] = hashlib.sha256(data).hexdigest()
        for line, text in enumerate(data.splitlines(), 1):
            row = json.loads(text)
            row["raw_ref"] = f"server-backup:{path.name}:{line}"
            rows.append(row)
    if not inputs:
        parser.error("no captured backup poll files")
    quotes, minutes = convert(rows, args.date)
    summary = {"trade_date": args.date, "sampling_coverage_version": 2, "input_sha256": inputs,
               "quote_events": len(quotes), "minute_events": len(minutes),
               "quote_sha256": write_lines(args.quote_out, quotes),
               "minute_sha256": write_lines(args.minute_out, minutes),
               "minute_policy": "separate_Tencent_source_do_not_silently_mix_with_Snowball"}
    atomic_json(args.quote_out.with_suffix(".manifest.json"), summary)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
