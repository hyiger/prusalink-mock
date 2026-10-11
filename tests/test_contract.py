"""Firmware behavior that must hold on the mock and on real firmware alike.

Without options these run against an in-process mock. To check the mock's
assumptions against Mini404 or a printer:

    pytest -m contract --target http://127.0.0.1:8080 --password-file=$HOME/.prusalink-password

Keep the "=": with a space, pytest takes the file's folder for the project root
and never loads the options.

Only GET/HEAD requests and requests that fail authentication are sent, unless
--allow-writes is given (then a small file is uploaded and deleted again).

Credentials stay wrapped in Secret (conftest.py) all the way to the socket, so
assertion messages and tracebacks show <secret> instead of the key.
"""

from __future__ import annotations

import json
import re

import pytest

from conftest import Secret
from httpraw import Conn, nonce_masked
from prusalink_mock.auth import DigestClient

pytestmark = pytest.mark.contract

TIMEOUT = 60.0  # the emulator can be slow


def conn(t) -> Conn:
    return Conn(t.host, t.port, timeout=TIMEOUT)


def key(t):
    return [("X-Api-Key", t.password)]


def digest(t, method, target, nonce, uri=None):
    return [("Authorization", Secret(DigestClient(t.password.reveal()).header(method, target, nonce, uri)))]


def get(t, target, headers=None, **kw):
    with conn(t) as c:
        return c.request("GET", target, key(t) if headers is None else headers, **kw)


def ordered(body: bytes):
    return json.loads(body, object_pairs_hook=lambda pairs: pairs)


def keys(pairs):
    return [k for k, _ in pairs]


JSON_HEADERS = ["Content-Type: application/json", "Connection: keep-alive", "Transfer-Encoding: chunked"]
UNAUTH = ["HTTP/1.1 401 Unauthorized", "Content-Type: text/plain", "Connection: keep-alive", "Content-Length: 20",
          'WWW-Authenticate: Digest realm="Printer API", nonce="<nonce>", stale=false']


def test_version(contract_target):
    r = get(contract_target, "/api/version")
    assert r.status == 200 and r.header_lines()[1:] == JSON_HEADERS
    assert re.fullmatch(rb'\{"api":"2\.0\.0","server":"[^"]*","nozzle_diameter":\d+\.\d\d,"text":"PrusaLink",'
                        rb'"hostname":"[^"]*","firmware":"[^"]*","printer":"\d+\.\d+\.\d+",'
                        rb'"capabilities":\{"upload-by-put":true\}\}', r.body)


def test_info(contract_target):
    r = get(contract_target, "/api/v1/info")
    assert r.status == 200 and r.header_lines()[1:] == JSON_HEADERS
    assert re.fullmatch(rb'\{"nozzle_diameter":\d+\.\d\d,"mmu":(true|false),"serial":"[^"]*","hostname":"[^"]*",'
                        rb'"min_extrusion_temp":\d+\}', r.body)


def test_storage(contract_target):
    r = get(contract_target, "/api/v1/storage")
    assert re.fullmatch(rb'\{"storage_list":\[\{"path":"/usb/","name":"usb","type":"USB","read_only":false,'
                        rb'"available":(true|false)\}\]\}', r.body)


def test_tools(contract_target):
    r = get(contract_target, "/api/v1/tools")
    if r.status == 404:
        pytest.skip("this firmware has no /api/v1/tools")
    assert r.status == 200 and r.header_lines()[1:] == JSON_HEADERS
    [(top, tools)] = ordered(r.body)
    assert top == "tools"
    numbers = [int(k) for k in keys(tools)]
    assert numbers == sorted(numbers) and all(1 <= n <= 8 for n in numbers)
    for _, tool in tools:
        assert keys(tool) == ["loaded", "material", "vendor", "color", "color_hex", "nozzle_diameter", "hardened",
                              "high_flow"]
        t = dict(tool)
        assert re.fullmatch(r"(#[0-9A-F]{6})?", t["color_hex"])
        if not t["loaded"]:
            assert t["material"] == t["vendor"] == t["color"] == t["color_hex"] == ""
    assert all(re.fullmatch(rb"\d+\.\d\d", m) for m in re.findall(rb'"nozzle_diameter":([^,]*)', r.body))


