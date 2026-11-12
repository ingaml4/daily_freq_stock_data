"""Replay captured minute observations without requesting market data or sending alerts."""

import argparse
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median

SHANGHAI = timezone(timedelta(hours=8))
MINUTE = timedelta(minutes=1)
SESSIONS = ((time(9, 30), time(11, 30)), (time(13), time(14, 57)))


def _stamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("archive time must include a UTC offset")
    return stamp.astimezone(SHANGHAI)


def _range(value, day):
    start, end = (time.fromisoformat(part) for part in value.split("-", 1))
    if any(t.second or t.microsecond or t.tzinfo is not None for t in (start, end)):
        raise ValueError("use HH:MM-HH:MM")
    if start >= end:
        raise ValueError("range end must follow start")
    return tuple(datetime.combine(day, t, SHANGHAI) for t in (start, end))


def _labels(start, end):
    return [start + i * MINUTE for i in range((end - start) // MINUTE + 1)]


def _read_observations(lines, symbol, window):
    start, cutoff = window
    observations = []
    for line in lines:
        packet = json.loads(line)
        for frame in packet["frames"]:
            if frame["symbol"] != symbol:
                continue
            seen = _stamp(frame["seen_at"])
            if not start <= seen <= cutoff + MINUTE:
                continue
            bars, conflicts = {}, set()
            for bar in frame["bars"]:
                label = _stamp(bar["label"])
                end = _stamp(bar["end"])
                raw = bar["raw"]
                price = bar["price"]
                if (
                    not start <= label < cutoff
                    or label.second
                    or label.microsecond
                    or end != label + MINUTE
                    or end > seen
                    or raw.get("timestamp") != int(label.timestamp() * 1000)
                    or type(price) not in (int, float)
                    or not math.isfinite(price)
                    or price <= 0
                    or raw.get("current") != price
                ):
                    continue
                shares = bar["cum_shares"]
                if type(shares) is not int or shares < 0 or raw.get("volume_total") != shares:
                    shares = None
                point = (price, shares)
                if label in bars and bars[label] != point:
                    conflicts.add(label)
                bars[label] = point
            for label in conflicts:
                del bars[label]
            observations.append((seen, bars))
    return sorted(observations, key=lambda observation: observation[0])


def _volume(bars, end):
    points = [bars.get(label, (None, None))[1] for label in _labels(end - 6 * MINUTE, end)]
    # The last interval and its five preceding intervals need seven consecutive endpoints.
    if points[-2] is None or points[-1] is None or points[-1] < points[-2]:
        return None, None, None
    last = points[-1] - points[-2]
    if None in points:
        return last, None, None
    deltas = [b - a for a, b in zip(points, points[1:])]
    if min(deltas) < 0:
        return last, None, None
    baseline = median(deltas[:-1])
    return last, baseline, last / baseline if baseline > 0 else None


def replay(lines, *, symbol, day, window, spans):
    """Report the first usable observation and later revisions within the captured window."""
    start, cutoff = _range(window, day)
    if not any(a <= start.time() < cutoff.time() <= b for a, b in SESSIONS):
        raise ValueError("monitor window must be within one continuous session")
    ranges = [_range(span, day) for span in spans]
    if any(not start <= a < b < cutoff for a, b in ranges):
        raise ValueError("span labels must be inside the monitor window and before cutoff")
    observations = _read_observations(lines, symbol, (start, cutoff))
    results = []
    for a, b in ranges:
        labels = _labels(a, b)
        expected_end = b + MINUTE
        comparable = [
            (seen, bars)
            for seen, bars in observations
            if expected_end <= seen <= expected_end + MINUTE
            and all(label in bars for label in labels)
        ]
        result = {"from_label": a.isoformat(), "to_label": b.isoformat(), "status": "gap"}
        if comparable:
            seen, bars = comparable[0]
            last, baseline, multiple = _volume(bars, b)
            later = [bar_set[b] for stamp, bar_set in observations if stamp > seen and b in bar_set]
            result.update(
                status="observed",
                first_seen_at=seen.isoformat(),
                seconds_after_assumed_end=(seen - expected_end).total_seconds(),
                from_price=bars[a][0],
                to_price=bars[b][0],
                price_change_pct=(bars[b][0] / bars[a][0] - 1) * 100,
                last_minute_shares=last,
                prior_five_median_shares=baseline,
                last_minute_multiple=multiple,
                target_later_observations=len(later),
                target_changed_observations=sum(point != bars[b] for point in later),
            )
        results.append(result)
    return {
        "symbol": symbol,
        "date": day.isoformat(),
        "monitor_window": window,
        "observations": len(observations),
        "minute_end_policy": "archive_label_plus_60_seconds",
        "spans": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--window", required=True)
    parser.add_argument("--span", required=True, action="append")
    args = parser.parse_args(argv)
    try:
        with args.frames.open(encoding="utf-8-sig") as lines:
            report = replay(
                lines, symbol=args.symbol, day=args.date, window=args.window, spans=args.span
            )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(f"cannot replay archive ({type(exc).__name__})")
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
