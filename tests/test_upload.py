"""PUT /api/v1/files/usb/... as PrusaSlicer sends it, and the firmware's answers."""

from __future__ import annotations

import json
import socket
import time

import pytest

from httpraw import Conn
from prusalink_mock import MockPrinter
from prusalink_mock.auth import DigestClient


def put(printer, target, body=b"G28\n", headers=(), password=None, accept_json=False):
    hdrs = [("X-Api-Key", printer.password if password is None else password),
            ("Content-Type", "text/x.gcode"), ("Content-Length", str(len(body)))] + list(headers)
    if accept_json:
        hdrs.append(("Accept", "application/json"))
    with Conn("127.0.0.1", printer.port) as conn:
        return conn.request("PUT", target, hdrs, body)


def get(printer, target, method="GET"):
    with Conn("127.0.0.1", printer.port) as conn:
        return conn.request(method, target, [("X-Api-Key", printer.password)], no_body=method == "HEAD")


def test_upload_like_prusaslicer(printer):
    r = put(printer, "/api/v1/files/usb//a%20b.gcode", b"G28\nG1 X10\n", [("Overwrite", "?1")])
    assert r.status == 201 and r.closed and not r.chunked
    assert r.header_lines() == ["HTTP/1.1 201 Created", "Content-Type: application/json", "Connection: close",
                                "Read-Only: false", "Currently-Printing: false"]
    info = json.loads(r.body)
    assert list(info) == ["name", "ro", "type", "m_timestamp", "size", "refs", "display_name"]
    assert info["name"] == "AB~1.GCO" and info["display_name"] == "a b.gcode" and info["size"] == 11
    assert info["refs"] == {"icon": "/thumb/s/usb/AB~1.GCO", "thumbnail": "/thumb/l/usb/AB~1.GCO",
                            "download": "/usb/AB~1.GCO"}
    assert printer.file_bytes("a b.gcode") == b"G28\nG1 X10\n"
    [entry] = [e for e in printer.requests() if e["method"] == "PUT"]
    assert entry["upload"] == {"stored_as": "/usb/a b.gcode", "sfn_path": "/usb/AB~1.GCO", "overwrite": True,
                               "print_after_upload": False}
    assert entry["body_len"] == 11 and entry["path"] == "/api/v1/files/usb/a%20b.gcode"


def test_plus_decodes_to_space(printer):
    assert put(printer, "/api/v1/files/usb/a+b.gcode").status == 201
    assert printer.files()[0]["path"] == "/usb/a b.gcode"


def test_existing_file_needs_overwrite(printer):
    assert put(printer, "/api/v1/files/usb/x.gcode").status == 201
    r = put(printer, "/api/v1/files/usb/X.GCODE", b"other")
    assert r.status == 409 and r.body == b"409: Conflict\n\nFile already exists\n" and r.closed and not r.reset
    r = put(printer, "/api/v1/files/usb/x.gcode", b"new", [("Overwrite", "?1")])
    assert r.status == 201 and printer.file_bytes("x.gcode") == b"new"
    assert put(printer, "/api/v1/files/usb/x.gcode", b"no", [("Overwrite", "1")]).status == 409


def test_json_errors_before_the_body_is_read(printer):
    r = put(printer, "/api/v1/files/usb/x.txt", accept_json=True)
    assert r.status == 415 and r.body == b'{"title": "415: Unsupported Media Type","message":"Not a GCODE"}'


@pytest.mark.parametrize("name", ["x.g", "x.gc", "x.gco", "x.gcode", "x.bgc", "x.bgcode", "X.GCODE"])
def test_printable_extensions(printer, name):
    assert put(printer, f"/api/v1/files/usb/{name}").status == 201


