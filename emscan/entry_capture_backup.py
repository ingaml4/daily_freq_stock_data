"""Independent two-provider public quote recorder, supervised by systemd.

Poll evidence and quote usability are separate. Raw observations are never
silently promoted to entry-minute-v1 complete coverage or minute bars.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, time as daytime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from .request_cache import daily_lock

CN = timezone(timedelta(hours=8))
SYMBOL = re.compile(r"^(SH(?:600|601|603|605)|SZ(?:000|001|002|003|300|301))\d{3}$")
PROVIDERS = ("tencent", "sina")
MAX_BYTES = 1024 * 1024


def now():
    return datetime.now(CN)


def atomic_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes((json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode())
    os.replace(temporary, path)


def number(value, *, integer=False):
    x = float(value)
    if not math.isfinite(x) or x < 0 or (integer and not x.is_integer()):
        raise ValueError("invalid quote number")
    return int(x) if integer else x


def parse_quotes(provider, raw):
    rows = {}
    pattern = r'v_((?:sh|sz)\d{6})="([^"\r\n]*)"' if provider == "tencent" else r'hq_str_((?:sh|sz)\d{6})="([^"\r\n]*)"'
    for match in re.finditer(pattern, raw, flags=re.I):
        symbol = match[1].upper()
        if not SYMBOL.fullmatch(symbol):
            continue
        fields = match[2].split("~" if provider == "tencent" else ",")
        try:
            if provider == "tencent":
                source_at = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=CN)
                last, previous, opening = (number(fields[i]) for i in (3, 4, 5))
                ask, ask_qty = number(fields[19]), number(fields[20], integer=True) * 100
                bid, bid_qty = number(fields[9]), number(fields[10], integer=True) * 100
                volume = number(fields[6], integer=True) * 100
                status = "provider_status_not_exposed"
            else:
                source_at = datetime.fromisoformat(fields[30] + "T" + fields[31]).replace(tzinfo=CN)
                last, previous, opening = (number(fields[i]) for i in (3, 2, 1))
                ask, ask_qty = number(fields[21]), number(fields[20], integer=True)
                bid, bid_qty = number(fields[11]), number(fields[10], integer=True)
                volume = number(fields[8], integer=True)
                status = fields[32]
            if previous <= 0:
                continue
            rows[symbol] = dict(symbol=symbol, source_at=source_at.isoformat(), price=last,
                                previous_close=previous, open_price=opening, volume_shares=volume,
                                ask_price=ask, ask_shares=ask_qty, bid_price=bid, bid_shares=bid_qty,
                                provider_status=status, quantity_verified=True,
                                quantity_basis="displayed_lots_times_100" if provider == "tencent" else "displayed_shares")
        except (ValueError, IndexError, OverflowError):
            continue
    return rows


def usability(row, received):
    if not row:
        return "missing_or_invalid_symbol"
    source = datetime.fromisoformat(row["source_at"])
    if source.date() != received.date():
        return "wrong_trade_date"
    if not 0 <= (received - source).total_seconds() <= 15:
        return "stale_or_future_quote"
    t = source.time()
    if not (daytime(9, 30) <= t < daytime(11, 30) or daytime(13) <= t < daytime(14, 57)):
        return "outside_continuous_session"
    if row["ask_price"] <= 0 or row["ask_shares"] <= 0:
        return "no_displayed_ask"
    if row["bid_price"] > row["ask_price"]:
        return "crossed_book"
    return "fresh_displayed_ask"


def request(provider, symbols, budget=2.5):
    code = ",".join(s.lower() for s in symbols)
    url = ("https://qt.gtimg.cn/q=" if provider == "tencent" else "https://hq.sinajs.cn/list=") + code
    referer = "https://gu.qq.com/" if provider == "tencent" else "https://stock.finance.sina.com.cn/"
    if provider == "tencent-minute":
        url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=" + symbols[0].lower()
        referer = "https://gu.qq.com/"
    try:
        reply = subprocess.run([sys.executable, "-m", "emscan.entry_capture_backup", "--http-worker"],
                               input=json.dumps([url, referer, budget]).encode(), capture_output=True,
                               timeout=budget, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("provider_wall_deadline") from exc
    status, separator, body = reply.stdout.partition(b"\n")
    if reply.returncode or not separator or status != b"200":
        raise RuntimeError("provider_HTTP_" + status.decode("ascii", errors="replace")[:15])
    return body.decode("gb18030", errors="replace")


def http_worker():
    url, referer, timeout = json.load(sys.stdin)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
        payload = b"200\n" + body if len(body) <= MAX_BYTES else b"0\nresponse_too_large"
    except urllib.error.HTTPError as exc:
        payload = str(exc.code).encode() + b"\n"
    except OSError:
        payload = b"0\nnetwork_error"
    sys.stdout.buffer.write(payload)


def sample(provider, symbols, *, fetch=request, clock=now):
    started = clock()
    raw, error, rows = None, None, {}
    try:
        raw = fetch(provider, symbols)
        rows = parse_quotes(provider, raw)
    except (OSError, ValueError, RuntimeError) as exc:
        error = type(exc).__name__ + ":" + str(exc)[:80]
    received = clock()
    return {"capture_schema": 2, "type": "poll", "observer": "personal-site-server",
            "source": provider, "trade_date": received.date().isoformat(),
            "requested_at": started.isoformat(), "received_at": received.isoformat(),
            "transport_success": raw is not None, "error": error,
            "requested_symbols": symbols, "quotes": rows,
            "quote_usability": {s: usability(rows.get(s), received) for s in symbols},
            "raw_response": raw, "response_text_sha256": hashlib.sha256(raw.encode()).hexdigest() if raw is not None else None}


def minute_sample(symbol, *, fetch=request, clock=now):
    started, raw, error, minutes = clock(), None, None, []
    try:
        raw = fetch("tencent-minute", [symbol])
        received = clock()
        chart = json.loads(raw)["data"][symbol.lower()]["data"]
        if chart["date"] != received.strftime("%Y%m%d"):
            raise ValueError("minute_date_mismatch")
        for line in chart["data"]:
            fields = line.split()
            if not re.fullmatch(r"\d{4}", fields[0]):
                continue
            end = datetime.combine(received.date(), datetime.strptime(fields[0], "%H%M").time(), CN)
            label = end - timedelta(minutes=1)
            if not end <= received <= end + timedelta(minutes=1):
                continue
            if not (daytime(9, 30) <= label.time() < end.time() <= daytime(11, 30)
                    or daytime(13) <= label.time() < end.time() <= daytime(14, 57)):
                continue
            close = number(fields[1])
            if close <= 0:
                continue
            minutes.append(dict(symbol=symbol, label=label.isoformat(), end=end.isoformat(),
                                close=close, low=None, complete=True, received_at=received.isoformat(),
                                timestamp_basis="Tencent_end_label_minus_one_minute",
                                risk_path_basis="close_only_no_intraminute_low", raw_line=line))
    except (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError) as exc:
        error = type(exc).__name__ + ":" + str(exc)[:80]
    received = clock()
    return {"capture_schema": 2, "type": "minute_poll", "observer": "personal-site-server",
            "source": "tencent-minute", "trade_date": received.date().isoformat(),
            "requested_at": started.isoformat(), "received_at": received.isoformat(),
            "transport_success": raw is not None, "error": error, "requested_symbols": [symbol],
            "minutes": minutes if error is None else [], "quotes": {}, "quote_usability": {},
            "raw_response": raw, "response_text_sha256": hashlib.sha256(raw.encode()).hexdigest() if raw is not None else None}


def interval(at):
    t = at.time()
    if daytime(9, 24, 45) <= t < daytime(9, 33):
        return 2.0
    return 5.0


def trading_day(db, day):
    with sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", ("mairui_calendar:" + day[:4],)).fetchone()
    days = json.loads(row[0])["days"] if row else []
    if len(days) < 220 or len(set(days)) != len(days):
        raise ValueError("verified_annual_calendar_missing")
    return day in days


def load_plan(path, day):
    plan = json.loads(path.read_bytes())
    if plan.get("trade_date") != day:
        raise ValueError("capture_plan_date_mismatch")
    symbols = plan.get("symbols")
    if (not isinstance(symbols, list) or not symbols or len(symbols) > 100
            or len(set(symbols)) != len(symbols)
            or any(not isinstance(s, str) or not SYMBOL.fullmatch(s) for s in symbols)):
        raise ValueError("invalid_capture_symbols")
    providers = plan.get("providers", list(PROVIDERS))
    if (not isinstance(providers, list) or not providers or len(set(providers)) != len(providers)
            or any(p not in PROVIDERS for p in providers)):
        raise ValueError("invalid_capture_providers")
    return plan


def run(plan_path, output, db, *, probe=False, clock=now):
    day = clock().date().isoformat()
    if json.loads(plan_path.read_bytes()).get("trade_date") != day:
        return "different_trade_date"
    plan = load_plan(plan_path, day)
    if not trading_day(db, day):
        return "market_closed"
    symbols = plan["symbols"]
    providers = plan.get("providers", list(PROVIDERS))
    output.mkdir(parents=True, exist_ok=True)
    with daily_lock(output / "capture") as locked:
        if not locked:
            return "already_running"
        frozen = output / "frozen-plan.json"
        plan_bytes = plan_path.read_bytes()
        if frozen.exists() and frozen.read_bytes() != plan_bytes:
            raise ValueError("capture plan changed after first start")
        if not frozen.exists():
            temporary = output / ".plan.tmp"
            temporary.write_bytes(plan_bytes)
            os.replace(temporary, frozen)
        messages, stop = queue.Queue(maxsize=200), threading.Event()
        last = {}
        unreadable_lines = 0
        for prior_path in sorted(output.glob("polls-*.jsonl")):
            for line in prior_path.read_bytes().splitlines():
                try:
                    row = json.loads(line)
                    if not isinstance(row.get("source"), str) or not isinstance(row.get("received_at"), str):
                        raise ValueError("invalid prior receipt")
                    datetime.fromisoformat(row["received_at"])
                except (ValueError, UnicodeError, AttributeError):
                    unreadable_lines += 1
                    continue
                last[row["source"]] = row["received_at"]
        started = clock()
        path = output / ("polls-" + started.strftime("%H%M%S-%f") + ".jsonl")
        state = dict(state="probing" if probe else "running", trade_date=day, pid=os.getpid(),
                     started_at=started.isoformat(), plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                     symbols=symbols, providers={}, previous_receipts=last, probe_only=probe,
                     output_file=path.name, prior_unreadable_lines=unreadable_lines)
        atomic_json(output / "state.json", state)
        begin = datetime.combine(started.date(), daytime(9, 14), CN)
        finish = datetime.combine(started.date(), daytime(15, 1), CN)

        def worker(provider):
            previous = last.get(provider)
            while not stop.is_set() and (probe or clock() < finish):
                current = clock()
                if not probe and (current < begin or daytime(11, 31) <= current.time() < daytime(12, 59)):
                    stop.wait(1)
                    continue
                tick = time.monotonic()
                row = sample(provider, symbols, clock=clock)
                row["previous_received_at"] = previous
                row["observer_gap_seconds"] = ((datetime.fromisoformat(row["requested_at"]) - datetime.fromisoformat(previous)).total_seconds()
                                               if previous else max(0, (current - begin).total_seconds()))
                previous = row["received_at"]
                messages.put(row)
                if probe:
                    break
                delay = 30 if row["error"] and ("429" in row["error"] or "403" in row["error"]) else interval(current)
                stop.wait(max(0, delay - (time.monotonic() - tick)))

        def minute_worker():
            last_label = None
            with ThreadPoolExecutor(max_workers=4) as pool:
                while not stop.is_set() and (probe or clock() < finish):
                    current = clock()
                    label = current.replace(second=0, microsecond=0)
                    eligible = (daytime(9, 31) <= label.time() <= daytime(11, 30)
                                or daytime(13, 1) <= label.time() <= daytime(14, 57))
                    if not probe and (not eligible or current.second < 3 or label == last_label):
                        stop.wait(.5)
                        continue
                    last_label = label
                    todo = set(symbols)
                    for attempt in range(3):
                        pending = {pool.submit(minute_sample, symbol, clock=clock): symbol for symbol in sorted(todo)}
                        for future in as_completed(pending):
                            row = future.result()
                            row.update(previous_received_at=None, observer_gap_seconds=None)
                            messages.put(row)
                            if any(m["end"] == label.isoformat() for m in row["minutes"]) or (
                                row["error"] and ("429" in row["error"] or "403" in row["error"])
                            ):
                                todo.discard(pending[future])
                        if probe or not todo or stop.is_set() or attempt == 2:
                            break
                        if clock() >= label + timedelta(seconds=45):
                            break
                        stop.wait(7)
                    if probe:
                        break

        workers = [threading.Thread(target=worker, args=(p,), daemon=True) for p in providers]
        workers.append(threading.Thread(target=minute_worker, daemon=True))
        for thread in workers:
            thread.start()
        try:
            with path.open("ab", buffering=0) as file:
                while any(t.is_alive() for t in workers) or not messages.empty():
                    if not probe and clock() < finish and any(not t.is_alive() for t in workers):
                        raise RuntimeError("provider_worker_stopped_early")
                    try:
                        row = messages.get(timeout=1)
                    except queue.Empty:
                        state["checked_at"] = clock().isoformat()
                        atomic_json(output / "state.json", state)
                        continue
                    file.write((json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode())
                    os.fsync(file.fileno())
                    state["providers"][row["source"]] = {
                        "received_at": row["received_at"], "error": row["error"],
                        "transport_success": row["transport_success"], "parsed": len(row["quotes"]),
                        "minutes": len(row.get("minutes", [])),
                        "usability": dict(Counter(row["quote_usability"].values())),
                        "observer_gap_seconds": row["observer_gap_seconds"],
                    }
                    state["checked_at"] = clock().isoformat()
                    atomic_json(output / "state.json", state)
        except BaseException as exc:
            state.update(state="failed", error=type(exc).__name__, checked_at=clock().isoformat())
            atomic_json(output / "state.json", state)
            raise
        finally:
            stop.set()
            for thread in workers:
                thread.join(timeout=4)
        state.update(state="probe_complete" if probe else "complete", checked_at=clock().isoformat())
        atomic_json(output / "state.json", state)
    return state["state"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-worker", action="store_true")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--calendar-db", type=Path)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if args.http_worker:
        return http_worker()
    if not all((args.plan, args.output, args.calendar_db)):
        parser.error("plan, output and calendar-db required")
    print(run(args.plan, args.output, args.calendar_db, probe=args.probe))


if __name__ == "__main__":
    main()
