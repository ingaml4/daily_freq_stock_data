"""按需查询沪深 A 股五档，区分行情时间、采集时间和集合竞价专用字段。"""

from __future__ import annotations

import math
import re
import threading
from collections import OrderedDict, deque
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone

from . import tzutil

SHANGHAI = timezone(timedelta(hours=8))
ENDPOINT = "hsstock.real.five"
UNIT_SOURCE = "https://www.mairuiapi.com/blog/05-2/"
MAX_SYMBOLS = 10
ACTIVE = {
    "opening_cancelable",
    "opening_locked",
    "opening_result",
    "continuous_am",
    "continuous_pm",
    "closing_auction",
}
SH_PREFIX = ("600", "601", "603", "605")
SZ_PREFIX = ("000", "001", "002", "003", "300", "301")


class AuctionRequestError(ValueError):
    pass


def _phase(stamp, trading_day):
    if stamp.weekday() >= 5 or trading_day is False:
        return "non_trading_day"
    hm = stamp.strftime("%H:%M:%S")
    if hm < "09:15:00":
        return "preopen"
    if hm < "09:20:00":
        return "opening_cancelable"
    if hm < "09:25:00":
        return "opening_locked"
    if hm < "09:30:00":
        return "opening_result"
    if hm <= "11:30:00":
        return "continuous_am"
    if hm < "13:00:00":
        return "lunch"
    if hm < "14:57:00":
        return "continuous_pm"
    return "closing_auction" if hm <= "15:00:00" else "closed"


def _symbol(raw):
    if not isinstance(raw, str):
        raise AuctionRequestError("股票代码必须为字符串")
    key = raw.strip().upper()
    match = re.fullmatch(
        r"(?:(SH|SZ|BJ)([0-9]{6})|([0-9]{6})(?:\.(SH|SZ|BJ))?|([012])\.([0-9]{6}))", key
    )
    if not match:
        raise AuctionRequestError("股票代码格式不正确")
    prefix, prefixed, suffixed, suffix, market_id, internal = match.groups()
    code = prefixed or suffixed or internal
    expected = (
        "SH"
        if code.startswith(SH_PREFIX + ("688", "689"))
        else ("SZ" if code.startswith(SZ_PREFIX) else None)
    )
    market_ids = {"0": "SZ", "1": "SH", "2": "BJ"}
    inferred = "BJ" if code.startswith(("4", "8", "92")) else None
    market = prefix or suffix or market_ids.get(market_id) or expected or inferred
    secid = {"SH": "1.", "SZ": "0.", "BJ": "2."}.get(market, "") + code
    if expected and market != expected:
        reason = "market_code_mismatch"
    elif code.startswith(("688", "689")) and market == "SH":
        reason = "excluded_star_market"
    elif not expected:
        reason = "unsupported_market_or_instrument"
    else:
        reason = None
    return (market or "") + code, secid, reason


def _numeric(value):
    if type(value) not in (int, float, str):
        return None
    if isinstance(value, str) and not re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,10})?", value):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(number) and number >= 0 else None


def _book(payload, now, received, phase):
    raw = {}
    if not isinstance(payload, dict):
        return raw, None, "gap", "invalid_response"
    for key in ("ps", "pb", "vs", "vb"):
        values = payload.get(key)
        if isinstance(values, list):
            raw[key] = [_numeric(x) for x in values[:5]]
    t = payload.get("t")
    if not isinstance(t, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", t
    ):
        return raw, None, "gap", "invalid_source_time"
    try:
        stamp = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
    except ValueError:
        return raw, None, "gap", "invalid_source_time"
    raw["t"] = t
    if stamp > received or stamp > now:
        return raw, stamp, "gap", "future_source_time"
    if stamp.date() != now.date():
        return raw, stamp, "gap", "stale_source_time"
    source_phase = _phase(stamp, True)
    if phase not in ACTIVE or source_phase != phase:
        return raw, stamp, "gap", "source_phase_mismatch"
    if (now - stamp).total_seconds() > 60:
        return raw, stamp, "gap", "stale_source_time"
    complete, live = True, False
    for prices, volumes in (("ps", "vs"), ("pb", "vb")):
        p, v = raw.get(prices, []), raw.get(volumes, [])
        if (
            len(p) != 5
            or len(v) != 5
            or len(payload.get(prices, [])) != 5
            or len(payload.get(volumes, [])) != 5
        ):
            complete = False
        for price, volume in zip(p, v):
            if price is None or volume is None:
                complete = False
            elif float(price) > 0 and float(volume) > 0:
                live = True
            elif bool(float(price)) != bool(float(volume)):
                complete = False
    if not live:
        return raw, stamp, "gap", "no_quoted_levels"
    return (raw, stamp, "ok", None) if complete else (raw, stamp, "partial", "incomplete_levels")


