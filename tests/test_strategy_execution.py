from datetime import date, timedelta

import pytest

from emscan import execution
from emscan.strategy_execution import simulate_candidate


DAYS = [
    (date(2026, 8, 3) + timedelta(days=i)).isoformat()
    for i in range(21)
    if (date(2026, 8, 3) + timedelta(days=i)).weekday() < 5
]
SIGNAL = {
    "date": DAYS[5],
    "secid": "0.000001",
    "name": "普通股",
    "point_in_time": True,
    "strategy": "breakout",
    "invalidation_price": 9.7,
    "invalidation_basis": "spot",
}
OPTS = {**execution.DEFAULTS, "capital": 10000}


def bar(o=10, c=10, ref=10, **kw):
    data = dict(
        source="spot",
        open=o,
        close=c,
        high=max(o, c),
        low=min(o, c),
        volume=10000,
        chg=c - ref,
        pct_chg=(c / ref - 1) * 100,
    )
    return {**data, **kw}


def run(changes=None, *, candidate=None, days=DAYS, policy="structure_time", **kw):
    series = {d: bar() for d in DAYS}
    series.update({DAYS[k]: v for k, v in (changes or {}).items()})
    return simulate_candidate(candidate or SIGNAL, days, series, OPTS, exit_policy=policy, **kw)


def test_fixed_outputs_match_original_and_default_fifth_session_is_after_buy():
    r = run(policy="fixed_5d")
    baseline = {**SIGNAL, "strategy": "baseline_core", "invalidation_price": None}
    base = run(candidate=baseline, policy="fixed_5d")
    old = execution.simulate(
        dict(SIGNAL, generated_at=DAYS[5] + " 15:00", status="已完成"),
        DAYS,
        5,
        {d: bar() for d in DAYS},
        DAYS[0],
        (5,),
        OPTS,
    )
    assert r["status"] == base["status"] == "closed"
    assert r["net_return"] == base["net_return"] == old["exits"][5]["net_return"]
    assert (
        r["buy"]["cash"] == old["buy"]["cash"]
        and r["sell"]["cash"] == old["exits"][5]["sell"]["cash"]
    )
    assert r["cost"] == r["total_cost"] == old["exits"][5]["total_cost"]
    assert r["buy"]["execution_phase"] == "open" and r["sell"]["execution_phase"] == "close"
    assert r["sell"]["date"] == DAYS[11] and r["eligible_for_validation"]
    assert r["provenance"] == "point_in_time_reconstruction"


def test_buy_day_close_can_trigger_t_plus_one_and_gap_uses_actual_open():
    r = run({6: bar(10, 9.6), 7: bar(9.2, 9.4, 9.6)})
    assert r["trigger_date"] == r["buy"]["date"] == DAYS[6]
    assert r["sell"]["date"] == DAYS[7] and r["sell"]["price"] == 9.2
    assert r["sell"]["execution_phase"] == "open" and r["reason"] == "structure_next_open"
    assert r["exit_rule_version"] == "close_confirmed_next_open.v1"
    assert r["invalidation_price"] == 9.7 and r["cost"] > 0


def test_no_low_trigger_and_strict_close_breach_waits_for_next_open():
    r = run({6: bar(low=9.4), 7: bar(10, 9.7), 8: bar(9.8, 9.5, 9.7), 9: bar(9.2, 9.6, 9.5)})
    assert r["trigger_date"] == DAYS[8] and r["sell"]["date"] == DAYS[9]
    assert r["sell"]["price"] == 9.2


def test_trigger_latches_through_limit_down_and_suspension():
    r = run({6: bar(10, 9.6), 7: bar(8.64, 9.7, 9.6), 8: bar(volume=0), 9: bar(9.6, 9.8, 9.7)})
    assert r["trigger_date"] == DAYS[6] and r["sell"]["date"] == DAYS[9]
    assert [a["date"] for a in r["exit_attempts"]] == DAYS[7:9]
    assert r["sell"]["price"] == 9.6


@pytest.mark.parametrize("policy", ["fixed_5d", "structure_time"])
def test_open_at_floor_retires_candidate_and_blocked_entry_cannot_chase_after_bad_close(policy):
    for changes in ({6: bar(9.7, 10)}, {6: bar(11, 9.6), 7: bar(10, 10, 9.6)}):
        r = run(changes, policy=policy)
        assert r["status"] == "entry_invalidated" and r["buy"] is None and r["net_return"] is None


def test_wait_at_most_three_days_and_fixed_vs_structure_uses_same_entry():
    change = {6: bar(volume=0), 7: bar(volume=0), 8: bar(volume=0)}
    assert run(change, opts={**OPTS, "entry_wait": 20})["status"] == "entry_expired"
    del change[8]
    assert run(change)["buy"] == run(change, policy="fixed_5d")["buy"]
    assert run(change)["buy"]["date"] == DAYS[8]


@pytest.mark.parametrize(
    "bad,quality",
    [
        (None, "unavailable"),
        (bar(source="kline"), "exploratory_history"),
        (bar(open=float("nan")), "unavailable"),
        (bar(volume="a"), "unavailable"),
        (bar(ref=9), "unavailable"),
        (bar(source="kline", volume=0), "exploratory_history"),
    ],
)
def test_unknown_paths_keep_cash_occupied_and_exclude_net_return(bad, quality):
    r = run({8: bad})
    assert r["buy"]["shares"] > 0 and r["sell"] is None and r["net_return"] is None
    assert (
        r["status"] == "unavailable"
        and r["quality"] == quality
        and not r["eligible_for_validation"]
    )


def test_qfq_signal_and_missing_or_late_provenance_cannot_validate():
    assert run({5: bar(source="kline")})["quality"] == "exploratory_history"
    assert (
        run(candidate={**SIGNAL, "invalidation_basis": "qfq"})["quality"] == "exploratory_history"
    )
    for stamp in (None, DAYS[6] + " 15:00:00", DAYS[5] + " 14:59:59"):
        r = run(candidate={**SIGNAL, "point_in_time": False, "generated_at": stamp})
        assert r["status"] == "excluded" and r["sell"] is None
    assert (
        run(candidate={**SIGNAL, "point_in_time": False, "generated_at": DAYS[5] + "T07:00:00Z"})[
            "status"
        ]
        == "closed"
    )
    assert run(candidate={**SIGNAL, "invalidation_price": None})["status"] == "unavailable"


def test_partial_today_excluded_and_future_bars_do_not_change_an_early_structural_sale():
    change = {6: bar(10, 9.6), 7: bar(9.2, 9.4, 9.6), 8: None}
    r = run(change, as_of=DAYS[7] + "T06:59:00Z")
    assert r["status"] == "pending_exit" and r["sell"] is None and r["buy"]
    closed = run(change, as_of=DAYS[7] + "T07:00:00Z")
    assert closed["sell"] == run(change)["sell"] and closed["sell"]["date"] == DAYS[7]
    assert closed["net_return"] == run(change)["net_return"]


def test_time_exit_does_not_use_same_day_close_to_execute_a_past_open():
    r = run({11: bar(10, 9.5)})
    assert r["sell"]["date"] == DAYS[11] and r["sell"]["execution_phase"] == "close"
    assert r["reason"] == "time_exit_close" and r["trigger_date"] is None
