"""Research-only pre-order sizing. No broker, notification or live-default writes."""

from dataclasses import asdict, dataclass
from datetime import datetime
from math import isfinite

from . import execution

VERSION = "position-sizing-research-v1"


@dataclass(frozen=True)
class Policy:
    first_risk: float = 0.0025
    name_risk: float = 0.005
    name_value: float = 0.20
    theme_value: float = 0.30
    gross_value: float = 0.55
    portfolio_risk: float = 0.0125
    name_stress: float = 0.01
    stress_drop: float = 0.06
    warning_drawdown: float = 0.05
    stop_drawdown: float = 0.10
    fixed_amount: float = 48000.0
    max_age_seconds: float = 60.0

    def __post_init__(self):
        for key, value in asdict(self).items():
            if not _number(value) or value <= 0:
                raise ValueError(f"invalid policy {key}")
            if key not in ("fixed_amount", "max_age_seconds") and value >= 1:
                raise ValueError(f"invalid policy fraction {key}")
        if not self.first_risk <= self.name_risk <= self.portfolio_risk:
            raise ValueError("risk budgets must be nested")
        if not self.name_value <= self.theme_value <= self.gross_value:
            raise ValueError("exposure limits must be nested")
        if self.warning_drawdown >= self.stop_drawdown:
            raise ValueError("drawdown levels must be ordered")


def _number(value):
    return type(value) in (int, float) and isfinite(value)


def _stamp(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.utcoffset() is None:
        raise ValueError("timezone required")
    return result


def _themes(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(x, str) and x.strip() for x in value)
            and len(value) == len(set(value)))


def loss_at(entry, exit_price, shares, costs=None):
    if shares == 0:
        return 0.0
    costs = execution.settings({}) if costs is None else costs
    return execution._money(max(0, execution._cost(entry, shares, "buy", costs)["cash"]
                                - execution._cost(exit_price, shares, "sell", costs)["cash"]))


