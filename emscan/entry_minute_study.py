"""Offline D+1 opening-book and minute-confirmed entry replay."""

import json
from collections import Counter, defaultdict
from datetime import date, datetime, time
from itertools import groupby
from math import isfinite

from . import execution, strategy_execution
from .entry_study import _stats
from .minute_replay import MINUTE, SESSIONS, SHANGHAI

VERSION = "abc-entry-minute-v1"
OPEN_BOOK = "opening_book_proxy"
CONFIRMED_BOOK = "confirmed_book_proxy"
AUCTION = "opening_auction_proxy"
PRICE_BASIS_CONFLICTS = {"conflict", "mismatch", "inconsistent", "adjusted", "qfq"}


def _stamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("timestamps must include a UTC offset")
    return value.astimezone(SHANGHAI)


def _price(value):
    return type(value) in (int, float) and isfinite(value) and value > 0


def _count(value):
    return type(value) is int and value >= 0


def _evidence(row):
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


def read_events(lines):
    """Decode versioned JSONL without changing evidence order or timestamps."""
    result = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"invalid evidence JSON on line {number}") from exc
        if not isinstance(row, dict) or type(row.get("schema_version")) is not int:
            raise ValueError(f"invalid evidence envelope on line {number}")
        if row["schema_version"] != 1:
            raise ValueError(f"unsupported evidence schema on line {number}")
        result.append(row)
    return result


def _event(row, day, received, windows, ref, bounds, coverage_version=1):
    source, kind = row.get("source"), row.get("type")
    if type(row.get("schema_version")) is not int or row["schema_version"] != 1:
        raise ValueError("unsupported_schema")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("missing_source")
    event = {"type": kind, "source": source, "received_at": received}
    if isinstance(row.get("raw_ref"), str):
        event["raw_ref"] = row["raw_ref"]
    if kind in ("minute", "quote", "opening"):
        previous_close = row.get("previous_close")
        basis = row.get("price_basis_status")
        if row.get("price_basis_verified") is False or (
            isinstance(basis, str) and basis.lower() in PRICE_BASIS_CONFLICTS
        ):
            raise ValueError("price_basis_conflict")
        if previous_close is not None:
            if not _price(previous_close):
                raise ValueError("unverified_previous_close")
            if execution._money(previous_close) != execution._money(ref):
                raise ValueError("previous_close_conflicts_with_frozen_signal")
            event["previous_close"] = previous_close
        if isinstance(basis, str):
            event["price_basis_status"] = basis
    if kind == "minute":
        label, end = _stamp(row.get("label")), _stamp(row.get("end"))
        close, low = row.get("close"), row.get("low")
        if (
            row.get("complete") is not True
            or label.second or label.microsecond
            or end != label + MINUTE
            or not end <= received <= end + MINUTE
            or not any(start <= label < end <= stop for start, stop in windows)
            or not _price(close)
        ):
            raise ValueError("invalid_closed_minute")
        if not bounds[0] <= close <= bounds[1]:
            raise ValueError("minute_close_outside_ordinary_limits")
        return dict(event, label=label, end=end, close=close,
                    low=low if _price(low) and bounds[0] <= low <= close else None)
    if kind == "coverage":
        start, end = _stamp(row.get("start")), _stamp(row.get("end"))
        scope, complete = row.get("scope"), row.get("complete")
        if (
            scope not in ("quote", "minute")
            or type(complete) is not bool
            or not start.date() == end.date() == day
            or not start < end <= received
        ):
            raise ValueError("invalid_coverage_interval")
        reason = row.get("reason")
        version = row.get("sampling_coverage_version", 1)
        if scope == "quote":
            if type(version) is not int or version not in (1, 2) or version != coverage_version:
                raise ValueError("coverage_version_conflicts_with_frozen_configuration")
            if version == 2:
                fields = ("transport_ok", "symbol_parsed", "quote_usable", "current_fresh")
                if any(type(row.get(key)) is not bool for key in fields):
                    raise ValueError("missing_v2_sampling_diagnostics")
                if complete and not (row["transport_ok"] and row["symbol_parsed"]):
                    raise ValueError("complete_sampling_without_successful_symbol_response")
                event.update(sampling_coverage_version=2, **{key: row[key] for key in fields})
        return dict(event, start=start, end=end, scope=scope, complete=complete,
                    reason=reason if isinstance(reason, str) else None)
    source_at = _stamp(row.get("source_at"))
    if not source_at <= received or source_at.date() != day:
        raise ValueError("invalid_source_time")
    event["source_at"] = source_at
    if kind == "quote":
        price, shares = row.get("ask_price"), row.get("ask_shares")
        if not any(start <= source_at <= received < stop for start, stop in windows):
            raise ValueError("quote_outside_continuous_session")
        if (received - source_at).total_seconds() > 15:
            raise ValueError("stale_quote")
        if not _price(price) or not _count(shares) or row.get("quantity_verified") is not True:
            raise ValueError("unverified_quote_price_or_quantity")
        last = row.get("price")
        return dict(event, ask_price=price, ask_shares=shares, price=last if _price(last) else None)
    if kind == "opening":
        price, shares = row.get("open_price"), row.get("auction_shares")
        if (
            source_at.time() != time(9, 25)
            or received > windows[0][0]
            or not _price(price)
            or row.get("auction_verified") is not True
        ):
            raise ValueError("unverified_opening_auction")
        known_shares = shares if row.get("quantity_verified") is True and _count(shares) else None
        return dict(event, open_price=price, auction_shares=known_shares)
    if kind == "sector":
        key, version = row.get("key"), row.get("membership_version")
        expected, fresh, up = (row.get(field) for field in ("expected", "fresh", "up"))
        median = row.get("median_pct")
        if (
            not isinstance(key, str) or not key
            or not isinstance(version, str) or not version
            or not all(_count(value) for value in (expected, fresh, up))
            or not up <= fresh <= expected
            or (fresh and (type(median) not in (int, float) or not isfinite(median)))
        ):
            raise ValueError("invalid_sector_observation")
        return dict(event, key=key, membership_version=version,
                    expected=expected, fresh=fresh, up=up, median_pct=median if fresh else None)
    raise ValueError("unknown_evidence_type")


