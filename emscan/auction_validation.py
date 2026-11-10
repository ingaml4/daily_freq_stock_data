"""一次性的本机竞价接口验证。只读官方日历缓存，不直接访问行情供应商。"""

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import requests

from .auction_archive import current_session
from .config import Config

EXPECTED_DATE = "2026-11-11"
SHANGHAI = timezone(timedelta(hours=8))
ENDPOINT = "/api/auction-snapshot"
QUERY = "symbols=SZ300611,SZ300969"
WINDOW = (time(9, 15), time(9, 22, 30))
TIMEOUT = (1, 7)


def _now():
    return datetime.now(SHANGHAI)


def _allowed(stamp, day):
    return (
        day == EXPECTED_DATE
        and stamp.date().isoformat() == day
        and WINDOW[0] <= stamp.time() <= WINDOW[1]
    )


class _Calendar:
    def __init__(self, conn):
        self.conn = conn

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def _official_session(cfg, stamp):
    uri = cfg.path("store.path").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
        store = _Calendar(conn)
        day = stamp.date().isoformat()
        cached = json.loads(store.get_meta("mairui_calendar:" + day[:4]) or "{}")
        if cached.get("expires", 0) <= stamp.timestamp() or not isinstance(
            cached.get("days"), list
        ):
            return None
        return day in cached["days"] and current_session(store, day) is True


def _private_file(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("unsafe output")
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, "ab")
    except BaseException:
        os.close(fd)
        raise


def _append(file, record):
    data = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if os.name == "posix":
        import fcntl

        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
    file.write((data + "\n").encode("utf-8"))
    file.flush()
    os.fsync(file.fileno())


def _get(url, auth):
    with requests.Session() as session:
        session.trust_env = False  # 本机请求不得由环境代理、netrc 或重定向转发。
        return session.get(
            url, headers={"Authorization": auth}, timeout=TIMEOUT, allow_redirects=False
        )


def _sensitive(payload):
    if isinstance(payload, list):
        return any(_sensitive(value) for value in payload)
    if isinstance(payload, dict):
        return any(
            str(key).lower()
            in {"authorization", "password", "token", "licence", "license", "api_key"}
            or _sensitive(value)
            for key, value in payload.items()
        )
    return False


def sample_once(
    expected_date,
    output,
    *,
    clock=_now,
    load_config=Config.load,
    calendar=_official_session,
    get=_get,
):
    def now():
        value = clock()
        if value.tzinfo is None:
            raise ValueError("clock requires timezone")
        return value.astimezone(SHANGHAI)

    start = now()
    if not _allowed(start, expected_date):
        return "skipped_window"
    with _private_file(output) as file:
        record = {
            "expected_date": expected_date,
            "timezone": "Asia/Shanghai",
            "started_at": start.isoformat(timespec="microseconds"),
            "endpoint": ENDPOINT,
            "query": QUERY,
            "request_url": None,
            "requested_at": None,
            "received_at": None,
            "http_status": None,
            "status": "configuration_error",
        }
        try:
            cfg = load_config()
            password = cfg.get("web.password") or ""
            port = int(cfg.get("web.port", 8080))
            if not 1 <= port <= 65535 or not isinstance(password, str) or not password:
                raise ValueError()
            url = f"http://127.0.0.1:{port}{ENDPOINT}?{QUERY}"
            record["request_url"] = url
            auth_token = base64.b64encode(("emscan:" + password).encode("utf-8")).decode("ascii")
            record["status"] = "calendar_unavailable"
            if calendar(cfg, start) is True:
                requested = now()
                if not _allowed(requested, expected_date):
                    record["status"] = "skipped_window"
                else:
                    record.update(
                        requested_at=requested.isoformat(timespec="microseconds"),
                        status="transport_error",
                    )
                    resp = get(url, "Basic " + auth_token)
                    record.update(
                        received_at=now().isoformat(timespec="microseconds"),
                        http_status=resp.status_code,
                        status="http_error",
                    )
                    if resp.status_code == 200:
                        record["status"] = "invalid_response"
                        raw = resp.content.decode("utf-8")
                        body = json.loads(raw)
                        if isinstance(body, dict) and isinstance(body.get("items"), list):
                            decoded = json.dumps(body, ensure_ascii=False, allow_nan=False)
                            secrets = (
                                password,
                                auth_token,
                                cfg.get("mairui.licence"),
                                cfg.get("summary.api_key"),
                            )
                            if _sensitive(body) or any(
                                secret in raw or secret in decoded for secret in secrets if secret
                            ):
                                record["status"] = "sensitive_response_omitted"
                            else:
                                record.update(
                                    status="sampled",
                                    response=body,
                                    response_raw=raw,
                                    response_sha256=hashlib.sha256(resp.content).hexdigest(),
                                )
        except Exception:
            # 配置/HTTP/JSON 异常正文可能含凭证。仅记录已知处理阶段。
            pass
        record["finished_at"] = now().isoformat(timespec="microseconds")
        _append(file, record)
        return record["status"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-date", required=True, choices=[EXPECTED_DATE])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sample-once", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        status = sample_once(args.expected_date, args.output)
    except Exception:
        status = "validation_record_error"
    print(status)
    return 0 if status in ("sampled", "skipped_window", "calendar_unavailable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
