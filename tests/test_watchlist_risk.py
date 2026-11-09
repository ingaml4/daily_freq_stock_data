import base64
import json
import urllib.error
import urllib.request

import pytest

from helpers import bar, trading_dates
from emscan.store import Store
from emscan.watchlist_risk import InvalidRequest, risk_report
from emscan.web import serve


@pytest.fixture
def market(tmp_path):
    st = Store(tmp_path / "m.sqlite").init_schema()
    days = trading_dates(180, "2026-11-06")
    st.upsert_symbols(
        [
            {"secid": "1.600519", "code": "600519", "name": "测试", "board": "sh_main"},
            {"secid": "0.300001", "code": "300001", "board": "sz_gem"},
        ]
    )
    rows, macd = [], []
    for i, day in enumerate(days):
        b = bar(
            "1.600519", day, 150 if day == days[-1] else 100, pct_chg=-3 if day == days[-1] else -1
        )
        b["high"] = 15 if day == days[-3] else 13 if day == days[-11] else 11
        rows.append(b)
        dif = 2 if day == days[-11] else -1 if day == days[-1] else 1
        macd.append({"secid": "1.600519", "date": day, "macd_diff": dif, "macd_dea": 0.0})
    st.upsert_bars(rows)
    st.upsert_indicators("macd", macd)
    st.conn.execute(
        "CREATE TABLE IF NOT EXISTS benchmark_bars(date TEXT,code TEXT,close REAL,"
        "pct_chg REAL,source TEXT,PRIMARY KEY(code,date))"
    )
    st.conn.executemany(
        "INSERT INTO benchmark_bars(date,code,close,pct_chg,source) VALUES(?,?,?,?,?)",
        [(d, "000001", 3000, 1.0, "test-index") for d in days],
    )
    st.conn.commit()
    yield st, days
    st.close()


def test_all_six_and_peak_confirmation(market):
    st, days = market
    result = risk_report(st, days[-1], "SH600519")
    row = result["rows"][0]
    assert row["stock_return_pct"] == -3 and row["relative_pp"] == -4
    assert row["benchmark"]["code"] == "000001" and row["benchmark"]["return_pct"] == 1.0
    assert row["data_date"] == days[-1]
    assert all(v["status"] == "triggered" for v in row["signals"].values())
    peak = row["signals"]["macd_bearish_divergence"]
    assert peak["evidence"]["first_peak_date"] == days[-11]
    assert peak["trigger_date"] == days[-3] and peak["confirmation_date"] == days[-1]
    assert peak["evidence"]["second_high"] == 15 and peak["evidence"]["second_dif"] == 1
    assert row["signals"]["macd_dead_cross"]["new_trigger"] is True
    assert row["signals"]["market_up_stock_down"]["new_trigger"] is False
    assert row["signals"]["high_volume_drop"]["evidence"]["volume_ratio"] == 1.5
    assert [r["date"] for r in row["recent"]] == days[-5:]
    before = risk_report(st, days[-2], "600519.SH")["rows"][0]
    assert before["signals"]["macd_bearish_divergence"]["status"] == "not_triggered"
    assert before["signals"]["relative_underperformance"]["status"] == "triggered"
    assert before["relative_pp"] == -2
    assert before["recent"][-1]["date"] == days[-2]


def test_missing_data_and_order(market):
    st, days = market
    st.conn.execute("DELETE FROM benchmark_bars WHERE date=?", (days[-1],))
    st.conn.execute("DELETE FROM indicators WHERE date=?", (days[-11],))
    st.conn.commit()
    inputs = ["1.600519", "SZ300001", "US.TSLA", "SZ000999", "oops", "1.600519"]
    rows = risk_report(st, days[-1], ",".join(inputs))["rows"]
    assert [r["requested_symbol"] for r in rows] == inputs
    assert rows[0] == rows[-1]
    assert (
        rows[0]["signals"]["macd_bearish_divergence"]["evidence"]["indicator_source"]
        == "recomputed_from_local_bars"
    )
    assert rows[0]["signals"]["relative_underperformance"]["status"] == "unavailable"
    assert rows[0]["signals"]["macd_dead_cross"]["status"] == "triggered"
    assert rows[1]["reason"] == "no_valid_bar" and rows[1]["benchmark"]["code"] == "399006"
    assert [r["reason"] for r in rows[2:5]] == [
        "unsupported_market",
        "unknown_symbol",
        "invalid_symbol",
    ]
    for r in rows[1:5]:
        assert all(v["status"] == "unavailable" for v in r["signals"].values())
    assert (
        risk_report(st, days[2], "SH600519")["rows"][0]["signals"]["macd_dead_cross"]["status"]
        == "unavailable"
    )