def _tape(events, day, symbol, sector_membership, mainline_keys, as_of, windows, ref, bounds,
          coverage_version=1):
    accepted, rejected = defaultdict(list), Counter()
    for row in events:
        if row.get("trade_date") != day.isoformat():
            continue
        if row.get("symbol") != symbol and not (
            row.get("type") == "sector" and row.get("symbol") is None
            and (row.get("key") in sector_membership or row.get("key") in mainline_keys)
        ):
            continue
        try:
            received = _stamp(row.get("received_at"))
            if received > as_of:
                continue
            if received.date() != day:
                raise ValueError("receipt_outside_trade_date")
            event = _event(row, day, received, windows, ref, bounds, coverage_version)
            if event["type"] == "sector":
                key = event["key"]
                membership = sector_membership.get(key)
                if membership:
                    if (
                        event["membership_version"] != membership["membership_version"]
                        or event["expected"] != membership["expected"]
                    ):
                        raise ValueError("sector_membership_conflicts_with_frozen_configuration")
                elif key not in mainline_keys:
                    raise ValueError("sector_membership_unlinked_to_candidate")
                event["membership_verified"] = bool(membership)
        except (ValueError, OverflowError) as exc:
            rejected[str(exc)] += 1
            continue
        accepted[event["type"]].append(event)
    for rows in accepted.values():
        rows.sort(key=lambda row: (row["received_at"], row["source"]))
    return accepted, rejected


def _first_minutes(rows):
    by_label = defaultdict(list)
    for row in rows:
        by_label[row["label"]].append(row)
    first = {}
    for label, observations in by_label.items():
        head = observations[0]
        same_time = [row for row in observations if row["received_at"] == head["received_at"]]
        if len({row["close"] for row in same_time}) == 1:
            # A conflicting optional low cannot establish a retest or a risk breach.
            low = head["low"] if len({row["low"] for row in same_time}) == 1 else None
            first[label] = dict(head, low=low)
    return first


