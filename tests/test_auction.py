import json
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from emscan import mairui, request_usage
from emscan.auction import AuctionBookService, AuctionRequestError, SHANGHAI


@pytest.fixture(autouse=True)
def isolated_usage(monkeypatch):
    monkeypatch.setattr(request_usage, "_counts", defaultdict(Counter))


def at(hms="09:25:30", date="2026-11-10"):
    return datetime.fromisoformat(date + " " + hms).replace(tzinfo=SHANGHAI)


def quote(stamp="2026-11-10 09:25:00"):
    return {
        "pb": [65.5, 65.49, 65.48, 65.47, 65.46],
        "ps": [65.66, 65.67, 65.68, 65.69, 65.7],
        "vb": [17, 7, 30, 11, 22],
        "vs": [2, 1, 14, 2, 1],
        "t": stamp,
    }


def test_real_shape_time_units_and_conservative_auction_fields():
    seen = []

    def fetch(secid):
        seen.append(secid)
        return {**quote(), "credential_echo": "private"}

    service = AuctionBookService(fetch, clock=lambda: at().astimezone(timezone.utc))
    result = service.snapshot(["301502", "SZ301502", "0.301502", "301502.SZ"])
    assert seen == ["0.301502"] and len(result["items"]) == 1
    row = result["items"][0]
    assert row["phase"] == result["phase"] == "opening_result"
    assert row["source"] == "mairui" and row["source_endpoint"] == "hsstock.real.five"
    assert row["received_at"] == "2026-11-10T09:25:30.000000+08:00"
    assert row["source_time"] == "2026-11-10T09:25:00.000000+08:00"
    assert row["raw"] == quote() and row["book_quality"] == "ok" and row["quality"] == "gap"
    assert row["volume_unit"] == "lot" and row["shares_per_lot"] == 100
    assert row["matched_volume"] is row["unmatched_volume"] is row["unmatched_direction"] is None
    assert row["indicative_match_price"] is None and "private" not in json.dumps(result)
    assert (
        result["schema_version"] and result["cache_hits"] == 0 and result["upstream_requests"] == 1
    )


@pytest.mark.parametrize(
    "when,phase,active",
    [
        ("09:14:59", "preopen", False),
        ("09:15:00", "opening_cancelable", True),
        ("09:20:00", "opening_locked", True),
        ("09:30:00", "continuous_am", True),
        ("11:30:00", "continuous_am", True),
        ("11:30:01", "lunch", False),
        ("13:00:00", "continuous_pm", True),
        ("14:57:00", "closing_auction", True),
        ("15:00:00", "closing_auction", True),
        ("15:00:01", "closed", False),
    ],
)
def test_boundaries(when, phase, active):
    hits = []

    def fetch(secid):
        hits.append(secid)
        return quote("2026-11-10 " + when)

    result = AuctionBookService(fetch, clock=lambda: at(when)).snapshot(["SH603986"])
    row = result["items"][0]
    assert result["phase"] == row["phase"] == phase
    assert row["upstream_requested"] is active and len(hits) == int(active)
    assert row["book_quality"] == ("ok" if active else "gap")
    assert row["quality"] == "gap" and row["received_at"]


@pytest.mark.parametrize("stamp,trading", [(at(date="2026-11-08"), None), (at(), False)])
def test_known_closures_do_not_fetch(stamp, trading):
    service = AuctionBookService(lambda _: pytest.fail("unexpected request"), clock=lambda: stamp)
    row = service.snapshot(["301502"], is_trading_day=trading)["items"][0]
    assert row["phase"] == "non_trading_day" and row["reason"] == "market_closed"


def test_validate_whole_batch_before_sending_and_reject_non_a_shares():
    service = AuctionBookService(lambda _: pytest.fail("unexpected request"), clock=lambda: at())
    for bad in ([], ["SZ301502"] * 11, ["SZ301502", "../certificate"], [300969]):
        with pytest.raises(AuctionRequestError):
            service.snapshot(bad)
    items = service.snapshot(
        ["688001", "SH689009", "BJ920001", "SZ510300", "SH301502", "SH900901"]
    )["items"]
    assert all(row["received_at"] and not row["upstream_requested"] for row in items)
    assert (
        items[0]["reason"] == "excluded_star_market"
        and items[4]["reason"] == "market_code_mismatch"
    )


@pytest.mark.parametrize(
    "changed,quality,reason",
    [
        ({"t": "2026-11-09 09:25:00"}, "gap", "stale_source_time"),
        ({"t": "2026-11-10 09:25:35"}, "gap", "future_source_time"),
        ({"t": "2026-11-10 09:24:59"}, "gap", "source_phase_mismatch"),
        ({"t": "09:25:00"}, "gap", "invalid_source_time"),
        ({"vb": ["17", float("nan"), {}, -1, "secret"]}, "partial", "incomplete_levels"),
        ({"pb": [0] * 5, "ps": [0] * 5, "vb": [0] * 5, "vs": [0] * 5}, "gap", "no_quoted_levels"),
        ({"ps": [], "vs": []}, "partial", "incomplete_levels"),
        ({"ps": [0] * 5, "vs": [0] * 5}, "ok", None),
    ],
)
def test_bad_data(changed, quality, reason):
    row = AuctionBookService(lambda _: {**quote(), **changed}, clock=lambda: at()).snapshot(
        ["301502"]
    )["items"][0]
    assert (row["book_quality"], row["reason"]) == (quality, reason)
    assert row["quality"] == "gap" and "secret" not in json.dumps(row, allow_nan=False)


