"""复盘总结的请求参数与"思考模式不给正文/被截断"的兜底逻辑。"""

import json
import types

import pandas as pd
import pytest
import requests
from helpers import make_cfg

from emscan import summary as summary_mod
from emscan.summary import (
    MAX_TOKENS_CEILING,
    _build_body,
    build_digest,
    call_deepseek,
    extract_reasoning,
    extract_text,
    generate_summary,
    mode_text,
)


def _payload(content="正文", reasoning="", finish="stop"):
    message = {"content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200},
    }


def test_body_defaults_to_thinking_with_roomy_budget(tmp_path):
    """默认开思考 + 档位 high + 给足 max_tokens（思考 token 也算在里面）。"""
    cfg = make_cfg(tmp_path)
    body = _build_body(cfg, "deepseek-flash", "prompt", True)
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    assert body["max_tokens"] == 32000
    assert "思考" in mode_text(cfg)


def test_body_can_turn_thinking_off_and_drop_budget(tmp_path):
    """关思考要走 effort=none；max_tokens 填 0 表示不传（用官方默认值）。"""
    cfg = make_cfg(tmp_path, {"summary": {"thinking": False, "max_tokens": 0}})
    body = _build_body(cfg, "deepseek-flash", "prompt", False)
    assert body["thinking"] == {"type": "disabled"}
    assert body["reasoning_effort"] == "none"
    assert "max_tokens" not in body
    assert "非思考模式" in mode_text(cfg)

    # effort=none 本身就等于关思考（官方文档），即使 thinking 仍是默认的 true
    cfg = make_cfg(tmp_path, {"summary": {"reasoning_effort": "none"}})
    assert _build_body(cfg, "deepseek-flash", "prompt", False)["thinking"]["type"] == "disabled"


def test_body_accepts_max_effort_and_clamps_budget(tmp_path):
    """官方支持 effort=max；max_tokens 超过上限按上限夹住。"""
    cfg = make_cfg(tmp_path, {"summary": {"reasoning_effort": "max", "max_tokens": 999_999}})
    body = _build_body(cfg, "deepseek-flash", "prompt", True)
    assert body["reasoning_effort"] == "max"
    assert body["max_tokens"] == MAX_TOKENS_CEILING
    # 老写法 medium 映射到 high
    cfg = make_cfg(tmp_path, {"summary": {"reasoning_effort": "medium"}})
    assert _build_body(cfg, "deepseek-flash", "prompt", True)["reasoning_effort"] == "high"


def test_extract_text_ignores_chain_of_thought():
    """思维链是过程不是结论：只有 reasoning_content 时视为没有正文，好让兜底重试接管。"""
    only_cot = {"choices": [{"message": {"content": "", "reasoning_content": "我先看看板块…"}}]}
    assert extract_text(only_cot) == ""
    assert extract_reasoning(only_cot) == "我先看看板块…"
    assert extract_text({"choices": [{"message": {"content": "结论"}}]}) == "结论"


def test_call_deepseek_retries_without_thinking_when_truncated(tmp_path, monkeypatch):
    """思考预算被吃满（finish_reason=length）→ 自动关思考重试，用完整的那一份。"""
    cfg = make_cfg(tmp_path)
    seen = []
    replies = [
        _payload(content="半截正文", reasoning="想了很多", finish="length"),
        _payload(content="完整正文", finish="stop"),
    ]

    def fake_post(url, headers, body, timeout):
        seen.append(body)
        return replies[len(seen) - 1]

    monkeypatch.setattr(summary_mod, "_post_json", fake_post)
    text = call_deepseek(cfg, "prompt", "sk-test")
    assert text == "完整正文"
    assert seen[0]["thinking"] == {"type": "enabled"}
    assert seen[1]["thinking"] == {"type": "disabled"}
    assert seen[1]["reasoning_effort"] == "none"


