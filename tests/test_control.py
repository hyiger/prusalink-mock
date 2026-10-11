"""The control API on its own port."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from httpraw import Conn
from prusalink_mock.auth import DigestClient


def call(printer, method: str, path: str, body: Any = None, raw: bool = False) -> tuple[int, Any]:
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(printer.control_url + path, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def tools_body(printer) -> dict:
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("GET", "/api/v1/tools", [("X-Api-Key", printer.password)])
    assert r.status == 200
    return json.loads(r.body)["tools"]


def test_health(printer):
    status, body = call(printer, "GET", "/_mock/health")
    assert status == 200 and body["ok"] and body["scenario"] == "indx-hy8"
    assert body["url"] == printer.url and body["url"].startswith("http://127.0.0.1:")


def test_state_hides_the_password(printer):
    status, body = call(printer, "GET", "/_mock/state")
    assert status == 200 and "auth" not in body and body["printer"]["model"] == "COREONEINDX"
    assert printer.password not in json.dumps(body)


def test_patch_replaces_tools_wholesale(printer):
    status, body = call(printer, "PATCH", "/_mock/state", {"tools": {
        "3": {"loaded": True, "material": "PETG", "vendor": "Prusament", "color": "Black",
              "nozzle_diameter": 0.4, "hardened": False, "high_flow": True}}})
    assert status == 200 and list(body["tools"]) == ["3"]
    assert tools_body(printer)["3"]["color_hex"] == "#000000"


def test_patch_merges_other_objects(printer):
    status, body = call(printer, "PATCH", "/_mock/state", {"printer": {"bed": {"actual": 60.5}}})
    assert status == 200 and body["printer"]["bed"] == {"actual": 60.5, "target": 0.0}


def test_validation_errors(printer):
    status, body = call(printer, "PATCH", "/_mock/state", {"tools": {
        "9": {"loaded": True, "material": "PLA", "nozzle_diameter": 0.4, "hardened": False, "high_flow": True},
        "1": {"loaded": True, "material": "TOOLONGMAT", "color_hex": "#ff0000", "color": "Red",
              "nozzle_diameter": 0.4, "hardened": False, "high_flow": True}}})
    assert status == 422
    text = " ".join(body["errors"])
    assert "/tools/9" in text and "/tools/1/material" in text and "/tools/1/color_hex" in text
    assert call(printer, "GET", "/_mock/state")[1]["tools"]["1"]["material"] == "PLA"  # unchanged


def test_put_state_keeps_the_password(printer):
    _, current = call(printer, "GET", "/_mock/state")
    current["printer"]["state"] = "BUSY"
    status, body = call(printer, "PUT", "/_mock/state", current)
    assert status == 200 and body["printer"]["state"] == "BUSY"
    assert tools_body(printer)  # the old key still works


def test_tools_endpoint(printer):
    tool = {"loaded": True, "material": "PA", "nozzle_diameter": 0.6, "hardened": True, "high_flow": True}
    assert call(printer, "PUT", "/_mock/tools/4", tool)[0] == 200
    assert tools_body(printer)["4"]["material"] == "PA"
    assert call(printer, "DELETE", "/_mock/tools/4")[0] == 200
    assert "4" not in tools_body(printer)
    assert call(printer, "DELETE", "/_mock/tools/4")[0] == 404


def test_load_scenario_and_reset(printer):
    status, body = call(printer, "POST", "/_mock/scenario", {"name": "indx-gaps"})
    assert status == 200 and list(body["tools"]) == ["1", "2", "3", "5"]
    assert call(printer, "POST", "/_mock/scenario", {"name": "nope"})[0] == 422
    call(printer, "POST", "/_mock/faults", {"action": {"type": "delay", "ms": 1}})
    tools_body(printer)
    status, body = call(printer, "POST", "/_mock/reset")
    assert status == 200 and body["name"] == "indx-gaps"
    assert call(printer, "GET", "/_mock/faults")[1]["faults"] == []
    assert call(printer, "GET", "/_mock/requests")[1]["entries"] == []


def test_scenarios_list(printer):
    names = [s["name"] for s in call(printer, "GET", "/_mock/scenarios")[1]["scenarios"]]
    assert {"indx-hy8", "indx-gaps", "mini404-3tool", "login-disabled"} <= set(names)


def test_request_log_since_and_redaction(printer):
    tools_body(printer)
    _, first = call(printer, "GET", "/_mock/requests")
    assert first["last_seq"] == 1
    tools_body(printer)
    _, later = call(printer, "GET", "/_mock/requests?since=1")
    assert [e["seq"] for e in later["entries"]] == [2]
    entry = later["entries"][0]
    assert entry["auth"] == {"scheme": "apikey", "present": True, "ok": True}
    assert ["X-Api-Key", "<redacted:match>"] in entry["headers"]
    assert printer.password not in json.dumps(first) + json.dumps(later)
    assert call(printer, "DELETE", "/_mock/requests")[0] == 200
    assert call(printer, "GET", "/_mock/requests")[1]["entries"] == []


def test_digest_response_is_redacted(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        nonce = conn.request("GET", "/api/version").nonce
        header = DigestClient(printer.password).header("GET", "/api/version", nonce)
        assert conn.request("GET", "/api/version", [("Authorization", header)]).status == 200
    response = header.rsplit('response="', 1)[1].rstrip('"')
    text = json.dumps(call(printer, "GET", "/_mock/requests")[1])
    assert response not in text and 'response=\\"<redacted>\\"' in text


def test_files(printer):
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("PUT", "/api/v1/files/usb//a%20b.gcode",
                         [("X-Api-Key", printer.password), ("Content-Length", "4")], b"G28\n")
        assert r.status == 201
    status, body = call(printer, "GET", "/_mock/files")
    assert status == 200 and body["files"][0]["path"] == "/usb/a b.gcode" and body["files"][0]["size"] == 4
    assert call(printer, "GET", "/_mock/files/a%20b.gcode", raw=True) == (200, b"G28\n")
    assert call(printer, "GET", "/_mock/files/AB~1.GCO", raw=True) == (200, b"G28\n")
    assert call(printer, "DELETE", "/_mock/files/a%20b.gcode")[0] == 200
    assert call(printer, "GET", "/_mock/files/a%20b.gcode")[0] == 404


def test_faults_crud(printer):
    status, rule = call(printer, "POST", "/_mock/faults",
                        {"match": {"path": "/api/v1/tools"}, "action": {"type": "status", "status": 500}})
    assert status == 201 and rule["id"] == 1
    assert call(printer, "POST", "/_mock/faults", {"action": {"type": "nope"}})[0] == 422
    assert len(call(printer, "GET", "/_mock/faults")[1]["faults"]) == 1
    assert call(printer, "DELETE", "/_mock/faults/1")[0] == 200
    assert call(printer, "DELETE", "/_mock/faults/1")[0] == 404


def _signed(printer, nonce: str) -> int:
    header = DigestClient(printer.password).header("GET", "/api/version", nonce)
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("GET", "/api/version", [("Authorization", header)])
    return r.status if r.status == 200 else (r.status, "stale=true" in r.header("WWW-Authenticate"))


@pytest.mark.parametrize("how", ["advance", "reboot"])
def test_clock_and_reboot_make_nonces_stale(printer, how):
    with Conn("127.0.0.1", printer.port) as conn:
        nonce = conn.request("GET", "/api/version").nonce
    assert _signed(printer, nonce) == 200
    if how == "advance":
        assert call(printer, "POST", "/_mock/clock/advance", {"seconds": 301})[0] == 200
    else:
        assert call(printer, "POST", "/_mock/reboot")[0] == 200
    assert _signed(printer, nonce) == (401, True)


@pytest.mark.parametrize("seconds", [b"1e309", b"-1e309", b"NaN", b"Infinity"])
def test_clock_advance_rejects_non_finite_seconds(printer, seconds):
    with Conn("127.0.0.1", printer.port) as conn:
        nonce = conn.request("GET", "/api/version").nonce
    status, body = call(printer, "POST", "/_mock/clock/advance", b'{"seconds": ' + seconds + b"}")
    assert status == 400 and "error" in body
    assert _signed(printer, nonce) == 200  # the clock is untouched
    with pytest.raises(ValueError):
        printer.advance_clock(float(seconds))


@pytest.mark.parametrize("number", [b"1e309", b"-1e309", b"NaN", b"Infinity"])
def test_state_rejects_non_finite_numbers(printer, number):
    before = call(printer, "GET", "/_mock/state")[1]["printer"]["bed"]
    status, body = call(printer, "PATCH", "/_mock/state", b'{"printer": {"bed": {"actual": ' + number + b"}}}")
    assert status == 400 and "error" in body
    assert call(printer, "GET", "/_mock/state")[1]["printer"]["bed"] == before
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("GET", "/api/v1/status", [("X-Api-Key", printer.password)])
    assert r.status == 200 and json.loads(r.body)["printer"]["temp_bed"] == before["actual"]


def test_python_api_rejects_non_finite_numbers(printer):
    with pytest.raises(ValueError):
        printer.patch_state({"printer": {"bed": {"actual": float("inf")}}})


def test_scenario_files_reject_non_finite_numbers(tmp_path):
    from prusalink_mock.state import ScenarioError, load_document
    bad = tmp_path / "bad.json"
    bad.write_text('{"printer": {"bed": {"actual": NaN}}}', encoding="utf-8")
    with pytest.raises(ScenarioError):
        load_document(str(bad))


@pytest.mark.parametrize("method,path,status", [
    ("GET", "/nope", 404),
    ("GET", "/_mock/nope", 404),
    ("DELETE", "/_mock/health", 405),
    ("PATCH", "/_mock/state", 400),
    ("POST", "/_mock/clock/advance", 400),
])
def test_control_errors(printer, method, path, status):
    body = b"not json" if method in ("PATCH", "POST") else None
    assert call(printer, method, path, body)[0] == status


def test_job_commands_after_the_job_ends():
    """The id is marlin_vars job_id, which outlives the job: 409 from the state checks, not 404."""
    from prusalink_mock import MockPrinter
    with MockPrinter("indx-printing") as printer:
        def job(method, target):
            with Conn("127.0.0.1", printer.port) as conn:
                return conn.request(method, target, [("X-Api-Key", printer.password)]).status

        assert job("DELETE", "/api/v1/job/41") == 404
        assert job("PUT", "/api/v1/job/42/pause") == 204
        assert job("PUT", "/api/v1/job/42/resume") == 204
        assert job("DELETE", "/api/v1/job/42") == 204
        assert [job("DELETE", "/api/v1/job/42"), job("PUT", "/api/v1/job/42/pause"),
                job("PUT", "/api/v1/job/42/resume"), job("DELETE", "/api/v1/job/41")] == [409, 409, 409, 404]
        assert job("GET", "/api/v1/job") == 204
        assert printer.state()["printer"]["job_id"] == 42


def _version_info(printer) -> tuple[dict, dict]:
    with Conn("127.0.0.1", printer.port) as conn:
        version = conn.request("GET", "/api/version", [("X-Api-Key", printer.password)])
        info = conn.request("GET", "/api/v1/info", [("X-Api-Key", printer.password)])
    return json.loads(version.body), json.loads(info.body)


def test_nozzle_diameter_follows_tool_1(printer):
    """basic_gets.cpp:203, 231 and tools_renderer.cpp:22 read the same config store entry."""
    tool = {"loaded": True, "material": "PLA", "nozzle_diameter": 0.6, "hardened": False, "high_flow": True}
    printer.set_tool(1, tool)
    version, info = _version_info(printer)
    assert version["nozzle_diameter"] == info["nozzle_diameter"] == tools_body(printer)["1"]["nozzle_diameter"] == 0.6
    printer.remove_tool(1)  # an uncalibrated dock 1 keeps the stored diameter
    assert _version_info(printer)[0]["nozzle_diameter"] == 0.6


def test_model_values_follow_the_model(printer):
    one_tool = {"1": {"loaded": False, "nozzle_diameter": 0.4, "hardened": False, "high_flow": False}}
    printer.patch_state({"printer": {"model": "COREONEL", "active_tool": 1}, "tools": one_tool})
    version, info = _version_info(printer)
    assert version["printer"] == "8.1.0" and version["hostname"] == info["hostname"] == "prusa-core-one-l"
    status, body = call(printer, "PATCH", "/_mock/state", {"tools": dict(one_tool, **{"2": one_tool["1"]})})
    assert status == 422 and any("/printer/tool_count" in e for e in body["errors"])
    printer.patch_state({"printer": {"printer_version": "9.9.9", "hostname": "lab-printer"}})
    printer.patch_state({"printer": {"model": "COREONE"}})
    version, _ = _version_info(printer)
    assert version["printer"] == "9.9.9" and version["hostname"] == "lab-printer"
    printer.patch_state({"printer": {"printer_version": None}})
    assert _version_info(printer)[0]["printer"] == "7.1.0"


def test_reset_drops_a_request_in_flight(printer):
    printer.add_fault({"match": {"method": "PUT"}, "action": {"type": "delay", "ms": 600}})
    printer.add_fault({"match": {"method": "GET"}, "action": {"type": "delay", "ms": 600}})
    put = Conn("127.0.0.1", printer.port)
    put.send((f"PUT /api/v1/files/usb/slow.gcode HTTP/1.1\r\nX-Api-Key: {printer.password}\r\n"
              f"Content-Length: 4\r\n\r\nG28\n").encode())
    get = Conn("127.0.0.1", printer.port)
    get.send(f"GET /api/v1/tools HTTP/1.1\r\nX-Api-Key: {printer.password}\r\n\r\n".encode())
    time.sleep(0.2)
    printer.reset()
    assert put.read_to_eof() == b"" and get.read_to_eof() == b""  # dropped, as by a reboot
    put.close()
    get.close()
    time.sleep(0.8)
    assert printer.files() == [] and printer.requests() == []


def test_reset_frees_a_stalled_upload(printer):
    stalled = Conn("127.0.0.1", printer.port)
    stalled.send((f"PUT /api/v1/files/usb/stalled.gcode HTTP/1.1\r\nX-Api-Key: {printer.password}\r\n"
                  f"Content-Length: 10\r\n\r\nG28").encode())
    deadline = time.monotonic() + 10
    while printer._call(lambda: printer.core.upload_path) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    printer.load_scenario("indx-gaps")
    with Conn("127.0.0.1", printer.port) as conn:
        r = conn.request("PUT", "/api/v1/files/usb/next.gcode", [("X-Api-Key", printer.password),
                                                                 ("Content-Length", "4")], b"G28\n")
    assert r.status == 201
    try:
        stalled.send(b"\nG1 X1\n")
    except OSError:
        pass
    stalled.close()
    time.sleep(0.2)
    assert [f["path"] for f in printer.files()] == ["/usb/next.gcode"]


def test_expect_100_continue(printer):
    """libcurl waits a second for the 100 before sending a PUT body."""
    body = json.dumps({"loaded": True, "material": "PA", "nozzle_diameter": 0.6, "hardened": True,
                       "high_flow": True}).encode()
    with Conn("127.0.0.1", printer.core.control_port) as conn:
        conn.send((f"PUT /_mock/tools/4 HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode())
        conn.sock.settimeout(0.5)
        assert conn.sock.recv(64) == b"HTTP/1.1 100 Continue\r\n\r\n"
        conn.sock.settimeout(15)
        conn.send(body)
        assert conn.read_response().status == 200
    assert tools_body(printer)["4"]["material"] == "PA"


def test_control_bind_failure_releases_the_printer_port():
    from prusalink_mock import MockPrinter
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        printer = MockPrinter(control_port=busy.getsockname()[1])
        with pytest.raises(OSError):
            printer.start()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", printer.core.port), timeout=2).close()


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.parametrize("host", ["", "0.0.0.0", "localhost", "::"])
def test_advertised_url_answers(host):
    from prusalink_mock import MockPrinter
    if host == "::" and not _ipv6_loopback():
        pytest.skip("no IPv6 loopback")
    with MockPrinter(host=host) as printer:
        assert printer.url == f"http://{'[::1]' if host == '::' else '127.0.0.1'}:{printer.port}"
        with Conn(printer.host, printer.port) as conn:
            assert conn.request("GET", "/api/version", [("X-Api-Key", printer.password)]).status == 200


def test_password_is_redacted_from_every_logged_field(tmp_path):
    from prusalink_mock import MockPrinter
    log = tmp_path / "log.jsonl"
    with MockPrinter(log_file=str(log)) as printer:
        key = printer.password
        lower = "".join(f"%{b:02x}" for b in key.encode())
        for target in (f"/api/version?token={key}", f"/api/v1/files/usb/{lower}.gcode", f"/{key}"):
            with Conn("127.0.0.1", printer.port) as conn:
                conn.request("GET", target, [("X-Api-Key", key)])
        with Conn("127.0.0.1", printer.port) as conn:
            conn.request("GET", "/api/version", [("X-Api-Key", key), ("X-Leak", lower), (lower, "x")])
        with Conn("127.0.0.1", printer.port) as conn:
            conn.request("PUT", f"/api/v1/files/usb/{key}.gcode",
                         [("X-Api-Key", key), ("Content-Length", "5")], b"G28\n\n")
        entries = json.dumps(printer.requests())
        assert key not in entries and lower not in entries.lower() and entries.count("<redacted>") >= 6
    assert key not in log.read_text(encoding="utf-8")


def test_password_changed_mid_request_is_still_redacted(printer):
    old = printer.password
    with Conn("127.0.0.1", printer.port) as conn:
        conn.sock.sendall(f"PUT /api/v1/files/usb/{old}.gcode HTTP/1.1\r\nHost: x\r\nX-Api-Key: {old}\r\n"
                          "Content-Length: 8\r\n\r\nG28\n".encode())
        time.sleep(0.3)  # the request waits for the rest of its body
        printer.patch_state({"auth": {"password": "newKey123"}})
        conn.sock.sendall(b"G28\n")
        conn.read_response()
    entries = json.dumps(printer.requests())
    assert old not in entries and "<redacted>" in entries


def test_every_password_a_request_met_is_redacted(printer):
    a, b = printer.password, "secondKey12"
    with Conn("127.0.0.1", printer.port) as conn:
        conn.sock.sendall(f"PUT /api/v1/files/usb/{a}.gcode HTTP/1.1\r\n".encode())  # begins under A
        time.sleep(0.05)
        printer.patch_state({"auth": {"password": b}})
        conn.sock.sendall(f"X-Api-Key: {b}\r\nX-Note: {b}\r\nContent-Length: 8\r\n\r\nG28\n".encode())  # B
        time.sleep(0.3)  # authenticated under B, waiting for the body
        printer.patch_state({"auth": {"password": "thirdKey123"}})
        conn.sock.sendall(b"G28\n")
        conn.read_response()
    entries = json.dumps(printer.requests())
    assert a not in entries and b not in entries and "<redacted>" in entries
