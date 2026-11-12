from datetime import datetime, timedelta
import subprocess
import sys
import json
import sqlite3

import pytest

from emscan import sector_snapshot as ss


NOW = datetime(2026, 11, 12, 10, 0, tzinfo=ss.TZ)


def test_scheduler_checks_cached_calendar_and_closed_hours(tmp_path, monkeypatch):
    db = tmp_path / "calendar.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE meta(key TEXT, value TEXT)")
        conn.execute("INSERT INTO meta VALUES (?,?)", ("mairui_calendar:2026", json.dumps({"days": ["2026-11-12"]})))
    monkeypatch.setattr("sys.argv", ["sector", "--membership", "members.json", "--output", "out.json", "--market-hours", "--calendar-db", str(db)])
    calls = []
    def collect(*args, **kwargs):
        calls.append(kwargs)
        return {"generated_at": NOW.isoformat(), "batches": []}
    monkeypatch.setattr(ss, "collect", collect)
    monkeypatch.setattr(ss, "_now", lambda: NOW)
    assert ss.main() == 0 and calls[-1]["calendar_verified"] is True
    monkeypatch.setattr(ss, "_now", lambda: NOW + timedelta(days=1))
    assert ss.main() == 0 and len(calls) == 1
    monkeypatch.setattr(ss, "_now", lambda: NOW.replace(hour=12))
    assert ss.main() == 0 and len(calls) == 1


def test_b_share_prices_do_not_claim_cny_amount():
    rows = ss._parse(quote("SH900913") + "\n" + quote("SZ200001") + "\n" + quote("SH600001"))
    assert rows["SH900913"]["price_currency"] == "USD"
    assert rows["SZ200001"]["price_currency"] == "HKD"
    assert rows["SH900913"]["amount_yuan"] is None
    assert rows["SZ200001"]["amount_yuan"] is None
    assert rows["SH600001"]["amount_yuan"] == 10000


def quote(symbol, timestamp="20261112100000", name="ST Test", price="10", lots="12", amount="10000", change="11.11"):
    fields = [""] * 37
    fields[1], fields[3], fields[4], fields[5], fields[6] = name, price, "9", "9.5", "12"
    fields[30], fields[32] = timestamp, "11.11"
    fields[32], fields[35], fields[36] = change, "12/12/" + amount, lots
    return f'v_{symbol.lower()}="' + "~".join(fields) + '";'


def test_batches_coverage_and_read_without_network(tmp_path, monkeypatch):
    members = [f"SZ{i:06d}" for i in range(101)] + ["SH688001", "BJ920010", "SH900913"]
    calls = []
    def fetch(batch):
        calls.append(batch)
        return quote(batch[0])
    data = {"sectors": {"gas": {"members": members, "source": "test", "observed_at": "2026-11-11", "verified": False, "expected_count": 105}}}
    path = tmp_path / "snapshot.json"
    result = ss.collect(data, path, fetch=fetch, now=NOW)
    assert list(map(len, calls)) == [100, 3]
    assert not any("SH688001" in batch for batch in calls)
    sector = result["sectors"]["gas"]
    assert sector["listed_count"] == 104 and sector["unidentified_gap_count"] == 1
    assert len(sector["members"]) == 104 and sector["members"][0]["freshness"] == "fresh"
    assert sector["members"][1]["freshness"] == "missing"
    assert sector["members"][101]["action_eligible"] is False
    assert sector["members"][101]["freshness"] == "excluded_no_permission"
    assert sector["coverage"]["monitored"] == 103
    assert sector["members"][102]["permission_status"] == "unknown"
    monkeypatch.setattr(ss, "_download", lambda _: pytest.fail("reader requested upstream"))
    read = ss.build_sector_snapshot_payload(path, now=NOW + timedelta(seconds=91))
    assert read["sectors"]["gas"]["members"][0]["freshness"] == "stale"
    assert read["sectors"]["gas"]["coverage"]["stale"] == 2
    assert read["sectors"]["gas"]["breadth"]["count"] == 0


def test_failure_session_and_lock(tmp_path):
    path = tmp_path / "out.json"
    def fail(_): raise OSError("offline")
    result = ss.collect(["SZ000669", "BJ920014"], path, fetch=fail, now=NOW.replace(hour=12))
    assert result["session"] == "lunch_break"
    assert all(row["error"] == "offline" for row in result["sectors"]["explicit"]["members"])
    assert ss.build_sector_snapshot_payload(path, now=NOW.replace(hour=16))["session"] == "closed"
    with ss.daily_lock(path.resolve()) as acquired:
        assert acquired
        with pytest.raises(RuntimeError, match="already running"):
            ss.collect(["SZ000669"], path, fetch=fail, now=NOW)
    result = ss.collect(["SZ000669"], path, fetch=fail, now=NOW)
    assert result["sectors"]["explicit"]["listed_count"] == 1


