"""SQLite 行情仓库：股票列表 + 日线 bars + 交易日 + 运行记录。

设计要点：

* 单文件 SQLite，零外部依赖，随手 `cp` 就能备份/迁移。
* ``bars`` 以 (secid, date) 为主键，天然幂等：同一天重复跑只会覆盖当天那一行，
  历史数据不会被写坏。
* ``trading_days`` 表由 bars 里的日期去重得到，用来判断"上一个交易日"，
  从而发现漏抓的日期并自动回补（不需要额外的交易日历接口）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from .eastmoney import BAR_FIELDS

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    secid      TEXT PRIMARY KEY,
    code       TEXT NOT NULL,
    name       TEXT,
    market     INTEGER,
    board      TEXT,
    is_st      INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS bars (
    secid        TEXT NOT NULL,
    date         TEXT NOT NULL,
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    volume       REAL,
    amount       REAL,
    amplitude    REAL,
    pct_chg      REAL,
    chg          REAL,
    turnover     REAL,
    float_mv     REAL,
    volume_ratio REAL,
    source       TEXT,
    PRIMARY KEY (secid, date)
);

CREATE INDEX IF NOT EXISTS idx_bars_date ON bars(date);

CREATE TABLE IF NOT EXISTS trading_days (
    date TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    trade_date TEXT,
    mode       TEXT,
    symbols    INTEGER,
    scanned    INTEGER,
    hits       INTEGER,
    params     TEXT,
    note       TEXT
);

-- 记录"为了补漏已经回补过"的股票，避免同一天反复回补停牌股
CREATE TABLE IF NOT EXISTS heal_log (
    trade_date TEXT NOT NULL,
    secid      TEXT NOT NULL,
    rows_added INTEGER,
    attempted_at TEXT,
    PRIMARY KEY (trade_date, secid)
);
"""

