"""进程内麦蕊用量。输出固定端点名称，不保存带证书的 URL、参数或响应正文。"""

import atexit
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
import sys
import threading
from urllib.parse import urlsplit

from requests.adapters import HTTPAdapter


_ROUTES = {
    ("hslt", "list"): "hslt.list",
    ("hsrl", "real", "all"): "hsrl.real.all",
    ("hsrl", "ssjy_more"): "hsrl.ssjy_more",
    ("hsrl", "ssjy"): "hsrl.ssjy",
    ("hsrl", "zbjy"): "hsrl.zbjy",
    ("hsstock", "history"): "hsstock.history.kline",
    ("hsstock", "real", "five"): "hsstock.real.five",
    ("hsstock", "latest"): "hsstock.latest",
    ("hsstock", "instrument"): "hsstock.instrument",
    ("tcalendar", "list"): "tcalendar.list",
    ("hszg", "list"): "hszg.list",
    ("hszg", "gg"): "hszg.gg",
    ("hszg", "zg"): "hszg.zg",
    ("hsindex", "history"): "hsindex.history",
    ("hsindex", "latest"): "hsindex.latest",
}
_FIELDS = (
    "logical_calls",
    "http_attempts",
    "http_responses",
    "http_2xx",
    "http_3xx",
    "http_4xx",
    "http_5xx",
    "transport_errors",
    "retry_attempts",
    "redirect_hops",
    "json_successes",
    "external_gets",
    "circuit_skips",
)
_started_at = datetime.now(timezone.utc).isoformat()
_lock = threading.Lock()
_counts = defaultdict(Counter)
_local = threading.local()


def endpoint(url, base_url):
    path = url[len(base_url) :] if url.startswith(base_url + "/") else urlsplit(url).path
    parts = tuple(urlsplit(path).path.strip("/").split("/"))
    if parts[:2] == ("hsstock", "history") and parts[2:3] in (
        ("macd",),
        ("kdj",),
        ("boll",),
        ("ma",),
    ):
        return "hsstock.history." + parts[2]
    return _ROUTES.get(parts[:3], _ROUTES.get(parts[:2], "other"))


def count(route, *fields):
    with _lock:
        _counts[route].update(fields)


def snapshot():
    with _lock:
        endpoints = {
            route: {field: counter[field] for field in _FIELDS}
            for route, counter in sorted(_counts.items())
        }
    return {
        "pid": os.getpid(),
        "started_at": _started_at,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "billing": "provider_units_unknown",
        "totals": {field: sum(data[field] for data in endpoints.values()) for field in _FIELDS},
        "endpoints": endpoints,
    }


@contextmanager
def call(route, retry=False):
    before = getattr(_local, "call", None)
    _local.call = {"route": route, "retry": retry, "sends": 0}
    try:
        yield
    finally:
        _local.call = before


class _SensitiveTransport(logging.Filter):
    def filter(self, record):
        # urllib3 在 DEBUG 下会打印完整请求路径，其中最后一段就是麦蕊证书。
        return getattr(_local, "call", None) is None


for _name in ("urllib3.connectionpool", "urllib3.util.retry", "urllib3.connection"):
    logging.getLogger(_name).addFilter(_SensitiveTransport())


class MeteredAdapter(HTTPAdapter):
    def __init__(self):
        super().__init__(max_retries=0)  # 所有重试由 MairuiClient 管理，一次 send 对应一次尝试。

    def send(self, request, **kwargs):
        current = getattr(_local, "call", None)
        if current is None:
            return super().send(request, **kwargs)
        route = current["route"]
        fields = ["http_attempts"]
        if current["sends"]:
            fields.append("redirect_hops")
        elif current["retry"]:
            fields.append("retry_attempts")
        current["sends"] += 1
        count(route, *fields)
        try:
            response = super().send(request, **kwargs)
        except Exception:
            count(route, "transport_errors")
            raise
        status_class = f"http_{response.status_code // 100}xx"
        count(route, "http_responses", *([status_class] if status_class in _FIELDS else []))
        return response


def log_summary():
    record = snapshot()
    if not record["endpoints"]:
        return
    message = "mairui_usage " + json.dumps(record, separators=(",", ":"), sort_keys=True)
    logger = logging.getLogger("emscan")
    if logger.isEnabledFor(logging.INFO) and logger.hasHandlers():
        logger.info("%s", message)
    else:
        # 独立模块入口（如 benchmark/history）也产生一行，可在 systemd journal 查询。
        print(message, file=sys.stderr)


atexit.register(log_summary)