def test_recheck_age_and_phase_and_preserve_first_receipt_on_cache():
    now = [at("09:25:59")]
    svc = AuctionBookService(lambda _: quote(), clock=lambda: now[0])
    first = svc.snapshot(["301502"])["items"][0]
    now[0] += timedelta(seconds=2)
    second = svc.snapshot(["301502"])
    row = second["items"][0]
    assert row["book_quality"] == "gap" and row["reason"] == "stale_source_time"
    assert (
        row["received_at"] == first["received_at"] and row["cached"] and row["upstream_requested"]
    )
    assert second["upstream_requests"] == 0 and second["cache_hits"] == 1
    now[0] = at("09:30:01")
    row = svc.snapshot(["301502"])["items"][0]
    assert not row["cached"] and row["reason"] == "source_phase_mismatch"


def test_time_matches_receipt_and_revalidates_future_quotes_in_cache():
    now = [at("10:00:00")]

    def delayed(_):
        now[0] += timedelta(seconds=1)
        return quote("2026-11-10 10:00:01")

    svc = AuctionBookService(delayed, clock=lambda: now[0])
    row = svc.snapshot(["301502"])["items"][0]
    assert row["book_quality"] == "ok"
    svc = AuctionBookService(lambda _: quote("2026-11-10 10:00:02"), clock=lambda: now[0])
    assert svc.snapshot(["301502"])["items"][0]["reason"] == "future_source_time"
    now[0] += timedelta(seconds=2)
    assert svc.snapshot(["301502"])["items"][0]["reason"] == "future_source_time"


def test_overlapping_calls_and_failures_are_coalesced_without_secret_echo():
    entered, release = threading.Event(), threading.Event()
    hits = []

    def fetch(secid):
        hits.append(secid)
        entered.set()
        assert release.wait(2)
        raise ValueError("url/secret")

    svc = AuctionBookService(fetch, clock=lambda: at())
    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks = [pool.submit(svc.snapshot, ["301502"]) for _ in range(8)]
        assert entered.wait(2)
        release.set()
        rows = [t.result() for t in tasks]
    assert len(hits) == sum(r["upstream_requests"] for r in rows) == 1
    assert sum(r["cache_hits"] for r in rows) == 7
    assert all(r["items"][0]["reason"] == "upstream_error" for r in rows)
    assert "secret" not in json.dumps(rows)


def test_budget_limits_distinct_keys_and_expires():
    now = [at("09:30:10")]
    svc = AuctionBookService(lambda _: quote(), clock=lambda: now[0], per_minute=1)
    result = svc.snapshot(["301502", "300969"])
    assert (
        result["upstream_requests"] == 1
        and result["items"][1]["reason"] == "request_budget_exhausted"
    )
    assert not result["items"][1]["upstream_requested"]
    now[0] += timedelta(seconds=60)
    assert svc.snapshot(["300969"])["upstream_requests"] == 1


def test_book_only_sends_one_get_for_redirect_103_and_recovers(monkeypatch):
    hits, responses = [], [(302, {}), (429, 103), (200, quote())]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            status, body = responses.pop(0)
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/other/neverfollow")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = mairui.MairuiClient(
        "fixture", base_url=f"http://127.0.0.1:{server.server_port}", retries=9
    )
    now = [at()]
    mono = [1000.0]
    monkeypatch.setattr(mairui.time, "monotonic", lambda: mono[0])
    svc = AuctionBookService(client.fetch_order_book, clock=lambda: now[0])
    try:
        assert svc.snapshot(["301502"])["upstream_requests"] == 1
        assert svc.snapshot(["300969"])["upstream_requests"] == 1
        row = svc.snapshot(["600000"])
        assert row["upstream_requests"] == 0 and row["items"][0]["reason"] == "upstream_skipped"
        mono[0] += 61
        now[0] += timedelta(seconds=61)
        assert svc.snapshot(["600000"])["upstream_requests"] == 1
        assert hits == [
            f"/hsstock/real/five/{code}/fixture" for code in ("301502", "300969", "600000")
        ]
        assert request_usage.endpoint(hits[0], "") == "hsstock.real.five"
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("failure,minutes", [(101, 24 * 60 + 1), (102, 5), (429, 1)])
def test_web_client_circuits_expire_without_restart_and_single_timeout(
    monkeypatch, failure, minutes
):
    import requests
    from unittest.mock import Mock

    session = Mock()
    session.headers = {}
    blocked = requests.Response()
    blocked.status_code = 429
    blocked._content = str(failure).encode() if failure != 429 else b"{}"
    recovered = requests.Response()
    recovered.status_code = 200
    recovered._content = json.dumps(quote()).encode()
    session.get.side_effect = [blocked, recovered]
    client = mairui.MairuiClient("fixture", retries=7, timeout=50, session=session)
    tm = [0.0]
    monkeypatch.setattr(mairui.time, "monotonic", lambda: tm[0])
    with pytest.raises(mairui.MairuiQuotaError) as first:
        client.fetch_order_book("0.301502")
    with pytest.raises(mairui.MairuiQuotaError) as skipped:
        client.fetch_order_book("0.300969")
    assert first.value.upstream_requested and not skipped.value.upstream_requested
    tm[0] += minutes * 60
    assert client.fetch_order_book("0.300969") == quote()
    assert session.get.call_count == 2
    assert session.get.call_args.kwargs["timeout"] == 2.5
    assert session.get.call_args.kwargs["allow_redirects"] is False


def test_bulk_validity_is_evaluated_at_response_time_for_all_symbols():
    now = [at("09:25:59")]

    def fetch(secid):
        if secid == "0.300969":
            now[0] += timedelta(seconds=2)
        return quote()

    result = AuctionBookService(fetch, clock=lambda: now[0]).snapshot(["301502", "300969"])
    assert all(row["reason"] == "stale_source_time" for row in result["items"])
