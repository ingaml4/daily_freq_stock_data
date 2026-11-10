import base64
import hashlib
import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from emscan import auction_validation as audit


def at(clock="09:15:05", day=audit.EXPECTED_DATE):
    return datetime.fromisoformat(f"{day} {clock}").replace(tzinfo=audit.SHANGHAI)


@pytest.fixture
def inputs(tmp_path):
    cfg = {"web.password": "local-secret-密", "web.port": 8180, "mairui.licence": "provider-secret"}
    raw = b'{ "items": [], "fixture_number":1.00 }'
    return {
        "output": tmp_path / "sample.jsonl",
        "expected_date": audit.EXPECTED_DATE,
        "clock": lambda: at(),
        "load_config": lambda: cfg,
        "calendar": Mock(return_value=True),
        "get": Mock(return_value=SimpleNamespace(content=raw, status_code=200)),
    }


def records(kwargs):
    return [json.loads(x) for x in kwargs["output"].read_text(encoding="utf8").splitlines()]


@pytest.mark.parametrize(
    "day,clock",
    [
        ("2026-11-10", "09:15:05"),
        ("2026-11-12", "09:15:05"),
        ("2026-11-11", "09:14:59"),
        ("2026-11-11", "09:22:30.000001"),
        ("2026-11-11", "10:00:00"),
    ],
)
def test_outside_schedule_never_loads_config_or_calls_http(inputs, day, clock):
    inputs["clock"] = lambda: at(clock, day)
    inputs["load_config"] = Mock(side_effect=AssertionError)
    assert audit.sample_once(**inputs) == "skipped_window"
    inputs["load_config"].assert_not_called()
    inputs["calendar"].assert_not_called()
    inputs["get"].assert_not_called()
    assert not inputs["output"].exists()


def test_wrong_expected_date_cannot_enable_another_day(inputs):
    inputs["expected_date"] = "2026-11-10"
    inputs["clock"] = lambda: at(day="2026-11-10")
    assert audit.sample_once(**inputs) == "skipped_window"
    inputs["get"].assert_not_called()


def test_window_rechecked_after_preflight(inputs):
    stamps = iter([at("09:22:29"), at("09:22:31"), at("09:22:31")])
    inputs["clock"] = lambda: next(stamps)
    assert audit.sample_once(**inputs) == "skipped_window"
    inputs["get"].assert_not_called()
    assert records(inputs)[0]["requested_at"] is None


@pytest.mark.parametrize("decision", [False, None, 1])
def test_only_explicit_confirmed_calendar_is_permitted(inputs, decision):
    inputs["calendar"].return_value = decision
    assert audit.sample_once(**inputs) == "calendar_unavailable"
    inputs["get"].assert_not_called()
    assert records(inputs)[0]["http_status"] is None


@pytest.mark.parametrize("moment", ["09:15:00", "09:22:30"])
def test_capture_contains_exact_local_endpoint_raw_bytes_times_and_private_mode(inputs, moment):
    inputs["clock"] = lambda: at(moment).astimezone(timezone.utc)
    for _ in range(2):
        assert audit.sample_once(**inputs) == "sampled"
    a, b = records(inputs)
    assert a == b
    assert a["endpoint"] == "/api/auction-snapshot" and a["query"] == "symbols=SZ300611,SZ300969"
    url = "http://127.0.0.1:8180/api/auction-snapshot?symbols=SZ300611,SZ300969"
    args = inputs["get"].call_args.args
    assert args[0] == url == a["request_url"]
    assert base64.b64decode(args[1].split()[1]).decode("utf8") == "emscan:local-secret-密"
    assert a["response_raw"].encode() == inputs["get"].return_value.content
    assert a["response_sha256"] == hashlib.sha256(a["response_raw"].encode()).hexdigest()
    assert a["response"]["fixture_number"] == 1 and a["http_status"] == 200
    assert a["requested_at"].endswith("+08:00") and a["received_at"] == a["requested_at"]
    assert not any(
        x in inputs["output"].read_text(encoding="utf8")
        for x in ["local-secret", "provider-secret", args[1]]
    )
    if os.name == "posix":
        assert stat.S_IMODE(inputs["output"].stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "case,expected",
    [
        ("401", "http_error"),
        ("raw", "invalid_response"),
        ("provider", "sensitive_response_omitted"),
        ("echo", "sensitive_response_omitted"),
        ("transport", "transport_error"),
        ("calendar", "calendar_unavailable"),
        ("config", "configuration_error"),
    ],
)
def test_faults_are_bounded_and_do_not_archive_secrets(inputs, case, expected):
    secret = "provider-secret"
    if case == "401":
        inputs["get"].return_value = SimpleNamespace(status_code=401, content=secret.encode())
    elif case in ("raw", "provider", "echo"):
        raw = {
            "raw": secret,
            "provider": json.dumps({"items": [{"value": secret}]}),
            "echo": json.dumps({"items": [], "token": secret}),
        }[case]
        inputs["get"].return_value = SimpleNamespace(status_code=200, content=raw.encode())
    elif case == "transport":
        inputs["get"].side_effect = RuntimeError(secret)
    elif case == "calendar":
        inputs["calendar"].side_effect = RuntimeError(secret)
    else:
        inputs["load_config"] = Mock(side_effect=RuntimeError(secret))
    assert audit.sample_once(**inputs) == expected
    assert secret not in inputs["output"].read_text(encoding="utf8")
    assert "response" not in records(inputs)[0]
    assert inputs["get"].call_count == int(case not in ("calendar", "config"))


def test_requests_direct_no_redirect_and_timeout():
    with patch.object(audit.requests, "Session") as Session:
        client = Session.return_value.__enter__.return_value
        resp = audit._get("http://127.0.0.1:8080/api/auction-snapshot", "Basic encoded")
        assert client.trust_env is False and resp is client.get.return_value
        client.get.assert_called_once_with(
            "http://127.0.0.1:8080/api/auction-snapshot",
            headers={"Authorization": "Basic encoded"},
            timeout=(1, 7),
            allow_redirects=False,
        )


def test_official_cache_required_even_if_trading_days_has_row(tmp_path):
    stamp = at()
    path = tmp_path / "db.sqlite"
    cfg = Mock()
    cfg.path.return_value = path
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE trading_days(date TEXT)")
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO trading_days VALUES(?)", (audit.EXPECTED_DATE,))
        conn.commit()
        assert audit._official_session(cfg, stamp) is None
        for expire, days, answer in [
            (-1, [audit.EXPECTED_DATE], None),
            (60, [], False),
            (60, [audit.EXPECTED_DATE], True),
        ]:
            cached = {"expires": stamp.timestamp() + expire, "days": days}
            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES(?,?)",
                ("mairui_calendar:2026", json.dumps(cached)),
            )
            conn.commit()
            assert audit._official_session(cfg, stamp) is answer
    finally:
        conn.close()


def test_read_only_calendar_check_cannot_create_missing_database(tmp_path):
    target = tmp_path / "absent.sqlite"
    cfg = Mock()
    cfg.path.return_value = target
    with pytest.raises(sqlite3.OperationalError):
        audit._official_session(cfg, at())
    assert not target.exists()