def test_call_deepseek_retries_when_thinking_returns_empty(tmp_path, monkeypatch):
    """思考模式只回思维链（正文空）→ 关思考重试，别把思维链当复盘发出去。"""
    cfg = make_cfg(tmp_path)
    seen = []
    replies = [
        _payload(content="", reasoning="只有思维链", finish="length"),
        _payload(content="正式复盘", finish="stop"),
    ]

    def fake_post(url, headers, body, timeout):
        seen.append(body)
        return replies[len(seen) - 1]

    monkeypatch.setattr(summary_mod, "_post_json", fake_post)
    assert call_deepseek(cfg, "prompt", "sk-test") == "正式复盘"
    assert len(seen) == 2


def test_call_deepseek_keeps_partial_when_retry_is_empty(tmp_path, monkeypatch):
    """重试也没正文时，不要丢掉已经拿到的半截内容。"""
    cfg = make_cfg(tmp_path)
    seen = []
    replies = [
        _payload(content="半截但有用", finish="length"),
        _payload(content="", finish="length"),
    ]

    def fake_post(url, headers, body, timeout):
        seen.append(body)
        return replies[len(seen) - 1]

    monkeypatch.setattr(summary_mod, "_post_json", fake_post)
    assert call_deepseek(cfg, "prompt", "sk-test") == "半截但有用"


def test_call_deepseek_drops_unsupported_params_on_400(tmp_path, monkeypatch):
    """接口不认 max_tokens/reasoning_effort 时，去掉这两个参数再试一次（不整单失败）。"""
    cfg = make_cfg(tmp_path)
    bodies = []

    class Resp:
        status_code = 400
        text = '{"error":{"message":"Invalid parameter: max_tokens"}}'

        @staticmethod
        def json():
            return _payload(content="重试成功")

        @staticmethod
        def raise_for_status():
            raise requests.HTTPError("400 Client Error", response=Resp())

    def fake_post(url, headers, json, timeout):
        bodies.append(json)
        if len(bodies) == 1:
            return Resp()
        return types.SimpleNamespace(
            status_code=200,
            text="{}",
            json=lambda: _payload(content="重试成功"),
            raise_for_status=lambda: None,
        )

    monkeypatch.setattr(summary_mod.requests, "post", fake_post)
    assert call_deepseek(cfg, "prompt", "sk-test") == "重试成功"
    assert "max_tokens" in bodies[0]
    assert "max_tokens" not in bodies[1] and "reasoning_effort" not in bodies[1]


def test_call_deepseek_reraises_other_http_errors(tmp_path, monkeypatch):
    """别的 4xx/5xx 不要吞掉，交给上层出规则版总结并记录错误。"""
    cfg = make_cfg(tmp_path)

    class Resp:
        status_code = 401
        text = '{"error":{"message":"Authentication Fails"}}'

        @staticmethod
        def json():
            return {}

        @staticmethod
        def raise_for_status():
            raise requests.HTTPError("401 Client Error", response=Resp())

    monkeypatch.setattr(summary_mod.requests, "post", lambda *a, **k: Resp())
    with pytest.raises(requests.HTTPError):
        call_deepseek(cfg, "prompt", "sk-bad")


def test_generate_summary_labels_engine_with_thinking_mode(tmp_path, monkeypatch):
    """落库的 engine 要写明思考档位，网页上能一眼确认这份复盘开了思考。"""
    from emscan.summary import generate_summary

    cfg = make_cfg(tmp_path)
    saved = {}

    class Store:
        @staticmethod
        def get_summary(_date):
            return None

        @staticmethod
        def save_summary(record):
            saved.update(record)

    monkeypatch.setattr(summary_mod, "build_digest", lambda *a, **k: {"date": "2026-10-13"})
    monkeypatch.setattr(summary_mod, "call_deepseek", lambda *a, **k: "复盘正文")
    record = generate_summary(Store(), cfg, "2026-10-13", api_key="sk-test")
    assert record["content"] == "复盘正文"
    assert saved["engine"] == "deepseek-flash(thinking=high)"


def _signals_frame(rows):
    return pd.DataFrame(rows)


class _Store:
    def __init__(self, frame):
        self.frame = frame

    def load_daily_signals(self, **_kwargs):
        return self.frame


