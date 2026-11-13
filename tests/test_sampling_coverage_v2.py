from copy import deepcopy

from test_entry_minute_study import CANDIDATE, coverage, quote, replay, confirmation_events, quote_events, auction


def v2(row, **extra):
    return {**row, "sampling_coverage_version": 2, "transport_ok": True,
            "symbol_parsed": True, "quote_usable": True, "current_fresh": True, **extra}


def run(events, version=2):
    return replay(events, candidate={**CANDIDATE, "sampling_coverage_version": version})


def test_known_preopen_response_does_not_become_an_executable_quote():
    rows = [quote("09:29:54", "09:30:01", 10.1),
            v2(coverage("09:30:00", "09:30:01"), quote_usable=False, current_fresh=False),
            quote("09:30:02", "09:30:03", 10.2),
            v2(coverage("09:30:01", "09:30:03"))]
    out = run(rows)
    assert out["orders"]["O"]["buy"]["price"] == 10.2
    assert out["coverage"]["rejected_records"]["quote_outside_continuous_session"] == 1
    # An old contract cannot silently acquire the new coverage interpretation.
    old = run(rows, version=1)
    assert old["orders"]["O"]["buy"] is None
    assert old["coverage"]["rejected_records"]["coverage_version_conflicts_with_frozen_configuration"] == 2


def test_one_source_timeout_is_covered_by_independent_observed_source():
    rows = [v2(coverage("09:30:00", "09:30:03", complete=False), transport_ok=False, symbol_parsed=False),
            v2(coverage("09:30:00", "09:30:03", source="backup")),
            dict(quote("09:30:02", "09:30:03", 10.2), source="backup")]
    assert run(rows)["orders"]["O"]["buy"]["price"] == 10.2
    rows[1]["complete"] = False
    assert run(rows)["orders"]["O"]["buy"] is None


def test_complementary_sources_can_cover_segments_but_not_a_real_gap():
    first = v2(coverage("09:30:00", "09:30:01", source="first"))
    second = v2(coverage("09:30:01", "09:30:03", source="backup"))
    rows = [first, second, dict(quote("09:30:02", "09:30:03", 10.2), source="backup")]
    assert run(rows)["orders"]["O"]["buy"]
    second["start"] = second["start"].replace("09:30:01", "09:30:02")
    assert run(rows)["orders"]["O"]["buy"] is None


def test_failed_same_source_interval_cannot_be_overridden_by_complete_claim():
    rows = [v2(coverage("09:30:00", "09:30:03")),
            v2(coverage("09:30:01", "09:30:02", complete=False)),
            quote("09:30:02", "09:30:03", 10.2)]
    assert run(rows)["orders"]["O"]["buy"] is None
    rows.append(v2(coverage("09:30:01", "09:30:02", source="backup")))
    assert run(rows)["orders"]["O"]["buy"]


def test_invalid_v2_complete_claim_is_rejected():
    rows = [v2(coverage("09:30:00", "09:30:03"), transport_ok=False),
            quote("09:30:02", "09:30:03", 10.2)]
    out = run(rows)
    assert out["orders"]["O"]["buy"] is None
    assert "complete_sampling_without_successful_symbol_response" in out["coverage"]["rejected_records"]


def test_confirmation_still_uses_real_post_confirmation_quote_and_shared_coverage():
    rows = [auction()] + confirmation_events()
    rows += [v2(coverage("09:32:04", "09:32:06", source="backup")),
             v2(coverage("09:32:04", "09:32:06", complete=False), transport_ok=False, symbol_parsed=False),
             dict(quote("09:32:03", "09:32:04.500", 10.01), source="backup"),
             dict(quote("09:32:04", "09:32:05", 10.25), source="backup")]
    order = run(rows)["orders"]["R"]
    assert order["buy"]["price"] == 10.25
