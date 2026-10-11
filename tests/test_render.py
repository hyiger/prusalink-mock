"""Renderers against bodies reconstructed from the firmware source (tests/fixtures/firmware)."""

from __future__ import annotations

import pytest

from conftest import FIXTURES
from prusalink_mock import render, state


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.mark.parametrize("scenario,fn,name", [
    ("indx-hy8", render.version, "api_version.json"),
    ("indx-hy8", render.info, "api_v1_info.json"),
    ("indx-hy8", render.storage, "api_v1_storage.json"),
    ("indx-gaps", render.tools, "api_v1_tools_indx.json"),
    ("indx-hy8", render.status, "api_v1_status_idle_indx_no_tool.json"),
    ("indx-printing", render.status, "api_v1_status_printing.json"),
    ("indx-printing", render.job, "api_v1_job_printing.json"),
])
def test_byte_identical_to_firmware_fixture(scenario, fn, name):
    assert fn(state.load(scenario)) == fixture(name)


def test_settings_and_file_info():
    assert render.SETTINGS == fixture("api_settings.json")
    assert render.file_info("/usb/BENCHY~1.BGC", "benchy.bgcode", 1234567, 1728650000) == \
        fixture("api_v1_files_usb_file.json")


def test_status_page_json():
    body, ctype = render.status_page_body(404, "", True)
    assert body == fixture("error_404_accept_json.json") and ctype == "application/json"


def test_status_page_text():
    assert render.status_page_body(401, "", False) == (b"401: Unauthorized\n\n\n", "text/plain")
    assert render.status_page_body(204, "ignored", False) == (b"", "text/plain")
    assert render.status_page_body(418, "", False)[0] == b"418: I'm a Teapot Printer\n\n\n"
    assert render.status_page_body(599, "", False)[0] == b"599: ???\n\n\n"


def test_status_page_is_cut_at_127_bytes():
    body, _ = render.status_page_body(409, "x" * 300, False)
    assert len(body) == 127 and body.startswith(b"409: Conflict\n\nxxx")


@pytest.mark.parametrize("raw,escaped", [
    ('a"b', 'a\\"b'),
    ("back\\slash", "back\\\\slash"),
    ("\b\f\n\r\t", "\\b\\f\\n\\r\\t"),
    ("nul\0", "nul\\u0000"),
    ("\x01ctrl", "\x01ctrl"),
    ("Žluťoučký", "Žluťoučký"),
])
def test_jsonify_str(raw, escaped):
    assert render.jsonify_str(raw) == escaped


@pytest.mark.parametrize("value,precision,text", [
    (0.4, 2, "0.40"),
    (0.6, 2, "0.60"),
    (215.2, 1, "215.2"),
    (-19.0, 1, "-19.0"),
    (37, 2, "37.00"),
    (0.25, 2, "0.25"),
])
def test_float_formatting(value, precision, text):
    assert render.ffixed(value, precision) == text


def test_tools_escape_user_names():
    st = state.load("indx-gaps")
    st["tools"]["1"]["vendor"] = 'My "Best"'
    st["tools"]["1"]["color"] = "Back\\slash"
    st["tools"]["1"]["color_hex"] = "#123456"
    body = render.tools(st).decode()
    assert '"vendor":"My \\"Best\\""' in body and '"color":"Back\\\\slash"' in body


def test_tools_skip_uncalibrated_docks_and_sort_numerically():
    st = state.load("indx-hy8")
    tools = {k: st["tools"][k] for k in ("8", "2", "1")}
    st["tools"] = tools
    body = render.tools(st).decode()
    assert body.index('"1":') < body.index('"2":') < body.index('"8":')
    assert '"3":' not in body


def test_status_with_no_tool_picked_reports_zero_hotend():
    st = state.load("indx-hy8")
    st["printer"]["nozzle"] = {"actual": 30.0, "target": 0.0}
    st["printer"]["fans"] = {"hotend": 1200, "print": 0}
    body = render.status(st).decode()
    assert '"temp_nozzle":0.0' in body and '"flow":0' in body and '"fan_hotend":0' in body


def test_status_omits_xy_while_printing():
    body = render.status(state.load("indx-printing")).decode()
    assert '"axis_x"' not in body and '"axis_y"' not in body and body.startswith('{"job":')
