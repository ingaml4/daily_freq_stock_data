"""行情仓库的单元测试。"""

import sqlite3

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


def test_last_dates_and_date_index(tmp_path):
    store = Store(tmp_path / "market.sqlite").init_schema()
    dates = trading_dates(periods=4)
    store.upsert_bars([bar("1.600000", d, volume=1.0) for d in dates[:2]])
    store.upsert_bars([bar("0.000001", d, volume=1.0) for d in dates[:3]])
    last = store.last_dates()
    assert last == {"1.600000": dates[1], "0.000001": dates[2]}
    # 自愈逻辑靠"每只票已有哪些交易日"来判断缺口
    have = store.bar_dates_by_secid()
    assert have["1.600000"] == set(dates[:2])
    assert have["0.000001"] == set(dates[:3])
    assert store.bar_dates_by_secid(since=dates[2]) == {"0.000001": {dates[2]}}
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


def test_load_recent_bars_column_projection(tmp_path):
    """按列裁剪：扫描路径只读需要的列，secid/date 始终保留。"""
    store = Store(tmp_path / "market.sqlite").init_schema()
    dates = trading_dates(periods=5)
    store.upsert_bars([bar("1.600000", d, volume=1.0) for d in dates])
    slim = store.load_recent_bars(calendar_days=30, columns=("volume", "close", "amount"))
    assert list(slim.columns) == ["secid", "date", "volume", "close", "amount"]
    # 不存在的列会被忽略，而不是拼进 SQL
    weird = store.load_recent_bars(columns=("volume", "drop table bars; --"))
    assert list(weird.columns) == ["secid", "date", "volume"]
    assert store.stats()["bars"] == 5


def test_indicators_upsert_merges_kinds_without_clobbering(tmp_path):
    store = Store(tmp_path / "market.sqlite").init_schema()
    date = "2026-10-09"
    secid = "1.600001"

    assert (
        store.upsert_indicators(
            "macd",
            [
                {
                    "secid": secid,
                    "date": date,
                    "macd_diff": 0.067,
                    "macd_dea": 0.068,
                    "macd_bar": -0.002,
                }
            ],
        )
        == 1
    )
    # 同一天再写 KDJ 与 BOLL：不同指标共处一行，互不覆盖
    store.upsert_indicators(
        "kdj", [{"secid": secid, "date": date, "kdj_k": 37.54, "kdj_d": 45.01, "kdj_j": 22.62}]
    )
    store.upsert_indicators(
        "boll",
        [
            {
                "secid": secid,
                "date": date,
                "boll_upper": 11.28,
                "boll_mid": 11.03,
                "boll_lower": 10.78,
            }
        ],
    )
    # 重复写 MACD（幂等），并且不把别的指标冲掉
    store.upsert_indicators("macd", [{"secid": secid, "date": date, "macd_bar": -0.01}])

    frame = store.load_indicators()
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["macd_bar"] == -0.01
    assert row["kdj_k"] == 37.54
    assert row["boll_lower"] == 10.78
    assert store.latest_indicator_date() == date
    assert store.stats()["indicator_rows"] == 1
    assert store.stats()["indicator_symbols"] == 1
    store.close()


def test_indicators_load_window_and_column_projection(tmp_path):
    store = Store(tmp_path / "market.sqlite").init_schema()
    dates = trading_dates(periods=4)
    store.upsert_indicators(
        "macd",
        [{"secid": "1.600001", "date": d, "macd_diff": 1.0} for d in dates],
    )
    window = store.load_indicators(start=dates[1], end=dates[2])
    assert list(window["date"]) == dates[1:3]
    slim = store.load_indicators(columns=("macd_diff",))
    assert list(slim.columns) == ["secid", "date", "macd_diff"]
    # 不认识的列会被忽略，而不是拼进 SQL
    weird = store.load_indicators(columns=("macd_diff", "drop table indicators; --"))
    assert list(weird.columns) == ["secid", "date", "macd_diff"]
    assert store.stats()["indicator_rows"] == 4
    store.close()


def test_upsert_symbols_keeps_name_and_shares(tmp_path):
    """只刷新行情（不带名称）时不能把股票名冲掉；换手率靠流通股本推算。"""
    store = Store(tmp_path / "market.sqlite").init_schema()
    store.upsert_symbols(
        [
            {
                "secid": "1.600001",
                "code": "600001",
                "name": "浦发银行",
                "market": 1,
                "board": "sh_main",
                "float_shares": 1.0e9,
                "listed_date": "1999-11-10",
            }
        ]
    )
    store.upsert_symbols([{"secid": "1.600001", "code": "600001", "name": "", "market": 1}])
    frame = store.symbols_df()
    row = frame.iloc[0]
    assert row["name"] == "浦发银行"
    assert row["float_shares"] == 1.0e9
    assert row["listed_date"] == "1999-11-10"
    assert store.float_shares_map() == {"1.600001": 1.0e9}
    assert store.secids_missing_shares() == []

    store.upsert_symbols([{"secid": "0.300001", "code": "300001", "name": "测试"}])
    assert store.secids_missing_shares() == ["0.300001"]
    store.close()


