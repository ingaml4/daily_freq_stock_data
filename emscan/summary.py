"""当日复盘总结：把六个按钮的命中整理成结构化摘要，可选交给 DeepSeek 生成中文复盘。

流程（只在当天数据齐全后跑，默认 17:00 之后，见 config 的 summary.only_after）：

    build_digest()   从 daily_signals 缓存里抽出：各按钮命中数、板块分布、多信号共振个股、
                     每个按钮的**全部**命中个股与关键指标、整体统计、筛选口径
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
from .views import view_criteria, vol_up_mask

log = logging.getLogger("emscan")

DEEPSEEK_BASE = "https://api.deepseek.com"
# 当前官方模型名：deepseek-flash（快/便宜，复盘够用）、deepseek-v4-pro（更强，更慢更贵）
DEEPSEEK_MODEL = "deepseek-flash"
# 官方文档：max_tokens 取值 1 ~ 393216；不填时非思考 8K、思考 64K、effort=max 时 128K
MAX_TOKENS_CEILING = 393_216
DEFAULT_MAX_TOKENS = 32_000  # 思考 token 也算在里面，默认给足（想省一点可调小，填 0 = 用官方默认）
# 思考档位（官方：none / low / high / max，默认 high）；兼容老写法
EFFORTS = ("none", "low", "high", "max")
EFFORT_ALIASES = {"minimal": "low", "medium": "high", "xhigh": "high"}

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

PROMPT = """你是 A 股复盘分析师。下面是程序在 {date} 收盘后扫描出的结构化结果（来自本地行情库，
只包含信号命中的个股，**是全量、没有抽样**；每个按钮附带筛选口径和整体统计，请严格按口径理解数据，
不要自行脑补定义。命中多的时候不要只盯前几只，比例、分布、集中度都自己算。

请基于**这些数据**写一份中文复盘，Markdown 格式，**1500~3000 字**，宁长勿短、信息密度要高，
不要写"整体中性、注意风险"这类空话。按下面的结构写：

## 一、今日全景
命中总数、六个按钮各自的命中数（把它读成市场结构，而不是一个一个报数）、涨跌幅分布说明了什么
（普涨/普跌/分化、跌停与跌幅超 5% 的规模）、量能与位置的组合含义（是低位止跌、还是高位放量加速）。

## 二、板块与结构
按给到的板块口径（沪主板/深主板/创业板/科创板/北交所）比较各按钮命中的分布差异，指出集中在哪里、
意味着什么（例如超跌贴下轨更集中在双创，说明什么）。若数据里没有行业分类，明确写"行业维度数据未提供"，
**不要编造行业、题材、赛道归属**。

## 三、逐个按钮解读
每个按钮单独一小段（有命中的至少 200 字；当日 0 只命中的也要写一段，说明"0 只说明什么"）：
名单整体特征、量价形态、**点名 3~5 只个股并带上给定数据里的具体数字**（量比5、DIFF、J 值、
布林位置、箱体位置、反转分与证据等）、这批票适合短线博弈还是先观察、以及名单里的反例
（例如放量大涨中出现滞涨或冲高回落的样本）。

## 四、多信号共振
逐个点评同时命中 ≥2 个按钮的个股：命中了哪几个按钮、为什么这个组合更值得跟踪、结合数字给出强弱排序。

## 五、反转信号质量排序
把反转信号名单按"证据条数 + 位置高低"分成三档（值得优先跟踪 / 观察 / 证据不足），点名个股并写清依据。

## 六、风险提示与跟踪清单
本次数据的局限（未提供行业、资金流向、公告新闻；指标只基于日线收盘价）、容易误判的地方
（布林下轨数量多≠马上反转、KDJ 金叉在下跌趋势中容易钝化、放量大涨里滞涨样本的含义），
最后给出 **3~8 条可执行的跟踪清单**：看什么、什么条件算确认、什么条件算否证。