@pytest.mark.parametrize("target,status,message", [
    ("/api/v1/files/usb/notes.txt", 415, b"Not a GCODE"),
    ("/api/v1/files/usb/a%3Fb.gcode", 404, b"Destination directory does not exist"),
    ("/api/v1/files/usb/a%FFb.gcode", 404, b"Destination directory does not exist"),
    ("/api/v1/files/usb/../x.gcode", 404, b""),
    ("/api/v1/files/usb/%zz.gcode", 404, b""),
    ("/api/v1/files/local/x.gcode", 403, b""),
    ("/api/v1/filesX/usb/x.gcode", 403, b""),
    ("/api/v1/files/usb/" + "n" * 95 + ".gcode", 414, b""),
])
def test_rejected_uploads(printer, target, status, message):
    r = put(printer, target)
    assert r.status == status and r.closed and not r.reset
    assert message in r.body
    assert printer.files() == []


def test_zero_bytes_is_not_implemented(printer):
    r = put(printer, "/api/v1/files/usb/x.gcode", b"")
    assert r.status == 501 and r.body == b"501: Not Implemented\n\n0-sized files aren't implemented\n"


def test_folders_are_created_with_short_names(printer):
    r = put(printer, "/api/v1/files/usb/My%20Prints/benchy_0.4n.bgcode", b"GCDE")
    assert r.status == 201
    info = json.loads(r.body)
    assert info["refs"]["download"] == "/usb/MYPRIN~1/BENCHY~1.BGC" and info["display_name"] == "benchy_0.4n.bgcode"
    assert put(printer, "/api/v1/files/usb/My%20Prints/benchy_0.4n.bgcode/x.gcode").status == 409


def test_short_name_tails_count_up(printer):
    names = [json.loads(put(printer, f"/api/v1/files/usb/benchy{n}.bgcode").body)["name"] for n in ("", "2", "3")]
    assert names == ["BENCHY~1.BGC", "BENCHY~2.BGC", "BENCHY~3.BGC"]


def test_file_info_get_head_and_delete(printer):
    put(printer, "/api/v1/files/usb/a%20b.gcode")
    r = get(printer, "/api/v1/files/usb/a%20b.gcode")
    assert r.status == 200 and r.chunked and r.header("Read-Only") == "false"
    assert json.loads(r.body)["name"] == "AB~1.GCO"
    assert get(printer, "/api/v1/files/usb/AB~1.GCO").status == 200  # the short name works too
    head = get(printer, "/api/v1/files/usb/a%20b.gcode", "HEAD")
    assert head.status == 200 and head.header("Content-Length") is None and head.header("Transfer-Encoding") is None
    assert get(printer, "/api/v1/files/usb/a%20b.gcode", "DELETE").status == 204
    assert get(printer, "/api/v1/files/usb/a%20b.gcode").status == 404
    assert get(printer, "/api/v1/files/usb/").status == 501  # folder listings are not modeled


def test_create_folder(printer):
    r = put(printer, "/api/v1/files/usb/new", b"", [("Create-Folder", "?1")])
    assert r.status == 201 and r.body == b"201: Created\n\n\n"
    r = put(printer, "/api/v1/files/usb/new", b"", [("Create-Folder", "?1")])
    assert r.status == 409 and b"Already exists" in r.body


def test_print_after_upload_is_recorded(printer):
    r = put(printer, "/api/v1/files/usb/p.gcode", headers=[("Print-After-Upload", "?1")])
    assert r.status == 201 and printer.files()[0]["print_after_upload"] is True


def test_print_after_upload_while_printing():
    with MockPrinter("indx-printing") as printer:
        r = put(printer, "/api/v1/files/usb/p.gcode", headers=[("Print-After-Upload", "true")])
        assert r.status == 409 and r.body == b"409: Conflict\n\nCan't print right now\n"
        assert printer.files()[0]["path"] == "/usb/p.gcode"  # stored anyway


def test_storage_missing(printer):
    printer.patch_state({"storage": {"available": False}})
    r = put(printer, "/api/v1/files/usb/x.gcode")
    assert r.status == 507 and b"Failed to write to location" in r.body


def test_one_transfer_at_a_time(printer):
    with Conn("127.0.0.1", printer.port) as slow:
        slow.send((f"PUT /api/v1/files/usb/slow.gcode HTTP/1.1\r\nX-Api-Key: {printer.password}\r\n"
                   f"Content-Length: 10\r\n\r\nG28").encode())
        deadline = time.monotonic() + 10
        while printer._call(lambda: printer.core.upload_path) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        r = put(printer, "/api/v1/files/usb/fast.gcode")
        assert r.status == 409 and b"Another transfer in progress" in r.body
        slow.send(b"\nG1 X1\n")
        assert slow.read_response().status == 201


