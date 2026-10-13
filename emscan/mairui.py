"""麦蕊智数（mairui.club）行情接口客户端。

本项目用到的四个接口（都需要「证书」licence，见 https://www.mairui.club/hsdata ）：

1) 股票列表（代码 + 名称，沪深 A 股）
   ``GET https://api.mairuiapi.com/hslt/list/{licence}``

2) 实时交易数据（全市场 / 多股 / 单股，含当日成交量·额·换手率·量比）
   ``GET https://api.mairuiapi.com/hsrl/real/all/{licence}``                 全市场
   ``GET https://api.mairuiapi.com/hsrl/ssjy_more/{licence}?stock_codes=...`` 一次最多 20 只
   ``GET https://api.mairuiapi.com/hsrl/ssjy/{code}/{licence}``              单只

3) 历史分时交易（日线，用于回补历史与基准计算）
   ``GET https://api.mairuiapi.com/hsstock/history/{code}.{MKT}/d/{adjust}/{licence}?st=&et=&lt=``

4) 交易日历（判断"今天是不是交易日"，比看行情时间戳更直接）
   ``GET https://api.mairuiapi.com/tcalendar/list/{year}/{licence}``

5) 日频技术指标（MACD / KDJ / BOLL / MA，一次一种指标）
   ``GET .../hsstock/history/{macd|kdj|boll|ma}/{code}.{MKT}/d/{adjust}/{licence}?st=&et=&lt=``

字段含义见 ``docs/mairui_api.md``。所有解析逻辑都写成纯函数，
这样即使没有网络，也能用固定报文做单元测试（见 tests/test_mairui_parse.py）。

内部统一使用 ``secid = "{market}.{code}"``（``1``=沪、``0``=深、``2``=京），
与麦蕊的 ``000001.SZ`` 写法互转只发生在 ``dm_of()`` / ``secid_of()`` 两处。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import requests

log = logging.getLogger(__name__)

# 官方文档主域名；api.mairui.club 是同一服务的备用域名
BASE_URL = "https://api.mairuiapi.com"

PATH_STOCK_LIST = "/hslt/list/{licence}"
PATH_QUOTE_ALL = "/hsrl/real/all/{licence}"
PATH_QUOTE_MORE = "/hsrl/ssjy_more/{licence}"
PATH_QUOTE_ONE = "/hsrl/ssjy/{code}/{licence}"
PATH_KLINE = "/hsstock/history/{dm}/{level}/{adjust}/{licence}"
PATH_INDICATOR = "/hsstock/history/{kind}/{dm}/{level}/{adjust}/{licence}"
PATH_LATEST = "/hsstock/latest/{dm}/{level}/{adjust}/{licence}"
PATH_INSTRUMENT = "/hsstock/instrument/{dm}/{licence}"
PATH_CALENDAR = "/tcalendar/list/{year}/{licence}"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

# 市场编号：与东财口径一致（1=沪、0=深），北交所另用 2
MARKET_SH = 1
MARKET_SZ = 0
MARKET_BJ = 2

MARKET_OF_SUFFIX: Dict[str, int] = {"SH": MARKET_SH, "SZ": MARKET_SZ, "BJ": MARKET_BJ}
SUFFIX_OF_MARKET: Dict[int, str] = {MARKET_SH: "SH", MARKET_SZ: "SZ", MARKET_BJ: "BJ"}

BOARD_LABELS: Dict[str, str] = {
    "sh_main": "沪主板",
    "sh_star": "科创板",
    "sz_main": "深主板",
    "sz_gem": "创业板",
    "bse": "北交所",
    "sh_other": "沪其他",
    "sz_other": "深其他",
    "unknown": "未知",
}

# 分时级别 -> 麦蕊参数（本项目只用日线，其余列出便于查文档）
LEVEL_PARAM: Dict[str, str] = {"d": "d", "w": "w", "m": "m", "y": "y"}

# 技术指标：接口路径里的名字 -> 返回字段 -> 本项目落库列名
INDICATOR_MAP: Dict[str, Dict[str, str]] = {
    "macd": {"diff": "macd_diff", "dea": "macd_dea", "macd": "macd_bar"},
    "kdj": {"k": "kdj_k", "d": "kdj_d", "j": "kdj_j"},
    "boll": {"u": "boll_upper", "m": "boll_mid", "d": "boll_lower"},
    # 接口还会返回 ma3/ma15/ma30/ma120/ma200/ma250，这里只落常用的四条
    "ma": {"ma5": "ma5", "ma10": "ma10", "ma20": "ma20", "ma60": "ma60"},
}

INDICATOR_KINDS: Tuple[str, ...] = tuple(INDICATOR_MAP)


def normalize_indicator_kind(value: Any) -> str:
    key = str(value or "").strip().lower()
    if key not in INDICATOR_MAP:
        raise ValueError(f"不支持的技术指标：{value!r}（可选 {'/'.join(INDICATOR_KINDS)}）")
    return key


# 除权方式：本项目默认前复权；同时兼容老配置里的数字写法（0=不复权 1=前复权 2=后复权）
ADJUST_ALIASES: Dict[str, str] = {
    "n": "n",
    "f": "f",
    "b": "b",
    "fr": "fr",
    "br": "br",
    "0": "n",
    "1": "f",
    "2": "b",
    "none": "n",
    "no": "n",
    "qfq": "f",
    "hfq": "b",
    "": "f",
}

BAR_FIELDS = (
    "secid",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "amplitude",
    "pct_chg",
    "chg",
    "turnover",
    "float_mv",
    "volume_ratio",
    "source",
)


class MairuiError(RuntimeError):
    """接口请求失败（重试后仍未成功），或证书无效/未配置。"""


# --------------------------------------------------------------------------- 基础转换
def to_float(value: Any) -> Optional[float]:
    """把接口返回值转成 float；缺失值（None / '-' / ''）统一返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text in ("", "-", "--", "None", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def market_of_code(code: str, suffix: Optional[str] = None) -> int:
    """由麦蕊的代码后缀（SH/SZ/BJ）或代码前缀推断市场编号。"""
    if suffix:
        market = MARKET_OF_SUFFIX.get(str(suffix).strip().upper())
        if market is not None:
            return market
    code = str(code).strip()
    if code.startswith(("4", "8", "92")):
        return MARKET_BJ
    if code.startswith(("5", "6", "9")):
        return MARKET_SH
    if code.startswith(("0", "1", "2", "3")):
        return MARKET_SZ
    return MARKET_SH


def secid_of(code: Any, suffix: Optional[str] = None) -> Optional[str]:
    """麦蕊代码 -> 本项目 secid，例如 ``000001.SZ`` -> ``0.000001``。"""
    text = to_text(code)
    if text is None:
        return None
    code_part, _, tail = text.partition(".")
    market = market_of_code(code_part, tail or suffix)
    return f"{market}.{code_part}"


def dm_of(secid: Any) -> str:
    """本项目 secid -> 麦蕊代码，例如 ``0.000001`` -> ``000001.SZ``。"""
    market_part, _, code = str(secid).partition(".")
    if not code:
        raise ValueError(f"非法 secid：{secid!r}")
    market = int(to_float(market_part) or 0)
    suffix = SUFFIX_OF_MARKET.get(market) or SUFFIX_OF_MARKET[market_of_code(code)]
    return f"{code}.{suffix}"


def coerce_secid(value: Any) -> Optional[str]:
    """兼容两种写法：内部 secid（``1.600519``）与麦蕊代码（``600519.SH``）。"""
    text = to_text(value)
    if text is None:
        return None
    market_part, _, code = text.partition(".")
    if market_part in ("0", "1", "2") and len(code) == 6 and code.isdigit():
        return f"{market_part}.{code}"
    return secid_of(text)


def board_of(secid: Any) -> str:
    """由 secid 推断板块（1=沪市，0=深市，2=北交所）。"""
    try:
        market, code = str(secid).split(".", 1)
    except ValueError:
        return "unknown"
    code = code.strip()
    if market == str(MARKET_SH):
        if code.startswith(("688", "689")):
            return "sh_star"
        if code.startswith(("5", "6", "9")):
            return "sh_main"
        return "sh_other"
    if market == str(MARKET_BJ):
        return "bse"
    if market == str(MARKET_SZ):
        if code.startswith(("300", "301")):
            return "sz_gem"
        if code.startswith(("4", "8", "920")):
            return "bse"
        if code.startswith(("000", "001", "002", "003")):
            return "sz_main"
        return "sz_other"
    return "unknown"


def is_st_name(name: Any) -> bool:
    """名称里带 ST / *ST / 退 的按风险股处理。"""
    text = str(name or "").upper()
    return "ST" in text or "退" in text


def _as_items(payload: Any) -> List[Dict[str, Any]]:
    """接口返回可能是数组、也可能是单个对象；统一成 list[dict]。"""
    if payload is None:
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, (list, tuple)):
        return [item for item in payload if isinstance(item, dict)]
    return []


