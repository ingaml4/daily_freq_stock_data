import json
import sqlite3
import time

import requests

from emscan.auction_archive import current_session
from emscan.store import Store
from emscan.web import serve


def test_calendar_distinguishes_unpublished_session_from_holiday(tmp_path):
    with Store(tmp_path / "market.sqlite").init_schema() as st:
        assert current_session(st, "2026-11-10") is None
        assert current_session(st, "2026-11-08") is False
        st.set_meta("mairui_calendar:2026", json.dumps({"days": ["2026-11-10"], "expires": time.time()+60}))
        assert current_session(st, "2026-11-10") is True
        assert current_session(st, "2026-11-11") is False


def test_auction_http_auth_read_only_and_archive_dedup(tmp_path):
    class Frame:
        calls = 0

        def snapshot(self, symbols, *, is_trading_day):
            self.calls += 1
            return {"items": [{
                "symbol": symbols[0], "source_endpoint": "hsstock.real.five",
                "received_at": "2026-11-10T09:25:05.344122+08:00", "raw": {"vb": [17, 0, 0, 0, 0]},
                "quality": "gap", "upstream_requested": True, "cached": self.calls > 1,
            }]}

    path = tmp_path / "market.sqlite"
    with Store(path).init_schema():
        pass
    book = Frame()
    server = serve(lambda: Store(path), host="127.0.0.1", port=0, password="test",
                   blocking=False, auction_service=book)
    base = f"http://127.0.0.1:{server.server_port}/api/auction-snapshot"
    params = {"symbols": "SZ301502"}
    auth = ("", "test")
    try:
        assert requests.get(base, params=params, timeout=5).status_code == 401
        assert requests.get(base, params={**params, "date":"2026-11-09"}, auth=auth, timeout=5).status_code == 400
        assert book.calls == 0
        for _ in range(2):
            res = requests.get(base, params=params, auth=auth, timeout=5)
            assert res.status_code == 200 and res.json()["archive_status"] == "stored"
        with sqlite3.connect(tmp_path / "market-auction.sqlite") as c:
            rows = c.execute("SELECT frame_json FROM auction_frames").fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0][0])["raw"]["vb"][0] == 17
    finally:
        server.shutdown()
        server.server_close()