def test_legacy_multipart_post_is_gone(printer):
    body = b"--xx\r\nContent-Disposition: form-data; name=\"print\"\r\n\r\nfalse\r\n--xx--\r\n"
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("POST", "/api/files/local", [
            ("X-Api-Key", printer.password), ("Content-Type", "multipart/form-data; boundary=xx"),
            ("Content-Length", str(len(body)))], body)
    assert r.status == 410 and r.body == b"410: Gone\n\n\n" and r.closed and not r.reset


def test_start_print_of_a_stored_file(printer):
    put(printer, "/api/v1/files/usb/p.gcode")
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("POST", "/api/v1/files/usb/p.gcode", [("X-Api-Key", printer.password)])
        assert r.status == 204 and r.closed
    with Conn("127.0.0.1", printer.port) as conn:
        assert conn.request("POST", "/api/v1/files/usb/q.gcode", [("X-Api-Key", printer.password)]).status == 404


def test_digest_upload_two_legs(printer):
    target = "/api/v1/files/usb//digest.gcode"
    with Conn("127.0.0.1", printer.port) as conn:
        first = conn.request("PUT", target, [("Content-Length", "0"), ("Overwrite", "?1")])
    assert first.status == 401 and first.closed
    auth = DigestClient(printer.password).header("PUT", target, first.nonce)
    with Conn("127.0.0.1", printer.port) as conn:
        second = conn.request("PUT", target, [("Authorization", auth), ("Content-Length", "4"), ("Overwrite", "?1")],
                              b"G28\n")
    assert second.status == 201 and printer.file_bytes("digest.gcode") == b"G28\n"


GOODBYE = b"HTTP/1.1 408 Request Timeout\r\n\r\n\x00"


@pytest.fixture
def short_timeouts(monkeypatch):
    from prusalink_mock import wire
    monkeypatch.setattr(wire, "ACTIVE_TIMEOUT_S", 0.3)


def _upload_head(printer, name="a.gcode", length=100, extra=""):
    return (f"PUT /api/v1/files/usb/{name} HTTP/1.1\r\nX-Api-Key: {printer.password}\r\n"
            f"Content-Length: {length}\r\n{extra}\r\n").encode()


@pytest.mark.parametrize("body", [b"", b"G28\nG1"], ids=["before-body", "mid-body"])
def test_stalled_upload_gets_the_bare_408(printer, short_timeouts, body):
    """The transfer's "Connection timeout" result is dropped; only the goodbye goes out (server.cpp:709-730)."""
    with Conn("127.0.0.1", printer.port) as conn:
        conn.send(_upload_head(printer) + body)
        assert conn.read_to_eof() == GOODBYE
    assert printer.files() == []
    entry = printer.requests()[-1]
    assert entry["status"] == 408 and entry["error"] == "body timed out"


def test_client_close_before_the_body(printer):
    """GcodeUpload::step with terminated_by_client (gcode_upload.cpp:380-383)."""
    with Conn("127.0.0.1", printer.port) as conn:
        conn.send(_upload_head(printer, extra="Accept: application/json\r\n"))
        conn.sock.shutdown(socket.SHUT_WR)
        r = conn.read_response()
    assert r.status == 400 and r.closed
    assert r.body == b'{"title": "400: Bad Request","message":"Truncated body"}'


def test_client_close_mid_body(printer):
    """The transfer slot sees the close (server.cpp:785-795, gcode_upload.cpp:302-303)."""
    with Conn("127.0.0.1", printer.port) as conn:
        conn.send(_upload_head(printer, extra="Accept: application/json\r\n") + b"G28\nG1")
        conn.sock.shutdown(socket.SHUT_WR)
        r = conn.read_response()
    assert r.status == 400 and r.closed and r.header("Content-Type") == "text/plain"
    assert r.body == b"400: Bad Request\n\nConnection closed by client\n"
    assert printer.files() == []


