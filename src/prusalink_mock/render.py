"""JSON bodies rendered the way the firmware renders them.

The firmware writes JSON by hand with ``snprintf`` (lib/WUI/link_content/
basic_gets.cpp, lib/WUI/nhttp/status_renderer.cpp, tools_renderer.cpp,
file_info.cpp), so key order, float precision and string escaping are fixed.
These functions reproduce that output byte for byte; they never go through
``json.dumps``.
"""

from __future__ import annotations

import posixpath
import struct
from typing import Any, Mapping

from . import state as statemod

# src/common/json_encode.cpp:21-29 plus the \0 special case at :64-66.
# Everything else, including other control characters and non-ASCII, passes
# through unescaped.
_ESCAPES = {
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    '"': '\\"',
    "\\": "\\\\",
    "\0": "\\u0000",
}

API_VERSION = "2.0.0"  # PL_VERSION_STRING, basic_gets.h
SERVER_VERSION = "2.1.2"  # LWIP_VERSION_STRING

# lib/WUI/nhttp/headers.cpp:20-49
STATUS_TEXT = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    204: "No Content",
    206: "Partial Content",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    409: "Conflict",
    410: "Gone",
    411: "Length Required",
    413: "Payload Too Large",
    414: "URI Too Long",
    415: "Unsupported Media Type",
    418: "I'm a Teapot Printer",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Temporarily Unavailable",
    504: "Gateway Timeout",
    507: "Insufficient Storage",
}


def status_text(status: int) -> str:
    """Reason phrase; unknown codes get "???" like StatusText::find."""
    return STATUS_TEXT.get(status, "???")


def jsonify_str(value: str) -> str:
    """Port of jsonify_str (src/common/json_encode.cpp:58-72)."""
    return "".join(_ESCAPES.get(ch, ch) for ch in value)


def f32(value: float) -> float:
    """Round-trip through a 32-bit float, as the firmware stores these values."""
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def ffixed(value: float, precision: int) -> str:
    """``%.*f`` of a float value (segmented_json.cpp:108-110)."""
    return "%.*f" % (precision, f32(value))


def _s(name: str, value: str) -> str:
    return f'"{name}":"{jsonify_str(value)}"'


def _b(name: str, value: bool) -> str:
    return f'"{name}":{"true" if value else "false"}'


def _i(name: str, value: int) -> str:
    return f'"{name}":{int(value)}'


def _f(name: str, value: float, precision: int) -> str:
    return f'"{name}":{ffixed(value, precision)}'


def _obj(*fields: str) -> str:
    return "{" + ",".join(fields) + "}"


def version(state: Mapping[str, Any]) -> bytes:
    """GET /api/version (basic_gets.cpp:200-226)."""
    p = state["printer"]
    body = _obj(
        _s("api", API_VERSION),
        _s("server", SERVER_VERSION),
        _f("nozzle_diameter", p["nozzle_diameter"], 2),
        _s("text", "PrusaLink"),
        _s("hostname", statemod.hostname(state)),
        _s("firmware", state["firmware"]["version"]),
        _s("printer", statemod.printer_version(state)),
        '"capabilities":' + _obj(_b("upload-by-put", True)),
    )
    return body.encode()


def info(state: Mapping[str, Any]) -> bytes:
    """GET /api/v1/info (basic_gets.cpp:228-253)."""
    p = state["printer"]
    body = _obj(
        _f("nozzle_diameter", p["nozzle_diameter"], 2),
        _b("mmu", p["mmu"]),
        _s("serial", p["serial"]),
        _s("hostname", statemod.hostname(state)),
        _i("min_extrusion_temp", p["min_extrusion_temp"]),
    )
    return body.encode()


def storage(state: Mapping[str, Any]) -> bytes:
    """GET /api/v1/storage (basic_gets.cpp:473-490)."""
    entry = _obj(
        _s("path", "/usb/"),
        _s("name", "usb"),
        _s("type", "USB"),
        _b("read_only", False),
        _b("available", state["storage"]["available"]),
    )
    return ('{"storage_list":[' + entry + "]}").encode()


def tools(state: Mapping[str, Any]) -> bytes:
    """GET /api/v1/tools (tools_renderer.cpp:34-65)."""
    parts = []
    for key in sorted(state["tools"], key=int):
        t = state["tools"][key]
        parts.append(
            f'"{int(key)}":'
            + _obj(
                _b("loaded", t["loaded"]),
                _s("material", t["material"]),
                _s("vendor", t["vendor"]),
                _s("color", t["color"]),
                _s("color_hex", t["color_hex"]),
                _f("nozzle_diameter", t["nozzle_diameter"], 2),
                _b("hardened", t["hardened"]),
                _b("high_flow", t["high_flow"]),
            )
        )
    return ('{"tools":{' + ",".join(parts) + "}}").encode()


def has_job(state: Mapping[str, Any]) -> bool:
    """printer_state::has_job (src/state/printer_state.cpp:586-598)."""
    return state.get("job") is not None and state["printer"]["state"] in ("PRINTING", "PAUSED", "ATTENTION")


def is_printing(state: Mapping[str, Any]) -> bool:
    """marlin_server::is_printing, approximated from the reported state."""
    return state["printer"]["state"] in ("PRINTING", "PAUSED") or (
        state["printer"]["state"] == "ATTENTION" and has_job(state)
    )


# HAS_INDX builds (ProjectOptions.cmake:827)
INDX_MODELS = ("COREONEINDX", "COREONELINDX")


