"""全链路测试：自愈同步（缺口/指标）+ 合成行情端到端 + 信号命令 + 推送开关。"""

import pandas as pd
import yaml
from helpers import bar, make_cfg, trading_dates

from emscan.cli import main, open_store
from emscan.demo import synthesize
from emscan.notify import build_text, send
from emscan.pipeline import (
    SyncStats,
    _plan_jobs,
    resolve_trade_days,
    sync_bars,
    sync_indicators,
    sync_spot,
    sync_universe,
)
from emscan.report import write_files
from emscan.scan import scan
from emscan.store import Store


class FakeClient:
    """假客户端：记录被请求的 secid/beg，并按需返回 K 线。"""

    def __init__(self, respond=None, dates=("2026-10-08",)):
        self.calls = []
        self.respond = respond
        self.dates = list(dates)

    def fetch_kline_many(self, secids, beg="0", end=None, float_shares=None, progress=None):
        self.calls.append((beg, list(secids)))
        results = {}
        for secid in secids:
            if self.respond:
                rows = self.respond(secid, beg)
            else:
                rows = [bar(secid, d, 1000.0) for d in self.dates if beg == "0" or d >= beg]
            results[secid] = rows
        return results, []


class FakeInstrumentClient(FakeClient):
    def fetch_instrument_many(self, secids, progress=None, max_errors=50):
        return (
            {secid: {"float_shares": 1.0e9, "listed_date": "1999-11-10"} for secid in secids},
            [],
        )


def test_plan_jobs_prefers_gap_then_full_history():
    expected = ["2026-10-01", "2026-10-02", "2026-10-03"]
    have = {"a": {"2026-10-01", "2026-10-03"}, "b": set()}
    jobs = _plan_jobs(["a", "b", "c"], expected, have)
    assert jobs == {"2026-10-02": ["a"], "0": ["b", "c"]}
    # 数据齐全的股票不产生请求
    assert _plan_jobs(["a"], expected, {"a": set(expected)}) == {}
    # 次新股：上市之前的日期不算缺口，避免每天重复请求
    assert _plan_jobs(["a"], expected, have, listed={"a": "2026-10-02"}) == {"2026-10-02": ["a"]}
    assert (
        _plan_jobs(["a"], expected, {"a": {"2026-10-02", "2026-10-03"}}, listed={"a": "2026-10-02"})
        == {}
    )


def test_sync_bars_heals_only_broken_series(tmp_path):
    cfg = make_cfg(tmp_path)
    store = open_store(cfg)
    dates = trading_dates(6, end="2026-10-09")
    x, y, z, w = "1.600001", "1.600002", "1.600003", "1.600004"
    store.upsert_bars([bar(x, d, 1000.0) for d in dates[:4]])  # 缺两天
    store.upsert_bars([bar(y, d, 1000.0) for d in dates[:-1]])  # 只缺最后一天（视作停牌）
    store.upsert_bars([bar(z, d, 1000.0) for d in dates])  # 完整
    store.upsert_symbols(
        [
            {"secid": s, "code": s.split(".")[1], "name": s, "market": 1, "board": "sh_main"}
            for s in (x, y, z, w)
        ]
    )
    client = FakeClient(dates=dates)
    stats = SyncStats()
    written = sync_bars(store, client, cfg, stats, [x, y, z, w], dates)
    assert written == 2 + 1 + len(dates)  # x 补 2 行 + y 补 1 行 + w 全历史
    # 先补最近的缺口（y 缺最后一天，x 缺最后两天），最后才是新股票的"全历史"
    assert client.calls == [(dates[5], [y]), (dates[4], [x]), ("0", [w])]
    # 补齐后所有股票的数据都到最后一个交易日了
    assert store.last_dates()[x] == dates[-1]
    assert store.last_dates()[w] == dates[-1]
    # 再跑一次：数据已完整，不再发请求
    before = len(client.calls)
    assert sync_bars(store, client, cfg, stats, [x, y, z, w], dates) == 0
    assert len(client.calls) == before
    store.close()


