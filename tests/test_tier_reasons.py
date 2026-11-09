"""原始 AI 复盘、逐股证据、历史份额和鉴权读取接口。"""

import hashlib
import json

import pytest
from helpers import make_cfg
from emscan.backtest import sync_tier_picks
from emscan.store import Store
from emscan.tier_reasons import backfill, readiness, report, source_document
from emscan.watchlist_risk import InvalidRequest


def db(tmp_path):
    store = Store(tmp_path / "market.sqlite").init_schema()
    store.upsert_symbols(
        [
            {"secid": "0.000001", "code": "000001", "name": "平安银行"},
            {"secid": "1.600519", "code": "600519", "name": "贵州茅台"},
        ]
    )
    return store


DAY = "2026-11-06"


def reason(tier, **changes):
    item = dict(
        symbol="SZ000001",
        tier=tier,
        strategy_type="箱体",
        reason="回踩箱底后放量",
        confirmation="次日站回箱体中位",
        invalidation="收盘跌破箱底",
        indicator_asof=DAY,
    )
    item.update(changes)
    return "REASON_JSON " + json.dumps(item, ensure_ascii=False)


def test_new_summary_multiple_tiers_reasons_and_references(tmp_path):
    doc = "\n".join(
        [
            "# 复盘",
            reason(1),
            reason(1, strategy_type="缩量反转", reason="长下影"),
            reason(2, invalidation=None, indicator_asof="2026-11-09"),
            "TIER1: 000001 平安银行 | 600519 贵州茅台",
            "TIER2: 000001 平安银行",
        ]
    )
    with db(tmp_path) as store:
        store.save_summary({"date": DAY, "engine": "test-ai", "content": doc})
        sync_tier_picks(store, make_cfg(tmp_path), dates=[DAY])
        backfill(store)
        answer = report(store.conn, DAY, "SZ000001,SH600519,000001.SZ,HK00700,SH999999")
        assert [r["requested_symbol"] for r in answer["rows"]] == [
            "SZ000001",
            "SH600519",
            "000001.SZ",
            "HK00700",
            "SH999999",
        ]
        first = answer["rows"][0]["entries"]
        assert first == answer["rows"][2]["entries"]
        assert [p["tier"] for p in first] == [1, 2]
        assert len(first[0]["reasons"]) == 2
        assert "indicator_asof" in first[1]["reasons"][0]["gaps"]
        assert answer["rows"][1]["reason"] == "no_attributable_original_reason"
        assert answer["rows"][3]["reason"] == "unsupported_market"
        assert answer["rows"][4]["reason"] == "unknown_symbol"
        ready = answer["readiness"]
        assert (ready["expected"], ready["available"], ready["complete"]) == (3, 2, 1)
        assert ready["fields"]["reason"] == 2 and ready["fields"]["invalidation"] == 1
        source = source_document(store.conn, DAY, ready["source_version"])
        assert source["content_sha256"] == hashlib.sha256(doc.encode()).hexdigest()
        assert source["content"] == doc
        assert source["first_published_at"] == store.get_summary(DAY)["created_at"]
        for group in first:
            for item in group["reasons"]:
                assert item["quote"] in doc.splitlines()[item["line"] - 1]


def test_legacy_evidence_preserves_versions_and_does_not_assign_group_prose(tmp_path):
    old = (
        "平安银行和贵州茅台：均需放量突破前高后确认。\n"
        "优先跟踪平安银行，放量突破箱顶。贵州茅台涨 3%。\n"
        "TIER1: 000001 平安银行\nTIER2: 600519 贵州茅台"
    )
    changed = "TIER1: 600519 贵州茅台（回踩支撑）\nTIER2: 空"
    with db(tmp_path) as store:
        store.save_summary({"date": DAY, "engine": "original", "content": old})
        store.save_summary({"date": DAY, "engine": "new", "content": changed})
        for _ in range(2):
            backfill(store)
        answer = report(store.conn, DAY, "SZ000001,SH600519")
        assert len(answer["sources"]) == 2
        one = answer["rows"][0]["entries"][0]["reasons"]
        assert len(one) == 1 and one[0]["reason"] == "优先跟踪平安银行，放量突破箱顶"
        assert one[0]["confirmation"] is None and one[0]["invalidation"] is None
        entries = answer["rows"][1]["entries"]
        assert len(entries) == 2
        old = next(x for x in entries if x["tier"] == 2)
        assert old["status"] == "unavailable"
        assert old["reasons"][0]["origin"] == "related_observation"
        assert old["reasons"][0]["reason"] is None
        assert readiness(store.conn, DAY)["expected"] == 1


def test_manual_rule_ambiguous_and_limits(tmp_path):
    with db(tmp_path) as store:
        store.upsert_symbols([{"secid": "1.600520", "code": "600520", "name": "平安银行"}])
        store.save_summary(
            {
                "date": DAY,
                "engine": "test-ai",
                "content": "优先跟踪平安银行，缩量回踩。\nTIER1: 平安银行\nTIER2: 空",
            }
        )
        store.save_tier_picks([{"date": DAY, "secid": "0.000001", "tier": 1, "source": "manual"}])
        backfill(store)
        assert report(store.conn, DAY, "SZ000001")["rows"][0]["reason"] == "not_in_original_tiers"
        store.save_summary(
            {"date": "2026-11-05", "engine": "rule", "content": "TIER1: 000001 平安银行"}
        )
        backfill(store)
        prev = report(store.conn, "2026-11-05", "SZ000001")
        assert not prev["sources"] and prev["rows"][0]["reason"] == "no_ai_source"
        for day, symbols in [
            (DAY, ""),
            ("no date", "SZ000001"),
            (DAY, ",".join(["SZ000001"] * 101)),
        ]:
            with pytest.raises(InvalidRequest):
                report(store.conn, day, symbols)


def test_projected_bulk_endpoint_auth_and_exact_source_download(tmp_path):
    import requests
    from emscan.web import serve

    with db(tmp_path) as store:
        doc = "TIER1: 000001 平安银行（放量突破）\nTIER2: 空"
        store.save_summary({"date": DAY, "engine": "ai", "content": doc})
        backfill(store)
        path = store.path
    server = serve(lambda: Store(path), host="127.0.0.1", port=0, password="test", blocking=False)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        url = base + "/api/tier-reasons"
        params = {"date": DAY, "symbols": "SZ000001,SZ000001"}
        assert requests.get(url, params=params, timeout=5).status_code == 401
        auth = ("", "test")
        res = requests.get(url, params=params, auth=auth, timeout=5)
        assert res.status_code == 200
        result = res.json()
        assert len(result["rows"]) == 2
        link = base + result["sources"][0]["url"]
        assert requests.get(link, timeout=5).status_code == 401
        assert requests.get(link, auth=auth, timeout=5).json()["content"] == doc
        assert (
            requests.get(
                url + "/source", params={"date": DAY, "version": "0" * 64}, auth=auth, timeout=5
            ).status_code
            == 404
        )
    finally:
        server.shutdown()
        server.server_close()