BAR_INSERT = (
    "INSERT OR REPLACE INTO bars "
    "(secid, date, open, high, low, close, volume, amount, amplitude, pct_chg, chg, "
    " turnover, float_mv, volume_ratio, source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

SYMBOL_INSERT = (
    "INSERT OR REPLACE INTO symbols (secid, code, name, market, board, is_st, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _bar_tuple(row: Dict[str, Any]) -> tuple:
    return tuple(row.get(field) for field in BAR_FIELDS)


class Store:
    """行情仓库。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------ 连接
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.path), timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def init_schema(self) -> "Store":
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ 写入
    def upsert_symbols(self, rows: Iterable[Dict[str, Any]]) -> int:
        ts = _now()
        payload = []
        for row in rows:
            secid = row.get("secid")
            code = row.get("code")
            if not secid or not code:
                continue
            name = str(row.get("name") or "")
            is_st = int(bool(row.get("is_st")) or ("ST" in name.upper()) or ("退" in name))
            payload.append(
                (
                    secid,
                    str(code),
                    name,
                    row.get("market"),
                    row.get("board"),
                    is_st,
                    ts,
                )
            )
        if not payload:
            return 0
        self.conn.executemany(SYMBOL_INSERT, payload)
        self.conn.commit()
        return len(payload)

    def upsert_bars(self, rows: Iterable[Dict[str, Any]], refresh_days: bool = True) -> int:
        payload = []
        for row in rows:
            if not row.get("secid") or not row.get("date"):
                continue
            payload.append(_bar_tuple(row))
        if not payload:
            return 0
        self.conn.executemany(BAR_INSERT, payload)
        self.conn.commit()
        if refresh_days:
            self.refresh_trading_days()
        return len(payload)

    def refresh_trading_days(self) -> int:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO trading_days(date) SELECT DISTINCT date FROM bars"
        )
        self.conn.commit()
        return cur.rowcount

    def record_run(
        self,
        mode: str,
        trade_date: Optional[str] = None,
        symbols: Optional[int] = None,
        scanned: Optional[int] = None,
        hits: Optional[int] = None,
        params: Optional[Dict[str, Any]] = None,
        note: Optional[str] = None,
        started_at: Optional[str] = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, finished_at, trade_date, mode, symbols, scanned, "
            "hits, params, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                started_at or _now(),
                _now(),
                trade_date,
                mode,
                symbols,
                scanned,
                hits,
                json.dumps(params or {}, ensure_ascii=False),
                note,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    # ------------------------------------------------------------------ 查询
    def latest_date(self) -> Optional[str]:
        row = self.conn.execute("SELECT MAX(date) AS d FROM bars").fetchone()
        return row["d"] if row and row["d"] else None

    def trading_day_before(self, date: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT MAX(date) AS d FROM trading_days WHERE date < ?", (date,)
        ).fetchone()
        return row["d"] if row and row["d"] else None

    def recent_trading_days(self, n: int = 10) -> List[str]:
        rows = self.conn.execute(
            "SELECT date FROM trading_days ORDER BY date DESC LIMIT ?", (int(n),)
        ).fetchall()
        return [r["date"] for r in rows][::-1]

    def last_dates(self) -> Dict[str, str]:
        """每只股票本地最新一个交易日的日期。"""
        rows = self.conn.execute("SELECT secid, MAX(date) AS d FROM bars GROUP BY secid").fetchall()
        return {r["secid"]: r["d"] for r in rows}

    # -------------------------------------------------------- 回补记录（heal_log）
    def healed_secids(self, trade_date: str) -> set:
        rows = self.conn.execute(
            "SELECT secid FROM heal_log WHERE trade_date = ?", (trade_date,)
        ).fetchall()
        return {r["secid"] for r in rows}

    def log_heal(self, trade_date: str, entries: Iterable[tuple]) -> int:
        payload = [(trade_date, secid, int(rows_added), _now()) for secid, rows_added in entries]
        if not payload:
            return 0
        self.conn.executemany(
            "INSERT OR REPLACE INTO heal_log (trade_date, secid, rows_added, attempted_at) "
            "VALUES (?, ?, ?, ?)",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def symbols_df(self, boards: Optional[Sequence[str]] = None) -> pd.DataFrame:
        sql = "SELECT secid, code, name, market, board, is_st FROM symbols"
        params: List[Any] = []
        if boards:
            placeholders = ",".join("?" for _ in boards)
            sql += f" WHERE board IN ({placeholders})"
            params.extend(boards)
        return pd.read_sql_query(sql, self.conn, params=params)

    def secids(self, boards: Optional[Sequence[str]] = None) -> List[str]:
        frame = self.symbols_df(boards)
        return [str(s) for s in frame["secid"].tolist()]

    def load_bars(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        secids: Optional[Sequence[str]] = None,
    ) -> pd.DataFrame:
        sql = (
            "SELECT secid, date, open, high, low, close, volume, amount, amplitude, "
            "pct_chg, chg, turnover, float_mv, volume_ratio, source FROM bars WHERE 1=1"
        )
        params: List[Any] = []
        if start:
            sql += " AND date >= ?"
            params.append(start)
        if end:
            sql += " AND date <= ?"
            params.append(end)
        if secids:
            placeholders = ",".join("?" for _ in secids)
            sql += f" AND secid IN ({placeholders})"
            params.extend(list(secids))
        return pd.read_sql_query(sql, self.conn, params=params)

    def load_recent_bars(self, calendar_days: int = 250) -> pd.DataFrame:
        """加载最近 N 个自然日的 bars（扫描时的输入，默认覆盖 60 个交易日窗口）。"""
        latest = self.latest_date()
        if latest is None:
            return self.load_bars().head(0)
        start = (
            dt.datetime.strptime(latest, "%Y-%m-%d") - dt.timedelta(days=int(calendar_days))
        ).strftime("%Y-%m-%d")
        return self.load_bars(start=start)

    def stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"path": str(self.path)}
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT secid) AS s, MIN(date) AS a, MAX(date) AS b "
            "FROM bars"
        ).fetchone()
        out.update(
            {
                "bars": row["n"],
                "symbols_with_bars": row["s"],
                "first_date": row["a"],
                "last_date": row["b"],
            }
        )
        row = self.conn.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()
        out["symbols"] = row["n"]
        row = self.conn.execute("SELECT COUNT(*) AS n FROM trading_days").fetchone()
        out["trading_days"] = row["n"]
        try:
            out["size_mb"] = round(self.path.stat().st_size / 1048576, 2)
        except OSError:  # pragma: no cover
            out["size_mb"] = None
        return out
