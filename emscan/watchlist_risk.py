"""按请求日原样返回逐股负面信号和证据。只访问本地行情，不读取用户自选。"""

from __future__ import annotations

import math
import re
from datetime import date as Date

import pandas as pd

from .indicators import compute_frame

from .mairui import board_of, dm_of, secid_of


MAX_SYMBOLS = 100
RULE_VERSION = "2026-11-09.1"
BENCHMARKS = {
    "sh_main": ("000001", "上证指数"),
    "sz_main": ("399001", "深证成指"),
    "sz_gem": ("399006", "创业板指"),
    "sh_star": ("000688", "科创50"),
}
PARAMETERS = {
    "high_volume_drop": {"return_max_pct": -3.0, "prior_sessions": 5, "volume_ratio_min": 1.5},
    "persistent_decline": {"sessions": 5, "negative_days_min": 4, "cumulative_max_pct": -5.0},
    "macd_dead_cross": {
        "fast": 12,
        "slow": 26,
        "signal": 9,
        "min_valid_bars": 100,
        "comparison": "prev DIF>=DEA, now DIF<DEA",
    },
    "macd_bearish_divergence": {
        "peak_left": 2,
        "peak_right": 2,
        "min_peak_gap_sessions": 5,
        "max_peak_gap_sessions": 60,
        "price": "high",
        "oscillator": "DIF",
    },
    "market_up_stock_down": {"stock_lt_pct": 0, "benchmark_gt_pct": 0},
    "market_up_stock_down_significant": {
        "stock_lt_pct": 0,
        "benchmark_gt_pct": 0,
        "stock_minus_benchmark_max_pp": -2.0,
    },
    "relative_underperformance": {"stock_minus_benchmark_max_pp": -2.0},
}
# 近5天、前一日的新触发判定、候选峰右侧2天、两峰最多60日、前峰左侧2天。
HISTORY = 5 + 1 + 2 + 60 + 2 + PARAMETERS["macd_dead_cross"]["min_valid_bars"]


class InvalidRequest(ValueError):
    pass


def _symbol(raw):
    value = raw.strip().upper()
    match = re.fullmatch(r"(SH|SZ)([0-9]{6})", value)
    if match:
        return secid_of(match[2], match[1]), None
    match = re.fullmatch(r"([0-9]{6})\.(SH|SZ)", value)
    if match:
        return secid_of(match[1], match[2]), None
    if re.fullmatch(r"[01]\.[0-9]{6}", value):
        return value, None
    if re.match(r"^(BJ|HK|US|2\.)", value) or re.search(r"\.(BJ|HK|US)$", value):
        return None, "unsupported_market"
    return None, "invalid_symbol"


def _result(status="unavailable", reason=None, evidence=None, trigger=None, confirmation=None):
    return {
        "status": status,
        "reason": reason,
        "evidence": evidence or {},
        "trigger_date": trigger,
        "confirmation_date": confirmation,
        "new_trigger": None if status == "unavailable" else False,
    }


def _unavailable(reason):
    return {name: _result(reason=reason) for name in PARAMETERS}


def _check(condition, day, evidence, trigger=None):
    if condition:
        return _result("triggered", evidence=evidence, trigger=trigger or day, confirmation=day)
    return _result("not_triggered", evidence=evidence)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _valid(row):
    return bool(row and (row["close"] or 0) > 0 and (row["volume"] or 0) > 0)


def _macd_pair(first, second):
    """两个日期统一口径：优先两日完整且来源相同的已存指标，其次用同一组日线重算。"""
    if first is None or second is None:
        return None, None, None, None, None
    if first["indicator_source"] == second["indicator_source"] and all(
        r[k] is not None for r in (first, second) for k in ("macd_diff", "macd_dea")
    ):
        return (
            first["macd_diff"],
            first["macd_dea"],
            second["macd_diff"],
            second["macd_dea"],
            first["indicator_source"] or "indicators",
        )
    return (
        first.get("computed_dif"),
        first.get("computed_dea"),
        second.get("computed_dif"),
        second.get("computed_dea"),
        "recomputed_from_local_bars",
    )