def test_status(contract_target):
    r = get(contract_target, "/api/v1/status")
    assert r.status == 200
    top = keys(ordered(r.body))
    assert [k for k in top if k not in ("job", "transfer")] == ["storage", "printer"]
    assert top[-1] == "printer" and (top[0] in ("job", "storage"))
    printer = dict(ordered(r.body))["printer"]
    names = [k for k in keys(printer) if k not in ("axis_x", "axis_y")]
    assert names == ["state", "temp_bed", "target_bed", "temp_nozzle", "target_nozzle", "axis_z", "flow", "speed",
                     "fan_hotend", "fan_print"]
    assert dict(printer)["state"] in ("IDLE", "BUSY", "PRINTING", "PAUSED", "FINISHED", "STOPPED", "ATTENTION",
                                      "ERROR")
    for field in (b"temp_bed", b"target_bed", b"temp_nozzle", b"target_nozzle", b"axis_z"):
        assert re.search(b'"' + field + rb'":-?\d+\.\d[,}]', r.body)


def test_job(contract_target):
    r = get(contract_target, "/api/v1/job")
    if r.status == 204:
        assert r.header_lines() == ["HTTP/1.1 204 No Content", "Content-Type: text/plain", "Connection: keep-alive",
                                    "Content-Length: 0"]
    else:
        assert r.status == 200 and keys(ordered(r.body))[:2] == ["id", "state"]


def test_unauthorized(contract_target):
    with conn(contract_target) as c:
        r = c.request("GET", "/api/version")
        assert nonce_masked(r.header_lines()) == UNAUTH and r.body == b"401: Unauthorized\n\n\n"
        r = c.request("GET", "/api/version", [("Accept", "application/json")])
        assert r.header("Content-Length") == "43" and r.body == b'{"title": "401: Unauthorized","message":""}'


@pytest.mark.parametrize("variant", ["wrong-key", "", "<case>", "<prefix>", "<extra>"])
def test_api_key_rejected(contract_target, variant):
    pw = contract_target.password
    sent = Secret({"<case>": pw.reveal().swapcase(), "<prefix>": pw.reveal()[:-1],
                   "<extra>": pw.reveal() + "x"}.get(variant, variant))
    if sent.reveal() == pw.reveal():
        pytest.skip("the password has no letters to change the case of")
    r = get(contract_target, "/api/version", [("X-Api-Key", sent)])
    assert r.status == 401 and r.header("WWW-Authenticate") == 'ApiKey realm="Printer API"'


def test_basic_auth_gets_a_digest_challenge(contract_target):
    r = get(contract_target, "/api/version", [("Authorization", "Basic bWFrZXI6eA==")])
    assert r.status == 401 and r.header("WWW-Authenticate").startswith('Digest realm="Printer API", nonce="')


def test_auth_before_routing(contract_target):
    assert get(contract_target, "/api/v1/nope", []).status == 401
    r = get(contract_target, "/api/v1/nope")
    assert r.header_lines() == ["HTTP/1.1 404 Not Found", "Content-Type: text/plain", "Connection: keep-alive",
                                "Content-Length: 17"]
    assert r.body == b"404: Not Found\n\n\n"
    assert get(contract_target, "/plmock-nope", []).status == 404


@pytest.mark.parametrize("target", ["/api/v1/storage?x", "/api/v1/storage/"])
def test_routes_match_the_raw_target(contract_target, target):
    assert get(contract_target, target).status == 404


def test_head_is_405_and_closes(contract_target):
    with conn(contract_target) as c:
        r = c.request("HEAD", "/api/version", key(contract_target))
    assert r.header_lines() == ["HTTP/1.1 405 Method Not Allowed", "Content-Type: text/plain", "Connection: close",
                                "Content-Length: 26"]
    assert r.closed and not r.reset