def _iso(stamp):
    return stamp.isoformat(timespec="microseconds")


class CurrentSnapshotService:
    """单次行情快照的交易时段、每股缓存、并发合并及实例用量上限。"""

    query_phases = ACTIVE

    def __init__(self, fetch, *, clock=tzutil.now, ttl_seconds=10, cache_size=512, per_minute=60):
        self.fetch = fetch
        self.clock = clock
        self.ttl = ttl_seconds
        self.cache_size = cache_size
        self.per_minute = per_minute
        self._cache = OrderedDict()
        self._inflight = {}
        self._starts = deque()
        self._lock = threading.Lock()

    def _now(self):
        stamp = self.clock()
        return stamp.astimezone(SHANGHAI) if stamp.tzinfo else stamp.replace(tzinfo=SHANGHAI)

    def _fetch(self, key, now):
        with self._lock:
            cached = self._cache.get(key)
            if cached and 0 <= (now - cached[0]).total_seconds() < self.ttl:
                self._cache.move_to_end(key)
                return (*cached, True)
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                while self._starts and (now - self._starts[0]).total_seconds() >= 60:
                    self._starts.popleft()
                if len(self._starts) >= self.per_minute:
                    return now, None, "request_budget_exhausted", False, False
                self._starts.append(now)
                future = self._inflight[key] = Future()
        if not owner:
            return (*future.result(), True)
        try:
            try:
                payload = self.fetch(key[0])
                failure, requested = None, True
            except Exception as exc:
                # 上游异常可能含请求路径与证书；不回显。
                requested = getattr(exc, "upstream_requested", True)
                payload, failure = None, "upstream_error" if requested else "upstream_skipped"
            result = (self._now(), payload, failure, requested)
            with self._lock:
                self._cache[key] = result
                self._cache.move_to_end(key)
                if len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            future.set_result(result)
            return (*result, False)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)

    def snapshot(self, symbols, *, is_trading_day=None):
        if not isinstance(symbols, (list, tuple)) or not 1 <= len(symbols) <= MAX_SYMBOLS:
            raise AuctionRequestError("每批须提供 1 至 10 个标的")
        if is_trading_day is not None and type(is_trading_day) is not bool:
            raise AuctionRequestError("交易日状态必须为 true、false 或 null")
        parsed = dict.fromkeys(_symbol(value) for value in symbols)
        start = self._now()
        frames = []
        for symbol, secid, unsupported in parsed:
            now = self._now()
            trading = is_trading_day if now.date() == start.date() else None
            phase = _phase(now, trading)
            received, payload, reason = now, None, unsupported
            requested = cached = False
            if not reason:
                if phase in self.query_phases:
                    received, payload, reason, requested, cached = self._fetch(
                        (secid, now.date(), phase), now
                    )
                else:
                    reason = "market_closed"
            frames.append((symbol, secid, received, payload, reason, requested, cached))

        # 最慢一只返回后，再以同一个时刻检查整批的时效与交易阶段。
        end = self._now()
        phase = _phase(end, is_trading_day if end.date() == start.date() else None)
        items = []
        for symbol, secid, received, payload, reason, requested, cached in frames:
            details = self._details(payload, end, received, phase)
            items.append(
                {
                    **details,
                    "symbol": symbol,
                    "secid": secid,
                    "phase": phase,
                    "received_at": _iso(received),
                    "source": "mairui",
                    "source_endpoint": self.endpoint,
                    "upstream_requested": requested,
                    "cached": cached,
                    "reason": reason or details["reason"],
                }
            )
        return {
            "schema_version": "1.0",
            "requested_at": _iso(start),
            "as_of": _iso(end),
            "cache_hits": sum(item["cached"] for item in items),
            "upstream_requests": sum(
                item["upstream_requested"] and not item["cached"] for item in items
            ),
            "phase": phase,
            "timezone": "Asia/Shanghai",
            "items": items,
        }


class AuctionBookService(CurrentSnapshotService):
    """fetch(secid) 返回当前五档。"""

    endpoint = ENDPOINT

    def _details(self, payload, end, received, phase):
        raw, stamp, quality, book_reason = _book(payload, end, received, phase)
        return {
            "t": raw.get("t"),
            "source_time": _iso(stamp) if stamp else None,
            "book_quality": quality,
            "reason": book_reason,
            "raw": raw,
            "volume_unit": "lot",
            "shares_per_lot": 100,
            "unit_source": UNIT_SOURCE,
            "quality": "gap",
            "auction_reason": "virtual_match_fields_unverified",
            "indicative_match_price": None,
            "matched_volume": None,
            "unmatched_volume": None,
            "unmatched_direction": None,
        }