def _active_hotend(state: Mapping[str, Any]) -> tuple[float, float, int, int, int]:
    """Nozzle temperature, target, flow and the two fan RPMs of the active hotend.

    With no tool picked, active_hotend() is the extra NoTool slot
    (marlin_vars.hpp:402-404) and flow is 0 (status_renderer.cpp:74). Nothing
    writes that slot's temperatures. On the INDX the server loop copies the
    head's fan RPMs into it (marlin_server.cpp:3653-3660), so the fans still
    report; on other toolchangers the slot stays all zero.
    """
    p = state["printer"]
    if p["active_tool"] == 0:
        if p["model"] in INDX_MODELS:
            return 0.0, 0.0, 0, p["fans"]["hotend"], p["fans"]["print"]
        return 0.0, 0.0, 0, 0, 0
    return p["nozzle"]["actual"], p["nozzle"]["target"], p["flow"], p["fans"]["hotend"], p["fans"]["print"]


def status(state: Mapping[str, Any]) -> bytes:
    """GET /api/v1/status (status_renderer.cpp:19-82). Transfers are not modeled."""
    p = state["printer"]
    parts = []
    if has_job(state):
        job = state["job"]
        fields = [_i("id", p["job_id"]), _f("progress", job["progress"], 2)]
        if job.get("time_remaining") is not None:
            fields.append(_i("time_remaining", job["time_remaining"]))
        if job.get("filament_change_in") is not None:
            fields.append(_i("filament_change_in", job["filament_change_in"]))
        fields.append(_i("time_printing", job["time_printing"]))
        parts.append('"job":' + _obj(*fields))
    parts.append('"storage":' + _obj(_s("path", "/usb/"), _s("name", "usb"), _b("read_only", False)))
    temp, target, flow, fan_hotend, fan_print = _active_hotend(state)
    printer = [
        _s("state", p["state"]),
        _f("temp_bed", p["bed"]["actual"], 1),
        _f("target_bed", p["bed"]["target"], 1),
        _f("temp_nozzle", temp, 1),
        _f("target_nozzle", target, 1),
        _f("axis_z", p["axis"]["z"], 1),
    ]
    if not is_printing(state):
        printer.append(_f("axis_x", p["axis"]["x"], 1))
        printer.append(_f("axis_y", p["axis"]["y"], 1))
    printer += [
        _i("flow", flow),
        _i("speed", p["speed"]),
        _i("fan_hotend", fan_hotend),
        _i("fan_print", fan_print),
    ]
    parts.append('"printer":' + _obj(*printer))
    return _obj(*parts).encode()


def _job_state(state: Mapping[str, Any]) -> str:
    """get_job_v1 state mapping (basic_gets.cpp:389-410)."""
    s = state["printer"]["state"]
    if s == "PRINTING":
        return "PRINTING"
    if s in ("PAUSED", "ATTENTION"):
        return "PAUSED"
    if s in ("FINISHED", "STOPPED"):
        return s
    return "ERROR"


def job(state: Mapping[str, Any]) -> bytes:
    """GET /api/v1/job while a job exists (basic_gets.cpp:388-459)."""
    j = state["job"]
    f = j["file"]
    sfn_path = f["sfn_path"]
    escaped = jsonify_str(sfn_path)
    fields = [
        _i("id", state["printer"]["job_id"]),
        _s("state", _job_state(state)),
        _f("progress", j["progress"], 2),
    ]
    if j.get("time_remaining") is not None:
        fields.append(_i("time_remaining", j["time_remaining"]))
    fields.append(_i("time_printing", j["time_printing"]))
    refs = '"refs":{' + f'"icon":"/thumb/s{escaped}",' + f'"thumbnail":"/thumb/l{escaped}",' + _s("download", sfn_path) + "}"
    file_fields = [
        refs,
        _s("name", posixpath.basename(sfn_path)),
        _s("display_name", f["display_name"]),
        _s("path", posixpath.dirname(sfn_path)),
    ]
    if f.get("size") is not None:
        file_fields.append(_i("size", f["size"]))
        file_fields.append(_i("m_timestamp", f["m_timestamp"]))
    fields.append('"file":' + _obj(*file_fields))
    return _obj(*fields).encode()


def file_info(sfn_path: str, display_name: str, size: int, m_timestamp: int) -> bytes:
    """v1 file info of a printable file (file_info.cpp:232-264).

    Used for GET /api/v1/files/usb/<file> and for the 201 body after an upload.
    """
    escaped = jsonify_str(sfn_path)
    refs = '"refs":{' + f'"icon":"/thumb/s{escaped}",' + f'"thumbnail":"/thumb/l{escaped}",' + _s("download", sfn_path) + "}"
    body = _obj(
        _s("name", posixpath.basename(sfn_path)),
        _b("ro", False),
        _s("type", "PRINT_FILE"),
        _i("m_timestamp", m_timestamp),
        _i("size", size),
        refs,
        _s("display_name", display_name),
    )
    return body.encode()


SETTINGS = b'{"printer": {}}'  # prusa_link_api_octo.cpp:50-52


def status_page_body(status: int, message: str, json_content: bool) -> tuple[bytes, str]:
    """Body and content type of a firmware StatusPage (status_page.cpp:42-66).

    The page is formatted into a 128-byte buffer, so it is cut at 127 bytes.
    """
    if status in (204, 304):
        return b"", "text/plain"
    text = status_text(status)
    if json_content:
        content = '{"title": "%u: %s","message":"%s"}' % (status, jsonify_str(text), jsonify_str(message))
        ctype = "application/json"
    else:
        content = "%u: %s\n\n%s\n" % (status, text, message)
        ctype = "text/plain"
    return content.encode("utf-8")[:127], ctype