def _divergence(days, bars, i, valid_counts):
    # 在确认日 i 只考察 i-2 是否为第二个峰，向前寻找最近已确认峰。
    span = PARAMETERS["macd_bearish_divergence"]["max_peak_gap_sessions"]
    pivot = i - 2
    if pivot < 2:
        return _result(reason="insufficient_peak_history")

    def usable_high(day):
        row = bars.get(day)
        return _valid(row) and row["high"] is not None and row["high"] > 0

    def peak_at(k):
        window = days[k - 2 : k + 3]
        if not all(usable_high(day) for day in window):
            return None
        price = bars[days[k]]["high"]
        return all(price > bars[d]["high"] for d in window if d != days[k])

    def missing_peak(k):
        return _result(
            reason="missing_peak_bars",
            evidence={
                **evidence,
                "calendar_basis": "market_trading_sessions",
                "blocking_window": days[k - 2 : k + 3],
                "missing_sessions": [
                    d for d in days[max(0, pivot - span - 2) : i + 1] if not usable_high(d)
                ],
            },
        )

    if valid_counts[pivot] < PARAMETERS["macd_dead_cross"]["min_valid_bars"]:
        return _result(reason="insufficient_macd_history")
    current = peak_at(pivot)
    evidence = {"candidate_peak_date": days[pivot], "confirmation_session": days[i]}
    if current is None:
        return missing_peak(pivot)
    if not current:
        return _check(False, days[i], evidence)
    for k in range(
        pivot - PARAMETERS["macd_bearish_divergence"]["min_peak_gap_sessions"],
        max(1, pivot - span - 1),
        -1,
    ):
        previous = peak_at(k)
        if previous is None:
            return missing_peak(k)
        if previous:
            first, second = bars[days[k]], bars[days[pivot]]
            first_dif, _, second_dif, _, source = _macd_pair(first, second)
            evidence.update(
                first_peak_date=days[k],
                second_peak_date=days[pivot],
                first_high=first["high"],
                second_high=second["high"],
                first_dif=first_dif,
                second_dif=second_dif,
                first_valid_bars=valid_counts[k],
                second_valid_bars=valid_counts[pivot],
                peak_gap_sessions=pivot - k,
                indicator_source=source,
            )
            if valid_counts[k] < PARAMETERS["macd_dead_cross"]["min_valid_bars"]:
                return _result(reason="insufficient_macd_history", evidence=evidence)
            if first_dif is None or second_dif is None:
                return _result(reason="missing_peak_macd", evidence=evidence)
            qualifies = second["high"] > first["high"] and second_dif < first_dif
            return _check(qualifies, days[i], evidence, trigger=days[pivot])
    if pivot - span < 2:
        return _result(reason="insufficient_peak_history", evidence=evidence)
    return _check(False, days[i], evidence)


def _daily(days, i, bars, valid_counts):
    day = days[i]
    now = bars.get(day)
    if not _valid(now):
        return _unavailable("no_valid_bar")
    pct = now["pct_chg"]
    prior = [bars.get(d) for d in days[max(0, i - 5) : i]]
    volume = {
        "volume": now["volume"],
        "stock_return_pct": pct,
        "prior_volumes": [{"date": r["date"], "volume": r["volume"]} for r in prior if _valid(r)],
    }
    thresholds = PARAMETERS["high_volume_drop"]
    if len(volume["prior_volumes"]) == thresholds["prior_sessions"] and pct is not None:
        avg = sum(r["volume"] for r in prior) / 5
        ratio = now["volume"] / avg
        volume.update(prior_volume_mean=avg, volume_ratio=ratio)
        spike = _check(
            pct <= thresholds["return_max_pct"] and ratio >= thresholds["volume_ratio_min"],
            day,
            volume,
        )
    else:
        spike = _result(
            reason="missing_stock_return" if pct is None else "insufficient_volume_history",
            evidence=volume,
        )
    recent = [bars.get(d) for d in days[max(0, i - 4) : i + 1]]
    returns = [
        {"date": r["date"], "return_pct": r["pct_chg"]}
        for r in recent
        if _valid(r) and r["pct_chg"] is not None
    ]
    trend = {"daily_returns": returns}
    counts = PARAMETERS["persistent_decline"]
    if len(returns) == counts["sessions"]:
        negative = sum(r["return_pct"] < 0 for r in returns)
        compound = (math.prod(1 + r["return_pct"] / 100 for r in returns) - 1) * 100
        trend.update(negative_days=negative, cumulative_return_pct=compound)
        decline = _check(
            negative >= counts["negative_days_min"]
            and round(compound, 8) <= counts["cumulative_max_pct"],
            day,
            trend,
        )
    else:
        decline = _result(reason="insufficient_return_history", evidence=trend)

    prev = prior[-1] if prior else None
    prev_dif, prev_dea, dif, dea, source = _macd_pair(prev, now)
    pair = {
        "dif": dif,
        "dea": dea,
        "indicator_source": source,
        "valid_bars": valid_counts[i],
        "prev_date": days[i - 1] if i else None,
        "prev_dif": prev_dif,
        "prev_dea": prev_dea,
    }
    if not i or valid_counts[i - 1] < PARAMETERS["macd_dead_cross"]["min_valid_bars"]:
        dead = _result(reason="insufficient_macd_history", evidence=pair)
    elif _valid(prev) and all(pair[k] is not None for k in ("dif", "dea", "prev_dif", "prev_dea")):
        dead = _check(pair["prev_dif"] >= pair["prev_dea"] and pair["dif"] < pair["dea"], day, pair)
    else:
        dead = _result(reason="missing_macd_pair", evidence=pair)

    return {
        "high_volume_drop": spike,
        "persistent_decline": decline,
        "macd_dead_cross": dead,
        "macd_bearish_divergence": _divergence(days, bars, i, valid_counts),
    }


