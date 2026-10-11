"""PUT /api/v1/files/usb/... as PrusaSlicer sends it, and the firmware's answers."""

from __future__ import annotations

import json
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
    ("/api/v1/files/usb/a%3Fb.gcode", 422, b"Filename contains invalid characters"),
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
