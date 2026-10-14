"""复盘总结的请求参数与"思考模式不给正文/被截断"的兜底逻辑。"""

import types

import pytest
import requests
from helpers import make_cfg

from emscan import summary as summary_mod
from emscan.summary import (
    MAX_TOKENS_CEILING,
    _build_body,
    call_deepseek,
    extract_reasoning,
    extract_text,
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
    replies = [_payload(content="半截但有用", finish="length"), _payload(content="", finish="length")]

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
