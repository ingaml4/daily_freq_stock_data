import json
import logging
import socket
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from emscan import mairui, request_usage
from emscan.mairui import MairuiClient, MairuiError, MairuiQuotaError


@pytest.fixture
def local_api(monkeypatch):
    monkeypatch.setattr(request_usage, "_counts", defaultdict(Counter))
    monkeypatch.setattr(mairui.time, "sleep", lambda *_: None)
    hits, responses = [], []
    secret = "never-print-test-certificate"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            status, data = responses.pop(0)
            if status == 0:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/redirect/" + secret)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = MairuiClient(
        secret,
        base_url=f"http://127.0.0.1:{server.server_port}",
        retries=2,
        rate_limit_per_min=0,
        sleep=0,
    )
    yield client, responses, hits, secret
    client.close()
    server.shutdown()
    server.server_close()
    thread.join()


def test_transport_matches_server_retries_redirects_and_disconnects(local_api, caplog):
    client, responses, hits, secret = local_api
    responses.extend([(0, None), (503, None), (302, None), (200, [])])
    with caplog.at_level(logging.DEBUG):
        assert client.fetch_universe() == []
        request_usage.log_summary()
    data = request_usage.snapshot()
    row = data["endpoints"]["hslt.list"]
    assert len(hits) == row["http_attempts"] == 4
    assert row["http_responses"] == 3 and row["transport_errors"] == 1
    assert row["retry_attempts"] == 2 and row["redirect_hops"] == 1
    assert row["json_successes"] == row["http_2xx"] == row["logical_calls"] == 1
    assert row["http_5xx"] == 1 and data["billing"] == "provider_units_unknown"
    assert "mairui_usage " in caplog.text and secret not in caplog.text
    assert client.request_count == 2  # 原字段只统计最终有响应的 get 调用


@pytest.mark.parametrize(
    "status,expected",
    [(401, MairuiQuotaError), (403, MairuiQuotaError), (404, MairuiError), (422, MairuiError)],
)
def test_permanent_failure_stops_and_redacts_provider_echo(local_api, status, expected):
    client, responses, hits, secret = local_api
    responses.append((status, {"code": status, "message": "key=" + secret}))
    with pytest.raises(expected) as err:
        client.fetch_universe()
    assert secret not in str(err.value)
    assert request_usage.snapshot()["totals"]["retry_attempts"] == 0 and len(hits) == 1


def test_provider_429_is_bounded_and_classified(local_api):
    client, responses, hits, secret = local_api
    responses.extend([(200, {"code": "429", "message": secret})] * 3)
    with pytest.raises(MairuiQuotaError):
        client.fetch_universe()
    row = request_usage.snapshot()["totals"]
    assert len(hits) == row["http_attempts"] == 3 and row["retry_attempts"] == 2
    assert row["http_2xx"] == 3 and row["json_successes"] == 0


def test_process_aggregate_is_threadsafe(local_api):
    client, responses, hits, secret = local_api
    responses.extend([(200, [])] * 40)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: client.fetch_universe(), range(40)))
    assert request_usage.snapshot()["totals"]["json_successes"] == len(hits) == 40
    assert (
        request_usage.endpoint(
            "http://h/proxy/hsstock/history/macd/000001.SZ/d/f/" + secret, "http://h/proxy"
        )
        == "hsstock.history.macd"
    )
    assert request_usage.endpoint("http://h/private/" + secret, "http://h") == "other"


def test_credential_quota_cancels_large_queue_and_blocks_other_routes(local_api):
    client, responses, hits, _ = local_api
    responses.extend([(200, {"code": 429, "message": "daily quota exceeded"})] * client.workers)
    with pytest.raises(MairuiQuotaError) as err:
        client.fetch_kline_many([f"0.{i:06d}" for i in range(1000)])
    assert err.value.scope == "credential"
    actual = len(hits)
    assert 1 <= actual <= client.workers
    with pytest.raises(MairuiQuotaError):
        client.fetch_universe()
    assert len(hits) == actual == request_usage.snapshot()["totals"]["http_attempts"]


def test_endpoint_denial_does_not_block_unrelated_resources(local_api):
    client, responses, hits, _ = local_api
    responses.extend([(403, {"code": 403, "message": "无权限"}), (404, None), (200, [])])
    for _ in range(2):
        with pytest.raises(MairuiQuotaError) as err:
            client.fetch_universe()
        assert err.value.scope == "endpoint"
    assert len(hits) == 1
    with pytest.raises(MairuiError):
        client.fetch_kline("0.000001")
    assert client.fetch_kline("0.000002") == []
    assert len(hits) == 3


