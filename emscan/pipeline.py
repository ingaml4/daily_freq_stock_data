"""自愈式数据同步：脚本自己判断"缺了什么"，然后把缺的全补上。

设计目标：部署到服务器后，使用者只需要配一个 licence，之后
``python3 -m emscan run``（cron / systemd 每天跑一次）就够了：

1. :func:`sync_universe`  —— 拉《股票列表》刷新 symbols（新上市/退市的自动进出）
2. :func:`sync_float_shares` —— 补流通股本（历史换手率、流通市值靠它推算）
3. :func:`sync_bars`     —— 用交易日历做标尺，逐只比对本地日线，缺哪天就从哪天补；
                            从没抓过的股票直接全历史回补
4. :func:`sync_spot`     —— 落当日快照（停牌不落库；非交易日/数据未更新直接跳过）
5. :func:`sync_indicators` —— 同样按交易日历比对，缺哪天补哪天（MACD/KDJ/BOLL/MA）

所有同步都是幂等的：主键 ``(secid, date)`` + ``INSERT OR REPLACE``，
同一天重复跑不会写坏数据，也不会重复请求已经拿到的日期。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from time import time_ns
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .history import history_window
from .indicators import compute_and_store
from .mairui import MairuiClient, MairuiError, board_of, spot_trade_date
from .request_cache import Receipts, annual_calendar
from .store import Store
from .tzutil import now_hm
from .tzutil import shift
from .tzutil import today as tz_today

log = logging.getLogger("emscan")


@dataclass
class SyncStats:
    """一次自愈同步的账本（写日志、记 runs 表都用它）。"""

    symbols: int = 0
    shares_filled: int = 0
    bars_written: int = 0
    bars_symbols: int = 0
    indicators_written: int = 0
    trade_date: Optional[str] = None
    skipped: Optional[str] = None
    errors: List[Tuple[str, str]] = field(default_factory=list)

    def note(self) -> str:
        parts = [
            f"symbols={self.symbols}",
            f"shares={self.shares_filled}",
            f"bars={self.bars_written}",
            f"indicators={self.indicators_written}",
        ]
        if self.trade_date:
            parts.append(f"trade_date={self.trade_date}")
        if self.skipped:
            parts.append(f"skip={self.skipped}")
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


# --------------------------------------------------------------------------- 交易日历
def resolve_trade_days(
    client: MairuiClient,
    start: str,
    end: Optional[str] = None,
    cfg=None,
    store=None,
    known_closed=None,
) -> List[str]:
    """取 ``[start, end]`` 之间的交易日（来自《交易日历》）。

    日历接口挂了也不致命：退回"周一~周五"，最多是多查几天没有数据的日子
    （落库时自然为空，不会写脏数据）。
    """
    end = end or tz_today()
    if start > end:
        return []
    years = list(range(int(start[:4]), int(end[:4]) + 1))
    days: List[str] = []
    try:
        if store is None:
            days = client.fetch_trade_calendar_span(years)
        else:
            for year in years:
                official, complete = annual_calendar(store, client, year)
                if (
                    complete
                    and known_closed is not None
                    and official[0] < end < official[-1]
                    and end not in official
                ):
                    known_closed.add(end)
                days.extend(
                    official or _weekdays(max(start, f"{year}-01-01"), min(end, f"{year}-12-31"))
                )
    except Exception as exc:  # pragma: no cover - 日历异常时兜底
        log.warning("交易日历获取失败（%s），改用「周一~周五」估算交易日", exc)
    if not days:
        days = _weekdays(start, end)
    return [day for day in days if start <= day <= end]


def _weekdays(start: str, end: str) -> List[str]:
    import datetime as dt

    out: List[str] = []
    cursor = dt.datetime.strptime(start, "%Y-%m-%d")
    stop = dt.datetime.strptime(end, "%Y-%m-%d")
    while cursor <= stop:
        if cursor.weekday() < 5:
            out.append(cursor.strftime("%Y-%m-%d"))
        cursor += dt.timedelta(days=1)
    return out


# --------------------------------------------------------------------------- 逐项同步
def sync_universe(store: Store, client: MairuiClient, cfg, stats: SyncStats) -> List[str]:
    """刷新股票列表，返回参与处理的 secid 列表（按配置的板块过滤）。"""
    boards = set(cfg.get("universe.boards") or [])
    rows = client.fetch_universe()
    if not rows:
        raise MairuiError("股票列表为空：证书可能无效，或接口有变")
    rows = [row for row in rows if not boards or row["board"] in boards]
    store.upsert_symbols(rows)
    stats.symbols = len(rows)
    log.info("股票列表：%s 只（板块 %s）", len(rows), ",".join(sorted(boards)) or "全部")
    return [row["secid"] for row in rows]


def sync_float_shares(
    store: Store,
    client: MairuiClient,
    cfg,
    stats: SyncStats,
    secids: Sequence[str],
    limit: Optional[int] = None,
) -> int:
    """补齐缺失的流通股本：优先用《股票基础信息》，失败就靠每日快照慢慢补。"""
    have = store.float_shares_map()
    missing = [secid for secid in secids if secid not in have]
    if limit:
        missing = missing[: int(limit)]
    if not missing:
        return 0
    if not cfg.get("mairui.fetch_instrument"):
        log.info(
            "有 %s 只股票还没有流通股本：本次跳过（%s），"
            "每天 update 会从快照的流通市值反推；想立即补齐就设 mairui.fetch_instrument: true",
            len(missing),
            "mairui.fetch_instrument = false",
        )
        return 0
    batch = max(1, int(cfg.get("mairui.backfill_batch", 400) or 400))
    filled = 0
    log.info("补流通股本（《股票基础信息》）：%s 只，每只 1 次请求", len(missing))
    for start in range(0, len(missing), batch):
        chunk = missing[start : start + batch]
        results, errors = client.fetch_instrument_many(chunk)
        stats.errors.extend(errors)
        rows = [
            {
                "secid": secid,
                "code": secid.split(".", 1)[1],
                "name": info.get("name") or "",
                "market": int(secid.split(".", 1)[0]),
                "board": board_of(secid),
                "float_shares": info.get("float_shares"),
                "listed_date": info.get("listed_date"),
            }
            for secid, info in results.items()
        ]
        filled += store.upsert_symbols(rows)
    stats.shares_filled = filled
    log.info("流通股本补齐：%s 只", filled)
    return filled


def _plan_jobs(
    secids: Sequence[str],
    expected: Sequence[str],
    have_by_secid: Dict[str, set],
    full_if_missing: bool = True,
    listed: Optional[Dict[str, str]] = None,
    full_beg: str = "0",
) -> Dict[str, List[str]]:
    """算出"每只股票要从哪一天开始补"，返回 ``{beg: [secid, ...]}``。

    * 本地一条都没有（新股 / 首次运行）-> ``full_beg``（``"0"``=全历史，
      也可以给 ``2023-09-01`` 这种起点，避免一次拉三十年）
    * 中间或尾部有缺口 -> 从最早缺失的那天开始拉（顺带覆盖缺口之后的所有日期）
    * 知道上市日期的，上市之前的日期不算缺口（否则新股每天都会被重复请求）
    """
    jobs: Dict[str, List[str]] = {}
    expected_set = list(expected)
    listed = listed or {}
    for secid in secids:
        dates = have_by_secid.get(secid)
        if not dates:
            if full_if_missing:
                jobs.setdefault(str(full_beg or "0"), []).append(secid)
            continue
        floor = listed.get(secid) or ""
        missing = [day for day in expected_set if day >= floor and day not in dates]
        if not missing:
            continue
        jobs.setdefault(min(missing), []).append(secid)
    return jobs


def _job_order(jobs: Dict[str, List[str]]) -> List[str]:
    """先补最近缺口（日期键），最后才做"从零开始的全历史"，让日报尽快可用。"""
    return sorted(jobs, reverse=True)


def _cap_jobs(jobs: Dict[str, List[str]], max_symbols: int) -> Tuple[Dict[str, List[str]], int]:
    """限制单次运行处理多少只（``0`` = 不限），返回 (裁剪后的任务, 剩余只数)。"""
    if not max_symbols or max_symbols <= 0:
        return jobs, 0
    out: Dict[str, List[str]] = {}
    budget = int(max_symbols)
    left = 0
    for beg in _job_order(jobs):
        group = jobs[beg]
        if budget <= 0:
            left += len(group)
            continue
        if len(group) > budget:
            out[beg] = group[:budget]
            left += len(group) - budget
            budget = 0
            continue
        out[beg] = group
        budget -= len(group)
    return out, left


def sync_bars(
    store: Store,
    client: MairuiClient,
    cfg,
    stats: SyncStats,
    secids: Sequence[str],
    expected: Sequence[str],
    max_symbols: int = 0,
    full_beg: str = "0",
    progress=None,
    use_cache=False,
) -> int:
    """按交易日历补齐日线缺口；没抓过的股票从 ``full_beg`` 开始回补。"""
    if not secids or not expected:
        return 0
    window_start = min(expected)
    end = max(expected)
    cache = Receipts(store) if use_cache else None
    # 接口成功返回但某日仍空，可稍后重试。真实错误不写空回执。
    deferred = cache.absent(window_start, end) if cache else {}
    have = store.bar_dates_by_secid(since=window_start)
    listed = store.listed_dates()
    for secid, days in deferred.items():
        have.setdefault(secid, set()).update(days)
    jobs = _plan_jobs(secids, expected, have, listed=listed, full_beg=str(full_beg or "0"))
    todo = sum(len(group) for group in jobs.values())
    if not todo:
        log.info("日线数据完整（%s 只 / 窗口 %s 起），无需回补", len(secids), window_start)
        return 0
    if cache and max_symbols > 0 and todo > max_symbols:
        attempts = dict(
            store.conn.execute(
                "SELECT substr(key,13),value FROM meta WHERE key LIKE 'bar_attempt:%'"
            )
        )
        tasks = [(beg, s) for beg in _job_order(jobs) for s in jobs[beg]]
        tasks.sort(key=lambda task: attempts.get(task[1], ""))
        left = len(tasks) - max_symbols
        jobs = {}
        for beg, secid in tasks[:max_symbols]:
            jobs.setdefault(beg, []).append(secid)
    else:
        jobs, left = _cap_jobs(jobs, max_symbols)
    todo = sum(len(group) for group in jobs.values())
    log.info(
        "日线缺口：%s 只需要回补（全历史 %s 只 / 断点续传 %s 只）%s",
        todo,
        len(jobs.get("0", [])),
        todo - len(jobs.get("0", [])),
        f"，超出本次上限的 {left} 只留到下次" if left else "",
    )
    batch = max(1, int(cfg.get("mairui.backfill_batch", 400) or 400))
    shares = store.float_shares_map()
    written = 0
    # 控制回执大小；长历史回补只记录日常核对窗口内的空日。
    floor_recent = shift(end, -60)
    history_start, _ = history_window(cfg, end)
    recent = {day for day in expected if day >= floor_recent}
    short_from = shift(tz_today(), -2)
    for beg in _job_order(jobs):
        group = jobs[beg]
        for start in range(0, len(group), batch):
            chunk = list(group[start : start + batch])
            if cache and max_symbols > 0:
                stamp = str(time_ns())
                with store.conn:
                    store.conn.executemany(
                        "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                        [("bar_attempt:" + s, stamp) for s in chunk],
                    )
            results, errors = client.fetch_kline_many(chunk, beg=beg, end=end, float_shares=shares)
            stats.errors.extend(errors)
            rows = [
                row
                for result in results.values()
                for row in result
                if (row.get("close") or 0) > 0
                and (row.get("volume") or 0) > 0
                and row["date"] <= end
            ]
            written += store.upsert_bars(rows, refresh_days=False, missing_only=True)
            short, older = [], []
            for secid, bars in results.items():
                covered = have.get(secid, set()) | {r["date"] for r in bars}
                floor = max(listed.get(secid) or "", beg)
                for day in recent - covered:
                    if day >= floor:
                        (short if day >= short_from else older).append((secid, day))
            if cache:
                cache.put("bar-missing", short, 3600)
                cache.put("bar-missing", older, 7 * 86400)
            if beg <= history_start:
                with store.conn:
                    store.conn.executemany(
                        "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                        [("history_checked:" + secid, end) for secid in results],
                    )
            stats.bars_symbols += len(results)
            del results, rows
            if progress:
                progress(min(start + batch, len(group)), len(group), beg)
    store.refresh_trading_days()
    stats.bars_written += written
    log.info("日线回补完成：%s 行", written)
    return written


def sync_spot(
    store: Store,
    client: MairuiClient,
    cfg,
    stats: SyncStats,
    secids: Sequence[str],
    force: bool = False,
) -> Optional[str]:
    """落当日快照；非交易日或数据未更新时返回 ``None``（不落库、不扫描）。"""
    from .readiness import code_version

    today = tz_today()

    pool = set(secids)
    cache = Receipts(store) if pool and now_hm() >= "17:15" else None
    verified = {}
    if cache:
        if force:
            cache.clear_spot(today, pool)
        else:
            verified = {s: kind for s, kind in cache.final_spot(today).items() if s in pool}
    needed = [s for s in dict.fromkeys(secids) if s not in verified]
    spot = client.fetch_spot(needed) if needed or not pool else []
    if pool:
        wanted = set(needed)
        spot = [r for r in spot if r["secid"] in wanted]
    trade_date = (spot_trade_date(spot) or today) if force else today
    dated = {r["secid"]: r for r in spot if (spot_trade_date([r]) or r.get("date")) == trade_date}
    store.upsert_symbols(
        [
            {"secid": s, "code": s.split(".", 1)[1], "float_shares": row["float_shares"]}
            for s, row in dated.items()
            if row.get("float_shares")
        ]
    )
    bars = [
        dict(r, date=trade_date, source="spot")
        for r in dated.values()
        if (r.get("volume") or 0) > 0 and (r.get("close") or 0) > 0
    ]
    written = store.upsert_bars(bars)
    stats.bars_written += written
    active = {r["secid"] for r in bars} | {s for s, k in verified.items() if k == "spot-priced"}
    # 日期可靠、成交量明确为零才能计入无成交。整批全零没有市场已更新的证据。
    zero = {s for s, r in dated.items() if r.get("volume") == 0} if active else set()
    if cache and trade_date == today:
        cache.put("spot-priced", ((r["secid"], today) for r in bars), 2 * 3600)
        cache.put("spot-zero", ((s, today) for s in zero), 3600)
    no_trade = zero | {s for s, k in verified.items() if k == "spot-zero"}
    missing = pool - active - no_trade
    if missing:
        stats.errors.append(("spot", f"{len(missing)} 只快照未确认"))
    store.record_run(
        "spot_coverage",
        trade_date=trade_date,
        symbols=len(pool),
        params={
            "universe": len(pool),
            "active": len(active),
            "no_trade": len(no_trade),
            "reported": len(set(dated) | set(verified)),
            "cached": len(verified),
            "source": "dated_quotes",
            "code_version": code_version(),
        },
    )
    if not active:
        stats.skipped = "非交易日或无当日有效成交"
        return None
    stats.trade_date = trade_date
    log.info(
        "盘后行情 %s：有效成交 %d、无成交 %d、未确认 %d、复用 %d",
        trade_date,
        len(active),
        len(no_trade),
        len(missing),
        len(verified),
    )
    return trade_date


def sync_indicators(
    store: Store,
    client: MairuiClient,
    cfg,
    stats: SyncStats,
    secids: Sequence[str],
    expected: Sequence[str],
    kinds: Sequence[str],
    max_symbols: int = 0,
    progress=None,
) -> int:
    """补齐技术指标缺口。

    ``indicators.source`` 决定数据从哪来（默认 ``local``，本地按日线算，零接口请求）：

    * ``local`` —— 本地计算（毫秒级、不占配额、可随时重算历史）；
    * ``api``   —— 拉麦蕊的指标接口（一只股票一种指标一次请求，慢但口径以券商为准）。
    """
    if not secids or not expected or not kinds:
        return 0
    source = str(cfg.get("indicators.source", "local") or "local").strip().lower()
    window_start = min(expected)
    if source == "local":
        # 全都本地算（零接口请求）：新股/从没算过的整段补，老股票只重算最近几天（够判金叉）
        have = store.indicator_dates_by_secid(since=window_start)
        fresh = [secid for secid in secids if not have.get(secid)]
        known = [secid for secid in secids if have.get(secid)]
        keep = max(2, int(cfg.get("indicators.local_days", 5) or 5))
        log.info(
            "技术指标：本地计算（%s 只：%s 只补全窗口、%s 只只重算最近 %s 天；零接口请求）",
            len(secids),
            len(fresh),
            len(known),
            keep,
        )
        chunk = int(cfg.get("indicators.local_chunk", 400) or 400)
        written = 0
        if fresh:
            written += compute_and_store(
                store, fresh, cfg=cfg, dates=expected, kinds=kinds, chunk=chunk, progress=progress
            )
        if known:
            written += compute_and_store(
                store,
                known,
                cfg=cfg,
                dates=list(expected)[-keep:],
                kinds=kinds,
                chunk=chunk,
                progress=progress,
            )
        stats.indicators_written += written
        log.info("技术指标（本地）完成：%s 行", written)
        return written

    limit = int(cfg.get("indicators.limit", 0) or 0)
    batch = max(1, int(cfg.get("mairui.backfill_batch", 400) or 400))
    written = 0
    for kind in kinds:
        have = store.indicator_dates_by_secid(kind, since=window_start)
        jobs = _plan_jobs(secids, expected, have, listed=store.listed_dates())
        todo = sum(len(group) for group in jobs.values())
        if not todo:
            log.info("指标 %s 数据完整（%s 只 / %s 起），无需回补", kind, len(secids), window_start)
            continue
        jobs, left = _cap_jobs(jobs, max_symbols)
        todo = sum(len(group) for group in jobs.values())
        log.info(
            "指标 %s 缺口：%s 只（全历史 %s 只 / 断点续传 %s 只）%s",
            kind,
            todo,
            len(jobs.get("0", [])),
            todo - len(jobs.get("0", [])),
            f"，超出本次上限的 {left} 只留到下次" if left else "",
        )
        for beg in _job_order(jobs):
            group = jobs[beg]
            # 从没抓过的股票只需要最近 limit 条（够判断金叉/触轨）；补缺口时不设上限
            request_limit = limit if (beg == "0" and limit) else None
            for start in range(0, len(group), batch):
                chunk = list(group[start : start + batch])
                by_kind, errors = client.fetch_indicators_many(
                    chunk, kinds=[kind], beg=beg, limit=request_limit
                )
                stats.errors.extend(errors)
                for rows in by_kind.values():
                    written += store.upsert_indicators(kind, rows)
                if progress:
                    progress(min(start + batch, len(group)), len(group), f"{kind}@{beg}")
    stats.indicators_written += written
    log.info("技术指标回补完成：%s 行", written)
    return written


def reconcile_window(cfg, days: Optional[int] = None) -> Tuple[str, str]:
    """自愈比对的默认窗口：``[今天 - N 个自然日, 今天]``。"""
    span = int(days if days is not None else cfg.get("reconcile_calendar_days", 45) or 45)
    end = tz_today()
    return shift(end, -span), end
