"""Printer-port routing, in the order of the firmware's selectors.

lib/WUI/http_lifetime.cpp:29-40: validate, /api/v1/, /api/ (OctoPrint
compatible), /usb/, /thumb/, static files, 404. Everything under /api/, /usb/
and /thumb/ checks auth before it looks at the path or the method, so a bad
credential gets 401 even where the route would be 404 or 405.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import posixpath
import re
from typing import TYPE_CHECKING, Callable, Optional

from . import render, state as statemod, wire
from .auth import AuthResult
from .wire import CLOSE, ERROR_CLOSE, KEEP, Request, Response

if TYPE_CHECKING:  # pragma: no cover
    from .core import MockCore

NOT_MODELED = "Not modeled by prusalink-mock"
PATH_MAX = 103  # filename_defs::max_path_length (include/buddy/filename_defs.hpp)
ACTIVE_TIMEOUT_S = 10.0  # Server::ACTIVE_TIMEOUT (server.h:168)
READY_STATES = ("IDLE", "STOPPED", "FINISHED")  # printer_state::remote_print_ready
PRINTABLE = (".g", ".gc", ".gco", ".gcode", ".bgcode", ".bgc")  # src/common/filename_type.cpp
_FAT_INVALID = set('"*:<>?|\x7f\\')


def is_protected(target: str) -> bool:
    return (target.startswith(("/api/", "/usb/"))
            or (target.startswith("/thumb/") and len(target) >= len("/thumb/") + 2))


def _page(req: Request, status: int, message: str = "", extra: tuple[str, ...] = ()) -> Response:
    """StatusPage(status, parser, message)."""
    return wire.status_page(status, req.page_handling(status), req.accepts_json, message, extra)


def _not_modeled(req: Request) -> Response:
    return _page(req, 501, NOT_MODELED)


def _get_only(req: Request, body: Callable[[], bytes]) -> Response:
    """get_only (prusa_api_helpers.cpp:146-153)."""
    if req.method == "GET":
        return wire.json_response(body(), req.can_keep_alive())
    return wire.status_page(405, ERROR_CLOSE, req.accepts_json)


def uri_filename(target: str) -> Optional[str]:
    """RequestParser::uri_filename (req_parser.cpp:273-307) plus http::url_decode."""
    raw = target.split("?", 1)[0]
    out = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "+":
            out.append(" ")
        elif ch == "%":
            m = re.match(r"[0-9a-fA-F]{1,2}", raw[i + 1:i + 3])
            if not m:
                return None
            out.append(chr(int(m.group(0), 16)))
            i += 2
        else:
            out.append(ch)
        i += 1
    decoded = "".join(out).split("\0", 1)[0]
    n = len(decoded)
    if n >= 3 and (decoded[:3] == "../" or decoded[-3:] == "/.."):
        return None
    for k in range(0, max(0, n - 4)):
        if decoded[k:k + 4] == "/../":
            return None
    return decoded


def dedup_slashes(path: str) -> str:
    return re.sub(r"/{2,}", "/", path)


def _as_text(latin: str) -> Optional[str]:
    """Decoded URL bytes as a UTF-8 file name, or None if they are not valid UTF-8."""
    try:
        return latin.encode("latin-1").decode("utf-8")
    except UnicodeDecodeError:
        return None


def printer_ready(state: dict) -> bool:
    return state["printer"]["state"] in READY_STATES


def being_printed(core: "MockCore", path: str) -> bool:
    """wui_is_file_being_printed (wui_api.cpp:315-325)."""
    f = core.storage.get(path)
    job = core.state.get("job")
    return (f is not None and job is not None and render.is_printing(core.state)
            and job["file"]["sfn_path"].upper() == f.sfn_path.upper())


async def route(core: "MockCore", req: Request, auth: Optional[AuthResult],
                reader: asyncio.StreamReader, entry: dict) -> Optional[Response]:
    target = req.target
    if is_protected(target):
        assert auth is not None
        if not auth.ok:
            entry["route"] = "auth"
            return _page(req, 401, extra=(auth.challenge,))
        if target.startswith("/api/v1/"):
            return await _v1(core, req, target[len("/api/v1/"):], reader, entry)
        if target.startswith("/api/"):
            return _octo(core, req, target[len("/api/"):], entry)
        entry["route"] = "usb" if target.startswith("/usb/") else "thumb"
        return _not_modeled(req)

    decoded = uri_filename(target)
    if req.method == "GET" and decoded is not None and (len(decoded) <= 1 or decoded == "/index.html"):
        entry["route"] = "static.index"
        return wire.static_response(_index_page(core), "text/html; charset=utf-8", req.can_keep_alive())
    entry["route"] = "unknown"
    return _page(req, 404)


# --- /api/v1/ ----------------------------------------------------------------

async def _v1(core: "MockCore", req: Request, suffix: str, reader: asyncio.StreamReader,
              entry: dict) -> Optional[Response]:
    state = core.state
    if suffix == "storage":
        entry["route"] = "v1.storage"
        return _get_only(req, lambda: render.storage(state))
    if suffix == "info":
        entry["route"] = "v1.info"
        return _get_only(req, lambda: render.info(state))
    if suffix == "tools" and state["firmware"]["has_filament_slots"]:
        entry["route"] = "v1.tools"
        return _get_only(req, lambda: render.tools(state))
    if suffix.startswith("job/"):
        entry["route"] = "v1.job.command"
        return _job_command(core, req, suffix[len("job/"):])
    if suffix == "job":
        entry["route"] = "v1.job"
        if render.has_job(state):
            return _get_only(req, lambda: render.job(state))
        return _page(req, 204)
    if suffix == "status":
        entry["route"] = "v1.status"
        return _get_only(req, lambda: render.status(state))
    if suffix == "transfer":
        entry["route"] = "v1.transfer"
        return _page(req, 204)
    if suffix.startswith("files"):
        return await _files(core, req, reader, entry)
    entry["route"] = "v1.unknown"
    return _page(req, 404)


def _job_command(core: "MockCore", req: Request, job_suffix: str) -> Response:
    """PUT /api/v1/job/{id}/pause|resume, DELETE /api/v1/job/{id} (prusa_link_api_v1.cpp:42-155).

    The id is compared with marlin_vars job_id, which keeps the last job's id
    after the job ends (marlin_server.cpp:1396, 2313-2315, 3730), so a command
    for a job that just ended gets 409 from the state checks, not 404. State
    changes are immediate; the firmware passes through pausing and stopping
    states first.
    """
    m = re.match(r"-?[0-9]+", job_suffix)
    if not m or not -(2 ** 31) <= int(m.group(0)) < 2 ** 31:
        return _page(req, 400)
    state = core.state
    printer = state["printer"]
    if int(m.group(0)) != printer["job_id"]:
        return _page(req, 404)
    if req.method == "PUT":
        slash = job_suffix.find("/")
        if slash < 0:
            return _page(req, 400)
        command = job_suffix[slash + 1:]
        if command == "resume":
            if printer["state"] != "PAUSED":
                return _page(req, 409)
            printer["state"] = "PRINTING"
        elif command == "pause":
            if printer["state"] != "PRINTING":
                return _page(req, 409)
            printer["state"] = "PAUSED"
        else:
            return _page(req, 400)
        return _page(req, 204)
    if req.method == "DELETE":
        if printer["state"] not in ("PRINTING", "PAUSED", "ATTENTION"):
            return _page(req, 409)
        printer["state"] = "STOPPED"
        state.pop("job", None)
        return _page(req, 204)
    return _page(req, 405)


# --- /api/v1/files -----------------------------------------------------------

async def _files(core: "MockCore", req: Request, reader: asyncio.StreamReader,
                 entry: dict) -> Optional[Response]:
    """prusa_link_api_v1.cpp:182-233 with parse_file_url (prusa_api_helpers.cpp:32-99)."""
    entry["route"] = "v1.files"
    decoded = uri_filename(req.target)
    prefix_len = len("/api/v1/files")
    if decoded is None or len(decoded) < prefix_len:
        return _page(req, 404)
    real = decoded[prefix_len:]
    if not (real.startswith("/usb/") or real == "/usb"):
        return _page(req, 403)
    filename = dedup_slashes(real)

    if req.method == "PUT":
        if req.create_folder:
            entry["route"] = "v1.files.mkdir"
            return _create_folder(core, req, filename)
        entry["route"] = "v1.files.put"
        if len(filename.encode("latin-1")) > PATH_MAX:
            return _page(req, 414)
        return await _upload(core, req, filename, reader, entry)
    if req.method in ("GET", "HEAD"):
        entry["route"] = "v1.files.info"
        return _file_info(core, req, filename)
    if req.method == "DELETE":
        entry["route"] = "v1.files.delete"
        return _delete(core, req, filename)
    if req.method == "POST":
        entry["route"] = "v1.files.print"
        return _print_file(core, req, filename, entry)
    return wire.status_page(405, ERROR_CLOSE, req.accepts_json)


def _lookup(core: "MockCore", filename: str) -> tuple[Optional[str], Optional[str]]:
    """(kind, text path) of a decoded file path; kind is "file", "dir" or None."""
    path = _as_text(filename)
    if path is None or not core.state["storage"]["available"]:
        return None, path
    kind = core.storage.kind(path)
    if kind is None:
        f = core.storage.find_short(path)
        if f is not None:
            return "file", f.path
    return kind, path


def _file_extra(core: "MockCore", path: str) -> tuple[str, str]:
    printing = "true" if being_printed(core, path) else "false"
    return ("Read-Only: false\r\n", f"Currently-Printing: {printing}\r\n")


def _file_info(core: "MockCore", req: Request, filename: str) -> Response:
    """FileInfo for one file (file_info.cpp:289-357). Folder listings are not modeled."""
    kind, path = _lookup(core, filename)
    if kind == "file":
        f = core.storage.get(path)
        body = render.file_info(f.sfn_path, posixpath.basename(f.path), f.size, f.m_timestamp)
        keep = req.can_keep_alive()
        extra = _file_extra(core, path)
        if req.method == "HEAD":
            # ContentLengthKeep with no length: neither Content-Length nor chunking.
            head = wire.write_headers(200, "application/json", KEEP if keep else CLOSE, extra=extra)
            return Response(200, head, b"", after=KEEP if keep else CLOSE, content_type="application/json")
        return wire.json_response(body, keep, extra=extra)
    if kind == "dir" or (filename.rstrip("/") == "/usb"):
        return _not_modeled(req)
    return wire.status_page(404, req.plain_handling(), req.accepts_json)


def _delete(core: "MockCore", req: Request, filename: str) -> Response:
    """delete_file (prusa_api_helpers.cpp:101-113)."""
    kind, path = _lookup(core, filename)
    if kind == "file":
        if being_printed(core, path):
            return _page(req, 409, "File is busy")
        if core.upload_path is not None and core.upload_path.upper() == path.upper():
            return _page(req, 409, "File is being transferred")
    if kind is None or not core.storage.remove(path):
        return _page(req, 404)
    return _page(req, 204)


def _print_file(core: "MockCore", req: Request, filename: str, entry: dict) -> Response:
    """print_file (prusa_api_helpers.cpp:128-144). Accepted prints are logged, not simulated."""
    kind, path = _lookup(core, filename)
    if kind is None:
        return wire.status_page(404, ERROR_CLOSE, req.accepts_json)
    if kind == "dir" or not printer_ready(core.state):
        return wire.status_page(409, ERROR_CLOSE, req.accepts_json)
    entry["print_requested"] = path
    return wire.status_page(204, ERROR_CLOSE, req.accepts_json)


def _create_folder(core: "MockCore", req: Request, filename: str) -> Response:
    """create_folder (prusa_api_helpers.cpp:115-126)."""
    path = _as_text(filename)
    if path is None or not core.state["storage"]["available"]:
        return _page(req, 500, "Error creating directory")
    result = core.storage.mkdir(path)
    if result == "exists":
        return _page(req, 409, "Already exists")
    if result == "error":
        return _page(req, 500, "Error creating directory")
    return _page(req, 201)


def _invalid_name(path: str) -> bool:
    return any(ord(ch) < 0x20 or ch in _FAT_INVALID for ch in path)


def _check_filename(core: "MockCore", req: Request, path: Optional[str], filename: str) -> Optional[tuple[int, str]]:
    """GcodeUpload::check_filename (gcode_upload.cpp:334-377), run on the first body bytes."""
    name = filename[len("/usb/"):]
    if not name.lower().endswith(PRINTABLE):
        return 415, "Not a GCODE"
    if path is None:
        return None  # rejected when the file is renamed into place
    pos = name.rfind("/")
    if pos >= 0:
        kind = core.storage.kind("/usb/" + path[len("/usb/"):].rsplit("/", 1)[0])
        if kind == "file":
            return 409, "Conflicting upload path"
        if kind is None:
            return None  # parent folder doesn't exist yet: checked at the end
    if req.overwrite:
        if being_printed(core, path):
            return 409, "File is busy"
        return None
    if core.storage.kind(path) is not None:
        return 409, "File already exists"
    if _invalid_name(path):
        return 422, "Filename contains invalid characters"
    return None


def _finish_upload(core: "MockCore", req: Request, path: Optional[str], data: bytes, entry: dict) -> Response:
    """PutTransfer::done (gcode_upload.cpp:257-315) and the reply (server.cpp:931-941).

    Errors after the body has been read are plain text with Connection: close.
    """
    def fail(status: int, message: str) -> Response:
        return wire.status_page(status, CLOSE, False, message)

    if path is None or _invalid_name(path):
        return fail(422, "Filename contains invalid characters")
    if not core.storage.make_dirs(path):
        return fail(500, "Failed to create a folder")
    kind = core.storage.kind(path)
    if kind == "file":
        if not req.overwrite:
            return fail(409, "File already exists")
        if being_printed(core, path):
            return fail(409, "File is busy")
    elif kind == "dir":
        return fail(409, "Is a directory or ongoing transfer" if req.overwrite else "File already exists")
    stored = core.storage.store(path, data, core.clock.now(), print_after_upload=req.print_after_upload,
                                overwrite=req.overwrite)
    entry["upload"] = {
        "stored_as": stored.path,
        "sfn_path": stored.sfn_path,
        "overwrite": req.overwrite,
        "print_after_upload": req.print_after_upload,
    }
    if req.print_after_upload and not printer_ready(core.state):
        return fail(409, "Can't print right now")
    body = render.file_info(stored.sfn_path, posixpath.basename(stored.path), stored.size, stored.m_timestamp)
    head = wire.write_headers(201, "application/json", CLOSE, extra=_file_extra(core, path))
    return Response(201, head, body, after=CLOSE, is_json=True, content_type="application/json")


async def _read_some(reader: asyncio.StreamReader, n: int) -> bytes:
    return await asyncio.wait_for(reader.read(n), ACTIVE_TIMEOUT_S)


async def _upload(core: "MockCore", req: Request, filename: str, reader: asyncio.StreamReader,
                  entry: dict) -> Optional[Response]:
    """PUT upload (GcodeUpload::start, gcode_upload.cpp:116-151, then step and done)."""
    json_errors = req.accepts_json
    entry["upload"] = {"overwrite": req.overwrite, "print_after_upload": req.print_after_upload}
    if req.content_length is None:
        return wire.status_page(411, ERROR_CLOSE, json_errors)
    if req.content_length == 0:
        return _page(req, 501, "0-sized files aren't implemented")
    if core.upload_path is not None:
        return _page(req, 409, "Another transfer in progress")
    if not core.state["storage"]["available"]:
        return wire.status_page(507, ERROR_CLOSE, json_errors, "Failed to write to location")
    if req.content_length > core.max_upload_bytes:
        return wire.status_page(507, ERROR_CLOSE, json_errors, "USB drive full")

    path = _as_text(filename)
    core.upload_path = path or filename
    digest = hashlib.sha256()
    data = bytearray()
    try:
        remaining = req.content_length
        try:
            first = await _read_some(reader, min(remaining, 65536))
        except asyncio.TimeoutError:
            return wire.status_page(408, CLOSE, False, "Connection timeout")
        if not first:
            entry["error"] = "client closed before the body"
            return None
        error = _check_filename(core, req, path, filename)
        if error is not None:
            entry["body_len"] = len(first)
            return wire.status_page(error[0], ERROR_CLOSE, json_errors, error[1])
        data += first
        remaining -= len(first)
        while remaining > 0:
            try:
                chunk = await _read_some(reader, min(remaining, 65536))
            except asyncio.TimeoutError:
                entry["body_len"] = len(data)
                return wire.status_page(408, CLOSE, False, "Connection timeout")
            if not chunk:
                entry["body_len"] = len(data)
                entry["error"] = "client closed mid-body"
                return None
            data += chunk
            remaining -= len(chunk)
        digest.update(data)
        entry["body_len"] = len(data)
        entry["body_sha256"] = digest.hexdigest()
        return _finish_upload(core, req, path, bytes(data), entry)
    finally:
        core.upload_path = None


# --- /api/ (OctoPrint compatible) ------------------------------------------------

def _octo(core: "MockCore", req: Request, suffix: str, entry: dict) -> Response:
    """prusa_link_api_octo.cpp:34-133."""
    state = core.state
    if suffix == "settings":
        entry["route"] = "octo.settings"
        if req.method != "GET":
            return wire.status_page(405, ERROR_CLOSE, req.accepts_json)
        return wire.static_response(render.SETTINGS, "application/json", req.can_keep_alive())
    if suffix.startswith("files"):
        entry["route"] = "octo.files"
        if req.method == "POST" and req.boundary:
            return _page(req, 410)
        return _not_modeled(req)
    if suffix == "version":
        entry["route"] = "octo.version"
        return _get_only(req, lambda: render.version(state))
    if suffix == "job":
        entry["route"] = "octo.job"
        if req.method in ("GET", "POST"):
            return _not_modeled(req)
        return wire.status_page(405, ERROR_CLOSE, req.accepts_json)
    if suffix == "printer":
        entry["route"] = "octo.printer"
        if req.method != "GET":
            return wire.status_page(405, ERROR_CLOSE, req.accepts_json)
        return _not_modeled(req)
    if suffix in ("download", "transfer"):
        entry["route"] = "octo.transfer"
        return _page(req, 204)
    entry["route"] = "octo.unknown"
    return _page(req, 404)


# --- GET / ---------------------------------------------------------------------

def _index_page(core: "MockCore") -> bytes:
    state = core.state
    p = state["printer"]
    rows = []
    for key, t in state["tools"].items():
        swatch = (f'<span class="sw" style="background:{html.escape(t["color_hex"])}"></span>'
                  if t["color_hex"] else "")
        rows.append(
            "<tr>" + "".join(f"<td>{c}</td>" for c in (
                html.escape(key),
                "yes" if t["loaded"] else "no",
                html.escape(t["material"]),
                html.escape(t["vendor"]),
                swatch + html.escape(t["color"]),
                f'{t["nozzle_diameter"]:.2f}',
                "yes" if t["hardened"] else "no",
                "yes" if t["high_flow"] else "no",
            )) + "</tr>")
    slots = "" if state["firmware"]["has_filament_slots"] else "<p>This firmware has no /api/v1/tools endpoint.</p>"
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>PrusaLink mock: {html.escape(state['name'])}</title>
<style>body{{font-family:sans-serif;margin:2em}}table{{border-collapse:collapse}}td,th{{border:1px solid #999;padding:.3em .6em}}.sw{{display:inline-block;width:1em;height:1em;margin-right:.4em;border:1px solid #333;vertical-align:middle}}</style>
</head><body>
<h1>PrusaLink mock: {html.escape(state['name'])}</h1>
<p>{html.escape(state['description'])}</p>
<p>{html.escape(p['model'])} (printer {html.escape(statemod.printer_version(state))}), firmware {html.escape(state['firmware']['version'])}, state {html.escape(p['state'])}</p>
{slots}<table><tr><th>Tool</th><th>Loaded</th><th>Material</th><th>Vendor</th><th>Color</th><th>Nozzle</th><th>Hardened</th><th>High flow</th></tr>
{''.join(rows)}
</table></body></html>
"""
    return page.encode("utf-8")