def error_message(payload: Any) -> Optional[str]:
    """识别 ``{"code":503,"message":"upstream unavailable"}`` 这类错误体。"""
    if isinstance(payload, dict):
        code = payload.get("code")
        message = payload.get("message") or payload.get("msg")
        if message is not None and code is not None:
            return f"{code}: {message}"
    return None


def normalize_date(value: Any) -> Optional[str]:
    """把 ``2026-10-13`` / ``2026-10-13 08:56:59`` / ``20261013`` 统一成 ``YYYY-MM-DD``。"""
    text = to_text(value)
    if text is None:
        return None
    head = text.replace("/", "-").split(" ")[0]
    digits = head.replace("-", "")
    if len(digits) >= 8 and digits[:8].isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def normalize_adjust(value: Any) -> str:
    """除权方式：支持 ``n/f/b/fr/br`` 与数字 ``0/1/2``。"""
    key = str(value if value is not None else "").strip().lower()
    return ADJUST_ALIASES.get(key, "f")


def normalize_level(value: Any) -> str:
    key = str(value if value is not None else "d").strip().lower()
    if key not in LEVEL_PARAM:
        raise ValueError(f"不支持的 K 线级别：{value!r}（本接口支持 d/w/m/y）")
    return LEVEL_PARAM[key]