def test_sync_bars_respects_max_symbols(tmp_path):
    """单次运行的上限：超出部分留到下次（避免 cron 单次跑太久）。"""
    cfg = make_cfg(tmp_path)
    store = open_store(cfg)
    dates = trading_dates(3, end="2026-10-09")
    secids = [f"1.60{i:04d}" for i in range(5)]
    store.upsert_symbols(
        [
            {
                "secid": s,
                "code": s.split(".")[1],
                "name": s,
                "market": 1,
                "board": "sh_main",
            }
            for s in secids
        ]
    )
    client = FakeClient()
    stats = SyncStats()
    sync_bars(store, client, cfg, stats, secids, dates, max_symbols=2)
    assert sum(len(group) for _, group in client.calls) == 2
    # 第二批把剩下的补齐
    sync_bars(store, client, cfg, stats, secids, dates, max_symbols=10)
    assert store.stats()["symbols_with_bars"] == 5
    store.close()


def test_resolve_trade_days_falls_back_to_weekdays(tmp_path):
    cfg = make_cfg(tmp_path)

    class BrokenCalendar:
        def fetch_trade_calendar_span(self, years):
            raise RuntimeError("boom")

    days = resolve_trade_days(BrokenCalendar(), "2026-10-05", "2026-10-09", cfg)
    assert days == [f"2026-10-0{d}" for d in (5, 6, 7, 8, 9)]  # 周一到周五

    class OkCalendar:
        def fetch_trade_calendar_span(self, years):
            return ["2026-10-05", "2026-10-06"]

    assert resolve_trade_days(OkCalendar(), "2026-10-01", "2026-10-09", cfg) == [
        "2026-10-05",
        "2026-10-06",
    ]


def test_sync_universe_and_float_shares(tmp_path):
    cfg = make_cfg(tmp_path, {"mairui": {"fetch_instrument": True}})
    store = open_store(cfg)

    class ListClient(FakeInstrumentClient):
        def fetch_universe(self):
            return [
                {
                    "secid": "1.600001",
                    "code": "600001",
                    "name": "测试",
                    "market": 1,
                    "board": "sh_main",
                    "is_st": 0,
                },
                {
                    "secid": "0.300999",
                    "code": "300999",
                    "name": "创业板票",
                    "market": 0,
                    "board": "sz_gem",
                    "is_st": 0,
                },
            ]

    client = ListClient()
    stats = SyncStats()
    secids = sync_universe(store, client, cfg, stats)
    # 默认板块配置不含 bse，但包含 sh_main/sz_gem
    assert secids == ["1.600001", "0.300999"]
    assert store.secids_missing_shares() == secids
    from emscan.pipeline import sync_float_shares

    assert sync_float_shares(store, client, cfg, stats, secids) == 2
    assert store.float_shares_map() == {"1.600001": 1.0e9, "0.300999": 1.0e9}
    store.close()


def test_sync_spot_writes_bars_and_skips_suspended(tmp_path):
    cfg = make_cfg(tmp_path)
    store = open_store(cfg)
    store.upsert_bars([bar("1.600001", "2026-10-08", 1000.0)])
    today = trading_dates(1, end="2026-10-09")[0]

    class SpotClient:
        def __init__(self, rows):
            self.rows = rows

        def fetch_spot(self, secids=None, mode=None):
            return self.rows

    rows = [
        {
            "secid": "1.600001",
            "code": "600001",
            "date": None,
            "ts": f"{today} 15:00:03",
            "close": 11.0,
            "volume": 5000.0,
            "amount": 5.5e6,
            "turnover": 1.0,
            "float_shares": 1.0e9,
            "source": "spot",
        },
        {
            "secid": "0.300999",
            "code": "300999",
            "date": None,
            "ts": f"{today} 15:00:03",
            "close": 0.0,
            "volume": 0.0,
            "amount": 0.0,
            "float_shares": None,
            "source": "spot",
        },
    ]
    stats = SyncStats()
    result = sync_spot(store, SpotClient(rows), cfg, stats, ["1.600001", "0.300999"])
    assert result in (today, None)
    if result is not None:
        assert store.stats()["bars"] == 2  # 停牌那只不落库
    # 全部为 0（盘前/未更新）则跳过
    zero = [{**row, "close": 0.0, "volume": 0.0} for row in rows]
    stats2 = SyncStats()
    assert sync_spot(store, SpotClient(zero), cfg, stats2, ["1.600001"]) in ("2026-10-09", None)
    store.close()