def _covered(rows, source, start, end, version=1):
    if start == end:
        return True  # A quote at the start of the window has no earlier interval to verify.
    if version == 2:
        changes = defaultdict(list)
        for row in rows:
            if row["scope"] != "quote" or row.get("sampling_coverage_version") != 2:
                continue
            left, right = max(start, row["start"]), min(end, row["end"])
            if left < right:
                key = (row["source"], row["complete"])
                changes[left].append((key, 1))
                changes[right].append((key, -1))
        changes[start]
        changes[end]
        active = Counter()
        points = sorted(changes)
        for i, point in enumerate(points[:-1]):
            for key, delta in changes[point]:
                active[key] += delta
            if points[i + 1] > point and not any(
                count > 0 and good and active[(provider, False)] == 0
                for (provider, good), count in active.items()
            ):
                return False
        return True
    spans = [
        row for row in rows
        if row["scope"] == "quote" and row["source"] == source
        and row["end"] > start and row["start"] < end
    ]
    if any(not row["complete"] for row in spans):
        return False
    frontier = start
    for row in sorted(spans, key=lambda item: item["start"]):
        if row["start"] > frontier:
            break
        frontier = max(frontier, row["end"])
        if frontier >= end:
            return True
    return False


def _finish(order, status, reason):
    if status == "data_gap":
        order["coverage_gap"] = True
        status = "unavailable"
    order.update(status=status, entry_status=status, reason=reason)
    return order


def _order(day, secid, layer):
    return {
        "date": day, "secid": secid, "price_evidence_layer": layer, "status": "pending_entry",
        "entry_status": "pending_entry", "reason": "awaiting_entry_session",
        "buy": None, "sell": None, "eligible_for_validation": False,
        "net_return": None, "net_pnl": None, "cost": None, "total_cost": None,
    }


def _fill(order, row, price, shares, entry_day, opts):
    order["buy"] = dict(execution._cost(price, shares, "buy", opts), date=entry_day,
                        at=row["received_at"].isoformat(),
                        execution_phase="auction" if row["type"] == "opening" else "book",
                        evidence=_evidence(row))
    return _finish(order, "entered", "")


def _blocked(price, shares, bounds, minimum, opts):
    if not bounds[0] <= price <= bounds[1]:
        return "price_outside_ordinary_limits", 0
    if price >= bounds[1]:
        return "upper_limit_queue_unconfirmed", 0
    qty = minimum if opts.get("quantity_probe") else execution._shares(price, minimum, opts)
    if not qty:
        return "insufficient_cash_for_minimum_lot", 0
    if shares < qty:
        return "insufficient_verified_counterparty_shares", 0
    return "", qty


def _opening(order, rows, coverage, start, as_of, risk, bounds, minimum, opts):
    stop = start.replace(second=15)
    quotes = [row for row in rows if start <= row["received_at"] <= stop]
    if not quotes:
        if as_of < stop:
            return _finish(order, "pending_entry", "awaiting_opening_book_window")
        return _finish(order, "data_gap", "missing_usable_opening_book")
    row = quotes[0]
    order["observed_quote"] = _evidence(row)
    tied = [item for item in quotes if item["received_at"] == row["received_at"]]
    if len({(item["ask_price"], item["ask_shares"]) for item in tied}) > 1:
        return _finish(order, "data_gap", "conflicting_opening_book_quotes")
    if not _covered(coverage, row["source"], start, row["received_at"], opts.get("sampling_coverage_version", 1)):
        return _finish(order, "data_gap", "opening_book_coverage_unverified")
    price = row["ask_price"]
    blocked, qty = _blocked(price, row["ask_shares"], bounds, minimum, opts)
    if blocked == "price_outside_ordinary_limits":
        return _finish(order, "data_gap", blocked)
    if price <= risk:
        return _finish(order, "entry_invalidated", "opening_book_at_or_below_risk")
    if blocked:
        return _finish(order, "entry_expired", blocked)
    return _fill(order, row, price, qty, start.date().isoformat(), opts)