硬性要求：
- 只依据给定数据分析，**不要编造新闻、公告、事件、行业、资金流向**；数据没有的就写"数据未提供"。
- 所有数字必须与给定数据一致，引用个股时带上数字，不要只写"表现强势"。
- 允许对信号强弱做排序、取舍和结论性判断（这是复盘的价值所在），但要说清依据是哪些数字。
- 放量大涨 = 突然放量 **且** 当日上涨（见口径），名单里不该有放量下跌的票；若数据与口径不符，
  直接指出这个矛盾，不要替它圆场。""".strip()

DIGEST_SCHEMA_COMMON = """\
说明：""口径"" 是每个按钮的筛选条件（严格按口径理解数据，不要自行脑补定义）；
""views_stats"" 是同一批票的整体统计（中位数/区间/上涨比例/成交额合计）；
""共振个股""= 同时命中 ≥2 个按钮。"""


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 2)


def _view_stats(hit: List[Dict[str, Any]], metric_key: str, metric_label: str) -> Dict[str, Any]:
    """每个按钮的横向统计，让 AI 有"整体画像"可讲，而不是只能点名个股。"""
    pcts = [p for p in (_num(r.get("pct_chg")) for r in hit) if p is not None]
    turnovers = [t for t in (_num(r.get("turnover")) for r in hit) if t is not None]
    amounts = [a for a in (_num(r.get("amount")) for r in hit) if a is not None]
    metrics = [m for m in (_num(r.get(metric_key)) for r in hit) if m is not None]
    stats = {
        "只数": len(hit),
        "涨跌幅中位数%": _median(pcts),
        "涨跌幅区间%": [min(pcts), max(pcts)] if pcts else None,
        "上涨比例%": round(100.0 * sum(1 for p in pcts if p > 0) / len(pcts), 1) if pcts else None,
        "换手中位数%": _median(turnovers),
        "成交额合计亿": round(sum(amounts) / 1e8, 1) if amounts else None,
        metric_label + "中位数": _median(metrics),
    }
    return {k: v for k, v in stats.items() if v is not None}


def build_digest(store: Store, cfg, trade_date: str, top_n: Optional[int] = None) -> Dict[str, Any]:
    """从缓存里抽一份当日摘要（给 AI 或直接出兜底总结）。

    **默认不抽样**：每个按钮有多少命中就给 AI 多少（``summary.top_symbols: 0``）。
    上下文 1M、输入又便宜，漏掉个股比多给几千行更亏；想省 token 可以设成正数。
    """
    if top_n is None:
        top_n = int(cfg.get("summary.top_symbols", 0) or 0)
    n = int(top_n)
    frame = store.load_daily_signals(
        start=str(trade_date), end=str(trade_date), join_symbols=True, limit=20000
    )
    digest: Dict[str, Any] = {
        "date": str(trade_date),
        "口径": {label: view_criteria(cfg).get(flag[2:], "") for flag, label in VIEW_LABELS.items()},
        "totals": {},
        "boards": {},
        "views": {},
        "views_stats": {},
    }
    if frame is None or frame.empty:
        return digest

    rows = frame.to_dict("records")
    # 放量大涨 = 信号命中 + 当日涨幅 ≥ 门槛（和网页视图同一个口径，别再出现放量下跌的票）
    vol_up = vol_up_mask(frame, cfg).tolist()
    # 注意：这是当日**扫描**（缓存里有行）的股票数，不是命中数——命中数按按钮分别算
    digest["totals"]["扫描股票数"] = len(rows)
    hits_per_symbol: Dict[str, List[str]] = {}
    for flag, label in VIEW_LABELS.items():
        hit = [
            r
            for index, r in enumerate(rows)
            if _num(r.get(flag)) == 1 and (flag != "s_vol_spike" or vol_up[index])
        ]
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
        ranked = sorted(hit, key=lambda r: (boost(r), _num(r.get("amount")) or 0), reverse=True)
        if n > 0:  # n<=0：全部命中都给（默认）
            ranked = ranked[:n]
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
        digest["views_stats"][label] = _view_stats(hit, metric_key, metric_label)
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
    resonance = sorted(resonance, key=lambda x: len(x["命中按钮"]), reverse=True)
    digest["共振个股"] = resonance[:n] if n > 0 else resonance
    if bool(cfg.get("summary.include_universe", False)):
        digest["全市场简表"] = [
            {
                "代码": str(r.get("code") or ""),
                "名称": str(r.get("name") or ""),
                "板块": str(r.get("board") or ""),
                "涨跌幅%": round(_num(r.get("pct_chg")) or 0, 2),
                "成交额亿": round((_num(r.get("amount")) or 0) / 1e8, 2),
                "换手%": round(_num(r.get("turnover")) or 0, 2),
                "量比5": round(_num(r.get("vol_ratio_5")) or 0, 2),
                "命中": str(r.get("signals") or ""),
            }
            for r in rows
        ]
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


def digest_text(digest: Dict[str, Any], top_n: int = 0) -> str:
    """把摘要转成给 AI 的文本；``top_n > 0`` 时说明"只给了前 N 只"。"""
    if top_n > 0:
        head = f'说明：""views"" 里每个按钮只列出命中最多的前 {top_n} 只（summary.top_symbols 限流）。\n'
    else:
        head = '说明：""views"" 里每个按钮列出**全部**命中个股（不抽样）。\n'
    return head + DIGEST_SCHEMA_COMMON + "\n" + json.dumps(digest, ensure_ascii=False, indent=1)


def digest_for_store(digest: Dict[str, Any], keep: int = 20) -> Dict[str, Any]:
    """落库用的精简版摘要。

    AI 拿的是**全量**命中名单，但没必要把几千行 JSON 再存一份到数据库里
    （原始数据在 ``daily_signals``），所以库里只留统计口径 + 每个按钮少量代表个股。
    """
    lean: Dict[str, Any] = {}
    for key, value in digest.items():
        if key == "views":
            lean["views"] = {
                label: {"只数": len(items), "代表": items[:keep]}
                for label, items in value.items()
            }
        elif key == "全市场简表":
            lean["全市场简表"] = {"只数": len(value), "说明": "完整名单见 daily_signals 表"}
        else:
            lean[key] = value
    return lean


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
    if digest.get("views_stats"):
        lines.append("")
        lines.append("## 各按钮整体画像")
        for label, stats in digest["views_stats"].items():
            detail = "，".join(f"{k} {v}" for k, v in stats.items())
            lines.append(f"- {label}：{detail}")
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


def _build_body(
    cfg, model: str, prompt: str, thinking: bool, max_tokens: Optional[int] = None
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # 思考模式下 temperature 不生效（官方文档），只有关掉思考时才起作用
        "temperature": float(cfg.get("summary.temperature", 0.3) or 0.3),
        "stream": False,
        "thinking": {"type": "enabled" if thinking else "disabled"},
        # 关思考要配 effort=none；开思考才给档位（不给的话官方默认 high）
        "reasoning_effort": _effort(cfg) if thinking else "none",
    }
    limit = _max_tokens(cfg) if max_tokens is None else max_tokens
    if limit:  # 0/None = 不传，交给官方默认值（思考 64K / 非思考 8K）
        body["max_tokens"] = limit
    return body


def call_deepseek(cfg, prompt: str, api_key: str) -> str:
    """调用 DeepSeek（OpenAI 兼容的 /chat/completions）。

    复盘是"综合判断"类任务（跨六张名单找主线、判断反转信号质量、分辨消息面共振），所以
    **默认开思考**（``summary.thinking: true`` + ``reasoning_effort: high``）。

    坑：思考 token 也算进 ``max_tokens``，预算被思考吃满时正文要么是空的、要么是半截。
    于是这里有两道保险：
    * 参数不被接口接受（400）→ 去掉 max_tokens / reasoning_effort 再试一次；
    * 思考模式返回空正文或被截断 → 用非思考模式重试一次（保留能用的那一份）。
    """
    base = str(cfg.get("summary.base_url", DEEPSEEK_BASE) or DEEPSEEK_BASE).rstrip("/")
    model = str(cfg.get("summary.model", DEEPSEEK_MODEL) or DEEPSEEK_MODEL)
    timeout = float(cfg.get("summary.timeout", 120) or 120)
    url = f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    thinking = _thinking(cfg)
    payload = _post_json_safe(url, headers, cfg, model, prompt, thinking, timeout)
    text = extract_text(payload)
    if thinking and (not text or _truncated(payload)):
        log.warning(
            "思考模式返回不完整（finish_reason=%s，思考 %d 字，正文 %d 字，usage=%s），"
            "自动改用非思考模式重试一次（想根治就把 summary.max_tokens 调大、或 effort 调到 max）",
            _finish_reason(payload) or "?",
            len(extract_reasoning(payload)),
            len(text),
            payload.get("usage"),
        )
        retry = _post_json_safe(url, headers, cfg, model, prompt, False, timeout)
        retry_text = extract_text(retry)
        if retry_text and (not text or not _truncated(retry)):
            payload, text = retry, retry_text
    if not text:
        log.warning(
            "DeepSeek 返回正文为空：finish_reason=%s usage=%s；原始响应前 400 字：%s",
            _finish_reason(payload) or "?",
            payload.get("usage"),
            json.dumps(payload, ensure_ascii=False)[:400],
        )
    elif _truncated(payload):
        log.warning(
            "DeepSeek 正文被 max_tokens 截断（finish_reason=length，%d 字），内容可能不完整："
            "把 summary.max_tokens 调大（上限 393216）或把 summary.reasoning_effort 调低",
            len(text),
        )
    else:
        log.info(
            "DeepSeek 复盘完成：正文 %d 字，思考 %d 字，finish_reason=%s，usage=%s",
            len(text),
            len(extract_reasoning(payload)),
            _finish_reason(payload) or "-",
            payload.get("usage"),
        )
    return text


def _post_json_safe(
    url: str,
    headers: Dict[str, str],
    cfg,
    model: str,
    prompt: str,
    thinking: bool,
    timeout: float,
) -> Dict[str, Any]:
    """发请求；接口若嫌 max_tokens / reasoning_effort 不合法（400），去掉这两个参数再试一次。"""
    body = _build_body(cfg, model, prompt, thinking)
    try:
        return _post_json(url, headers, body, timeout)
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        detail = (response.text if response is not None else "")[:300]
        rejected = "max_tokens" in detail or "reasoning_effort" in detail
        if response is None or response.status_code != 400 or not rejected:
            raise
        log.warning("接口不认 max_tokens/reasoning_effort（%s），去掉这两个参数再试一次", detail)
        trimmed = {k: v for k, v in body.items() if k not in ("max_tokens", "reasoning_effort")}
        return _post_json(url, headers, trimmed, timeout)


def _post_json(
    url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float
) -> Dict[str, Any]:
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def _choice(payload: Any) -> Dict[str, Any]:
    choices = payload.get("choices") if isinstance(payload, dict) else None
    first = (choices or [None])[0]
    return first if isinstance(first, dict) else {}


def _finish_reason(payload: Any) -> str:
    return str(_choice(payload).get("finish_reason") or "")


def _truncated(payload: Any) -> bool:
    """finish_reason=length：生成被 max_tokens 或上下文长度截断，正文可能只是半截。"""
    return _finish_reason(payload) == "length"


def extract_reasoning(payload: Any) -> str:
    """思考模式返回的思维链（``reasoning_content``），只用于日志诊断，绝不当复盘正文。"""
    message = _choice(payload).get("message")
    value = message.get("reasoning_content") if isinstance(message, dict) else None
    return value.strip() if isinstance(value, str) else ""


def _thinking(cfg) -> bool:
    """默认开思考（复盘是综合判断类任务）；``summary.thinking: false`` 或 effort=none 关掉。"""
    if _effort(cfg) == "none":
        return False
    value = cfg.get("summary.thinking", True)
    if value is None:
        return True
    return str(value).lower() in ("1", "true", "yes", "on")


def _effort(cfg) -> str:
    """思考档位：none / low / high / max（官方默认 high）；写法不认识就回落到 high。"""
    raw = str(cfg.get("summary.reasoning_effort", "high") or "high").strip().lower()
    value = EFFORT_ALIASES.get(raw, raw)
    return value if value in EFFORTS else "high"


def _max_tokens(cfg) -> Optional[int]:
    """生成上限：1 ~ 393216，超了按上限夹住；0/负数/空 = 不传（用官方默认）。"""
    raw = cfg.get("summary.max_tokens", DEFAULT_MAX_TOKENS)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_MAX_TOKENS
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        log.warning("summary.max_tokens=%r 不是数字，改用默认 %d", raw, DEFAULT_MAX_TOKENS)
        return DEFAULT_MAX_TOKENS
    if value <= 0:
        return None
    if value > MAX_TOKENS_CEILING:
        log.warning("summary.max_tokens=%d 超过官方上限，按 %d 处理", value, MAX_TOKENS_CEILING)
        return MAX_TOKENS_CEILING
    return value


def mode_text(cfg) -> str:
    """给 check / 日志用的一行描述：思考开关 + 档位 + 生成上限。"""
    limit = _max_tokens(cfg)
    budget = f"max_tokens {limit}" if limit else "max_tokens 用官方默认"
    if not _thinking(cfg):
        return f"非思考模式（{budget}）"
    return f"思考模式 reasoning_effort={_effort(cfg)}（{budget}）"


def extract_text(payload: Any) -> str:
    """从常见的几种返回结构里取正文（不同版本/供应商字段名不一样）。

    * OpenAI 风格：``choices[0].message.content``（可能是字符串，也可能是分段列表）
    * 有的版本把正文放 ``text``/``output_text``
    注意：思考模式的思维链在 ``message.reasoning_content``，那是过程不是结论，
    这里**不算正文**（否则会把半截思维链当成复盘发出去）；需要时用 :func:`extract_reasoning`。
    """
    if not isinstance(payload, dict):
        return ""
    choice = _choice(payload)
    if choice:
        message = choice.get("message") or {}
        for key in ("content", "text"):
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
            value = choice.get(key)
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
            top_n = int(cfg.get("summary.top_symbols", 0) or 0)
            prompt = (
                PROMPT.format(date=trade_date)
                + "\n\n数据如下：\n"
                + digest_text(digest, top_n)
            )
            ai_text = call_deepseek(cfg, prompt, api_key)
            model = str(cfg.get("summary.model", DEEPSEEK_MODEL) or DEEPSEEK_MODEL)
            if ai_text:
                # 引擎名带上思考档位：网页"当日总结"里一眼能看出这份复盘是不是开了思考
                content = ai_text
                engine = f"{model}(thinking={_effort(cfg)})" if _thinking(cfg) else model
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
        "digest": json.dumps(digest_for_store(digest), ensure_ascii=False),
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
