import json
from datetime import datetime

import pytest

from emscan import execution
from emscan.entry_minute_study import read_events, replay_candidate, summarize
from emscan.minute_replay import MINUTE, SESSIONS, SHANGHAI

D = "2026-11-12"
D1 = "2026-11-13"
D5 = "2026-11-19"
SYMBOL = "SZ000001"
DAYS = [
    "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", D,
    D1, "2026-11-16", "2026-11-17", "2026-11-18", D5, "2026-11-20",
]
CANDIDATE = {
    "date": D, "secid": "0.000001", "symbol": SYMBOL, "mode": "C", "name": "平安银行",
    "trigger": 10.1, "risk": 9.6, "first_seen": DAYS[0], "generated_at": D + "T16:30:00+08:00",
    "signal_bar": {"close": 10, "source": "spot"},
    "mainlines": [{"key": "bank"}],
}


def stamp(clock, day=D1):
    return f"{day}T{clock}+08:00"


def event(kind, received, **fields):
    return {
        "schema_version": 1, "type": kind, "trade_date": D1, "symbol": SYMBOL,
        "source": "verified_feed", "received_at": stamp(received), **fields,
    }


def minute(label, close, received=None, **fields):
    end = datetime.fromisoformat(stamp(label)) + MINUTE
    return event("minute", received or end.time().isoformat(), label=stamp(label),
                 end=end.isoformat(), close=close, complete=True, **fields)


def quote(source, received, ask, shares=100_000, **fields):
    return event("quote", received, source_at=stamp(source), ask_price=ask, ask_shares=shares,
                 quantity_verified=fields.pop("quantity_verified", True), **fields)


def coverage(start, end, received=None, complete=True, **fields):
    return event("coverage", received or end, start=stamp(start), end=stamp(end),
                 scope="quote", complete=complete, **fields)


def auction(shares=None, price=10.19):
    return event("opening", "09:25:03", source_at=stamp("09:25:00"), open_price=price,
                 auction_verified=True, auction_shares=shares, quantity_verified=shares is not None)


def raw(close, ref):
    return {"source": "spot", "open": close, "high": close, "low": close, "close": close,
            "volume": 100_000, "chg": round(close - ref, 2),
            "pct_chg": round((close / ref - 1) * 100, 2)}


def prices():
    return {D: raw(10, 10), D1: raw(10.3, 10),
            **{day: raw(10.3, 10.3) for day in DAYS[7:]}}


def opening_events():
    return [quote("09:30:02", "09:30:03", 10.2), coverage("09:30:00", "09:30:04")]


def confirmation_events(second_low=None):
    return [minute("09:30:00", 10.2, "09:31:02"),
            minute("09:31:00", 10.3, "09:32:04", low=second_low)]


def quote_events():
    return [quote("09:32:04", "09:32:05", 10.25, raw_ref="quote:5"),
            coverage("09:32:04", "09:32:06")]


def replay(rows, as_of=stamp("09:35:00"), *, series=None, candidate=CANDIDATE):
    return replay_candidate(candidate, rows, DAYS, prices() if series is None else series, as_of=as_of)


def test_first_visible_complete_minutes_and_post_confirmation_ask():
    events = opening_events() + confirmation_events() + quote_events() + [
        minute("09:31:00", 10.0, "09:31:59"),  # Still forming.
        minute("09:31:00", 10.0, "09:32:09"),  # Later revision.
        quote("09:32:03", "09:32:04", 10.01),
        quote("09:32:03", "09:32:04.500", 10.02),  # Source predates confirmation.
        auction(),
    ]
    result = replay(reversed(events))
    o, r = (result["orders"][arm] for arm in ("O", "R"))
    assert result["candidate_key"] == [D, "0.000001", "C"]
    assert o["buy"]["price"] == 10.2
    assert r["confirmation"]["at"] == stamp("09:32:04")
    assert r["confirmation"]["minutes"][1]["close"] == 10.3
    assert r["buy"]["price"] == 10.25
    assert r["buy"]["evidence"]["raw_ref"] == "quote:5"
    assert r["buy"]["evidence"]["source"] == o["buy"]["evidence"]["source"]
    assert r["path"]["type"] == "direct_hold"
    assert result["opening_auction"]["observed_opening"]["open_price"] == 10.19
    assert result["opening_auction"]["buy"] is None
    assert o["status"] == r["status"] == "pending_exit"
    assert r["sell"] is r["net_return"] is None


def test_retest_after_open_and_quantity_verified_auction_remain_separate():
    events = [auction(shares=100_000)] + opening_events() + confirmation_events(10.1) + quote_events()
    result = replay(events)
    assert result["opening_auction"]["buy"]["price"] == 10.19
    assert result["orders"]["O"]["buy"]["price"] == 10.2
    assert result["orders"]["R"]["path"] == {
        "type": "retest_then_hold", "basis": "minute_close_and_available_low"
    }
    stats = summarize([result])
    assert stats["paired"]["both_entries"] == 1
    assert stats["confirmation_paths"] == {"retest_then_hold": 1}
    assert stats["book_source_pairs"] == {"same_source": 1, "different_sources": 0}


