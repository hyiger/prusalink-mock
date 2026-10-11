"""Fault rules on the printer port."""

from __future__ import annotations

import json
import time

import pytest

from httpraw import Conn
from prusalink_mock import MockPrinter
from prusalink_mock.faults import FaultError


def key(printer):
    return [("X-Api-Key", printer.password)]


def tools(printer, conn=None):
    if conn is not None:
        return conn.request("GET", "/api/v1/tools", key(printer))
    with Conn("127.0.0.1", printer.port) as c:
        return c.request("GET", "/api/v1/tools", key(printer))


def test_status_once(printer):
    printer.add_fault({"match": {"method": "GET", "path": "/api/v1/tools"}, "times": 1,
                       "action": {"type": "status", "status": 500, "message": "boom"}})
    with Conn("127.0.0.1", printer.port) as conn:
        r = tools(printer, conn)
        assert r.status == 500 and r.body == b"500: Internal Server Error\n\nboom\n"
        assert r.header("Connection") == "keep-alive"
        assert tools(printer, conn).status == 200
    [entry] = [e for e in printer.requests() if e["status"] == 500]
    assert entry["fault"] == 1


def test_status_with_custom_body(printer):
    printer.add_fault({"match": {"path": "/api/version"},
                       "action": {"type": "status", "status": 503, "body": "<h1>busy</h1>", "content_type": "text/html"}})
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("GET", "/api/version", key(printer))
    assert r.status == 503 and r.reason == "Service Temporarily Unavailable"
    assert r.header("Content-Type") == "text/html" and r.body == b"<h1>busy</h1>"


def test_status_on_upload_closes_and_drains(printer):
    printer.add_fault({"match": {"method": "PUT"}, "action": {"type": "status", "status": 409,
                                                              "message": "Another transfer in progress"}})
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("PUT", "/api/v1/files/usb/x.gcode", key(printer) + [("Content-Length", "4")], b"G28\n")
    assert r.status == 409 and r.closed and not r.reset and printer.files() == []


def test_skip_and_times(printer):
    printer.add_fault({"match": {"path": "/api/v1/tools"}, "skip": 1, "times": 1,
                       "action": {"type": "status", "status": 404}})
    assert [tools(printer).status for _ in range(3)] == [200, 404, 200]
    [rule] = printer.faults()
    assert rule["hits"] == 1 and rule["seen"] == 2


def test_match_on_auth(printer):
    printer.add_fault({"match": {"path_glob": "/api/*", "auth": "ok"}, "action": {"type": "status", "status": 500}})
    with Conn("127.0.0.1", printer.port) as conn:
        assert conn.request("GET", "/api/v1/tools").status == 401
        assert conn.request("GET", "/api/v1/tools", [("X-Api-Key", "bad")]).status == 401
        assert conn.request("GET", "/api/v1/tools", key(printer)).status == 500


def test_malformed_truncate(printer):
    printer.add_fault({"match": {"path": "/api/v1/tools"}, "action": {"type": "malformed", "mode": "truncate",
                                                                     "keep_bytes": 40}})
    r = tools(printer)
    assert r.status == 200 and r.chunked and len(r.body) == 40 and r.body.startswith(b'{"tools":{"1":')
    with pytest.raises(json.JSONDecodeError):
        json.loads(r.body)


def test_malformed_wrong_shape(printer):
    printer.add_fault({"match": {"path": "/api/v1/tools"}, "times": 1,
                       "action": {"type": "malformed", "mode": "wrong_shape"}})
    printer.add_fault({"match": {"path": "/api/v1/tools"}, "times": 1,
                       "action": {"type": "malformed", "mode": "wrong_shape", "body": {"tools": [1, 2]}}})
    assert isinstance(json.loads(tools(printer).body), list)
    assert tools(printer).body == b'{"tools":[1,2]}'


def test_malformed_html(printer):
    printer.add_fault({"match": {"path": "/api/v1/tools"}, "action": {"type": "malformed", "mode": "html"}})
    r = tools(printer)
    assert r.status == 200 and r.header("Content-Type").startswith("text/html") and b"<html>" in r.body


def test_delay(printer):
    printer.add_fault({"match": {"path": "/api/v1/tools"}, "action": {"type": "delay", "ms": 300}})
    start = time.monotonic()
    assert tools(printer).status == 200
    assert time.monotonic() - start >= 0.29


def test_hang_is_capped(printer):
    printer.add_fault({"match": {"path": "/api/v1/tools"}, "action": {"type": "hang", "seconds": 0.5}})
    with Conn("127.0.0.1", printer.port) as conn:
        start = time.monotonic()
        conn.send(f"GET /api/v1/tools HTTP/1.1\r\nX-Api-Key: {printer.password}\r\n\r\n".encode())
        assert conn.read_to_eof(timeout=10) == b""
        assert 0.4 <= time.monotonic() - start < 5


def test_hang_is_canceled_on_shutdown():
    printer = MockPrinter().start()
    printer.add_fault({"match": {}, "action": {"type": "hang"}})
    conn = Conn("127.0.0.1", printer.port)
    conn.send(f"GET /api/v1/tools HTTP/1.1\r\nX-Api-Key: {printer.password}\r\n\r\n".encode())
    time.sleep(0.2)
    start = time.monotonic()
    printer.stop()
    assert time.monotonic() - start < 5 and printer.stopped
    assert conn.read_to_eof(timeout=5) == b""
    conn.close()


def test_drop_before_response(printer):
    printer.add_fault({"match": {"path": "/api/v1/tools"}, "action": {"type": "drop", "when": "before_response"}})
    with Conn("127.0.0.1", printer.port) as conn:
        conn.send(f"GET /api/v1/tools HTTP/1.1\r\nX-Api-Key: {printer.password}\r\n\r\n".encode())
        assert conn.read_to_eof() == b"" and not conn.reset


def test_drop_mid_body(printer):
    printer.add_fault({"match": {"path": "/api/v1/tools"},
                       "action": {"type": "drop", "when": "mid_body", "after_bytes": 10}})
    with Conn("127.0.0.1", printer.port) as conn:
        conn.send(f"GET /api/v1/tools HTTP/1.1\r\nX-Api-Key: {printer.password}\r\n\r\n".encode())
        data = conn.read_to_eof()
        assert not conn.reset
    head, _, rest = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK") and len(rest) == 10


@pytest.mark.parametrize("rule", [
    {"action": {"type": "explode"}},
    {"match": {"verb": "GET"}, "action": {"type": "delay", "ms": 1}},
    {"match": {"auth": "maybe"}, "action": {"type": "delay", "ms": 1}},
    {"action": {"type": "hang", "seconds": 31}},
    {"action": {"type": "status", "status": 99}},
    {"action": {"type": "malformed", "mode": "garbage"}},
    {"action": {"type": "drop", "when": "after_headers"}},
    {"times": 0, "action": {"type": "delay", "ms": 1}},
])
def test_invalid_rules(printer, rule):
    with pytest.raises(FaultError):
        printer.add_fault(rule)


def test_faults_do_not_block_other_connections(printer):
    printer.add_fault({"match": {"path": "/api/v1/tools"}, "times": 1, "action": {"type": "hang", "seconds": 5}})
    hung = Conn("127.0.0.1", printer.port)
    hung.send(f"GET /api/v1/tools HTTP/1.1\r\nX-Api-Key: {printer.password}\r\n\r\n".encode())
    try:
        assert tools(printer).status == 200
    finally:
        hung.close()
