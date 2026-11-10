from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import Mock
import threading

import pytest

from emscan.auction import SHANGHAI
from emscan.mairui import MairuiClient
from emscan.request_usage import endpoint
from emscan.trade_ticks import TradeTicksService, TradeTicksRequestError


def at(hms="14:42:46"):
    return datetime.fromisoformat("2026-11-10 " + hms).replace(tzinfo=SHANGHAI)


def trade(t, p=72.18, v=114200, ts=1, d="2026-11-10"):
    return dict(d=d, t=t, p=p, v=v, ts=ts)


def snapshot(rows, clock=lambda: at()):
    return TradeTicksService(lambda _: rows, clock=clock).snapshot(["SZ301502"])["items"][0]


def test_delayed_real_examples_amount_and_direction_preserve_order_and_duplicate_prints():
    earlier = trade("10:11:54")
    later = trade("14:41:09", p="8.12", v=200, ts=2)
    row = snapshot([earlier, later, earlier, trade("09:30:00", p="94.80", v=110800, ts=0)])
    ticks = row["ticks"]
    assert [x["source_index"] for x in ticks] == [0, 1, 2, 3]
    assert [x["amount_yuan"] for x in ticks] == [8242956, 1624, 8242956, 10503840]
    assert [x["side"] for x in ticks] == ["buy", "sell", "buy", "unknown"]
    assert all(x["source_trade_id"] is None and x["coverage_gap"] for x in ticks)
    assert row["source_time"].startswith("2026-11-10T14:41:09") and row["source_age_seconds"] == 97
    assert row["quality"] == "gap" and row["reason"] == "stale_source_time"
    assert (
        row["coverage_gap"]
        and not row["realtime_verified"]
        and row["source_update_interval_minutes"] == 60
    )
    assert row["received_at"] != row["source_time"]


def test_recent_tick_cannot_verify_coverage_and_no_side_inference():
    row = snapshot([trade("14:42:44", ts=0), trade("14:42:43", ts=87)])
    assert row["quality"] == "partial" and row["coverage_gap"] and not row["realtime_verified"]
    assert row["ticks"][0]["quality"] == "reported" and row["ticks"][0]["side"] == "unknown"
    assert row["ticks"][1]["quality"] == "gap" and row["ticks"][1]["side"] == "unknown"


def test_match_separated_from_preauction_missing_dates_and_future_rows_not_promoted():
    row = snapshot(
        [
            trade("09:15:00"),
            trade("09:25:00"),
            trade("14:52:01"),
            trade("14:42:45", p=-1),
            trade("14:42:42", d=None),
            trade("14:42:01", v=1.5),
            trade("12:01:00"),
        ]
    )
    assert [x["is_reported_trade"] for x in row["ticks"]] == [
        False,
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    assert row["ticks"][0]["reason"] == "auction_execution_unverified"
    assert row["ticks"][1]["session"] == "opening_match" and row["ticks"][4]["trade_at"] is None
    assert row["source_time"].startswith("2026-11-10T09:25:00")
    close = snapshot([trade("14:58:00"), trade("15:00:00")], clock=lambda: at("15:00:00"))
    assert [x["is_reported_trade"] for x in close["ticks"]] == [False, True]
    assert close["ticks"][1]["session"] == "closing_match"


@pytest.mark.parametrize(
    "payload,reason",
    [(None, "invalid_response"), ([], "empty_source_fragment"), ([{}], "no_valid_reported_trade")],
)
def test_missing_data(payload, reason):
    row = snapshot(payload)
    assert row["quality"] == "gap" and row["source_time"] is None and row["reason"] == reason


def test_cache_batch_and_clock_rechecks_stale_without_new_request():
    tm = [at("14:42:00")]
    fetch = Mock(return_value=[trade("14:41:01")])
    svc = TradeTicksService(fetch, clock=lambda: tm[0], per_minute=1)
    first = svc.snapshot(["301502", "0.301502", "SH688001"])
    assert first["upstream_requests"] == 1 and first["items"][0]["quality"] == "partial"
    assert first["items"][1]["reason"] == "excluded_star_market"
    tm[0] += timedelta(seconds=3)
    second = svc.snapshot(["301502", "300969"])
    assert second["cache_hits"] == 1 and second["upstream_requests"] == 0
    assert second["items"][0]["received_at"] == first["items"][0]["received_at"]
    assert (
        second["items"][0]["quality"] == "gap"
        and second["items"][1]["reason"] == "request_budget_exhausted"
    )
    with pytest.raises(TradeTicksRequestError):
        svc.snapshot(["301502"] * 11)
    assert fetch.call_count == 1


def test_concurrent_requests_share_one_fragment_even_on_error():
    enter, release = threading.Event(), threading.Event()
    calls = []

    def fetch(secid):
        calls.append(secid)
        enter.set()
        assert release.wait(2)
        raise RuntimeError("never-print-secret")

    svc = TradeTicksService(fetch, clock=at)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(svc.snapshot, ["301502"]) for _ in range(4)]
        assert enter.wait(2)
        release.set()
        rows = [job.result() for job in jobs]
    assert sum(r["upstream_requests"] for r in rows) == len(calls) == 1
    assert all(r["items"][0]["reason"] == "upstream_error" for r in rows)
    assert "never-print-secret" not in str(rows)


def test_client_uses_measured_one_attempt_transport():
    client = MairuiClient("test-only", base_url="https://fixture.invalid")
    try:
        client._get_json = Mock(return_value=[])
        assert client.fetch_trade_ticks("0.301502") == []
        url = "https://fixture.invalid/hsrl/zbjy/301502/test-only"
        client._get_json.assert_called_once_with(url, one_attempt=True)
        assert endpoint(url, client.base_url) == "hsrl.zbjy"
    finally:
        client.close()
