"""行情仓库的单元测试。"""

from helpers import bar, trading_dates

from emscan.store import Store


def test_upsert_is_idempotent_and_tracks_trading_days(tmp_path):
    store = Store(tmp_path / "market.sqlite").init_schema()
    dates = trading_dates(periods=5)
    rows = [bar("1.600000", d, volume=100000.0) for d in dates]
    assert store.upsert_bars(rows) == 5
    assert store.upsert_bars(rows) == 5  # 重复写入同一批：行数不变
    info = store.stats()
    assert info["bars"] == 5
    assert info["symbols_with_bars"] == 1
    assert info["trading_days"] == 5
    assert store.latest_date() == dates[-1]
    assert store.trading_day_before(dates[-1]) == dates[-2]
    assert store.trading_day_before(dates[0]) is None
    store.close()


def test_last_dates_and_heal_log(tmp_path):
    store = Store(tmp_path / "market.sqlite").init_schema()
    dates = trading_dates(periods=4)
    store.upsert_bars([bar("1.600000", d, volume=1.0) for d in dates[:2]])
    store.upsert_bars([bar("0.000001", d, volume=1.0) for d in dates[:3]])
    last = store.last_dates()
    assert last == {"1.600000": dates[1], "0.000001": dates[2]}

    assert store.healed_secids(dates[-1]) == set()
    store.log_heal(dates[-1], [("1.600000", 3), ("0.000001", 0)])
    assert store.healed_secids(dates[-1]) == {"1.600000", "0.000001"}
    # 重复记录同一天同一只：靠主键去重
    store.log_heal(dates[-1], [("1.600000", 5)])
    row = store.conn.execute(
        "SELECT rows_added FROM heal_log WHERE trade_date=? AND secid=?",
        (dates[-1], "1.600000"),
    ).fetchone()
    assert row["rows_added"] == 5
    store.close()


def test_symbols_upsert_marks_st(tmp_path):
    store = Store(tmp_path / "market.sqlite").init_schema()
    store.upsert_symbols(
        [
            {
                "secid": "1.600000",
                "code": "600000",
                "name": "浦发银行",
                "market": 1,
                "board": "sh_main",
            },
            {
                "secid": "1.600001",
                "code": "600001",
                "name": "ST退市风险",
                "market": 1,
                "board": "sh_main",
            },
        ]
    )
    frame = store.symbols_df().set_index("secid")
    assert frame.loc["1.600000", "is_st"] == 0
    assert frame.loc["1.600001", "is_st"] == 1
    assert store.secids(["sh_main"]) == ["1.600000", "1.600001"]
    store.close()


def test_load_recent_bars_limits_window(tmp_path):
    store = Store(tmp_path / "market.sqlite").init_schema()
    dates = trading_dates(periods=40)
    store.upsert_bars([bar("1.600000", d, volume=1.0) for d in dates])
    recent = store.load_recent_bars(calendar_days=10)
    assert len(recent) < len(dates)
    assert recent["date"].max() == dates[-1]
    assert all(d >= recent["date"].min() for d in recent["date"])
    store.close()