class FakeIndicatorClient:
    """假指标客户端：为每个交易日返回 MACD/KDJ/BOLL，最后两天构成"金叉 + 踩下轨"。"""

    def __init__(self, dates):
        self.dates = list(dates)
        self.calls = []

    def fetch_indicators_many(self, secids, kinds=("macd", "kdj", "boll"), beg="0", **kwargs):
        self.calls.append((beg, list(secids), list(kinds)))
        rows = {kind: [] for kind in kinds}
        last, prev = len(self.dates) - 1, len(self.dates) - 2
        for secid in secids:
            for index, date in enumerate(self.dates):
                if "macd" in kinds:
                    rows["macd"].append(
                        {
                            "secid": secid,
                            "date": date,
                            "macd_diff": (
                                -0.05 if index == last else (-0.2 if index == prev else 0.0)
                            ),
                            "macd_dea": (
                                -0.08 if index == last else (-0.1 if index == prev else 0.0)
                            ),
                            "macd_bar": 0.06 if index == last else (-0.2 if index == prev else 0.0),
                        }
                    )
                if "kdj" in kinds:
                    rows["kdj"].append(
                        {
                            "secid": secid,
                            "date": date,
                            "kdj_k": 45.0 if index == last else (20.0 if index == prev else 30.0),
                            "kdj_d": 38.0 if index == last else (30.0 if index == prev else 30.0),
                            "kdj_j": 59.0 if index == last else 0.0,
                        }
                    )
                if "boll" in kinds:
                    rows["boll"].append(
                        {
                            "secid": secid,
                            "date": date,
                            "boll_upper": 11.2 if index == last else 11.5,
                            "boll_mid": 10.6 if index == last else 10.8,
                            "boll_lower": 10.0 if index == last else 10.1,
                        }
                    )
        return {kind: value for kind, value in rows.items() if kind in kinds}, []


def _seed_symbols(store, secids):
    store.upsert_symbols(
        [
            {
                "secid": secid,
                "code": secid.split(".")[1],
                "name": secid,
                "market": int(secid.split(".")[0]),
                "board": "sh_main",
            }
            for secid in secids
        ]
    )


def test_sync_indicators_heals_each_kind(tmp_path):
    cfg = make_cfg(tmp_path)
    store = open_store(cfg)
    dates = trading_dates(3, end="2026-10-09")
    secids = ["1.600001", "0.300002"]
    _seed_symbols(store, secids)
    client = FakeIndicatorClient(dates)
    stats = SyncStats()
    written = sync_indicators(store, client, cfg, stats, secids, dates, ["macd", "kdj", "boll"])
    assert written == 2 * 3 * 3  # 股票 × 日期 × 指标种类
    frame = store.load_indicators()
    assert set(frame["secid"]) == set(secids)
    assert set(frame["date"]) == set(dates)
    assert store.indicator_dates_by_secid("macd")["1.600001"] == set(dates)
    # KDJ 缺失时会单独补 KDJ，不影响已有的 MACD
    store.conn.execute("UPDATE indicators SET kdj_k = NULL, kdj_d = NULL, kdj_j = NULL")
    store.conn.commit()
    assert store.indicator_dates_by_secid("kdj") == {}
    calls_before = len(client.calls)
    sync_indicators(store, client, cfg, stats, secids, dates, ["macd", "kdj"])
    assert [kind for _, _, kinds in client.calls[calls_before:] for kind in kinds] == ["kdj"]
    assert store.indicator_dates_by_secid("kdj")["1.600001"] == set(dates)
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


