"""Evidence for after-close themes, calculated from a single SQLite read snapshot."""

import json
from datetime import date
from math import isfinite
from statistics import median

from .readiness import _bars
from .tzutil import now, parse_stamp

VERSION = "1.0"
MAPPING_KINDS = {"sw": "申万一级", "gn": "概念", "chgn": "热门概念"}
MIN_SAMPLE = 10
MIN_COVERAGE = 0.8
MIN_RISE_RATIO = 0.6
MIN_MEDIAN_PCT = 0.8
MIN_RELATIVE_PP = 0.5
MIN_AMOUNT_RATIO = 1.1
MIN_ANCHOR_AMOUNT = 100_000_000
MAX_MAINLINES = 3


def _market(store, day):
    stage = _bars(store, day)
    observed = parse_stamp(stage["observed_at"])
    finalized = bool(
        observed
        and observed.date().isoformat() == day
        and observed.strftime("%H:%M") >= "15:00"
        and stage["expected"]
    )
    stage["after_close_snapshot"] = finalized
    stage["ready"] = stage["ready"] and finalized
    if not finalized and not stage["reason"]:
        stage["reason"] = "after_close_snapshot_unverified"
    return stage


def _groups(conn, day):
    groups, mappings = {}, {}
    cutoff = day + " 23:59:59.999999"
    for kind, label in MAPPING_KINDS.items():
        latest = conn.execute(
            "SELECT MAX(updated_at) FROM symbol_tags WHERE kind=?", (kind,)
        ).fetchone()[0]
        if latest and latest <= cutoff:
            stamp = latest
            rows = conn.execute("SELECT secid,code,name FROM symbol_tags WHERE kind=?", (kind,))
        else:
            stamp = conn.execute(
                "SELECT MAX(observed_at) FROM symbol_tag_history WHERE kind=? AND observed_at<=?",
                (kind, cutoff),
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT secid,code,name FROM symbol_tag_history WHERE kind=? AND observed_at=?",
                (kind, stamp),
            )
        codes, members = set(), set()
        for secid, code, name in rows:
            key = f"{kind}:{code}"
            groups.setdefault(key, {"key": key, "kind": kind, "name": name, "members": set()})
            groups[key]["members"].add(secid)
            codes.add(code)
            members.add(secid)
        mappings[kind] = {
            "label": label,
            "source": "mairui.hszg.gg",
            "observed_at": stamp,
            "nodes": len(codes),
            "members": len(members),
        }
    return groups, mappings


def _daily_rows(conn, days):
    by_day = {d: {} for d in days}
    if days:
        placeholders = ",".join("?" for _ in days)
        for row in conn.execute(
            "SELECT b.date,b.secid,b.close,b.low,b.pct_chg,b.amount,b.source,"
            "s.code,s.name,s.is_st,s.listed_date "
            "FROM bars b LEFT JOIN symbols s ON s.secid=b.secid "
            f"WHERE b.date IN ({placeholders}) AND b.close>0 AND b.volume>0",
            days,
        ):
            pct, amount, close = row["pct_chg"], row["amount"], row["close"]
            if (
                all(
                    type(value) in (float, int) and isfinite(value)
                    for value in (pct, amount, close)
                )
                and pct > -100
                and amount > 0
                and close > 0
            ):
                by_day[row["date"]][row["secid"]] = row
    return by_day


def _breadth(rows):
    rising = sum(row["pct_chg"] > 0 for row in rows)
    return rising, rising / len(rows) if rows else None


def _cross_section(members, daily, baseline):
    rows = [daily[sid] for sid in sorted(members & daily.keys())]
    if not rows:
        return rows, None
    up, ratio = _breadth(rows)
    middle = median(row["pct_chg"] for row in rows)
    return rows, {
        "sample": len(rows),
        "coverage_ratio": len(rows) / len(members),
        "rising": up,
        "rising_ratio": ratio,
        "median_pct_chg": middle,
        "relative_pct_points": middle - baseline,
    }


