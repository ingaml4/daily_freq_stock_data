"""统一时区：本项目所有"今天 / 现在"都按东八区（Asia/Shanghai）算。

服务器可能跑在 UTC（容器默认），如果直接用 ``datetime.now()``，
北京时间 0:00~8:00 之间会算成"昨天"，交易日判断、报告日期、缺口回补窗口全会错。
所以这里集中提供：

* :func:`now` / :func:`today` / :func:`now_str` —— 一律东八区；
* :func:`ensure_env_tz` —— 进程启动时把 ``TZ`` **强制**设成东八区（覆盖系统/继承来的值）；
* :func:`describe` —— 把"系统怎么看时间"和"应用按东八区怎么看"摆一起，方便排查。

想换时区只有一个入口：环境变量 ``EMSCAN_TZ``（例如 ``EMSCAN_TZ=UTC``）。
除此之外，**系统时区、``TZ``、``/etc/localtime`` 怎么设都不影响本应用** ——
"今天是不是交易日""数据是不是当天的"永远按东八区判断。
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

# 进程启动时的"原始"系统时区（在我们强制 TZ=东八区之前抓拍），只用于诊断输出；
# 这样 describe() 能如实报告"你这台机器本来是什么时区"，而不是被我们改过的值。
_ORIGINAL = {
    "env_tz": os.environ.get("TZ"),
    "tzname": dt.datetime.now().astimezone().tzname(),
    "offset": dt.datetime.now().astimezone().utcoffset() or dt.timedelta(0),
}


def ensure_env_tz() -> None:
    """把进程时区**强制**设成东八区（覆盖系统/继承来的 ``TZ``）；``EMSCAN_TZ`` 可改。"""
    name = _zone_name()
    if os.environ.get("TZ") != name:
        log.debug("把 TZ 从 %r 改成 %r（本应用一律按东八区判断交易日）", os.environ.get("TZ"), name)
        os.environ["TZ"] = name
    try:
        time.tzset()
    except AttributeError:  # pragma: no cover - 非 POSIX 平台
        log.debug("当前平台不支持 time.tzset()")


def _zone_name() -> str:
    """生效时区名：只有 ``EMSCAN_TZ`` 能改，其余（系统 TZ）一律忽略。"""
    return (os.environ.get("EMSCAN_TZ") or TZ_NAME).strip() or TZ_NAME


@lru_cache(maxsize=4)
def _zone_for(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # pragma: no cover - 极端环境下退回固定偏移 +08:00
        return dt.timezone(dt.timedelta(hours=8))


def _zone():
    """当前时区对象（缓存：ZoneInfo 构造要读系统 tzdata，别每次调用都建）。"""
    return _zone_for(_zone_name())


def describe() -> dict:
    """诊断用：系统时间 vs 应用（东八区）时间 —— ``emscan check`` 会打印它。"""
    app = now()
    utc_now = dt.datetime.now(dt.timezone.utc)
    local_offset = _ORIGINAL["offset"] or dt.timedelta(0)
    app_offset = app.utcoffset() or dt.timedelta(0)
    return {
        "system_time": (utc_now + local_offset).strftime("%Y-%m-%d %H:%M:%S"),
        "system_tz": str(_ORIGINAL["tzname"] or "?"),
        "system_tz_name": _ORIGINAL["env_tz"] or "(未设置，按 /etc/localtime)",
        "system_offset_hours": local_offset.total_seconds() / 3600.0,
        "app_time": app.strftime("%Y-%m-%d %H:%M:%S"),
        "app_zone": _zone_name(),
        "app_offset_hours": app_offset.total_seconds() / 3600.0,
        "utc_now": utc_now.strftime("%Y-%m-%d %H:%M:%S"),
    }


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