def test_signals_command_end_to_end(tmp_path):
    """`python3 -m emscan signals` 跑通：本地指标 → 终端表格 + CSV/XLSX。"""
    store_path = tmp_path / "market.sqlite"
    cfg = make_cfg(
        tmp_path,
        {
            "store": {
                "path": str(store_path),
                "reports_dir": str(tmp_path / "reports"),
                "logs_dir": str(tmp_path / "logs"),
            },
            "signals": {"top_n": 10},
        },
    )
    store = open_store(cfg)
    dates = trading_dates(6, end="2026-10-09")
    secid = "1.600001"
    rows = [bar(secid, d, 1000.0, close=10.0) for d in dates]
    rows[-1]["low"] = 9.5
    rows[-1]["high"] = 10.4
    store.upsert_bars(rows)
    _seed_symbols(store, [secid])
    sync_indicators(
        store,
        FakeIndicatorClient(dates[-2:]),
        cfg,
        SyncStats(),
        [secid],
        dates[-2:],
        ["macd", "kdj", "boll"],
    )
    store.close()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "store": {
                    "path": str(store_path),
                    "reports_dir": str(tmp_path / "reports"),
                    "logs_dir": str(tmp_path / "logs"),
                },
                "signals": {"rules": ["macd_golden", "kdj_golden", "boll_lower"]},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    assert main(["--config", str(config_path), "signals"]) == 0
    csv_path = tmp_path / "reports" / f"signals_{dates[-1]}.csv"
    assert csv_path.exists()
    frame = pd.read_csv(csv_path)
    assert [str(value) for value in frame["code"]] == ["600001"]
    assert "MACD金叉" in frame.iloc[0]["signals_cn"]