def _auction(order, rows, start, as_of, risk, bounds, minimum, opts):
    if not rows:
        if as_of < start:
            return _finish(order, "pending_entry", "awaiting_auction_evidence")
        return _finish(order, "data_gap", "missing_verified_auction_price")
    row = rows[0]
    order["observed_opening"] = _evidence(row)
    price = row["open_price"]
    if any(item["open_price"] != price for item in rows):
        return _finish(order, "data_gap", "conflicting_auction_prices")
    if not bounds[0] <= price <= bounds[1]:
        return _finish(order, "data_gap", "price_outside_ordinary_limits")
    if price <= risk:
        return _finish(order, "entry_invalidated", "auction_at_or_below_risk")
    verified = [item for item in rows if item["auction_shares"] is not None]
    if not verified:
        return _finish(order, "data_gap", "auction_counterparty_shares_unverified")
    row = verified[0]
    shares = row["auction_shares"]
    if any(item["auction_shares"] != shares for item in verified):
        return _finish(order, "data_gap", "conflicting_auction_counterparty_shares")
    blocked, qty = _blocked(price, shares, bounds, minimum, opts)
    if blocked:
        return _finish(order, "entry_expired", blocked)
    return _fill(order, row, price, qty, start.date().isoformat(), opts)


def _missing(minutes, labels, at, through=None):
    return [
        label.isoformat() for label in labels
        if (label + 2 * MINUTE <= at or (through is not None and label <= through))
        and (label not in minutes or minutes[label]["received_at"] > at)
    ]


def _confirmation(minutes, labels, trigger):
    pairs = [
        (minutes[label - MINUTE], minutes[label])
        for label in labels
        if label in minutes and label - MINUTE in minutes
        and min(minutes[label - MINUTE]["close"], minutes[label]["close"]) > trigger
    ]
    return min(pairs, key=lambda pair: (max(row["received_at"] for row in pair), pair[1]["label"]),
               default=None)


def _path(minutes, labels, pair, seen, trigger, opening_price):
    above = opening_price is not None and opening_price > trigger
    retest = False
    used_low = False
    for label in labels:
        if label > pair[1]["label"]:
            break
        row = minutes[label]
        if row["received_at"] > seen:
            continue
        low = row["low"]
        used_low |= low is not None
        retest |= above and (low if low is not None else row["close"]) <= trigger
        above |= row["close"] > trigger
    if retest:
        path = "retest_then_hold"
    elif opening_price is None:
        path = "opening_path_unknown"
    elif opening_price > trigger:
        path = "direct_hold"
    else:
        path = "breakout_then_hold"
    return {"type": path, "basis": "minute_close_and_available_low" if used_low else "minute_close"}


def _quote_prefix(coverage, source, seen, end, windows, version=1):
    return all(
        _covered(coverage, source, max(start, seen), min(stop, end), version)
        for start, stop in windows if start <= end and seen < stop
    )


