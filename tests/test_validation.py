"""Hostile input at the validation boundary: scenarios, the control API, the Python API
and the command line reject it up front, so the printer port never serves a state
the firmware couldn't be in."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from httpraw import Conn
from prusalink_mock import MockPrinter
from prusalink_mock.faults import FaultError
from prusalink_mock.state import ScenarioError, load_document
from test_cli import env, plmock
from test_control import call

BIG = b"1" + b"0" * 400  # an integer too large for a double


def printer_get(printer, target: str):
    with Conn("127.0.0.1", printer.port) as conn:
        return conn.request("GET", target, [("X-Api-Key", printer.password)])


def assert_printer_port_ok(printer) -> None:
    """Every JSON route still answers valid JSON."""
    for target in ("/api/version", "/api/v1/info", "/api/v1/status", "/api/v1/tools"):
        r = printer_get(printer, target)
        assert r.status == 200, target
        json.loads(r.body)


def errors_for(printer, method: str, path: str, body) -> list[str]:
    status, payload = call(printer, method, path, body)
    assert status == 422, payload
    return payload["errors"]


TOOL = {"loaded": True, "material": "PLA", "vendor": "", "color": "", "nozzle_diameter": 0.4,
        "hardened": False, "high_flow": True}


def tool(**fields) -> dict:
    return dict(TOOL, **fields)


# --- numbers -------------------------------------------------------------------

@pytest.mark.parametrize("body,where", [
    (b'{"printer":{"bed":{"actual":1e100}}}', "/printer/bed/actual"),
    (b'{"printer":{"axis":{"z":-1e39}}}', "/printer/axis/z"),
    (b'{"printer":{"bed":{"actual":' + BIG + b"}}}", "/printer/bed/actual"),
    (b'{"printer":{"nozzle_diameter":-' + BIG + b"}}", "/printer/nozzle_diameter"),
    (b'{"printer":{"bed":{"target":60.5}}}', "/printer/bed/target"),
    (b'{"printer":{"bed":{"target":40000}}}', "/printer/bed/target"),
    (b'{"printer":{"nozzle":{"target":-32769}}}', "/printer/nozzle/target"),
    (b'{"job":{"time_printing":4294967296}}', "/job/time_printing"),
    (b'{"job":{"time_printing":' + BIG + b"}}", "/job/time_printing"),
    (b'{"job":{"time_remaining":4294967295}}', "/job/time_remaining"),
    (b'{"job":{"filament_change_in":4294967295}}', "/job/filament_change_in"),
    (b'{"job":{"file":{"size":2147483648,"m_timestamp":1728650000}}}', "/job/file/size"),
    (b'{"job":{"file":{"size":1,"m_timestamp":0}}}', "/job/file/m_timestamp"),
    (b'{"job":{"file":{"size":1,"m_timestamp":4354819200}}}', "/job/file/m_timestamp"),
    (b'{"printer":{"fans":{"print":65536}}}', "/printer/fans/print"),
], ids=lambda x: x if isinstance(x, str) else "body")
def test_numbers_must_fit_the_firmware_types(body, where):
    with MockPrinter("indx-printing") as printer:
        before = printer.state()
        assert any(e.startswith(where) for e in errors_for(printer, "PATCH", "/_mock/state", body))
        assert printer.state() == before
        assert_printer_port_ok(printer)
        assert json.loads(printer_get(printer, "/api/v1/job").body)["id"] == 42


def test_numbers_at_the_limits_are_kept():
    with MockPrinter("indx-printing") as printer:
        printer.patch_state({"printer": {"bed": {"actual": 3.4e38, "target": 32767}, "nozzle": {"target": 215}},
                             "job": {"time_printing": 4294967295, "time_remaining": 4294967294,
                                     "file": {"size": 2147483647, "m_timestamp": 4354819199}}})
        status = json.loads(printer_get(printer, "/api/v1/status").body)
        assert status["printer"]["target_bed"] == 32767.0 and status["printer"]["target_nozzle"] == 215.0
        assert status["job"]["time_printing"] == 4294967295 and status["job"]["time_remaining"] == 4294967294
        job = json.loads(printer_get(printer, "/api/v1/job").body)
        assert job["file"]["size"] == 2147483647 and job["file"]["m_timestamp"] == 4354819199


def test_targets_are_whole_numbers_given_either_way(printer):
    printer.patch_state({"printer": {"bed": {"target": 60}, "nozzle": {"target": 215.0}}})
    assert printer.state()["printer"]["bed"]["target"] == 60.0
    body = printer_get(printer, "/api/v1/status").body
    assert b'"target_bed":60.0,' in body


def test_python_api_rejects_what_would_crash_the_renderers():
    with MockPrinter("indx-printing") as printer:
        job = printer.state()["job"]
        for bad in ({"job": dict(job, time_printing=10 ** 5000)},
                    {"printer": {"bed": {"actual": 10 ** 400}}},
                    {"printer": {"bed": {"actual": 1e300}}}):
            with pytest.raises(ScenarioError):
                printer.patch_state(bad)
        assert_printer_port_ok(printer)
        assert call(printer, "GET", "/_mock/state")[0] == 200


# --- strings -------------------------------------------------------------------

@pytest.mark.parametrize("patch,where", [
    ({"firmware": {"version": "6.9\u0001"}}, "/firmware/version"),
    ({"printer": {"hostname": "h\u001fx"}}, "/printer/hostname"),
    ({"printer": {"serial": "\u0007"}}, "/printer/serial"),
    ({"printer": {"printer_version": "7.10.0\u000b"}}, "/printer/printer_version"),
    ({"name": "a\u007fb"}, "/name"),
    ({"job": {"file": {"display_name": "a\u0001b.gcode"}}}, "/job/file/display_name"),
    ({"job": {"file": {"sfn_path": "/usb/\u0001.GCO"}}}, "/job/file/sfn_path"),
    ({"tools": {"1": tool(material="P\u0001A")}}, "/tools/1/material"),
    ({"tools": {"1": tool(vendor="Pr\u001fsa")}}, "/tools/1/vendor"),
    ({"tools": {"1": tool(color="R\u0002d", color_hex="#FF0000")}}, "/tools/1/color"),
    ({"tools": {"1": tool(color="Red", color_hex="#FF0000\n")}}, "/tools/1/color_hex"),
], ids=lambda x: x if isinstance(x, str) else "body")
def test_control_characters_are_rejected(patch, where):
    with MockPrinter("indx-printing") as printer:
        assert any(e.startswith(where) for e in errors_for(printer, "PATCH", "/_mock/state", patch))
        assert_printer_port_ok(printer)


@pytest.mark.parametrize("raw,where", [
    (b'{"firmware":{"version":"\\udfff"}}', "/firmware/version"),
    (b'{"printer":{"hostname":"\\ud800"}}', "/printer/hostname"),
    (b'{"printer":{"serial":"\\ud800"}}', "/printer/serial"),
    (b'{"name":"\\ud800"}', "/name"),
    (b'{"description":"\\ud800"}', "/description"),
    (b'{"auth":{"password":"\\ud800"}}', "/auth/password"),
    (b'{"tools":{"1":{"loaded":true,"material":"\\ud800","nozzle_diameter":0.4,"hardened":false,'
     b'"high_flow":false}}}', "/tools/1/material"),
    (b'{"tools":{"1":{"loaded":true,"material":"PLA","vendor":"\\udc00","nozzle_diameter":0.4,'
     b'"hardened":false,"high_flow":false}}}', "/tools/1/vendor"),
], ids=lambda x: x if isinstance(x, str) else "body")
def test_lone_surrogates_are_rejected(raw, where):
    with MockPrinter("indx-printing") as printer:
        assert any(e.startswith(where) for e in errors_for(printer, "PATCH", "/_mock/state", raw))
        assert_printer_port_ok(printer)
        assert printer_get(printer, "/").status == 200


@pytest.mark.parametrize("patch,where", [
    ({"printer": {"hostname": "h" * 21}}, "/printer/hostname"),
    ({"printer": {"serial": "S" * 25}}, "/printer/serial"),
    ({"job": {"file": {"display_name": "x" * 167}}}, "/job/file/display_name"),
    ({"job": {"file": {"sfn_path": "/usb/" + "X" * 98}}}, "/job/file/sfn_path"),
    ({"printer": {"printer_version": "abc"}}, "/printer/printer_version"),
    ({"printer": {"printer_version": "7.10"}}, "/printer/printer_version"),
    ({"printer": {"printer_version": "07.1.0"}}, "/printer/printer_version"),
    ({"printer": {"printer_version": "256.0.0"}}, "/printer/printer_version"),
    ({"printer": {"printer_version": "7.١.0"}}, "/printer/printer_version"),
], ids=lambda x: x if isinstance(x, str) else "body")
def test_strings_must_fit_the_firmware_buffers(patch, where):
    with MockPrinter("indx-printing") as printer:
        assert any(e.startswith(where) for e in errors_for(printer, "PATCH", "/_mock/state", patch))


def test_strings_at_the_limits_are_kept():
    with MockPrinter("indx-printing") as printer:
        printer.patch_state({"printer": {"hostname": "h" * 20, "serial": "S" * 24, "printer_version": "255.0.10"},
                             "job": {"file": {"display_name": "x" * 166, "sfn_path": "/usb/" + "X" * 97}}})
        version = json.loads(printer_get(printer, "/api/version").body)
        assert version["hostname"] == "h" * 20 and version["printer"] == "255.0.10"
        assert json.loads(printer_get(printer, "/api/v1/job").body)["file"]["display_name"] == "x" * 166


# --- tools -----------------------------------------------------------------------

@pytest.mark.parametrize("fields,where", [
    ({"material": 'P"A'}, "/tools/1/material"),
    ({"material": "---"}, "/tools/1/material"),
    ({"material": "PLA+"}, "/tools/1/material"),
    ({"vendor": "P\\x"}, "/tools/1/vendor"),
    ({"vendor": " x "}, "/tools/1/vendor"),
    ({"vendor": "prusament"}, "/tools/1/vendor"),
    ({"color": "Röt", "color_hex": "#FF0000"}, "/tools/1/color"),
    ({"color": "red", "color_hex": "#FF0000"}, "/tools/1/color"),
    ({"color": "Red", "color_hex": "#00FF00"}, "/tools/1/color_hex"),
    ({"color": "Natural", "color_hex": "#123456"}, "/tools/1/color_hex"),
], ids=lambda x: x if isinstance(x, str) else "body")
def test_tool_names_follow_the_firmware_library(printer, fields, where):
    assert any(e.startswith(where) for e in errors_for(printer, "PUT", "/_mock/tools/1", tool(**fields)))


def test_tool_names_the_firmware_allows(printer):
    for fields in ({"material": "PCTG-HF"}, {"material": "my_PLA"}, {"vendor": "3D-Fuel"},
                   {"vendor": "A.B+C & D_E-F"}, {"color": "Red", "color_hex": "#FF0000"},
                   {"color": "Sky Blue", "color_hex": "#87CEEB"}):
        assert call(printer, "PUT", "/_mock/tools/1", tool(**fields))[0] == 200, fields


def test_tools_share_one_library_entry_per_name(printer):
    """A user entry has one name and one color (filament_library.cpp:121-182)."""
    teal = tool(color="Teal", color_hex="#008080")
    errors = errors_for(printer, "PATCH", "/_mock/state", {"tools": {"1": teal, "2": dict(teal, color="teal")}})
    assert any(e.startswith("/tools/2/color") for e in errors)
    errors = errors_for(printer, "PATCH", "/_mock/state", {"tools": {"1": teal, "2": dict(teal, color_hex="#008081")}})
    assert any(e.startswith("/tools/2/color_hex") for e in errors)
    many = {str(n): tool(vendor=f"Vendor {n}", color=f"Color {n}", color_hex=f"#00000{n}") for n in range(1, 9)}
    assert call(printer, "PATCH", "/_mock/state", {"tools": many})[0] == 200


@pytest.mark.parametrize("key", ["1" + "0" * 5000, "1" * 5000, "0", "9", "01", "+1", " 1", "1\n", "١"])
def test_tool_keys_are_one_digit(printer, key):
    if key.isascii() and key.isdigit():
        status, body = call(printer, "PUT", "/_mock/tools/" + key, tool())
        assert status == 422 and body["errors"][0].startswith("/tools/")
    status, body = call(printer, "PATCH", "/_mock/state", {"tools": {key: tool()}})
    assert status == 422 and body["errors"][0].startswith("/tools/")
    assert call(printer, "GET", "/_mock/state")[0] == 200


def test_tool_keys_must_be_strings_in_process(printer):
    with pytest.raises(ScenarioError, match="tool keys are the strings"):
        printer.patch_state({"tools": {1: tool()}})
    with pytest.raises(ScenarioError, match="tool keys are the strings"):
        printer.set_state({"tools": {1: tool()}})
    assert printer.set_tool(1, tool())["tools"]["1"]["material"] == "PLA"


@pytest.mark.parametrize("doc", [{"tools": {10 ** 5000: {}}}, {10 ** 5000: 1}, {(1, 2): 1}, {"printer": {None: 1}}])
def test_odd_keys_in_process_are_scenario_errors(printer, doc):
    """Only a Python caller can send them; the error still names the key it can print."""
    with pytest.raises(ScenarioError):
        printer.patch_state(doc)
    with pytest.raises(FaultError):
        printer.add_fault(doc)


# --- documents -------------------------------------------------------------------

def test_a_scenario_is_an_object(printer, tmp_path):
    assert call(printer, "PUT", "/_mock/state", b"null")[0] == 422
    assert call(printer, "PUT", "/_mock/state", b"[]")[0] == 422
    null = tmp_path / "null.json"
    null.write_text("null")
    status, body = call(printer, "POST", "/_mock/scenario", {"name": str(null)})
    assert status == 422 and body["errors"] == ["/: a scenario must be a JSON object"]
    r = plmock("run", "--scenario", str(null), "--", sys.executable, "-c", "pass")
    assert r.returncode == 2 and "must be a JSON object" in r.stderr


@pytest.mark.parametrize("change", [
    lambda c: c("PUT", "/_mock/state", b"null"),
    lambda c: c("PATCH", "/_mock/state", {"auth": None}),
    lambda c: c("PATCH", "/_mock/state", {"auth": {"password": None}}),
    lambda c: c("PUT", "/_mock/state", dict(c("GET", "/_mock/state")[1], auth={})),
    lambda c: c("PUT", "/_mock/state", dict(c("GET", "/_mock/state")[1], auth=None)),
])
def test_state_writes_keep_the_password_override(change):
    with MockPrinter("indx-hy8", password="override99") as printer:
        change(lambda *args: call(printer, *args))
        assert printer.password == "override99"
        r = printer_get(printer, "/api/v1/info")
        assert r.status == 200


def test_an_explicit_password_still_changes_it(printer):
    printer.patch_state({"auth": {"password": "newKey123"}})
    assert printer.password == "newKey123"
    current = call(printer, "GET", "/_mock/state")[1]
    assert call(printer, "PUT", "/_mock/state", dict(current, auth={"password": "other456"}))[0] == 200
    assert printer.password == "other456"


@pytest.mark.parametrize("body", [
    b'{"name":' + b"[" * 600 + b"]" * 600 + b"}",
    b'{"printer":' + b'{"a":' * 1000 + b"1" + b"}" * 1001,
    b'{"tools":' + b"[" * 800 + b"]" * 800 + b"}",
    b'{"name":' + b"[" * 100000 + b"]" * 100000 + b"}",
])
def test_deep_nesting_is_a_client_error(printer, body):
    status, payload = call(printer, "PATCH", "/_mock/state", body)
    assert status in (400, 422), payload
    status, payload = call(printer, "POST", "/_mock/clock/advance", body)
    assert status == 400, payload


def test_deep_nesting_in_process(printer):
    deep: dict = {}
    for _ in range(2000):
        deep = {"a": deep}
    with pytest.raises(ScenarioError, match="nested deeper"):
        printer.patch_state({"printer": deep})
    loop: dict = {}
    loop["self"] = loop
    with pytest.raises(ScenarioError, match="nested deeper"):
        printer.patch_state({"printer": loop})


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs file permissions")
def test_unreadable_scenario_file(printer, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text("{}")
    secret.chmod(0)
    try:
        status, body = call(printer, "POST", "/_mock/scenario", {"name": str(secret)})
        assert status == 422 and "denied" in body["errors"][0]
    finally:
        secret.chmod(0o600)


@pytest.mark.parametrize("name", ["a" * 300, "/tmp/" + "b" * 300 + ".json", "a\0b", "\ud800"])
def test_unusable_scenario_names(printer, name):
    status, body = call(printer, "POST", "/_mock/scenario", json.dumps({"name": name}).encode())
    assert status == 422, body
    with pytest.raises(ScenarioError):
        load_document(name)


# --- the password override -------------------------------------------------------

@pytest.mark.parametrize("password", ["a\0b", "\ud800", "x" * 16])
def test_password_override_is_checked_like_a_scenario_password(password):
    with pytest.raises(ScenarioError):
        MockPrinter(password=password)


def test_password_override_with_nul_from_a_file(tmp_path):
    pw = tmp_path / "pw"
    pw.write_bytes(b"ab\x00cd\n")
    r = plmock("run", "--password-file", str(pw), "--", sys.executable, "-c", "pass")
    assert r.returncode == 2 and "must not contain NUL" in r.stderr and "Traceback" not in r.stderr


# --- the clock -------------------------------------------------------------------

@pytest.mark.parametrize("seconds", [b"1e308", BIG, b"4354819200", b"-1", b"true", b'"5"'])
def test_clock_advance_is_bounded(printer, seconds):
    status, body = call(printer, "POST", "/_mock/clock/advance", b'{"seconds": ' + seconds + b"}")
    assert status == 400 and "error" in body
    assert printer.core.clock._offset == 0
    with pytest.raises(ValueError):
        printer.advance_clock(json.loads(seconds))


def test_clock_advances_add_up_to_2107_at_most():
    with MockPrinter(frozen_clock=True, epoch=4_000_000_000) as printer:
        assert call(printer, "POST", "/_mock/clock/advance", {"seconds": 354819199})[1]["uptime"] == 354819199
        assert call(printer, "POST", "/_mock/clock/advance", {"seconds": 1})[0] == 400
        assert call(printer, "POST", "/_mock/clock/advance", {"seconds": 0})[0] == 200
        assert printer_get(printer, "/api/version").status == 200
        with Conn("127.0.0.1", printer.port) as conn:
            assert conn.request("GET", "/api/version").status == 401  # a Digest challenge, not a drop
        for reset in ("/_mock/reboot", "/_mock/reset"):
            assert call(printer, "POST", reset)[0] == 200
            with Conn("127.0.0.1", printer.port) as conn:
                r = conn.request("PUT", "/api/v1/files/usb/late.gcode",
                                 [("X-Api-Key", printer.password), ("Content-Length", "3"), ("Overwrite", "?1")],
                                 b"G1\n")
            assert r.status == 201 and json.loads(r.body)["m_timestamp"] == 4354819199


@pytest.mark.parametrize("epoch", [0, 1e300, float("nan"), 4354819200])
def test_epoch_is_a_fat_time(epoch):
    with pytest.raises(ValueError):
        MockPrinter(epoch=epoch)


# --- fault rules -----------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    b'{"match":{"path":"/api/version"},"action":{"type":"status","status":500,"message":"\\ud800"}}',
    b'{"match":{"path":"/api/version"},"action":{"type":"status","status":500,"message":"a\\nb"}}',
    b'{"match":{"path":"/api/version"},"action":{"type":"status","status":500,"body":"\\udfff"}}',
    '{"match":{"path":"/api/version"},"action":{"type":"status","status":500,"body":"x","content_type":"text/€"}}'
    .encode(),
    b'{"match":{"path":"/api/version"},"action":{"type":"status","status":500,"body":"x",'
    b'"content_type":"text/plain\\r\\nX-Injected: 1"}}',
    b'{"action":{"type":"malformed","mode":"wrong_shape","body":' + b"[" * 100 + b"]" * 100 + b"}}",
    b'{"action":{"type":[]}}', b'{"action":{"type":{}}}',
])
def test_fault_rules_the_printer_port_could_not_send(printer, raw):
    status, body = call(printer, "POST", "/_mock/faults", raw)
    assert status == 422, body
    assert call(printer, "GET", "/_mock/faults")[1]["faults"] == []
    assert printer_get(printer, "/api/version").status == 200


@pytest.mark.parametrize("body", [float("nan"), object(), {"a": [float("inf")]}])
def test_wrong_shape_body_must_be_json(printer, body):
    with pytest.raises(FaultError):
        printer.add_fault({"match": {"path": "/api/version"},
                           "action": {"type": "malformed", "mode": "wrong_shape", "body": body}})


@pytest.mark.parametrize("field", ["skip", "times", "keep_bytes"])
def test_fault_counts_print_back_as_json(printer, field):
    """An int of more than 4300 digits can't be turned into text, so GET /_mock/faults would fail."""
    rule = {"match": {"path": "/api/version"}, "action": {"type": "malformed", "mode": "truncate"}}
    target = rule["action"] if field == "keep_bytes" else rule
    target[field] = 10 ** 5000
    with pytest.raises(FaultError):
        printer.add_fault(rule)
    target[field] = 2 ** 31 - 1
    printer.add_fault(rule)
    status, body = call(printer, "GET", "/_mock/faults")
    assert status == 200 and len(body["faults"]) == 1


