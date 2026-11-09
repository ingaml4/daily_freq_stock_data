"""有效日线收盘的简单移动平均。历史回补只更新给定日期范围的均线及其交叉标记。"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import date
from math import fsum, isclose

from .config import Config
from .store import Store


PERIODS = (5, 10, 20, 60)
COLUMNS = tuple(f"ma{n}" for n in PERIODS)


def _previous(conn, secid, before, count):
    return list(
        conn.execute(
            "SELECT date,close,source FROM bars WHERE secid=? AND date<? AND volume>0 AND close>0 "
            "ORDER BY date DESC LIMIT ?",
            (secid, before, count),
        )
    )[::-1]


def _mean(window, period):
    return (
        fsum(row["close"] for row in window[-period:]) / period if len(window) >= period else None
    )


def evidence(conn, secid, day, close, adjust, reason=None):
    """给出截至 day 含当日的窗口。缺少目标日有效行情时禁止回退。"""
    window = []
    if secid and close is not None and not reason:
        current = conn.execute(
            "SELECT date,close,source FROM bars WHERE secid=? AND date=? AND volume>0 AND close>0",
            (secid, day),
        ).fetchone()
        if current is not None:
            window = _previous(conn, secid, day, max(PERIODS) - 1) + [current]
    result = {}
    for n in PERIODS:
        sample = window[-n:]
        ma = _mean(sample, n)
        position = None
        if ma is not None:
            position = (
                "at" if isclose(close, ma, rel_tol=1e-12) else "above" if close > ma else "below"
            )
        sources = sorted({row["source"] or "unknown" for row in sample})
        result[f"ma{n}"] = {
            "status": "available" if ma is not None else "unavailable",
            "reason": reason or (None if ma is not None else "insufficient_ma_history"),
            "date": day if sample else None,
            "requested_date": day,
            "period": n,
            "sample_count": len(sample),
            "window_start": sample[0]["date"] if sample else None,
            "value": ma,
            "close": close,
            "position": position,
            "distance_pct": (close / ma - 1) * 100 if ma is not None else None,
            "source": "sma_from_local_bars",
            "bar_sources": sources,
            "price_basis": "as_stored",
            "configured_kline_adjust": adjust,
        }
    return result


def _rows(conn, secid, start, end, targets, prev_market):
    # 附带上一根，以便区间首日的交叉信号也能与上一市场交易日核对。
    prior = _previous(conn, secid, start, max(PERIODS))
    window = deque(prior, maxlen=max(PERIODS))
    prev_values = [_mean(prior, n) for n in (5, 10)]
    prev_day = prior[-1]["date"] if prior else None
    for bar in conn.execute(
        "SELECT date,close FROM bars WHERE secid=? AND date BETWEEN ? AND ? "
        "AND volume>0 AND close>0 ORDER BY date",
        (secid, start, end),
    ):
        day = bar["date"]
        window.append(bar)
        sample = list(window)
        values = [_mean(sample, n) for n in PERIODS]
        if day in targets:
            comparable = prev_day == prev_market.get(day) and None not in [
                *values[:2],
                *prev_values,
            ]
            golden = int(comparable and values[0] > values[1] and prev_values[0] <= prev_values[1])
            dead = int(comparable and values[0] < values[1] and prev_values[0] >= prev_values[1])
            yield (secid, day, *values), (secid, day, *values, golden, dead)
        prev_day, prev_values = day, values[:2]


def write_days(store, secids, days, update_cache=False, batch=64):
    """按股票分批、按日期写，批内两表原子更新；已有其他指标和信号列原样保留。"""
    targets = set(days)
    if not targets or not secids:
        return 0
    start, end = min(targets), max(targets)
    conn = store.conn
    market = [
        r[0]
        for r in conn.execute(
            "SELECT date FROM trading_days WHERE date >= COALESCE("
            "(SELECT MAX(date) FROM trading_days WHERE date<?),?) AND date<=? ORDER BY date",
            (start, start, end),
        )
    ]
    prev_market = dict(zip(market[1:], market))
    written = 0
    for offset in range(0, len(secids), batch):
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            values, cache = [], []
            for secid in secids[offset : offset + batch]:
                for indicator, signal in _rows(conn, secid, start, end, targets, prev_market):
                    values.append(indicator)
                    if update_cache:
                        cache.append(signal)
            cols = ",".join(COLUMNS)
            assignments = ",".join(f"{c}=excluded.{c}" for c in COLUMNS)
            changed = " OR ".join(f"indicators.{c} IS NOT excluded.{c}" for c in COLUMNS)
            conn.executemany(
                f"INSERT INTO indicators(secid,date,{cols},source) VALUES(?,?,?,?,?,?,'local') "
                f"ON CONFLICT(secid,date) DO UPDATE SET {assignments} WHERE {changed}",
                values,
            )
            if cache:
                cols = (*COLUMNS, "s_ma_golden", "s_ma_dead")
                assignments = ",".join(f"{c}=?{idx}" for idx, c in enumerate(cols, 3))
                changed = " OR ".join(f"{c} IS NOT ?{idx}" for idx, c in enumerate(cols, 3))
                cur = conn.executemany(
                    f"UPDATE daily_signals SET {assignments} "
                    f"WHERE secid=?1 AND date=?2 AND ({changed})",
                    cache,
                )
                if cur.rowcount:
                    conn.execute(
                        "INSERT INTO meta(key,value) VALUES('data_version','1') "
                        "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1"
                    )
            written += len(values)
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--secid", help="可选，单股内部代码，例如 1.603986")
    args = parser.parse_args()
    if not 0 <= (args.end - args.start).days <= 365:
        parser.error("起止日期须升序，单次最多 366 个自然日")
    start, end = args.start.isoformat(), args.end.isoformat()
    cfg = Config.load()
    with Store(cfg.path("store.path")).init_schema() as store:
        days = [
            r[0]
            for r in store.conn.execute(
                "SELECT date FROM trading_days WHERE date BETWEEN ? AND ? ORDER BY date",
                (start, end),
            )
        ]
        codes = (
            [args.secid]
            if args.secid
            else [
                r[0]
                for r in store.conn.execute(
                    "SELECT DISTINCT secid FROM bars WHERE date BETWEEN ? AND ?", (start, end)
                )
            ]
        )
        written = write_days(store, codes, days, update_cache=True)
    print(f"{start} ~ {end}，处理有效日线 {written} 根。")


if __name__ == "__main__":
    main()
