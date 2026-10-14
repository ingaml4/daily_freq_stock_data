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
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from .mairui import BAR_FIELDS, INDICATOR_MAP

log = logging.getLogger(__name__)

# 表结构版本号：改 SCHEMA / 迁移逻辑就 +1（init_schema 靠它决定要不要重建）
SCHEMA_VERSION = 7

DAILY_SIGNALS_VIEW = """CREATE VIEW IF NOT EXISTS daily_signals_named AS
SELECT
    *,
    RTRIM(
        CASE WHEN s_vol_spike        THEN 'vol_spike,'        ELSE '' END ||
        CASE WHEN s_macd_golden      THEN 'macd_golden,'      ELSE '' END ||
        CASE WHEN s_kdj_golden       THEN 'kdj_golden,'       ELSE '' END ||
        CASE WHEN s_boll_break_lower THEN 'boll_break_lower,' ELSE '' END ||
        CASE WHEN s_boll_lower       THEN 'boll_lower,'       ELSE '' END ||
        CASE WHEN s_ma_golden        THEN 'ma_golden,'        ELSE '' END ||
        CASE WHEN s_macd_dead        THEN 'macd_dead,'        ELSE '' END ||
        CASE WHEN s_kdj_dead         THEN 'kdj_dead,'         ELSE '' END ||
        CASE WHEN s_ma_dead          THEN 'ma_dead,'          ELSE '' END ||
        CASE WHEN s_boll_upper       THEN 'boll_upper,'       ELSE '' END ||
        CASE WHEN s_boll_break_upper THEN 'boll_break_upper,' ELSE '' END ||
        CASE WHEN s_box_range        THEN 'box_range,'         ELSE '' END ||
        CASE WHEN s_low_doji         THEN 'low_doji,'          ELSE '' END ||
        CASE WHEN s_reversal         THEN 'reversal,'          ELSE '' END,
        ','
    ) AS signals
FROM daily_signals;
"""

SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS symbols (
    secid      TEXT PRIMARY KEY,
    code       TEXT NOT NULL,
    name       TEXT,
    market     INTEGER,
    board      TEXT,
    is_st      INTEGER DEFAULT 0,
    float_shares REAL,
    listed_date  TEXT,
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

-- 日频技术指标：一只票一个交易日一行（MACD / KDJ / BOLL / MA 分别由不同接口写入，
-- 因此每一列的更新是"只动自己那几列"，不会把别的指标冲掉）
CREATE TABLE IF NOT EXISTS indicators (
    secid       TEXT NOT NULL,
    date        TEXT NOT NULL,
    macd_diff   REAL,
    macd_dea    REAL,
    macd_bar    REAL,
    kdj_k       REAL,
    kdj_d       REAL,
    kdj_j       REAL,
    boll_upper  REAL,
    boll_mid    REAL,
    boll_lower  REAL,
    ma5         REAL,
    ma10        REAL,
    ma20        REAL,
    ma60        REAL,
    source      TEXT,
    PRIMARY KEY (secid, date)
);

CREATE INDEX IF NOT EXISTS idx_indicators_date ON indicators(date);

-- 每日信号缓存：一行 = 一只股票一个交易日，把"行情 + 放量指标 + 技术指标 + 全部信号标记"
-- 都摊平在一行里（宽表）。扫描/信号都是本地计算，结果写这里，之后查询/回看不用重算；
-- 两个写入者（放量扫描、指标信号）各写自己那几列，靠 COALESCE 合并，互不覆盖。
CREATE TABLE IF NOT EXISTS daily_signals (
    secid              TEXT NOT NULL,
    date               TEXT NOT NULL,
    -- 行情
    close              REAL,
    pct_chg            REAL,
    volume             REAL,
    amount             REAL,
    turnover           REAL,
    -- 放量指标
    vol_ratio_5        REAL,
    vol_ratio_10       REAL,
    vol_ratio_20       REAL,
    vol_ratio_60       REAL,
    vol_z60            REAL,
    spot_volume_ratio  REAL,
    vol_signal         TEXT,
    vol_score          REAL,
    -- 技术指标数值
    macd_diff          REAL,
    macd_dea           REAL,
    macd_bar           REAL,
    kdj_k              REAL,
    kdj_d              REAL,
    kdj_j              REAL,
    boll_upper         REAL,
    boll_mid           REAL,
    boll_lower         REAL,
    boll_pos           REAL,
    ma5                REAL,
    ma10               REAL,
    ma20               REAL,
    ma60               REAL,
    -- 箱体震荡（做 T 备选池）指标
    box_high           REAL,
    box_low            REAL,
    box_width          REAL,
    box_pos            REAL,
    box_net_chg        REAL,
    box_ma_gap         REAL,
    box_avg_turnover   REAL,
    box_turnover_cv    REAL,
    box_avg_amplitude  REAL,
    box_extreme_days   INTEGER,
    box_days           INTEGER,
    -- 低位缩量十字星
    doji_body          REAL,
    doji_stop_pos      REAL,
    doji_upper         REAL,
    doji_lower         REAL,
    doji_pos           REAL,
    doji_drawdown      REAL,
    doji_vol_ratio     REAL,
    rev_score          REAL,
    rev_signals        TEXT,
    -- 信号标记（1=当日命中，0=未命中，NULL=这次没算）
    s_vol_spike        INTEGER,
    s_macd_golden      INTEGER,
    s_kdj_golden       INTEGER,
    s_boll_break_lower INTEGER,
    s_box_range        INTEGER,
    s_low_doji         INTEGER,
    s_reversal         INTEGER,
    s_macd_dead        INTEGER,
    s_kdj_dead         INTEGER,
    s_ma_golden        INTEGER,
    s_ma_dead          INTEGER,
    s_boll_lower       INTEGER,
    s_boll_upper       INTEGER,
    s_boll_break_upper INTEGER,
    computed_at        TEXT,
    PRIMARY KEY (secid, date)
);

CREATE INDEX IF NOT EXISTS idx_daily_signals_date ON daily_signals(date);

"""
    + DAILY_SIGNALS_VIEW
    + """

CREATE TABLE IF NOT EXISTS trading_days (
    date TEXT PRIMARY KEY
);

-- 键值小表：目前存"数据版本"计数器（网页端靠它判断页面缓存是否过期）
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
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
"""
)

BAR_INSERT = (
    "INSERT OR REPLACE INTO bars "
    "(secid, date, open, high, low, close, volume, amount, amplitude, pct_chg, chg, "
    " turnover, float_mv, volume_ratio, source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# 名称/流通股本等字段可能这次没抓到：用 COALESCE 保留库里已有的值，
# 避免"只刷新行情、没带名称"把股票名字冲成空串。
SYMBOL_INSERT = (
    "INSERT INTO symbols "
    "(secid, code, name, market, board, is_st, float_shares, listed_date, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(secid) DO UPDATE SET "
    "  code = excluded.code, "
    "  name = CASE WHEN COALESCE(excluded.name, '') = '' THEN symbols.name "
    "              ELSE excluded.name END, "
    "  market = COALESCE(excluded.market, symbols.market), "
    "  board = COALESCE(excluded.board, symbols.board), "
    "  is_st = MAX(COALESCE(excluded.is_st, 0), COALESCE(symbols.is_st, 0)), "
    "  float_shares = COALESCE(excluded.float_shares, symbols.float_shares), "
    "  listed_date = COALESCE(excluded.listed_date, symbols.listed_date), "
    "  updated_at = excluded.updated_at"
)

# 老库升级：新版本新增的列（CREATE TABLE IF NOT EXISTS 不会补列）
SYMBOL_MIGRATIONS = (
    ("float_shares", "ALTER TABLE symbols ADD COLUMN float_shares REAL"),
    ("listed_date", "ALTER TABLE symbols ADD COLUMN listed_date TEXT"),
)

# 指标名 -> 该指标落库的列（与 mairui.INDICATOR_MAP 的取值一一对应）
INDICATOR_COLUMNS: Dict[str, tuple] = {
    kind: tuple(mapping.values()) for kind, mapping in INDICATOR_MAP.items()
}

INDICATOR_ALL_COLUMNS: tuple = tuple(
    column for columns in INDICATOR_COLUMNS.values() for column in columns
)

# daily_signals 的可写列（顺序即 DDL 顺序；数值列之外还有 s_* 标记与 vol_signal/computed_at）
DAILY_SIGNAL_COLUMNS: tuple = (
    "close",
    "pct_chg",
    "volume",
    "amount",
    "turnover",
    "vol_ratio_5",
    "vol_ratio_10",
    "vol_ratio_20",
    "vol_ratio_60",
    "vol_z60",
    "spot_volume_ratio",
    "vol_signal",
    "vol_score",
    "macd_diff",
    "macd_dea",
    "macd_bar",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "boll_upper",
    "boll_mid",
    "boll_lower",
    "boll_pos",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "box_high",
    "box_low",
    "box_width",
    "box_pos",
    "box_net_chg",
    "box_ma_gap",
    "box_avg_turnover",
    "box_turnover_cv",
    "box_avg_amplitude",
    "box_extreme_days",
    "box_days",
    "doji_body",
    "doji_stop_pos",
    "doji_upper",
    "doji_lower",
    "doji_pos",
    "doji_drawdown",
    "doji_vol_ratio",
    "rev_score",
    "rev_signals",
    "s_vol_spike",
    "s_macd_golden",
    "s_kdj_golden",
    "s_boll_break_lower",
    "s_box_range",
    "s_low_doji",
    "s_reversal",
    "s_macd_dead",
    "s_kdj_dead",
    "s_ma_golden",
    "s_ma_dead",
    "s_boll_lower",
    "s_boll_upper",
    "s_boll_break_upper",
    "computed_at",
)

# 信号标记列（0/1）
SIGNAL_FLAGS: tuple = tuple(c for c in DAILY_SIGNAL_COLUMNS if c.startswith("s_"))

# 四个"主信号"：突然放量 / MACD 金叉 / KDJ 金叉 / BOLL 下轨突破
PRIMARY_SIGNALS: Dict[str, str] = {
    "s_vol_spike": "突然放量",
    "s_macd_golden": "MACD金叉",
    "s_kdj_golden": "KDJ金叉",
    "s_boll_break_lower": "BOLL下轨突破",
}


def _now() -> str:
    from .tzutil import now_str

    return now_str()


def _db_value(value: Any) -> Any:
    """把 pandas/numpy 的标量（含 NaN/NaT）转成 SQLite 能存的值。"""
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:  # pragma: no cover - 极少见
            pass
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if (isinstance(value, float) and value != value) else float(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _bar_tuple(row: Dict[str, Any]) -> tuple:
    return tuple(row.get(field) for field in BAR_FIELDS)


class Store:
    """行情仓库。

    ``EMSCAN_SQLITE_SYNC`` 可以覆盖 SQLite 的落盘策略（默认 ``NORMAL``）：
    行情数据全部能从接口重建，所以在"磁盘 fsync 很慢"的环境（CI/容器/共享盘）里
    把它设成 ``OFF`` 会让建库和写入快一个数量级（代价是断电可能丢掉最后几条写入）。
    """

    def __init__(self, path: str | Path, synchronous: Optional[str] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        chosen = str(synchronous or os.environ.get("EMSCAN_SQLITE_SYNC") or "NORMAL").upper()
        self.synchronous = (
            chosen if chosen in ("OFF", "0", "NORMAL", "FULL", "1", "2", "3") else "NORMAL"
        )

    # ------------------------------------------------------------------ 连接
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            timeout = float(os.environ.get("EMSCAN_SQLITE_TIMEOUT", 30.0) or 30.0)
            self._conn = sqlite3.connect(str(self.path), timeout=timeout)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(f"PRAGMA synchronous={self.synchronous}")
        return self._conn

    def init_schema(self) -> "Store":
        # 表结构没变就跳过建表：少一次 DDL 解析/落盘，多个命令并发（cron + 手动）时也更省锁
        if self._schema_version() == SCHEMA_VERSION:
            return self
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()
        return self

    def _schema_version(self) -> int:
        row = self.conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def _migrate(self) -> None:
        """给老版本建的库补列（幂等，重复跑无副作用）。"""
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(symbols)")}
        for column, sql in SYMBOL_MIGRATIONS:
            if column not in existing:
                log.info("行情库升级：symbols 表新增列 %s", column)
                self.conn.execute(sql)
        # daily_signals 也会随版本加列：表里有、代码里没有的都补上（ALTER 幂等）
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(daily_signals)")}
        for column in DAILY_SIGNAL_COLUMNS:
            if column in have:
                continue
            kind = "INTEGER" if column.startswith("s_") or column == "box_days" else "REAL"
            if column in ("vol_signal", "computed_at"):
                kind = "TEXT"
            log.info("行情库升级：daily_signals 表新增列 %s", column)
            self.conn.execute(f"ALTER TABLE daily_signals ADD COLUMN {column} {kind}")
        # 视图要跟着新列走：VIEW 是 SELECT *，但 signals 文本列要重新拼
        self.conn.execute("DROP VIEW IF EXISTS daily_signals_named")
        self.conn.execute(DAILY_SIGNALS_VIEW)

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
                    row.get("float_shares"),
                    row.get("listed_date"),
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

    # ------------------------------------------------------ 每日信号缓存（宽表）
    def upsert_daily_signals(self, rows: Iterable[Dict[str, Any]]) -> int:
        """写每日信号缓存：``(secid, date)`` 一行，包含行情/放量/技术指标/信号标记。

        放量扫描和指标信号是两个写入者，各自只写自己算出来的列，
        其余列用 ``COALESCE`` 保留原值 —— 所以两边的结果能拼在同一行里，重复跑也不打架。
        """
        names = ("secid", "date") + DAILY_SIGNAL_COLUMNS
        placeholders = ", ".join("?" for _ in names)
        updates = ", ".join(
            f"{name} = COALESCE(excluded.{name}, daily_signals.{name})" for name in names[2:]
        )
        sql = (
            f"INSERT INTO daily_signals ({', '.join(names)}) VALUES ({placeholders}) "
            f"ON CONFLICT(secid, date) DO UPDATE SET {updates}"
        )
        payload = []
        bad = 0
        stamp = _now()  # 统一时间戳：网页端的"数据版本"靠它判断缓存是否过期
        for row in rows:
            secid, date = row.get("secid"), row.get("date")
            day = "" if date is None else str(date).replace("/", "-").strip()
            # 只接受 YYYY-MM-DD：挡住"日期缺失后被 pandas 变成 nan"这类脏数据
            if not secid or len(day) != 10 or day[4] != "-" or day[:4].isdigit() is False:
                bad += 1
                continue
            values = [_db_value(row.get(c)) for c in DAILY_SIGNAL_COLUMNS]
            if values[-1] is None:  # computed_at 由写入者负责，缺省用当前时间兜底
                values[-1] = stamp
            payload.append(tuple([str(secid), day] + values))
        if bad:
            log.warning("跳过 %s 行日期不规范的每日信号（期望 YYYY-MM-DD）", bad)
        if not payload:
            return 0
        self.conn.executemany(sql, payload)
        self.conn.commit()
        self.bump_data_version()
        return len(payload)

    def bump_data_version(self) -> int:
        """把"数据版本"计数器 +1（每次写入每日信号缓存都调用）。

        网页端用 ``data_version()`` 判断自己缓存的那几页是否需要重算 ——
        比"看时间戳"精确（同一秒内多次写入也不会漏掉）。
        """
        cur = self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('data_version', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
        )
        self.conn.commit()
        row = self.conn.execute("SELECT value FROM meta WHERE key='data_version'").fetchone()
        return int(row["value"]) if row else int(cur.lastrowid or 1)

    def data_version(self) -> str:
        """当前数据版本（没有就是 "0"）。"""
        row = self.conn.execute("SELECT value FROM meta WHERE key='data_version'").fetchone()
        return str(row["value"]) if row and row["value"] is not None else "0"

    def load_daily_signals(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        secids: Optional[Sequence[str]] = None,
        only: Optional[Sequence[str]] = None,
        any_of: Optional[Sequence[str]] = None,
        min_count: int = 0,
        named: bool = True,
        limit: Optional[int] = None,
        join_symbols: bool = False,
    ) -> pd.DataFrame:
        """读每日信号缓存。

        ``only`` 给一组标记列名（如 ``s_macd_golden``）表示"必须命中这些"，
        ``min_count`` 表示"至少命中几个信号"。``named=True`` 时会带上视图算出来的
        ``signals`` 文本列（逗号分隔的英文规则名）。
        """
        table = "daily_signals_named" if named else "daily_signals"
        if join_symbols:
            sql = (
                f"SELECT d.*, s.code, s.name, s.board, s.is_st FROM {table} d "
                "LEFT JOIN symbols s ON s.secid = d.secid WHERE 1=1"
            )
        else:
            sql = f"SELECT d.* FROM {table} d WHERE 1=1"
        params: List[Any] = []
        if start:
            sql += " AND d.date >= ?"
            params.append(str(start))
        if end:
            sql += " AND d.date <= ?"
            params.append(str(end))
        if secids:
            sql += f" AND d.secid IN ({','.join('?' for _ in secids)})"
            params.extend(list(secids))
        for flag in only or []:
            if flag not in SIGNAL_FLAGS:
                raise ValueError(f"未知信号标记：{flag!r}（可选 {', '.join(SIGNAL_FLAGS)}）")
            sql += f" AND COALESCE(d.{flag}, 0) = 1"
        if any_of:
            for flag in any_of:
                if flag not in SIGNAL_FLAGS:
                    raise ValueError(f"未知信号标记：{flag!r}（可选 {', '.join(SIGNAL_FLAGS)}）")
            sql += " AND (" + " OR ".join(f"COALESCE(d.{flag}, 0) = 1" for flag in any_of) + ")"
        if min_count and min_count > 0:
            total = " + ".join(f"COALESCE(d.{flag}, 0)" for flag in SIGNAL_FLAGS)
            sql += f" AND ({total}) >= ?"
            params.append(int(min_count))
        sql += " ORDER BY d.date DESC, d.secid"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return pd.read_sql_query(sql, self.conn, params=params)

    # -------------------------------------------------------- 技术指标写入
    def upsert_indicators(self, kind: str, rows: Iterable[Dict[str, Any]]) -> int:
        """写入某一类指标（macd/kdj/boll/ma），只更新这一类对应的列。

        用 ``COALESCE(excluded.x, indicators.x)`` 兜住"这次没给值"的情况，
        所以多类指标可以分多次写进同一行，互不覆盖。
        """
        columns = INDICATOR_COLUMNS.get(str(kind).lower())
        if not columns:
            raise ValueError(f"未知的指标类型：{kind!r}（可选 {sorted(INDICATOR_COLUMNS)}）")
        names = ("secid", "date") + tuple(columns) + ("source",)
        placeholders = ", ".join("?" for _ in names)
        updates = ", ".join(
            f"{name} = COALESCE(excluded.{name}, indicators.{name})" for name in names[2:]
        )
        sql = (
            f"INSERT INTO indicators ({', '.join(names)}) VALUES ({placeholders}) "
            f"ON CONFLICT(secid, date) DO UPDATE SET {updates}"
        )
        payload = []
        for row in rows:
            if not row.get("secid") or not row.get("date"):
                continue
            payload.append(
                tuple(
                    [row.get("secid"), row.get("date")]
                    + [row.get(name) for name in columns]
                    + [row.get("source", "indicator")]
                )
            )
        if not payload:
            return 0
        self.conn.executemany(sql, payload)
        self.conn.commit()
        return len(payload)

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

    def bar_dates_by_secid(self, since: Optional[str] = None) -> Dict[str, set]:
        """每只股票已有哪些交易日（用于和交易日历比对找缺口）。"""
        return self._dates_by_secid("bars", since=since)

    def indicator_dates_by_secid(
        self, kind: Optional[str] = None, since: Optional[str] = None
    ) -> Dict[str, set]:
        """每只股票已有哪些指标日期；给了 ``kind`` 就只算该指标已落库的行。"""
        condition = None
        if kind:
            columns = INDICATOR_COLUMNS.get(str(kind).lower())
            if not columns:
                raise ValueError(f"未知的指标类型：{kind!r}")
            condition = " AND (" + " OR ".join(f"{c} IS NOT NULL" for c in columns) + ")"
        return self._dates_by_secid("indicators", since=since, condition=condition)

    def _dates_by_secid(
        self, table: str, since: Optional[str] = None, condition: Optional[str] = None
    ) -> Dict[str, set]:
        if table not in ("bars", "indicators"):
            raise ValueError(f"不支持的表：{table}")
        sql = f"SELECT secid, date FROM {table} WHERE 1=1"
        params: List[Any] = []
        if since:
            sql += " AND date >= ?"
            params.append(str(since))
        if condition:
            sql += condition
        out: Dict[str, set] = {}
        for row in self.conn.execute(sql, params):
            out.setdefault(row["secid"], set()).add(row["date"])
        return out

    def symbols_df(self, boards: Optional[Sequence[str]] = None) -> pd.DataFrame:
        sql = (
            "SELECT secid, code, name, market, board, is_st, float_shares, listed_date "
            "FROM symbols"
        )
        params: List[Any] = []
        if boards:
            placeholders = ",".join("?" for _ in boards)
            sql += f" WHERE board IN ({placeholders})"
            params.extend(boards)
        return pd.read_sql_query(sql, self.conn, params=params)

    def float_shares_map(self) -> Dict[str, float]:
        """流通股本映射（股），用于在本地推算历史换手率与流通市值。"""
        rows = self.conn.execute(
            "SELECT secid, float_shares FROM symbols WHERE float_shares IS NOT NULL"
        ).fetchall()
        return {r["secid"]: float(r["float_shares"]) for r in rows if r["float_shares"]}

    def listed_dates(self) -> Dict[str, str]:
        """上市日期映射（有《股票基础信息》数据时才知道），用于剔除"上市前"的伪缺口。"""
        rows = self.conn.execute(
            "SELECT secid, listed_date FROM symbols WHERE listed_date IS NOT NULL"
        ).fetchall()
        return {r["secid"]: r["listed_date"] for r in rows if r["listed_date"]}

    def secids_missing_shares(self) -> List[str]:
        """还没有流通股本的股票（用于按需补抓《股票基础信息》）。"""
        rows = self.conn.execute(
            "SELECT secid FROM symbols WHERE float_shares IS NULL OR float_shares <= 0"
        ).fetchall()
        return [r["secid"] for r in rows]

    def secids(self, boards: Optional[Sequence[str]] = None) -> List[str]:
        frame = self.symbols_df(boards)
        return [str(s) for s in frame["secid"].tolist()]

    def load_bars(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        secids: Optional[Sequence[str]] = None,
        columns: Optional[Sequence[str]] = None,
    ) -> pd.DataFrame:
        select = self._select_list(columns)
        sql = f"SELECT {select} FROM bars WHERE 1=1"
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

    def load_recent_bars(
        self,
        calendar_days: int = 140,
        columns: Optional[Sequence[str]] = None,
    ) -> pd.DataFrame:
        """加载最近 N 个自然日的 bars。

        扫描只需要"列 + 最近 60 个交易日"，所以可以按需裁剪（见 ``columns``）：
        行数少一半、列少三分之一，1GB 内存的小机器也跑得动。
        """
        latest = self.latest_date()
        if latest is None:
            return self.load_bars(columns=columns).head(0)
        start = (
            dt.datetime.strptime(latest, "%Y-%m-%d") - dt.timedelta(days=int(calendar_days))
        ).strftime("%Y-%m-%d")
        return self.load_bars(start=start, columns=columns)

    # 允许外部指定的列（白名单，避免 SQL 注入）
    _BAR_COLUMNS = (
        "secid",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "amplitude",
        "pct_chg",
        "chg",
        "turnover",
        "float_mv",
        "volume_ratio",
        "source",
    )

    @classmethod
    def _select_list(cls, columns: Optional[Sequence[str]]) -> str:
        if not columns:
            return ", ".join(cls._BAR_COLUMNS)
        picked = [c for c in columns if c in cls._BAR_COLUMNS]
        if "secid" not in picked:
            picked.insert(0, "secid")
        if "date" not in picked:
            picked.insert(1, "date")
        return ", ".join(picked)

    # ------------------------------------------------------------------ 指标
    _INDICATOR_COLUMNS_ALL = ("secid", "date") + INDICATOR_ALL_COLUMNS + ("source",)

    @classmethod
    def _indicator_select_list(cls, columns: Optional[Sequence[str]]) -> str:
        allowed = ("secid", "date") + INDICATOR_ALL_COLUMNS + ("source",)
        if not columns:
            return ", ".join(allowed)
        picked = [c for c in columns if c in allowed]
        if "secid" not in picked:
            picked.insert(0, "secid")
        if "date" not in picked:
            picked.insert(1, "date")
        return ", ".join(picked)

    def load_indicators(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        secids: Optional[Sequence[str]] = None,
        columns: Optional[Sequence[str]] = None,
    ) -> pd.DataFrame:
        select = self._indicator_select_list(columns)
        sql = f"SELECT {select} FROM indicators WHERE 1=1"
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

    def latest_indicator_date(self) -> Optional[str]:
        row = self.conn.execute("SELECT MAX(date) AS d FROM indicators").fetchone()
        return row["d"] if row and row["d"] else None

    def latest_signal_date(self) -> Optional[str]:
        row = self.conn.execute("SELECT MAX(date) AS d FROM daily_signals").fetchone()
        return row["d"] if row and row["d"] else None

    def indicator_stats(self) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT secid) AS s, MIN(date) AS a, MAX(date) AS b "
            "FROM indicators"
        ).fetchone()
        return {
            "indicator_rows": row["n"],
            "indicator_symbols": row["s"],
            "indicator_first_date": row["a"],
            "indicator_last_date": row["b"],
        }

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
        out.update(self.indicator_stats())
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT date) AS d, MAX(date) AS b FROM daily_signals"
        ).fetchone()
        out["signal_rows"] = row["n"]
        out["signal_days"] = row["d"]
        out["signal_last_date"] = row["b"]
        try:
            out["size_mb"] = round(self.path.stat().st_size / 1048576, 2)
        except OSError:  # pragma: no cover
            out["size_mb"] = None
        return out