def _broad(evidence):
    return bool(
        evidence
        and evidence["sample"] >= MIN_SAMPLE
        and evidence["coverage_ratio"] >= MIN_COVERAGE
        and evidence["rising_ratio"] >= MIN_RISE_RATIO
        and evidence["median_pct_chg"] >= MIN_MEDIAN_PCT
        and evidence["relative_pct_points"] >= MIN_RELATIVE_PP
    )


def _metrics(group, days, by_day, benchmarks):
    day, *history = days
    members = group["members"]
    rows, evidence = _cross_section(members, by_day[day], benchmarks[day])
    if not evidence:
        return None
    leading = sorted(rows, key=lambda r: (-r["pct_chg"], r["secid"]))[:3]
    leader_codes = {r["secid"] for r in leading}
    followers = [r for r in rows if r["secid"] not in leader_codes]
    follower_up, follower_ratio = _breadth(followers)

    comparable = set(r["secid"] for r in rows)
    for d in history:
        comparable.intersection_update(by_day[d])
    recent_amount = amount_ratio = None
    if len(history) == 5 and len(comparable) >= MIN_COVERAGE * len(rows):
        recent_amount = median(sum(by_day[d][s]["amount"] for s in comparable) for d in history)
        amount_ratio = sum(by_day[day][s]["amount"] for s in comparable) / recent_amount
    sessions = []
    for d in reversed(days[:5]):
        _, daily_evidence = _cross_section(members, by_day[d], benchmarks[d])
        sessions.append(
            {"date": d, "broad_strength": _broad(daily_evidence), **(daily_evidence or {})}
        )
    strong3 = sum(s["broad_strength"] for s in sessions[-3:])
    strong5 = sum(s["broad_strength"] for s in sessions)
    persistent = (len(sessions) >= 3 and strong3 >= 2) or (len(sessions) == 5 and strong5 >= 3)
    failures = []
    if not _broad(evidence):
        failures.append("insufficient_breadth_or_excess")
    if not follower_ratio or follower_ratio < MIN_RISE_RATIO:
        failures.append("leaders_without_broad_followers")
    if amount_ratio is None or amount_ratio < MIN_AMOUNT_RATIO:
        failures.append("insufficient_amount_expansion")
    if not persistent:
        failures.append("unconfirmed_persistence")
    total_amount = sum(r["amount"] for r in rows)
    top_amounts = sorted((r["amount"] for r in rows), reverse=True)[:3]
    return {
        "member_count": len(members),
        "sample_secids": [r["secid"] for r in rows],
        "amount_yuan": total_amount,
        "top_three_amount_share": sum(top_amounts) / total_amount,
        "amount_comparable_sample": len(comparable) if len(history) == 5 else 0,
        "prior_five_median_amount_yuan": recent_amount,
        "amount_ratio_5d": amount_ratio,
        "leader_rising": sum(r["pct_chg"] > 0 for r in leading),
        "follower_sample": len(followers),
        "follower_rising": follower_up,
        "follower_rising_ratio": follower_ratio,
        "strong_sessions_3d": strong3,
        "strong_sessions_5d": strong5,
        "sessions": sessions,
        "failures": failures,
        **evidence,
    }


def _eligible_anchor(row, day):
    secid = row["secid"]
    prefix, _, code = secid.partition(".")
    return (
        prefix in ("0", "1")
        and len(code) == 6
        and code.isascii()
        and code.isdigit()
        and row["code"] == code
        and row["name"]
        and row["is_st"] == 0
        and not (prefix == "1" and code.startswith(("688", "689")))
        and (not row["listed_date"] or row["listed_date"] <= day)
        and row["pct_chg"] > 0
        and row["amount"] >= MIN_ANCHOR_AMOUNT
        and type(row["low"]) in (int, float)
        and isfinite(row["low"])
        and 0 < row["low"] <= row["close"]
    )


