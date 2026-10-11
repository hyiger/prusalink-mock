"""PUT /api/v1/files/usb/... as PrusaSlicer sends it, and the firmware's answers."""

from __future__ import annotations

import json
import socket
import sys
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


def test_a_short_name_stands_for_its_file(printer):
    """FatFS matches each path component against the long and the short name (dir_find)."""
    assert json.loads(put(printer, "/api/v1/files/usb/foobar-long.gcode", b"one").body)["name"] == "FOOBAR~1.GCO"
    r = put(printer, "/api/v1/files/usb/FOOBAR~1.GCO", b"two")
    assert r.status == 409 and b"File already exists" in r.body
    assert [f["path"] for f in printer.files()] == ["/usb/foobar-long.gcode"]

    # Overwriting through the short name replaces the file with one named as given
    assert put(printer, "/api/v1/files/usb/foobar~1.gco", b"two", [("Overwrite", "?1")]).status == 201
    assert [(f["path"], f["sfn_path"]) for f in printer.files()] == [("/usb/foobar~1.gco", "/usb/FOOBAR~1.GCO")]
    assert printer.file_bytes("FOOBAR~1.GCO") == b"two"


def test_a_short_folder_name_stands_for_its_folder(printer):
    assert put(printer, "/api/v1/files/usb/My%20Prints/a.gcode").status == 201
    r = put(printer, "/api/v1/files/usb/MYPRIN~1/b.gcode")
    assert r.status == 201 and json.loads(r.body)["refs"]["download"] == "/usb/MYPRIN~1/B~1.GCO"
    assert sorted(f["path"] for f in printer.files()) == ["/usb/My Prints/a.gcode", "/usb/My Prints/b.gcode"]
    assert get(printer, "/api/v1/files/usb/My%20Prints/b.gcode").status == 200
    assert get(printer, "/api/v1/files/usb/MYPRIN~1/A~1.GCO", "DELETE").status == 204
    assert [f["path"] for f in printer.files()] == ["/usb/My Prints/b.gcode"]


def test_create_folder_in_a_folder_named_by_its_short_name(printer):
    assert put(printer, "/api/v1/files/usb/My%20Prints/a.gcode").status == 201
    assert put(printer, "/api/v1/files/usb/MYPRIN~1/sub", b"", [("Create-Folder", "?1")]).status == 201
    assert put(printer, "/api/v1/files/usb/My%20Prints/sub/b.gcode").status == 201
    assert sorted(f["path"] for f in printer.files()) == ["/usb/My Prints/a.gcode", "/usb/My Prints/sub/b.gcode"]


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


# --- FatFS names (ff.c create_name, follow_path, dir_register) -------------------

def _fill_short_names(printer, folder: str, lfn: str) -> None:
    """Take all 99 numbered short names FatFS would try for ``lfn`` in ``folder``."""
    from prusalink_mock import sfn
    fn, lossy = sfn._base(lfn)
    assert lossy
    names = [sfn._join(sfn._numbered(fn, lfn, seq)) for seq in range(1, 100)]

    def fill():
        for name in names:
            assert printer.core.storage.store(f"{folder}/{name}", b"x", 0, print_after_upload=False,
                                              overwrite=False) is not None
    printer._call(fill)


def test_no_short_name_left_for_an_upload(printer):
    """dir_register gives up after ~99 (FR_DENIED, EACCES): try_rename's 422 (gcode_upload.cpp:224-238)."""
    _fill_short_names(printer, "/usb", "collide.gcode")
    r = put(printer, "/api/v1/files/usb/collide.gcode", accept_json=True)  # the check file's rename
    assert r.status == 422 and r.closed and not r.reset
    assert r.body == b'{"title": "422: Unprocessable Entity","message":"Filename contains invalid characters"}'
    r = put(printer, "/api/v1/files/usb/collide.gcode", headers=[("Overwrite", "?1")])  # the final rename
    assert r.status == 422 and r.body == b"422: Unprocessable Entity\n\nFilename contains invalid characters\n"
    assert len(printer.files()) == 99
    assert [e["status"] for e in printer.requests()] == [422, 422]


