"""同步大盘对照用的四个价格指数。运行 python3 -m emscan.benchmarks --help。"""
from __future__ import annotations

import argparse
import logging
import math
from datetime import date, timedelta

from .config import Config
from .mairui import MairuiClient, MairuiError, normalize_date, to_float
from .store import Store
from .tzutil import now

log = logging.getLogger("emscan")
INDEX_CODES = ("000001.SH", "399001.SZ", "399006.SZ", "000688.SH")


def parse_rows(items, dm, start, end, source):
    """只接受请求区间内带有效收盘和前收的日线；缺前收时不推测涨跌。"""
    rows = {}
    for item in items:
        day = normalize_date(item.get("t"))
        if not day or not start <= day <= end:
            continue
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        close = to_float(item.get("c"))
        previous = to_float(item.get("pc"))
        if not all(n is not None and math.isfinite(n) and n > 0 for n in (close, previous)):
            continue
        pct = (close / previous - 1.0) * 100
        if math.isfinite(pct):
            rows[day] = (dm[:6], day, close, previous, pct, source)
    return rows


def _fetch(client, dm, start, end, latest=False):
    mode = "latest" if latest else "history"
    try:
        payload = client.fetch_benchmark_daily(dm, start, end, latest=latest)
    except MairuiError:
        # 异常对象包含授权 URL。日志只记指数与接口，不记录证书。
        log.warning("基准指数 %s 的 %s 接口暂不可用", dm, mode)
        return {}
    return parse_rows(payload, dm, start, end, f"mairui.hsindex.{mode}.d")


def sync_benchmarks(store, client, start: str, end: str, *, incremental=False) -> int:
    """抓取历史并补最后一个交易日。同一指数的迟到数据不会阻断其他指数。"""
    clock = now()
    if end > clock.date().isoformat() or (
        end == clock.date().isoformat() and clock.strftime("%H:%M") < "15:30"
    ):
        raise ValueError("基准日线仅可同步到已收盘的日期（北京时间 15:30 后）")
    if start > end:
        raise ValueError("起始日期晚于截止日期")
    expected = {r[0] for r in store.conn.execute(
        "SELECT date FROM trading_days WHERE date BETWEEN ? AND ?", (start, end)
    )}
    # 没有本地个股日历时，至少核验调用者指定的截止日。
    expected.add(end)
    missing_codes = 0
    for dm in INDEX_CODES:
        absent = expected
        if incremental:
            present = {
                r[0]
                for r in store.conn.execute(
                    "SELECT date FROM benchmark_bars WHERE code=? AND date BETWEEN ? AND ? "
                    "AND close>0 AND prev_close>0",
                    (dm[:6], start, end),
                )
            }
            absent = expected - present
            if not absent:
                continue
        first, last = (min(absent), max(absent)) if incremental else (start, end)
        latest_first = incremental and first == last == end and end == clock.date().isoformat()
        rows = _fetch(client, dm, first, last, latest=latest_first)
        if end in absent and end not in rows:
            rows.update(_fetch(client, dm, end, end, latest=not latest_first))
        with store.conn:
            store.conn.executemany(
                "INSERT INTO benchmark_bars(code,date,close,prev_close,pct_chg,source) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(code,date) DO UPDATE SET "
                "close=excluded.close,prev_close=excluded.prev_close,"
                "pct_chg=excluded.pct_chg,source=excluded.source",
                rows.values(),
            )
        have = {
            r[0]
            for r in store.conn.execute(
                "SELECT date FROM benchmark_bars WHERE code=? AND date BETWEEN ? AND ? AND close>0 AND prev_close>0",
                (dm[:6], start, end),
            )
        }
        absent = sorted(expected - have)
        if absent:
            missing_codes += 1
            log.warning("基准指数 %s 区间缺 %d 个交易日，最近缺日 %s", dm, len(absent), absent[-1])
        log.info("基准指数 %s 写入 %d 行，区间 %s 至 %s", dm, len(rows), start, end)
    return missing_codes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, help="起始日期，默认向前 365 个自然日")
    parser.add_argument("--end", type=date.fromisoformat, help="截止交易日，默认本地最近行情日")
    args = parser.parse_args()
    logging.basicConfig(format="%(levelname)s %(message)s", level=logging.INFO)
    cfg = Config.load()
    client = MairuiClient.from_config(cfg)
    try:
        with Store(cfg.path("store.path")).init_schema() as store:
            latest = store.latest_date()
            if args.end is None and not latest:
                parser.error("尚无本地日线，请指定 --end")
            end = args.end or date.fromisoformat(latest)
            start = args.start or end - timedelta(days=365)
            return int(bool(sync_benchmarks(store, client, start.isoformat(), end.isoformat())))
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