def _date_param(value: Any) -> Optional[str]:
    """把 ``2026-10-01`` / ``20261001`` 转成接口要的 ``20261001``；``0``/空 -> None。"""
    text = to_text(value)
    if text is None or text in ("0", "00000000"):
        return None
    digits = text.replace("-", "").replace("/", "").split(" ")[0]
    if len(digits) < 8 or not digits[:8].isdigit():
        return None
    return digits[:8]


def _shares_and_turnover(
    volume: Optional[float], close: Optional[float], float_shares: Optional[float]
) -> Tuple[Optional[float], Optional[float]]:
    """由流通股本 + 成交量推算 (换手率 %, 流通市值元)；缺数据时返回 (None, None)。"""
    if not float_shares or float_shares <= 0:
        return None, None
    turnover = None
    if volume is not None:
        # 成交量单位是「手」，1 手 = 100 股
        turnover = volume * 100.0 / float_shares * 100.0
    float_mv = close * float_shares if close is not None else None
    return turnover, float_mv


# --------------------------------------------------------------------------- 解析
def parse_stock_list(payload: Any) -> List[Dict[str, Any]]:
    """解析《股票列表》``hslt/list``：``[{dm, mc, jys}]`` -> 本项目股票列表。"""
    rows: List[Dict[str, Any]] = []
    for item in _as_items(payload):
        code_text = to_text(item.get("dm"))
        if code_text is None:
            continue
        secid = secid_of(code_text, item.get("jys"))
        if secid is None:
            continue
        name = str(item.get("mc") or "").strip()
        rows.append(
            {
                "secid": secid,
                "code": secid.split(".", 1)[1],
                "name": name,
                "market": int(secid.split(".", 1)[0]),
                "board": board_of(secid),
                "is_st": int(is_st_name(name)),
                "source": "list",
            }
        )
    return rows


def parse_quote_payload(payload: Any, secid_hint: Optional[str] = None) -> List[Dict[str, Any]]:
    """解析实时交易数据（全市场 / 多股 / 单股三种形态都用它）。"""
    rows: List[Dict[str, Any]] = []
    for item in _as_items(payload):
        secid = secid_of(item.get("dm"))
        if secid is None and secid_hint is not None:
            secid = coerce_secid(secid_hint)
        if secid is None:
            continue
        close = to_float(item.get("p"))
        prev_close = to_float(item.get("yc"))
        float_mv = to_float(item.get("lt"))
        # 盘前 p=0，用昨收估流通股本，避免除以 0
        basis = close if close else prev_close
        float_shares = float_mv / basis if (float_mv is not None and basis) else None
        volume = to_float(item.get("v"))
        turnover = to_float(item.get("hs"))
        if turnover is None:
            turnover = to_float(item.get("tr"))  # ssjy_more 的换手率字段名叫 tr
        rows.append(
            {
                "secid": secid,
                "code": secid.split(".", 1)[1],
                "board": board_of(secid),
                "name": to_text(item.get("mc")),
                "market": int(secid.split(".", 1)[0]),
                "date": None,  # 由 spot_trade_date() 统一填充
                "open": to_float(item.get("o")),
                "high": to_float(item.get("h")),
                "low": to_float(item.get("l")),
                "close": close,
                "prev_close": prev_close,
                "volume": volume,  # 手
                "amount": to_float(item.get("cje")),  # 元
                "amplitude": to_float(item.get("zf")),  # %
                "pct_chg": to_float(item.get("pc")),  # %
                "chg": to_float(item.get("ud")),  # 元
                "turnover": turnover,  # %
                "volume_ratio": to_float(item.get("lb")),  # 量比（快照口径，仅当日有）
                "total_mv": to_float(item.get("sz")),  # 元
                "float_mv": float_mv,  # 元
                "float_shares": float_shares,  # 股
                "ts": to_text(item.get("t")),  # "2026-10-13 15:00:03"
                "source": "spot",
            }
        )
    return rows