def _comparison(day, pct, code, name, benchmarks, missing_reason=None):
    index = benchmarks.get(code, {}).get(day) or {}
    bret = index.get("pct_chg")
    diff = round(pct - bret, 8) if None not in (pct, bret) else None
    evidence = {
        "stock_return_pct": pct,
        "benchmark_return_pct": bret,
        "relative_pp": diff,
        "benchmark_code": code,
        "benchmark_date": index.get("date"),
    }
    checks = (
        "market_up_stock_down",
        "market_up_stock_down_significant",
        "relative_underperformance",
    )
    if diff is None or missing_reason:
        reason = missing_reason or (
            "missing_stock_return" if pct is None else "missing_benchmark_return"
        )
        flags = {key: _result(reason=reason, evidence=evidence) for key in checks}
    else:
        lag = diff <= PARAMETERS["relative_underperformance"]["stock_minus_benchmark_max_pp"]
        opposite = pct < 0 < bret
        flags = {
            key: _check(hit, day, evidence)
            for key, hit in zip(checks, (opposite, opposite and lag, lag))
        }
    return {
        "code": code,
        "name": name,
        "date": index.get("date"),
        "source": index.get("source"),
        "close": index.get("close"),
        "return_pct": bret,
        "stock_return_pct": pct,
        "relative_pp": diff,
        "signals": flags,
    }


def _mark_new(flags, prev):
    for key, signal in flags.items():
        if signal["status"] != "triggered":
            continue
        if key in ("macd_dead_cross", "macd_bearish_divergence"):
            signal["new_trigger"] = True
        else:
            before = prev[key]["status"] if prev else "unavailable"
            signal["new_trigger"] = None if before == "unavailable" else before != "triggered"


def _history(conn, secid, start, end):
    columns = (
        "open",
        "high",
        "low",
        "close",
        "pct_chg",
        "volume",
        "amount",
        "macd_diff",
        "macd_dea",
    )
    records = conn.execute(
        "SELECT b.date,b.open,b.high,b.low,b.close,b.pct_chg,b.volume,b.amount,b.source,"
        "i.macd_diff,i.macd_dea,i.source AS indicator_source FROM bars b LEFT JOIN indicators i "
        "ON b.secid=i.secid AND b.date=i.date WHERE b.secid=? AND b.date BETWEEN ? AND ?",
        (secid, start, end),
    )
    bars = {}
    for record in records:
        row = dict(record)
        for key in columns:
            row[key] = _number(row[key])
        bars[row["date"]] = row
    valid = sorted((r for r in bars.values() if _valid(r)), key=lambda r: r["date"])
    if len(valid) >= PARAMETERS["macd_dead_cross"]["min_valid_bars"]:
        computed = compute_frame(pd.DataFrame(valid))
        for row, dif, dea in zip(valid, computed.macd_diff, computed.macd_dea):
            row["computed_dif"], row["computed_dea"] = _number(dif), _number(dea)
    return bars


