from argparse import Namespace
from collections import Counter
from datetime import date, datetime, timedelta

import pytest

from emscan import benchmarks, cli, industry, pipeline, readiness
from emscan.history import fill_history
from emscan.mairui import MairuiClient, MairuiError, MairuiQuotaError
from emscan.request_cache import daily_lock
from emscan.store import Store
from tests.helpers import bar, make_cfg

DAY = "2026-11-09"


class Quotes(MairuiClient):
    def __init__(self):
        super().__init__("unused", multi_batch=20)
        self.count = Counter()
        self.stale = set()
        self.suspended = set()
        self.down = False

    def fetch_quote_multi(self, secids):
        self.count["multi"] += 1
        if self.down:
            raise MairuiError("unavailable")
        return [
            dict(
                secid=s,
                ts="2026-11-06" if s in self.stale else DAY,
                close=10,
                volume=0 if s in self.suspended else 100,
            )
            for s in secids
        ]

    def _fetch_quote_each(self, secids):
        self.count["single"] += len(secids)
        return []


def test_second_full_pool_run_keeps_coverage_and_requests_only_missing(tmp_path, monkeypatch):
    cfg = make_cfg(
        tmp_path, {"industry": {"enabled": False}, "mairui": {"fetch_instrument": False}}
    )
    pool = [f"1.{600000+i:06}" for i in range(5227)]
    client = Quotes()
    client.suspended = {pool[1]}
    monkeypatch.setattr(cli.MairuiClient, "from_config", lambda cfg: client)
    monkeypatch.setattr(cli, "tz_today", lambda: DAY)
    monkeypatch.setattr(pipeline, "tz_today", lambda: DAY)
    monkeypatch.setattr(pipeline, "now_hm", lambda: "17:25")
    monkeypatch.setattr(cli, "_window", lambda *a: (DAY, DAY))
    monkeypatch.setattr(client, "fetch_trade_calendar_span", lambda years: [DAY])
    monkeypatch.setattr(
        client, "fetch_universe", lambda: [dict(secid=s, code=s[2:], board="sh_main") for s in pool]
    )
    monkeypatch.setattr(cli, "_indicators_due", lambda *a: False)
    monkeypatch.setattr(cli, "_post_sync", lambda *a: 0)
    args = Namespace(limit=1)
    assert cli.cmd_run(cfg, args) == 0
    assert client.count == {"multi": 262}
    assert cli.cmd_run(cfg, args) == 0
    assert client.count == {"multi": 262}
    with cli.open_store(cfg) as st:
        assert readiness._bars(st, DAY)["ready"]
        import json

        row = st.conn.execute(
            "SELECT params FROM runs WHERE mode='spot_coverage' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        assert json.loads(row[0])["cached"] == 5227
        assert (
            st.conn.execute("SELECT COUNT(*) FROM runs WHERE mode='run_finished'").fetchone()[0]
            == 2
        )
        st.conn.execute("DELETE FROM bars WHERE secid=?", (pool[-1],))
        st.conn.commit()
        client.stale = {pool[-1]}
        pipeline.sync_spot(st, client, cfg, pipeline.SyncStats(), pool)
        assert client.count == {"multi": 263}
        assert readiness._bars(st, DAY)["missing"] == 1
        client.stale.clear()
        pipeline.sync_spot(st, client, cfg, pipeline.SyncStats(), pool)
        assert readiness._bars(st, DAY)["ready"]
        monkeypatch.setattr(pipeline, "now_hm", lambda: "15:32")
        pipeline.sync_spot(st, client, cfg, pipeline.SyncStats(), pool)
        assert client.count == {"multi": 526}


def test_spot_early_stop_and_no_quota_fanout(monkeypatch):
    client = Quotes()
    pool = [f"1.{600000+i}" for i in range(100)]
    client.down = True
    assert not client.fetch_spot(pool)
    assert client.count == {"multi": 2, "single": 40}

    def quota(s):
        raise MairuiQuotaError("429")

    monkeypatch.setattr(client, "fetch_quote_multi", quota)
    with pytest.raises(MairuiQuotaError):
        client.fetch_spot(pool)
    assert client.count["single"] == 40


def test_expiry_errors_manual_recheck_and_fairness(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    pool = ["1.600000", "1.600001"]
    dates = ["2026-10-28", DAY]

    class Client:
        calls = []
        error = False

        def fetch_kline_many(self, codes, **kw):
            self.calls.extend(codes)
            assert kw["end"] == DAY
            return (
                ({}, [(s, "failure") for s in codes])
                if self.error
                else ({s: [] for s in codes}, [])
            )

    client = Client()
    with Store(tmp_path / "cache.db").init_schema() as st:
        st.upsert_bars([bar(s, DAY, 10) for s in pool])

        def run(**kw):
            pipeline.sync_bars(st, client, cfg, pipeline.SyncStats(), pool, dates, **kw)

        client.error = True
        run(use_cache=True, max_symbols=1)
        run(use_cache=True, max_symbols=1)
        assert client.calls == pool
        client.error = False
        run(use_cache=True)
        run(use_cache=True)
        assert client.calls == pool * 2
        st.conn.execute("UPDATE sync_receipts SET expires=0")
        st.conn.commit()
        run(use_cache=True)
        run(use_cache=False)
        assert client.calls == pool * 4


def test_weekly_industry_partial_resumes_missing_node_only(tmp_path):
    cfg = make_cfg(tmp_path)

    class Client:
        calls = []

        def fetch_tag_tree(self, kinds):
            self.calls.append("tree")
            return [{"code": s, "name": s, "kind": "sw"} for s in ("sw_one", "sw_two")]

        def fetch_tag_members_many(self, nodes, **kw):
            self.calls.extend(n["code"] for n in nodes)
            code = "sw_one" if len(nodes) == 2 else "sw_two"
            return {code: [{"secid": "1.600000"}]}, []

    client = Client()
    with Store(tmp_path / "cache.db").init_schema() as st:
        st.replace_symbol_tags("sw", [{"code": "sw_old", "name": "old", "secid": "1.600000"}])
        with pytest.raises(MairuiError):
            industry.sync_tags(st, client, cfg, kinds=["sw"])
        assert st.symbol_tag_names("sw") == {"sw_old": "old"}
        industry.sync_tags(st, client, cfg, kinds=["sw"])
        assert client.calls == ["tree", "sw_one", "sw_two", "sw_two"]
        assert set(st.symbol_tag_names("sw")) == {"sw_one", "sw_two"}


def test_full_history_failed_stock_yields_to_next_and_new_listing(tmp_path):
    cfg = make_cfg(tmp_path)
    pool = ["1.600000", "1.600001", "1.600002"]

    class Client:
        calls = []

        def fetch_kline_many(self, codes, **kw):
            self.calls.extend(codes)
            if pool[0] in codes:
                return {}, [(pool[0], "failure")]
            return {s: [] for s in codes}, []

    client = Client()
    with Store(tmp_path / "cache.db").init_schema() as st:
        st.upsert_symbols(
            [
                dict(secid=s, code=s[2:], listed_date=DAY if i == 2 else None)
                for i, s in enumerate(pool)
            ]
        )
        st.upsert_bars([bar(s, DAY, 100) for s in pool])
        with pytest.raises(RuntimeError):
            fill_history(st, client, cfg, limit=1, cooldown_days=7)
        fill_history(st, client, cfg, limit=1, cooldown_days=7)
        fill_history(st, client, cfg, limit=1, cooldown_days=7)
        assert client.calls == pool[:2]


def test_incremental_benchmarks_latest_and_absent_only(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmarks, "now", lambda: datetime.fromisoformat(DAY + "T18:00:00"))

    class Client:
        calls = []

        def fetch_benchmark_daily(self, dm, start, end, latest=False):
            self.calls.append((dm, start, end, latest))
            return [{"t": end, "c": 100, "pc": 99}]

    client = Client()
    with Store(tmp_path / "cache.db").init_schema() as st:
        st.upsert_bars([bar("1.600000", DAY, 100)])
        for _ in range(2):
            assert benchmarks.sync_benchmarks(st, client, "2026-10-01", DAY, incremental=True) == 0
        assert client.calls == [(code, DAY, DAY, True) for code in benchmarks.INDEX_CODES]
        st.conn.execute("DELETE FROM benchmark_bars WHERE code='000001'")
        st.conn.commit()
        assert benchmarks.sync_benchmarks(st, client, "2026-10-01", DAY, incremental=True) == 0
        assert client.calls[4:] == [("000001.SH", DAY, DAY, True)]


def test_daily_lock_and_confirmed_holiday(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    with daily_lock(cfg.path("store.path")) as first:
        assert first
        assert cli.cmd_run(cfg, Namespace()) == 0
    with daily_lock(cfg.path("store.path")) as again:
        assert again
    holiday = "2026-11-08"
    days = [
        (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        for i in range(365)
        if (date(2026, 1, 1) + timedelta(days=i)).weekday() < 5 and i % 25 != 0
    ]

    class Calendar:
        calls = 0

        def fetch_trade_calendar_span(self, years):
            self.calls += 1
            return days

    client = Calendar()
    monkeypatch.setattr(cli.MairuiClient, "from_config", lambda cfg: client)
    monkeypatch.setattr(cli, "_window", lambda *a: ("2026-10-01", holiday))
    with cli.open_store(cfg) as st:

        class Partial:
            def fetch_trade_calendar_span(self, years):
                return ["2026-11-06", "2026-11-09"]

        closed = set()
        pipeline.resolve_trade_days(Partial(), holiday, holiday, cfg, store=st, known_closed=closed)
        assert not closed
    assert cli.cmd_run(cfg, Namespace()) == 0
    assert cli.cmd_run(cfg, Namespace()) == 0
    assert client.calls == 1


def test_history_repairs_invalid_row_but_preserves_valid_spot(tmp_path):
    cfg = make_cfg(tmp_path)
    yesterday, code = "2026-11-06", "1.600001"

    class Client:
        def fetch_kline_many(self, codes, **kwargs):
            return {code: [bar(code, yesterday, 20), bar(code, DAY, 50, close=300)]}, []

    with Store(tmp_path / "cache.db").init_schema() as st:
        st.upsert_bars([bar(code, yesterday, 0), dict(bar(code, DAY, 100), source="spot")])
        assert (
            pipeline.sync_bars(
                st, Client(), cfg, pipeline.SyncStats(), [code], [yesterday, DAY], use_cache=True
            )
            == 1
        )
        assert list(
            map(tuple, st.conn.execute("SELECT date,close,volume FROM bars ORDER BY date"))
        ) == [(yesterday, 10, 20), (DAY, 10, 100)]