def test_run_skips_report_when_market_closed(tmp_path):
    """非交易日（快照日期≠今天）时：不落库、不扫描，退出码 0。"""
    store_path = tmp_path / "market.sqlite"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "mairui": {"licence": "LICENCE-TEST"},
                "store": {
                    "path": str(store_path),
                    "reports_dir": str(tmp_path / "reports"),
                    "logs_dir": str(tmp_path / "logs"),
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    # 快照日期是"昨天"（非交易日/数据没更新）-> 整个 run 除了补数据什么都不做
    class YesterdayClient(FakeClient):
        @classmethod
        def from_config(cls, cfg):
            return cls()

        def fetch_universe(self):
            return [
                {
                    "secid": "1.600001",
                    "code": "600001",
                    "name": "测试",
                    "market": 1,
                    "board": "sh_main",
                    "is_st": 0,
                }
            ]

        def fetch_trade_calendar_span(self, years):
            return ["2026-10-08"]

        def fetch_spot(self, secids=None, mode=None):
            return [
                {
                    "secid": "1.600001",
                    "code": "600001",
                    "ts": "2026-10-08 15:00:00",
                    "close": 10.0,
                    "volume": 100.0,
                    "amount": 1.0e5,
                    "source": "spot",
                }
            ]

    import emscan.cli as cli

    saved = cli.MairuiClient
    cli.MairuiClient = YesterdayClient  # type: ignore[assignment]
    try:
        assert main(["--config", str(config_path), "run"]) == 0
    finally:
        cli.MairuiClient = saved  # type: ignore[assignment]
    assert not list((tmp_path / "reports").glob("*.csv"))


def test_sync_universe_rejects_empty_list(tmp_path):
    """股票列表为空（证书失效/接口变更）要明确报错，而不是静默跑空。"""
    import pytest

    from emscan.mairui import MairuiError

    cfg = make_cfg(tmp_path)
    store = open_store(cfg)

    class EmptyClient:
        def fetch_universe(self):
            return []

    with pytest.raises(MairuiError):
        sync_universe(store, EmptyClient(), cfg, SyncStats())
    store.close()


def test_run_writes_daily_signal_cache(tmp_path):
    """跑完 scan + signals 后，daily_signals 里一行就带齐"四个主信号"和指标数值。"""
    store_path = tmp_path / "market.sqlite"
    cfg = make_cfg(
        tmp_path,
        {
            "store": {
                "path": str(store_path),
                "reports_dir": str(tmp_path / "reports"),
                "logs_dir": str(tmp_path / "logs"),
            }
        },
    )
    store = open_store(cfg)
    dates = trading_dates(40, end="2026-10-09")  # 要够 min_bars（默认 25 根）
    secid = "1.600001"
    rows = [bar(secid, d, 10000.0, close=10.0) for d in dates[:-1]]
    rows.append(bar(secid, dates[-1], 60000.0, close=10.0, pct_chg=6.0))  # 当日放量
    rows[-1]["low"] = 9.4
    rows[-1]["high"] = 10.5
    store.upsert_bars(rows)
    _seed_symbols(store, [secid])
    sync_indicators(
        store,
        FakeIndicatorClient(dates[-2:]),
        cfg,
        SyncStats(),
        [secid],
        dates[-2:],
        ["macd", "kdj", "boll"],
    )
    store.close()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "store": {
                    "path": str(store_path),
                    "reports_dir": str(tmp_path / "reports"),
                    "logs_dir": str(tmp_path / "logs"),
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    assert main(["--config", str(config_path), "scan"]) == 0
    assert main(["--config", str(config_path), "signals"]) == 0

    store = open_store(cfg)
    frame = store.load_daily_signals(join_symbols=True)
    store.close()
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["date"] == dates[-1]
    assert row["s_vol_spike"] == 1  # 突然放量
    assert row["s_macd_golden"] == 1  # MACD 金叉
    assert row["s_kdj_golden"] == 1  # KDJ 金叉
    assert row["s_boll_break_lower"] in (0, 1)  # BOLL 下轨突破（这里没跌破）
    assert row["vol_ratio_5"] > 2  # 放量指标也留了一份
    assert row["name"] == secid
    assert {"vol_spike", "macd_golden", "kdj_golden"} <= set(str(row["signals"]).split(","))


def test_hits_command_reads_cache(tmp_path):
    """`hits` 直接读缓存：默认看四个主信号（任一命中），支持 --only 组合筛选。"""
    store_path = tmp_path / "market.sqlite"
    config = {
        "store": {
            "path": str(store_path),
            "reports_dir": str(tmp_path / "reports"),
            "logs_dir": str(tmp_path / "logs"),
        }
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    cfg = make_cfg(tmp_path, config)
    store = open_store(cfg)
    store.upsert_symbols(
        [
            {
                "secid": "1.600001",
                "code": "600001",
                "name": "放量股",
                "market": 1,
                "board": "sh_main",
            },
            {
                "secid": "0.300002",
                "code": "300002",
                "name": "金叉股",
                "market": 0,
                "board": "sz_gem",
            },
            {
                "secid": "1.600003",
                "code": "600003",
                "name": "普通股",
                "market": 1,
                "board": "sh_main",
            },
        ]
    )
    date = "2026-10-09"
    store.upsert_daily_signals(
        [
            {"secid": "1.600001", "date": date, "close": 10.0, "amount": 1.0e8, "s_vol_spike": 1},
            {
                "secid": "0.300002",
                "date": date,
                "close": 20.0,
                "s_macd_golden": 1,
                "s_kdj_golden": 1,
            },
            {"secid": "1.600003", "date": date, "close": 30.0},
        ]
    )
    store.close()

    assert main(["--config", str(config_path), "hits"]) == 0  # 两个命中，一个默默无闻
    assert main(["--config", str(config_path), "hits", "--only", "macd_golden,kdj_golden"]) == 0
    assert main(["--config", str(config_path), "hits", "--secid", "600001", "--days", "10"]) == 0
    assert main(["--config", str(config_path), "hits", "--min-signals", "2"]) == 0
    out = tmp_path / "out" / "hits.csv"
    assert main(["--config", str(config_path), "hits", "--out", str(out)]) == 0
    frame = pd.read_csv(out)
    assert len(frame) == 2  # 放量股 + 金叉股
