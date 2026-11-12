"""Personal APIs validate authentication, query parameters and cached data."""

import pytest
import requests

from emscan.store import Store
from emscan.web import serve
from helpers import bar

DAY = "2026-11-11"
ROUTES = ("/api/data-quality", "/api/personal-opportunities", "/api/sector-snapshot")


@pytest.fixture
def server(tmp_path):
    db = tmp_path / "market.sqlite"
    with Store(db).init_schema() as store:
        store.upsert_bars([bar("1.600001", DAY, 1000)])
    httpd = serve(lambda: Store(db).init_schema(), host="127.0.0.1", port=0,
                  password="test-password", blocking=False)
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def get(server, path, authenticated=True):
    return requests.get(server + path, auth=("", "test-password") if authenticated else None,
                        timeout=5)


@pytest.mark.parametrize("path", ROUTES)
def test_personal_routes_require_authentication(server, path):
    assert get(server, path, False).status_code == 401


@pytest.mark.parametrize("path", ROUTES[:2])
@pytest.mark.parametrize("query", ["?date=2026-11-11&date=2026-11-10", "?unknown=1",
                                   "?date=2026-02-30", "?date=", "?date=20261111"])
def test_personal_dates_reject_invalid_queries(server, path, query):
    assert get(server, path + query).status_code == 400


@pytest.mark.parametrize("path", ROUTES[:2])
def test_personal_dates_default_to_latest_bar(server, path, monkeypatch):
    monkeypatch.setattr("emscan.data_quality.turnover_quality", lambda conn, date: {"date": date})
    monkeypatch.setattr("emscan.opportunity_archive.report",
                        lambda store, date: {"date": date})
    response = get(server, path)
    assert response.status_code == 200
    assert response.json() == {"date": DAY}
    response = get(server, path + "?date=2026-11-10")
    assert response.status_code == 200
    assert response.json() == {"date": "2026-11-10"}


def test_sector_snapshot_query_and_absent_or_valid_cache(server, monkeypatch):
    assert get(server, ROUTES[2] + "?date=2026-11-11").status_code == 400
    response = get(server, ROUTES[2])
    assert response.status_code == 503
    assert response.json()["status"] == "waiting"
    monkeypatch.setattr("emscan.sector_snapshot.build_sector_snapshot_payload",
                        lambda path: {"status": "ready", "items": []})
    response = get(server, ROUTES[2])
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "items": []}
