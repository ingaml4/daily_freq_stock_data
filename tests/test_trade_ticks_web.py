from datetime import datetime

import requests

from emscan.store import Store
from emscan.trade_ticks import TradeTicksService
from emscan.web import serve


def test_fragment_endpoint_keeps_duplicate_ticks_and_requires_auth(tmp_path):
    calls = []

    def fetch(sid):
        calls.append(sid)
        row = dict(d="2026-11-10", t="14:33:09", p=95, v=3000, ts=1)
        return [row, dict(row)]

    service = TradeTicksService(fetch, clock=lambda: datetime.fromisoformat("2026-11-10T14:42:47+08:00"))
    path = tmp_path / "market.sqlite"
    with Store(path).init_schema():
        pass
    server = serve(lambda: Store(path), host="127.0.0.1", port=0, password="test", blocking=False, ticks_service=service)
    url = f"http://127.0.0.1:{server.server_port}/api/trade-ticks"
    try:
        assert requests.get(url, params={"symbols": "SZ300969"}, timeout=5).status_code == 401
        assert calls == []
        auth = ("", "test")
        for _ in range(2):
            r = requests.get(url, params={"symbols": "SZ300969"}, auth=auth, timeout=5)
            assert r.status_code == 200
            item = r.json()["items"][0]
            assert item["quality"] == "gap" and not item["realtime_verified"]
            assert len(item["ticks"]) == 2
            assert all(t["source_trade_id"] is None for t in item["ticks"])
        assert calls == ["0.300969"]
        assert requests.get(url, params={"symbols": "SZ300969", "date": "2026-11-09"}, auth=auth, timeout=5).status_code == 400
    finally:
        server.shutdown()
        server.server_close()