def test_stale_and_pre_history(market):
    st, days = market
    row = risk_report(st, "2026-11-07", "SH600519")["rows"][0]
    assert row["data_date"] == days[-1] and row["reason"] == "market_data_not_yet_available"
    assert all(v["status"] == "unavailable" for v in row["signals"].values())
    assert row["recent"][-1]["signals"]["macd_dead_cross"]["status"] == "triggered"
    old = risk_report(st, "1990-01-01", "SH600519")["rows"][0]
    assert (
        old["data_date"] is None
        and not old["recent"]
        and old["reason"] == "no_market_data_for_date"
    )
    st.conn.execute("UPDATE bars SET volume=0 WHERE date=?", (days[-1],))
    st.conn.commit()
    halted = risk_report(st, days[-1], "SH600519")["rows"][0]
    assert halted["reason"] == "no_valid_bar" and halted["data_date"] == days[-2]
    assert halted["bar"] is None


@pytest.mark.parametrize(
    "d,s",
    [
        ("invalid", "SH600519"),
        ("2026-02-30", "SZ000001"),
        ("2026-11-06", ""),
        ("2026-11-06", ",".join(["SH600519"] * 101)),
    ],
)
def test_invalid_request(market, d, s):
    st, _ = market
    with pytest.raises(InvalidRequest):
        risk_report(st, d, s)


def test_http_auth_and_bad_date(market):
    st, days = market
    httpd = serve(lambda: Store(st.path), host="127.0.0.1", port=0, password="test", blocking=False)
    base = f"http://127.0.0.1:{httpd.server_port}/api/watchlist-risk"
    try:
        url = f"{base}?date={days[-1]}&symbols=SH600519"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url, timeout=5)
        assert e.value.code == 401
        headers = {"Authorization": "Basic " + base64.b64encode(b":test").decode()}
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=5
        ) as resp:
            payload = json.load(resp)
        assert payload["rows"][0]["signals"]["macd_dead_cross"]["status"] == "triggered"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base, headers=headers), timeout=5)
        assert e.value.code == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_dual_benchmarks_distinguish_general_and_opposite_lag(market):
    st, days = market
    day = days[-1]
    st.upsert_bars([bar("0.300001", day, 100, pct_chg=-1)])
    st.conn.execute(
        "INSERT INTO benchmark_bars(code,date,pct_chg,close,source) VALUES(?,?,?,?,?)",
        ("399006", day, -0.1, 1500, "test-index"),
    )
    st.conn.commit()
    row = risk_report(st, day, "SZ300001")["rows"][0]
    m, sh = row["benchmarks"]["market"], row["benchmarks"]["shanghai"]
    assert m["code"] == "399006" and m["relative_pp"] == -0.9
    assert sh["code"] == "000001" and sh["relative_pp"] == -2
    assert m["signals"]["market_up_stock_down_significant"]["status"] == "not_triggered"
    assert sh["signals"]["market_up_stock_down_significant"]["status"] == "triggered"
    st.conn.execute("UPDATE bars SET pct_chg=1 WHERE secid='0.300001' AND date=?", (day,))
    st.conn.execute("UPDATE benchmark_bars SET pct_chg=3 WHERE code='399006' AND date=?", (day,))
    st.conn.commit()
    m = risk_report(st, day, "SZ300001")["rows"][0]["benchmarks"]["market"]
    assert m["signals"]["relative_underperformance"]["status"] == "triggered"
    assert m["signals"]["market_up_stock_down_significant"]["status"] == "not_triggered"


def test_peak_spacing_and_history(market):
    st, days = market
    # A close intervening peak would defeat the divergence if it were wrongly used.
    st.conn.execute("UPDATE bars SET high=99 WHERE date=?", (days[-6],))
    st.conn.commit()
    peak = risk_report(st, days[-1], "SH600519")["rows"][0]["signals"]["macd_bearish_divergence"]
    assert peak["status"] == "triggered" and peak["evidence"]["peak_gap_sessions"] == 8
    day = days[99]
    flags = risk_report(st, day, "SH600519")["rows"][0]["signals"]
    assert flags["macd_dead_cross"]["reason"] == "insufficient_macd_history"
    assert flags["macd_dead_cross"]["evidence"]["valid_bars"] == 100
    assert flags["persistent_decline"]["status"] == "not_triggered"  # five -1% compound to -4.901%


def test_matched_adjusted_returns_ignore_other_days_and_defend_gaps(market):
    st, days = market
    st.conn.execute("DELETE FROM bars WHERE date=?", (days[-2],))
    st.conn.commit()
    row = risk_report(st, days[-1], "SH600519")["rows"][0]
    assert row["signals"]["macd_dead_cross"]["status"] == "unavailable"
    assert row["signals"]["high_volume_drop"]["status"] == "unavailable"
    assert row["signals"]["persistent_decline"]["status"] == "unavailable"
    assert row["signals"]["macd_bearish_divergence"]["status"] == "unavailable"
    assert row["signals"]["market_up_stock_down"]["status"] == "triggered"
