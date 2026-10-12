"""东方财富免费行情接口客户端。

用到的两个公开接口（无需 token，浏览器直接可访问）：

1) 全市场快照（含当日成交量/额/换手率/量比）
   https://push2.eastmoney.com/api/qt/clist/get

2) 个股日K线（历史成交明细，用于回补与基准计算）
   https://push2his.eastmoney.com/api/qt/stock/kline/get

字段含义见 ``docs/eastmoney_api.md``。这里把所有解析逻辑都写成纯函数，
这样即使没有网络，也能用固定报文做单元测试（见 tests/test_eastmoney_parse.py）。
"""

from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import requests

log = logging.getLogger(__name__)

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"

# 东财公开页面里使用的固定 token，不是用户密钥
UT_KLINE = "fa5fd1943c7b386f172d6893dbfba10b"
UT_CLIST = "bd1d9ddb04089700cf9c27f6f7426281"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/center/gridlist.html",
    "Accept": "*/*",
}

# 板块 -> 行情列表的 fs 过滤串
BOARD_FS: Dict[str, str] = {
    "sh_main": "m:1+t:2",  # 沪市主板
    "sh_star": "m:1+t:23",  # 科创板
    "sz_main": "m:0+t:6",  # 深市主板
    "sz_gem": "m:0+t:80",  # 创业板
    "bse": "m:0+t:81+s:2048",  # 北交所
}

BOARD_LABELS: Dict[str, str] = {
    "sh_main": "沪主板",
    "sh_star": "科创板",
    "sz_main": "深主板",
    "sz_gem": "创业板",
    "bse": "北交所",
}

SPOT_FIELDS = "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13,f14,f15,f16,f17,f18,f20,f21,f26,f124"
KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

# 日K线的字段顺序（f51..f61）
KLINE_COLUMNS = [
    "date",
    "open",
    "close",
    "high",
    "low",
    "volume",  # 成交量，单位：手
    "amount",  # 成交额，单位：元
    "amplitude",  # 振幅 %
    "pct_chg",  # 涨跌幅 %
    "chg",  # 涨跌额
    "turnover",  # 换手率 %
]

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


class EastMoneyError(RuntimeError):
    """接口请求失败（重试后仍未成功）。"""


def to_float(value: Any) -> Optional[float]:
    """东财会把缺失值写成 '-'，这里统一转成 float 或 None。"""
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


def secid_of(market: Any, code: Any) -> Optional[str]:
    """拼出东财的 secid，如 1.600519（沪）/ 0.000001（深）。"""
    m = to_float(market)
    if m is None or code is None:
        return None
    return f"{int(m)}.{str(code).strip()}"


