"""D 日收盘候选的独立订单回放；只用未复权 spot 模拟 T+1 买卖。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, time, timedelta, timezone
from math import isfinite

from . import execution, tzutil

RULES = {
    "fixed_5d": "fixed_5d_close.v1",
    "structure_time": "close_confirmed_next_open.v1",
}
SHANGHAI = timezone(timedelta(hours=8))
HISTORICAL_SOURCES = {"kline", "qfq", "history", "kline_qfq", "historical"}
PRICE_FIELDS = ("open", "high", "low", "close", "volume", "chg", "pct_chg")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return num if isfinite(num) else None


class _Bars:
    """按需校验读取到的行，避免在每一笔订单上复制整只股票的全部历史。"""

    def __init__(self, series):
        self.series = series

    def get(self, day):
        row = self.series.get(day)
        if not isinstance(row, dict):
            return None
        bar = {"source": row.get("source"), **{k: _number(row.get(k)) for k in PRICE_FIELDS}}
        if bar["source"] != "spot" and bar["volume"] == 0:
            bar["volume"] = None  # 历史源的零成交字段不能证明该日实际停牌。
        return bar


def _provenance(candidate, day):
    if candidate.get("status") == "事后补生成":
        return None
    stamp = candidate.get("available_at") or candidate.get("generated_at")
    if stamp:
        try:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(SHANGHAI)
        if dt.date().isoformat() != day or dt.time() < time(15):
            return None
        return "dated_eod_record"
    if candidate.get("point_in_time") is True:
        return "point_in_time_reconstruction"
    return None


def _settings(cfg, opts):
    options = opts if opts is not None else cfg
    if isinstance(options, dict):
        # 接受完整或部分 execution 参数，也接受 Config 的 dotted keys。
        options = {
            "backtest.execution."
            + key: options.get(key, options.get("backtest.execution." + key, default))
            for key, default in execution.DEFAULTS.items()
        }
    result = execution.settings(options if options is not None else {})
    result["entry_wait"] = min(3, result["entry_wait"])
    return result


def _structure(out, days, series, opts, time_exit, *, target_index=None):
    buy = out["buy"]
    start = bisect_left(days, buy["date"])
    target = start + time_exit if target_index is None else target_index
    out["target_date"] = days[target] if target < len(days) else None
    stop = out["invalidation_price"]
    limit, _ = execution._board(out["secid"])
    prev = series.get(buy["date"])
    # 买入当日收盘可发出离场指令，首个可能的卖出点在下一个交易日开盘。
    if prev["close"] < stop:
        out["trigger_date"] = buy["date"]
    out.update(status="pending_exit", reason="等候可卖交易日或时间退出日")
    for n in range(start + 1, len(days)):
        day = days[n]
        bar = series.get(day)
        reason, bounds = execution._session(bar, prev, limit)
        if target_index is not None and not bar:
            out.setdefault("path_gaps", []).append(day)
            if out["trigger_date"] or n >= target:
                out["exit_attempts"].append({"date": day, "reason": reason})
                out.update(status="blocked_exit", reason=reason)
            continue
        if reason and reason != "停牌或零成交":
            out.update(status="unavailable", reason=reason, problem_date=day)
            return
        at_open = out["trigger_date"] is not None
        if at_open or n >= target:
            key = "open" if at_open else "close"
            if not reason and bar[key] <= bounds[0]:
                reason = "开盘跌停，卖出排队不可确认" if at_open else "收盘跌停，卖出排队不可确认"
            if not reason and bar["volume"] * 100 < buy["shares"]:
                reason = "卖出数量超过全天成交量"
            if reason:
                out["exit_attempts"].append({"date": day, "reason": reason})
                out.update(status="blocked_exit", reason=reason)
            else:
                out.update(
                    status="closed",
                    reason="structure_next_open" if at_open else "time_exit_close",
                    **execution._settle(buy, day, bar[key], opts),
                )
                return
        if bounds:
            prev = bar
            if out["trigger_date"] is None and bar["close"] < stop:
                out["trigger_date"] = day


def simulate_candidate(
    candidate, days, series, cfg=None, *, opts=None, exit_policy="fixed_5d", time_exit=5, as_of=None,
    entry_policy=None, structure_sensitivity=False,
):
    """days 为完整、升序交易日；series 含 D 日及之前的上市行情证据。

    传入 D 日收盘后生成的名单时间，或对严格按 D 截断重建的信号显式声明 point_in_time=True。
    数据日不应包含尚未收盘的日线。time_exit 以买入日为第 0 日。
    eligible_for_validation 仅表示完成了一笔满足所列日线约束的模拟，不证明真实成交。
    """
    if exit_policy not in RULES:
        raise ValueError("不支持的退出策略")
    if (structure_sensitivity and not entry_policy) or entry_policy not in (None, "open", "daily_confirmation") or (
        entry_policy and exit_policy != "fixed_5d"
    ):
        raise ValueError("入场对照仅支持共同的信号日后第五个交易日收盘退出")
    if (
        type(time_exit) is not int
        or time_exit < 1
        or (exit_policy == "fixed_5d" and time_exit != 5)
    ):
        raise ValueError("时间退出天数必须为正整数，fixed_5d 固定为 5 个交易日")
    options = _settings(cfg, opts)
    day = str(candidate.get("date") or "")
    stop = _number(candidate.get("invalidation_price"))
    provenance = _provenance(candidate, day)
    out = {
        "date": day,
        "secid": candidate.get("secid"),
        "name": candidate.get("name"),
        "strategy": candidate.get("strategy"),
        "exit_policy": exit_policy,
        "time_exit": time_exit,
        "exit_rule_version": RULES[exit_policy],
        "invalidation_price": stop,
        "invalidation_basis": candidate.get("invalidation_basis"),
        "provenance": provenance,
        "status": "unavailable",
        "reason": "",
        "quality": "unavailable",
        "available": False,
        "eligible_for_validation": False,
        "buy": None,
        "sell": None,
        "net_return": None,
        "net_pnl": None,
        "cost": None,
        "total_cost": None,
        "target_date": None,
        "trigger_date": None,
        "entry_attempts": [],
        "exit_attempts": [],
    }
    if not provenance:
        out.update(status="excluded", reason="缺少名单日收盘可见证据或名单为事后补生成")
        return out
    if not isinstance(out["secid"], str) or not isinstance(out["name"], str):
        out.update(status="excluded", reason="缺少有效股票代码或名称")
        return out
    baseline = candidate.get("is_baseline") is True or candidate.get("strategy") in (
        None,
        "",
        "baseline",
        "baseline_core",
        "original",
        "original_baseline",
    )
    omit_floor = (
        baseline and not entry_policy and exit_policy == "fixed_5d"
        and candidate.get("invalidation_price") is None
    )
    if not omit_floor and (stop is None or stop <= 0):
        out["reason"] = "缺少有效的名单日固定失效点"
        return out
    observation = as_of or tzutil.now()
    if isinstance(observation, str):
        observation = datetime.fromisoformat(observation.replace("Z", "+00:00"))
    if not isinstance(observation, datetime):
        raise ValueError("as_of 必须为有时分秒的日期时间")
    if observation.tzinfo is not None:
        observation = observation.astimezone(SHANGHAI)
    last_day = observation.date()
    if observation.time() < time(15):
        last_day -= timedelta(days=1)
    end = bisect_right(days, last_day.isoformat())
    if end < len(days):
        days = days[:end]
    idx = bisect_left(days, day)
    if idx == len(days) or days[idx] != day:
        out["reason"] = "缺少名单日交易日历"
        return out
    bars = _Bars(series)
    signal = bars.get(day) or {}
    basis = out["invalidation_basis"]
    if signal.get("source") != "spot" or (basis and basis not in ("spot", "unadjusted", "raw")):
        if signal.get("source") in HISTORICAL_SOURCES or basis in ("qfq", "kline", "adjusted"):
            out["quality"] = "exploratory_history"
        out["reason"] = "缺少同价格口径的未复权名单日收盘与失效点"
        return out
    if signal.get("close") is None or signal["close"] <= 0:
        out["reason"] = "缺少名单日有效收盘"
        return out
    if stop is not None and signal["close"] <= stop:
        out.update(status="entry_invalidated", reason="名单日收盘不高于失效点")
        return out

    offset, trigger = 1, None
    if entry_policy:
        options["entry_wait"] = 1
        out["entry_policy"] = entry_policy
        out["exit_rule_version"] = "signal_day_plus_5_close.v1"
    if entry_policy == "daily_confirmation":
        trigger = _number(candidate.get("confirmation_price"))
        rules = execution._board(out["secid"])
        if trigger is None or trigger <= 0 or not rules:
            out["reason"] = "缺有效触发位或普通 A 股制度依据"
            return out
        if idx + 1 >= len(days):
            out.update(status="pending_entry", reason="等候确认日收盘")
            return out
        confirm_day = days[idx + 1]
        confirmation = bars.get(confirm_day)
        reason, _ = execution._session(confirmation, signal, rules[0])
        out["confirmation"] = {"date": confirm_day, "trigger": trigger, "reason": reason}
        if reason:
            if reason == "停牌或零成交":
                out["status"] = "confirmation_not_met"
            out["reason"] = reason
            return out
        out["confirmation"].update(open=confirmation["open"], close=confirmation["close"])
        if min(confirmation["open"], confirmation["close"]) <= stop:
            out.update(status="entry_invalidated", reason="确认日开盘或收盘不高于失效位")
            return out
        if confirmation["close"] <= trigger:
            out.update(status="confirmation_not_met", reason="确认日收盘未站上触发位")
            return out
        offset = 2
    first = candidate.get("first_seen")
    if not first:
        first = min(
            (
                d
                for d, b in series.items()
                if d <= day and isinstance(b, dict) and (_number(b.get("close")) or 0) > 0
            ),
            default=None,
        )
    # 可见性已在上方验证；供原模拟器沿用其已核验制度日期、股票范围、入场与交易成本约束。
    row = dict(candidate, status="已完成", generated_at=day, date=day)
    horizons = (5,) if exit_policy == "fixed_5d" else ()
    order = execution.simulate(
        row, days, idx, bars, str(first) if first else None, horizons, options, entry_floor=stop,
        entry_offset=offset, entry_minimum=trigger, signal_anchored=bool(entry_policy),
    )
    out.update({k: order[k] for k in ("status", "reason", "buy", "entry_attempts")})
    if order["buy"]:
        if exit_policy == "structure_time" or structure_sensitivity:
            target_index = idx + time_exit if structure_sensitivity else None
            _structure(out, days, bars, options, time_exit, target_index=target_index)
            if structure_sensitivity:
                out["exit_rule_version"] = "structure_or_signal_day_plus_5.v1"
        else:
            exit_ = order["exits"][5]
            out.update({k: v for k, v in exit_.items() if k != "attempts"})
            out["exit_attempts"] = exit_["attempts"]
            if out["status"] == "closed":
                out["reason"] = "time_exit_close"
    if out["buy"]:
        out["buy"]["execution_phase"] = "open"
    if out["status"] == "closed":
        out["sell"]["execution_phase"] = (
            "open" if out["reason"] == "structure_next_open" else "close"
        )
        out.update(
            available=True,
            eligible_for_validation=not out.get("path_gaps"),
            quality="spot_daily_with_unverified_gaps" if out.get("path_gaps") else "spot_daily_simulation",
            cost=out["total_cost"],
        )
    elif out["status"] == "unavailable":
        attempts = out["entry_attempts"]
        failed = out.get("problem_date") or (attempts[-1]["date"] if attempts else None)
        if failed and (bars.get(failed) or {}).get("source") in HISTORICAL_SOURCES:
            out["quality"] = "exploratory_history"
    else:
        out["quality"] = "no_completed_trade"
    return out