def test_init_schema_migrates_old_symbols_table(tmp_path):
    """老版本建的库（symbols 没有 float_shares/listed_date）能自动升级。"""
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE symbols (
            secid TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT, market INTEGER,
            board TEXT, is_st INTEGER DEFAULT 0, updated_at TEXT
        );
        INSERT INTO symbols (secid, code, name, market, board, is_st)
        VALUES ('1.600001', '600001', '老库股票', 1, 'sh_main', 0);
        """
    )
    conn.commit()
    conn.close()

    store = Store(path).init_schema()
    columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(symbols)")}
    assert {"float_shares", "listed_date"} <= columns
    assert store.symbols_df().iloc[0]["name"] == "老库股票"
    store.upsert_symbols(
        [{"secid": "1.600001", "code": "600001", "name": "老库股票", "float_shares": 5.0}]
    )
    assert store.float_shares_map() == {"1.600001": 5.0}
    store.close()


def test_daily_signals_cache_merges_two_writers(tmp_path):
    """放量扫描和指标信号各写自己那几列，拼在同一行里，重复跑不打架。"""
    store = Store(tmp_path / "market.sqlite").init_schema()
    date = "2026-10-09"
    secid = "1.600001"
    store.upsert_symbols(
        [{"secid": secid, "code": "600001", "name": "测试", "market": 1, "board": "sh_main"}]
    )
    # 写入者 1：放量扫描
    assert (
        store.upsert_daily_signals(
            [
                {
                    "secid": secid,
                    "date": date,
                    "close": 10.0,
                    "pct_chg": 6.0,
                    "amount": 6.0e7,
                    "turnover": 2.5,
                    "vol_ratio_5": 5.0,
                    "vol_z60": 8.0,
                    "vol_signal": "放量上攻",
                    "s_vol_spike": 1,
                }
            ]
        )
        == 1
    )
    # 写入者 2：指标信号（同一行，不能把放量那几列冲掉）
    store.upsert_daily_signals(
        [
            {
                "secid": secid,
                "date": date,
                "macd_diff": 0.1,
                "macd_dea": 0.05,
                "macd_bar": 0.1,
                "kdj_k": 40.0,
                "kdj_d": 30.0,
                "boll_lower": 9.5,
                "boll_upper": 11.0,
                "boll_pos": 0.33,
                "s_macd_golden": 1,
                "s_kdj_golden": 1,
                "s_boll_break_lower": 0,
            }
        ]
    )
    frame = store.load_daily_signals()
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["s_vol_spike"] == 1 and row["s_macd_golden"] == 1
    assert row["vol_ratio_5"] == 5.0 and row["boll_lower"] == 9.5
    assert row["close"] == 10.0 and row["macd_dea"] == 0.05
    # 视图把标记拼成可读的规则名
    assert row["signals"] == "vol_spike,macd_golden,kdj_golden"
    store.close()


def test_load_daily_signals_filters(tmp_path):
    store = Store(tmp_path / "market.sqlite").init_schema()
    store.upsert_symbols(
        [
            {"secid": "1.600001", "code": "600001", "name": "A", "market": 1, "board": "sh_main"},
            {"secid": "0.300002", "code": "300002", "name": "B", "market": 0, "board": "sz_gem"},
        ]
    )
    rows = [
        {"secid": "1.600001", "date": "2026-10-08", "s_macd_golden": 1, "s_kdj_golden": 1},
        {"secid": "1.600001", "date": "2026-10-09", "s_vol_spike": 1},
        {"secid": "0.300002", "date": "2026-10-09", "s_vol_spike": 1, "s_boll_break_lower": 1},
    ]
    assert store.upsert_daily_signals(rows) == 3
    assert store.latest_signal_date() == "2026-10-09"
    assert list(store.load_daily_signals(start="2026-10-09", end="2026-10-09")["secid"]) == [
        "0.300002",
        "1.600001",
    ]
    # only = 全部命中；any_of = 命中任一
    assert list(store.load_daily_signals(only=["s_macd_golden", "s_kdj_golden"])["secid"]) == [
        "1.600001"
    ]
    assert set(store.load_daily_signals(any_of=["s_vol_spike", "s_macd_golden"])["secid"]) == {
        "1.600001",
        "0.300002",
    }
    # 至少命中 2 个信号：10-08 的 1.600001（金叉×2）与 10-09 的 0.300002（放量+下轨）
    assert list(store.load_daily_signals(min_count=2)["secid"]) == ["0.300002", "1.600001"]
    # join_symbols 带上名称/代码
    joined = store.load_daily_signals(secids=["0.300002"], join_symbols=True)
    assert joined.iloc[0]["name"] == "B"
    # 脏日期（缺日期被 pandas 变成 nan）应该被拒掉
    assert store.upsert_daily_signals([{"secid": "1.600001", "date": None, "s_vol_spike": 1}]) == 0
    assert store.upsert_daily_signals([{"secid": "1.600001", "date": "nan", "s_vol_spike": 1}]) == 0
    store.close()
    store.close()
