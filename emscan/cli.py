"""emscan 命令行入口。

服务器上只需要配好 licence，然后让 cron / systemd / k8s 每天跑一条命令::

    python3 -m emscan run

``run`` 会自己判断"缺了什么"并逐项补齐（幂等，重复跑无副作用）：

    股票列表 → 流通股本 → 日线缺口（含新股全历史）→ 当日行情 → 技术指标缺口
    → 放量扫描 + 指标信号 → 报告（+ 可选推送）

其它常用命令::

    python3 -m emscan check --probe          # 部署后第一步：验证证书/接口/字段映射
    python3 -m emscan sync                   # 只补数据，不出报告（预热/半夜慢跑）
    python3 -m emscan scan --trade-date X    # 用本地数据回看某一天（不联网）
    python3 -m emscan signals                # 单独筛"金叉 / 布林下轨"等指标信号
    python3 -m emscan demo                   # 无网络自检：合成行情跑通全链路
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .config import Config
from .demo import synthesize
from .mairui import (
    MairuiClient,
    MairuiError,
    normalize_indicator_kind,
    spot_trade_date,
)
from .notify import send as send_notify
from .pipeline import (
    SyncStats,
    reconcile_window,
    resolve_trade_days,
    sync_bars,
    sync_float_shares,
    sync_indicators,
    sync_spot,
    sync_universe,
)
from .report import print_console, print_frame, render_table, write_files
from .scan import SCAN_COLUMNS, scan
from .signals import (
    FLAG_LABELS,
    SIGNAL_BAR_COLUMNS,
    SIGNAL_COLUMNS,
    SIGNAL_FORMATS,
    SIGNAL_LABELS,
)
from .signals import build_summary as build_signal_summary
from .signals import detect as detect_signals
from .signals import (
    normalize_rules,
)
from .store import Store
from .tzutil import ensure_env_tz, now_hm, shift
from .tzutil import today as tz_today

log = logging.getLogger("emscan")

# --------------------------------------------------------------------------- 常量
# 四个"主信号"：命令行筛选、缓存标记、报告都用这几个名字
PRIMARY_SIGNAL_ALIASES = {
    "vol_spike": "s_vol_spike",
    "macd_golden": "s_macd_golden",
    "kdj_golden": "s_kdj_golden",
    "boll_break_lower": "s_boll_break_lower",
}

# `hits` 命令的展示列（读的是每日信号缓存 daily_signals）
HITS_COLUMNS = (
    "date",
    "code",
    "name",
    "close",
    "pct_chg",
    "amount_yi",
    "signals",
    "vol_ratio_5",
    "vol_ratio_20",
    "vol_z60",
    "boll_pos",
    "boll_lower",
    "macd_diff",
    "macd_dea",
    "kdj_k",
    "kdj_d",
)

HITS_LABELS = {
    "date": "交易日",
    "code": "代码",
    "name": "名称",
    "close": "收盘价",
    "pct_chg": "涨跌幅%",
    "amount_yi": "成交额(亿)",
    "signals": "命中信号",
    "vol_ratio_5": "量比5",
    "vol_ratio_20": "量比20",
    "vol_z60": "Z60",
    "boll_pos": "布林位置",
    "boll_lower": "布林下轨",
    "macd_diff": "DIFF",
    "macd_dea": "DEA",
    "kdj_k": "K",
    "kdj_d": "D",
}

HITS_FORMATS = {
    "close": "{:.2f}",
    "pct_chg": "{:+.2f}",
    "amount_yi": "{:.2f}",
    "vol_ratio_5": "{:.2f}",
    "vol_ratio_20": "{:.2f}",
    "vol_z60": "{:.1f}",
    "boll_pos": "{:.2f}",
    "boll_lower": "{:.2f}",
    "macd_diff": "{:.3f}",
    "macd_dea": "{:.3f}",
    "kdj_k": "{:.1f}",
    "kdj_d": "{:.1f}",
}


# --------------------------------------------------------------------------- 工具
def setup_logging(cfg: Config, verbose: bool = False) -> Path:
    logs_dir = cfg.path("store.logs_dir")
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "emscan.log"
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(stream)
    root.addHandler(file_handler)
    return log_file


def open_store(cfg: Config) -> Store:
    store = Store(cfg.path("store.path"))
    store.init_schema()
    return store


def _window(cfg: Config, args: argparse.Namespace) -> Tuple[str, str]:
    """自愈比对的日期窗口：``--start`` 优先，否则取最近 N 个自然日。"""
    start = getattr(args, "start", None)
    if start:
        return str(start), tz_today()
    return reconcile_window(cfg, getattr(args, "days", None))


def _limit(args: argparse.Namespace, cfg: Config) -> int:
    """单次运行最多处理多少只股票（0=不限）。试点时用 ``--limit``。"""
    value = getattr(args, "limit", None)
    if value is None:
        value = cfg.get("sync.max_symbols_per_run", 0)
    return int(value or 0)


def _history_start(cfg: Config) -> str:
    """回补起点：``sync.history_start`` 优先，否则按 ``sync.history_years`` 往前推。"""
    explicit = str(cfg.get("sync.history_start") or "").strip()
    if explicit and explicit not in ("0", "00000000"):
        return explicit
    years = int(cfg.get("sync.history_years", 3) or 0)
    if years <= 0:
        return "0"
    return shift(tz_today(), -int(round(365.25 * years)))


def _log_errors(stats: SyncStats, title: str = "请求失败") -> None:
    if stats.errors:
        log.warning("%s %s 次，样例：%s", title, len(stats.errors), stats.errors[0])


def _close(client) -> None:
    """收尾时关掉会话：即使客户端是假的/已经坏了，也不该让整个任务失败。"""
    try:
        client.close()
    except Exception as exc:  # pragma: no cover - 兜底
        log.debug("关闭客户端失败：%s", exc)


def _progress(title: str):
    """简单进度播报：每 5% 打一行，避免日志被淹掉。"""
    state = {"calls": 0}

    def report(done: int, total: int, tag: str = "") -> None:
        state["calls"] += 1
        if total and (done == total or state["calls"] % 20 == 0):
            log.info("  %s %s/%s%s", title, done, total, f"（{tag}）" if tag else "")

    return report


def indicator_kinds(cfg: Config, args: argparse.Namespace) -> List[str]:
    """要处理的指标种类：命令行 ``--kinds`` 优先，其次 ``indicators.kinds``。"""
    raw = getattr(args, "kinds", None)
    if raw:
        values = [part for part in str(raw).replace(" ", "").split(",") if part]
    else:
        values = [str(kind) for kind in (cfg.get("indicators.kinds") or [])]
    picked: List[str] = []
    for value in values:
        try:
            kind = normalize_indicator_kind(value)
        except ValueError as exc:
            log.warning("%s", exc)
            continue
        if kind not in picked:
            picked.append(kind)
    return picked


def _indicators_enabled(cfg: Config, args: argparse.Namespace) -> bool:
    if getattr(args, "skip_indicators", False):
        return False
    return bool(cfg.get("indicators.enabled", True)) and bool(cfg.get("indicators.kinds"))


def _indicators_due(
    cfg: Config, args: argparse.Namespace, expected: Sequence[str], trade_date: Optional[str]
) -> bool:
    """今天该不该刷指标：有当日行情，或者按交易日历今天本来就是交易日。

    后者是为了兜住"快照接口偶发失败/还没更新，但指标已经出来了（15:35 更新）"的情况；
    休市日则一次请求都不发。
    """
    if not _indicators_enabled(cfg, args):
        return False
    return bool(trade_date) or tz_today() in set(expected)


def _minus_unpublished_today(cfg: Config, expected: Sequence[str], what: str) -> List[str]:
    """去掉"今天但还没到发布/落库时间"的那一天，避免白耗接口配额。"""
    today = tz_today()
    days = list(expected)
    if today not in set(days):
        return days
    if what == "indicators":
        publish = str(cfg.get("indicators.publish_time", "15:35") or "15:35")
        if now_hm() >= publish:
            return days
        log.info(
            "现在是 %s，还没到今天指标的发布时间（%s）：本次跳过 %s 的指标，"
            "当天稍晚再跑一次或明天补上",
            now_hm(),
            publish,
            today,
        )
    else:
        log.info("今天 %s 的行情还没落库，本次不按「今天」补缺口（下次运行再补）", today)
    return [day for day in days if day != today]


def _signals_enabled(cfg: Config, args: argparse.Namespace) -> bool:
    return bool(cfg.get("signals.enabled", True)) and not getattr(args, "skip_signals", False)


# --------------------------------------------------------------------------- 同步类命令
def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    store = open_store(cfg)
    info = store.stats()
    log.info("行情库已就绪：%s", info["path"])
    log.info(
        "当前：%s 只股票 / 日线 %s 行（%s ~ %s）/ 技术指标 %s 行",
        info["symbols"],
        info["bars"],
        info["first_date"],
        info["last_date"],
        info.get("indicator_rows", 0),
    )
    store.close()
    return 0


def cmd_universe(cfg: Config, args: argparse.Namespace) -> int:
    """只刷新股票列表（顺带补可用的流通股本）。"""
    store = open_store(cfg)
    client = MairuiClient.from_config(cfg)
    stats = SyncStats()
    secids = sync_universe(store, client, cfg, stats)
    sync_float_shares(store, client, cfg, stats, secids, limit=_limit(args, cfg))
    by_board: Dict[str, int] = {}
    for board, group in store.symbols_df().groupby("board"):
        by_board[str(board)] = len(group)
    for board, count in sorted(by_board.items()):
        log.info("  %-8s %s", board, count)
    log.info("股票列表更新完成：%s", stats.note())
    store.record_run("universe", symbols=len(secids), note=stats.note())
    store.close()
    _close(client)
    return 0


def cmd_backfill(cfg: Config, args: argparse.Namespace) -> int:
    """回补日线：没抓过的股票拉全历史，抓过的按交易日历补缺口。"""
    store = open_store(cfg)
    client = MairuiClient.from_config(cfg)
    started = time.time()
    stats = SyncStats()
    secids = sync_universe(store, client, cfg, stats)
    sync_float_shares(store, client, cfg, stats, secids, limit=_limit(args, cfg))
    start, end = _window(cfg, args)
    if not getattr(args, "start", None):
        start = _history_start(cfg)
    expected = resolve_trade_days(client, start, end, cfg)
    sync_bars(
        store,
        client,
        cfg,
        stats,
        secids,
        expected,
        max_symbols=_limit(args, cfg),
        progress=_progress("日线"),
    )
    _log_errors(stats)
    log.info("回补完成：%s，耗时 %.1f 秒", stats.note(), time.time() - started)
    store.record_run("backfill", symbols=len(secids), note=stats.note())
    store.close()
    _close(client)
    return 0


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    """只做数据自愈（列表 + 流通股本 + 日线 + 当日行情 + 指标），不出报告。"""
    store = open_store(cfg)
    client = MairuiClient.from_config(cfg)
    started = time.time()
    stats = SyncStats()
    secids = sync_universe(store, client, cfg, stats)
    sync_float_shares(store, client, cfg, stats, secids, limit=_limit(args, cfg))
    start, end = _window(cfg, args)
    expected = resolve_trade_days(client, start, end, cfg)
    trade_date = sync_spot(store, client, cfg, stats, secids, force=getattr(args, "force", False))
    bars_expected = (
        _minus_unpublished_today(cfg, expected, "bars") if not trade_date else list(expected)
    )
    sync_bars(
        store,
        client,
        cfg,
        stats,
        secids,
        bars_expected,
        max_symbols=_limit(args, cfg),
        progress=_progress("日线"),
    )
    if _indicators_due(cfg, args, expected, trade_date):
        sync_indicators(
            store,
            client,
            cfg,
            stats,
            secids,
            _minus_unpublished_today(cfg, expected, "indicators"),
            indicator_kinds(cfg, args),
            max_symbols=_limit(args, cfg),
            progress=_progress("指标"),
        )
    _log_errors(stats)
    log.info("数据同步完成：%s，耗时 %.1f 秒", stats.note(), time.time() - started)
    store.record_run(
        "sync",
        trade_date=stats.trade_date or trade_date,
        symbols=len(secids),
        note=stats.note(),
    )
    store.close()
    _close(client)
    return 0


def cmd_update(cfg: Config, args: argparse.Namespace) -> int:
    """只更新当日行情 + 补近期日线缺口（不抓指标、不扫描）。"""
    store = open_store(cfg)
    client = MairuiClient.from_config(cfg)
    stats = SyncStats()
    secids = store.secids(cfg.get("universe.boards"))
    if not secids:
        log.info("symbols 表为空，先拉一次股票列表")
        secids = sync_universe(store, client, cfg, stats)
    start, end = _window(cfg, args)
    expected = resolve_trade_days(client, start, end, cfg)
    sync_bars(
        store,
        client,
        cfg,
        stats,
        secids,
        expected,
        max_symbols=_limit(args, cfg),
        progress=_progress("日线"),
    )
    trade_date = sync_spot(store, client, cfg, stats, secids, force=getattr(args, "force", False))
    _log_errors(stats)
    log.info("增量更新完成：%s", stats.note())
    store.record_run("update", trade_date=trade_date, symbols=len(secids), note=stats.note())
    store.close()
    _close(client)
    return 0


def cmd_indicators(cfg: Config, args: argparse.Namespace) -> int:
    """按交易日历补齐日频技术指标（MACD / KDJ / BOLL / MA）。"""
    store = open_store(cfg)
    source = getattr(args, "source", None)
    if source:  # 命令行临时覆盖配置
        cfg["indicators"]["source"] = source
    client = MairuiClient.from_config(cfg)
    stats = SyncStats()
    kinds = indicator_kinds(cfg, args)
    if not kinds:
        log.error("没有配置任何技术指标：检查 indicators.kinds / --kinds")
        store.close()
        return 1
    secids = store.secids(cfg.get("universe.boards"))
    if not secids:
        log.info("symbols 表为空，先拉一次股票列表")
        secids = sync_universe(store, client, cfg, stats)
    start, end = _window(cfg, args)
    if not getattr(args, "start", None):
        start = _history_start(cfg)
    expected = resolve_trade_days(client, start, end, cfg)
    started = time.time()
    sync_indicators(
        store,
        client,
        cfg,
        stats,
        secids,
        expected,
        kinds,
        max_symbols=_limit(args, cfg),
        progress=_progress("指标"),
    )
    _log_errors(stats)
    log.info("技术指标完成：%s，耗时 %.1f 秒", stats.note(), time.time() - started)
    store.record_run("indicators", symbols=len(secids), note=f"{stats.note()} kinds={kinds}")
    store.close()
    _close(client)
    return 0


# --------------------------------------------------------------------------- 报告类命令
def cmd_scan(cfg: Config, args: argparse.Namespace) -> int:
    store = open_store(cfg)
    trade_date = getattr(args, "trade_date", None) or store.latest_date()
    if not trade_date:
        log.error("本地没有任何行情数据，先执行 backfill 或 run")
        store.close()
        return 1
    calendar_days = int(cfg.get("scan.lookback_calendar_days", 140) or 140)
    bars = store.load_recent_bars(calendar_days=calendar_days, columns=SCAN_COLUMNS)
    symbols = store.symbols_df()
    if trade_date not in set(bars["date"].astype(str)):
        log.error(
            "本地没有 %s 的行情（多半是那天休市，或数据还没更新）。"
            "可先跑 `python3 -m emscan run`，或用 --trade-date 指定有数据的交易日。",
            trade_date,
        )
        store.close()
        return 1
    log.info(
        "本地行情 %s 行（%s 起），开始扫描 %s",
        len(bars),
        bars["date"].min() if len(bars) else "-",
        trade_date,
    )

    result = scan(bars, symbols, cfg, trade_date=trade_date)
    print_console(result, cfg)
    files = write_files(result, cfg)
    for path in files:
        log.info("报告已写出：%s", path)
    cached = _cache_volume_result(store, result)
    if cached:
        log.info("每日信号缓存已更新：%s 行（放量指标 + 命中标记）", cached)
    outcome = send_notify(result, cfg)
    if outcome.get("enabled"):
        log.info("推送结果：%s", outcome)
    store.record_run(
        "scan",
        trade_date=result.trade_date,
        scanned=result.stats.get("scanned"),
        hits=len(result.hits),
        params=result.stats.get("thresholds"),
    )
    store.close()
    return 0


def _cache_volume_result(store: Store, result) -> int:
    """把放量扫描的"全市场指标 + 命中标记"写进每日信号缓存。"""
    frame = getattr(result, "all_metrics", None)
    if frame is None or frame.empty:
        return 0
    return store.upsert_daily_signals(frame.to_dict("records"))


def _cache_signal_result(store: Store, result) -> int:
    """把指标信号的"全市场数值 + 全套信号标记"写进每日信号缓存。"""
    frame = getattr(result, "all_metrics", None)
    if frame is None or frame.empty:
        return 0
    return store.upsert_daily_signals(frame.to_dict("records"))


def cmd_signals(cfg: Config, args: argparse.Namespace) -> int:
    """筛"MACD/KDJ 金叉、布林下轨"等指标信号，出终端表格 + CSV/XLSX。"""
    store = open_store(cfg)
    trade_date = getattr(args, "trade_date", None) or store.latest_indicator_date()
    if not trade_date:
        log.error("本地还没有技术指标数据，先跑 `python3 -m emscan indicators` 或 `run`")
        store.close()
        return 1
    calendar_days = int(cfg.get("signals.lookback_calendar_days", 30) or 30)
    bars = store.load_recent_bars(calendar_days=calendar_days, columns=SIGNAL_BAR_COLUMNS)
    indicators = store.load_indicators(start=shift(trade_date, -calendar_days))
    symbols = store.symbols_df()
    log.info(
        "本地指标 %s 行（%s 起），开始筛选 %s",
        len(indicators),
        indicators["date"].min() if len(indicators) else "-",
        trade_date,
    )
    result = detect_signals(
        bars,
        indicators,
        symbols,
        cfg,
        trade_date=trade_date,
        rules=_split_rules(getattr(args, "rules", None)),
    )
    summary = build_signal_summary(result, cfg)
    print_frame(
        result,
        columns=SIGNAL_COLUMNS,
        labels=SIGNAL_LABELS,
        float_formats=SIGNAL_FORMATS,
        summary=summary,
    )
    files = write_files(result, cfg, prefix="signals", sheet_name="指标信号")
    for path in files:
        log.info("报告已写出：%s", path)
    cached = _cache_signal_result(store, result)
    if cached:
        log.info("每日信号缓存已更新：%s 行（指标数值 + 信号标记）", cached)
    store.record_run(
        "signals",
        trade_date=result.trade_date,
        scanned=result.stats.get("universe"),
        hits=len(result.hits),
        params={"rules": result.stats.get("rules"), "match": result.stats.get("match")},
    )
    store.close()
    return 0


def _split_rules(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    return [part for part in str(raw).replace(" ", "").split(",") if part]


def _resolve_flags(raw: Optional[str]) -> List[str]:
    """把 ``macd_golden,kdj_golden`` 或 ``s_macd_golden`` 解析成缓存里的标记列名。"""
    flags: List[str] = []
    for name in _split_rules(raw) or []:
        key = str(name).strip()
        column = PRIMARY_SIGNAL_ALIASES.get(key) or (
            key if key in PRIMARY_SIGNAL_ALIASES.values() else f"s_{key}"
        )
        if column not in PRIMARY_SIGNAL_ALIASES.values() and not column.startswith("s_"):
            raise ValueError(f"未知信号：{name!r}")
        if column not in flags:
            flags.append(column)
    return flags


def cmd_hits(cfg: Config, args: argparse.Namespace) -> int:
    """查每日信号缓存（daily_signals）——不联网、不重算，秒回。

    * 默认：看最近一个交易日的"四类主信号"名单（放量 / MACD 金叉 / KDJ 金叉 / BOLL 下轨突破）；
    * ``--secid 600519 --days 30``：看单只股票最近 30 个自然日的每日信号；
    * ``--only macd_golden,kdj_golden``：只看同时命中这些信号的股票；
    * ``--date 2026-10-13``：指定某一天。
    """
    store = open_store(cfg)
    days = int(getattr(args, "days", 30) or 30)
    date = getattr(args, "date", None)
    secid_arg = getattr(args, "secid", None)
    secids: Optional[List[str]] = None
    if secid_arg:
        code = str(secid_arg).strip()
        if "." in code:  # 已经是 1.600519 这种内部写法
            secids = [code]
        else:
            row = store.conn.execute("SELECT secid FROM symbols WHERE code = ?", (code,)).fetchone()
            if row is None:
                log.error("股票代码 %s 不在本地股票列表里", code)
                store.close()
                return 1
            secids = [row["secid"]]

    if date:
        start = end = str(date)
    elif secids:
        end = store.latest_signal_date() or tz_today()
        start = shift(end, -days)
    else:
        latest = store.latest_signal_date()
        if not latest:
            log.error("还没有每日信号缓存：先跑 `python3 -m emscan run`（或 scan + signals）")
            store.close()
            return 1
        start = end = latest

    only = _resolve_flags(getattr(args, "only", None))
    any_of: Optional[List[str]] = None
    if not secids and not only:
        # 默认视角：四个主信号里"任一命中"
        any_of = list(PRIMARY_SIGNAL_ALIASES.values())

    frame = store.load_daily_signals(
        start=start,
        end=end,
        secids=secids,
        only=only or None,
        any_of=any_of,
        min_count=int(getattr(args, "min_signals", 0) or 0),
        join_symbols=True,
        limit=int(getattr(args, "limit", 500) or 500),
    )
    store.close()
    if frame.empty:
        print(f"（{start} ~ {end} 没有命中任何信号）")
        return 0

    frame["amount_yi"] = pd.to_numeric(frame.get("amount"), errors="coerce") / 1e8
    if "signals" in frame.columns:
        frame["signals"] = frame["signals"].fillna("")
    selected = [] if secids else (only or any_of or [])
    rule_text = "、".join(FLAG_LABELS.get(flag, flag) for flag in selected)
    summary = f"每日信号缓存 | {start} ~ {end}"
    if secid_arg:
        summary += f" | {secid_arg}"
    summary += f"\n共 {len(frame)} 行"
    if rule_text:
        prefix = "任一命中" if any_of else "同时命中"
        summary += f"；筛选条件：{prefix} {rule_text}"
    table = render_table(
        frame, columns=HITS_COLUMNS, float_formats=HITS_FORMATS, labels=HITS_LABELS
    )
    print(f"{summary}\n\n{table}")

    out = getattr(args, "out", None)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frame.to_csv(index=False), encoding="utf-8-sig")
        print(f"已写出：{path}")
    return 0


# --------------------------------------------------------------------------- 每日入口
def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    """服务器上的唯一入口：自愈同步 + 放量扫描 + 指标信号 + 报告/推送。"""
    store = open_store(cfg)
    client = MairuiClient.from_config(cfg)
    started = time.time()
    stats = SyncStats()
    try:
        secids = sync_universe(store, client, cfg, stats)
        sync_float_shares(store, client, cfg, stats, secids, limit=_limit(args, cfg))
        start, end = _window(cfg, args)
        expected = resolve_trade_days(client, start, end, cfg)
        # 先落当日行情：它是最权威的"今天"（收盘后的快照），也能把今天从缺口里消掉，
        # 避免随后为了补"今天"给全市场各发一次 K 线请求。
        trade_date = sync_spot(
            store, client, cfg, stats, secids, force=getattr(args, "force", False)
        )
        bars_expected = (
            _minus_unpublished_today(cfg, expected, "bars") if not trade_date else list(expected)
        )
        sync_bars(
            store,
            client,
            cfg,
            stats,
            secids,
            bars_expected,
            max_symbols=_limit(args, cfg),
            full_beg=_history_start(cfg),
            progress=_progress("日线"),
        )
        if _indicators_due(cfg, args, expected, trade_date):
            sync_indicators(
                store,
                client,
                cfg,
                stats,
                secids,
                _minus_unpublished_today(cfg, expected, "indicators"),
                indicator_kinds(cfg, args),
                max_symbols=_limit(args, cfg),
                progress=_progress("指标"),
            )
        _log_errors(stats)
        log.info("同步阶段完成：%s，耗时 %.1f 秒", stats.note(), time.time() - started)
        store.record_run(
            "run",
            trade_date=trade_date or stats.trade_date,
            symbols=len(secids),
            note=stats.note(),
        )
    finally:
        store.close()
        _close(client)

    if not stats.trade_date:
        log.info("今天没有可扫描的行情（%s），跳过扫描与信号", stats.skipped or "无当日数据")
        return 0

    args.trade_date = stats.trade_date
    rc = cmd_scan(cfg, args)
    if _signals_enabled(cfg, args):
        try:
            rc = max(rc, cmd_signals(cfg, args))
        except Exception as exc:  # 信号失败不影响放量报告
            log.error("指标信号筛选失败：%s", exc)
            log.debug("详细堆栈", exc_info=True)
    return rc


# --------------------------------------------------------------------------- 自检类命令
def cmd_demo(cfg: Config, args: argparse.Namespace) -> int:
    """合成行情自检：不联网也能跑通"落库 → 扫描 → 报告"整条链路。"""
    store_path = getattr(args, "store", None) or "data/demo.sqlite"
    path = Path(store_path)
    if not path.is_absolute():
        path = cfg.root / path
    store = Store(path).init_schema()
    info = synthesize(
        store,
        n_symbols=int(getattr(args, "symbols", 300)),
        days=int(getattr(args, "days", 220)),
        seed=int(getattr(args, "seed", 11)),
        end_date=getattr(args, "end_date", None),
    )
    log.info(
        "合成行情：%s 只股票 / %s 行 / 最后一个交易日 %s",
        info["symbols"],
        info["bars"],
        info["trade_date"],
    )
    result = scan(store.load_recent_bars(), store.symbols_df(), cfg, trade_date=info["trade_date"])
    print_console(result, cfg)
    files = write_files(result, cfg)
    for p in files:
        log.info("报告已写出：%s", p)

    hits = set(result.hits["secid"])
    targets = set(info["targets"])
    missed = sorted(targets - hits)
    extra = sorted(hits - targets)
    boundary = {
        "ST股": info["st_spike"],
        "新股(历史不足)": info["short_history_spike"],
        "当日停牌": info["suspended_spike"],
        "成交额过小": info["tiny_amount_spike"],
    }
    leaks = [f"{label}({secid})" for label, secid in boundary.items() if secid in hits]

    print()
    print("——— 合成数据自检 ———")
    print(f"注入真实放量 {len(targets)} 只，命中 {len(targets & hits)} 只，漏报 {len(missed)} 只")
    if missed:
        print(f"  漏报：{missed}")
    print(f"额外命中（正常波动触发）{len(extra)} 只")
    print(f"边界样本：{'全部按预期剔除' if not leaks else '未剔除 -> ' + ', '.join(leaks)}")
    store.close()
    return 0 if not missed and not leaks else 1


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    print(
        f"配置文件：{cfg.source if cfg.source else '（使用内置默认值，可用 MAIRUI_LICENCE 环境变量）'}"
    )
    print(f"工作目录：{cfg.root}（当前东八区日期 {tz_today()}）")
    licence = str(cfg.get("mairui.licence") or "")
    masked = f"{licence[:6]}…{licence[-4:]}" if len(licence) > 12 else ("已配置" if licence else "")
    print(
        f"数据源：麦蕊智数 {cfg.get('mairui.base_url')}"
        f"（证书：{masked or '未配置 → 设置 mairui.licence 或 MAIRUI_LICENCE'}）"
    )
    store = open_store(cfg)
    info = store.stats()
    print(f"行情库：{info['path']}（{info.get('size_mb')} MB）")
    print(
        f"  股票 {info['symbols']} 只 / 有行情 {info['symbols_with_bars']} 只 / "
        f"日线 {info['bars']} 行 / 交易日 {info['first_date']} ~ {info['last_date']}"
    )
    print(
        f"  技术指标 {info.get('indicator_rows', 0)} 行 / 覆盖 "
        f"{info.get('indicator_symbols', 0)} 只 / "
        f"{info.get('indicator_first_date')} ~ {info.get('indicator_last_date')}"
        f"（配置：{', '.join(cfg.get('indicators.kinds') or []) or '未开启'}）"
    )
    print(
        f"  每日信号缓存 {info.get('signal_rows', 0)} 行 / "
        f"{info.get('signal_days', 0)} 个交易日 / 最近 {info.get('signal_last_date')}"
        f"（查：python3 -m emscan hits）"
    )
    print(f"  参与扫描板块：{', '.join(cfg.get('universe.boards'))}")
    print(f"  自愈窗口：最近 {cfg.get('reconcile_calendar_days')} 个自然日")
    print(
        f"  快照取数：{cfg.get('mairui.spot_mode', 'auto')}"
        f"（多股一批 {cfg.get('mairui.multi_batch', 20)} 只；all=全市场接口，需 Pro/包年档）"
        f" · 限频 {cfg.get('mairui.rate_limit_per_min')} 次/分钟"
        f" · 并发 {MairuiClient.from_config(cfg).workers}（0=自动）"
    )
    print(
        f"  指标来源：{cfg.get('indicators.source', 'local')}"
        f"（local=本地按日线算，零接口请求；api=拉接口）"
        f" · 种类 {', '.join(cfg.get('indicators.kinds') or [])}"
    )
    _print_request_budget(cfg, info)
    print(f"  信号规则：{', '.join(normalize_rules(cfg.get('signals.rules')))}")
    store.close()

    if getattr(args, "probe", False):
        print("\n——— 联网探测（验证证书、接口与字段映射）———")
        client = MairuiClient.from_config(cfg)
        try:
            universe = client.fetch_universe()
            print(f"① 股票列表 OK：{len(universe)} 只，样例 {universe[:2]}")
            sample_secid = universe[0]["secid"] if universe else "1.600519"
            sample_secids = [row["secid"] for row in universe[:5]] or [sample_secid]
            print(f"② 当日快照（取数方式 {cfg.get('mairui.spot_mode', 'auto')}）：", end=" ")
            spot = client.fetch_spot(sample_secids)
            if spot:
                print(f"OK，{len(spot)} 条，行情日期 {spot_trade_date(spot)}")
            else:
                print("拿不到（检查证书档位/网络）")
            if spot:
                sample = spot[0]
                print(
                    "  样例：{secid} 收盘={close} 涨跌幅={pct_chg} 成交量={volume}(手) "
                    "成交额={amount} 换手率={turnover} 流通股本={float_shares}".format(**sample)
                )
                sample_secid = sample["secid"]
                rows = client.fetch_kline(
                    sample_secid, limit=5, float_shares=sample.get("float_shares")
                )
                print(f"③ 历史日线 OK：{sample_secid} 拿到 {len(rows)} 行（取最新 5 根）")
                for row in rows[-3:]:
                    print(
                        "  {date} 收={close} 量={volume}(手) 额={amount} "
                        "涨幅={pct_chg}% 换手={turnover}%".format(**row)
                    )
            for kind in indicator_kinds(cfg, args) or ["macd", "kdj", "boll"]:
                rows = client.fetch_indicator(sample_secid, kind, limit=3)
                print(f"④ 指标 {kind} OK：{sample_secid} 拿到 {len(rows)} 行；样例 {rows[-1:]}")
            year = tz_today()[:4]
            days = client.fetch_trade_calendar(year)
            print(f"⑤ 交易日历 OK：{year} 年 {len(days)} 个交易日，最后 3 个 {days[-3:]}")
        except Exception as exc:
            print(f"探测失败：{type(exc).__name__}: {exc}")
            return 1
        finally:
            _close(client)
    return 0


def _print_request_budget(cfg: Config, info: Dict[str, Any]) -> None:
    """按当前配置估一下"首次/每天要发多少请求、大概多久"，方便选证书档位。"""
    symbols = int(info.get("symbols") or 0) or 5400
    rate = float(cfg.get("mairui.rate_limit_per_min", 300) or 300)
    spot_mode = str(cfg.get("mairui.spot_mode", "auto")).lower()
    batch = max(1, int(cfg.get("mairui.multi_batch", 20) or 20))
    spot = 1 if spot_mode == "all" else max(1, (symbols + batch - 1) // batch)
    kinds = cfg.get("indicators.kinds") or []
    source = str(cfg.get("indicators.source", "local")).lower()
    indicators = symbols * len(kinds) if source == "api" else 0
    per_day = 1 + spot + indicators
    first = per_day + symbols  # 首次还要把历史日线拉一遍

    def minutes(count: int) -> str:
        value = count / max(1.0, rate)
        return f"{value:.0f} 分钟" if value < 90 else f"{value / 60:.1f} 小时"

    print(
        f"  请求预算：首次 ≈ {first} 次（约 {minutes(first)}）"
        f" · 每天 ≈ {per_day} 次（约 {minutes(per_day)}）"
        f" —— 按 {rate:g} 次/分钟算；改 mairui.rate_limit_per_min / indicators.source 都能提速"
    )


# --------------------------------------------------------------------------- 网页端
def cmd_web(cfg: Config, args: argparse.Namespace) -> int:
    """起网页端：四个查询按钮（放量 / MACD金叉 / KDJ金叉 / 布林下轨）+ 可排序表格。"""
    from .web import serve  # 延迟导入：只有跑 web 命令才需要它

    host = getattr(args, "host", None) or cfg.get("web.host", "127.0.0.1")
    port = int(getattr(args, "port", None) or cfg.get("web.port", 8080))
    password = str(
        getattr(args, "password", None)
        or getattr(args, "token", None)  # --token 是旧名字，保留兼容
        or cfg.get("web.password", "")
        or ""
    )
    title = str(cfg.get("web.title", "A股日频信号"))
    limit = int(getattr(args, "limit", None) or cfg.get("web.limit", 300) or 300)
    session_days = int(cfg.get("web.session_days", 7) or 7)

    store = open_store(cfg)
    if not store.latest_signal_date():
        log.warning("本地还没有每日信号缓存：网页能打开但会是空的，先跑 `python3 -m emscan run`")
    store.close()

    def factory() -> Store:
        # 每个请求开一个连接：SQLite WAL 支持并发读，cron 正在写也不影响看页面
        return open_store(cfg)

    try:
        serve(
            factory,
            host=host,
            port=port,
            title=title,
            limit=limit,
            password=password,
            session_days=session_days,
        )
    except OSError as exc:
        log.error("端口 %s 起不来（%s）：换一个 --port，或先关掉占用的进程", port, exc)
        return 1
    return 0


# --------------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emscan", description="A股日频放量扫描 + 指标信号（数据源：麦蕊智数）"
    )
    parser.add_argument(
        "--config",
        help="配置文件路径，默认 ./config.yaml（也可以只用 MAIRUI_LICENCE 环境变量）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化行情库")

    p_universe = sub.add_parser("universe", help="只刷新股票列表")
    p_universe.add_argument("--limit", type=int, help="最多处理 N 只（试点）")

    p_backfill = sub.add_parser("backfill", help="回补日线（新股全历史 + 缺口续传）")
    p_backfill.add_argument("--limit", type=int, help="最多处理 N 只（试点）")
    p_backfill.add_argument("--start", help="起始日期 YYYY-MM-DD（默认全部历史）")

    p_sync = sub.add_parser("sync", help="只补数据：列表+日线+当日行情+指标（不出报告）")
    p_sync.add_argument("--limit", type=int, help="最多处理 N 只（试点）")
    p_sync.add_argument("--start", help="自愈窗口起始日期 YYYY-MM-DD")
    p_sync.add_argument("--days", type=int, help="自愈窗口自然日数（默认取配置）")
    p_sync.add_argument("--force", action="store_true", help="即使快照日期不是今天也落库")
    p_sync.add_argument(
        "--skip-indicators", dest="skip_indicators", action="store_true", help="本次不抓技术指标"
    )

    p_update = sub.add_parser("update", help="只更新当日行情 + 补近期日线缺口")
    p_update.add_argument("--force", action="store_true", help="即使快照日期不是今天也落库")
    p_update.add_argument("--limit", type=int, help="最多处理 N 只（试点）")
    p_update.add_argument("--start", help="自愈窗口起始日期 YYYY-MM-DD")
    p_update.add_argument("--days", type=int, help="自愈窗口自然日数")

    p_ind = sub.add_parser("indicators", help="补齐日频 MACD/KDJ/BOLL/MA 指标")
    p_ind.add_argument("--limit", type=int, help="最多处理 N 只（试点）")
    p_ind.add_argument("--start", help="起始日期 YYYY-MM-DD（默认全部历史）")
    p_ind.add_argument("--kinds", help="指标类型：macd,kdj,boll,ma")
    p_ind.add_argument(
        "--source",
        choices=["local", "api"],
        help="指标来源：local=本地按日线算（默认，零请求）；api=拉麦蕊接口",
    )

    p_scan = sub.add_parser("scan", help="用本地数据扫描放量（可回看历史某天）")
    p_scan.add_argument("--trade-date", dest="trade_date", help="要扫描的交易日 YYYY-MM-DD")

    p_sig = sub.add_parser("signals", help="筛指标信号（金叉 / 布林下轨 / 死叉 …）")
    p_sig.add_argument("--trade-date", dest="trade_date", help="交易日 YYYY-MM-DD")
    p_sig.add_argument("--rules", help="规则名逗号分隔，如 macd_golden,kdj_golden,boll_lower")

    p_hits = sub.add_parser(
        "hits", help="查每日信号缓存：四个主信号(放量/MACD金叉/KDJ金叉/BOLL下轨突破)"
    )
    p_hits.add_argument("--date", help="指定交易日 YYYY-MM-DD（默认最近一天）")
    p_hits.add_argument("--secid", help="只看某只股票：600519 或 1.600519")
    p_hits.add_argument("--days", type=int, default=30, help="配合 --secid：回看多少个自然日")
    p_hits.add_argument(
        "--only",
        help="必须同时命中的信号，逗号分隔：vol_spike,macd_golden,kdj_golden,boll_break_lower",
    )
    p_hits.add_argument(
        "--min-signals", dest="min_signals", type=int, default=0, help="至少命中几个信号"
    )
    p_hits.add_argument("--limit", type=int, default=500, help="最多返回多少行")
    p_hits.add_argument("--out", help="把结果另存为 CSV（utf-8-sig）")

    p_run = sub.add_parser("run", help="每天跑这一条：自愈同步 + 放量扫描 + 指标信号 + 报告/推送")
    p_run.add_argument("--force", action="store_true", help="即使快照日期不是今天也处理")
    p_run.add_argument("--limit", type=int, help="最多处理 N 只（首跑试点）")
    p_run.add_argument("--start", help="自愈窗口起始日期 YYYY-MM-DD")
    p_run.add_argument("--days", type=int, help="自愈窗口自然日数")
    p_run.add_argument(
        "--skip-indicators", dest="skip_indicators", action="store_true", help="本次不抓技术指标"
    )
    p_run.add_argument(
        "--skip-signals", dest="skip_signals", action="store_true", help="本次不做指标信号筛选"
    )

    p_demo = sub.add_parser("demo", help="合成行情自检，不联网跑通全链路")
    p_demo.add_argument("--symbols", type=int, default=300, help="合成股票数量")
    p_demo.add_argument("--days", type=int, default=220, help="合成交易日数")
    p_demo.add_argument("--seed", type=int, default=11)
    p_demo.add_argument("--store", help="演示用行情库路径（默认 data/demo.sqlite）")
    p_demo.add_argument("--end-date", dest="end_date", help="最后一个交易日 YYYY-MM-DD")

    p_check = sub.add_parser("check", help="自检：配置/行情库/指标；--probe 联网验证接口")
    p_check.add_argument("--probe", action="store_true", help="调用真实接口验证证书与字段映射")
    p_check.add_argument("--kinds", help="探测哪些指标：macd,kdj,boll,ma")

    p_web = sub.add_parser("web", help="启动网页端：四个主信号查询 + 可排序表格（代码跳雪球）")
    p_web.add_argument("--host", help="监听地址，默认 127.0.0.1（只本机）；对外用 0.0.0.0")
    p_web.add_argument("--port", type=int, help="端口，默认 8080")
    p_web.add_argument(
        "--password",
        help="访问口令：浏览器首次访问跳登录页，登录后 Cookie 记住若干天（也可用 EMSCAN_WEB_PASSWORD）",
    )
    p_web.add_argument("--token", dest="token", help="（已废弃，等同 --password）")
    p_web.add_argument("--limit", type=int, help="单页最多显示多少行，默认 300")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    ensure_env_tz()
    parser = build_parser()
    args = parser.parse_args(argv)
    for flag in ("skip_indicators", "skip_signals"):
        if not hasattr(args, flag):
            setattr(args, flag, False)
    cfg = Config.load(args.config)
    cfg.ensure_dirs()
    setup_logging(cfg, verbose=args.verbose)
    log.debug("东八区今天 = %s", tz_today())

    handlers = {
        "init": cmd_init,
        "universe": cmd_universe,
        "backfill": cmd_backfill,
        "sync": cmd_sync,
        "update": cmd_update,
        "indicators": cmd_indicators,
        "scan": cmd_scan,
        "signals": cmd_signals,
        "hits": cmd_hits,
        "run": cmd_run,
        "demo": cmd_demo,
        "check": cmd_check,
        "web": cmd_web,
    }
    handler = handlers[args.command]
    try:
        return int(handler(cfg, args))
    except KeyboardInterrupt:
        log.warning("被中断")
        return 130
    except MairuiError as exc:
        log.error("接口/证书问题：%s", exc)
        log.error(
            "排错顺序：① mairui.licence 是否已配置且未过期（可在 "
            "https://www.mairui.club/hsdata 的 Playground 试调）；② 机器能否访问 "
            "api.mairuiapi.com（需要代理就用 https_proxy / no_proxy）；"
            "③ 按自己的证书档位调 mairui.rate_limit_per_min，并适当调小 workers、调大 timeout。"
        )
        log.debug("详细堆栈", exc_info=True)
        return 1
    except Exception as exc:
        log.exception("执行 %s 失败：%s", args.command, exc)
        return 1
