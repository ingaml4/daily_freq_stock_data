"""麦蕊智数报文解析的单元测试（不需要网络，报文取自真实接口返回）。"""

import pytest

from emscan.mairui import (
    MairuiClient,
    MairuiError,
    RateLimiter,
    align_quotes_by_position,
    board_of,
    dm_of,
    error_message,
    normalize_adjust,
    normalize_date,
    normalize_indicator_kind,
    parse_indicator_payload,
    parse_instrument,
    parse_kline_payload,
    parse_quote_payload,
    parse_stock_list,
    parse_trade_calendar,
    secid_of,
    spot_trade_date,
    to_float,
)

# --- 真实报文样本（2026-10-13 从 https://api.mairuiapi.com 抓取） ---------------
STOCK_LIST = [
    {"dm": "000001.SZ", "mc": "平安银行", "jys": "SZ"},
    {"dm": "600519.SH", "mc": "贵州茅台", "jys": "SH"},
    {"dm": "300750.SZ", "mc": "宁德时代", "jys": "SZ"},
    {"dm": "688981.SH", "mc": "中芯国际", "jys": "SH"},
    {"dm": "000004.SZ", "mc": "*ST国华", "jys": "SZ"},
]

QUOTE_ALL = [
    {
        "dm": "000001",
        "o": 11.31,
        "fm": -0.09,
        "h": 11.39,
        "hs": 0.33,
        "lb": 0.82,
        "l": 11.3,
        "lt": 220059184779.0,
        "pe": 4.94,
        "pc": -0.26,
        "p": 11.34,
        "sz": 220063112365.0,
        "cje": 730375807.93,
        "ud": -0.03,
        "v": 643914,
        "yc": 11.37,
        "zf": 0.79,
        "zs": 0.0,
        "sjl": 0.52,
        "t": "2025-04-03 15:29:10",
    }
]

QUOTE_ONE = {
    "t": "2026-10-13 08:56:59",
    "p": 0.0,
    "pc": 0.0,
    "ud": 0.0,
    "v": 0.0,
    "cje": 0.0,
    "zf": 0.0,
    "hs": 0.0,
    "pe": 4.43,
    "lb": 0.0,
    "fm": 0.0,
    "h": 0.0,
    "l": 0.0,
    "o": 0.0,
    "yc": 11.73,
    "sz": 227631420463,
    "lt": 227628684944,
    "zs": 0.0,
    "sjl": 0.49,
    "zdf60": 13.99,
    "zdfnc": 8.61,
}

KLINE = [
    {
        "a": 980347616.0,
        "c": 11.65,
        "h": 11.72,
        "l": 11.63,
        "o": 11.7,
        "pc": 11.72,
        "sf": 0.0,
        "t": "2025-09-15",
        "v": 840387.0,
    },
    {
        "a": 0.0,
        "c": 11.66,
        "h": 11.66,
        "l": 11.66,
        "o": 11.66,
        "pc": 11.66,
        "sf": 1.0,
        "t": "2026-10-09",
        "v": 0.0,
    },
    {
        "a": 1243459833.0,
        "c": 11.73,
        "h": 11.83,
        "l": 11.62,
        "o": 11.62,
        "pc": 11.59,
        "sf": 0.0,
        "t": "2026-10-12",
        "v": 1058635.0,
    },
]


def test_to_float_handles_placeholders():
    assert to_float("12.34") == 12.34
    assert to_float(5) == 5.0
    assert to_float(0) == 0.0
    assert to_float("-") is None
    assert to_float("--") is None
    assert to_float("") is None
    assert to_float(None) is None
    assert to_float("abc") is None


