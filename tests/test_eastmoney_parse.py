"""东方财富报文解析的单元测试（不需要网络）。"""

from emscan.eastmoney import (
    parse_clist_page,
    parse_kline_payload,
    secid_of,
    spot_trade_date,
    to_float,
)


def test_to_float_handles_eastmoney_placeholders():
    assert to_float("12.34") == 12.34
    assert to_float(5) == 5.0
    assert to_float("-") is None
    assert to_float("--") is None
    assert to_float("") is None
    assert to_float(None) is None


def test_secid_of():
    assert secid_of(1, "600519") == "1.600519"
    assert secid_of("0", "000001") == "0.000001"
    assert secid_of(None, "600519") is None


def test_parse_clist_page_dict_form():
    payload = {
        "rc": 0,
        "data": {
            "total": 2,
            "diff": {
                "0": {
                    "f12": "600519",
                    "f13": 1,
                    "f14": "贵州茅台",
                    "f2": 1680.5,
                    "f3": 1.23,
                    "f4": 20.4,
                    "f5": 32000,
                    "f6": 5370000000.0,
                    "f7": 2.1,
                    "f8": 0.25,
                    "f10": 1.8,
                    "f15": 1690.0,
                    "f16": 1660.0,
                    "f17": 1665.0,
                    "f18": 1660.1,
                    "f20": 2100000000000,
                    "f21": 2100000000000,
                    "f124": 1791500400,
                },
                "1": {
                    "f12": "000001",
                    "f13": 0,
                    "f14": "平安银行",
                    "f2": 11.5,
                    "f3": "-",
                    "f5": 500000,
                    "f6": 575000000.0,
                    "f8": 0.51,
                    "f124": 1791500400,
                },
            },
        },
    }
    rows = parse_clist_page(payload)
    assert len(rows) == 2
    first, second = rows
    assert first["secid"] == "1.600519"
    assert first["name"] == "贵州茅台"
    assert first["volume"] == 32000.0
    assert first["amount"] == 5370000000.0
    assert first["turnover"] == 0.25
    assert first["volume_ratio"] == 1.8
    assert first["prev_close"] == 1660.1
    assert first["source"] == "spot"
    assert second["secid"] == "0.000001"
    assert second["pct_chg"] is None  # "-" -> None


def test_parse_clist_page_list_form_and_missing_data():
    payload = {
        "data": {"total": 1, "diff": [{"f12": "300750", "f13": 0, "f14": "宁德时代", "f5": 100}]}
    }
    rows = parse_clist_page(payload)
    assert len(rows) == 1
    assert rows[0]["secid"] == "0.300750"
    assert rows[0]["volume"] == 100.0
    assert rows[0]["close"] is None
    assert parse_clist_page({"data": None}) == []
    assert parse_clist_page(None) == []


def test_parse_kline_payload_field_order():
    first = "2026-10-08,1660.00,1680.50,1690.00,1655.00,32000,5370000000.00,2.10,1.23,20.40,0.25"
    second = "2026-10-09,1680.00,1690.00,1700.00,1670.00,41000,6900000000.00,1.79,0.57,9.50,0.32"
    payload = {
        "data": {
            "code": "600519",
            "market": 1,
            "name": "贵州茅台",
            "klines": [first, second],
        }
    }
    result = parse_kline_payload(payload, "1.600519")
    assert result["name"] == "贵州茅台"
    assert result["code"] == "600519"
    assert result["market"] == 1
    rows = result["rows"]
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-10-08"
    assert rows[0]["open"] == 1660.0
    assert rows[0]["close"] == 1680.5
    assert rows[0]["high"] == 1690.0
    assert rows[0]["low"] == 1655.0
    assert rows[0]["volume"] == 32000.0
    assert rows[0]["amount"] == 5370000000.0
    assert rows[0]["amplitude"] == 2.1
    assert rows[0]["pct_chg"] == 1.23
    assert rows[0]["chg"] == 20.4
    assert rows[0]["turnover"] == 0.25
    assert rows[1]["turnover"] == 0.32
    assert rows[1]["source"] == "kline"
    assert parse_kline_payload({"data": None}, "1.600519")["rows"] == []


def test_spot_trade_date_uses_market_max_timestamp():
    # 1791500400 = 2026-10-09 15:00:00 +08:00
    rows = [{"ts": 1791500400}, {"ts": 1791500000}, {"ts": None}]
    assert spot_trade_date(rows) == "2026-10-09"
    assert spot_trade_date([]) is None
