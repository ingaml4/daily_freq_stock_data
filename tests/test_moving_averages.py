import pytest

from helpers import bar, make_cfg, trading_dates
from emscan.box import box_features
from emscan.cli import open_store
from emscan.indicators import compute_and_store
from emscan.moving_averages import PERIODS, write_days
from emscan.watchlist_risk import risk_report


CODE = "1.603986"


def test_same_day_box_cache_and_risk_match_sh603986(tmp_path):
    # 11月9日及此前59根实际入库收盘价，另加未来价格检查历史查询。
    closes = [
        403.03,
        402.29,
        413.17,
        404.5,
        417.48,
        444,
        436.74,
        403.71,
        403.5,
        409.06,
        381.66,
        395.51,
        392.5,
        411.2,
        396.82,
        406,
        393,
        388.86,
        383.2,
        371.88,
        385.44,
        385.96,
        381.87,
        380.64,
        371.32,
        356.88,
        363.11,
        373.14,
        369.88,
        388.18,
        391.71,
        402.1,
        398.72,
        383.77,
        358.51,
        367.57,
        353.9,
        334.4,
        326.46,
        310.84,
        306.22,
        292.56,
        278.22,
        281.86,
        282.44,
        282.43,
        288.02,
        276.75,
        292,
        276.45,
        268.98,
        289.88,
        275.99,
        257.8,
        265.67,
        250.16,
        271.38,
        263.15,
        239.49,
        238.41,
    ]
    days = trading_dates(60, "2026-11-09")
    cfg = make_cfg(tmp_path)
    assert "ma" in cfg.get("indicators.kinds")
    with open_store(cfg) as st:
        st.upsert_symbols(
            [{"secid": CODE, "code": "603986", "name": "兆易创新", "board": "sh_main"}]
        )
        st.upsert_bars(
            [dict(bar(CODE, d, 1000, close=c), source="spot") for d, c in zip(days, closes)]
            + [bar(CODE, "2026-11-10", 1000, close=999)]
        )
        st.upsert_daily_signals([{"secid": CODE, "date": days[-1], "s_box_range": 1}])
        assert (
            compute_and_store(st, [CODE], dates=days[-1:], kinds=cfg.get("indicators.kinds")) == 4
        )
        assert write_days(st, [CODE], days[-1:], update_cache=True) == 1
        before = st.conn.total_changes
        assert write_days(st, [CODE], days[-1:], update_cache=True) == 1
        assert before == st.conn.total_changes
        expected = [252.518, 262.091, 273.893, 348.672833333]
        for table in ("daily_signals", "indicators"):
            stored = st.conn.execute(
                f"select ma5,ma10,ma20,ma60 from {table} where secid=? and date=?", (CODE, days[-1])
            ).fetchone()
            assert list(stored) == pytest.approx(expected)
        ma = risk_report(st, days[-1], "SH603986")["rows"][0]["moving_averages"]
        for period, value in zip(PERIODS, expected):
            item = ma[f"ma{period}"]
            assert item["value"] == pytest.approx(value)
            assert item["sample_count"] == period and item["date"] == days[-1]
            assert item["position"] == "below" and item["price_basis"] == "as_stored"
            assert item["distance_pct"] == pytest.approx((238.41 / value - 1) * 100)
        box = box_features(st, cfg, days[-1]).iloc[0]
        assert box.box_ma_gap == pytest.approx(
            abs(ma["ma5"]["value"] - ma["ma20"]["value"]) / ma["ma20"]["value"]
        )
        assert st.conn.execute("select s_box_range from daily_signals").fetchone()[0] == 1


def test_ma_never_relabels_suspended_day_or_short_window(tmp_path):
    cfg = make_cfg(tmp_path)
    with open_store(cfg) as st:
        st.upsert_symbols([{"secid": CODE, "code": "603986", "board": "sh_main"}])
        days = trading_dates(8)
        st.upsert_bars([bar(CODE, d, 100 if i < 7 else 0, close=i + 1) for i, d in enumerate(days)])
        write_days(st, [CODE], days, update_cache=True)
        for d in (days[-2], days[-1]):
            ma = risk_report(st, d, "SH603986")["rows"][0]["moving_averages"]
            if d == days[-1]:
                assert all(
                    v["value"] is None and v["reason"] == "no_valid_bar" for v in ma.values()
                )
                continue
            assert ma["ma5"]["value"] == 5 and ma["ma5"]["position"] == "above"
            assert ma["ma10"]["value"] is None and ma["ma10"]["sample_count"] == 7
            assert ma["ma10"]["reason"] == "insufficient_ma_history"
        assert (
            st.conn.execute("select count(*) from indicators where date=?", (days[-1],)).fetchone()[
                0
            ]
            == 0
        )
        assert st.conn.execute("select count(*) from daily_signals").fetchone()[0] == 0