def test_secid_and_dm_conversion():
    assert secid_of("000001.SZ") == "0.000001"
    assert secid_of("600519.SH") == "1.600519"
    assert secid_of("920992.BJ") == "2.920992"
    assert secid_of("000001", "SZ") == "0.000001"
    # 没有后缀时按代码前缀推断
    assert secid_of("688981") == "1.688981"
    assert secid_of("300750") == "0.300750"
    assert secid_of(None) is None

    assert dm_of("0.000001") == "000001.SZ"
    assert dm_of("1.600519") == "600519.SH"
    assert dm_of("2.920992") == "920992.BJ"
    with pytest.raises(ValueError):
        dm_of("000001")


def test_board_of_maps_secid_to_board():
    assert board_of("1.600519") == "sh_main"
    assert board_of("1.688981") == "sh_star"
    assert board_of("0.000001") == "sz_main"
    assert board_of("0.300750") == "sz_gem"
    assert board_of("2.920992") == "bse"
    assert board_of("0.830799") == "bse"
    assert board_of("bad") == "unknown"


def test_normalize_date_and_adjust():
    assert normalize_date("2026-10-13") == "2026-10-13"
    assert normalize_date("2026-10-13 08:56:59") == "2026-10-13"
    assert normalize_date("20261013") == "2026-10-13"
    assert normalize_date("2025-11-27 00:00:00") == "2025-11-27"
    assert normalize_date(None) is None

    assert normalize_adjust("f") == "f"
    assert normalize_adjust(1) == "f"  # 兼容老配置 kline_fqt: 1
    assert normalize_adjust(0) == "n"
    assert normalize_adjust(None) == "f"
    assert normalize_adjust("FR") == "fr"
    assert normalize_adjust("乱填") == "f"


def test_error_message_detects_api_errors():
    assert error_message({"code": 503, "message": "upstream unavailable"}) == (
        "503: upstream unavailable"
    )
    assert error_message({"t": "2026-10-13", "p": 11.34}) is None
    assert error_message([{"dm": "000001"}]) is None


def test_parse_stock_list():
    rows = parse_stock_list(STOCK_LIST)
    assert [row["secid"] for row in rows] == [
        "0.000001",
        "1.600519",
        "0.300750",
        "1.688981",
        "0.000004",
    ]
    assert rows[0]["name"] == "平安银行"
    assert rows[0]["code"] == "000001"
    assert rows[0]["board"] == "sz_main"
    assert rows[0]["is_st"] == 0
    assert rows[3]["board"] == "sh_star"
    assert rows[4]["is_st"] == 1  # *ST国华
    assert parse_stock_list(None) == []
    assert parse_stock_list([{"mc": "没有代码"}]) == []


def test_parse_quote_payload_whole_market():
    rows = parse_quote_payload(QUOTE_ALL)
    assert len(rows) == 1
    row = rows[0]
    assert row["secid"] == "0.000001"
    assert row["close"] == 11.34
    assert row["prev_close"] == 11.37
    assert row["volume"] == 643914.0  # 手
    assert row["amount"] == 730375807.93
    assert row["turnover"] == 0.33
    assert row["volume_ratio"] == 0.82
    assert row["amplitude"] == 0.79
    assert row["pct_chg"] == -0.26
    assert row["chg"] == -0.03
    assert row["float_mv"] == 220059184779.0
    assert row["float_shares"] == pytest.approx(220059184779.0 / 11.34)
    assert row["source"] == "spot"
    assert spot_trade_date(rows) == "2025-04-03"


def test_parse_quote_payload_single_object_and_pre_open():
    # 单股接口返回的是对象而不是数组；盘前 p=0，要用昨收估流通股本
    rows = parse_quote_payload(QUOTE_ONE, secid_hint="0.000001")
    assert len(rows) == 1
    row = rows[0]
    assert row["secid"] == "0.000001"
    assert row["close"] == 0.0
    assert row["float_shares"] == pytest.approx(227628684944 / 11.73)
    assert spot_trade_date(rows) == "2026-10-13"
    assert parse_quote_payload(None) == []


def test_parse_quote_payload_accepts_tr_field_alias():
    """ssjy_more 文档里换手率字段叫 tr，这里要兼容。"""
    rows = parse_quote_payload([{"dm": "600519", "p": 10.0, "tr": 1.5, "v": 100}])
    assert rows[0]["turnover"] == 1.5