def align_quotes_by_position(payload: Any, secids: Sequence[str]) -> List[Dict[str, Any]]:
    """返回体里没有 ``dm`` 时，按"请求顺序"把报价对齐到 secid。

    ``hsrl/ssjy_more`` 的文档字段表里并没有 ``dm``（只列了 p/o/h/l/cje/v/…），
    实测有的证书会带、有的不带。不带时只能按顺序对齐（接口按请求顺序返回），
    数量对不上就放弃（让调用方退回到逐只抓，宁慢不错）。
    """
    items = _as_items(payload)
    if not items:
        return []
    if any(to_text(item.get("dm")) for item in items):
        return parse_quote_payload(items)
    if len(items) != len(secids):
        log.warning(
            "多股报价缺少 dm 且数量对不上（%s vs %s），退回逐只抓取", len(items), len(secids)
        )
        return []
    rows: List[Dict[str, Any]] = []
    for item, secid in zip(items, secids):
        rows.extend(parse_quote_payload(item, secid_hint=secid))
    return rows


def parse_kline_payload(
    payload: Any,
    secid: str,
    float_shares: Optional[float] = None,
    skip_suspended: bool = True,
) -> List[Dict[str, Any]]:
    """解析《历史分时交易》日线。

    麦蕊日线只给 前收 / 开 / 高 / 低 / 收 / 量 / 额 / 停牌标记，
    涨跌幅、涨跌额、振幅、换手率、流通市值都由本地推算（口径见 docs/mairui_api.md）。
    """
    rows: List[Dict[str, Any]] = []
    for item in _as_items(payload):
        date = normalize_date(item.get("t"))
        if date is None:
            continue
        if skip_suspended and (to_float(item.get("sf")) or 0.0) >= 1:
            continue  # 停牌日不落库，避免污染量能基准
        open_ = to_float(item.get("o"))
        high = to_float(item.get("h"))
        low = to_float(item.get("l"))
        close = to_float(item.get("c"))
        prev_close = to_float(item.get("pc"))
        volume = to_float(item.get("v"))

        pct_chg = chg = amplitude = None
        if close is not None and prev_close:
            chg = close - prev_close
            pct_chg = chg / prev_close * 100.0
            if high is not None and low is not None:
                amplitude = (high - low) / prev_close * 100.0
        turnover, float_mv = _shares_and_turnover(volume, close, float_shares)

        rows.append(
            {
                "secid": secid,
                "date": date,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": to_float(item.get("a")),
                "amplitude": amplitude,
                "pct_chg": pct_chg,
                "chg": chg,
                "turnover": turnover,
                "float_mv": float_mv,
                "volume_ratio": None,
                "source": "kline",
            }
        )
    return rows


def parse_trade_calendar(payload: Any) -> List[str]:
    """解析《交易日历》：``["20260105", ...]`` -> ``["2026-01-05", ...]``（升序去重）。"""
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("list") or []
    out: List[str] = []
    for item in payload or []:
        if isinstance(item, dict):
            item = item.get("date") or item.get("t") or item.get("dm")
        date = normalize_date(item)
        if date:
            out.append(date)
    return sorted(set(out))


