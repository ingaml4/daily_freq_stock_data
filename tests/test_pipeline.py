"""全链路测试：合成行情端到端 + 缺口回补逻辑 + 推送开关。"""

import pandas as pd
from helpers import bar, make_cfg, trading_dates

from emscan.cli import _heal_gaps, board_of, open_store
from emscan.demo import synthesize
from emscan.notify import build_text, send
from emscan.report import write_files
from emscan.scan import scan
from emscan.store import Store


class FakeClient:
    """假客户端：记录被请求的 secid/beg，并返回当天 K 线。"""

    def __init__(self, respond=None):
        self.calls = []
        self.respond = respond

    def fetch_kline_many(self, secids, beg="0", end="20500101", klt=101, fqt=None, progress=None):
        self.calls.append((beg, list(secids)))
        results = {}
        for secid in secids:
            rows = self.respond(secid, beg) if self.respond else [bar(secid, "2026-10-08", 1000.0)]
            results[secid] = {
                "secid": secid,
                "name": None,
                "code": None,
                "market": None,
                "rows": rows,
            }
        return results, []


def test_board_of_maps_secid_to_board():
    assert board_of("1.600519") == "sh_main"
    assert board_of("1.688981") == "sh_star"
    assert board_of("0.000001") == "sz_main"
    assert board_of("0.300750") == "sz_gem"
    assert board_of("0.830799") == "bse"
    assert board_of("bad") == "unknown"


def test_heal_gaps_only_fetches_broken_series(tmp_path):
    cfg = make_cfg(tmp_path)
    store = open_store(cfg)
    dates = trading_dates(6, end="2026-10-09")
    trade_date = dates[-1]
    prev = dates[-2]

    x, y, z, w = "1.600001", "1.600002", "1.600003", "1.600004"
    store.upsert_bars([bar(x, d, 1000.0) for d in dates[:4]])  # 缺 prev 与 trade_date
    store.upsert_bars([bar(y, d, 1000.0) for d in dates[:-1]])  # 只缺 trade_date（视作停牌）
    store.upsert_bars([bar(z, d, 1000.0) for d in dates])  # 完整
    store.upsert_symbols(
        [
            {"secid": s, "code": s.split(".")[-1], "name": s, "market": 1, "board": "sh_main"}
            for s in (x, y, z, w)
        ]
    )
    client = FakeClient()
    healed, errors = _heal_gaps(cfg, store, client, [x, y, z, w], trade_date)
    assert (healed, errors) == (2, 0)
    assert client.calls == [(dates[3], [x]), ("0", [w])]
    # 回补后 X 补齐了 prev 那一行
    assert store.last_dates()[x] == prev
    # 同一天再跑一次：heal_log 去重，不再发请求
    healed2, errors2 = _heal_gaps(cfg, store, client, [x, y, z, w], trade_date)
    assert (healed2, errors2) == (0, 0)
    assert len(client.calls) == 2
    store.close()


def test_heal_gaps_respects_budget(tmp_path):
    cfg = make_cfg(tmp_path)
    store = open_store(cfg)
    dates = trading_dates(4, end="2026-10-09")
    complete = "1.600999"  # 覆盖全部交易日，用来把 trading_days 定义清楚
    store.upsert_bars([bar(complete, d, 1000.0) for d in dates])
    secids = [f"1.60000{i}" for i in range(5)]
    for secid in secids:  # 都缺最后一两天 → 需要回补
        store.upsert_bars([bar(secid, d, 1000.0) for d in dates[:-2]])
    client = FakeClient()
    healed, _ = _heal_gaps(cfg, store, client, secids, dates[-1], max_symbols=2)
    assert healed == 2
    assert client.calls == [(dates[-3], secids[:2])]
    store.close()


def test_demo_pipeline_end_to_end(tmp_path):
    cfg = make_cfg(tmp_path, {"store": {"reports_dir": str(tmp_path / "reports")}})
    store = Store(tmp_path / "demo.sqlite").init_schema()
    info = synthesize(store, n_symbols=120, days=120, seed=3)
    result = scan(store.load_recent_bars(), store.symbols_df(), cfg, trade_date=info["trade_date"])
    hits = set(result.hits["secid"])
    assert set(info["targets"]) <= hits, sorted(set(info["targets"]) - hits)
    for key in ("st_spike", "short_history_spike", "suspended_spike", "tiny_amount_spike"):
        assert info[key] not in hits, key
    assert result.stats["scanned"] > 0
    assert set(result.hits["signal"]) <= {"放量上攻", "放量下跌", "放量滞涨", "放量震荡"}

    files = write_files(result, cfg)
    csv_path = tmp_path / "reports" / f"volscan_{info['trade_date']}.csv"
    assert csv_path.exists()
    frame = pd.read_csv(csv_path)
    assert {"secid", "vol_ratio_5", "score"} <= set(frame.columns)
    assert len(files) == 2
    store.close()


def test_notify_disabled_by_default(tmp_path):
    cfg = make_cfg(tmp_path)
    result = scan(pd.DataFrame(), None, cfg)
    assert send(result, cfg) == {"enabled": False}
    assert "没有触发" in build_text(result, cfg)
    assert "A股日频放量扫描" in build_text(result, cfg)


def test_rescan_is_stable(tmp_path):
    """同一天重复扫描应给出完全一致的结果（幂等）。"""
    cfg = make_cfg(tmp_path, {"store": {"reports_dir": str(tmp_path / "reports")}})
    store = Store(tmp_path / "demo.sqlite").init_schema()
    info = synthesize(store, n_symbols=60, days=100, seed=5)
    bars = store.load_recent_bars()
    first = scan(bars, store.symbols_df(), cfg, trade_date=info["trade_date"]).hits
    second = scan(bars, store.symbols_df(), cfg, trade_date=info["trade_date"]).hits
    pd.testing.assert_frame_equal(first.reset_index(drop=True), second.reset_index(drop=True))
    store.close()