def _anchors(evidence, days, by_day):
    day, *history = days
    candidates = [
        by_day[day][sid]
        for sid in evidence["sample_secids"]
        if _eligible_anchor(by_day[day][sid], day)
    ]
    if not candidates:
        return []

    def amount_ratio(row):
        sid = row["secid"]
        if len(history) == 5 and all(sid in by_day[d] for d in history):
            return row["amount"] / median(by_day[d][sid]["amount"] for d in history)
        return None

    roles = [
        ("leader", lambda r: (-r["pct_chg"], -r["amount"], r["secid"])),
        ("turnover_core", lambda r: (-r["amount"], -r["pct_chg"], r["secid"])),
        ("follow_through", lambda r: (-(amount_ratio(r) or 0), -r["amount"], r["secid"])),
    ]
    result = []
    for role, rank in roles:
        pool = candidates
        if role == "follow_through":
            pool = [r for r in pool if (amount_ratio(r) or 0) >= MIN_AMOUNT_RATIO]
        if not pool:
            continue
        row = min(pool, key=rank)
        candidates = [r for r in candidates if r["secid"] != row["secid"]]
        code = ("SH" if row["secid"].startswith("1.") else "SZ") + row["code"]
        result.append(
            {
                "role": role,
                "symbol": code,
                "secid": row["secid"],
                "name": row["name"],
                "pct_chg": row["pct_chg"],
                "amount_yuan": row["amount"],
                "amount_ratio_5d": amount_ratio(row),
                "close": row["close"],
                "low": row["low"],
                "bar_source": row["source"],
                "confirmation": {
                    "close_at_least": row["close"],
                    "group_rising_ratio_at_least": MIN_RISE_RATIO,
                    "group_relative_pct_points_at_least": MIN_RELATIVE_PP,
                },
                "invalidation": {
                    "close_below": row["low"],
                    "or_group_rising_ratio_below": 0.45,
                    "and_group_relative_pct_points_below": 0,
                },
            }
        )
    return result


def _overlap(left, right):
    shared = len(left & right)
    return shared >= 8 and (
        shared / len(left | right) >= 0.5 or shared / min(len(left), len(right)) >= 0.8
    )


def _classify(groups, days, by_day, benchmarks):
    if not days or not by_day[days[0]]:
        return [], {}, []
    metrics = {
        key: evidence
        for key, group in groups.items()
        if (evidence := _metrics(group, days, by_day, benchmarks)) is not None
    }
    ordered = sorted(
        metrics,
        key=lambda k: (
            -metrics[k]["relative_pct_points"],
            -(metrics[k]["amount_ratio_5d"] or 0),
            -metrics[k]["rising_ratio"],
            k,
        ),
    )
    selected, observations = [], []
    for key in ordered:
        group, evidence = groups[key], metrics[key]
        label = {k: group[k] for k in ("key", "kind", "name")}
        if evidence["failures"]:
            if len(observations) < 3 and (
                _broad(evidence)
                or evidence["leader_rising"]
                and evidence["relative_pct_points"] > 0
            ):
                observations.append(
                    {
                        **label,
                        "status": "pulse" if _broad(evidence) else "isolated_strength",
                        "sample": evidence["sample"],
                        "failures": evidence["failures"],
                    }
                )
            continue
        duplicate = next(
            (s for s in selected if _overlap(group["members"], groups[s["key"]]["members"])),
            None,
        )
        if duplicate:
            duplicate["overlapping_groups"].append(label)
        elif len(selected) < MAX_MAINLINES:
            selected.append(
                {
                    **label,
                    "evidence": evidence,
                    "anchors": _anchors(evidence, days, by_day),
                    "overlapping_groups": [],
                }
            )
    return selected, metrics, observations


