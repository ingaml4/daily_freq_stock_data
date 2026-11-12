import json
import sqlite3

import requests

from emscan.store import Store
from emscan.strategy_review import portfolio, promotion_decision
from emscan.web import serve


def test_promotion_requires_execution_dates_provenance_and_portfolio():
    sample = dict(
        signal_dates_with_closed_orders=60, closed=100, execution_coverage=0.95
    )
    comparison = {"simultaneous_95pct_interval": [0.001, 0.003]}
    sources = dict(
        point_in_time_universe=True,
        point_in_time_industries=True,
        comparable_indicator_price_history=True,
    )
    baseline = dict(
        valid_for_comparison=True,
        valuation_complete=True,
        max_drawdown=-0.1,
        net_return=0.03,
    )
    improved = dict(
        valid_for_comparison=True,
        valuation_complete=True,
        max_drawdown=-0.09,
        net_return=0.04,
    )
    assert promotion_decision(
        sample,
        comparison,
        sources,
        improved,
        baseline,
        {"net_return": 0.02, "valid_for_comparison": True},
        sample,
        {"net_return": 0.01, "valid_for_comparison": True},
    )["approved"]
    missing = promotion_decision(
        {**sample, "signal_dates_with_closed_orders": 10},
        comparison,
        {**sources, "point_in_time_industries": False},
        improved,
        baseline,
        {"net_return": 0.02, "valid_for_comparison": True},
        sample,
        {"net_return": 0.01, "valid_for_comparison": True},
    )
    assert not missing["approved"]
    assert "historical_industry_membership_unverified" in missing["reasons"]
    assert "insufficient_out_of_sample_dates" in missing["reasons"]
    invalid = promotion_decision(
        sample,
        {"simultaneous_95pct_interval": [float("nan"), 0.01]},
        sources,
        improved,
        baseline,
        {"net_return": 0.02, "valid_for_comparison": True},
        {**sample, "execution_coverage": float("nan")},
        {"net_return": 0.01, "valid_for_comparison": True},
    )
    assert not invalid["approved"]
    assert "net_improvement_not_established" in invalid["reasons"]
    assert "baseline_evidence_insufficient" in invalid["reasons"]


def test_shared_cash_cannot_spend_close_sale_at_same_day_open():
    days = ["2026-10-20", "2026-10-21"]
    orders = []
    prices = {}
    for i in range(9):
        sid = str(i)
        prices[sid] = {d: dict(source="spot", close=10) for d in days}
        orders.append(
            dict(
                date="2026-10-19",
                secid=sid,
                status="closed",
                buy=dict(date=days[0], price=10, shares=10000, cash=100000),
                sell=dict(date=days[1], execution_phase="close", cash=101000),
            )
        )
    result = portfolio(orders, prices, days, "2026-10-19")
    assert result["blocked"]["cash_or_slots"] == 1
    assert result["daily_equity"][0]["holdings"] == 8
    assert result["completed_trades"] == 8
    assert abs(result["net_return"] - 0.01) < 1e-9
    assert result["daily_equity"][-1]["cash"] == 808000


def test_review_artifacts_are_authenticated_and_do_not_run_research(tmp_path):
    path = tmp_path / "market.sqlite"
    with Store(path).init_schema():
        pass
    folder = tmp_path / "strategy-review"
    folder.mkdir()
    (folder / "report.json").write_text(
        json.dumps({"approved_strategies": [], "live_selection_changed": False})
    )
    (folder / "entry-timing").mkdir()
    (folder / "entry-timing" / "report.json").write_text('{"version":"abc-entry-timing-v1"}')
    minute = folder / "entry-minute" / "2026-11-12"
    minute.mkdir(parents=True)
    (minute / "report.json").write_text('{"version":"abc-minute-entry-v1"}')
    server = serve(
        lambda: Store(path), host="127.0.0.1", port=0, password="test", blocking=False
    )
    base = f"http://127.0.0.1:{server.server_port}/api/strategy-review"
    try:
        assert requests.get(base, timeout=5).status_code == 401
        assert requests.get(base + "/entry-timing", timeout=5).status_code == 401
        assert requests.get(base + "/entry-minute?date=2026-11-12", timeout=5).status_code == 401
        assert requests.get(base + "/entry-minute?date=../../12", auth=("", "test"), timeout=5).status_code == 400
        minute_study = requests.get(base + "/entry-minute?date=2026-11-12", auth=("", "test"), timeout=5)
        assert minute_study.json()["version"] == "abc-minute-entry-v1"
        study = requests.get(base + "/entry-timing", auth=("", "test"), timeout=5)
        assert study.json()["version"] == "abc-entry-timing-v1"
        result = requests.get(base, auth=("", "test"), timeout=5)
        assert (
            result.status_code == 200
            and result.json()["live_selection_changed"] is False
        )
        assert (
            requests.get(base + "/orders", auth=("", "test"), timeout=5).status_code
            == 404
        )
        with sqlite3.connect(path) as c:
            assert c.execute("SELECT count(*) FROM daily_signals").fetchone()[0] == 0
    finally:
        server.shutdown()
        server.server_close()


def test_order_urls_negotiate_browser_view_without_changing_json(tmp_path):
    path = tmp_path / "market.sqlite"
    with Store(path).init_schema():
        pass
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>orders client</html>", encoding="utf8")
    folder = tmp_path / "strategy-review"
    (folder / "entry-timing").mkdir(parents=True)
    (folder / "orders.json").write_bytes(b'{"baseline":[]}')
    (folder / "entry-timing" / "orders.json").write_bytes(b'[]')
    server = serve(lambda: Store(path), host="127.0.0.1", port=0, password="test",
                   blocking=False, frontend_dist=dist)
    site = f"http://127.0.0.1:{server.server_port}"
    browser = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    auth = ("", "test")
    try:
        for endpoint, dataset, payload in (
            ("/api/strategy-review/orders", "original", b'{"baseline":[]}'),
            ("/api/strategy-review/entry-timing/orders", "abc", b'[]'),
        ):
            response = requests.get(site + endpoint, headers=browser, allow_redirects=False, timeout=5)
            assert response.status_code == 303 and response.headers["Vary"] == "Accept"
            assert response.headers["Location"] == f"/strategy-review/orders?dataset={dataset}"
            assert requests.get(site + response.headers["Location"], allow_redirects=False, timeout=5).status_code == 303
            assert requests.get(site + endpoint, timeout=5).status_code == 401
            assert requests.get(site + endpoint, auth=auth, timeout=5).content == payload
            assert requests.get(site + endpoint, auth=auth, headers={"Accept": "text/html;q=0,application/json"}, timeout=5).content == payload
            assert requests.get(site + endpoint + "?format=json", auth=auth, headers=browser, timeout=5).content == payload
            assert "orders client" in requests.get(site + endpoint, auth=auth, headers=browser, timeout=5).text
    finally:
        server.shutdown()
        server.server_close()
