import requests

from emscan.store import Store
from emscan.ui import asset
from emscan.web import serve


def test_static_assets_cannot_escape_frontend_directory(tmp_path):
    root = tmp_path / "dist"
    root.mkdir()
    (root / "entry.js").write_text("export default 1")
    (tmp_path / "private.txt").write_text("private")
    assert asset(root, "/ui/entry.js")[0] == b"export default 1"
    assert asset(root, "/ui/%2e%2e/private.txt") is None
    assert asset(root, "/ui/%00.js") is None


def test_spa_cookie_session_and_json_endpoints_keep_separate_responses(tmp_path):
    path = tmp_path / "market.sqlite"
    with Store(path).init_schema():
        pass
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>vue-root</html>")
    (dist / "assets" / "index-test.js").write_text("export default 1")
    server = serve(lambda: Store(path), host="127.0.0.1", port=0, password="ui-test", blocking=False, frontend_dist=dist)
    base = f"http://127.0.0.1:{server.server_port}"
    with requests.Session() as session:
        try:
            assert session.get(base + "/api/session", timeout=5).json()["authenticated"] is False
            assert "vue-root" in session.get(base + "/login", timeout=5).text
            public = session.get(base + "/ui/assets/index-test.js", timeout=5)
            assert public.status_code == 200 and "immutable" in public.headers["Cache-Control"]
            assert session.get(base + "/api/ui/meta", timeout=5).status_code == 401
            bad = session.post(base + "/api/session", data={"password": "wrong"}, timeout=5)
            assert bad.status_code == 401
            logged_in = session.post(base + "/api/session", data={"password": "ui-test", "remember": "1", "next": "https://evil.test"}, timeout=5)
            assert logged_in.status_code == 200 and logged_in.json()["next"] == "/"
            assert "HttpOnly" in logged_in.headers["Set-Cookie"] and "SameSite=Lax" in logged_in.headers["Set-Cookie"]
            assert session.get(base + "/api/session", timeout=5).json()["authenticated"]
            for route in ("/", "/signals/box_range", "/summary", "/tiers", "/strategy-review"):
                assert "vue-root" in session.get(base + route, timeout=5).text
            assert session.get(base + "/api/ui/meta", timeout=5).json()["views"]
            missing = session.get(base + "/api/summary", timeout=5).json()
            assert missing["missing"] and missing["content"] == ""
            assert session.get(base + "/api/summary?date=bad", timeout=5).status_code == 400
            assert "rows" in session.get(base + "/api/signals?view=vol_spike", timeout=5).json()
            session.get(base + "/logout", allow_redirects=False, timeout=5)
            assert session.get(base + "/api/ui/meta", timeout=5).status_code == 401
        finally:
            server.shutdown()
            server.server_close()
