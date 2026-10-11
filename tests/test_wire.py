"""Wire-level behavior on raw sockets: framing, connection handling and error pages."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from httpraw import Conn

NO_EXTRA_HEADERS = ("date", "server", "access-control-allow-origin")


def key(printer):
    return [("X-Api-Key", printer.password)]


def test_keep_alive_and_chunked_json(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        first = conn.request("GET", "/api/version", key(printer))
        second = conn.request("GET", "/api/v1/tools", key(printer))
    for r in (first, second):
        assert r.status == 200 and r.chunked and not r.closed
        assert r.header_lines()[1:] == ["Content-Type: application/json", "Connection: keep-alive",
                                        "Transfer-Encoding: chunked"]
        assert not any(n.lower() in NO_EXTRA_HEADERS for n, _ in r.headers)


def test_chunk_sizes_follow_the_firmware_buffer(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("GET", "/api/v1/tools", key(printer))
    # 8 tools do not fit one 1024-byte packet next to the headers.
    assert len(r.chunk_sizes) == 3 and r.chunk_sizes[-1] == "0"
    assert all(len(s) == 4 and s == s.upper() for s in r.chunk_sizes[:-1])
    assert int(r.chunk_sizes[0], 16) == 1024 - len(r.head) - 8


def test_pipelined_requests(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        req = f"GET /api/version HTTP/1.1\r\nX-Api-Key: {printer.password}\r\n\r\n".encode()
        conn.send(req + req.replace(b"/api/version", b"/api/v1/info"))
        assert conn.read_response().body.startswith(b'{"api"')
        assert conn.read_response().body.startswith(b'{"nozzle_diameter"')


@pytest.mark.parametrize("version,headers,keep", [
    ("HTTP/1.1", [], True),
    ("HTTP/1.1", [("Connection", "close")], False),
    ("HTTP/1.0", [], False),
    ("HTTP/1.0", [("Connection", "keep-alive")], True),
    ("HTTP/1.1", [("Connection", "Keep-Alive, Upgrade")], True),
])
def test_connection_handling(printer, version, headers, keep):
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("GET", "/api/v1/storage", key(printer) + headers, version=version)
        assert r.chunked == keep and r.closed != keep
        assert r.header("Connection") == ("keep-alive" if keep else "close")
        assert r.body.startswith(b'{"storage_list":')
        assert r.header("Content-Length") is None


def test_lf_only_line_endings(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        conn.send(f"GET /api/version HTTP/1.1\nX-Api-Key: {printer.password}\n\n".encode())
        assert conn.read_response().status == 200


def test_uri_too_long(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        ok = conn.request("GET", "/" + "a" * 167)
        assert ok.status == 404
        long = conn.request("GET", "/" + "a" * 168)
        assert long.status == 414 and long.body == b"414: URI Too Long\n\n\n"
        assert long.header("Connection") == "keep-alive"
        assert conn.request("GET", "/api/version", key(printer)).status == 200


def test_uri_too_long_comes_before_auth(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        assert conn.request("GET", "/api/v1/" + "x" * 170).status == 414


def test_length_required(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("PUT", "/api/v1/files/usb/x.gcode", key(printer) + [("Transfer-Encoding", "chunked")],
                         body=b"4\r\nG28\n\r\n0\r\n\r\n")
    assert r.status == 411 and r.closed and not r.reset
    assert r.body == b"411: Length Required\n\n\n"


def test_head_on_get_only_route_is_405_with_close(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("HEAD", "/api/version", key(printer))
    assert r.status == 405 and r.closed
    assert r.header_lines() == ["HTTP/1.1 405 Method Not Allowed", "Content-Type: text/plain", "Connection: close",
                                "Content-Length: 26"]
    assert r.body == b"405: Method Not Allowed\n\n\n"


def test_post_on_get_only_route(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("POST", "/api/v1/tools", key(printer) + [("Content-Length", "2")], body=b"{}")
    assert r.status == 405 and r.closed and not r.reset


def test_unknown_method_is_405_and_keeps_the_connection(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("OPTIONS", "/api/version")
        assert r.status == 405 and r.body == b"405: Method Not Allowed\n\nUnrecognized method\n"
        assert r.header("Connection") == "keep-alive"
        assert conn.request("get", "/api/version", key(printer)).status == 200  # methods are case-insensitive


def test_auth_comes_before_routing(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        assert conn.request("GET", "/api/v1/nope").status == 401
        assert conn.request("GET", "/api/v1/nope", key(printer)).status == 404
        assert conn.request("GET", "/api/nope", key(printer)).status == 404
        assert conn.request("GET", "/usb/x.gcode").status == 401
        assert conn.request("GET", "/thumb/s/usb/x.gcode").status == 401
        assert conn.request("GET", "/nope").status == 404


@pytest.mark.parametrize("target", ["/api/v1/tools?x", "/api/v1/tools/", "/api/v1/Tools", "/api//v1/tools"])
def test_api_routes_match_the_raw_target_exactly(printer, target):
    with Conn("127.0.0.1", printer.port) as conn:
        assert conn.request("GET", target, key(printer)).status == 404


def test_job_is_204_for_any_method_without_a_job(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        for method in ("GET", "DELETE", "HEAD"):
            r = conn.request(method, "/api/v1/job", key(printer))
            assert r.status == 204
            assert r.header_lines() == ["HTTP/1.1 204 No Content", "Content-Type: text/plain",
                                        "Connection: keep-alive", "Content-Length: 0"]


def test_index_page_needs_no_auth(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("GET", "/")
        assert r.status == 200 and r.header("Content-Type") == "text/html; charset=utf-8"
        assert b"PrusaLink mock: indx-hy8" in r.body and b"Overture" in r.body
        assert conn.request("GET", "/?x=1").status == 200
        assert conn.request("HEAD", "/").status == 404


def test_settings_stub(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("GET", "/api/settings", key(printer))
    assert r.body == b'{"printer": {}}' and r.header("Content-Length") == "15"


def test_not_modeled_routes_say_so(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("GET", "/api/printer", key(printer))
    assert r.status == 501 and b"Not modeled by prusalink-mock" in r.body


def test_bad_request_line_closes(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        conn.send(b"THIS IS NOT HTTP\r\n\r\n")
        r = conn.read_response()
    assert r.status == 400 and r.closed


def test_boundary_too_long(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("POST", "/api/files/local", key(printer) + [
            ("Content-Type", "multipart/form-data; boundary=" + "b" * 200), ("Content-Length", "0")])
    assert r.status == 431


def test_never_sends_100_continue(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        head = (f"PUT /api/v1/files/usb/expect.gcode HTTP/1.1\r\nX-Api-Key: {printer.password}\r\n"
                f"Expect: 100-continue\r\nContent-Length: 4\r\n\r\n").encode()
        conn.send(head)
        conn.sock.settimeout(0.5)
        with pytest.raises(socket.timeout):
            conn.sock.recv(1)
        conn.sock.settimeout(15)
        conn.send(b"G28\n")
        r = conn.read_response()
    assert r.status == 201


def test_error_close_lingers_so_the_client_sees_the_status(printer):
    """A rejected 2 MB PUT: the client keeps sending, and still reads the 401."""
    body = b"G1 X0\n" * (2 * 1024 * 1024 // 6)
    errors = []
    with Conn("127.0.0.1", printer.port) as conn:
        conn.send((f"PUT /api/v1/files/usb//big.gcode HTTP/1.1\r\nX-Api-Key: wrong\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode())

        def sender():
            try:
                conn.sock.sendall(body)
            except OSError as exc:
                errors.append(exc)

        t = threading.Thread(target=sender)
        t.start()
        r = conn.read_response()
        t.join(30)
    assert r.status == 401 and r.closed and not r.reset
    assert r.header("WWW-Authenticate") == 'ApiKey realm="Printer API"'
    assert not errors


def test_idle_connection_is_kept_open(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        assert conn.request("GET", "/api/version", key(printer)).status == 200
        time.sleep(0.3)
        assert conn.request("GET", "/api/version", key(printer)).status == 200
