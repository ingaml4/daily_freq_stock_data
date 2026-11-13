from copy import deepcopy
from datetime import datetime

from emscan.entry_minute_study import replay_candidate
from emscan.position_report import compare, build_parser
from emscan.position_sizing import Policy
from test_entry_minute_study import (CANDIDATE, DAYS, D1, D5, auction, prices,
                                    opening_events, confirmation_events, quote_events, stamp)


def sample(*, series=None, events=None, candidate=None, as_of=None):
    series = series or prices()
    events = events if events is not None else opening_events() + confirmation_events() + quote_events() + [auction()]
    c = deepcopy(candidate or CANDIDATE)
    return replay_candidate(c, events, DAYS, series, as_of=as_of or stamp("15:30:00", D5), quantity_probe=True)


def test_small_lot_probe_does_not_change_original_orders():
    events = opening_events() + confirmation_events() + quote_events() + [auction()]
    for event in events:
        if event["type"] == "quote":
            event["ask_shares"] = 100
    original = replay_candidate(CANDIDATE, events, DAYS, prices(), as_of=stamp("09:35:00"))
    probe = sample(events=events)
    assert original["orders"]["O"]["buy"] is None
    assert probe["orders"]["O"]["buy"]["shares"] == 100
    assert probe["orders"]["R"]["buy"]["shares"] == 100
    result = compare([probe], {CANDIDATE["secid"]: prices()}, DAYS, stamp("15:30:00", D5))
    for arm in result["arms"].values():
        for mode in arm.values():
            assert mode["sized_count"] == 1
            assert mode["ledger"][0]["order"]["buy"]["shares"] == 100


def test_same_quote_dates_and_shared_cash_with_independent_costs():
    records, series = [], {}
    for i in range(3):
        candidate = deepcopy(CANDIDATE)
        candidate.update(secid=f"0.00000{i + 1}", symbol=f"SZ00000{i + 1}")
        events = opening_events() + confirmation_events() + quote_events() + [auction()]
        for event in events:
            event["symbol"] = candidate["symbol"]
        records.append(sample(candidate=candidate, events=events))
        series[candidate["secid"]] = prices()
    old = deepcopy(records)
    report = compare(records, series, DAYS, stamp("15:30:00", D5))
    assert records == old
    assert report["promoted"] is False
    books = report["arms"]["O"]
    assert books["all_in"]["sized_count"] == 1
    assert books["fixed"]["sized_count"] == 3
    assert books["risk"]["sized_count"] >= 2
    assert books["risk"]["entry_theme_weights"]["bank"] <= .3
    for book in books.values():
        assert book["outcomes_mature"]
        assert all(point["cash"] >= -1e-6 for point in book["daily_equity"])
        for item in book["ledger"]:
            if item["order"]:
                assert item["order"]["buy"]["date"] == D1
                assert item["order"]["sell"]["date"] == D5
                assert item["order"]["buy"]["at"] == stamp("09:30:03")
                assert item["order"]["buy"]["price"] == 10.2
                assert item["order"]["sell"]["shares"] == item["order"]["buy"]["shares"]
    assert books["risk_first"]["adds"]["executed"] == 0


def test_missing_prices_and_pending_exit_are_not_zero_return_or_mature():
    series = prices()
    del series[D1]
    row = sample(series=series)
    book = compare([row], {CANDIDATE["secid"]: series}, DAYS, stamp("15:30:00", D5))["arms"]["O"]["risk"]
    assert book["sized_count"] == 1
    assert book["net_return_to_date"] is None
    assert book["mature_net_return"] is None
    assert book["net_return_to_date"] is None
    assert book["max_drawdown"] is None
    assert not book["valuation_complete"]
    assert book["pending_or_unverifiable_exit_count"] == 1
    early = sample(as_of=stamp("10:00:00"))
    book = compare([early], {CANDIDATE["secid"]: prices()}, DAYS, stamp("10:00:00"))["arms"]["O"]["risk"]
    assert book["net_return_to_date"] is None
    assert book["mature_net_return"] is None
    assert book["pending_or_unverifiable_exit_count"] == 1


def test_future_daily_prices_cannot_change_allocation():
    row = sample(as_of=stamp("10:00:00"))
    one = compare([row], {CANDIDATE["secid"]: prices()}, DAYS, stamp("10:00:00"))
    changed = prices()
    changed[D5]["close"] = 100000
    two = compare([row], {CANDIDATE["secid"]: changed}, DAYS, stamp("10:00:00"))
    assert one == two


def test_data_gap_preserved_in_denominator():
    row = sample(events=[])
    result = compare([row], {CANDIDATE["secid"]: prices()}, DAYS, stamp("15:30:00", D5))
    book = result["arms"]["O"]["risk"]
    assert book["candidate_count"] == 1
    assert not book["entry_data_complete"]
    assert not book["outcomes_mature"]
    assert book["mature_net_return"] is None


def test_fixed_amount_cli_default_and_explicit_override():
    required = ["--db", "db", "--archive", "archive", "--capture-config", "config",
                "--events", "events", "--out", "out", "--archive-sha256", "hash",
                "--as-of", stamp("15:30:00", D5)]
    assert build_parser().parse_args(required).fixed_amount == 48000
    assert build_parser().parse_args(required + ["--fixed-amount", "24000"]).fixed_amount == 24000
    row = sample()
    original = compare([row], {CANDIDATE["secid"]: prices()}, DAYS, stamp("15:30:00", D5))
    changed = compare([row], {CANDIDATE["secid"]: prices()}, DAYS, stamp("15:30:00", D5),
                      policy=Policy(fixed_amount=24000))
    assert original["fixed_amount"] == original["policy"]["fixed_amount"] == 48000
    assert changed["fixed_amount"] == changed["policy"]["fixed_amount"] == 24000
    assert changed["fixed_amount_basis"] == "per_order_cash_budget_including_buy_costs"
    assert changed["cohort_basis"] == "archived_ABC_cohort_not_named_candidate_prospective_trial"
    for arm in ("O", "R"):
        old = original["arms"][arm]["fixed"]["ledger"][0]["order"]
        new = changed["arms"][arm]["fixed"]["ledger"][0]["order"]
        assert new["buy"]["cash"] <= 24000 < old["buy"]["cash"] <= 48000
        assert new["buy"]["at"] == old["buy"]["at"]
        for mode in ("all_in", "risk", "risk_first"):
            assert original["arms"][arm][mode]["ledger"][0]["order"] == changed["arms"][arm][mode]["ledger"][0]["order"]


def test_fixed_amount_override_does_not_fill_missing_evidence():
    row = sample(events=[])
    report = compare([row], {CANDIDATE["secid"]: prices()}, DAYS, stamp("15:30:00", D5),
                     policy=Policy(fixed_amount=24000))
    for arm in ("O", "R"):
        book = report["arms"][arm]["fixed"]
        assert book["candidate_count"] == 1
        assert book["sized_count"] == 0
        assert book["mature_net_return"] is None
        assert not book["entry_data_complete"]
