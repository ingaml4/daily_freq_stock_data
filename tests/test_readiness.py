import datetime as dt

import pytest

from emscan import readiness
from emscan.store import Store
from helpers import make_cfg


DAY = "2026-11-09"


def test_date_coverage_never_substitutes_yesterdays_rows(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    with Store(tmp_path / "m.db").init_schema() as st:
        st.upsert_bars([{"secid": "1.600519", "date": "2026-11-06", "close": 10, "volume": 1}])
        monkeypatch.setattr(readiness, "now", lambda: dt.datetime.fromisoformat(DAY + "T17:16:00+08:00"))
        r = readiness.report(st, cfg, DAY)
        assert r["state"] == "waiting" and not r["ready"]
        assert r["stages"]["market"]["actual_date"] == "2026-11-06"
        assert not r["stages"]["benchmarks"]["ready"]
        assert not r["stages"]["indicators"]["ready"]
        with pytest.raises(ValueError):
            readiness.report(st, cfg, "2026-02-30")


def test_ma_coverage_uses_actual_eligible_bars_and_both_materialized_tables(tmp_path):
    sid = "1.600519"
    dates = [str(dt.date(2026, 10, 1) + dt.timedelta(days=i)) for i in range(20)]
    with Store(tmp_path / "m.db").init_schema() as st:
        st.upsert_bars([{"secid": sid, "date": d, "close": 10, "volume": 1} for d in dates])
        row = {"secid": sid, "date": dates[-1], "macd_diff": 1, "macd_dea": .5,
               "kdj_k": 30, "kdj_d": 10, "boll_mid": 10, "boll_lower": 8,
               "ma5": 10, "ma10": 10, "ma20": 10}
        for kind in ("macd", "kdj", "boll", "ma"):
            st.upsert_indicators(kind, [row])
        flags = dict(secid=sid, date=dates[-1], s_macd_golden=0, s_kdj_golden=0, s_boll_break_lower=0)
        st.upsert_daily_signals([flags])
        before = readiness._indicators(st.conn, dates[-1])
        assert before["missing"] == 1 and not before["ready"]
        st.upsert_daily_signals([{**flags, "ma5": 10, "ma10": 10, "ma20": 10}])
        after = readiness._indicators(st.conn, dates[-1])
        assert after["ready"] and after["moving_averages"]["ma60"]["insufficient_history"] == 1


def test_market_partial_snapshot_distinguishes_zero_volume_and_missing(tmp_path):
    from emscan.pipeline import SyncStats, sync_spot
    class Client:
        def fetch_spot(self, secids):
            return [dict(secid="1.600000", ts=DAY+" 15:00:00", close=10, volume=2),
                    dict(secid="1.600001", ts=DAY+" 15:00:00", close=0, volume=0),
                    dict(secid="1.600002", ts="2026-11-06 15:00:00", close=10, volume=2)]
    with Store(tmp_path / "m.db").init_schema() as st:
        pool = ["1.600000", "1.600001", "1.600002", "1.600003"]
        sync_spot(st, Client(), make_cfg(tmp_path), SyncStats(), pool, force=True)
        status = readiness._bars(st, DAY)
        assert status["no_trade"] == 1 and status["active_bars"] == 1
        assert status["missing"] == 2 and not status["ready"]