def test_lock_released_after_process_death(tmp_path):
    path = tmp_path / "out.json"
    code = ("import sys,time; from emscan.request_cache import daily_lock; "
            "lock=daily_lock(sys.argv[1]); assert lock.__enter__(); "
            "print('held', flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code, str(path.resolve())], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        with pytest.raises(RuntimeError, match="already running"):
            ss.collect(["SZ000669"], path, fetch=lambda _: "", now=NOW)
    finally:
        proc.kill()
        proc.wait(timeout=10)
    result = ss.collect(["SZ000669"], path, fetch=lambda _: "", now=NOW)
    assert result["sectors"]["explicit"]["listed_count"] == 1


def test_rejects_invalid_membership_before_fetch(tmp_path):
    with pytest.raises(ValueError): ss.collect(["bogus"], tmp_path / "out", fetch=lambda _: None)
    with pytest.raises(ValueError): ss.collect(["SZ000001", "SZ000001"], tmp_path / "out", fetch=lambda _: None)


def test_source_time_and_overlapping_sectors(tmp_path):
    calls = []
    def fetch(batch):
        calls.append(batch)
        return quote("SZ000001", "20261111095900") + quote("SZ000002", "")
    data = {"sectors": {"one": ["SZ000001", "SZ000002"], "two": ["SZ000001"]}}
    result = ss.collect(data, tmp_path / "out", fetch=fetch, now=NOW)
    assert calls == [["SZ000001", "SZ000002"]]
    assert [x["freshness"] for x in result["sectors"]["one"]["members"]] == ["stale", "source_time_missing"]
    assert result["sectors"]["two"]["verified"] is False


@pytest.mark.parametrize("price", ["0", "-1", "nan", "inf", "-inf"])
def test_invalid_price_is_missing(tmp_path, price):
    result = ss.collect(["SZ000001"], tmp_path / "out", fetch=lambda _: quote("SZ000001", price=price), now=NOW)
    row = result["sectors"]["explicit"]["members"][0]
    assert row["quote"] is None and row["freshness"] == "missing"


def test_consecutive_comparison_and_breadth(tmp_path):
    path = tmp_path / "out"
    members = ["SZ000001", "BJ920010", "SH900913"]
    def first(_):
        return quote(members[0], lots="12", amount="10000") + quote(members[1], change="-1") + quote(members[2], change="0")
    ss.collect(members, path, fetch=first, now=NOW)
    def second(_):
        return quote(members[0], timestamp="20261112100030", price="11", lots="15", amount="15000") + quote(members[1], timestamp="20261112100030", change="-1") + quote(members[2], timestamp="20261112100030", change="0")
    data = ss.collect(members, path, fetch=second, now=NOW + timedelta(seconds=30))
    sector = data["sectors"]["explicit"]
    comparison = sector["members"][0]["comparison"]
    assert comparison["window_seconds"] == 30
    assert comparison["price_change_pct"] == pytest.approx(10)
    assert comparison["volume_increment_shares"] == 300 and comparison["amount_increment_yuan"] == 5000
    assert sector["breadth"] == {"basis": "fresh_quotes_with_change_pct", "count": 3, "up": 1, "down": 1, "unchanged": 1, "change_median_pct": 0}
    repeated = ss.collect(members, path, fetch=second, now=NOW + timedelta(seconds=31))
    assert repeated["sectors"]["explicit"]["members"][0]["comparison"] is None


def test_cross_lunch_out_of_order_and_bad_increment(tmp_path):
    path = tmp_path / "out"
    member = ["SZ000001"]
    def fetch_at(stamp, lots="12", amount="10000"):
        return lambda _: quote(member[0], timestamp=stamp, lots=lots, amount=amount)
    ss.collect(member, path, fetch=fetch_at("20261112113000"), now=NOW.replace(hour=11, minute=30))
    row = ss.collect(member, path, fetch=fetch_at("20261112130001"), now=NOW.replace(hour=13))["sectors"]["explicit"]["members"][0]
    assert row["comparison"] is None
    row = ss.collect(member, path, fetch=fetch_at("20261112130000"), now=NOW.replace(hour=13))["sectors"]["explicit"]["members"][0]
    assert row["comparison"] is None
    row = ss.collect(member, path, fetch=fetch_at("20261112130030", lots="1", amount="1"), now=NOW.replace(hour=13, second=30))["sectors"]["explicit"]["members"][0]
    assert row["comparison"]["volume_increment_shares"] is None
    assert row["comparison"]["amount_increment_yuan"] is None
