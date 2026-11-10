"""逐帧留存麦蕊盘口和接收时点，复用网页进程已有的本地交易日历。"""

import json
import logging
import sqlite3
import time
from contextlib import closing
from datetime import date

log = logging.getLogger("emscan")


def current_session(store, day):
    if date.fromisoformat(day).weekday() >= 5:
        return False
    if store.conn.execute("SELECT 1 FROM trading_days WHERE date=?", (day,)).fetchone():
        return True
    snapshot = json.loads(store.get_meta("mairui_calendar:" + day[:4]) or "{}")
    if snapshot.get("expires", 0) > time.time():
        return day in snapshot["days"]
    return None


def record_frames(path, items):
    rows = [
        (
            item["symbol"],
            item["received_at"],
            item["source_endpoint"],
            json.dumps({**item, "cached": False}, ensure_ascii=False, allow_nan=False),
        )
        for item in items
        if item.get("upstream_requested")
    ]
    if not rows:
        return "no_upstream_frame"
    try:
        with closing(sqlite3.connect(path, timeout=2)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS auction_frames("
                "symbol TEXT NOT NULL, received_at TEXT NOT NULL, endpoint TEXT NOT NULL, "
                "frame_json TEXT NOT NULL, PRIMARY KEY(symbol,received_at,endpoint))"
            )
            conn.executemany("INSERT OR IGNORE INTO auction_frames VALUES (?,?,?,?)", rows)
    except sqlite3.Error:
        log.error("盘口留存失败")
        return "unavailable"
    return "stored"