def parse_indicator_payload(payload: Any, kind: str, secid: str) -> List[Dict[str, Any]]:
    """解析 MACD / KDJ / BOLL / MA 日线指标为 ``[{secid, date, <列名>: 值}]``。

    接口返回字段名与落库列名的对应关系见 ``INDICATOR_MAP``：

    * macd: ``diff/dea/macd``          -> ``macd_diff/macd_dea/macd_bar``
    * kdj:  ``k/d/j``                  -> ``kdj_k/kdj_d/kdj_j``
    * boll: ``u/m/d``（上/中/下轨）      -> ``boll_upper/boll_mid/boll_lower``
    * ma:   ``ma5/ma10/ma20/ma60``      -> 同名
    """
    mapping = INDICATOR_MAP[normalize_indicator_kind(kind)]
    rows: List[Dict[str, Any]] = []
    for item in _as_items(payload):
        date = normalize_date(item.get("t"))
        if date is None:
            continue
        row: Dict[str, Any] = {"secid": secid, "date": date, "source": "indicator"}
        for source_key, column in mapping.items():
            row[column] = to_float(item.get(source_key))
        rows.append(row)
    return rows


def parse_instrument(payload: Any) -> Optional[Dict[str, Any]]:
    """解析《股票基础信息》：流通股本 fv、总股本 tv、上市日期 od、停牌状态 is。"""
    items = _as_items(payload)
    if not items:
        return None
    item = items[0]
    code_text = to_text(item.get("ii")) or to_text(item.get("dm"))
    secid = secid_of(code_text, item.get("ei")) if code_text else None
    return {
        "secid": secid,
        "name": to_text(item.get("name")) or to_text(item.get("mc")),
        "listed_date": normalize_date(item.get("od")),
        "prev_close": to_float(item.get("pc")),
        "float_shares": to_float(item.get("fv")),
        "total_shares": to_float(item.get("tv")),
        "suspended": to_float(item.get("is")),
    }


def spot_trade_date(rows: Sequence[Dict[str, Any]], tz: str = "Asia/Shanghai") -> Optional[str]:
    """从快照的更新时间 ``t`` 推断行情所属交易日（取全市场最大值）。

    兼容纯数字时间戳（历史东财口径）与字符串时间（麦蕊 ``t`` 字段）。
    """
    dates: List[str] = []
    for row in rows:
        value = row.get("ts")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            dates.append(_timestamp_date(float(value), tz))
        else:
            date = normalize_date(value)
            if date:
                dates.append(date)
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def _timestamp_date(stamp: float, tz: str = "Asia/Shanghai") -> Optional[str]:
    import datetime as _dt

    try:
        from zoneinfo import ZoneInfo

        moment = _dt.datetime.fromtimestamp(stamp, ZoneInfo(tz))
    except Exception:  # pragma: no cover - 极端环境下退回本地时区
        moment = _dt.datetime.fromtimestamp(stamp)
    return moment.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- 客户端
class RateLimiter:
    """跨线程共享的最小请求间隔限流器（麦蕊按证书限制"每分钟多少次"）。"""

    def __init__(self, per_minute: float = 0.0):
        self.min_interval = 60.0 / per_minute if per_minute and per_minute > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self.min_interval


def _auto_workers(workers: Any, rate_limit_per_min: float) -> int:
    """并发线程数：填 0（或负数）= 按限频自动推算。

    单个请求大约 0.2~0.5 秒，所以"每分钟 N 次"需要的并发 ≈ N/60 × 0.3 ≈ N/200，
    再留点余量。基础档 300 → 4 个线程就够（多了也只会空转抢 GIL），包年 3000 → 24。
    """
    try:
        value = int(workers or 0)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    rate = float(rate_limit_per_min or 300)
    return int(min(32, max(4, round(rate / 125))))