def test_digest_gives_every_hit_and_filters_vol_spike_by_pct(tmp_path):
    """放量大涨必须带涨幅门槛（放量下跌不算）；命中名单默认不抽样。"""
    cfg = make_cfg(tmp_path)
    rows = [
        # 放量上涨 → 算放量大涨
        dict(
            secid="1.600001",
            code="600001",
            name="放量上涨",
            board="sh_main",
            pct_chg=6.0,
            amount=5e8,
            turnover=3.0,
            vol_ratio_5=4.0,
            s_vol_spike=1,
            s_kdj_golden=1,
            kdj_j=80.0,
        ),
        # 放量但下跌 → 不该进"放量大涨"（曾经就是因为这个被 AI 抓出来）
        dict(
            secid="1.600002",
            code="600002",
            name="放量下跌",
            board="sz_main",
            pct_chg=-8.9,
            amount=6e8,
            turnover=4.0,
            vol_ratio_5=9.0,
            s_vol_spike=1,
            s_kdj_golden=0,
            kdj_j=20.0,
        ),
        # 放量滞涨（涨幅 1% < 3%）→ 同样不算
        dict(
            secid="1.600003",
            code="600003",
            name="放量滞涨",
            board="sh_main",
            pct_chg=1.0,
            amount=7e8,
            turnover=2.0,
            vol_ratio_5=8.0,
            s_vol_spike=1,
            s_kdj_golden=0,
            kdj_j=30.0,
        ),
    ]
    for index in range(30):  # 造够 30 只命中：默认必须全给，不能只给前 N
        rows.append(
            dict(
                secid=f"0.3000{index:02d}",
                code=f"3000{index:02d}",
                name=f"金叉{index}",
                board="sz_gem",
                pct_chg=2.0,
                amount=1e8,
                turnover=5.0,
                vol_ratio_5=1.0,
                s_vol_spike=0,
                s_kdj_golden=1,
                kdj_j=60.0,
            )
        )
    digest = build_digest(_Store(_signals_frame(rows)), cfg, "2026-10-14")
    assert digest["totals"]["放量大涨"] == 1
    assert [s["名称"] for s in digest["views"]["放量大涨"]] == ["放量上涨"]
    assert digest["totals"]["KDJ金叉"] == 31
    assert len(digest["views"]["KDJ金叉"]) == 31  # 不抽样
    # 口径随配置生成，AI 能读到"只显示涨幅 ≥ 3%"这层过滤
    assert "涨幅 ≥ 3.0%" in digest["口径"]["放量大涨"]
    assert digest["views_stats"]["KDJ金叉"]["只数"] == 31


def test_digest_can_limit_and_include_universe(tmp_path):
    """想省 token 可以限定前 N 只；需要时还能把全市场简表一起喂给 AI。"""
    cfg = make_cfg(tmp_path, {"summary": {"top_symbols": 5, "include_universe": True}})
    rows = [
        dict(
            secid=f"1.6000{index:02d}",
            code=f"6000{index:02d}",
            name=f"票{index}",
            board="sh_main",
            pct_chg=4.0 + index,
            amount=1e8 * (index + 1),
            turnover=2.0,
            vol_ratio_5=3.0,
            s_vol_spike=1,
            s_macd_golden=0,
            s_kdj_golden=0,
            signals="vol_spike,",
        )
        for index in range(12)
    ]
    digest = build_digest(_Store(_signals_frame(rows)), cfg, "2026-10-14")
    assert digest["totals"]["放量大涨"] == 12
    assert len(digest["views"]["放量大涨"]) == 5
    assert len(digest["全市场简表"]) == 12
    assert digest["全市场简表"][0]["命中"] == "vol_spike,"
    text = summary_mod.digest_text(digest, top_n=5)
    assert "前 5 只" in text
    assert "全部" in summary_mod.digest_text(digest, top_n=0)


def test_digest_for_store_keeps_lists_out_of_the_database(tmp_path):
    """AI 看全量，但落库的 digest 只留统计 + 少量代表个股，别把几千行 JSON 存进库。"""
    from emscan.summary import digest_for_store

    digest = {
        "date": "2026-10-14",
        "totals": {"放量大涨": 148},
        "views": {"放量大涨": [{"代码": f"6000{index:02d}"} for index in range(148)]},
        "全市场简表": [{"代码": f"6000{index:02d}"} for index in range(5000)],
    }
    lean = digest_for_store(digest)
    assert lean["totals"] == {"放量大涨": 148}
    assert lean["views"]["放量大涨"]["只数"] == 148
    assert len(lean["views"]["放量大涨"]["代表"]) == 20
    assert lean["全市场简表"]["只数"] == 5000
    assert len(json.dumps(lean, ensure_ascii=False)) < 5000


class _SummaryStore:
    """最小化的 store 桩：只实现 generate_summary 用到的三个方法。"""

    def __init__(self, state=None, existing=None):
        self.state = state
        self.existing = existing
        self.saved = []

    def signal_state(self, _date):
        return self.state

    def get_summary(self, _date):
        return self.existing

    def save_summary(self, record):
        self.saved.append(record)
        self.existing = dict(record)


def test_generate_summary_waits_until_indicator_signals_are_cached(tmp_path):
    """指标信号还没进缓存时不出总结：否则会落一份"MACD/KDJ/布林 0 只"的假复盘。"""
    cfg = make_cfg(tmp_path)
    store = _SummaryStore(state={"rows": 5200, "indicator_filled": 0, "fingerprint": "5200:0"})
    record = generate_summary(store, cfg, "2026-10-19")
    assert record["pending"] is True
    assert "指标" in record["reason"]
    assert store.saved == []


def test_generate_summary_rebuilds_when_signal_fingerprint_changes(tmp_path, monkeypatch):
    """数据没变就复用；命中数变了（指纹变化）自动重算，旧缓存不会一直骗人。"""
    cfg = make_cfg(tmp_path)
    store = _SummaryStore(state={"rows": 10, "indicator_filled": 10, "fingerprint": "a:1"})
    calls = {"n": 0}

    def fake_digest(*_args, **_kwargs):
        calls["n"] += 1
        return {"date": "2026-10-19", "totals": {"MACD金叉": calls["n"]}}

    monkeypatch.setattr(summary_mod, "build_digest", fake_digest)
    first = generate_summary(store, cfg, "2026-10-19")
    assert calls["n"] == 1 and len(store.saved) == 1

    again = generate_summary(store, cfg, "2026-10-19")
    assert again["content"] == first["content"]
    assert calls["n"] == 1 and len(store.saved) == 1  # 指纹没变 → 不重算

    store.state = {"rows": 10, "indicator_filled": 10, "fingerprint": "a:9"}
    third = generate_summary(store, cfg, "2026-10-19")
    assert calls["n"] == 2 and len(store.saved) == 2
    assert third["content"] != first["content"]


def test_generate_summary_rebuilds_legacy_record_without_fingerprint(tmp_path, monkeypatch):
    """旧版落库的总结没有指纹：下一次运行会自动重建，不用手动删库。"""
    cfg = make_cfg(tmp_path)
    legacy = {
        "date": "2026-10-19",
        "engine": "deepseek-flash",
        "content": "旧总结（三个按钮 0 只）",
        "digest": json.dumps({"totals": {"MACD金叉": 0}}, ensure_ascii=False),
    }
    store = _SummaryStore(
        state={"rows": 10, "indicator_filled": 10, "fingerprint": "new"}, existing=legacy
    )
    monkeypatch.setattr(
        summary_mod,
        "build_digest",
        lambda *_a, **_k: {"date": "2026-10-19", "totals": {"MACD金叉": 3}},
    )
    record = generate_summary(store, cfg, "2026-10-19")
    assert record["content"] != legacy["content"]
    assert "3" in record["content"]
    assert json.loads(store.saved[0]["digest"])["_fingerprint"] == "new"


def test_rule_summary_explains_missing_tiers():
    """规则版没有梯队名单，要在正文里说清楚，别让人以为"没按上限"是程序漏了。"""
    text = summary_mod.rule_summary({"date": "2026-10-19", "totals": {"放量大涨": 3}})
    assert "梯队" in text and "未调用 AI" in text
