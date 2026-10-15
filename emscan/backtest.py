"""梯队跟踪回测：把复盘里的"第一/第二梯队"名单变成可以算账的东西。

整条链路：

    T 日 17:15 复盘（AI）          -> 文末固定格式的 TIER1/TIER2 两行
    parse_tiers()                  -> (代码, 名称) -> 本地 secid 校验（对不上就丢弃并记账）
    save_tier_picks()              -> tier_picks 表（一天一行一只票，不覆盖手工添加的）
    evaluate()                     -> 每只票的 1/5/10/20 交易日收益 + 期间最大涨幅/回撤 + 超额

口径（页面上也会写明，免得过后自己都记不清）：

* **入场**：``backtest.entry``（默认 ``next_open``）= T+1 **开盘价**买入 —— 复盘是收盘后生成的，
  当天不可能买进去；``close`` 则是 T 日收盘价买入（更乐观，用来对比）。
* **N 日收益**：从入场日算起持有 N 个交易日（``next_open`` 时 N=1 就是入场当天收盘），
  卖出价取收盘。交易日以本地 ``trading_days`` 为准（自然日会被节假日拉歪）。
* **超额**：同一时间窗里全市场等权收益（同样的入场价口径）→ 个股收益减基准。
  没有超额就无法回答"这套名单是不是还不如随便买"。
* **跟踪中**：T+N 还没到，收益留空，页面显示"跟踪中"，不会拿没发生的数据充数。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from .store import Store

log = logging.getLogger("emscan")

TIER_LABELS: Dict[int, str] = {1: "第一梯队", 2: "第二梯队"}
DEFAULT_HORIZONS: Tuple[int, ...] = (1, 5, 10, 20)
DEFAULT_ENTRY = "next_open"

# 文末的机器可读行：TIER1: 301591 肯特股份 | 603344 星德胜
TIER_LINE_RE = re.compile(
    r"^[\s>*#-]*(?:TIER|T)\s*([12１２])\s*[:：]|^[\s>*#-]*第\s*([一二])\s*梯队\s*[:：]",
    re.I | re.M,
)
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
# 老格式（没有 TIER 行）的兜底：从"第一梯队为…；第二梯队为…"这类句子里捞名字
PROSE_TIER_RE = re.compile(
    r"第\s*([一二三])\s*(?:梯队|档)[^\n。；;]{0,12}?[为是:：、，,]?\s*([^\n。；;]+)"
)
_CN_NUM = {"一": 1, "二": 2, "三": 3}
_SPLIT_RE = re.compile(r"[|｜、,，;；/]+")


def tier_limits(cfg) -> Dict[int, int]:
    """名单规模上限（喂给 AI 的提示词与解析校验共用）。"""
    return {
        1: int(cfg.get("tiers.max_tier1", 8) or 8),
        2: int(cfg.get("tiers.max_tier2", 15) or 15),
    }


def target_horizons(cfg) -> List[int]:
    raw = cfg.get("backtest.horizons", None) or list(DEFAULT_HORIZONS)
    if isinstance(raw, str):
        raw = raw.replace("，", ",").split(",")
    out: List[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in out:
            out.append(value)
    return sorted(out) or list(DEFAULT_HORIZONS)


def entry_mode(cfg) -> str:
    value = str(cfg.get("backtest.entry", DEFAULT_ENTRY) or DEFAULT_ENTRY).strip().lower()
    return "close" if value in ("close", "close_t", "t_close") else "next_open"


def _normalize_name(value: str) -> str:
    text = str(value or "").upper()
    for ch in (" ", "\u3000", "*", "＊", "(", ")", "（", "）", "-", "－"):
        text = text.replace(ch, "")
    return text


def build_symbol_index(store: Store) -> Dict[str, Dict[str, Tuple[str, str, str]]]:
    """本地股票索引：按代码、按名称（名称归一化，ST/空格/全角都能对上）。"""
    frame = store.symbols_df()
    by_code: Dict[str, Tuple[str, str, str]] = {}
    by_name: Dict[str, Tuple[str, str, str]] = {}
    if frame is None or frame.empty:
        return {"code": by_code, "name": by_name}
    for row in frame.to_dict("records"):
        secid = str(row.get("secid") or "")
        code = str(row.get("code") or "")
        name = str(row.get("name") or "")
        if not secid or not code:
            continue
        by_code[code] = (secid, code, name)
        key = _normalize_name(name)
        if key and key not in by_name:
            by_name[key] = (secid, code, name)
    return {"code": by_code, "name": by_name}


def _split_items(text: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for chunk in _SPLIT_RE.split(str(text or "")):
        chunk = chunk.strip()
        if not chunk or chunk in ("（空）", "(空)", "无", "暂无", "空"):
            continue
        code_match = CODE_RE.search(chunk)
        code = code_match.group(1) if code_match else ""
        name = chunk.replace(code, " ", 1) if code else chunk
        reason = ""
        bracket = re.search(r"[（(]([^）)]*)[）)]", name)
        if bracket:
            reason = bracket.group(1).strip()
            name = name[: bracket.start()]
        name = re.sub(r"^[\s:：\-—*#]+", "", name).strip()
        if not code and not name:
            continue
        items.append({"code": code, "name": name, "reason": reason})
    return items


def parse_tier_lines(content: str) -> Dict[int, List[Dict[str, str]]]:
    """解析文末的 ``TIER1: 301591 肯特股份 | …`` 行（首选格式，最稳）。"""
    out: Dict[int, List[Dict[str, str]]] = {1: [], 2: []}
    current: Optional[int] = None
    for line in str(content or "").splitlines():
        match = TIER_LINE_RE.match(line)
        if match:
            digits = match.group(1)
            current = _CN_NUM.get(digits, None) or (int(digits) if digits else None)
            if current is None:
                current = _CN_NUM.get(match.group(2) or "", None)
            rest = line[match.end() :].strip()
            if current in out:
                out[current].extend(_split_items(rest))
            continue
        if current in out and line.strip() and not line.lstrip().startswith("#"):
            out[current].extend(_split_items(line))  # 换行续写
    return out


def parse_tiers_from_prose(content: str) -> Dict[int, List[Dict[str, str]]]:
    """兜底：老格式（没有 TIER 行）时，从"第一梯队为…"这类句子里捞股票名称。"""
    out: Dict[int, List[Dict[str, str]]] = {1: [], 2: []}
    seen: Dict[int, set] = {1: set(), 2: set()}
    for match in PROSE_TIER_RE.finditer(str(content or "")):
        tier = _CN_NUM.get(match.group(1), 0)
        if tier not in out:
            continue
        for chunk in _SPLIT_RE.split(match.group(2)):
            for entry in _split_items(chunk):
                key = entry["code"] or entry["name"]
                if not key or key in seen[tier]:
                    continue
                seen[tier].add(key)
                out[tier].append(entry)
    return out


def parse_tiers(content: str, symbols: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从复盘正文里取出两档名单，并校验成本地存在的股票。

    对不上的（AI 编的代码、名字写错、已退市）**不静默丢掉**，记在 ``unmatched`` 里，
    回头据此改提示词。
    """
    lines = parse_tier_lines(content)
    format_name = "tier-line"
    if not lines[1] and not lines[2]:
        lines = parse_tiers_from_prose(content)
        format_name = "prose"
    index = symbols or {"code": {}, "name": {}}
    by_code = index.get("code") or {}
    by_name = index.get("name") or {}
    picks: Dict[int, List[Dict[str, Any]]] = {1: [], 2: []}
    unmatched: List[Dict[str, Any]] = []
    noise = 0  # 老格式兜底时被过滤掉的"说明文字"条数（不值得报警，只记数量）
    seen: set = set()  # 同一只票只算一档（第一梯队优先）
    prose = format_name == "prose"
    for tier, items in lines.items():
        for item in items:
            resolved = by_code.get(str(item["code"])) if item.get("code") else None
            if resolved is None and item.get("name"):
                resolved = by_name.get(_normalize_name(item["name"]))
            if resolved is None:
                # 老格式（prose）是"尽力而为"：说明文字会被一起捞出来，不计入报警
                if prose:
                    if item.get("code") or item.get("name"):
                        noise += 1
                elif item.get("code") or item.get("name"):
                    unmatched.append({"tier": tier, **item})
                continue
            secid, code, name = resolved
            if secid in seen:
                continue
            seen.add(secid)
            picks[tier].append(
                {
                    "secid": secid,
                    "code": code,
                    "name": name,
                    "tier": tier,
                    "reason": str(item.get("reason") or "").strip(),
                }
            )
    return {"picks": picks, "unmatched": unmatched, "noise": noise, "format": format_name}