def test_as_of_cuts_off_quotes_coverage_and_daily_exits():
    events = opening_events() + confirmation_events() + quote_events()
    before = replay(events, as_of=stamp("09:32:04"))
    assert before["orders"]["R"]["status"] == "pending_entry"
    before_coverage = replay(events, as_of=stamp("09:32:05"))
    assert before_coverage["orders"]["R"]["coverage_gap"]
    assert before_coverage["orders"]["R"]["buy"] is None
    before_d5_close = replay(events, as_of=stamp("14:59:59", D5))
    assert before_d5_close["orders"]["R"]["target_date"] == D5
    assert before_d5_close["orders"]["R"]["sell"] is None


def test_same_d5_exit_and_costs_for_matched_orders():
    events = opening_events() + confirmation_events() + quote_events()
    result = replay(events, as_of=stamp("15:05:00", D5))
    o, r = (result["orders"][arm] for arm in ("O", "R"))
    for order in (o, r):
        assert order["status"] == "closed"
        assert order["eligible_for_validation"]
        assert order["sell"]["date"] == D5
        assert order["sell"]["price"] == 10.3
        assert order["net_return"] == execution._settle(
            order["buy"], D5, 10.3, execution.DEFAULTS
        )["net_return"]
    assert summarize([result])["paired"]["both_closed"] == 1
    gapped_series = prices()
    del gapped_series["2026-11-17"]
    gapped = replay(events, as_of=stamp("15:05:00", D5), series=gapped_series)
    assert gapped["orders"]["R"]["path_gaps"] == ["2026-11-17"]
    assert not gapped["orders"]["R"]["eligible_for_validation"]


def test_a_later_pair_after_missing_minutes_is_diagnostic_only():
    events = opening_events() + [minute("09:31:00", 10.3), minute("09:32:00", 10.3)]
    result = replay(events)["orders"]["R"]
    assert result["status"] == "unavailable"
    assert result["coverage_gap"]
    assert result["missing_minute_labels"] == [stamp("09:30:00")]
    assert result["confirmation"]["minutes"][0]["label"] == stamp("09:31:00")
    assert result["buy"] is None


def test_equal_closes_and_lunch_boundary_do_not_confirm_with_full_minute_coverage():
    rows = []
    for start, end in SESSIONS:
        current = datetime.combine(datetime.fromisoformat(D1), start, SHANGHAI)
        cutoff = datetime.combine(datetime.fromisoformat(D1), end, SHANGHAI)
        while current < cutoff:
            label = current.time().isoformat()
            close = 10.2 if label in ("11:29:00", "13:00:00") else CANDIDATE["trigger"]
            rows.append(minute(label, close))
            current += MINUTE
    result = replay(rows, as_of=stamp("15:05:00"))
    assert result["coverage"]["visible_minute_labels"] == result["coverage"]["expected_minute_labels"]
    assert result["orders"]["R"]["status"] == "confirmation_not_met"
    assert "confirmation" not in result["orders"]["R"]
    assert result["orders"]["O"]["status"] == "unavailable"


@pytest.mark.parametrize(
    ("ask", "reason", "status"),
    [
        (10.09, "confirmed_book_below_trigger", "confirmation_lost"),
        (9.60, "confirmed_book_at_or_below_risk", "entry_invalidated"),
        (8.99, "price_outside_ordinary_limits", "unavailable"),
    ],
)
def test_first_relevant_quote_can_cancel_even_if_later_quote_recovers(ask, reason, status):
    events = opening_events() + confirmation_events() + quote_events() + [
        quote("09:32:04", "09:32:04.500", ask),
    ]
    r = replay(events)["orders"]["R"]
    assert r["reason"] == reason
    assert r["status"] == status
    assert r["buy"] is None


def test_first_executable_quote_can_follow_quantity_block():
    events = opening_events() + confirmation_events() + quote_events() + [
        quote("09:32:04", "09:32:04.500", 10.2, shares=100),
    ]
    r = replay(events)["orders"]["R"]
    assert r["buy"]["price"] == 10.25
    assert r["quote_attempts"][0]["reason"] == "insufficient_verified_counterparty_shares"


def test_explicit_quote_gap_is_unresolved_and_does_not_count_as_cash():
    events = opening_events() + confirmation_events() + quote_events() + [
        coverage("09:32:04", "09:32:05", complete=False, reason="connection_interrupted"),
    ]
    result = replay(events, as_of=stamp("15:05:00", D5))
    r = result["orders"]["R"]
    assert r["status"] == "unavailable"
    assert r["coverage_gap"]
    assert r["reason"] == "confirmation_book_coverage_unverified"
    assert summarize([result])["paired"]["both_outcomes_known"] == 0


