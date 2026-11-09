import json
from argparse import Namespace

import pandas as pd
import pytest
import requests

from helpers import bar, make_cfg, trading_dates
from emscan import billing, summary
from emscan.backtest import evaluate
from emscan.cli import cmd_box, cmd_doji, open_store
from emscan.doji import compute_reversal_features
from emscan.history import fill_history
from emscan.web import publication_note


def test_rerun_removes_suspended_cached_hits(tmp_path):
    cfg = make_cfg(tmp_path, {"report": {"formats": ["csv"]}})
    store = open_store(cfg)
    days = trading_dates(70)
    active, paused = "0.000001", "0.002129"
    store.upsert_bars([bar(s, day, 5000) for s in (active, paused) for day in days[:-1]])
    store.upsert_bars([bar(active, days[-1], 5000)])
    store.upsert_daily_signals(
        [{"secid": paused, "date": days[-1], "s_box_range": 1, "s_reversal": 1}]
    )
    store.close()
    for command, prefix in [(cmd_box, "box"), (cmd_doji, "doji")]:
        assert command(cfg, Namespace(trade_date=days[-1])) == 0
        report = cfg.path("store.reports_dir") / f"{prefix}_{days[-1]}.csv"
        assert paused.split(".")[1] not in report.read_text(encoding="utf-8-sig")
    with open_store(cfg) as store:
        cache = store.load_daily_signals(start=days[-1])
        assert set(cache.secid) == {active}
        assert cache.iloc[0].reversal_bars == 70
        assert pd.isna(cache.iloc[0].doji_drawdown)


def test_long_window_only_uses_full_history():
    for n in (40, 60, 120):
        rows = [
            bar("1.600001", day, 1000, close=10 + i / 100) for i, day in enumerate(trading_dates(n))
        ]
        item = compute_reversal_features(pd.DataFrame(rows)).iloc[0]
        assert item.reversal_bars == n
        assert pd.isna(item.doji_pos) == (n < 60)
        assert pd.isna(item.doji_drawdown) == (n < 120)


def test_history_preserves_snapshot_and_rotates_short_new_listing(tmp_path):
    cfg = make_cfg(tmp_path)
    with open_store(cfg) as store:
        code = "0.000001"
        today = bar(code, "2026-11-06", 2000)
        store.upsert_symbols([{"secid": code, "code": "000001"}])
        store.upsert_bars([today])

        class Client:
            calls = 0

            def fetch_kline_many(self, codes, **kwargs):
                self.calls += 1
                return {code: [dict(today, close=200), bar(code, "2026-11-05", 100)]}, []

        client = Client()
        for _ in range(2):
            assert fill_history(store, client, cfg, cooldown_days=7) == 0
        bars = store.load_bars()
        assert len(bars) == 2 and bars[bars.date == today["date"]].iloc[0].close == today["close"]
        assert client.calls == 1


def test_402_incident_survives_retry_and_closes_on_success(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    day = "2026-11-06"

    def fail(*args):
        resp = requests.Response()
        resp.status_code = 402
        raise requests.HTTPError(response=resp)

    monkeypatch.setattr(summary, "call_deepseek", fail)
    with open_store(cfg) as store:
        for attempt in range(2):
            record = summary.generate_summary(store, cfg, day, api_key="test", force=True)
            assert "402" in json.loads(record["digest"])["_fallback_reason"]
            incident = store.get_meta(billing.KEY)
            if attempt == 0:
                first = incident
            assert incident == first and incident
        assert "请充值" in publication_note(store.publication_status(day))
        monkeypatch.setattr(summary, "call_deepseek", lambda *args: "TIER1: 空\nTIER2: 空")
        monkeypatch.setattr(summary, "_summary_ready", lambda *args: (True, ""))
        assert not summary.generate_summary(store, cfg, day, api_key="test")["engine"].startswith(
            "rule"
        )
        assert store.get_meta(billing.KEY) is None


def test_late_tier_pick_has_no_live_returns(tmp_path):
    cfg = make_cfg(tmp_path)
    with open_store(cfg) as store:
        code = "0.000001"
        days = trading_dates(4)
        store.upsert_bars([bar(code, d, 30) for d in days])
        store.save_tier_picks([{"date": days[0], "secid": code, "generated_at": days[2]}])
        res = evaluate(store, cfg)
        assert res["rows"][0]["status"] == "事后补生成"
        assert res["rows"][0]["rets"] == {}
        assert res["by_tier"] == {} and res["by_date"] == []


def test_recover_and_preserve_published_ai_with_manual_overlap(tmp_path, monkeypatch):
    day = "2026-11-06"
    cfg = make_cfg(tmp_path)
    stored = "TIER1: 000001 平安银行 | 600519 贵州茅台\nTIER2: 空"
    monkeypatch.setattr(summary, "call_deepseek", lambda *_: pytest.fail("重复请求 AI"))
    with open_store(cfg) as store:
        store.upsert_symbols([
            {"secid": "0.000001", "code": "000001", "name": "平安银行"},
            {"secid": "1.600519", "code": "600519", "name": "贵州茅台"}
        ])
        manual = {"date": day, "tier": 1, "secid": "0.000001", "source": "manual",
                  "reason": "手工入选", "generated_at": day + " 16:00:00"}
        store.save_tier_picks([manual])
        store.save_summary({"date": day, "engine": "deepseek", "content": stored})
        original = store.get_summary(day)
        for force_rule in (False, True):
            result = summary.generate_summary(store, cfg, day, api_key="test",
                                              force=True, force_rule=force_rule)
            assert result["reused"] and store.get_summary(day) == original
            picks = store.load_tier_picks().set_index("secid")
            assert picks.loc["1.600519", "created_at"] == original["created_at"]
            assert picks.loc["0.000001", "source"] == "manual"
            assert picks.loc["0.000001", "created_at"] == manual["generated_at"]
            assert picks.loc["0.000001", "reason"] == manual["reason"]


def test_competing_publisher_cannot_replace_snapshot(tmp_path):
    day = "2026-11-06"
    cfg = make_cfg(tmp_path)
    with open_store(cfg) as first, open_store(cfg) as second:
        initial = second.get_summary(day)
        record = {"date": day, "engine": "deepseek", "content": "first"}
        assert first.publish_summary(record, initial) is None
        preserved = second.publish_summary(dict(record, content="second"), initial)
        assert preserved["reused"] and preserved["content"] == "first"
        assert second.get_summary(day)["content"] == "first"