# ---------------------------------------------------------------- 落库 / 同步
def sync_tier_picks(
    store: Store, cfg, force: bool = False, dates: Optional[Sequence[str]] = None
) -> Dict[str, Any]:
    """把复盘正文里的梯队名单解析进 ``tier_picks``（默认只补还没解析过的交易日）。"""
    index = build_symbol_index(store)
    if dates:
        targets = [str(d) for d in dates]
    else:
        targets = [
            str(row["date"])
            for row in store.conn.execute("SELECT date FROM daily_summary ORDER BY date ASC")
        ]
    already = set(store.tier_dates())
    stats: Dict[str, Any] = {"dates": 0, "tier1": 0, "tier2": 0, "unmatched": [], "skipped": 0}
    for date in targets:
        if date in already and not force:
            stats["skipped"] += 1
            continue
        record = store.get_summary(date)
        content = str((record or {}).get("content") or "").strip()
        if not content:
            stats["skipped"] += 1
            continue
        parsed = parse_tiers(content, index)
        if parsed.get("noise"):
            log.debug("%s 的老格式复盘中忽略了 %s 条说明文字", date, parsed["noise"])
        rows: List[Dict[str, Any]] = []
        for tier, items in parsed["picks"].items():
            for item in items:
                rows.append({**item, "date": date, "source": "summary"})
        if not rows:
            stats["skipped"] += 1
            continue
        store.save_tier_picks(rows)
        stats["dates"] += 1
        stats["tier1"] += len(parsed["picks"][1])
        stats["tier2"] += len(parsed["picks"][2])
        for item in parsed["unmatched"]:
            stats["unmatched"].append({"date": date, **item})
    if stats["unmatched"]:
        log.warning(
            "梯队名单里有 %s 只对不上本地股票（AI 写错代码/名称，或已退市）：%s",
            len(stats["unmatched"]),
            "、".join(
                f"{it.get('code') or it.get('name')}(第{it.get('tier')}梯队)"
                for it in stats["unmatched"][:6]
            ),
        )
    store.set_meta("tier_unmatched", json.dumps(stats["unmatched"][-30:], ensure_ascii=False))
    return stats