def test_align_quotes_by_position_without_dm():
    """ssjy_more 的返回体可能没有 dm：按请求顺序对齐；数量对不上就放弃。"""
    payload = [{"p": 10.0, "yc": 9.9, "v": 100}, {"p": 20.0, "yc": 19.9, "v": 200}]
    rows = align_quotes_by_position(payload, ["1.600000", "0.000001"])
    assert [row["secid"] for row in rows] == ["1.600000", "0.000001"]
    assert rows[0]["close"] == 10.0 and rows[1]["close"] == 20.0
    # 数量对不上 -> 放弃（调用方会退回逐只抓）
    assert align_quotes_by_position(payload, ["1.600000"]) == []
    # 带 dm 就按代码走
    rows = align_quotes_by_position([{"dm": "600519", "p": 1.0}], ["1.600000"])
    assert rows[0]["secid"] == "1.600519"


def test_fetch_spot_default_chain_skips_pro_whole_market():
    """默认 auto：多股 → 逐只，**不碰** Pro 专属的全市场接口。"""

    class Probe(MairuiClient):
        def __init__(self, more_rows, single_rows):
            super().__init__("LICENCE-TEST")
            self.calls = []
            self._more = more_rows
            self._single = single_rows

        def fetch_quote_all(self):
            self.calls.append("all")
            return [{"secid": "1.600000"}]

        def fetch_quote_multi(self, secids):
            self.calls.append("more")
            return self._more

        def fetch_quote_one(self, secid):
            self.calls.append("single:%s" % secid)
            return self._single

    ok = Probe([{"secid": "1.600000"}], [{"secid": "1.600000"}])
    assert ok.fetch_spot(["1.600000"]) and ok.calls == ["more"]

    # 多股拿不到 -> 退逐只，且始终没碰 all
    fallback = Probe([], [{"secid": "1.600000"}])
    assert fallback.fetch_spot(["1.600000"])
    assert fallback.calls == ["more", "single:1.600000"]

    # 显式指定 all 才走全市场接口（给有 Pro/包年权限的证书用）
    forced = Probe([{"secid": "1.600000"}], [])
    assert forced.fetch_spot(["1.600000"], mode="all")
    assert forced.calls == ["all"]

    # 强制 more 时，接口报错会直接把错抛出来（不静默降级）；
    # 只是"返回空"仍会退到逐只（空数据多半是临时抖动，不是权限问题）
    class Boom(Probe):
        def fetch_quote_multi(self, secids):
            self.calls.append("more")
            raise MairuiError("429 too many requests")

    strict = Boom([], [{"secid": "1.600000"}])
    with pytest.raises(MairuiError):
        strict.fetch_spot(["1.600000"], mode="more")


def test_parse_kline_payload_derives_indicators():
    rows = parse_kline_payload(KLINE, "0.000001", float_shares=19_000_000_000)
    # 停牌行（sf=1）被丢弃
    assert [row["date"] for row in rows] == ["2025-09-15", "2026-10-12"]
    first = rows[0]
    assert first["open"] == 11.7
    assert first["close"] == 11.65
    assert first["volume"] == 840387.0
    assert first["chg"] == pytest.approx(11.65 - 11.72)
    assert first["pct_chg"] == pytest.approx((11.65 - 11.72) / 11.72 * 100)
    assert first["amplitude"] == pytest.approx((11.72 - 11.63) / 11.72 * 100)
    assert first["turnover"] == pytest.approx(840387.0 * 100 / 19_000_000_000 * 100)
    assert first["float_mv"] == pytest.approx(11.65 * 19_000_000_000)
    assert first["source"] == "kline"
    assert rows[-1]["pct_chg"] == pytest.approx((11.73 - 11.59) / 11.59 * 100)