def _confirmed(order, minutes, labels, tape, windows, as_of, opening_price, trigger, risk,
               bounds, minimum, opts):
    pair = _confirmation(minutes, labels, trigger)
    seen = max(row["received_at"] for row in pair) if pair else None
    risk_rows = sorted(
        (row for row in minutes.values()
         if (row["low"] if row["low"] is not None else row["close"]) <= risk),
        key=lambda row: (row["received_at"], row["label"]),
    )
    breach = risk_rows[0] if risk_rows else None
    if opening_price is not None and opening_price <= risk:
        return _finish(order, "entry_invalidated", "opening_at_or_below_risk")
    if pair:
        order["confirmation"] = {
            "at": seen.isoformat(), "trigger": trigger, "minutes": [_evidence(row) for row in pair]
        }
    first_check, through = as_of, None
    if breach and (seen is None or breach["received_at"] <= seen):
        first_check, through = breach["received_at"], breach["label"]
    elif pair:
        first_check, through = seen, pair[1]["label"]
    missing = _missing(minutes, labels, first_check, through)
    if missing:
        order["missing_minute_labels"] = missing
        return _finish(order, "data_gap", "incomplete_minute_path")
    if breach and first_check == breach["received_at"]:
        order["risk_breach"] = _evidence(breach)
        return _finish(order, "entry_invalidated", "minute_at_or_below_risk")
    cutoff = windows[-1][1]
    if not pair:
        if as_of < cutoff + MINUTE:
            return _finish(order, "pending_entry", "awaiting_complete_confirmation_window")
        return _finish(order, "confirmation_not_met", "no_adjacent_closes_strictly_above_trigger")

    order["path"] = _path(minutes, labels, pair, seen, trigger, opening_price)
    quotes = [row for row in tape["quote"] if row["source_at"] >= seen and row["received_at"] > seen]
    attempts = []
    order["quote_attempts"] = attempts
    if breach:
        quotes = [row for row in quotes if row["received_at"] < breach["received_at"]]
    for at, group in groupby(quotes, key=lambda item: item["received_at"]):
        tied = list(group)
        row = tied[0]
        price = row["ask_price"]
        order["observed_quote"] = _evidence(row)
        if len({(item["ask_price"], item["ask_shares"]) for item in tied}) > 1:
            return _finish(order, "data_gap", "conflicting_confirmed_book_quotes")
        missing = _missing(minutes, labels, at)
        if missing:
            order["missing_minute_labels"] = missing
            return _finish(order, "data_gap", "incomplete_minutes_while_waiting_for_quote")
        if not _quote_prefix(tape["coverage"], row["source"], seen, at, windows,
                             opts.get("sampling_coverage_version", 1)):
            return _finish(order, "data_gap", "confirmation_book_coverage_unverified")
        blocked, qty = _blocked(price, row["ask_shares"], bounds, minimum, opts)
        if blocked == "price_outside_ordinary_limits":
            return _finish(order, "data_gap", blocked)
        if price <= risk:
            return _finish(order, "entry_invalidated", "confirmed_book_at_or_below_risk")
        if price < trigger:
            return _finish(order, "confirmation_lost", "confirmed_book_below_trigger")
        if not blocked:
            if opening_price is None:
                return _finish(order, "data_gap", "opening_risk_unverified")
            return _fill(order, row, price, qty, at.date().isoformat(), opts)
        attempts.append({"reason": blocked, "quote": _evidence(row)})

    end = breach["received_at"] if breach else min(as_of, cutoff)
    missing = _missing(minutes, labels, end, breach["label"] if breach else None)
    if missing:
        order["missing_minute_labels"] = missing
        return _finish(order, "data_gap", "incomplete_minutes_while_waiting_for_quote")
    covered = any(
        _quote_prefix(tape["coverage"], source, seen, end, windows,
                      opts.get("sampling_coverage_version", 1))
        for source in {row["source"] for row in tape["coverage"] if row["scope"] == "quote"}
    )
    if breach:
        order["risk_breach"] = _evidence(breach)
        if covered:
            return _finish(order, "entry_invalidated", "minute_at_or_below_risk_before_buy")
        return _finish(order, "data_gap", "unverified_quote_path_before_risk_breach")
    if as_of < cutoff:
        return _finish(order, "pending_entry", "awaiting_confirmed_executable_book")
    if seen >= cutoff:
        return _finish(order, "entry_expired", "confirmation_visible_after_continuous_session")
    if covered and attempts:
        return _finish(order, "entry_expired", "no_executable_book_before_cutoff")
    return _finish(order, "data_gap", "missing_usable_confirmed_book")