def size_order(candidate, account, *, mode="risk", policy=None, costs=None):
    """Consume explicit point-in-time snapshots; missing facts yield shares=None.

    cash_available is either before all listed pending reservations or already net
    of those reservations. Positions and unfilled order remainders are separate
    exposure records. Broker adapters must reconcile partial fills first.
    """
    policy = policy or Policy()
    costs = execution.settings({}) if costs is None else costs
    if mode not in ("all_in", "fixed", "risk", "risk_first", "risk_add"):
        raise ValueError("unknown sizing mode")
    out = {"version": VERSION, "research_only": True, "mode": mode,
           "status": "unavailable", "shares": None, "reasons": [],
           "policy": asdict(policy), "costs": dict(costs)}

    def missing(reason):
        out["reasons"].append(reason)
        return out

    try:
        at, snapshot = _stamp(candidate.get("at")), _stamp(account.get("as_of"))
        visible = _stamp(candidate.get("risk_available_at"))
        theme_at = _stamp(candidate.get("themes_available_at"))
    except (ValueError, TypeError):
        return missing("missing_timestamp_or_timezone")
    if (not 0 <= (at - snapshot).total_seconds() <= policy.max_age_seconds
            or visible > at or theme_at > at):
        return missing("future_or_stale_snapshot")
    equity, cash, peak = (account.get(k) for k in ("equity", "cash_available", "monthly_peak_equity"))
    if any(not _number(x) for x in (equity, cash, peak)) or not 0 < equity <= peak or cash < 0:
        return missing("missing_or_invalid_equity_cash_monthly_peak")
    if account.get("kind") not in ("research", "broker_verified"):
        return missing("account_snapshot_unverified")
    if account.get("cash_basis") not in ("before_pending", "net_of_pending"):
        return missing("cash_reservation_basis_unknown")
    positions, pending = account.get("positions"), account.get("pending_orders")
    if not isinstance(positions, list) or not isinstance(pending, list):
        return missing("positions_or_pending_orders_unknown")
    exposure = positions + pending
    ids = set()
    for row in exposure:
        if not isinstance(row, dict):
            return missing("invalid_exposure_record")
        if not isinstance(row.get("id"), str) or not row["id"] or row["id"] in ids:
            return missing("missing_or_duplicate_exposure_id")
        ids.add(row["id"])
        if (not isinstance(row.get("secid"), str) or not _themes(row.get("themes"))
                or any(not _number(row.get(k)) or row[k] < 0
                       for k in ("market_value", "planned_loss", "stress_loss"))):
            return missing("incomplete_position_or_order_exposure")
    if any(not _number(p.get("reserved_cash")) or p["reserved_cash"] < p["market_value"] for p in pending):
        return missing("pending_cash_reservation_unknown_or_understated")
    if account["cash_basis"] == "before_pending":
        cash -= sum(p["reserved_cash"] for p in pending)
    cash = max(0, cash)
    price, risk, secid = (candidate.get(k) for k in ("entry_price", "risk_price", "secid"))
    if not isinstance(secid, str):
        return missing("missing_security")
    rules = execution._board(secid)
    if not rules or secid.startswith("1.688"):
        return missing("unsupported_security_or_no_star_market_permission")
    if not _number(price) or not _number(risk) or not 0 < risk < price:
        return missing("missing_valid_structural_risk")
    if not _themes(candidate.get("themes")):
        return missing("theme_membership_unknown")
    depth = candidate.get("ask_shares")
    if type(depth) is not int or depth < 0:
        return missing("verified_ask_quantity_missing")
    if candidate.get("execution_verified") is not True:
        return missing("entry_quote_or_eligibility_unverified")
    same = [row for row in exposure if row["secid"] == secid]
    if mode == "risk_add":
        try:
            previous = _stamp(candidate.get("previous_entry_at"))
            fresh = _stamp(candidate.get("add_signal_available_at"))
        except (ValueError, TypeError):
            return missing("missing_new_add_signal")
        if (not same or not previous < fresh < at or not candidate.get("add_signal_id")
                or candidate.get("add_signal_id") == candidate.get("previous_signal_id")):
            return missing("add_requires_distinct_later_evidence_and_existing_position")
        prior_price = candidate.get("previous_entry_price")
        if (not _number(prior_price) or prior_price <= 0 or price < prior_price
                or candidate.get("previous_risk_price") != risk):
            return missing("add_requires_same_structure_and_no_averaging_down")
    elif same:
        out.update(status="skipped", shares=0, reasons=["already_held_or_pending"])
        return out
    drawdown = max(0.0, 1 - equity / peak)
    risk_mode = mode.startswith("risk")
    multiplier = 0.5 if drawdown >= policy.warning_drawdown - 1e-12 else 1.0
    if risk_mode and drawdown >= policy.stop_drawdown - 1e-12:
        out.update(status="skipped", shares=0, reasons=["monthly_drawdown_stop"],
                   monthly_drawdown=drawdown)
        return out
    stress_price = min(risk, price * (1 - policy.stress_drop))
    planned = lambda q: loss_at(price, risk, q, costs)
    stressed = lambda q: loss_at(price, stress_price, q, costs)
    buy_cash = lambda q: execution._cost(price, q, "buy", costs)["cash"] if q else 0
    limits = {"available_cash": (cash, buy_cash), "displayed_ask": (depth, lambda q: q)}
    if mode == "fixed":
        limits["fixed_amount"] = (policy.fixed_amount, buy_cash)
    if risk_mode:
        total_risk = sum(p["planned_loss"] for p in exposure)
        own_risk = sum(p["planned_loss"] for p in same)
        limits.update({
            "single_name_value": (equity * policy.name_value - sum(p["market_value"] for p in same), lambda q: q * price),
            "gross_value": (equity * policy.gross_value - sum(p["market_value"] for p in exposure), lambda q: q * price),
            "single_name_risk": (equity * policy.name_risk * multiplier - own_risk, planned),
            "portfolio_risk": (equity * policy.portfolio_risk * multiplier - total_risk, planned),
            "overnight_stress": (equity * policy.name_stress - sum(p["stress_loss"] for p in same), stressed),
        })
        if mode == "risk_first":
            limits["first_entry_risk"] = (equity * policy.first_risk * multiplier - own_risk, planned)
        for theme in candidate["themes"]:
            used = sum(p["market_value"] for p in exposure if theme in p["themes"])
            limits["theme:" + theme] = (equity * policy.theme_value - used, lambda q: q * price)
    upper = max(0, int(cash / price) // 100)
    caps = {}
    for label, (budget, measure) in limits.items():
        lo, hi = 0, upper
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if measure(mid * 100) <= max(0, budget) + 1e-8:
                lo = mid
            else:
                hi = mid - 1
        caps[label] = lo * 100
    qty = min(caps.values())
    limiting = [label for label, cap in caps.items() if cap == qty]
    out.update(status="sized" if qty else "skipped", shares=qty, reasons=limiting,
               limiting_shares=caps, constraint_budgets={k: round(v[0], 4) for k, v in limits.items()},
               entry_price=price, risk_price=risk, stress_exit_price=stress_price,
               planned_loss=planned(qty), stress_loss=stressed(qty),
               buy=execution._cost(price, qty, "buy", costs) if qty else None,
               remaining_cash=execution._money(cash - buy_cash(qty)),
               monthly_drawdown=drawdown, risk_budget_multiplier=multiplier if risk_mode else None,
               execution_basis="displayed_book_proxy_not_guaranteed_fill")
    return out


def main():
    import argparse
    import json
    from pathlib import Path
    from .opportunity_archive import _atomic_write

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8-sig"))
    policy = Policy(**request.get("policy", {}))
    costs = execution.settings({"backtest.execution." + k: v for k, v in request.get("costs", {}).items()})
    result = size_order(request["candidate"], request["account"],
                        mode=request.get("mode", "risk"), policy=policy, costs=costs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(args.out, json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "shares": result["shares"], "out": str(args.out)}))


if __name__ == "__main__":
    main()
