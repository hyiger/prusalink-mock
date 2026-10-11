"""The control API on its own port."""

from __future__ import annotations

import json
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
