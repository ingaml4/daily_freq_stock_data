"""统一时区：本项目所有"今天 / 现在"都按东八区（Asia/Shanghai）算。

服务器可能跑在 UTC（容器默认），如果直接用 ``datetime.now()``，
北京时间 0:00~8:00 之间会算成"昨天"，交易日判断、报告日期、缺口回补窗口全会错。
所以这里集中提供：

* :func:`now` / :func:`today` / :func:`now_str` —— 一律东八区；
* :func:`ensure_env_tz` —— 进程启动时把 ``TZ`` 固定成东八区（cron/日志时间也跟着对）。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from functools import lru_cache

log = logging.getLogger(__name__)

TZ_NAME = "Asia/Shanghai"
DATE_FMT = "%Y-%m-%d"


def ensure_env_tz() -> None:
    """把进程时区固定为东八区（可用 EMSCAN_TZ 覆盖，例如容器里已经是 UTC）。"""
    name = os.environ.get("EMSCAN_TZ") or TZ_NAME
    if not os.environ.get("TZ"):
        os.environ["TZ"] = name
    try:
        time.tzset()
    except AttributeError:  # pragma: no cover - 非 POSIX 平台
        log.debug("当前平台不支持 time.tzset()")


@lru_cache(maxsize=4)
def _zone_for(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # pragma: no cover - 极端环境下退回固定偏移 +08:00
        return dt.timezone(dt.timedelta(hours=8))


def _zone():
    """当前时区对象（缓存：ZoneInfo 构造要读系统 tzdata，别每次调用都建）。"""
    return _zone_for(os.environ.get("TZ") or TZ_NAME)


def now() -> dt.datetime:
    """当前时间（东八区，带 tzinfo）。"""
    return dt.datetime.now(_zone())


def today() -> str:
    """今天（东八区）``YYYY-MM-DD``。"""
    return now().strftime(DATE_FMT)


def now_str() -> str:
    """当前时间字符串（东八区）``YYYY-MM-DD HH:MM:SS``，用于日志/落库时间戳。"""
    return now().strftime("%Y-%m-%d %H:%M:%S")


def now_hm() -> str:
    """当前时刻（东八区）``HH:MM``，用来和"数据几点发布"比较。"""
    return now().strftime("%H:%M")


def date_of(value: dt.date | dt.datetime) -> str:
    return value.strftime(DATE_FMT)


def shift(date: str, days: int) -> str:
    """在 ``YYYY-MM-DD`` 上加减自然日。"""
    base = dt.datetime.strptime(str(date), DATE_FMT)
    return (base + dt.timedelta(days=int(days))).strftime(DATE_FMT)
