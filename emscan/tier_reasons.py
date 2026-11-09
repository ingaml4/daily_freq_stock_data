"""按原始复盘版本保存逐股入池依据，提供只读批量查询和离线补索引。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import date as Date

from .backtest import CODE_RE, TIER_LINE_RE
from .config import Config
from .store import Store
from .watchlist_risk import MAX_SYMBOLS, InvalidRequest, _symbol

FIELDS = ("strategy_type", "reason", "confirmation", "invalidation", "indicator_asof")


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _version(engine, content):
    return _hash(engine + "\0" + content)


def archive_source(conn, record):
    """由调用者控制事务。相同版本重跑保留原始发布时间。"""
    engine = str(record.get("engine") or "")
    content = str(record.get("content") or "")
    if not engine or engine == "pending" or engine.startswith("rule") or not content.strip():
        return None
    version = _version(engine, content)
    conn.execute(
        "INSERT INTO tier_reason_sources(date,version,content_sha256,engine,first_published_at,content) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(date,version) DO NOTHING",
        (record["date"], version, _hash(content), engine, record.get("created_at"), content),
    )
    return version


def _text(value):
    if not isinstance(value, str) or value.strip().lower() in {
        "",
        "无",
        "不详",
        "未知",
        "未提供",
        "暂无",
        "null",
        "n/a",
    }:
        return None
    return value.strip()


def _reason(fields, quote, line, origin, day):
    out = {key: _text(fields.get(key)) for key in FIELDS}
    asof = out["indicator_asof"]
    if asof:
        try:
            if Date.fromisoformat(asof).isoformat() != asof or asof > day:
                raise ValueError()
        except ValueError:
            out["indicator_asof"] = None
    return dict(
        out, quote=quote, line=line, origin=origin, gaps=[key for key in FIELDS if not out[key]]
    )


def _structured(line):
    stripped = line.strip()
    if not stripped.startswith("REASON_JSON "):
        return None
    try:
        item = json.loads(stripped[len("REASON_JSON ") :])
    except ValueError:
        return None
    if (
        not isinstance(item, dict)
        or type(item.get("tier")) is not int
        or item["tier"] not in (1, 2)
    ):
        return None
    secid, error = _symbol(str(item.get("symbol") or ""))
    if not error and secid:
        return (secid, item["tier"]), item
    return None


def _symbol_index(conn):
    by_code = defaultdict(list)
    by_name = defaultdict(list)
    for secid, code, name in conn.execute("SELECT secid,code,name FROM symbols"):
        by_code[code].append((secid, name or ""))
        if name:
            by_name[name].append(secid)
    return by_code, by_name


def _roster(content, catalog, picked):
    """文末明确的名单双档各自解析；同名/代码冲突且无法消歧时跳过。"""
    by_code, by_name = catalog
    members = {
        (r["secid"], r["tier"]): [r["name"] or "", "published_tier_pick", []] for r in picked
    }
    for index, line in enumerate(content.splitlines(), 1):
        match = TIER_LINE_RE.match(line)
        if match:
            n = match.group(1) or match.group(2)
            tier = {"一": 1, "二": 2}.get(n) if n in ("一", "二") else int(n)
            rest = line[match.end() :]
        else:
            # 仅接纳显式梯队枚举，不从评论中的普通「关注 XXX」反推成员。
            match = re.search(r"第([一二])梯队(?:为|是)\s*([^。；;\n]+)", line)
            if not match:
                continue
            tier = 1 if match[1] == "一" else 2
            rest = match[2]
        for entry in re.split(r"[|｜、]", rest):
            entry = entry.strip().strip("。；;")
            if not entry:
                continue
            codes = CODE_RE.findall(entry)
            if len(codes) > 1:
                continue
            head = re.split(r"[（(]", entry, maxsplit=1)[0].strip()
            head = head.replace("**", "").strip()
            if codes:
                code = codes[0]
                name = head.replace(code, "", 1).strip()
                candidates = by_code.get(code, ())
                if name:
                    candidates = [(sid, n) for sid, n in candidates if name == n]
                if len(candidates) != 1:
                    continue
                secid, name = candidates[0]
            else:
                ids = by_name.get(head, ())
                if len(ids) != 1:
                    continue
                secid, name = ids[0], head
            member = members.setdefault((secid, tier), [name, "original_roster", []])
            marker = re.fullmatch(r".*?[（(]([^()（）]+)[）)]", entry)
            if marker and marker[1].strip() not in (codes or []) + [name]:
                member[2].append((index, entry, marker[1].strip()))
    return members


def _legacy_quote(line, secid, name, unique_name):
    """只提取有单一主语和显式分隔的逐股陈述，不把多人同段或市场概述分配给个股。"""
    code = secid.split(".", 1)[1]
    if not name or (name not in line and code not in line):
        return None
    text = line.strip().strip("*").lstrip("-•> ").strip().replace("**", "")
    parts = re.split(r"[:：]", text, maxsplit=1)
    if len(parts) != 2:
        return None
    title, detail = parts
    title = re.sub(r"^\d{1,2}[.)、]\s*", "", title).strip()
    compact = re.sub(r"[\s()（）]", "", title)
    identities = {code, code + name, name + code}
    if unique_name:
        identities.add(name)
    if compact not in identities or not detail.strip():
        return None
    if any(found != code for found in CODE_RE.findall(detail)):
        return None
    return detail.strip()


def _passages(lines, catalog, members, day):
    """将多股段落拆到句/分号；只有一个可核实的证券主语才关联引用。"""
    by_code, by_name = catalog
    body = "\n".join(lines)
    names = sorted(
        (name for name in by_name if len(name) >= 2 and name in body), key=len, reverse=True
    )
    matcher = re.compile("|".join(map(re.escape, names))) if names else None
    picks = {secid for secid, _ in members}
    found = defaultdict(list)
    heading = heading_line = None
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            heading, heading_line = line, number
            continue
        if TIER_LINE_RE.match(line) or line.strip().startswith("REASON_JSON "):
            continue
        ranked = next(
            (t for t in ("多信号共振", "反转信号质量排序") if heading and t in heading), None
        )
        # 括号内常有分号，必须保留其前面的证券名称以及两边的证据。
        pieces = re.split(r"[。]|[；;](?![^（()）]*[）)])|(?<=[）)])、", line)
        for quote in pieces:
            quote = quote.strip()
            if not quote:
                continue
            ids = set()
            codes = CODE_RE.findall(quote)
            for code in codes:
                ids.update(sid for sid, _ in by_code.get(code, ()))
            for name in matcher.findall(quote) if matcher else ():
                ids.update(by_name[name])
            if len(ids) != 1 or not picks.intersection(ids):
                continue
            secid = next(iter(ids))
            if any(code != secid.split(".")[1] for code in codes):
                continue
            if not re.search(
                r"放量|缩量|成交|换手|量比|MACD|KDJ|布林|金叉|反转|共振|箱体|回撤|涨跌|涨幅|量价|确认|否证|失效|\d\s*%",
                quote,
                re.I,
            ):
                continue
            # 股票的统计观察可以引用，但没有明确入选关系时保留理由缺口。
            selected = bool(
                re.search(r"入选|入池|优先跟踪|优先观察|较优跟踪|列入|排序第|最强|入围", quote)
            )
            selected = selected or bool(
                ranked
                and re.search(
                    r"量比|换手|DIFF|箱体位置|反转证据|底部放量|放量大涨|缩量止跌|长下影|金叉",
                    quote,
                )
            )
            fields = {"reason": quote if selected else None}
            if ranked and selected:
                fields["strategy_type"] = ranked
            for clause in re.split(r"[，,]", quote):
                if re.search(r"若|需|待|次日|跌破|站稳|突破", clause):
                    if re.search(r"否证|失效", clause):
                        fields["invalidation"] = clause.strip()
                    elif "确认" in clause:
                        fields["confirmation"] = clause.strip()
            item = _reason(
                fields,
                quote,
                number,
                "selection_passage" if selected else "related_observation",
                day,
            )
            if ranked and selected:
                item.update(strategy_heading=heading, strategy_heading_line=heading_line)
            found[secid].append(item)
    return found


def _index_source(conn, source, picked, catalog):
    day = source["date"]
    members = _roster(source["content"], catalog, picked)
    by_name = catalog[1]
    lines = source["content"].splitlines()
    passages = _passages(lines, catalog, members, day)
    structured = defaultdict(list)
    for index, line in enumerate(lines, 1):
        parsed = _structured(line)
        if parsed and parsed[0] in members:
            key, fields = parsed
            item = _reason(fields, line, index, "structured_ai", day)
            if item["reason"]:
                structured[key].append(item)
    tier_count = defaultdict(int)
    for secid, _ in members:
        tier_count[secid] += 1
    for (secid, tier), (name, membership, notes) in members.items():
        reasons = list(structured[secid, tier])
        for number, quote, detail in notes:
            reasons.append(_reason({"reason": detail}, quote, number, "roster_note", day))
        # 多梯队同股的普通段落没有梯队归属，保留原文但不擅自分配到两档。
        if tier_count[secid] == 1:
            for number, line in enumerate(lines, 1):
                detail = _legacy_quote(line, secid, name, len(by_name.get(name, ())) == 1)
                if detail:
                    selected = re.search(
                        r"入选|入池|优先|列入|入围|理由|选择|选取|跟踪依据", detail
                    )
                    reasons.append(
                        _reason(
                            {"reason": detail if selected else None},
                            line,
                            number,
                            "named_passage" if selected else "related_observation",
                            day,
                        )
                    )
            used_lines = {r["line"] for r in reasons}
            reasons.extend(r for r in passages[secid] if r["line"] not in used_lines)
        distinct = list({(r["line"], r["quote"]): r for r in reasons}.values())
        conn.execute(
            "INSERT INTO tier_reason_entries(date,version,secid,tier,name,membership_origin,reasons_json) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(date,version,secid,tier) DO UPDATE SET "
            "reasons_json=excluded.reasons_json",
            (
                day,
                source["version"],
                secid,
                tier,
                name,
                membership,
                json.dumps(distinct, ensure_ascii=False),
            ),
        )


def backfill(store, dates=None):
    """以原文重建索引，保留所有版本、实时名单及首次生成时间。"""
    conn = store.conn
    catalog = _symbol_index(conn)
    if dates is None:
        dates = [
            r[0]
            for r in conn.execute(
                "SELECT date FROM daily_summary UNION SELECT date FROM tier_reason_sources ORDER BY date"
            )
        ]
    for day in dates:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            current = store.get_summary(day)
            version = archive_source(conn, current) if current else None
            snapshots = conn.execute(
                "SELECT * FROM tier_reason_sources WHERE date=?", (day,)
            ).fetchall()
            picks = conn.execute(
                "SELECT secid,tier,name,created_at FROM tier_picks WHERE date=? AND source='summary'",
                (day,),
            ).fetchall()
            for source in snapshots:
                # 原文名单解析自每个版本；只将相符的持久化名单作为兜底证据。
                matching = [
                    p
                    for p in picks
                    if p["created_at"] and p["created_at"] == source["first_published_at"]
                ]
                if source["version"] == version and len(snapshots) == 1:
                    matching = picks
                _index_source(conn, source, matching, catalog)


def _source(conn, day, version=None):
    if version:
        return conn.execute(
            "SELECT * FROM tier_reason_sources WHERE date=? AND version=?", (day, version)
        ).fetchone()
    record = conn.execute(
        "SELECT engine,content FROM daily_summary WHERE date=?", (day,)
    ).fetchone()
    if record and record["engine"] and not str(record["engine"]).startswith("rule"):
        return _source(conn, day, _version(record["engine"], record["content"]))
    return None


def readiness(conn, day):
    """只计算当前原始 AI 名单，每个交易日×证券×梯队只计一次。"""
    source = _source(conn, day)
    fields = dict.fromkeys(FIELDS, 0)
    expected = {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT secid,tier FROM tier_picks WHERE date=? AND source='summary'", (day,)
        )
    }
    available = complete = 0
    if source:
        for row in conn.execute(
            "SELECT secid,tier,reasons_json FROM tier_reason_entries WHERE date=? AND version=?",
            (day, source["version"]),
        ):
            expected.add((row["secid"], row["tier"]))
            reasons = json.loads(row["reasons_json"])
            for field in FIELDS:
                fields[field] += int(any(r.get(field) for r in reasons))
            available += int(any(r.get("reason") for r in reasons))
            complete += int(any(all(r.get(k) for k in FIELDS) for r in reasons))
    return {
        "expected": len(expected),
        "available": available,
        "missing": len(expected) - available,
        "complete": complete,
        "fields": fields,
        "source_version": source["version"] if source else None,
    }


def _day(value):
    try:
        if Date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except (ValueError, TypeError):
        raise InvalidRequest("date must be YYYY-MM-DD") from None
    return value


def source_document(conn, day, version):
    if not re.fullmatch(r"[a-f0-9]{64}", version or ""):
        raise InvalidRequest("version must be a SHA-256 identifier")
    row = _source(conn, _day(day), version)
    return dict(row) if row else None


def report(conn, day, symbols):
    day = _day(day)
    if not symbols or not symbols.strip():
        raise InvalidRequest("symbols is required")
    requested = [value.strip() for value in symbols.split(",")]
    if len(requested) > MAX_SYMBOLS or len(symbols) > MAX_SYMBOLS * 32:
        raise InvalidRequest("at most 100 symbols")
    conn.execute("SAVEPOINT tier_reason_read")
    try:
        return _report(conn, day, requested)
    finally:
        conn.execute("RELEASE tier_reason_read")


def _report(conn, day, requested):
    status = readiness(conn, day)
    versions = {
        r["version"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM tier_reason_sources WHERE date=? ORDER BY first_published_at,version",
            (day,),
        )
    }
    for version, doc in versions.items():
        doc["url"] = f"/api/tier-reasons/source?date={day}&version={version}"
        doc["current"] = version == status["source_version"]
    grouped = defaultdict(list)
    for entry in conn.execute(
        "SELECT * FROM tier_reason_entries WHERE date=? ORDER BY version,tier", (day,)
    ):
        reasons = json.loads(entry["reasons_json"])
        evidenced = any(item["reason"] for item in reasons)
        grouped[entry["secid"]].append(
            {
                "tier": entry["tier"],
                "name": entry["name"],
                "version": entry["version"],
                "first_published_at": versions[entry["version"]]["first_published_at"],
                "membership_origin": entry["membership_origin"],
                "reasons": reasons,
                "status": "available" if evidenced else "unavailable",
                "gap_reason": None if evidenced else "no_attributable_original_reason",
            }
        )
    rows = []
    for raw in requested:
        secid, error = _symbol(raw)
        picks = grouped.get(secid, [])
        if not error and not versions:
            error = "no_ai_source"
        if not error and not picks:
            known = conn.execute("SELECT 1 FROM symbols WHERE secid=?", (secid,)).fetchone()
            error = "not_in_original_tiers" if known else "unknown_symbol"
        if not error and not any(p["status"] == "available" for p in picks):
            error = "no_attributable_original_reason"
        rows.append({"requested_symbol": raw, "secid": secid, "entries": picks, "reason": error})
    return {
        "requested_date": day,
        "max_symbols": MAX_SYMBOLS,
        "sources": list(versions.values()),
        "readiness": status,
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backfill", required=True, action="store_true")
    parser.add_argument("--date", action="append", type=_day)
    args = parser.parse_args()
    with Store(Config.load().path("store.path")).init_schema() as store:
        backfill(store, args.date)
        dates = args.date or [
            r[0] for r in store.conn.execute("SELECT date FROM daily_summary ORDER BY date")
        ]
        print(json.dumps({d: readiness(store.conn, d) for d in dates}, ensure_ascii=False))


if __name__ == "__main__":
    main()
