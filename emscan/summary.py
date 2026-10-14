"""当日复盘总结：把六个按钮的命中整理成结构化摘要，可选交给 DeepSeek 生成中文复盘。

流程（只在当天数据齐全后跑，默认 17:00 之后，见 config 的 summary.only_after）：

    build_digest()   从 daily_signals 缓存里抽出：各按钮命中数、板块分布、多信号共振个股、
                     每个按钮的代表个股与关键指标（整齐化、控制长度）
    rule_summary()   不调 AI 时的兜底：用上面的数据直接拼一份 Markdown
    call_deepseek()  有 DEEPSEEK_API_KEY 时，把摘要丢给 DeepSeek 生成"全面一点"的复盘
    generate_summary()/write_summary_files()  落库（daily_summary 表）+ 写 data/reports/summary_<日期>.md

密钥入口（三选一，优先环境变量）：
    export DEEPSEEK_API_KEY=sk-xxx           # 推荐（也可写进 /etc/emscan/emscan.env）
    config.yaml: summary.api_key
    python3 -m emscan summary --api-key sk-xxx
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .store import Store
from .tzutil import now_hm

log = logging.getLogger("emscan")

DEEPSEEK_BASE = "https://api.deepseek.com"
# 当前官方模型名：deepseek-flash（快/便宜，复盘够用）、deepseek-v4-pro（更强，更慢更贵）
DEEPSEEK_MODEL = "deepseek-flash"

VIEW_LABELS = {
    "s_vol_spike": "放量大涨",
    "s_macd_golden": "MACD金叉",
    "s_kdj_golden": "KDJ金叉",
    "s_boll_break_lower": "布林下轨",
    "s_box_range": "箱体做T",
    "s_reversal": "反转信号",
}
VIEW_METRICS = {
    "s_vol_spike": ("vol_ratio_5", "量比5"),
    "s_macd_golden": ("macd_diff", "DIFF"),
    "s_kdj_golden": ("kdj_j", "J"),
    "s_boll_break_lower": ("boll_pos", "布林位置"),
    "s_box_range": ("box_pos", "箱体位置"),
    "s_reversal": ("rev_signals", "证据"),
}

PROMPT = """你是 A 股复盘分析师。下面是程序在 {date} 收盘后扫描出的结构化结果（来自本地行情库，仅包含命中个股）。
请基于**这些数据**写一份中文复盘，Markdown 格式，400~800 字，包含：
1. 今日主线与板块集中度（哪些板块/行业明显扎堆，占命中比例多少）；
2. 结构性特征（涨跌幅分布、量能形态：是低位反转、还是高位放量加速）；
3. 多信号共振个股（同时命中多个按钮的，重点提示）；
4. 反转信号（缩量止跌）名单里，哪些位置更低、证据更多；
5. 需要进一步核实的点与风险提示（例如""板块集体异动可能与消息面有关，需自行核实""）。
要求：只依据给定数据分析，**不要编造新闻、公告或具体事件**；数据没有的就直说""数据未提供""；
数字要与给定数据一致。""".strip()

DIGEST_SCHEMA = """说明：每个按钮下列出命中最多的前 {n} 只；""共振""= 同时命中 ≥2 个按钮。
"""


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def build_digest(store: Store, cfg, trade_date: str, top_n: Optional[int] = None) -> Dict[str, Any]:
    """从缓存里抽一份"紧凑但全面"的当日摘要（给 AI 或直接出兜底总结）。"""
    n = int(top_n or cfg.get("summary.top_symbols", 15) or 15)
    frame = store.load_daily_signals(
        start=str(trade_date), end=str(trade_date), join_symbols=True, limit=20000
    )
    digest: Dict[str, Any] = {"date": str(trade_date), "totals": {}, "boards": {}, "views": {}}
    if frame is None or frame.empty:
        return digest

    rows = frame.to_dict("records")
    digest["totals"]["命中股票数"] = len(rows)
    hits_per_symbol: Dict[str, List[str]] = {}
    for flag, label in VIEW_LABELS.items():
        hit = [r for r in rows if _num(r.get(flag)) == 1]
        digest["totals"][label] = len(hit)
        if not hit:
            continue
        metric_key, metric_label = VIEW_METRICS[flag]
        boost = {
            "s_vol_spike": lambda r: _num(r.get("vol_ratio_5")) or 0,
            "s_macd_golden": lambda r: abs(_num(r.get("macd_bar")) or 0),
            "s_kdj_golden": lambda r: abs((_num(r.get("kdj_j")) or 0) - 50),
            "s_boll_break_lower": lambda r: -(_num(r.get("boll_pos")) or 0),
            "s_box_range": lambda r: -(_num(r.get("box_pos")) or 0),
            "s_reversal": lambda r: _num(r.get("rev_score")) or 0,
        }[flag]
        ranked = sorted(hit, key=lambda r: (boost(r), _num(r.get("amount")) or 0), reverse=True)[:n]
        digest["views"][label] = [
            {
                "代码": str(r.get("code") or ""),
                "名称": str(r.get("name") or ""),
                "板块": str(r.get("board") or ""),
                "涨跌幅%": round(_num(r.get("pct_chg")) or 0, 2),
                "成交额亿": round((_num(r.get("amount")) or 0) / 1e8, 2),
                "换手%": round(_num(r.get("turnover")) or 0, 2),
                metric_label: (
                    r.get(metric_key)
                    if isinstance(r.get(metric_key), str)
                    else round(_num(r.get(metric_key)) or 0, 3)
                ),
            }
            for r in ranked
        ]
        boards: Dict[str, int] = {}
        for r in hit:
            key = str(r.get("board") or "未知")
            boards[key] = boards.get(key, 0) + 1
        digest["boards"][label] = dict(sorted(boards.items(), key=lambda kv: kv[1], reverse=True))
        for r in hit:
            sid = str(r.get("secid"))
            hits_per_symbol.setdefault(sid, []).append(label)

    resonance = [
        {
            "代码": next(str(r.get("code")) for r in rows if str(r.get("secid")) == sid),
            "名称": next(str(r.get("name") or "") for r in rows if str(r.get("secid")) == sid),
            "板块": next(str(r.get("board") or "") for r in rows if str(r.get("secid")) == sid),
            "命中按钮": labels,
        }
        for sid, labels in hits_per_symbol.items()
        if len(labels) >= 2
    ]
    digest["共振个股"] = sorted(resonance, key=lambda x: len(x["命中按钮"]), reverse=True)[:20]
    pcts = [_num(r.get("pct_chg")) for r in rows if _num(r.get("pct_chg")) is not None]
    if pcts:
        digest["涨跌幅分布"] = {
            "上涨家数": sum(1 for p in pcts if p > 0),
            "下跌家数": sum(1 for p in pcts if p < 0),
            "中位数%": round(sorted(pcts)[len(pcts) // 2], 2),
            "涨停左右(>=9.5%)": sum(1 for p in pcts if p >= 9.5),
            "跌幅超5%": sum(1 for p in pcts if p <= -5),
        }
    return digest


def digest_text(digest: Dict[str, Any]) -> str:
    parts = [DIGEST_SCHEMA.format(n=15), json.dumps(digest, ensure_ascii=False, indent=1)]
    return "\n".join(parts)


def rule_summary(digest: Dict[str, Any]) -> str:
    """不调 AI 的兜底总结（纯数据描述）。"""
    lines = [f"# {digest.get('date','')} 收盘扫描总结（规则版）", ""]
    totals = digest.get("totals") or {}
    if not totals:
        return lines[0] + "\n\n当日缓存里没有数据。\n"
    lines.append("## 命中概览")
    for key, value in totals.items():
        lines.append(f"- {key}: {value}")
    if digest.get("涨跌幅分布"):
        lines.append("")
        lines.append("## 命中个股涨跌幅分布")
        for key, value in digest["涨跌幅分布"].items():
            lines.append(f"- {key}: {value}")
    for label, boards in (digest.get("boards") or {}).items():
        if not boards:
            continue
        top = "、".join(f"{k} {v}只" for k, v in list(boards.items())[:5])
        lines.append("")
        lines.append(f"## {label} 板块分布（前五）")
        lines.append(f"- {top}")
    if digest.get("共振个股"):
        lines.append("")
        lines.append("## 多信号共振（同时命中 ≥2 个按钮）")
        for item in digest["共振个股"][:10]:
            lines.append(
                f"- {item['代码']} {item['名称']}（{item['板块']}）：{' + '.join(item['命中按钮'])}"
            )
    for label, items in (digest.get("views") or {}).items():
        if not items:
            continue
        lines.append("")
        lines.append(f"## {label} 代表个股")
        for it in items[:10]:
            metric = [f"{k} {v}" for k, v in it.items() if k not in ("代码", "名称", "板块")]
            lines.append(f"- {it['代码']} {it['名称']}: " + "，".join(metric))
    lines.append("")
    lines.append(
        "> 规则版总结：仅统计本地扫描数据，未调用 AI。配置 `summary.api_key`（或环境变量 DEEPSEEK_API_KEY）后会自动改成 AI 复盘。"
    )
    return "\n".join(lines)


def _build_body(cfg, model: str, prompt: str, thinking: bool) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(cfg.get("summary.temperature", 0.3) or 0.3),
        "max_tokens": int(cfg.get("summary.max_tokens", 8000) or 8000),
        "stream": False,
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    if thinking:
        body["reasoning_effort"] = str(cfg.get("summary.reasoning_effort", "high") or "high")
    return body


def call_deepseek(cfg, prompt: str, api_key: str) -> str:
    """调用 DeepSeek（OpenAI 兼容的 /chat/completions）。

    复盘是"综合判断"类任务（跨六张名单找主线、排序反转信号质量），所以**默认开思考**
    （``summary.thinking: true``）并给足 ``max_tokens``（思考 token 也算在里面）。
    保险起见：思考模式若没返回正文（例如长度被吃满/结构异常），自动关掉思考重试一次。
    """
    base = str(cfg.get("summary.base_url", DEEPSEEK_BASE) or DEEPSEEK_BASE).rstrip("/")
    model = str(cfg.get("summary.model", DEEPSEEK_MODEL) or DEEPSEEK_MODEL)
    timeout = float(cfg.get("summary.timeout", 120) or 120)
    url = f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    thinking = _thinking(cfg)
    payload = _post_json(url, headers, _build_body(cfg, model, prompt, thinking), timeout)
    text = extract_text(payload)
    if not text and thinking:
        choice = (payload.get("choices") or [{}])[0]
        log.warning(
            "思考模式没返回正文（finish_reason=%s usage=%s），自动用 disabled 重试一次",
            choice.get("finish_reason"),
            payload.get("usage"),
        )
        payload = _post_json(url, headers, _build_body(cfg, model, prompt, False), timeout)
        text = extract_text(payload)
    if not text:
        choice = (payload.get("choices") or [{}])[0]
        log.warning(
            "DeepSeek 返回正文为空：finish_reason=%s usage=%s；原始响应前 400 字：%s",
            choice.get("finish_reason"),
            payload.get("usage"),
            json.dumps(payload, ensure_ascii=False)[:400],
        )
    return text


def _post_json(
    url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float
) -> Dict[str, Any]:
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def _thinking(cfg) -> bool:
    """默认开思考（复盘是综合判断类任务）；``summary.thinking: false`` 可关掉。"""
    value = cfg.get("summary.thinking", True)
    if value is None:
        return True
    return str(value).lower() in ("1", "true", "yes", "on")


def extract_text(payload: Any) -> str:
    """从常见的几种返回结构里取正文（不同版本/供应商字段名不一样）。

    * OpenAI 风格：``choices[0].message.content``（可能是字符串，也可能是分段列表）
    * 有的版本把思考过程放 ``reasoning_content``，正文在 ``content``；有的把正文放 ``text``/``output_text``
    """
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        for key in ("content", "text", "reasoning_content"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):  # 分段内容：[{"type":"text","text":"..."}]
                joined = "".join(
                    str(part.get("text") or "") if isinstance(part, dict) else str(part)
                    for part in value
                ).strip()
                if joined:
                    return joined
        for key in ("text", "content", "output_text"):
            value = choices[0].get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("output_text", "content", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def generate_summary(
    store: Store, cfg, trade_date: str, api_key: str = "", force: bool = False
) -> Dict[str, Any]:
    """生成（或复用）当日总结：有密钥走 AI，没有就出规则版。"""
    api_key = api_key or str(cfg.get("summary.api_key") or "")
    existing = store.get_summary(str(trade_date))
    if existing and not force:
        return existing
    digest = build_digest(store, cfg, str(trade_date))
    engine = "rule"
    content = rule_summary(digest)
    if api_key:
        try:
            prompt = PROMPT.format(date=trade_date) + "\n\n数据如下：\n" + digest_text(digest)
            ai_text = call_deepseek(cfg, prompt, api_key)
            model = str(cfg.get("summary.model", DEEPSEEK_MODEL) or DEEPSEEK_MODEL)
            if ai_text:
                content, engine = ai_text, model
            else:
                content = rule_summary(digest)
                engine = f"rule(无正文:{model})"
                log.warning("AI 没返回正文，本次先用规则版总结（上面有原始响应片段）")
        except Exception as exc:
            log.error("调用 DeepSeek 失败（本次先出规则版）：%s", exc)
    record = {
        "date": str(trade_date),
        "engine": engine,
        "content": content,
        "digest": json.dumps(digest, ensure_ascii=False),
    }
    store.save_summary(record)
    return record


def summary_after(cfg) -> str:
    """当天几点之后才生成总结（默认 17:00，避开 15:32 那次数据不全的运行）。"""
    return str(cfg.get("summary.only_after", "17:00") or "17:00")


def is_due(cfg) -> bool:
    return now_hm() >= summary_after(cfg)


def write_summary_files(record: Dict[str, Any], cfg) -> List[Path]:
    reports = cfg.path("store.reports_dir")
    reports.mkdir(parents=True, exist_ok=True)
    prefix = "summary" if record.get("engine") != "rule" else "summary_rule"
    path = reports / f"{prefix}_{record.get('date')}.md"
    path.write_text(str(record.get("content") or ""), encoding="utf-8")
    return [path]