def test_gap_starting_at_quote_receipt_does_not_retroactively_block_it():
    events = opening_events() + confirmation_events() + quote_events() + [
        coverage("09:32:05", "09:32:06", complete=False, reason="connection_interrupted"),
    ]
    order = replay(events)["orders"]["R"]
    assert order["buy"]["at"] == stamp("09:32:05")


def test_sector_evidence_is_as_of_and_does_not_filter_entry():
    events = opening_events() + confirmation_events() + quote_events()
    events.extend([
        event("sector", "09:30:10", source_at=stamp("09:30:09"), symbol=None,
              key="bank", membership_version="dated", expected=10, fresh=5, up=0, median_pct=-2),
        event("sector", "09:35:01", source_at=stamp("09:35:00"),
              key="bank", membership_version="dated", expected=10, fresh=10, up=10, median_pct=2),
    ])
    result = replay(events)
    assert result["sector"][0]["fresh"] == 5
    assert not result["sector"][0]["membership_verified"]
    assert result["orders"]["R"]["buy"]["price"] == 10.25


def test_c_candidate_uses_its_frozen_industry_membership_without_mainline():
    candidate = {**CANDIDATE, "mainlines": [], "sector_membership": {
        "bank": {"membership_version": "frozen", "expected": 10}
    }}
    events = opening_events() + confirmation_events() + quote_events()
    events.extend([
        event("sector", "09:30:10", source_at=stamp("09:30:09"), symbol=None,
              key="bank", membership_version="frozen", expected=10, fresh=5, up=0, median_pct=-2),
        event("sector", "09:30:12", source_at=stamp("09:30:11"), symbol=None,
              key="bank", membership_version="frozen", expected=11, fresh=5, up=4, median_pct=2),
    ])
    result = replay(events, candidate=candidate)
    assert result["sector"][0]["fresh"] == 5
    assert result["sector"][0]["membership_verified"]
    assert result["coverage"]["rejected_records"] == {
        "sector_membership_conflicts_with_frozen_configuration": 1
    }
    assert result["orders"]["R"]["buy"]["price"] == 10.25


@pytest.mark.parametrize(
    ("price_fields", "rejection"),
    [
        ({"previous_close": 9.99}, "previous_close_conflicts_with_frozen_signal"),
        ({"price_basis_verified": False}, "price_basis_conflict"),
        ({"price_basis_status": "mismatch"}, "price_basis_conflict"),
    ],
)
def test_conflicting_reference_price_excludes_the_quote(price_fields, rejection):
    events = opening_events() + confirmation_events() + [
        quote("09:32:04", "09:32:05", 10.25, **price_fields),
        coverage("09:32:04", "09:32:06"),
    ]
    result = replay(events)
    assert result["coverage"]["rejected_records"] == {rejection: 1}
    assert result["orders"]["R"]["buy"] is None
    assert result["orders"]["O"]["buy"]["price"] == 10.2


def test_verified_opening_auction_at_risk_invalidates_both_arms():
    events = [auction(price=CANDIDATE["risk"])] + opening_events() + confirmation_events() + quote_events()
    result = replay(events)
    for order in result["orders"].values():
        assert order["status"] == "entry_invalidated"
        assert order["buy"] is None
    assert result["opening_auction"]["observed_opening"]["auction_shares"] is None


def test_opening_evidence_gap_preserves_confirmation_and_quote_diagnostics():
    result = replay(confirmation_events() + quote_events())
    r = result["orders"]["R"]
    assert r["status"] == "unavailable"
    assert r["reason"] == "opening_risk_unverified"
    assert r["confirmation"]["at"] == stamp("09:32:04")
    assert r["observed_quote"]["ask_price"] == 10.25
    assert r["buy"] is None


def test_out_of_range_minute_cannot_form_confirmation():
    events = opening_events() + [minute("09:30:00", 100), minute("09:31:00", 10.2)] + quote_events()
    result = replay(events)
    assert result["coverage"]["rejected_records"] == {"minute_close_outside_ordinary_limits": 1}
    assert "confirmation" not in result["orders"]["R"]
    assert result["orders"]["R"]["status"] == "unavailable"


def test_preopen_preserves_the_real_next_session_without_using_future_evidence():
    result = replay(opening_events(), as_of=stamp("20:00:00", D))
    assert result["entry_session"] == D1
    assert result["target_date"] == D5
    assert result["orders"]["O"]["status"] == "pending_entry"
    assert result["orders"]["O"]["buy"] is None


def test_read_events_rejects_incorrect_version_and_invalid_json():
    assert read_events([json.dumps(auction()), " "]) == [auction()]
    with pytest.raises(ValueError, match="unsupported evidence schema"):
        read_events(['{"schema_version":2}'])
    with pytest.raises(ValueError, match="invalid evidence JSON"):
        read_events(["{"])
