"""emscan 命令行入口。

常用流程::

    python3 -m emscan init                      # 初始化行情库
    python3 -m emscan backfill                  # 首次：回补全市场历史日K线
    python3 -m emscan run                       # 每天 17:00 跑这个（更新 + 扫描 + 报告）
    python3 -m emscan scan --trade-date 2026-10-09   # 用本地数据回看某一天
    python3 -m emscan demo                      # 无网络自检（合成行情）
    python3 -m emscan check --probe             # 在能联网的机器上验证接口与字段映射
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .demo import synthesize
from .eastmoney import EastMoneyClient, EastMoneyError, spot_trade_date
from .notify import send as send_notify
from .report import print_console, write_files
from .scan import scan
from .store import Store

log = logging.getLogger("emscan")


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


def board_of(secid: str) -> str:
    """由 secid 推断板块（1=沪市，0=深市）。"""
    try:
        market, code = str(secid).split(".", 1)
    except ValueError:
        return "unknown"
    if market == "1":
        if code.startswith("688"):
            return "sh_star"
        if code.startswith(("5", "6")):
            return "sh_main"
        return "sh_other"
    if code.startswith(("300", "301")):
        return "sz_gem"
    if code.startswith(("4", "8", "920")):
        return "bse"
    if code.startswith(("000", "001", "002", "003")):
        return "sz_main"
    return "sz_other"


def _enrich_spot(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(row, board=board_of(row["secid"])) for row in rows]


def _upsert_bars_in_chunks(store: Store, rows: Sequence[Dict[str, Any]], chunk: int = 50000) -> int:
    total = 0
    buffer: List[Dict[str, Any]] = []
    for row in rows:
        buffer.append(row)
        if len(buffer) >= chunk:
            total += store.upsert_bars(buffer, refresh_days=False)
            buffer = []
    if buffer:
        total += store.upsert_bars(buffer, refresh_days=False)
    return total


# --------------------------------------------------------------------------- 命令
def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    store = open_store(cfg)
    info = store.stats()
    log.info("行情库已就绪：%s", info["path"])
    log.info(
        "当前：%s 只股票 / %s 行日线 / 交易日 %s ~ %s",
        info["symbols"],
        info["bars"],
        info["first_date"],
        info["last_date"],
    )
    store.close()
    return 0


def cmd_universe(cfg: Config, args: argparse.Namespace) -> int:
    store = open_store(cfg)
    client = EastMoneyClient.from_config(cfg)
    boards = cfg.get("universe.boards")
    spot = client.fetch_spot(boards)
    if not spot:
        log.error("快照为空，接口可能被限流或参数有变，请检查网络/参数")
        return 1
    rows = _enrich_spot(spot)
    count = store.upsert_symbols(rows)
    trade_date = spot_trade_date(spot)
    by_board: Dict[str, int] = {}
    for row in rows:
        by_board[row["board"]] = by_board.get(row["board"], 0) + 1
    log.info("股票列表已更新：%s 只（行情日期 %s）", count, trade_date)
    for board, n in sorted(by_board.items()):
        log.info("  %-8s %s", board, n)
    store.close()
    return 0


def cmd_backfill(cfg: Config, args: argparse.Namespace) -> int:
    store = open_store(cfg)
    client = EastMoneyClient.from_config(cfg)
    boards = cfg.get("universe.boards")

    log.info("① 拉取全市场快照，用于获取股票列表（板块：%s）", ",".join(boards))
    started = time.time()
    spot = client.fetch_spot(boards)
    if not spot:
        log.error("快照为空，无法确定股票列表")
        return 1
    rows = _enrich_spot(spot)
    store.upsert_symbols(rows)
    trade_date = spot_trade_date(spot)
    log.info("   共 %s 只股票（行情日期 %s）", len(rows), trade_date)

    secids = [row["secid"] for row in rows]
    if getattr(args, "limit", None):
        secids = secids[: int(args.limit)]
        log.info("   按 --limit 只回补前 %s 只", len(secids))

    log.info(
        "② 回补历史日K线：%s 只，并发 %s，预计 %s 分钟（受网络影响）",
        len(secids),
        client.workers,
        max(1, round(len(secids) / max(1, client.workers) / 20)),
    )

    def progress(done: int, total: int) -> None:
        if done % 250 == 0 or done == total:
            log.info("   进度 %s/%s", done, total)

    results, errors = client.fetch_kline_many(
        secids,
        beg=getattr(args, "start", None) or "0",
        progress=progress,
    )
    written = _upsert_bars_in_chunks(
        store, [row for result in results.values() for row in result["rows"]]
    )
    store.refresh_trading_days()
    elapsed = time.time() - started
    log.info(
        "③ 回补完成：%s 只成功 / %s 行日线落库，耗时 %.1f 秒，失败 %s 只",
        len(results),
        written,
        elapsed,
        len(errors),
    )
    for secid, err in errors[:10]:
        log.warning("   失败样例 %s：%s", secid, err)
    store.record_run(
        "backfill",
        trade_date=trade_date,
        symbols=len(secids),
        note=f"rows={written} errors={len(errors)}",
    )
    store.close()
    return 0  # 单只失败不影响整体，靠日志和 note 里的 errors 计数观察


def do_update(cfg: Config, args: argparse.Namespace) -> Optional[str]:
    """增量更新行情。返回可扫描的交易日；None 表示本次无需扫描（如非交易日）。"""
    store = open_store(cfg)
    if store.latest_date() is None:
        log.warning("本地行情库为空，先做一次全量回补")
        if cmd_backfill(cfg, args) != 0:
            store.close()
            return None

    client = EastMoneyClient.from_config(cfg)
    boards = cfg.get("universe.boards")
    spot = client.fetch_spot(boards)
    if not spot:
        log.error("快照为空，接口可能被限流或参数有变")
        store.close()
        return None

    rows = _enrich_spot(spot)
    trade_date = spot_trade_date(spot) or dt.datetime.now().strftime("%Y-%m-%d")
    today = dt.datetime.now().strftime("%Y-%m-%d")
    if trade_date != today and not getattr(args, "force", False):
        log.warning(
            "快照行情日期是 %s，不是今天 %s：今天大概率不是交易日，或数据还没更新。"
            "本次不落库、不扫描。",
            trade_date,
            today,
        )
        store.record_run("update", trade_date=trade_date, note="skip: 非交易日或数据未更新")
        store.close()
        return None

    store.upsert_symbols(rows)
    bar_rows: List[Dict[str, Any]] = []
    suspended = 0
    for row in rows:
        if not row.get("volume"):  # 停牌/无成交，不写当日K线
            suspended += 1
            continue
        bar = dict(row)
        bar["date"] = trade_date
        bar_rows.append(bar)
    written = _upsert_bars_in_chunks(store, bar_rows)
    store.refresh_trading_days()
    log.info(
        "增量落库：%s 只股票 %s 的当日行情（停牌/无成交跳过 %s 只）",
        written,
        trade_date,
        suspended,
    )

    healed, heal_errors = _heal_gaps(cfg, store, client, [row["secid"] for row in rows], trade_date)
    if healed:
        log.info("缺口回补：%s 只（失败 %s 只）", healed, heal_errors)
    store.record_run(
        "update",
        trade_date=trade_date,
        symbols=len(rows),
        note=f"bars={written} healed={healed} suspended={suspended}",
    )
    store.close()
    return trade_date


def _heal_gaps(
    cfg: Config,
    store: Store,
    client: EastMoneyClient,
    secids: Sequence[str],
    trade_date: str,
    max_symbols: int = 500,
) -> Tuple[int, int]:
    """找出"本地数据缺了中间某一天"的股票，用日K线接口补回来。

    停牌股只缺当天属于正常情况，不发请求；同一天内已经尝试补过的股票靠
    heal_log 表去重，避免反复抓取。
    """
    expected_prev = store.trading_day_before(trade_date)
    last = store.last_dates()
    already = store.healed_secids(trade_date)

    groups: Dict[str, List[str]] = {}
    for secid in secids:
        if secid in already:
            continue
        last_date = last.get(secid)
        if last_date is None:
            groups.setdefault("0", []).append(secid)  # 从没抓到过：拉全历史
        elif last_date == trade_date or last_date == expected_prev:
            continue  # 数据是连着的
        else:
            groups.setdefault(last_date, []).append(secid)  # 从断点开始补

    todo = sum(len(v) for v in groups.values())
    if todo == 0:
        log.debug("没有发现需要回补的股票")
        return 0, 0
    log.info("发现 %s 只股票的历史数据不连续，开始回补", todo)

    healed = 0
    errors = 0
    budget = max_symbols
    for beg, group in groups.items():
        if budget <= 0:
            log.warning("本次回补数量达到上限 %s，剩余留到下次运行处理", max_symbols)
            break
        if len(group) > budget:
            log.warning("回补上限 %s，本次只处理 %s 只", max_symbols, budget)
            group = group[:budget]
        budget -= len(group)
        results, group_errors = client.fetch_kline_many(group, beg=beg)
        entries = []
        for secid, result in results.items():
            rows = result["rows"]
            if rows:
                store.upsert_bars(rows, refresh_days=False)
            entries.append((secid, len(rows)))
        store.log_heal(trade_date, entries)
        store.refresh_trading_days()
        healed += len(results)
        errors += len(group_errors)
    return healed, errors


def cmd_update(cfg: Config, args: argparse.Namespace) -> int:
    do_update(cfg, args)
    return 0


def cmd_scan(cfg: Config, args: argparse.Namespace) -> int:
    store = open_store(cfg)
    trade_date = getattr(args, "trade_date", None) or store.latest_date()
    if not trade_date:
        log.error("本地没有任何行情数据，先执行 backfill")
        store.close()
        return 1
    calendar_days = int(cfg.get("scan.lookback_calendar_days", 250) or 250)
    bars = store.load_recent_bars(calendar_days=calendar_days)
    symbols = store.symbols_df()
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


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    """每天 17:00 的入口：增量更新 + 扫描 + 报告 + 推送。"""
    trade_date = do_update(cfg, args)
    if trade_date is None:
        log.info("本次没有新的交易日数据，跳过扫描")
        return 0
    args.trade_date = trade_date
    return cmd_scan(cfg, args)


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
    print(f"配置文件：{cfg.source if cfg.source else '（使用内置默认值）'}")
    print(f"工作目录：{cfg.root}")
    store = open_store(cfg)
    info = store.stats()
    print(f"行情库：{info['path']}（{info.get('size_mb')} MB）")
    print(
        f"  股票 {info['symbols']} 只 / 有行情 {info['symbols_with_bars']} 只 / "
        f"日线 {info['bars']} 行 / 交易日 {info['first_date']} ~ {info['last_date']}"
    )
    print(f"  参与扫描板块：{', '.join(cfg.get('universe.boards'))}")
    store.close()

    if getattr(args, "probe", False):
        print("\n——— 联网探测（验证接口与字段映射）———")
        client = EastMoneyClient.from_config(cfg)
        try:
            boards = cfg.get("universe.boards")
            spot = client.fetch_spot(boards, page_size=5)
            print(f"快照接口 OK：样本 {len(spot)} 条，行情日期 {spot_trade_date(spot)}")
            if spot:
                sample = spot[0]
                print(
                    "  样例：{secid} {name} 收盘={close} 涨跌幅={pct_chg} 成交量={volume}(手) "
                    "成交额={amount} 换手率={turnover}".format(**sample)
                )
                result = client.fetch_kline(sample["secid"], beg="0", end="20500101")
                rows = result["rows"]
                print(f"日K线接口 OK：{sample['secid']} {result['name']} 拿到 {len(rows)} 行")
                for row in rows[-3:]:
                    print(
                        "  {date} 收={close} 量={volume}(手) 额={amount} "
                        "涨幅={pct_chg}% 换手={turnover}%".format(**row)
                    )
        except Exception as exc:
            print(f"探测失败：{type(exc).__name__}: {exc}")
            return 1
        finally:
            client.close()
    return 0


# --------------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emscan", description="A股日频放量扫描（数据源：东方财富）"
    )
    parser.add_argument("--config", help="配置文件路径，默认 ./config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化行情库")
    sub.add_parser("universe", help="只刷新股票列表")

    p_backfill = sub.add_parser("backfill", help="首次全量回补历史日K线")
    p_backfill.add_argument("--limit", type=int, help="只回补前 N 只，用于试点")
    p_backfill.add_argument("--start", help="起始日期 YYYY-MM-DD（默认全部历史）")

    p_update = sub.add_parser("update", help="增量更新当天行情（含缺口回补）")
    p_update.add_argument("--force", action="store_true", help="即使快照日期不是今天也落库")

    p_scan = sub.add_parser("scan", help="用本地数据扫描（可回看历史某天）")
    p_scan.add_argument("--trade-date", dest="trade_date", help="要扫描的交易日 YYYY-MM-DD")

    p_run = sub.add_parser("run", help="每日 17:00 入口：update + scan + 报告/推送")
    p_run.add_argument("--force", action="store_true", help="即使快照日期不是今天也处理")

    p_demo = sub.add_parser("demo", help="合成行情自检，不联网跑通全链路")
    p_demo.add_argument("--symbols", type=int, default=300, help="合成股票数量")
    p_demo.add_argument("--days", type=int, default=220, help="合成交易日数")
    p_demo.add_argument("--seed", type=int, default=11)
    p_demo.add_argument("--store", help="演示用行情库路径（默认 data/demo.sqlite）")
    p_demo.add_argument("--end-date", dest="end_date", help="最后一个交易日 YYYY-MM-DD")

    p_check = sub.add_parser("check", help="自检：配置/行情库状态；--probe 会联网验证接口")
    p_check.add_argument("--probe", action="store_true", help="调用一次真实接口验证字段映射")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    cfg.ensure_dirs()
    setup_logging(cfg, verbose=args.verbose)

    handlers = {
        "init": cmd_init,
        "universe": cmd_universe,
        "backfill": cmd_backfill,
        "update": cmd_update,
        "scan": cmd_scan,
        "run": cmd_run,
        "demo": cmd_demo,
        "check": cmd_check,
    }
    handler = handlers[args.command]
    try:
        return int(handler(cfg, args))
    except KeyboardInterrupt:
        log.warning("被中断")
        return 130
    except EastMoneyError as exc:
        log.error("接口请求失败：%s", exc)
        log.error(
            "排错建议：确认机器能直连 push2.eastmoney.com / push2his.eastmoney.com；"
            "如果需要代理，请设置 https_proxy 环境变量；仍失败就调小 eastmoney.workers 和 timeout。"
        )
        log.debug("详细堆栈", exc_info=True)
        return 1
    except Exception as exc:
        log.exception("执行 %s 失败：%s", args.command, exc)
        return 1