def test_uri_length_limit(contract_target):
    assert get(contract_target, "/" + "a" * 167, []).status == 404
    assert get(contract_target, "/" + "a" * 168, []).status == 414


def _challenge_nonce(t, target="/api/version") -> str:
    return get(t, target, []).nonce


def test_digest_uses_the_raw_target(contract_target):
    t = contract_target
    nonce = _challenge_nonce(t)
    r = get(t, "/api/version", digest(t, "GET", "/api/version", nonce, uri="/ignored"))
    assert r.status == 200
    r = get(t, "/api/version", digest(t, "GET", "/api//version", nonce, uri="/api/version"))
    assert r.status == 401 and r.header("WWW-Authenticate").endswith("stale=false")


def test_previous_boot_nonce_is_stale(contract_target):
    t = contract_target
    nonce = _challenge_nonce(t)
    old = f"{int(nonce[:8], 16) ^ 0x5A5A5A5A:08x}" + nonce[8:]
    r = get(t, "/api/version", digest(t, "GET", "/api/version", old))
    assert r.status == 401 and r.header("WWW-Authenticate").endswith("stale=true")


def test_last_auth_header_wins(contract_target):
    t = contract_target
    bad_digest = ("Authorization", f'Digest username="maker", realm="Printer API", nonce="{_challenge_nonce(t)}", '
                                   f'uri="/api/version", response="{"0" * 32}"')
    r = get(t, "/api/version", key(t) + [bad_digest])
    assert r.status == 401 and r.header("WWW-Authenticate").startswith("Digest")
    r = get(t, "/api/version", [bad_digest] + key(t))
    assert r.status == 200


def test_http10(contract_target):
    with conn(contract_target) as c:
        r = c.request("GET", "/api/v1/storage", key(contract_target), version="HTTP/1.0")
    assert r.header_lines()[1:] == ["Content-Type: application/json", "Connection: close"]
    assert r.closed and r.body.startswith(b'{"storage_list":[')


def test_keep_alive(contract_target):
    with conn(contract_target) as c:
        for _ in range(3):
            r = c.request("GET", "/api/version", key(contract_target))
            assert r.status == 200 and not r.closed
            assert not any(n.lower() in ("date", "server") for n, _ in r.headers)


@pytest.fixture
def writes(contract_target):
    if not contract_target.allow_writes:
        pytest.skip("needs --allow-writes (a PUT also opens the print preview on a real printer)")
    return contract_target


def test_upload_round_trip(writes):
    t = writes
    target = "/api/v1/files/usb//plmock/contract%20test.gcode"
    body = b"; prusalink-mock contract test\n"

    def put(path, data=body, extra=()):
        with conn(t) as c:
            return c.request("PUT", path, key(t) + [("Content-Length", str(len(data)))] + list(extra), data)

    try:
        r = put(target, extra=[("Overwrite", "?1")])
        assert r.status == 201 and r.closed and r.header_lines()[1:] == [
            "Content-Type: application/json", "Connection: close", "Read-Only: false", "Currently-Printing: false"]
        info = ordered(r.body)
        assert keys(info) == ["name", "ro", "type", "m_timestamp", "size", "refs", "display_name"]
        assert dict(info)["display_name"] == "contract test.gcode" and dict(info)["size"] == len(body)
        assert put(target).status == 409
        # FatFS matches the reported short name as the same file (dir_find)
        assert put(f"/api/v1/files/usb/plmock/{dict(info)['name']}").status == 409
        assert put("/api/v1/files/usb/plmock/x.txt").status == 415
        with conn(t) as c:
            r = c.request("PUT", "/api/v1/files/usb/plmock/x.gcode", key(t))
        assert r.status == 411
        assert get(t, "/api/v1/files/usb/plmock/contract%20test.gcode").status == 200
    finally:
        with conn(t) as c:
            c.request("DELETE", "/api/v1/files/usb/plmock/contract%20test.gcode", key(t))
        with conn(t) as c:
            c.request("DELETE", "/api/v1/files/usb/plmock", key(t))