def test_plain_invalid_certificate_is_not_json_success(local_api):
    client, responses, hits, _ = local_api
    responses.append((200, "证书已失效"))
    with pytest.raises(MairuiQuotaError):
        client.fetch_universe()
    assert len(hits) == 1
    assert request_usage.snapshot()["totals"]["json_successes"] == 0


@pytest.mark.parametrize(
    "status,body,code",
    [
        (200, 101, "101"),
        (429, "101", "101"),
        (200, {"code": 101}, "101"),
        (503, {"code": "101", "msg": "daily"}, "101"),
        (429, 102, "102"),
        (200, "102", "102"),
        (200, {"code": "102"}, "102"),
        (429, {"code": 102, "message": "invalid"}, "102"),
    ],
)
def test_declared_business_errors_stop_all_endpoints(local_api, status, body, code):
    client, responses, hits, _ = local_api
    responses.append((status, body))
    with pytest.raises(MairuiQuotaError) as err:
        client.fetch_universe()
    assert err.value.scope == "credential"
    assert f"HTTP {status} / 接口错误码 {code}" in str(err.value)
    with pytest.raises(MairuiQuotaError):
        client.fetch_kline("0.000001")
    counts = request_usage.snapshot()["totals"]
    assert counts["http_attempts"] == len(hits) == 1
    assert counts[f"http_{status // 100}xx"] == counts["circuit_skips"] == 1
    assert counts["retry_attempts"] == counts["json_successes"] == 0


def test_ip_window_bounds_retries_and_recovers_across_endpoints(local_api, monkeypatch):
    client, responses, hits, _ = local_api
    client.retries = 8
    now = [1000]
    waits = []
    monkeypatch.setattr(mairui.time, "monotonic", lambda: now[0])

    def advance(seconds):
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(mairui.time, "sleep", advance)
    responses.extend([(429, {"code": 103, "message": "quota exhausted"}), (200, "103"), (200, [])])
    with pytest.raises(MairuiQuotaError) as err:
        client.fetch_universe()
    assert err.value.scope == "ip_window" and "出口 IP" in str(err.value)
    assert len(hits) == 2 and waits == [60]
    now[0] += 59
    with pytest.raises(MairuiQuotaError) as blocked:
        client.fetch_kline("0.000001")
    assert blocked.value.scope == "ip_window" and len(hits) == 2
    now[0] += 1
    assert client.fetch_kline("0.000001") == []
    counts = request_usage.snapshot()["totals"]
    assert counts["http_attempts"] == 3 and counts["retry_attempts"] == 1
    assert counts["json_successes"] == counts["circuit_skips"] == 1


def test_103_stops_mixed_endpoint_batch_without_permanent_block(local_api):
    client, responses, hits, _ = local_api
    client.retries = 0
    responses.extend([(429, 103)] * client.workers)
    with pytest.raises(MairuiQuotaError) as err:
        client.fetch_indicators_many([f"0.{i:06d}" for i in range(300)])
    assert err.value.scope == "ip_window"
    assert 1 <= len(hits) <= client.workers


def test_data_records_and_transport_status_are_independent_of_business_codes(local_api):
    client, responses, hits, _ = local_api
    client.retries = 0
    valid = [
        [{"code": 101, "message": "row"}],
        {"code": "102", "price": 100},
        {"data": {"code": 101}},
        {"code": 102, "message": "data record", "price": 100},
        {"code": 101, "data": [{"close": 10}]},
        {"code": "00001"},
        101.5,
        "订单 #102",
        "历史公告提到证书无效",
    ]
    responses.extend((200, row) for row in valid)
    url = client._url("/hslt/list/" + client.licence)
    for row in valid:
        assert client._get_json(url) == row
    # An undocumented business code matching an HTTP status is not an HTTP auth failure.
    responses.extend([(200, {"code": 401, "msg": "unknown"}), (200, [])])
    with pytest.raises(MairuiError) as err:
        client.fetch_universe()
    assert not isinstance(err.value, MairuiQuotaError)
    assert client.fetch_universe() == []
    assert len(hits) == len(valid) + 2