def test_parse_kline_payload_without_float_shares_keeps_none():
    rows = parse_kline_payload(KLINE, "0.000001")
    assert rows[0]["turnover"] is None
    assert rows[0]["float_mv"] is None
    assert rows[0]["pct_chg"] is not None
    # 允许保留停牌行（回补时按需打开）
    kept = parse_kline_payload(KLINE, "0.000001", skip_suspended=False)
    assert len(kept) == 3
    assert parse_kline_payload(None, "0.000001") == []
    assert parse_kline_payload([{"v": 1}], "0.000001") == []


def test_parse_indicator_payloads():
    macd = parse_indicator_payload(
        [{"t": "2025-11-27 00:00:00", "diff": 0.067, "dea": 0.068, "macd": -0.002}],
        "macd",
        "0.000001",
    )
    assert macd == [
        {
            "secid": "0.000001",
            "date": "2025-11-27",
            "macd_diff": 0.067,
            "macd_dea": 0.068,
            "macd_bar": -0.002,
            "source": "indicator",
        }
    ]

    kdj = parse_indicator_payload(
        [{"t": "2025-11-27", "k": 37.54, "d": 45.01, "j": 22.62}], "kdj", "0.000001"
    )
    assert (kdj[0]["kdj_k"], kdj[0]["kdj_d"], kdj[0]["kdj_j"]) == (37.54, 45.01, 22.62)

    # BOLL 的 d 是"下轨"，不能和 KDJ 的 D 值混淆
    boll = parse_indicator_payload(
        [{"t": "2025-11-27", "u": 11.28, "m": 11.03, "d": 10.78}], "boll", "0.000001"
    )
    assert (boll[0]["boll_upper"], boll[0]["boll_mid"], boll[0]["boll_lower"]) == (
        11.28,
        11.03,
        10.78,
    )

    ma = parse_indicator_payload(
        [
            {
                "t": "2025-11-27",
                "ma5": 11.088,
                "ma10": 11.105,
                "ma20": 11.0275,
                "ma60": 10.8767,
                "ma250": 10.65,
            }
        ],
        "ma",
        "0.000001",
    )
    assert ma[0]["ma5"] == 11.088 and ma[0]["ma60"] == 10.8767
    assert "ma250" not in ma[0]

    assert parse_indicator_payload(None, "macd", "0.000001") == []
    with pytest.raises(ValueError):
        normalize_indicator_kind("rsi")


def test_parse_trade_calendar_and_instrument():
    days = parse_trade_calendar(["20260107", "20260105", "20260106", "20260105"])
    assert days == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert parse_trade_calendar({"data": ["20260105"]}) == ["2026-01-05"]
    assert parse_trade_calendar(None) == []

    info = parse_instrument(
        [{"ii": "000001.SZ", "name": "平安银行", "od": "1991-04-03", "fv": 19405918198, "tv": 1}]
    )
    assert info["secid"] == "0.000001"
    assert info["name"] == "平安银行"
    assert info["listed_date"] == "1991-04-03"
    assert info["float_shares"] == 19405918198
    assert parse_instrument(None) is None


def test_client_requires_licence():
    client = MairuiClient("")
    with pytest.raises(MairuiError) as excinfo:
        client.fetch_universe()
    assert "licence" in str(excinfo.value)


def test_client_from_config_reads_licence_and_adjust(tmp_path):
    from helpers import make_cfg

    cfg = make_cfg(
        tmp_path,
        {"mairui": {"licence": "LICENCE-TEST", "kline_adjust": 0, "rate_limit_per_min": 600}},
    )
    client = MairuiClient.from_config(cfg)
    assert client.licence == "LICENCE-TEST"
    assert client.adjust == "n"  # 0 -> 不复权
    assert client.limiter.min_interval == pytest.approx(0.1)


def test_rate_limiter_spacing():
    limiter = RateLimiter(per_minute=600)  # 0.1 秒一个
    import time

    limiter.acquire()
    start = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - start >= 0.09
    assert RateLimiter(0).min_interval == 0