def test_no_short_name_left_for_a_folder(printer):
    _fill_short_names(printer, "/usb", "collide dir")
    r = put(printer, "/api/v1/files/usb/collide%20dir", b"", [("Create-Folder", "?1")])
    assert r.status == 500 and b"Error creating directory" in r.body
    r = put(printer, "/api/v1/files/usb/collide%20dir/x.gcode")  # make_dirs (path_utils.cpp:35-51)
    assert r.status == 500 and b"Failed to create a folder" in r.body
    assert all(e["status"] == 500 for e in printer.requests())


def test_a_dot_folder_is_the_folder_itself(printer):
    r = put(printer, "/api/v1/files/usb/./x.gcode")
    assert r.status == 201 and json.loads(r.body)["refs"]["download"] == "/usb/X~1.GCO"
    assert [f["path"] for f in printer.files()] == ["/usb/x.gcode"]
    assert get(printer, "/api/v1/files/usb/x.gcode").status == 200
    assert put(printer, "/api/v1/files/usb/sub/./y.gcode").status == 201
    assert get(printer, "/api/v1/files/usb/sub/y.gcode").status == 200


@pytest.mark.parametrize("folder", ["...", "%20", ".%20.", "a/%20%20"])
def test_a_folder_name_with_nothing_left_is_refused(printer, folder):
    """Trailing dots and spaces go; an empty name is FR_INVALID_NAME (ff.c:2851-2858)."""
    r = put(printer, f"/api/v1/files/usb/{folder}/x.gcode")
    assert r.status == 500 and b"Failed to create a folder" in r.body
    r = put(printer, f"/api/v1/files/usb/{folder}", b"", [("Create-Folder", "?1")])
    assert r.status == 500 and b"Error creating directory" in r.body
    assert printer.files() == [] and printer._call(lambda: len(printer.core.storage.dirs)) <= 2


@pytest.mark.parametrize("target", ["/api/v1/files/usb/.", "/api/v1/files/usb/../", "/api/v1/files/usb/dir/.",
                                    "/api/v1/files/usb/dir/sub/../"])
def test_create_folder_on_a_dot_entry_exists(printer, target):
    assert put(printer, "/api/v1/files/usb/dir/sub", b"", [("Create-Folder", "?1")]).status == 500  # no dir yet
    assert put(printer, "/api/v1/files/usb/dir", b"", [("Create-Folder", "?1")]).status == 201
    assert put(printer, "/api/v1/files/usb/dir/sub", b"", [("Create-Folder", "?1")]).status == 201
    r = put(printer, target, b"", [("Create-Folder", "?1")])
    assert r.status == 409 and b"Already exists" in r.body


def test_dot_entry_after_a_missing_folder(printer):
    r = put(printer, "/api/v1/files/usb/nope/../", b"", [("Create-Folder", "?1")])
    assert r.status == 500 and b"Error creating directory" in r.body


@pytest.mark.parametrize("variant", ["dir.", "dir%20", "DIR..", "dir%20."])
def test_trailing_dots_and_spaces_name_the_same_folder(printer, variant):
    assert put(printer, "/api/v1/files/usb/dir/a.gcode").status == 201
    r = put(printer, f"/api/v1/files/usb/{variant}/b.gcode")
    assert r.status == 201 and json.loads(r.body)["refs"]["download"] == "/usb/DIR/B~1.GCO"
    assert get(printer, "/api/v1/files/usb/DIR/B~1.GCO").status == 200
    assert get(printer, "/api/v1/files/usb/dir/b.gcode").status == 200
    assert sorted(f["path"] for f in printer.files()) == ["/usb/dir/a.gcode", "/usb/dir/b.gcode"]
    assert get(printer, "/api/v1/files/usb/dir/b.gcode.").status == 200