@pytest.mark.parametrize("folder", ["a%3Ab", "a%3Fb", "a%FFb", "ok/a%2Ab"])
@pytest.mark.parametrize("overwrite", [False, True])
def test_folder_name_fatfs_refuses(printer, folder, overwrite):
    """make_dirs fails before the rename is tried (gcode_upload.cpp:266-268, path_utils.cpp:35-51)."""
    r = put(printer, f"/api/v1/files/usb/{folder}/x.gcode", headers=[("Overwrite", "?1")] if overwrite else [])
    assert r.status == 500 and r.body == b"500: Internal Server Error\n\nFailed to create a folder\n"
    assert printer.files() == []


def test_file_name_fatfs_refuses_with_overwrite(printer):
    """With Overwrite the name is only tried at the rename: ENOTDIR, plain text (gcode_upload.cpp:224-238)."""
    r = put(printer, "/api/v1/files/usb/new%20dir/a%3Cb.gcode", headers=[("Overwrite", "?1")], accept_json=True)
    assert r.status == 404 and r.header("Content-Type") == "text/plain"
    assert r.body == b"404: Not Found\n\nDestination directory does not exist\n"


@pytest.mark.parametrize("name", ["a%01b.gcode", "a%5Cb.gcode", "a\\b.gcode"])
def test_names_fatfs_would_change_are_not_modeled(printer, name):
    r = put(printer, f"/api/v1/files/usb/{name}")
    assert r.status == 501 and b"Not modeled by prusalink-mock" in r.body and r.closed and not r.reset


def test_delete_during_an_overwriting_upload(printer):
    """The PUT writes a temporary file, so its destination can still be deleted (print_utils.cpp:84-117)."""
    assert put(printer, "/api/v1/files/usb/x.gcode", b"old\n").status == 201
    with Conn("127.0.0.1", printer.port) as slow:
        slow.send(_upload_head(printer, "x.gcode", 8, "Overwrite: ?1\r\n") + b"new\n")
        deadline = time.monotonic() + 10
        while printer._call(lambda: printer.core.upload_path) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert get(printer, "/api/v1/files/usb/x.gcode", "DELETE").status == 204
        assert printer.files() == []
        slow.send(b"G28\n")
        assert slow.read_response().status == 201
    assert printer.file_bytes("x.gcode") == b"new\nG28\n"


def test_later_upload_after_a_reboot_is_newer(printer):
    """Reboots restart uptime, not the wall clock that stamps files."""
    printer.advance_clock(3600)
    before = json.loads(put(printer, "/api/v1/files/usb/before.gcode").body)["m_timestamp"]
    printer.reboot()
    after = json.loads(put(printer, "/api/v1/files/usb/after.gcode").body)["m_timestamp"]
    assert after >= before


def test_log_has_the_entry_when_the_client_has_the_response(printer):
    """Error-close responses linger until the client closes; the entry must not wait for that."""
    put(printer, "/api/v1/files/usb/p.gcode")
    for _ in range(20):
        with Conn("127.0.0.1", printer.port) as conn:
            r = conn.request("POST", "/api/v1/files/usb/p.gcode", [("X-Api-Key", printer.password)])
            assert r.status == 204
            entries = [e for e in printer.requests() if e["route"] == "v1.files.print"]
            assert entries and entries[-1]["print_requested"] == "/usb/p.gcode"
            printer.clear_requests()
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("PUT", "/api/v1/files/usb/bad.txt", [("X-Api-Key", printer.password),
                                                              ("Content-Length", "4")], b"G28\n")
        assert r.status == 415 and [e["status"] for e in printer.requests()] == [415]


def test_http_client_sees_the_print_entry(printer):
    import http.client
    put(printer, "/api/v1/files/usb/p.gcode")
    for _ in range(20):
        c = http.client.HTTPConnection("127.0.0.1", printer.port, timeout=15)
        c.request("POST", "/api/v1/files/usb/p.gcode", headers={"X-Api-Key": printer.password})
        assert c.getresponse().status == 204
        c.close()
        assert [e["status"] for e in printer.requests() if e["route"] == "v1.files.print"]
        printer.clear_requests()