def _previous(lines, previous_groups, current_groups, current_metrics, day):
    result = []
    for line in lines:
        key = line["key"]
        evidence = current_metrics.get(key)
        current = current_groups.get(key)
        old = previous_groups[key]["members"]
        status = "pending"
        if (
            current
            and evidence
            and evidence["sample"] >= MIN_SAMPLE
            and evidence["coverage_ratio"] >= MIN_COVERAGE
            and len(old & current["members"]) / len(old | current["members"]) >= MIN_COVERAGE
        ):
            if not evidence["failures"]:
                status = "continuing"
            elif evidence["rising_ratio"] < 0.45 and evidence["relative_pct_points"] < 0:
                status = "fading"
            else:
                status = "diverging"
        result.append(
            {
                "key": key,
                "name": line["name"],
                "date": day,
                "status": status,
                "rising_ratio": evidence["rising_ratio"] if evidence else None,
                "relative_pct_points": evidence["relative_pct_points"] if evidence else None,
            }
        )
    return result


def report(store, day=None, *, use_archive=True):
    if day is not None and date.fromisoformat(day).isoformat() != day:
        raise ValueError("date must be YYYY-MM-DD")
    conn = store.conn
    conn.execute("SAVEPOINT mainline_read")
    try:
        return _report(store, day, use_archive=use_archive)
    finally:
        conn.execute("RELEASE mainline_read")


def materialize(store, day):
    """Save a completed day's evidence for later read-only history queries."""
    result = report(store, day, use_archive=False)
    if not result["publication_ready"]:
        return False
    generated = now().isoformat(timespec="microseconds")
    with store.conn:
        store.conn.execute(
            "INSERT INTO market_mainline_reports(date,generated_at,payload) VALUES(?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET generated_at=excluded.generated_at,payload=excluded.payload "
            "WHERE excluded.generated_at>=market_mainline_reports.generated_at",
            (day, generated, json.dumps(result, ensure_ascii=False, allow_nan=False)),
        )
    return True


def _archived(conn, day, market):
    if not market["ready"]:
        return None
    row = conn.execute(
        "SELECT generated_at,payload FROM market_mainline_reports WHERE date=?", (day,)
    ).fetchone()
    if not row:
        return None
    result = json.loads(row["payload"])
    if (
        result["schema_version"] != VERSION
        or result["market"]["observed_at"] != market["observed_at"]
    ):
        return None
    result["publication"] = {"state": "archived", "generated_at": row["generated_at"]}
    return result


def _recent_sessions(store, day):
    """Use the already cached exchange calendar to expose whole-session gaps."""
    dates = {
        row[0]
        for row in store.conn.execute(
            "SELECT date FROM trading_days WHERE date<=? ORDER BY date DESC LIMIT 7", (day,)
        )
    }
    year = int(day[:4])
    calendar_used = False
    for y in (year, year - 1):
        raw = store.get_meta(f"mairui_calendar:{y}")
        if not raw:
            continue
        try:
            official = json.loads(raw)["days"]
            if not isinstance(official, list) or not official:
                continue
            dates.update(
                stamp
                for stamp in official
                if isinstance(stamp, str)
                and stamp.startswith(f"{y:04d}-")
                and len(stamp) == 10
                and stamp <= day
            )
            calendar_used = True
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(dates, reverse=True)[:7], calendar_used


def _history_complete(days, by_day):
    if len(days) != 6:
        return False
    count = len(by_day[days[0]])
    return count >= MIN_SAMPLE and all(
        len(by_day[d]) >= max(MIN_SAMPLE, MIN_COVERAGE * count) for d in days[1:]
    )


def _report(store, day, *, use_archive):
    conn = store.conn
    dates = [
        r[0] for r in conn.execute("SELECT date FROM trading_days ORDER BY date DESC LIMIT 250")
    ]
    day = day or (dates[0] if dates else store.latest_date())
    result = {
        "schema_version": VERSION,
        "date": day,
        "dates": dates,
        "status": "waiting",
        "publication_ready": False,
        "market": None,
        "coverage": None,
        "mappings": {},
        "benchmark": None,
        "mainlines": [],
        "observations": [],
        "previous": None,
        "gaps": [],
        "publication": {"state": "computed", "generated_at": None},
    }
    if not day:
        result["gaps"].append("no_trading_day_data")
        return result
    days, calendar_used = _recent_sessions(store, day)
    market = _market(store, day)
    archived = _archived(conn, day, market) if use_archive else None
    if archived:
        return {**archived, "dates": dates}
    groups, mappings = _groups(conn, day)
    result.update(market=market, mappings=mappings)
    if not any(mappings[k]["nodes"] for k in ("gn", "chgn")):
        result["gaps"].append("concept_membership_unavailable")
    if not mappings["sw"]["nodes"]:
        result["gaps"].append("industry_membership_unavailable")
    if not days or days[0] != day:
        result.update(status="waiting" if day >= now().date().isoformat() else "degraded")
        result["gaps"].append("no_requested_trading_day")
        return result
    by_day = _daily_rows(conn, days)
    benchmarks = {
        d: median(r["pct_chg"] for r in rows.values()) if rows else 0 for d, rows in by_day.items()
    }
    stocks = by_day[day]
    mapped = set().union(*(g["members"] for g in groups.values() if g["kind"] == "sw"))
    result["coverage"] = {
        "valid_price_amount_rows": len(stocks),
        "industry_mapped_rows": len(stocks.keys() & mapped),
        "anchor_permission_excluded": sum(s.startswith(("1.688", "1.689")) for s in stocks),
        "history_dates": list(reversed(days[:6])),
        "calendar_source": "cached_exchange_calendar" if calendar_used else "recorded_trading_days",
    }
    if stocks:
        result["benchmark"] = {
            "kind": "equal_weight_cross_section_median",
            "sample": len(stocks),
            "median_pct_chg": benchmarks[day],
        }
    if not market["ready"]:
        result["gaps"].append(market["reason"] or "market_coverage_incomplete")
    if len(stocks) < max(MIN_SAMPLE, market["active_bars"] * 0.98):
        result["gaps"].append("price_or_amount_coverage_incomplete")
    history_ready = _history_complete(days[:6], by_day)
    if not history_ready:
        result["gaps"].append("recent_sessions_incomplete")
    ready = (
        market["ready"]
        and groups
        and history_ready
        and "price_or_amount_coverage_incomplete" not in result["gaps"]
    )
    current_metrics = {}
    if ready:
        lines, current_metrics, observations = _classify(groups, days[:6], by_day, benchmarks)
        result.update(publication_ready=True, mainlines=lines, observations=observations)
    result["status"] = (
        "waiting"
        if not market["after_close_snapshot"] and day >= now().date().isoformat()
        else "degraded" if result["gaps"] else "ready"
    )
    if len(days) > 1:
        prev_day = days[1]
        prev_market = _market(store, prev_day)
        prev_groups, _ = _groups(conn, prev_day)
        prev_ready = (
            prev_market["ready"]
            and prev_groups
            and _history_complete(days[1:], by_day)
            and len(by_day[prev_day]) >= max(MIN_SAMPLE, prev_market["active_bars"] * 0.98)
        )
        prev_archive = _archived(conn, prev_day, prev_market)
        prev_lines = (
            prev_archive["mainlines"]
            if prev_archive and prev_groups
            else _classify(prev_groups, days[1:], by_day, benchmarks)[0] if prev_ready else []
        )
        prev_lines = [line for line in prev_lines if line["key"] in prev_groups]
        result["previous"] = {
            "date": prev_day,
            "status": "evaluated" if prev_ready or prev_archive else "pending",
            "items": _previous(prev_lines, prev_groups, groups, current_metrics, prev_day),
        }
    return result
