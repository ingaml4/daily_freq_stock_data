from emscan.entry_backup_export import convert
from test_entry_minute_study import CANDIDATE, SYMBOL, D1, replay, stamp


def poll(received, *, requested=None, source_at=None, ok=True, parsed=True):
    row = {"symbol":SYMBOL, "source_at":stamp(source_at or received), "price":10.2,
           "previous_close":10., "ask_price":10.2, "ask_shares":100000,
           "bid_price":10.19, "quantity_verified":True}
    return {"capture_schema":2, "type":"poll", "source":"tencent", "trade_date":D1,
            "requested_at":stamp(requested or received), "received_at":stamp(received),
            "requested_symbols":[SYMBOL], "transport_success":ok,
            "quotes":{SYMBOL:row} if parsed else {},
            "quote_usability":{SYMBOL:"fresh_displayed_ask"}, "raw_ref":"test:1"}


def test_successfully_observed_old_quote_then_fresh_quote_roundtrips_v2():
    rows = [poll("09:29:59"), poll("09:30:01", requested="09:30:00", source_at="09:29:59"),
            poll("09:30:03", requested="09:30:02", source_at="09:30:02")]
    rows[1]["quote_usability"][SYMBOL] = "outside_continuous_session"
    quotes, minutes = convert(rows, D1)
    out = replay(quotes, candidate={**CANDIDATE, "sampling_coverage_version":2})
    assert out["orders"]["O"]["buy"]["price"] == 10.2
    assert out["orders"]["O"]["buy"]["evidence"]["source"] == "tencent/server"
    assert len(minutes) == 0
    assert all(x["sampling_coverage_version"] == 2 for x in quotes if x["type"] == "coverage")


def test_restart_gap_is_preserved_and_not_converted_to_observation():
    quotes, _ = convert([poll("09:30:07", requested="09:30:06")], D1)
    gaps = [x for x in quotes if x["type"] == "coverage" and not x["complete"]]
    assert gaps[0]["start"] == stamp("09:30:00")
    assert gaps[0]["end"] == stamp("09:30:06")
    out = replay(quotes, candidate={**CANDIDATE, "sampling_coverage_version":2})
    assert out["orders"]["O"]["buy"] is None


def test_timed_out_or_missing_symbol_is_not_complete():
    for kwargs in ({"ok":False,"parsed":False}, {"parsed":False}):
        quotes, _ = convert([poll("09:30:01", requested="09:30:00", **kwargs)], D1)
        assert not quotes[0]["complete"]


def test_server_minutes_are_separate_stream():
    row = {"capture_schema":2, "type":"minute_poll", "source":"tencent-minute", "trade_date":D1,
           "requested_at":stamp("09:31:03"), "received_at":stamp("09:31:04"), "raw_ref":"test:2",
           "minutes":[{"symbol":SYMBOL, "label":stamp("09:30:00"), "end":stamp("09:31:00"),
                       "close":10.2, "low":None, "complete":True, "received_at":stamp("09:31:04")}]}
    quotes, minutes = convert([row], D1)
    assert quotes == []
    assert minutes[0]["source"] == "tencent-minute/server"