# --------------------------------------------------------------------------- 解析
def parse_clist_page(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """解析行情列表接口的一页数据。

    ``np=1`` 时 diff 是 {"0": {...}, "1": {...}} 形式，``np=0`` 时是 list，
    两种都要兼容。
    """
    data = (payload or {}).get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        try:
            items = [item for _, item in sorted(diff.items(), key=lambda kv: int(kv[0]))]
        except (TypeError, ValueError):
            items = list(diff.values())
    else:
        items = list(diff)

    rows: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("f12")
        secid = secid_of(item.get("f13"), code)
        if secid is None:
            continue
        rows.append(
            {
                "secid": secid,
                "code": str(code),
                "name": str(item.get("f14") or "").strip(),
                "market": int(to_float(item.get("f13")) or 0),
                "date": None,  # 由 spot_trade_date() 统一填充
                "open": to_float(item.get("f17")),
                "high": to_float(item.get("f15")),
                "low": to_float(item.get("f16")),
                "close": to_float(item.get("f2")),
                "prev_close": to_float(item.get("f18")),
                "volume": to_float(item.get("f5")),  # 手
                "amount": to_float(item.get("f6")),  # 元
                "amplitude": to_float(item.get("f7")),  # %
                "pct_chg": to_float(item.get("f3")),  # %
                "chg": to_float(item.get("f4")),
                "turnover": to_float(item.get("f8")),  # %
                "volume_ratio": to_float(item.get("f10")),  # 量比
                "total_mv": to_float(item.get("f20")),
                "float_mv": to_float(item.get("f21")),
                "ts": to_float(item.get("f124")),  # 行情时间戳（秒）
                "source": "spot",
            }
        )
    return rows


def parse_kline_payload(payload: Optional[Dict[str, Any]], secid: str) -> Dict[str, Any]:
    """解析个股日K线接口，返回 {secid, name, code, market, rows}。"""
    data = (payload or {}).get("data") or {}
    klines = data.get("klines") or []
    rows: List[Dict[str, Any]] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < len(KLINE_COLUMNS):
            continue
        rec = dict(zip(KLINE_COLUMNS, parts))
        date = str(rec["date"]).strip()
        if not date:
            continue
        rows.append(
            {
                "secid": secid,
                "date": date,
                "open": to_float(rec["open"]),
                "high": to_float(rec["high"]),
                "low": to_float(rec["low"]),
                "close": to_float(rec["close"]),
                "volume": to_float(rec["volume"]),
                "amount": to_float(rec["amount"]),
                "amplitude": to_float(rec["amplitude"]),
                "pct_chg": to_float(rec["pct_chg"]),
                "chg": to_float(rec["chg"]),
                "turnover": to_float(rec["turnover"]),
                "float_mv": None,
                "volume_ratio": None,
                "source": "kline",
            }
        )
    return {
        "secid": secid,
        "name": (data.get("name") or "").strip() if data.get("name") else None,
        "code": (str(data.get("code")) if data.get("code") is not None else None),
        "market": (
            int(to_float(data.get("market")) or 0) if data.get("market") is not None else None
        ),
        "rows": rows,
    }


def spot_trade_date(rows: Sequence[Dict[str, Any]], tz: str = "Asia/Shanghai") -> Optional[str]:
    """从快照时间戳里推断行情所属交易日（取全市场最大值）。"""
    stamps = [r.get("ts") for r in rows if r.get("ts")]
    if not stamps:
        return None
    import datetime as _dt

    try:
        from zoneinfo import ZoneInfo

        moment = _dt.datetime.fromtimestamp(max(stamps), ZoneInfo(tz))
    except Exception:  # pragma: no cover - 极端环境下退回本地时区
        moment = _dt.datetime.fromtimestamp(max(stamps))
    return moment.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- 客户端
class EastMoneyClient:
    """带重试/限速/并发的东财客户端。"""

    def __init__(
        self,
        workers: int = 8,
        timeout: float = 15.0,
        retries: int = 3,
        sleep: float = 0.05,
        page_size: int = 100,
        fqt: int = 1,
        session: Optional[requests.Session] = None,
    ):
        self.workers = max(1, int(workers))
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.sleep = max(0.0, float(sleep))
        self.page_size = int(page_size)
        self.fqt = int(fqt)
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.request_count = 0

    @classmethod
    def from_config(cls, cfg) -> "EastMoneyClient":
        return cls(
            workers=cfg.get("eastmoney.workers", 8),
            timeout=cfg.get("eastmoney.timeout", 15.0),
            retries=cfg.get("eastmoney.retries", 3),
            sleep=cfg.get("eastmoney.sleep", 0.05),
            page_size=cfg.get("eastmoney.spot_page_size", 100),
            fqt=cfg.get("eastmoney.kline_fqt", 1),
        )

    # ------------------------------------------------------------------ 底层
    def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        last_error = "unknown"
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                self.request_count += 1
                if resp.status_code == 200:
                    payload = resp.json()
                    if isinstance(payload, dict):
                        return payload
                    last_error = f"响应不是 JSON 对象: {type(payload).__name__}"
                else:
                    last_error = f"HTTP {resp.status_code}"
            except Exception as exc:  # 网络异常、JSON 解析失败等
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.retries:
                backoff = min(0.5 * (2**attempt), 5.0) + random.random() * 0.2
                log.debug("请求失败(%s)，%.1fs 后重试: %s", last_error, backoff, url)
                time.sleep(backoff)
        raise EastMoneyError(f"请求 {url} 失败：{last_error}")

    # ------------------------------------------------------- 全市场快照/股票列表
    def fetch_spot(
        self,
        boards: Sequence[str],
        page_size: Optional[int] = None,
        max_pages: int = 200,
    ) -> List[Dict[str, Any]]:
        """拉全市场当日快照（同时用作股票列表）。"""
        fs = ",".join(BOARD_FS[b] for b in boards if b in BOARD_FS)
        if not fs:
            raise ValueError(f"没有可用的板块配置：{list(boards)}")
        size = int(page_size or self.page_size)
        rows: List[Dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            payload = self._get_json(
                CLIST_URL,
                {
                    "pn": page,
                    "pz": size,
                    "po": 0,  # 按代码升序，保证分页稳定
                    "np": 1,
                    "ut": UT_CLIST,
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f12",
                    "fs": fs,
                    "fields": SPOT_FIELDS,
                },
            )
            batch = parse_clist_page(payload)
            total = to_float(((payload or {}).get("data") or {}).get("total"))
            if not batch:
                break
            rows.extend(batch)
            log.debug("快照分页 page=%s 累计 %s 条", page, len(rows))
            if total is not None and len(rows) >= int(total):
                break
            if len(batch) < size:
                break
            page += 1
            if self.sleep:
                time.sleep(self.sleep)
        return rows

    # ------------------------------------------------------------------ 日K线
    def fetch_kline(
        self,
        secid: str,
        beg: str = "0",
        end: str = "20500101",
        klt: int = 101,
        fqt: Optional[int] = None,
    ) -> Dict[str, Any]:
        """拉单只股票的日K线（klt=101 日线；fqt=1 前复权）。"""
        payload = self._get_json(
            KLINE_URL,
            {
                "secid": secid,
                "ut": UT_KLINE,
                "fields1": KLINE_FIELDS1,
                "fields2": KLINE_FIELDS2,
                "klt": int(klt),
                "fqt": int(self.fqt if fqt is None else fqt),
                "beg": beg,
                "end": end,
                "lmt": 1000000,
            },
        )
        return parse_kline_payload(payload, secid)

    def fetch_kline_many(
        self,
        secids: Sequence[str],
        beg: str = "0",
        end: str = "20500101",
        klt: int = 101,
        fqt: Optional[int] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[Dict[str, Dict[str, Any]], List[Tuple[str, str]]]:
        """并发拉取多只股票的日K线。

        返回 ``(结果字典, 失败列表)``；单只失败不影响整批。
        """
        results: Dict[str, Dict[str, Any]] = {}
        errors: List[Tuple[str, str]] = []
        total = len(secids)
        done = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self.fetch_kline, secid, beg, end, klt, fqt): secid for secid in secids
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
                    log.warning("拉取 %s 日K线失败：%s", secid, exc)
                if self.sleep:
                    time.sleep(self.sleep * random.random())
        return results, errors

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover
            pass
