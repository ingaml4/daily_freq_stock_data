from copy import deepcopy

import pytest

from emscan import execution
from emscan.position_sizing import Policy, size_order, loss_at

AT = "2026-11-13T09:32:05+08:00"


def candidate(**kw):
    return {"secid": "0.300629", "entry_price": 19.793, "risk_price": 19.24,
            "at": AT, "risk_available_at": "2026-11-12T17:00:00+08:00",
            "themes_available_at": "2026-11-12T17:00:00+08:00", "themes": ["aerospace"],
            "ask_shares": 50000, "execution_verified": True, **kw}


def account(**kw):
    return {"kind": "research", "as_of": AT, "equity": 240000., "monthly_peak_equity": 240000.,
            "cash_available": 240000., "cash_basis": "before_pending", "positions": [],
            "pending_orders": [], **kw}


def exposure(**kw):
    return {"id": "existing", "secid": "0.000001", "themes": ["aerospace", "bank"],
            "market_value": 60000., "planned_loss": 900., "stress_loss": 1800., **kw}


def test_exact_fees_minimum_commission_and_risk_vs_stress():
    first = size_order(candidate(), account(), mode="risk_first")
    full = size_order(candidate(), account())
    assert first["shares"] < full["shares"]
    assert first["planned_loss"] <= 600
    assert full["planned_loss"] <= 1200
    assert full["stress_loss"] <= 2400
    assert full["buy"]["gross"] <= 48000
    assert loss_at(19.793, 19.24, first["shares"] + 100) > 600
    assert "overnight_stress" in full["reasons"]
    assert loss_at(10, 9, 100) == pytest.approx(100 + execution._cost(10, 100, "buy", execution.settings({}))["cost"] + execution._cost(9, 100, "sell", execution.settings({}))["cost"])


def test_reserved_cash_and_theme_exposure_not_double_deducted():
    pending = exposure(reserved_cash=60050.)
    before = size_order(candidate(), account(pending_orders=[pending]))
    net = size_order(candidate(), account(cash_available=179950., cash_basis="net_of_pending", pending_orders=[pending]))
    assert before == net
    assert before["shares"] * 19.793 <= 12000
    assert "theme:aerospace" in before["reasons"]


@pytest.mark.parametrize("change,reason", [
    ({"cash_available": None}, "missing_or_invalid_equity_cash_monthly_peak"),
    ({"cash_basis": "unknown"}, "cash_reservation_basis_unknown"),
    ({"pending_orders": None}, "positions_or_pending_orders_unknown"),
    ({"as_of": "2026-11-13T09:31:00+08:00"}, "future_or_stale_snapshot"),
    ({"as_of": "2026-11-13T09:32:06+08:00"}, "future_or_stale_snapshot"),
    ({"positions": [exposure(), exposure()]}, "missing_or_duplicate_exposure_id"),
    ({"pending_orders": [exposure(reserved_cash=1)]}, "pending_cash_reservation_unknown_or_understated"),
])
def test_unknown_account_is_not_zero_position(change, reason):
    out = size_order(candidate(), account(**change))
    assert out["shares"] is None
    assert out["reasons"] == [reason]


@pytest.mark.parametrize("change", [
    {"risk_price": 20}, {"ask_shares": True}, {"entry_price": float("nan")},
    {"themes": []}, {"execution_verified": False}, {"secid": "1.688001"},
    {"risk_available_at": "2026-11-13T09:32:06+08:00"},
])
def test_candidate_gaps_fail_closed(change):
    assert size_order(candidate(**change), account())["shares"] is None


def test_no_lot_is_known_rejection_and_displayed_depth_caps_all_in():
    assert size_order(candidate(ask_shares=99), account())["shares"] == 0
    assert size_order(candidate(ask_shares=357), account(), mode="all_in")["shares"] == 300
    assert size_order(candidate(), account(cash_available=1980), mode="all_in")["shares"] == 0


def test_monthly_warning_halves_budget_and_stop_does_not_liquidate():
    at_warning = size_order(candidate(), account(equity=228000), mode="risk_first")
    assert at_warning["risk_budget_multiplier"] == .5
    assert at_warning["planned_loss"] <= 285
    stopped = size_order(candidate(), account(equity=216000))
    assert stopped["shares"] == 0
    assert stopped["reasons"] == ["monthly_drawdown_stop"]


def test_multi_theme_full_count_and_portfolio_risk():
    out = size_order(candidate(themes=["aerospace", "bank"]), account(positions=[exposure(themes=["bank"]) ]))
    assert "theme:bank" in out["reasons"]
    assert out["shares"] * 19.793 <= 12000
    out = size_order(candidate(), account(positions=[exposure(planned_loss=2999, themes=["other"]) ]))
    assert out["shares"] == 0 and "portfolio_risk" in out["reasons"]


def test_adds_require_new_evidence_and_share_the_name_budget():
    held = exposure(secid="0.300629", market_value=20000, planned_loss=600, stress_loss=1300)
    base = account(positions=[held])
    assert size_order(candidate(), base)["shares"] == 0
    assert size_order(candidate(), base, mode="risk_add")["shares"] is None
    c = candidate(previous_entry_at="2026-11-13T09:30:00+08:00",
                  add_signal_available_at="2026-11-13T09:32:00+08:00",
                  previous_signal_id="one", add_signal_id="two",
                  previous_entry_price=19.5, previous_risk_price=19.24)
    out = size_order(c, base, mode="risk_add")
    assert out["shares"] > 0
    assert out["planned_loss"] + 600 <= 1200
    assert out["stress_loss"] + 1300 <= 2400
    c["previous_entry_price"] = 20
    assert size_order(c, base, mode="risk_add")["shares"] is None
    c["previous_entry_price"] = 19.5
    c["add_signal_id"] = "one"
    assert size_order(c, base, mode="risk_add")["shares"] is None


def test_policy_validation_and_pure_inputs():
    with pytest.raises(ValueError):
        Policy(name_value=0.7)
    c, a = candidate(), account()
    old = deepcopy((c, a))
    size_order(c, a)
    assert (c, a) == old
