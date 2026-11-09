from datetime import date, timedelta

import pytest

from emscan import execution


DAYS = [(date(2026, 8, 3) + timedelta(days=i)).isoformat() for i in range(12)]
ROW = {
    "date": DAYS[5],
    "secid": "0.000001",
    "name": "普通股",
    "status": "已完成",
    "generated_at": DAYS[5] + " 17:15:00",
}
OPTS = {**execution.DEFAULTS, "capital": 10000}


def quote(price=10, previous=10, volume=10000):
    return dict(
        open=price,
        close=price,
        high=price,
        low=price,
        source="spot",
        volume=volume,
        chg=price - previous,
        pct_chg=(price / previous - 1) * 100,
    )


def run(series, days=DAYS, row=ROW, opts=OPTS, horizons=(1, 5)):
    return execution.simulate(row, days, 5, series, DAYS[0], horizons, opts)


def test_first_exit_is_day_after_buy_with_exact_costs():
    series = {day: quote() for day in DAYS}
    series[DAYS[7]] = quote(10.5)
    series[DAYS[8]] = quote(10, 10.5)
    series[DAYS[6]]["high"] = 10.8
    order = run(series)
    assert order["buy"]["date"] == DAYS[6]
    assert order["buy"]["shares"] == 900
    first = order["exits"][1]
    assert first["target_date"] == first["sell"]["date"] == DAYS[7]
    assert first["sell"]["price"] == 10.5
    assert first["sell"]["commission"] == 5 and first["sell"]["tax"] == 4.73
    assert first["total_cost"] == pytest.approx(24.14)
    assert first["net_return"] == pytest.approx((9450 - 14.55) / (9000 + 9.59) - 1, abs=1e-8)
    assert order["exits"][5]["sell"]["date"] == DAYS[11]
    csv = execution.csv_values({"execution": order}, [1, 5])
    assert set(csv) == set(execution.csv_columns([1, 5]))
    assert csv["sim_sell_date_1d"] == DAYS[7] and csv["sim_net_return_1d"] == first["net_return"]


def test_limit_ups_and_downs_are_directional_and_retry_preserves_holding_period():
    series = {day: quote() for day in DAYS}
    series[DAYS[6]] = quote(11, 10)  # limit up blocks entry
    series[DAYS[7]] = quote(9.9, 11)  # limit down allows buy
    series[DAYS[8]] = quote(8.91, 9.9)  # limit down blocks sell
    series[DAYS[9]] = quote(9.80, 8.91)  # limit up allows sell
    order = run(series, horizons=(1,))
    assert order["buy"]["date"] == DAYS[7]
    assert order["entry_attempts"][0]["date"] == DAYS[6]
    first = order["exits"][1]
    assert first["target_date"] == DAYS[8] and first["sell"]["date"] == DAYS[9]
    assert first["attempts"][0]["date"] == DAYS[8]


def test_zero_volume_delays_but_missing_data_is_indeterminate():
    series = {day: quote() for day in DAYS}
    series[DAYS[6]] = {"volume": 0}
    order = run(series)
    assert order["buy"]["date"] == DAYS[7]
    del series[DAYS[6]]
    order = run(series)
    assert order["status"] == "unavailable" and not order["buy"]
    series[DAYS[6]] = quote()
    del series[DAYS[8]]
    order = run(series)
    assert order["exits"][1]["status"] == "closed"
    assert order["exits"][5]["status"] == "unavailable"


def test_adjusted_or_reference_change_and_late_picks_have_no_returns():
    series = {day: quote() for day in DAYS}
    series[DAYS[7]] = quote(8, 8)  # incoming reference changed, cash dividend unknown
    assert run(series)["exits"][1]["status"] == "unavailable"
    series[DAYS[6]]["source"] = "kline"
    assert run(series)["status"] == "unavailable"
    assert run(series, row={**ROW, "generated_at": DAYS[7]})["status"] == "excluded"
    assert run(series, row={**ROW, "secid": "2.920001"})["status"] == "excluded"


def test_missing_future_waits_and_completed_sample_denominator_is_separate():
    series = {day: quote() for day in DAYS}
    completed = run(series)
    pending = run(series, days=DAYS[:7])
    assert pending["exits"][1]["status"] == "pending_exit"
    rows = [{"tier": 1, "execution": order} for order in (completed, pending)]
    stats = execution.aggregate(rows, [1])["1"]["1"]
    assert stats["total"] == 2 and stats["n"] == 1
    assert stats["states"] == {"closed": 1, "pending_exit": 1}


def test_order_size_lot_and_expiration():
    series = {day: quote() for day in DAYS}
    no_funds = run(series, row={**ROW, "secid": "1.688001"}, opts={**OPTS, "capital": 1500})
    assert no_funds["status"] == "entry_expired"
    series.update({d: {"volume": 0} for d in DAYS[6:9]})
    assert run(series)["status"] == "entry_expired"