class MairuiClient:
    """带重试 / 限流 / 并发的麦蕊智数客户端。"""

    def __init__(
        self,
        licence: str = "",
        *,
        workers: int = 0,
        timeout: float = 15.0,
        retries: int = 3,
        sleep: float = 0.05,
        rate_limit_per_min: float = 300.0,
        base_url: str = BASE_URL,
        level: str = "d",
        adjust: Any = "f",
        spot_mode: str = "auto",
        multi_batch: int = 20,
        session: Optional[requests.Session] = None,
    ):
        self.licence = str(licence or "").strip()
        self.rate_limit_per_min = float(rate_limit_per_min or 0)
        self.workers = _auto_workers(workers, self.rate_limit_per_min)
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.sleep = max(0.0, float(sleep))
        self.base_url = str(base_url or BASE_URL).rstrip("/")
        self.level = normalize_level(level)
        self.adjust = normalize_adjust(adjust)
        self.spot_mode = str(spot_mode or "auto").strip().lower()
        self.multi_batch = max(1, min(int(multi_batch), 20))
        self.limiter = RateLimiter(self.rate_limit_per_min)
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.request_count = 0

    @classmethod
    def from_config(cls, cfg) -> "MairuiClient":
        return cls(
            licence=cfg.get("mairui.licence", ""),
            workers=cfg.get("mairui.workers", 0),
            timeout=cfg.get("mairui.timeout", 15.0),
            retries=cfg.get("mairui.retries", 3),
            sleep=cfg.get("mairui.sleep", 0.05),
            rate_limit_per_min=cfg.get("mairui.rate_limit_per_min", 300),
            base_url=cfg.get("mairui.base_url", BASE_URL),
            level=cfg.get("mairui.kline_level", "d"),
            adjust=cfg.get("mairui.kline_adjust", "f"),
            spot_mode=cfg.get("mairui.spot_mode", "auto"),
            multi_batch=cfg.get("mairui.multi_batch", 20),
        )

    # ------------------------------------------------------------------ 底层
    def _require_licence(self) -> str:
        if not self.licence:
            raise MairuiError(
                "未配置麦蕊智数证书（licence）：请在 config.yaml 里填写 mairui.licence，"
                "或设置环境变量 MAIRUI_LICENCE。证书可在 https://www.mairui.club 领取。"
            )
        return self.licence

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET 一个 JSON 接口；失败按指数退避重试。"""
        last_error = "unknown"
        for attempt in range(self.retries + 1):
            self.limiter.acquire()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                self.request_count += 1
                try:
                    payload: Any = resp.json()
                except ValueError:
                    payload = None
                if payload is None:
                    last_error = f"HTTP {resp.status_code}（响应不是 JSON）"
                else:
                    problem = error_message(payload)
                    if problem:
                        last_error = f"接口报错 {problem}"
                    elif resp.status_code == 200:
                        if self.sleep:
                            time.sleep(self.sleep * random.random())
                        return payload
                    else:
                        last_error = f"HTTP {resp.status_code}"
            except Exception as exc:  # 网络异常、超时、TLS 失败等
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.retries:
                backoff = min(0.5 * (2**attempt), 5.0) + random.random() * 0.2
                log.debug("请求失败(%s)，%.1fs 后重试：%s", last_error, backoff, url)
                time.sleep(backoff)
        raise MairuiError(f"请求 {url} 失败：{last_error}")

    # ------------------------------------------------------------- 股票列表
    def fetch_universe(self) -> List[Dict[str, Any]]:
        """拉取《股票列表》（沪深 A 股代码 + 名称）。"""
        licence = self._require_licence()
        payload = self._get_json(self._url(PATH_STOCK_LIST.format(licence=licence)))
        rows = parse_stock_list(payload)
        log.debug("股票列表 %s 只", len(rows))
        return rows

    # ------------------------------------------------------------- 交易日历
    def fetch_trade_calendar(self, year: Union[int, str]) -> List[str]:
        """拉取指定年份的交易日列表（``YYYY-MM-DD``，升序）。"""
        licence = self._require_licence()
        payload = self._get_json(self._url(PATH_CALENDAR.format(year=int(year), licence=licence)))
        return parse_trade_calendar(payload)

    def fetch_trade_calendar_span(self, years: Iterable[Union[int, str]]) -> List[str]:
        days: List[str] = []
        for year in years:
            try:
                days.extend(self.fetch_trade_calendar(year))
            except MairuiError as exc:  # 未公布节假日的未来年份可能 404
                log.warning("交易日历 %s 年获取失败：%s", year, exc)
        return sorted(set(days))

    # --------------------------------------------------------- 实时行情快照
    def fetch_quote_all(self) -> List[Dict[str, Any]]:
        """全市场实时行情（``hsrl/real/all``，一次请求拿到所有股票）。"""
        licence = self._require_licence()
        payload = self._get_json(self._url(PATH_QUOTE_ALL.format(licence=licence)))
        return parse_quote_payload(payload)

    def fetch_quote_multi(self, secids: Sequence[str]) -> List[Dict[str, Any]]:
        """多股实时行情（``hsrl/ssjy_more``，一次最多 20 只）—— **非 Pro 档也能用**。

        返回体带 ``dm`` 就按代码对齐；不带（文档字段表里确实没有 dm）就按请求顺序对齐。
        """
        licence = self._require_licence()
        rows: List[Dict[str, Any]] = []
        items = list(secids)
        for start in range(0, len(items), self.multi_batch):
            batch = items[start : start + self.multi_batch]
            codes = [str(secid).split(".", 1)[1] for secid in batch]
            payload = self._get_json(
                self._url(PATH_QUOTE_MORE.format(licence=licence)),
                {"stock_codes": ",".join(codes)},
            )
            rows.extend(align_quotes_by_position(payload, batch))
        return rows

    def fetch_quote_one(self, secid: str) -> List[Dict[str, Any]]:
        """单只股票实时行情（``hsrl/ssjy``）。"""
        licence = self._require_licence()
        code = str(secid).split(".", 1)[1]
        payload = self._get_json(self._url(PATH_QUOTE_ONE.format(code=code, licence=licence)))
        return parse_quote_payload(payload, secid_hint=secid)

    def fetch_spot(
        self,
        secids: Optional[Sequence[str]] = None,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """拉当日快照。**默认不碰 Pro 专属的全市场接口。**

        取数方式（``mairui.spot_mode`` 可强制指定，默认 ``auto``）：

        * ``auto``   —— 先多股（``hsrl/ssjy_more``，一次 20 只，约 260 次请求拿全市场），
          拿不到再逐只（``hsrl/ssjy``）；**不试全市场接口**（那个要包年/钻石/
          Quant Pro 档位，没权限会 429，白等一轮）。
        * ``more`` / ``single`` —— 只走其中一种。
        * ``all``    —— ``hsrl/real/all``，一次拿全市场（最省，但**必须**证书含
          "全市场"权限，且每分钟只能 1 次）；只有你确认有这个权限时才用。
        """
        chosen = (mode or self.spot_mode or "auto").lower()
        if chosen == "all":
            return self.fetch_quote_all()

        if not secids:
            # 没给股票池（例如只想探活）：能试的只有全市场接口
            try:
                return self.fetch_quote_all()
            except MairuiError as exc:
                log.warning("全市场快照接口不可用（%s）；没给股票池时无法降级", exc)
                return []

        if chosen in ("auto", "more"):
            try:
                rows = self.fetch_quote_multi(secids)
                if rows:
                    return rows
                log.warning("多股快照接口返回空数据，改逐只抓取")
            except MairuiError as exc:
                if chosen == "more":
                    raise
                log.warning("多股快照接口不可用（%s），改逐只抓取", exc)

        return self._fetch_quote_each(secids)

    def _fetch_quote_each(self, secids: Sequence[str]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.fetch_quote_one, secid): secid for secid in secids}
            for future in as_completed(futures):
                secid = futures[future]
                try:
                    rows.extend(future.result())
                except Exception as exc:
                    log.warning("抓取 %s 实时行情失败：%s", secid, exc)
        return rows

    # ------------------------------------------------------------------ 日线
    def fetch_kline(
        self,
        secid: str,
        beg: str = "0",
        end: Optional[str] = None,
        level: Optional[str] = None,
        adjust: Optional[Any] = None,
        float_shares: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """单只股票历史日线（``beg``/``end`` 用 YYYYMMDD 或 YYYY-MM-DD，``0`` 表示不限）。"""
        licence = self._require_licence()
        params: Dict[str, Any] = {}
        start = _date_param(beg)
        stop = _date_param(end)
        if start:
            params["st"] = start
        if stop:
            params["et"] = stop
        if limit:
            params["lt"] = int(limit)
        payload = self._get_json(
            self._url(
                PATH_KLINE.format(
                    dm=dm_of(secid),
                    level=normalize_level(level or self.level),
                    adjust=normalize_adjust(self.adjust if adjust is None else adjust),
                    licence=licence,
                )
            ),
            params or None,
        )
        return parse_kline_payload(payload, secid, float_shares=float_shares)

    def fetch_kline_many(
        self,
        secids: Sequence[str],
        beg: str = "0",
        end: Optional[str] = None,
        level: Optional[str] = None,
        adjust: Optional[Any] = None,
        float_shares: Optional[Union[Mapping[str, float], float]] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Tuple[str, str]]]:
        """并发拉取多只股票的日线，返回 ``({secid: rows}, [(secid, error)])``。

        ``float_shares`` 可传"流通股本"映射（或统一值），用来在本地推算历史
        换手率与流通市值（麦蕊日线接口不直接给这两个字段）。
        """
        results: Dict[str, List[Dict[str, Any]]] = {}
        errors: List[Tuple[str, str]] = []
        total = len(secids)
        done = 0
        shares_map: Mapping[str, float] = float_shares if isinstance(float_shares, Mapping) else {}
        uniform = None if isinstance(float_shares, Mapping) else float_shares
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(
                    self.fetch_kline,
                    secid,
                    beg,
                    end,
                    level,
                    adjust,
                    shares_map.get(secid, uniform),
                ): secid
                for secid in secids
            }
            for future in as_completed(futures):
                secid = futures[future]
                done += 1
                if progress:
                    progress(done, total)
                try:
                    results[secid] = future.result()
                except Exception as exc:
                    errors.append((secid, f"{type(exc).__name__}: {exc}"))
                    log.warning("拉取 %s 日线失败：%s", secid, exc)
                if self.sleep:
                    time.sleep(self.sleep * random.random())
        return results, errors

    # ------------------------------------------------------------ 技术指标
    def fetch_indicator(
        self,
        secid: str,
        kind: str,
        beg: str = "0",
        end: Optional[str] = None,
        level: Optional[str] = None,
        adjust: Optional[Any] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """单只股票某一种日频指标（MACD / KDJ / BOLL / MA）。"""
        licence = self._require_licence()
        indicator = normalize_indicator_kind(kind)
        params: Dict[str, Any] = {}
        start = _date_param(beg)
        stop = _date_param(end)
        if start:
            params["st"] = start
        if stop:
            params["et"] = stop
        if limit:
            params["lt"] = int(limit)
        payload = self._get_json(
            self._url(
                PATH_INDICATOR.format(
                    kind=indicator,
                    dm=dm_of(secid),
                    level=normalize_level(level or self.level),
                    adjust=normalize_adjust(self.adjust if adjust is None else adjust),
                    licence=licence,
                )
            ),
            params or None,
        )
        return parse_indicator_payload(payload, indicator, secid)

    def fetch_indicators_many(
        self,
        secids: Sequence[str],
        kinds: Sequence[str] = ("macd", "kdj", "boll"),
        beg: str = "0",
        end: Optional[str] = None,
        level: Optional[str] = None,
        adjust: Optional[Any] = None,
        limit: Optional[int] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Tuple[str, str]]]:
        """并发拉取多只股票的多种指标。

        返回 ``({指标名: 行列表}, [(secid/指标, 错误)])``；每只股票 × 每种指标 = 1 次请求。
        """
        normalized = [normalize_indicator_kind(kind) for kind in kinds]
        jobs = [(secid, kind) for secid in secids for kind in normalized]
        by_kind: Dict[str, List[Dict[str, Any]]] = {kind: [] for kind in normalized}
        errors: List[Tuple[str, str]] = []
        total = len(jobs)
        done = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self.fetch_indicator, secid, kind, beg, end, level, adjust, limit): (
                    secid,
                    kind,
                )
                for secid, kind in jobs
            }
            for future in as_completed(futures):
                secid, kind = futures[future]
                done += 1
                if progress:
                    progress(done, total)
                try:
                    by_kind[kind].extend(future.result())
                except Exception as exc:
                    errors.append((f"{secid}/{kind}", f"{type(exc).__name__}: {exc}"))
        return by_kind, errors

    # --------------------------------------------------------- 股票基础信息
    def fetch_instrument(self, secid: str) -> Optional[Dict[str, Any]]:
        """单只股票基础信息（含流通股本 fv、上市日期 od）。"""
        licence = self._require_licence()
        payload = self._get_json(
            self._url(PATH_INSTRUMENT.format(dm=dm_of(secid), licence=licence))
        )
        info = parse_instrument(payload)
        if info and not info.get("secid"):
            info["secid"] = secid
        return info

    def fetch_instrument_many(
        self,
        secids: Sequence[str],
        progress: Optional[Callable[[int, int], None]] = None,
        max_errors: int = 50,
    ) -> Tuple[Dict[str, Dict[str, Any]], List[Tuple[str, str]]]:
        """并发拉取流通股本 / 上市日期；单只失败不影响整批。"""
        results: Dict[str, Dict[str, Any]] = {}
        errors: List[Tuple[str, str]] = []
        total = len(secids)
        done = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.fetch_instrument, secid): secid for secid in secids}
            for future in as_completed(futures):
                secid = futures[future]
                done += 1
                if progress:
                    progress(done, total)
                try:
                    info = future.result()
                except Exception as exc:
                    if len(errors) < max_errors:
                        errors.append((secid, f"{type(exc).__name__}: {exc}"))
                    continue
                if info:
                    results[secid] = info
        return results, errors

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover
            pass