@pytest.mark.parametrize("name,status", [
    ("a*b", 500), ("q%3fr", 500), ("c%3ad", 500), ("lt%3cgt%3e", 500), ("del%7f", 500), ("p%22q", 500),
    ("ok/a%7cb", 500), ("x%01y", 501), ("m%5cn", 501), ("m\\n", 501),
])
def test_create_folder_checks_names_like_uploads(printer, name, status):
    """mkdir with a character FatFS refuses fails (FR_INVALID_NAME, ff.c:2822-2828)."""
    r = put(printer, f"/api/v1/files/usb/{name}", b"", [("Create-Folder", "?1")])
    assert r.status == status
    assert printer._call(lambda: len(printer.core.storage.dirs)) == 1


def test_names_compare_one_character_at_a_time(printer):
    """cmp_lfn folds each UTF-16 unit with ff_wtoupper; ß has no one-letter capital."""
    assert put(printer, "/api/v1/files/usb/stra%C3%9Fe.gcode", b"one").status == 201
    assert put(printer, "/api/v1/files/usb/strasse.gcode", b"two").status == 201
    assert put(printer, "/api/v1/files/usb/%EF%AC%81le.gcode", b"three").status == 201
    assert put(printer, "/api/v1/files/usb/file.gcode", b"four").status == 201
    assert put(printer, "/api/v1/files/usb/STRASSE.GCODE", b"five").status == 409
    assert json.loads(get(printer, "/api/v1/files/usb/strasse.gcode").body)["size"] == 3
    assert printer.file_bytes("straße.gcode") == b"one" and printer.file_bytes("strasse.gcode") == b"two"


@pytest.mark.parametrize("length,body,status", [("4294967299", b"G1\n", 201), ("4294967297", b"G", 201),
                                                ("4294967296", b"", 501)])
def test_content_length_is_32_bits(printer, length, body, status):
    """size_t content_length on the STM32 (req_parser.h:54, req_parser.cpp:175-180)."""
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("PUT", "/api/v1/files/usb/w.gcode", [("X-Api-Key", printer.password),
                                                              ("Content-Length", length)], body)
    assert r.status == status
    if status == 201:
        assert printer.file_bytes("w.gcode") == body


@pytest.mark.parametrize("escape,status", [
    ("%-F%80%80%80", 201),  # 0xF1 0x80 0x80 0x80, U+40000
    ("a%-1b", 404),  # 0xFF, not UTF-8, so FatFS refuses the name
    ("a%+4b", 501),  # 0x04
    ("a%\x0b4b", 501),  # 0x04 after a vertical tab
    ("a%+", 404), ("a%-%41", 404),  # no digit: url_decode fails
])
def test_percent_escapes_read_like_strtol(printer, escape, status):
    """url_decode reads %XX with strtol: white space and a sign come first (url_decode.cpp:16-29)."""
    r = put(printer, f"/api/v1/files/usb/{escape}.gcode")
    assert r.status == status
    if status == 201:
        assert printer.files()[0]["path"] == "/usb/\U00040000.gcode"


def test_reset_mid_body_is_logged(printer):
    """A reset (RST) partway through a body is an early close too."""
    import struct
    conn = Conn("127.0.0.1", printer.port)
    conn.send(_upload_head(printer, "u.gcode", 1000) + b"G1")
    deadline = time.monotonic() + 10
    while printer._call(lambda: printer.core.upload_path) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    linger = struct.pack("HH", 1, 0) if sys.platform == "win32" else struct.pack("ii", 1, 0)
    conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
    conn.sock.close()
    entries = []
    while not entries and time.monotonic() < deadline:
        time.sleep(0.02)
        entries = [e for e in printer.requests() if e["method"] == "PUT"]
    assert entries and entries[0]["error"] == "connection reset by the client" and entries[0]["close"] is True
    assert printer.files() == [] and printer._call(lambda: printer.core.upload_path) is None
