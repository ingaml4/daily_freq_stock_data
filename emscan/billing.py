"""读取 DeepSeek 最近一次尚未恢复的 402 事件，供本机飞书提醒去重。"""

import json
import sqlite3

from .config import Config
from .tzutil import now_str


KEY = "deepseek_402_event"


def record_402(store):
    event = now_str() + "/deepseek-402"
    with store.conn:
        store.conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (KEY, event))


def record_recovery(store):
    with store.conn:
        store.conn.execute("DELETE FROM meta WHERE key=?", (KEY,))


def main():
    path = Config.load().path("store.path").resolve().as_uri()
    with sqlite3.connect(path + "?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
    print(json.dumps({"event": row[0] if row else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
