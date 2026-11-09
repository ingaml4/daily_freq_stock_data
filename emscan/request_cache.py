"""按数据库隔离的运行互斥和有时限的麦蕊请求回执。"""

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path


@contextmanager
def daily_lock(db_path):
    """独立 SQLite 写锁只用于同一数据库的 run 互斥，进程退出自动释放。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path) + ".run-lock", timeout=0)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            yield False
        else:
            yield True
    finally:
        conn.close()


class Receipts:
    def __init__(self, store):
        self.conn = store.conn
        self.ts = int(time.time())
        with self.conn:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS sync_receipts ("
                "kind TEXT NOT NULL, secid TEXT NOT NULL, day TEXT NOT NULL,"
                "expires INTEGER NOT NULL, PRIMARY KEY(kind,secid,day))"
            )
            self.conn.execute("DELETE FROM sync_receipts WHERE expires<=?", (self.ts,))

    def put(self, kind, pairs, ttl):
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO sync_receipts VALUES (?,?,?,?)",
                ((kind, secid, day, self.ts + ttl) for secid, day in pairs),
            )

    def absent(self, start, end):
        rows = self.conn.execute(
            "SELECT secid, day FROM sync_receipts WHERE kind IN ('bar-missing','spot-zero') "
            "AND day BETWEEN ? AND ? AND expires>?",
            (start, end, self.ts),
        )
        result = {}
        for secid, day in rows:
            result.setdefault(secid, set()).add(day)
        return result

    def clear_spot(self, day, secids):
        with self.conn:
            self.conn.executemany(
                "DELETE FROM sync_receipts WHERE day=? AND secid=? "
                "AND kind IN ('spot-priced','spot-zero')",
                ((day, s) for s in secids),
            )

    def final_spot(self, day):
        rows = self.conn.execute(
            "SELECT r.secid,r.kind FROM sync_receipts r LEFT JOIN bars b ON b.date=r.day "
            "AND b.secid=r.secid WHERE r.day=? AND r.expires>? AND "
            "((r.kind='spot-zero' AND (b.secid IS NULL OR coalesce(b.volume,0)<=0 OR coalesce(b.close,0)<=0)) "
            "OR (r.kind='spot-priced' AND b.source='spot' AND b.close>0 AND b.volume>0))",
            (day, self.ts),
        )
        return dict(rows)


def annual_calendar(store, client, year):
    """只缓存带年初年底及连续交易日覆盖证据的整年响应，七天刷新。"""
    key = f"mairui_calendar:{year}"
    ts = int(time.time())
    saved = json.loads(store.get_meta(key) or "{}")
    if saved.get("expires", 0) > ts:
        return saved["days"], True
    days = sorted(
        set(d for d in client.fetch_trade_calendar_span([year]) if d.startswith(str(year) + "-"))
    )
    parsed = [date.fromisoformat(d) for d in days]
    complete = bool(
        220 <= len(days) <= 255
        and days[0][5:7] == "01"
        and days[-1][5:7] == "12"
        and all((b - a).days <= 15 for a, b in zip(parsed, parsed[1:]))
    )
    if complete:
        store.set_meta(key, json.dumps({"expires": ts + 7 * 86400, "days": days}))
    return days, complete
