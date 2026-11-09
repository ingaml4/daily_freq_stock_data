"""按策略窗口补有效日线，只插入缺失日期，可重复或分批执行。"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from .config import Config
from .mairui import MairuiClient
from .store import Store


def history_window(cfg, end):
    target = max(
        61,
        int(cfg.get("reversal.drawdown_window", 120)),
        int(cfg.get("doji.drawdown_window", 120)),
        int(cfg.get("scan.baseline_window", 60)) + 1,
        int(cfg.get("box.window", 40)),
    )
    start = (date.fromisoformat(end) - timedelta(days=target * 2 + 30)).isoformat()
    return start, target


def fill_history(store, client, cfg, limit=0, batch=64, cooldown_days=0):
    end = store.latest_date()
    if not end:
        raise ValueError("请先同步股票池和当日行情")
    start, target = history_window(cfg, end)
    retry_before = (date.fromisoformat(end) - timedelta(days=cooldown_days)).isoformat()
    missing = [
        r[0]
        for r in store.conn.execute(
            "SELECT s.secid FROM symbols s LEFT JOIN bars b ON b.secid=s.secid "
            "AND b.date BETWEEN ? AND ? AND b.volume>0 AND b.close>0 "
            "LEFT JOIN meta m ON m.key='history_checked:' || s.secid "
            "LEFT JOIN meta a ON a.key='history_attempt:' || s.secid "
            "WHERE (?=0 OR m.value IS NULL OR m.value<=?) "
            "AND (?=0 OR a.value IS NULL OR a.value<?) GROUP BY s.secid "
            "HAVING COUNT(b.date) < ? AND (s.listed_date IS NULL OR s.listed_date<=? "
            "OR s.listed_date<(SELECT MIN(date) FROM trading_days) "
            "OR COUNT(b.date)<(SELECT COUNT(*) FROM trading_days d "
            "WHERE d.date BETWEEN s.listed_date AND ?)) "
            "ORDER BY COALESCE(a.value,m.value,''),s.secid",
            (start, end, cooldown_days, retry_before, cooldown_days, end, target, start, end),
        )
    ]
    if limit:
        missing = missing[:limit]
    shares = store.float_shares_map()
    errors = inserted = 0
    for offset in range(0, len(missing), batch):
        codes = missing[offset : offset + batch]
        results, failed = client.fetch_kline_many(codes, beg=start, end=end, float_shares=shares)
        errors += len(failed)
        payload = [
            row
            for rows in results.values()
            for row in rows
            if start <= row["date"] <= end
            and (row.get("volume") or 0) > 0
            and (row.get("close") or 0) > 0
        ]
        inserted += store.upsert_bars(payload, refresh_days=False, missing_only=True)
        with store.conn:
            store.conn.executemany(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                [("history_checked:" + secid, end) for secid in results]
                + [("history_attempt:" + secid, end) for secid in codes],
            )
        print(
            f"已检查 {offset + len(codes)}/{len(missing)}，新增 {inserted} 根，失败 {errors}",
            flush=True,
        )
        if len(failed) == len(codes):
            raise RuntimeError("整批请求失败；已保留之前的进度")
    if inserted:
        store.refresh_trading_days()
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    cfg = Config.load()
    client = MairuiClient.from_config(cfg)
    try:
        with Store(cfg.path("store.path")).init_schema() as store:
            return int(bool(fill_history(store, client, cfg, args.limit)))
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
