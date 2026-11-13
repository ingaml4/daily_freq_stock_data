from datetime import datetime
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from emscan import entry_capture_backup as capture


def stamp(t="09:30:05"):
    return datetime.fromisoformat("2026-11-16T" + t + "+08:00")


def raw(provider, at="093000", qty=6):
    if provider == "tencent":
        fields = ["0"] * 50
        for key, val in {1:"样本", 3:"19.16", 4:"19.53", 5:"19.10", 6:"1000",
                         9:"19.18", 10:"2", 19:"19.19", 20:str(qty), 30:"20261116"+at}.items():
            fields[key] = val
        return 'v_sz300629="' + "~".join(fields) + '";'
    fields = ["0"] * 33
    for key, val in {0:"样本", 3:"19.16", 2:"19.53", 1:"19.10", 8:"100000",
                     11:"19.18", 10:"200", 21:"19.19", 20:str(qty*100), 30:"2026-11-16",
                     31:at[:2]+":"+at[2:4]+":"+at[4:], 32:"00"}.items():
        fields[key] = val
    return 'var hq_str_sz300629="' + ",".join(fields) + '";'


def test_two_provider_quantity_units_agree():
    one, two = [capture.parse_quotes(p, raw(p))["SZ300629"] for p in capture.PROVIDERS]
    for key in ("source_at", "price", "open_price", "previous_close", "volume_shares", "ask_price", "ask_shares"):
        assert one[key] == two[key]
    assert one["ask_shares"] == 600


def test_stale_response_preserves_success_but_cannot_become_usable_quote():
    row = capture.sample("tencent", ["SZ300629"], fetch=lambda *a: raw("tencent", "092954"), clock=stamp)
    assert row["transport_success"] is True
    assert row["quote_usability"]["SZ300629"] == "outside_continuous_session"
    assert row["quotes"]["SZ300629"]["source_at"] == stamp("09:29:54").isoformat()
    assert "complete" not in row
    assert row["raw_response"]


def test_fault_and_missing_symbol_are_distinct():
    def failed(*a):
        raise TimeoutError("provider_wall_deadline")
    timeout = capture.sample("tencent", ["SZ300629"], fetch=failed, clock=stamp)
    missing = capture.sample("tencent", ["SZ300629"], fetch=lambda *a: "", clock=stamp)
    assert not timeout["transport_success"]
    assert missing["transport_success"] and not missing["quotes"]
    assert "TimeoutError" in timeout["error"]


def test_hard_deadline_and_correct_subprocess_module(monkeypatch):
    def stalled(args, **kw):
        assert args[1:3] == ["-m", "emscan.entry_capture_backup"]
        assert kw["timeout"] == 2.5
        raise subprocess.TimeoutExpired(args, kw["timeout"])
    monkeypatch.setattr(subprocess, "run", stalled)
    with pytest.raises(TimeoutError, match="provider_wall_deadline"):
        capture.request("tencent", ["SZ300629"])


@pytest.mark.parametrize("change,expected", [
    ({"ask_shares":0}, "no_displayed_ask"),
    ({"bid_price":20}, "crossed_book"),
    ({"source_at":stamp("09:30:06").isoformat()}, "stale_or_future_quote"),
    ({"source_at":"2026-11-13T09:30:00+08:00"}, "wrong_trade_date"),
])
def test_quote_ineligibility(change, expected):
    row = capture.parse_quotes("tencent", raw("tencent"))["SZ300629"]
    assert capture.usability({**row, **change}, stamp()) == expected


def test_plan_scope_and_calendar(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"trade_date":"2026-11-16", "symbols":["SZ300629"]}))
    assert capture.load_plan(p, "2026-11-16")["symbols"] == ["SZ300629"]
    with pytest.raises(ValueError, match="date_mismatch"):
        capture.load_plan(p, "2026-11-17")
    p.write_text(json.dumps({"trade_date":"2026-11-16", "symbols":["SH688001"]}))
    with pytest.raises(ValueError, match="symbols"):
        capture.load_plan(p, "2026-11-16")
    db = tmp_path / "calendar.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    with pytest.raises(ValueError, match="calendar_missing"):
        capture.trading_day(db, "2026-11-16")


def test_probe_independent_source_failure_and_restart_segments(tmp_path, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"trade_date":"2026-11-16", "symbols":["SZ300629"]}))
    monkeypatch.setattr(capture, "trading_day", lambda *a: True)
    real_sample = capture.sample
    def fake(provider, symbols, **kw):
        def fetch(*args):
            if provider == "tencent":
                raise TimeoutError("injected")
            return raw(provider)
        return real_sample(provider, symbols, fetch=fetch, clock=kw["clock"])
    monkeypatch.setattr(capture, "sample", fake)
    monkeypatch.setattr(capture, "minute_sample", lambda *a, **k: {
        "source":"tencent-minute", "received_at":k["clock"]().isoformat(),
        "requested_at":k["clock"]().isoformat(), "minutes":[], "error":None,
        "transport_success":True, "quotes":{}, "quote_usability":{},
    })
    out = tmp_path / "output"
    assert capture.run(plan, out, "unused", probe=True, clock=stamp) == "probe_complete"
    state = json.loads((out / "state.json").read_bytes())
    assert state["providers"]["tencent"]["transport_success"] is False
    assert state["providers"]["sina"]["usability"] == {"fresh_displayed_ask":1}
    first = (out / state["output_file"]).read_bytes()
    # A kill during a write may leave a partial UTF-8 tail. Preserve it and resume.
    first += b"\xff\n"
    (out / state["output_file"]).write_bytes(first)
    assert capture.run(plan, out, "unused", probe=True, clock=lambda:stamp("09:30:10")) == "probe_complete"
    state2 = json.loads((out / "state.json").read_bytes())
    assert state2["previous_receipts"]["sina"] == stamp().isoformat()
    assert (out / state["output_file"]).read_bytes() == first
    assert state2["output_file"] != state["output_file"]
    assert state2["prior_unreadable_lines"] == 1


def test_boot_outside_planned_date_skips_without_network_or_calendar(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"trade_date":"2026-11-16", "symbols":["SZ300629"]}))
    after = datetime.fromisoformat("2026-11-17T09:10:00+08:00")
    assert capture.run(plan, tmp_path / "out", "missing.db", clock=lambda:after) == "different_trade_date"
    assert not (tmp_path / "out").exists()


def test_minute_end_label_does_not_turn_opening_tick_or_forming_bar_into_closed_minute():
    raw_data = json.dumps({"data":{"sz300629":{"data":{"date":"20261116", "data":[
        "0930 19.1 1000 1000", "0931 19.27 1000 1000", "0932 19.37 1000 1000",
    ]}}}})
    row = capture.minute_sample("SZ300629", fetch=lambda *a:raw_data, clock=lambda:stamp("09:31:05"))
    assert len(row["minutes"]) == 1
    minute = row["minutes"][0]
    assert minute["label"] == stamp("09:30:00").isoformat()
    assert minute["end"] == stamp("09:31:00").isoformat()
    assert minute["close"] == 19.27 and minute["low"] is None
    late = capture.minute_sample("SZ300629", fetch=lambda *a:raw_data, clock=lambda:stamp("09:33:01"))
    assert late["minutes"] == []


def test_minute_stale_date_is_not_current_evidence():
    data = json.dumps({"data":{"sz300629":{"data":{"date":"20261113", "data":[]}}}})
    row = capture.minute_sample("SZ300629", fetch=lambda *a:data, clock=stamp)
    assert row["transport_success"] and row["minutes"] == []
    assert "minute_date_mismatch" in row["error"]
