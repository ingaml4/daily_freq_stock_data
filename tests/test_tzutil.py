"""时区测试：本应用的"今天/现在"只认东八区，不受系统或环境变量里的 TZ 影响。"""

import datetime as dt
import os

import pytest

from emscan import tzutil


@pytest.fixture(autouse=True)
def _restore_tz():
    before = os.environ.get("TZ")
    yield
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    try:
        import time

        time.tzset()
    except AttributeError:  # pragma: no cover
        pass


def test_app_clock_ignores_ambient_tz(monkeypatch):
    """把 TZ 设成 UTC/芝加哥，tzutil 的"现在/今天"仍然是东八区。"""
    for ambient in ("UTC", "America/Chicago"):
        monkeypatch.setenv("TZ", ambient)
        offset = tzutil.now().utcoffset()
        assert offset == dt.timedelta(hours=8), ambient
        # 同一时刻按 UTC 算的日期，和按东八区算的日期，在"北京时间 0~8 点"会差一天；
        # 这里只断言 tzutil 一律给东八区的日期
        assert tzutil.today() == tzutil.now().strftime("%Y-%m-%d")


def test_ensure_env_tz_forces_east_eight(monkeypatch):
    monkeypatch.setenv("TZ", "America/Chicago")
    tzutil.ensure_env_tz()
    assert os.environ["TZ"] == "Asia/Shanghai"
    assert tzutil.now().utcoffset() == dt.timedelta(hours=8)
    assert tzutil.now_hm().count(":") == 1


def test_emscan_tz_is_the_only_override(monkeypatch):
    """唯一能改时区的入口是 EMSCAN_TZ（系统 TZ 不算）。"""
    monkeypatch.setenv("TZ", "America/Chicago")
    monkeypatch.setenv("EMSCAN_TZ", "UTC")
    tzutil.ensure_env_tz()
    assert os.environ["TZ"] == "UTC"
    assert tzutil.now().utcoffset() == dt.timedelta(0)
    monkeypatch.delenv("EMSCAN_TZ")
    assert tzutil.now().utcoffset() == dt.timedelta(hours=8)


def test_describe_has_both_clocks():
    info = tzutil.describe()
    assert info["app_offset_hours"] == 8.0
    assert info["app_zone"] == "Asia/Shanghai"
    for key in ("system_time", "system_tz", "system_offset_hours", "app_time", "utc_now"):
        assert info[key] != "" and info[key] is not None
    # 同一时刻：系统时间 + (8h - 系统偏移) == 应用时间（分钟级容差，避免跨分钟抖动）
    system = dt.datetime.strptime(info["system_time"], "%Y-%m-%d %H:%M:%S")
    app = dt.datetime.strptime(info["app_time"], "%Y-%m-%d %H:%M:%S")
    delta_hours = (app - system).total_seconds() / 3600.0
    assert abs(delta_hours - (8.0 - float(info["system_offset_hours"]))) < 0.05


def test_date_helpers():
    assert tzutil.shift("2026-10-13", -3) == "2026-10-10"
    assert tzutil.date_of(dt.date(2026, 1, 5)) == "2026-01-05"
