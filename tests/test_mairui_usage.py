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
