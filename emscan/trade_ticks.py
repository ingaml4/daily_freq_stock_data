"""麦蕊当天逐笔片段。供应商交易时段约每 60 分钟更新，未提供成交编号或连续性证明。"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, localcontext

from . import tzutil
from .auction import (
    AuctionRequestError as TradeTicksRequestError,
    CurrentSnapshotService,
    SHANGHAI,
    _iso,
    _phase,
)

__all__ = ["TradeTicksService", "TradeTicksRequestError"]
SOURCE = "mairui"
ENDPOINT = "hsrl.zbjy"
OFFICIAL_DOC = "https://mairui.club/hsdata"
DATE_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
NUMBER = re.compile(r"[0-9]{1,16}(?:\.[0-9]{1,8})?")
SIDES = {"0": "unknown", "1": "buy", "2": "sell"}


def _trade_at(data):
    day, clock = data.get("d"), data.get("t")
    if not isinstance(day, str) or not isinstance(clock, str):
        return None
    value = f"{day} {clock}"
    if not DATE_TIME.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)
    except ValueError:
        return None


def _decimal(value):
    if type(value) not in (int, float, str):
        return None
    text = str(value)
    if not NUMBER.fullmatch(text):
        return None
    number = Decimal(text)
    return number if number > 0 else None


def _reported_session(stamp):
    if not stamp:
        return None, False
    phase = _phase(stamp, True)
    hm = stamp.strftime("%H:%M:%S")
    if hm == "09:25:00" and phase == "opening_result":
        return "opening_match", True
    if hm == "15:00:00" and phase == "closing_auction":
        return "closing_match", True
    return phase, phase in ("continuous_am", "continuous_pm")


def _tick(entry, position, end, received):
    data = entry if isinstance(entry, dict) else {}
    stamp = _trade_at(data)
    session, execution_session = _reported_session(stamp)
    price, volume = _decimal(data.get("p")), _decimal(data.get("v"))
    shares = int(volume) if volume is not None and volume == volume.to_integral_value() else None
    side_code = str(data.get("ts"))
    if side_code not in SIDES:
        side_code = None
    delay = (end - stamp).total_seconds() if stamp else None
    if not stamp:
        reason = "invalid_source_time"
    elif stamp > received or stamp > end:
        reason = "future_source_time"
    elif price is None or shares is None:
        reason = "invalid_price_or_volume"
    elif not execution_session:
        reason = (
            "auction_execution_unverified"
            if session
            in ("opening_cancelable", "opening_locked", "opening_result", "closing_auction")
            else "outside_trade_session"
        )
    elif stamp.date() != end.date() or delay > 60:
        reason = "stale_source_time"
    elif side_code is None:
        reason = "invalid_side_code"
    else:
        reason = None
    reported = bool(
        stamp and stamp <= min(end, received) and execution_session and price and shares
    )
    amount = None
    if price is not None and shares is not None:
        with localcontext() as ctx:
            ctx.prec = 48
            amount = float((price * shares).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    # 逐笔成交量字段 v 已是股数；单笔金额只由该条报价和股数计算。
    return stamp, {
        "source_index": position,
        "source_trade_id": None,
        "trade_at": _iso(stamp) if stamp else None,
        "session": session,
        "price": float(price) if price is not None else None,
        "shares": shares,
        "amount_yuan": amount,
        "is_reported_trade": reported,
        "side": SIDES.get(side_code, "unknown"),
        "source_side_code": int(side_code) if side_code is not None else None,
        "source": SOURCE,
        "quality": "gap" if reason else "reported",
        "reason": reason,
        "coverage_gap": True,
    }


class TradeTicksService(CurrentSnapshotService):
    """按股票缓存原始片段，只在收到查询时采集；逐笔数组按源顺序展示。"""

    endpoint = ENDPOINT

    def __init__(self, fetch, *, clock=tzutil.now, ttl_seconds=20, cache_size=24, per_minute=60):
        super().__init__(
            fetch,
            clock=clock,
            ttl_seconds=ttl_seconds,
            cache_size=cache_size,
            per_minute=per_minute,
        )

    def _details(self, payload, end, received, phase):
        ticks = []
        newest = None
        any_fresh = False
        if isinstance(payload, list):
            for position, entry in enumerate(payload):
                stamp, tick = _tick(entry, position, end, received)
                if tick["is_reported_trade"] and (newest is None or stamp > newest):
                    newest = stamp
                any_fresh |= tick["quality"] == "reported"
                ticks.append(tick)
        age = (end - newest).total_seconds() if newest else None
        if not isinstance(payload, list):
            reason = "invalid_response"
        elif not ticks:
            reason = "empty_source_fragment"
        elif newest is None:
            reason = "no_valid_reported_trade"
        elif newest.date() != end.date() or age > 60:
            reason = "stale_source_time"
        elif _phase(newest, True) != phase:
            reason = "source_phase_mismatch"
        elif not any_fresh:
            reason = "no_verifiable_recent_execution"
        else:
            reason = "source_continuity_unverified"
        return {
            "source_time": _iso(newest) if newest else None,
            "source_age_seconds": age,
            "source_update_interval_minutes": 60,
            "source_documentation": OFFICIAL_DOC,
            "source_trade_id_available": False,
            "coverage_gap": True,
            "realtime_verified": False,
            "side_basis": "provider_tag",
            "volume_unit": "share",
            "quality": "partial" if reason == "source_continuity_unverified" else "gap",
            "reason": reason,
            "ticks": ticks,
        }