def _exit(order, days, idx, series, opts, limit, as_of):
    if not order["buy"]:
        return
    entry_day = days[idx + 1]
    last = as_of.date().isoformat()
    complete = [day for day in days if day < last or (day == last and as_of.time() >= time(15))]
    order.update(status="pending_exit", reason="awaiting_d_plus_five_close")
    if entry_day not in complete:
        return
    session = series.get(entry_day)
    reason, _ = execution._session(session, series.get(days[idx]), limit)
    if reason:
        order.update(status="unavailable", reason=reason, problem_date=entry_day)
        if not session:
            order["path_gaps"] = [entry_day]
        return
    exit_ = execution.close_exits(
        order["buy"], complete, idx + 1, idx, series, limit, (5,), opts, defer_missing=True
    )[5]
    order.update({key: value for key, value in exit_.items() if key not in ("target_date", "attempts")})
    order["exit_attempts"] = exit_["attempts"]
    if exit_["status"] == "closed":
        order["sell"]["execution_phase"] = "close"
        order.update(cost=order["total_cost"], eligible_for_validation=not order.get("path_gaps"))


def _sector_at(rows, at):
    latest = {
        (row["key"], row["membership_version"]): _evidence(row)
        for row in rows if row["received_at"] <= at
    }
    return list(latest.values())


