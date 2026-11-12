import json
from datetime import datetime

import pytest
import requests

from emscan.market_mainline import materialize, report
from emscan.store import Store
from emscan.web import serve
from helpers import bar

DAYS = [
    "2026-11-03",
    "2026-11-04",
    "2026-11-05",
    "2026-11-06",
    "2026-11-09",
    "2026-11-10",
    "2026-11-11",
]
DAY = DAYS[-1]
CODES = [f"1.600{i:03d}" for i in range(29)] + ["1.688001"]
MEMBERS = CODES[:11] + [CODES[-1]]


def coverage(st, day, expected=len(CODES), observed="17:16:00"):
    run = st.record_run(
        "spot_coverage",
        trade_date=day,
        params={"universe": expected, "active": len(CODES), "no_trade": 0, "source": "fixture"},
    )
    with st.conn:
        st.conn.execute("UPDATE runs SET finished_at=? WHERE run_id=?", (f"{day} {observed}", run))


def tag(st, kind, code, name, members, stamp="2026-11-03 17:20:00"):
    with st.conn:
        st.conn.executemany(
            "INSERT INTO symbol_tags VALUES (?,?,?,?,?)",
            [(sid, kind, code, name, stamp) for sid in members],
        )


@pytest.fixture
def market(tmp_path):
    with Store(tmp_path / "market.sqlite").init_schema() as st:
        st.upsert_symbols(
            [
                {"secid": sid, "code": sid.split(".")[1], "name": f"样本 {i}", "market": 1}
                for i, sid in enumerate(CODES)
            ]
        )
        st.upsert_bars(
            [
                bar(
                    sid,
                    day,
                    1000,
                    pct_chg=(
                        8.0
                        if sid == CODES[-1] and j >= 4
                        else 2.0 if sid in MEMBERS and j >= 4 else 0.0
                    ),
                    amount=(160e6 + i * 1e6) if sid in MEMBERS and j >= 4 else 100e6,
                )
                for j, day in enumerate(DAYS)
                for i, sid in enumerate(CODES)
            ]
        )
        for day in DAYS[-2:]:
            coverage(st, day)
        tag(st, "sw", "power", "设备", MEMBERS)
        tag(st, "gn", "overlap", "相同成分概念", MEMBERS)
        yield st


def test_broad_line_and_overlap_keep_one_rank_and_permission_filtered_anchors(market):
    total = market.conn.total_changes
    result = report(market, DAY)
    assert market.conn.total_changes == total
    assert result["publication_ready"] and result["status"] == "ready"
    assert len(result["mainlines"]) == 1
    line = result["mainlines"][0]
    assert len(line["overlapping_groups"]) == 1
    evidence = line["evidence"]
    assert evidence["rising"] == evidence["member_count"] == 12
    assert evidence["follower_rising"] == 9
    assert evidence["strong_sessions_5d"] == 3 and evidence["amount_ratio_5d"] > 1.1
    assert result["benchmark"]["median_pct_chg"] == 0
    assert len({a["secid"] for a in line["anchors"]}) == 3
    assert all(not a["symbol"].startswith("SH688") for a in line["anchors"])
    assert result["previous"]["items"][0]["status"] == "continuing"


def test_ready_market_may_have_no_line_and_marks_previous_fade(market):
    with market.conn:
        market.conn.execute(
            "UPDATE bars SET pct_chg=-2 WHERE date=? AND secid IN "
            "(SELECT secid FROM symbol_tags WHERE kind='sw')",
            (DAY,),
        )
        market.conn.execute("DELETE FROM symbol_tags WHERE kind='gn'")
    result = report(market, DAY)
    assert result["publication_ready"] and result["status"] == "degraded"
    assert result["mainlines"] == []
    assert "concept_membership_unavailable" in result["gaps"]
    assert result["previous"]["items"][0]["status"] == "fading"


@pytest.mark.parametrize("expected,observed", [(31, "17:16:00"), (30, "14:10:00")])
def test_missing_symbols_or_intraday_snapshot_block_publication(market, expected, observed):
    coverage(market, DAY, expected, observed)
    result = report(market, DAY)
    assert not result["publication_ready"] and not result["mainlines"]
    assert result["previous"]["items"][0]["status"] == "pending"


def test_day_without_data_never_substitutes_prior_results(market):
    result = report(market, "2026-11-12")
    assert result["date"] == "2026-11-12"
    assert result["market"]["actual_date"] == DAY
    assert result["mainlines"] == [] and not result["publication_ready"]


def test_cached_calendar_does_not_skip_an_entire_missing_session(market):
    missing = DAYS[-3]
    with market.conn:
        market.conn.execute("DELETE FROM bars WHERE date=?", (missing,))
        market.conn.execute("DELETE FROM trading_days WHERE date=?", (missing,))
    market.set_meta("mairui_calendar:2026", json.dumps({"days": DAYS}))
    result = report(market, DAY)
    assert missing in result["coverage"]["history_dates"]
    assert "recent_sessions_incomplete" in result["gaps"]
    assert not result["publication_ready"]


def test_archive_retains_published_evidence_until_new_coverage(market):
    assert materialize(market, DAY)
    result = report(market, DAY)
    assert result["publication"]["state"] == "archived"
    with market.conn:
        market.conn.execute("UPDATE bars SET pct_chg=0 WHERE date=?", (DAY,))
    assert report(market, DAY)["mainlines"] == result["mainlines"]
    coverage(market, DAY, observed="17:17:00")
    assert report(market, DAY)["publication"]["state"] == "computed"
    assert report(market, DAY)["mainlines"] == []


def test_versioned_membership_for_historical_day_after_refresh(market, monkeypatch):
    monkeypatch.setattr(
        "emscan.tzutil.now", lambda: datetime.fromisoformat("2026-11-12T17:17:00+08:00")
    )
    market.replace_symbol_tags("sw", [{"secid": s, "code": "new", "name": "新行业"} for s in CODES])
    result = report(market, DAY)
    assert result["mappings"]["sw"]["observed_at"].startswith("2026-11-03")
    assert result["mainlines"][0]["name"] in {"设备", "相同成分概念"}
    assert report(market, "2026-11-02")["mappings"]["sw"]["nodes"] == 0


def test_http_market_mainline_uses_existing_auth_and_exact_date(market):
    server = serve(
        lambda: Store(market.path), host="127.0.0.1", port=0, password="test", blocking=False
    )
    url = f"http://127.0.0.1:{server.server_port}/api/market-mainline"
    auth = ("", "test")
    try:
        assert requests.get(url, timeout=5).status_code == 401
        for query in ("date=2026-02-30", "date=", f"date={DAY}&date={DAY}", "limit=99"):
            assert requests.get(f"{url}?{query}", auth=auth, timeout=5).status_code == 400
        historical = requests.get(url, params={"date": DAYS[-2]}, auth=auth, timeout=5).json()
        assert historical["date"] == DAYS[-2] and historical["publication_ready"]
        assert requests.get(url, auth=auth, timeout=5).json()["date"] == DAY
    finally:
        server.shutdown()
        server.server_close()
