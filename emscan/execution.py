"""按日线开/收盘撮合的普通 A 股 T+1 独立订单模拟。"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite


DEFAULTS = {
    "capital": 100000.0,
    "commission_bps": 2.5,
    "min_commission": 5.0,
    "transfer_bps": 0.1,
    "sell_tax_bps": 5.0,
    "slippage_bps": 5.0,
    "entry_wait": 3,
}
STATUS = {
    "closed": "完成买卖",
    "pending_entry": "待买入",
    "entry_expired": "买入窗口已过",
    "pending_exit": "持仓待卖",
    "blocked_exit": "卖出受阻",
    "unavailable": "不可判定",
    "excluded": "不纳入模拟",
}


def settings(cfg):
    opts = {k: float(cfg.get("backtest.execution." + k, v)) for k, v in DEFAULTS.items()}
    if any(not isfinite(v) or v < 0 for v in opts.values()) or opts["capital"] <= 0:
        raise ValueError("模拟资金必须为正数，成本参数必须是有限的非负数")
    wait = opts["entry_wait"]
    if not 1 <= wait <= 60 or not wait.is_integer():
        raise ValueError("entry_wait 必须是 1 至 60 之间的交易日数")
    opts["entry_wait"] = int(wait)
    return opts


def _money(x):
    return float(Decimal(str(x)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def _cost(price, qty, side, opts):
    gross = _money(price * qty)
    parts = {
        "commission": _money(max(opts["min_commission"], gross * opts["commission_bps"] / 10000)),
        "transfer": _money(gross * opts["transfer_bps"] / 10000),
        "tax": _money(gross * opts["sell_tax_bps"] / 10000) if side == "sell" else 0.0,
        "slippage": _money(gross * opts["slippage_bps"] / 10000),
    }
    total = _money(sum(parts.values()))
    cash = _money(gross + total if side == "buy" else gross - total)
    return {"price": price, "shares": qty, "gross": gross, **parts, "cost": total, "cash": cash}


def _settle(buy, day, price, opts):
    sell = _cost(price, buy["shares"], "sell", opts)
    return {
        "sell": dict(sell, date=day),
        "net_return": round(sell["cash"] / buy["cash"] - 1, 8),
        "net_pnl": _money(sell["cash"] - buy["cash"]),
        "total_cost": _money(buy["cost"] + sell["cost"]),
    }


def _board(secid):
    if secid.startswith(("1.600", "1.601", "1.603", "1.605", "0.000", "0.001", "0.002", "0.003")):
        return 10, 100
    if secid.startswith(("0.300", "0.301")):
        return 20, 100
    if secid.startswith("1.688"):
        return 20, 200
    return None


def _limit_bounds(ref, limit):
    ref_dec = Decimal(str(ref))
    pct = Decimal(limit) / 100
    high_limit = max(_money(ref_dec * (1 + pct)), _money(ref + 0.01))
    low_limit = max(0.01, min(_money(ref_dec * (1 - pct)), _money(ref - 0.01)))
    return low_limit, high_limit


def _session(bar, prev, limit):
    """返回状态与方向性边界。没有行情时无法证明停牌，也无法判断能否成交。"""
    if not bar:
        return "缺当日日线，无法区分停牌和数据缺失", None
    if bar.get("volume") == 0:
        return "停牌或零成交", None
    if bar.get("volume") is None or not isfinite(bar["volume"]) or bar["volume"] < 0:
        return "成交量缺失或无效", None
    if bar.get("source") != "spot":
        return "缺少未复权快照", None
    fields = ("open", "high", "low", "close", "chg", "pct_chg")
    if any(bar.get(k) is None or not isfinite(bar[k]) for k in fields):
        return "缺少完整价量或涨跌字段", None
    o, h, l, c = (bar[k] for k in fields[:4])
    if not (0 < l <= min(o, c) <= max(o, c) <= h):
        return "日线价格不一致", None
    ref = _money(c - bar["chg"])
    if ref <= 0 or abs((c / ref - 1) * 100 - bar["pct_chg"]) > 0.011:
        return "当日涨跌与反推昨收不一致", None
    if not prev or prev.get("source") != "spot" or not prev.get("close"):
        return "缺少上一有效未复权收盘", None
    if _money(prev["close"]) != ref:
        return "参考昨收变动或除权，缺权益变动记录", None
    low_limit, high_limit = _limit_bounds(ref, limit)
    if h > high_limit + 1e-6 or l < low_limit - 1e-6:
        return "报价超出普通涨跌停范围，交易状态待核实", None
    return "", (low_limit, high_limit)


def _shares(price, minimum, opts):
    # 上海科创板以 100 股为步长模拟，至少 200 股；该数量也满足其一股递增规则。
    count = int(opts["capital"] / (price * 100)) * 100
    while count >= minimum and _cost(price, count, "buy", opts)["cash"] > opts["capital"]:
        count -= 100
    return count if count >= minimum else 0


def simulate(row, days, idx, series, first_seen, horizons, opts, *, entry_floor=None,
             entry_offset=1, entry_minimum=None, signal_anchored=False):
    out = {"status": "pending_entry", "reason": "", "entry_attempts": [], "buy": None, "exits": {}}
    if row["status"] == "事后补生成" or (row.get("generated_at") or "")[:10] != row["date"]:
        out.update(status="excluded", reason="名单事后生成或生成时间缺失")
        return out
    rules = _board(row["secid"])
    if not rules or not row.get("name") or any(tag in row["name"].upper() for tag in ("ST", "退")):
        out.update(status="excluded", reason="仅模拟名单中已识别的沪深普通 A 股，排除风险警示")
        return out
    if idx is None or row["date"] < "2026-07-06":
        out.update(status="unavailable", reason="缺少名单日交易日历或超出已核验制度时期")
        return out
    limit, minimum = rules
    prev = series.get(days[idx + entry_offset - 1])
    idx_buy = None
    for offset in range(entry_offset, entry_offset + opts["entry_wait"]):
        n = idx + offset
        if n >= len(days):
            out["reason"] = "等候下一次买入交易日"
            return out
        day = days[n]
        if n < 5 or not first_seen or first_seen > days[n - 5]:
            out.update(status="unavailable", reason="未证实已过上市前五个交易日")
            return out
        bar = series.get(day)
        reason, bounds = _session(bar, prev, limit)
        if not reason and entry_floor is not None and bar["open"] <= entry_floor:
            reason = "开盘价不高于名单日失效点，取消入场"
            out["entry_attempts"].append({"date": day, "reason": reason})
            out.update(status="entry_invalidated", reason=reason)
            return out
        if not reason and entry_minimum is not None and bar["open"] < entry_minimum:
            reason = "确认后开盘低于触发位，取消入场"
            out["entry_attempts"].append({"date": day, "reason": reason})
            out.update(status="confirmation_lost", reason=reason)
            return out
        if not reason and bar["open"] >= bounds[1]:
            reason = "开盘涨停，买入排队不可确认"
        if not reason:
            qty = _shares(bar["open"], minimum, opts)
            if not qty or qty > bar["volume"] * 100:
                reason = "资金不足最小申报股数" if not qty else "买入数量超过全天成交量"
        if reason:
            out["entry_attempts"].append({"date": day, "reason": reason})
            if not bounds and reason != "停牌或零成交":
                out.update(status="unavailable", reason=reason)
                return out
            if bounds:
                prev = bar
                if entry_floor is not None and bar["close"] < entry_floor:
                    out.update(status="entry_invalidated", reason="等待入场时收盘跌破失效点")
                    return out
            continue
        idx_buy = n
        prev = bar
        out["buy"] = dict(_cost(bar["open"], qty, "buy", opts), date=day)
        break
    if idx_buy is None:
        out.update(status="entry_expired", reason="买入有效期内均未成交")
        return out

    out.update(status="entered", reason="")
    if not horizons:
        return out
    out["exits"] = close_exits(
        out["buy"], days, idx_buy, idx if signal_anchored else idx_buy,
        series, limit, horizons, opts, defer_missing=signal_anchored,
    )
    return out


def close_exits(buy, days, entry_idx, origin_idx, series, limit, horizons, opts, *, defer_missing=False):
    """Replay the same closing exits for daily and intraday simulated entries."""
    prev = series.get(days[entry_idx])
    qty = buy["shares"]
    # 持仓跨日若参考价变化或缺行情，现金分红/送转与路径无法核验，不再计算该段净收益。
    pending = {
        h: {"status": "pending_exit", "target_date": None, "attempts": [], "reason": "尚未到期"}
        for h in horizons
    }
    for n in range(entry_idx + 1, len(days)):
        h_day = n - origin_idx
        day = days[n]
        bar = series.get(day)
        reason, bounds = _session(bar, prev, limit)
        for horizon in horizons:
            item = pending[horizon]
            if h_day == horizon:
                item["target_date"] = day
            if item["status"] in ("closed", "unavailable"):
                continue
            if defer_missing and not bar:
                item.setdefault("path_gaps", []).append(day)
                if h_day >= horizon:
                    item["attempts"].append({"date": day, "reason": reason})
                    item.update(status="blocked_exit", reason=reason)
                continue
            if reason and reason != "停牌或零成交":
                item.update(status="unavailable", reason=reason, problem_date=day)
                continue
            if h_day < horizon:
                continue
            blocked = reason
            if not blocked and bar["close"] <= bounds[0]:
                blocked = "收盘跌停，卖出排队不可确认"
            if not blocked and bar["volume"] * 100 < qty:
                blocked = "卖出数量超过全天成交量"
            if blocked:
                item["attempts"].append({"date": day, "reason": blocked})
                item.update(status="blocked_exit", reason=blocked)
                continue
            item.update(status="closed", reason="", **_settle(buy, day, bar["close"], opts))
        if bounds:
            prev = bar
        if all(item["status"] in ("closed", "unavailable") for item in pending.values()):
            break
    for horizon, item in pending.items():
        if origin_idx + horizon < len(days):
            item["target_date"] = days[origin_idx + horizon]
    return pending


def csv_columns(horizons):
    keys = ["sim_buy_date", "sim_buy_price", "sim_shares", "sim_buy_cost", "sim_entry_attempts"]
    metrics = (
        "status",
        "reason",
        "target_date",
        "sell_date",
        "sell_price",
        "total_cost",
        "net_pnl",
        "net_return",
        "exit_attempts",
    )
    return keys + [f"sim_{key}_{h}d" for h in horizons for key in metrics]


def csv_values(row, horizons):
    """将逐持期模拟压平供网页和 CLI 导出；没有模拟成交时价格与成本留空。"""
    order = row["execution"]
    buy = order["buy"] or {}
    record = {
        "sim_buy_date": buy.get("date"),
        "sim_buy_price": buy.get("price"),
        "sim_shares": buy.get("shares"),
        "sim_buy_cost": buy.get("cost"),
        "sim_entry_attempts": "; ".join(
            a["date"] + " " + a["reason"] for a in order["entry_attempts"]
        ),
    }
    for h in horizons:
        exit_ = order["exits"].get(h, order)
        sell = exit_.get("sell") or {}
        values = {
            "status": exit_["status"],
            "reason": exit_["reason"],
            "target_date": exit_.get("target_date"),
            "sell_date": sell.get("date"),
            "sell_price": sell.get("price"),
            "total_cost": exit_.get("total_cost"),
            "net_pnl": exit_.get("net_pnl"),
            "net_return": exit_.get("net_return"),
            "exit_attempts": "; ".join(
                a["date"] + " " + a["reason"] for a in exit_.get("attempts", [])
            ),
        }
        record.update({f"sim_{key}_{h}d": value for key, value in values.items()})
    return record


def aggregate(rows, horizons):
    """每个梯队×持期单独计算已完成买卖的分母及其余状态。"""
    summary = {}
    for tier in sorted({r["tier"] for r in rows}):
        group = [r["execution"] for r in rows if r["tier"] == tier]
        stats = {}
        for horizon in horizons:
            states = Counter()
            returns, costs = [], []
            for order in group:
                outcome = order["exits"].get(horizon) or order
                states[outcome["status"]] += 1
                if outcome["status"] == "closed":
                    returns.append(outcome["net_return"])
                    costs.append(outcome["total_cost"])
            n = len(returns)
            stats[str(horizon)] = {
                "total": len(group),
                "n": n,
                "states": dict(states),
                "avg_net_return": sum(returns) / n if n else None,
                "win_rate": sum(r > 0 for r in returns) / n if n else None,
                "avg_cost": round(sum(costs) / n, 2) if n else None,
            }
        summary[str(tier)] = stats
    return summary