def replay_candidate(candidate, events, days, series, *, as_of, opts=None, quantity_probe=False):
    """Replay a single frozen candidate against its D+1 observations and completed daily exits."""
    as_of = _stamp(as_of)
    day, secid, symbol, mode = (candidate[key] for key in ("date", "secid", "symbol", "mode"))
    trigger, risk = (candidate[key] for key in ("trigger", "risk"))
    o, r, auction = (_order(day, secid, layer) for layer in (OPEN_BOOK, CONFIRMED_BOOK, AUCTION))
    result = {
        **candidate, "candidate_key": [day, secid, mode], "as_of": as_of.isoformat(),
        "entry_session": None, "target_date": None, "orders": {"O": o, "R": r},
        "opening_auction": auction, "coverage": {}, "sector": [],
    }

    def end_all(status, reason):
        for order in (o, r, auction):
            _finish(order, status, reason)
        return result

    try:
        frozen_at = _stamp(candidate.get("available_at") or candidate.get("generated_at"))
    except ValueError:
        return end_all("excluded", "missing_valid_frozen_candidate_time")
    if frozen_at.date().isoformat() != day or frozen_at.time() < time(15):
        return end_all("excluded", "candidate_not_frozen_on_signal_day_after_close")
    if frozen_at > as_of:
        return end_all("pending_entry", "candidate_not_yet_visible")
    if day not in days:
        return end_all("data_gap", "signal_session_missing_from_calendar")
    idx = days.index(day)
    if idx + 1 >= len(days):
        return end_all("pending_entry", "awaiting_d_plus_one_calendar")
    entry_day = date.fromisoformat(days[idx + 1])
    target = days[idx + 5] if idx + 5 < len(days) else None
    result.update(entry_session=entry_day.isoformat(), target_date=target)
    for order in (o, r, auction):
        order["target_date"] = target
    if as_of.date() < entry_day:
        return result
    rules = execution._board(secid)
    name = candidate.get("name")
    if not rules or not isinstance(name, str) or not name or any(tag in name.upper() for tag in ("ST", "退")):
        return end_all("excluded", "outside_supported_ordinary_a_share_scope")
    if day < "2026-07-06":
        return end_all("data_gap", "outside_verified_trading_rule_period")
    bars = strategy_execution._Bars(series)
    first = candidate.get("first_seen") or min(
        (d for d, row in series.items() if d <= day and _price(row.get("close"))), default=None
    )
    if idx + 1 < 5 or not first or first > days[idx - 4]:
        return end_all("data_gap", "first_five_listing_sessions_unverified")
    signal = bars.get(day) or {}
    frozen_bar = candidate.get("signal_bar") or {}
    ref = frozen_bar.get("close", signal.get("close"))
    if (
        signal.get("source") != "spot" or frozen_bar.get("source", "spot") != "spot"
        or not _price(ref) or ref != signal.get("close")
        or not (_price(risk) and _price(trigger) and risk < trigger)
    ):
        return end_all("data_gap", "unverified_signal_prices_or_price_basis")
    if ref <= risk:
        return end_all("entry_invalidated", "signal_close_at_or_below_risk")
    windows = [(datetime.combine(entry_day, a, SHANGHAI), datetime.combine(entry_day, b, SHANGHAI))
               for a, b in SESSIONS]
    labels = [start + n * MINUTE for start, stop in windows for n in range((stop - start) // MINUTE)]
    limit, minimum = rules
    bounds = execution._limit_bounds(ref, limit)
    sector_membership = candidate.get("sector_membership", {})
    coverage_version = candidate.get("sampling_coverage_version", 1)
    if type(coverage_version) is not int or coverage_version not in (1, 2):
        return end_all("data_gap", "unsupported_sampling_coverage_version")
    mainline_keys = {line["key"] for line in candidate.get("mainlines", [])}
    tape, rejected = _tape(events, entry_day, symbol, sector_membership, mainline_keys,
                           as_of, windows, ref, bounds, coverage_version)
    minutes = _first_minutes(tape["minute"])
    result["coverage"] = {
        "expected_minute_labels": len(labels), "visible_minute_labels": len(minutes),
        "elapsed_minute_labels": sum(label + 2 * MINUTE <= as_of for label in labels),
        "accepted_records": {key: len(tape[key]) for key in ("minute", "quote", "opening", "sector", "coverage")},
        "rejected_records": dict(rejected), "declared_intervals": [_evidence(row) for row in tape["coverage"]],
    }
    result["sector"] = _sector_at(tape["sector"], as_of)
    options = strategy_execution._settings(None, opts)
    # An explicit research-only probe locates a one-lot opportunity before sizing.
    # The regular O/R report retains its original fixed-budget eligibility.
    options["quantity_probe"] = quantity_probe
    options["sampling_coverage_version"] = coverage_version
    if quantity_probe:
        result["quantity_basis"] = "one_lot_probe_for_separate_sizing_research"
    start = windows[0][0]
    _auction(auction, tape["opening"], start, as_of, risk, bounds, minimum, options)
    if auction["reason"] == "auction_at_or_below_risk":
        o["observed_opening"] = auction["observed_opening"]
        _finish(o, "entry_invalidated", "auction_at_or_below_risk")
    else:
        _opening(o, tape["quote"], tape["coverage"], start, as_of, risk, bounds, minimum, options)
    opening_price = None
    if auction.get("observed_opening") and auction["reason"] not in (
        "conflicting_auction_prices", "price_outside_ordinary_limits",
    ):
        opening_price = auction["observed_opening"]["open_price"]
    elif not o.get("coverage_gap") and o.get("observed_quote"):
        opening_price = o["observed_quote"]["ask_price"]
    _confirmed(r, minutes, labels, tape, windows, as_of, opening_price,
               trigger, risk, bounds, minimum, options)
    if r.get("confirmation"):
        r["sector_at_confirmation"] = _sector_at(tape["sector"], _stamp(r["confirmation"]["at"]))
    for order in (o, r, auction):
        if order["buy"]:
            order["sector_at_entry"] = _sector_at(tape["sector"], _stamp(order["buy"]["at"]))
        _exit(order, days, idx, bars, options, limit, as_of)
    return result


def summarize(records):
    """Aggregate the matched book layers and preserve separate auction evidence counts."""
    records = list(records)
    summary = _stats(records)
    summary["opening_auction_statuses"] = dict(Counter(row["opening_auction"]["status"] for row in records))
    summary["confirmation_paths"] = dict(Counter(
        row["orders"]["R"]["path"]["type"] for row in records if row["orders"]["R"].get("path")
    ))
    book_pairs = [
        (row["orders"]["O"]["buy"]["evidence"]["source"],
         row["orders"]["R"]["buy"]["evidence"]["source"])
        for row in records if row["orders"]["O"]["buy"] and row["orders"]["R"]["buy"]
    ]
    summary["book_source_pairs"] = {
        "same_source": sum(o == r for o, r in book_pairs),
        "different_sources": sum(o != r for o, r in book_pairs),
    }
    return summary