# -------------------------------------------------------------------- 回测计算
def _price_map(
    frame: pd.DataFrame, secids: Sequence[str]
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """``{secid: {date: {open/high/low/close: 价格}}}``。"""
    out: Dict[str, Dict[str, Dict[str, float]]] = {}
    if frame is None or frame.empty:
        return out
    wanted = set(str(s) for s in secids)
    for row in frame.to_dict("records"):
        secid = str(row.get("secid"))
        if secid not in wanted:
            continue
        out.setdefault(secid, {})[str(row.get("date"))] = {
            key: (None if pd.isna(row.get(key)) else float(row[key]))
            for key in ("open", "high", "low", "close")
        }
    return out


def benchmark_returns(
    store: Store, pairs: Iterable[Tuple[str, str]], entry: str
) -> Dict[Tuple[str, str], float]:
    """全市场等权基准：``(入场日, 卖出日) -> 平均收益``（同样的入场价口径）。"""
    base = "open" if entry == "next_open" else "close"
    out: Dict[Tuple[str, str], float] = {}
    sql = (
        f"SELECT AVG(b.close / a.{base} - 1.0) AS ret FROM bars a "
        f"JOIN bars b ON b.secid = a.secid AND b.date = ? "
        f"WHERE a.date = ? AND a.{base} > 0 AND b.close > 0"
    )
    for start, end in sorted(set(pairs)):
        if not start or not end:
            continue
        row = store.conn.execute(sql, (str(end), str(start))).fetchone()
        if row is not None and row["ret"] is not None:
            out[(str(start), str(end))] = float(row["ret"])
    return out


def evaluate(store: Store, cfg, picks: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """算每只票的 1/5/10/20 交易日收益、期间最大涨幅/回撤、超额，并按梯队/日期汇总。"""
    horizons = target_horizons(cfg)
    entry = entry_mode(cfg)
    frame = store.load_tier_picks() if picks is None else picks
    empty: Dict[str, Any] = {
        "rows": [],
        "by_tier": {},
        "by_date": [],
        "entry": entry,
        "horizons": horizons,
    }
    if frame is None or frame.empty:
        return empty
    days = store.all_trading_days()
    if not days:
        return empty
    day_index = {date: index for index, date in enumerate(days)}
    from .industry import display_map

    _, industry = display_map(store, cfg)
    secids = [str(s) for s in frame["secid"].tolist()]
    bars = store.load_bars(
        secids=secids, columns=["secid", "date", "open", "high", "low", "close"]
    )
    prices = _price_map(bars, secids)

    rows: List[Dict[str, Any]] = []
    pairs: List[Tuple[str, str]] = []
    max_horizon = max(horizons)
    for item in frame.to_dict("records"):
        secid = str(item["secid"])
        date = str(item["date"])
        tier = int(item.get("tier") or 1)
        series = prices.get(secid, {})
        index = day_index.get(date)
        row: Dict[str, Any] = {
            "date": date,
            "tier": tier,
            "tier_label": TIER_LABELS.get(tier, f"第{tier}梯队"),
            "secid": secid,
            "code": str(item.get("code") or secid.split(".")[-1]),
            "name": str(item.get("name") or ""),
            "industry": industry.get(secid, ""),
            "reason": str(item.get("reason") or ""),
            "source": str(item.get("source") or ""),
            "close_t": (series.get(date) or {}).get("close"),
            "entry_date": None,
            "entry": None,
            "rets": {},
            "bench": {},
            "excess": {},
            "max_gain": None,
            "max_drawdown": None,
            "status": "跟踪中",
        }
        if index is None:
            row["status"] = "缺行情"  # 本地没有这根K线（退市/长期停牌）
            rows.append(row)
            continue
        entry_index = index + 1 if entry == "next_open" else index
        if entry_index >= len(days):
            row["status"] = "待入场"  # 名单刚生成，T+1 还没到
            rows.append(row)
            continue
        entry_date = days[entry_index]
        entry_price = (series.get(entry_date) or {}).get("open" if entry == "next_open" else "close")
        row["entry_date"] = entry_date
        row["entry"] = entry_price
        if not entry_price:
            row["status"] = "待入场"
            rows.append(row)
            continue

        window = days[entry_index : min(entry_index + max_horizon, len(days))]
        highs = [p for p in ((series.get(d) or {}).get("high") for d in window) if p]
        lows = [p for p in ((series.get(d) or {}).get("low") for d in window) if p]
        if highs:
            row["max_gain"] = round(max(highs) / entry_price - 1.0, 4)
        if lows:
            row["max_drawdown"] = round(min(lows) / entry_price - 1.0, 4)

        finished = 0
        for horizon in horizons:
            exit_index = entry_index + (horizon - 1 if entry == "next_open" else horizon)
            if exit_index >= len(days):
                continue
            exit_date = days[exit_index]
            exit_close = (series.get(exit_date) or {}).get("close")
            if not exit_close:
                continue
            row["rets"][horizon] = round(exit_close / entry_price - 1.0, 4)
            pairs.append((entry_date, exit_date))
            finished += 1
        row["status"] = "已完成" if finished == len(horizons) else "跟踪中"
        rows.append(row)

    bench = benchmark_returns(store, pairs, entry)
    for row in rows:
        if row["entry_date"] is None or row["entry"] is None:
            continue
        entry_index = day_index.get(str(row["entry_date"]), 0)
        for horizon, ret in row["rets"].items():
            exit_index = entry_index + (horizon - 1 if entry == "next_open" else horizon)
            if exit_index >= len(days):
                continue
            base = bench.get((str(row["entry_date"]), days[exit_index]))
            if base is None:
                continue
            row["bench"][horizon] = round(base, 4)
            row["excess"][horizon] = round(ret - base, 4)

    return {
        "rows": rows,
        "by_tier": _aggregate_by_tier(rows, horizons),
        "by_date": _aggregate_by_date(rows, horizons),
        "entry": entry,
        "horizons": horizons,
    }


def _stats(values: Sequence[float]) -> Dict[str, Any]:
    """一组收益的样本数/均值/中位/胜率/最好/最差。"""
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = ordered[middle]
    if len(ordered) % 2 == 0:
        median = (ordered[middle - 1] + ordered[middle]) / 2
    return {
        "n": len(ordered),
        "avg": round(sum(ordered) / len(ordered), 4),
        "median": round(median, 4),
        "win_rate": round(sum(1 for v in ordered if v > 0) / len(ordered), 4),
        "best": round(ordered[-1], 4),
        "worst": round(ordered[0], 4),
    }


def _aggregate_by_tier(rows: List[Dict[str, Any]], horizons: Sequence[int]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for tier in sorted({int(r["tier"]) for r in rows}):
        subset = [r for r in rows if int(r["tier"]) == tier]
        entry: Dict[str, Any] = {
            "tier": tier,
            "label": TIER_LABELS.get(tier, f"第{tier}梯队"),
            "picks": len(subset),
            "finished": sum(1 for r in subset if r["status"] == "已完成"),
        }
        for horizon in horizons:
            stats = _stats([r["rets"][horizon] for r in subset if r["rets"].get(horizon) is not None])
            excess = [r["excess"][horizon] for r in subset if r["excess"].get(horizon) is not None]
            if excess:
                stats["avg_excess"] = round(sum(excess) / len(excess), 4)
                stats["win_rate_vs_bench"] = round(sum(1 for v in excess if v > 0) / len(excess), 4)
            entry[str(horizon)] = stats
        draws = [r["max_drawdown"] for r in subset if r["max_drawdown"] is not None]
        gains = [r["max_gain"] for r in subset if r["max_gain"] is not None]
        if draws:
            entry["avg_max_drawdown"] = round(sum(draws) / len(draws), 4)
        if gains:
            entry["avg_max_gain"] = round(sum(gains) / len(gains), 4)
        out[str(tier)] = entry
    return out


def _aggregate_by_date(rows: List[Dict[str, Any]], horizons: Sequence[int]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for date in sorted({str(r["date"]) for r in rows}, reverse=True):
        for tier in sorted({int(r["tier"]) for r in rows if str(r["date"]) == date}):
            subset = [r for r in rows if str(r["date"]) == date and int(r["tier"]) == tier]
            item: Dict[str, Any] = {
                "date": date,
                "tier": tier,
                "tier_label": TIER_LABELS.get(tier, f"第{tier}梯队"),
                "picks": len(subset),
                "avg": {},
                "excess": {},
                "status": "跟踪中" if any(r["status"] != "已完成" for r in subset) else "已完成",
            }
            for horizon in horizons:
                rets = [r["rets"][horizon] for r in subset if r["rets"].get(horizon) is not None]
                if rets:
                    item["avg"][str(horizon)] = round(sum(rets) / len(rets), 4)
                excess = [r["excess"][horizon] for r in subset if r["excess"].get(horizon) is not None]
                if excess:
                    item["excess"][str(horizon)] = round(sum(excess) / len(excess), 4)
            out.append(item)
    return out


def report(store: Store, cfg, days: Optional[int] = None) -> Dict[str, Any]:
    """页面/命令行的入口：算好回测结果（``days`` 只看最近 N 个名单日）。"""
    picks = store.load_tier_picks()
    if days and not picks.empty:
        keep = set(store.tier_dates(limit=int(days)))
        picks = picks[picks["date"].astype(str).isin(keep)]
    result = evaluate(store, cfg, picks)
    result["unmatched"] = unmatched_picks(store)
    return result


def unmatched_picks(store: Store) -> List[Dict[str, Any]]:
    """最近一次同步里对不上的股票（页面据此提示"AI 写的代码有问题"）。"""
    raw = store.get_meta("tier_unmatched")
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []
