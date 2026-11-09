"""同日指数涨跌和缺数据状态。"""
from datetime import datetime

import pytest

from emscan import benchmarks
from emscan.mairui import MairuiError
from emscan.store import Store


def test_sync_uses_exact_latest_only_and_keeps_other_indexes(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmarks, "now", lambda: datetime(2026, 11, 9, 17, 30))
    class Client:
        def fetch_benchmark_daily(self, dm, start, end, latest=False):
            if dm == "000688.SH":
                raise MairuiError("source outage")
            day = "2026-11-09" if latest and dm != "399006.SZ" else "2026-11-06"
            return [{"t": day, "c": 105.0, "pc": 100.0}]
    with Store(tmp_path / "index.sqlite").init_schema() as store:
        for _ in range(2):
            assert benchmarks.sync_benchmarks(store, Client(), "2026-11-06", "2026-11-09") == 2
        rows = list(store.conn.execute(
            "SELECT code,date,pct_chg,source FROM benchmark_bars ORDER BY code,date"))
        assert len(rows) == 5
        assert tuple(rows[1]) == ("000001", "2026-11-09", pytest.approx(5), "mairui.hsindex.latest.d")
        assert [r[1] for r in rows if r[0] == "399006"] == ["2026-11-06"]
        assert store.latest_date() is None


def test_invalid_index_quotes_are_not_flat_signals():
    items = [{"t":"2026-11-06","c":value,"pc":previous}
             for value, previous in [(100, None), (None, 1), ("NaN", 100), ("Infinity", 100), (12, 0)]]
    assert not benchmarks.parse_rows(items, "000001.SH", "2026-11-06", "2026-11-06", "test")


def test_current_intraday_bar_is_not_published(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmarks, "now", lambda: datetime(2026, 11, 9, 14, 30))
    with Store(tmp_path / "index.sqlite").init_schema() as store:
        with pytest.raises(ValueError):
            benchmarks.sync_benchmarks(store, None, "2026-11-09", "2026-11-09")
        assert store.conn.execute("SELECT count(*) FROM benchmark_bars").fetchone()[0] == 0