def test_fault_rules_and_log_entries_are_copies(printer):
    body = {"tools": [1, 2]}
    printer.add_fault({"match": {"path": "/api/v1/tools"},
                       "action": {"type": "malformed", "mode": "wrong_shape", "body": body}})
    body["tools"].append(3)
    printer.faults()[0]["action"]["body"] = "changed"
    assert printer_get(printer, "/api/v1/tools").body == b'{"tools":[1,2]}'
    printer.requests()[-1]["status"] = 999
    assert call(printer, "GET", "/_mock/requests")[1]["entries"][-1]["status"] == 200


# --- control API framing --------------------------------------------------------

def raw_control(printer, data: bytes) -> tuple[int, dict]:
    with Conn("127.0.0.1", printer.core.control_port) as conn:
        conn.send(data)
        r = conn.read_response()
    return r.status, json.loads(r.body)


@pytest.mark.parametrize("data,status", [
    (b"POST /_mock/clock/advance HTTP/1.1\r\nContent-Length: -5\r\n\r\n{}", 400),
    (b"POST /_mock/clock/advance HTTP/1.1\r\nContent-Length: +2\r\n\r\n{}", 400),
    (b"POST /_mock/clock/advance HTTP/1.1\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\n", 413),
    (b"GET /_mock/health HTTP/1.1\r\nX-Long: " + b"a" * 70000 + b"\r\n\r\n", 431),
    (b"GET /_mock/health?" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n", 414),
])
def test_control_request_framing(printer, data, status):
    assert raw_control(printer, data)[0] == status
    assert call(printer, "GET", "/_mock/health")[0] == 200


def test_request_log_limit(printer):
    for _ in range(3):
        printer_get(printer, "/api/version")
    assert [e["seq"] for e in call(printer, "GET", "/_mock/requests?limit=1")[1]["entries"]] == [1]
    assert [e["seq"] for e in call(printer, "GET", "/_mock/requests?since=1&limit=5")[1]["entries"]] == [2, 3]
    for query in ("limit=0", "limit=-1", "limit=x", "since=-1", "since=1.5"):
        assert call(printer, "GET", "/_mock/requests?" + query)[0] == 400, query


# --- the Python API ---------------------------------------------------------------

def test_start_twice_is_refused():
    printer = MockPrinter().start()
    try:
        with pytest.raises(RuntimeError):
            printer.start()
        assert printer_get(printer, "/api/version").status == 200
    finally:
        printer.stop()
    assert printer.stopped


def test_restart_keeps_writing_the_log_file(tmp_path):
    log = tmp_path / "log.jsonl"
    printer = MockPrinter(log_file=str(log)).start()
    printer_get(printer, "/api/version")
    printer.stop()
    printer.start()
    try:
        printer_get(printer, "/api/version")
        printer_get(printer, "/api/v1/info")
    finally:
        printer.stop()
    assert len(log.read_text().splitlines()) == 3


@pytest.mark.parametrize("port", [-1, 65536, 70000])
def test_ports_out_of_range(port):
    with pytest.raises(ValueError):
        MockPrinter(port=port)
    r = plmock("serve", "--port", str(port), "--control-port", "0")
    assert r.returncode == 2 and "port number" in r.stderr and "Traceback" not in r.stderr


# --- the command line -------------------------------------------------------------

CHECK_KEY = r"""
import os, sys, urllib.request
req = urllib.request.Request(os.environ["PRUSALINK_MOCK_URL"] + "/api/version", headers={"X-Api-Key": "mykey"})
print(urllib.request.urlopen(req, timeout=10).status)
"""


def test_password_file_with_a_byte_order_mark(tmp_path):
    pw = tmp_path / "bom.txt"
    pw.write_bytes(b"\xef\xbb\xbfmykey\r\n")
    r = plmock("run", "--password-file", str(pw), "--", sys.executable, "-c", CHECK_KEY)
    assert r.returncode == 0 and r.stdout.strip() == "200", r.stderr


@pytest.mark.parametrize("content,message", [(b"\n", "is empty"), (b"\xffkey\n", "not UTF-8")])
def test_password_file_errors(tmp_path, content, message):
    pw = tmp_path / "pw"
    pw.write_bytes(content)
    r = plmock("run", "--password-file", str(pw), "--", sys.executable, "-c", "pass")
    assert r.returncode == 2 and message in r.stderr and "Traceback" not in r.stderr


@pytest.mark.skipif(os.name != "posix", reason="argv bytes and SIGHUP are POSIX")
def test_password_argument_not_utf8():
    r = subprocess.run([os.fsencode(sys.executable), b"-m", b"prusalink_mock", b"run", b"--password", b"\xff",
                        b"--", b"true"], env=env(), capture_output=True, timeout=60)
    assert r.returncode == 2 and b"Traceback" not in r.stderr


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0 only checks that the process exists
    except ProcessLookupError:
        return False
    return True


# Only signals that ask a process to end. Never one that makes a crash report.
@pytest.mark.skipif(os.name != "posix", reason="SIGHUP and signals to one process are POSIX")
@pytest.mark.parametrize("sig", ["SIGHUP", "SIGTERM", "SIGINT"])
def test_run_ends_the_command_on_a_signal_to_plmock_alone(sig, tmp_path):
    """The command runs in plmock's process group, so a signal sent to plmock's pid only
    must not leave it running without the mock."""
    marker = tmp_path / "child.pid"
    child = f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    proc = subprocess.Popen([sys.executable, "-m", "prusalink_mock", "run", "--", sys.executable, "-c", child],
                            env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    child_pid = None
    try:
        deadline = time.monotonic() + 20
        while not (marker.exists() and marker.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        child_pid = int(marker.read_text())
        time.sleep(0.2)  # plmock installs its handlers right after starting the command
        signum = getattr(signal, sig)
        started = time.monotonic()
        proc.send_signal(signum)
        assert proc.wait(timeout=20) == 128 + signum  # the command's own end, passed on
        assert time.monotonic() - started < 8
        assert not _alive(child_pid)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=20)
        if child_pid is not None and _alive(child_pid):
            os.kill(child_pid, signal.SIGTERM)


def test_huge_numbers_in_a_scenario_file(tmp_path):
    doc = load_document("indx-hy8")
    doc["tools"]["1"]["nozzle_diameter"] = int(BIG)
    path = tmp_path / "huge.json"
    path.write_text(json.dumps(doc))
    r = plmock("run", "--scenario", str(path), "--", sys.executable, "-c", "pass")
    assert r.returncode == 2 and "/tools/1/nozzle_diameter" in r.stderr and "Traceback" not in r.stderr
    path.write_text(json.dumps(dict(doc, tools={"1" * 5000: {}})))
    r = plmock("run", "--scenario", str(path), "--", sys.executable, "-c", "pass")
    assert r.returncode == 2 and "tool keys" in r.stderr and "Traceback" not in r.stderr



# --- the request log ----------------------------------------------------------------

def test_the_password_never_reaches_the_log(tmp_path):
    log = tmp_path / "log.jsonl"
    with MockPrinter(seed=1, log_file=str(log)) as printer:
        key = printer.password
        for head in (f"Authorization: {key} realm=x",
                     f'Authorization: Digest nonce="abc\r\nX-Api-Key: {key}\r\n"',
                     f'Authorization: Digest username="maker {key} x", nonce="0", response="0"',
                     f"Authorization: Basic {key}",
                     f"X-Note: {key}"):
            with Conn("127.0.0.1", printer.port) as conn:
                conn.send(f"GET /api/version HTTP/1.1\r\n{head}\r\n\r\n".encode())
                assert conn.read_response().status == 401
        entries = printer.requests()
    assert len(entries) == 5
    assert key not in json.dumps(entries) and key not in log.read_text()
    assert entries[0]["headers"] == [["Authorization", "<redacted>"]]
    assert entries[2]["auth"]["username"] == "maker <redacted> x"
    assert entries[3]["headers"] == [["Authorization", "Basic <redacted>"]]