def _evaluate(conn, raw, day, days, latest, benchmarks):
    secid, reason = _symbol(raw)
    symbol = (
        conn.execute("SELECT name,board FROM symbols WHERE secid=?", (secid,)).fetchone()
        if secid
        else None
    )
    board = (symbol["board"] or board_of(secid)) if symbol else None
    benchmark_code, benchmark_name = BENCHMARKS.get(board, (None, None))

    def comparisons(d, pct=None, why=None):
        return {
            "market": _comparison(d, pct, benchmark_code, benchmark_name, benchmarks, why),
            "shanghai": _comparison(d, pct, "000001", "上证指数", benchmarks, why),
        }

    out = {
        "requested_symbol": raw,
        "requested_date": day,
        "secid": secid,
        "symbol": dm_of(secid) if secid else None,
        "name": symbol["name"] if symbol else None,
        "board": board,
        "data_date": None,
        "reason": None,
        "bar": None,
        "stock_return_pct": None,
        "relative_pp": None,
        "recent": [],
    }
    if not symbol:
        out["reason"] = reason or "unknown_symbol"
        out["signals"] = _unavailable(out["reason"])
        out["benchmarks"] = comparisons(day, why=out["reason"])
        out["benchmark"] = out["benchmarks"]["market"]
        return out
    bars = _history(conn, secid, days[0], day) if days else {}
    out["data_date"] = conn.execute(
        "SELECT MAX(date) FROM bars WHERE secid=? AND date<=? AND volume>0 AND close>0",
        (secid, day),
    ).fetchone()[0]
    if day not in days:
        reason = (
            "market_data_not_yet_available"
            if latest and day > latest
            else "no_market_data_for_date"
        )
    elif not _valid(bars.get(day)):
        reason = "no_valid_bar"
    recent = []
    valid_counts = []
    count = 0
    for d in days:
        count += bool(_valid(bars.get(d)))
        valid_counts.append(count)
    previous = {}
    for i in range(max(0, len(days) - 6), len(days)):
        d = days[i]
        bar = bars.get(d)
        pct = bar["pct_chg"] if _valid(bar) else None
        comparison = comparisons(d, pct, None if _valid(bar) else "no_valid_bar")
        for name, obj in comparison.items():
            _mark_new(obj["signals"], previous.get(name))
            previous[name] = obj["signals"]
        flags = _daily(days, i, bars, valid_counts)
        _mark_new(flags, previous.get("stock"))
        previous["stock"] = flags
        flags.update(comparison["market"]["signals"])
        recent.append({"date": d, "signals": flags, "benchmarks": comparison})
    out["recent"] = recent[-5:]
    out["reason"] = reason
    out["signals"] = _unavailable(reason) if reason else recent[-1]["signals"]
    out["benchmarks"] = comparisons(day, why=reason) if reason else recent[-1]["benchmarks"]
    out["benchmark"] = out["benchmarks"]["market"]
    if not reason:
        bar = bars[day]
        out["bar"] = {
            k: bar[k]
            for k in ("date", "open", "high", "low", "close", "volume", "amount", "source")
        }
        out["stock_return_pct"] = bar["pct_chg"]
        out["relative_pp"] = out["signals"]["relative_underperformance"]["evidence"]["relative_pp"]
    return out


def risk_report(store, day, symbols, adjust="f"):
    try:
        if not day or Date.fromisoformat(day).isoformat() != day:
            raise ValueError()
    except (TypeError, ValueError):
        raise InvalidRequest("date must be YYYY-MM-DD") from None
    if not symbols or not symbols.strip():
        raise InvalidRequest("symbols is required")
    inputs = [part.strip() for part in symbols.split(",")]
    if len(inputs) > MAX_SYMBOLS or len(symbols) > MAX_SYMBOLS * 32:
        raise InvalidRequest("at most 100 symbols, at most 3200 characters in symbols")
    conn = store.conn
    conn.execute("SAVEPOINT watchlist_read")
    try:
        latest = conn.execute("SELECT MAX(date) FROM trading_days").fetchone()[0]
        days = [
            r[0]
            for r in conn.execute(
                "SELECT date FROM trading_days WHERE date<=? ORDER BY date DESC LIMIT ?",
                (day, HISTORY),
            )
        ][::-1]
        benchmarks = {}
        if (
            days
            and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='benchmark_bars'"
            ).fetchone()
        ):
            for row in conn.execute(
                "SELECT date,code,close,pct_chg,source FROM benchmark_bars WHERE date BETWEEN ? AND ?",
                (days[-6] if len(days) >= 6 else days[0], day),
            ):
                row = dict(row)
                row["close"] = _number(row["close"])
                row["pct_chg"] = _number(row["pct_chg"])
                benchmarks.setdefault(row["code"], {})[row["date"]] = row
        evaluated = {}
        rows = []
        for raw in inputs:
            secid, reason = _symbol(raw)
            key = secid or reason
            if key not in evaluated:
                evaluated[key] = _evaluate(conn, raw, day, days, latest, benchmarks)
            rows.append(dict(evaluated[key], requested_symbol=raw))
        return {
            "rule_version": RULE_VERSION,
            "parameters": PARAMETERS,
            "requested_date": day,
            "latest_market_date": latest,
            "recent_trade_dates": days[-5:],
            "max_symbols": MAX_SYMBOLS,
            "data_source": "local SQLite: bars, indicators, trading_days, benchmark_bars",
            "configured_kline_adjust": adjust,
            "rows": rows,
        }
    finally:
        conn.execute("RELEASE watchlist_read")
