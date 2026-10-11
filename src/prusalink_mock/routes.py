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
    from .core import Input, MockCore

NOT_MODELED = "Not modeled by prusalink-mock"
PATH_MAX = 103  # filename_defs::max_path_length (include/buddy/filename_defs.hpp)
READY_STATES = ("IDLE", "STOPPED", "FINISHED")  # printer_state::remote_print_ready
PRINTABLE = (".g", ".gc", ".gco", ".gcode", ".bgcode", ".bgc")  # src/common/filename_type.cpp
# Characters FatFS refuses in a long name (create_name, lib/Middlewares/Third_Party/FatFs/src/ff.c:2827).
_LFN_INVALID = frozenset('"*:<>?|\x7f')


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


# What strtol takes: leading isspace() in the C locale, a sign, then digits.
_STRTOL = r"[ \t\n\v\f\r]*([+-]?[0-9{}]+)"
_STRTOL_10 = re.compile(_STRTOL.format(""))
_STRTOL_16 = re.compile(_STRTOL.format("a-fA-F"))


def uri_filename(target: str) -> Optional[str]:
    """RequestParser::uri_filename (req_parser.cpp:273-307) plus http::url_decode.

    url_decode reads each %XX with from_chars_light(int, base 16) over the two
    bytes after the % (url_decode.cpp:16-29, str_utils.cpp:102-128), which is
    strtol: white space and a sign come first, so "%+4" is 0x04 and "%-1" is
    0xFF (cast to char).
    """
    raw = target.split("?", 1)[0]
    out = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "+":
            out.append(" ")
        elif ch == "%":
            m = _STRTOL_16.match(raw[i + 1:i + 3].split("\0", 1)[0])
            if not m:
                return None
            out.append(chr(int(m.group(1), 16) & 0xFF))
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


async def route(core: "MockCore", req: Request, auth: Optional[AuthResult], inp: "Input",
                entry: dict) -> Response:
    target = req.target
    if is_protected(target):
        assert auth is not None
        if not auth.ok:
            entry["route"] = "auth"
            return _page(req, 401, extra=(auth.challenge,))
        if target.startswith("/api/v1/"):
            return await _v1(core, req, target[len("/api/v1/"):], inp, entry)
        if target.startswith("/api/"):
            return _octo(core, req, target[len("/api/"):], entry)
        entry["route"] = "usb" if target.startswith("/usb/") else "thumb"
        return _not_modeled(req)

    # The firmware serves its gzipped web UI here (static_file.cpp:21-63). The
    # mock serves its own status page instead, listed under Fidelity in README.md.
    decoded = uri_filename(target)
    if req.method == "GET" and decoded is not None and (len(decoded) <= 1 or decoded == "/index.html"):
        entry["route"] = "static.index"
        return wire.static_response(_index_page(core), "text/html; charset=utf-8", req.can_keep_alive())
    entry["route"] = "unknown"
    return _page(req, 404)


# --- /api/v1/ ----------------------------------------------------------------

async def _v1(core: "MockCore", req: Request, suffix: str, inp: "Input", entry: dict) -> Response:
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
        return await _files(core, req, inp, entry)
    entry["route"] = "v1.unknown"
    return _page(req, 404)


def _job_id(job_suffix: str) -> Optional[int]:
    """get_job_id (prusa_link_api_v1.cpp:42-50): from_chars_light into an int.

    The whole suffix is copied into a 34-byte buffer first, so a longer one
    fails; strtol then skips white space and takes a sign and digits, and the
    rest is ignored (str_utils.cpp:102-128, 172-176).
    """
    if len(job_suffix) > 33:
        return None
    m = _STRTOL_10.match(job_suffix.split("\0", 1)[0])
    if not m:
        return None
    value = int(m.group(1))
    return value if -(2 ** 31) <= value < 2 ** 31 else None


def _job_command(core: "MockCore", req: Request, job_suffix: str) -> Response:
    """PUT /api/v1/job/{id}/pause|resume, DELETE /api/v1/job/{id} (prusa_link_api_v1.cpp:42-155).

    The id is compared with marlin_vars job_id, which keeps the last job's id
    after the job ends (marlin_server.cpp:1396, 2313-2315, 3730), so a command
    for a job that just ended gets 409 from the state checks, not 404. State
    changes are immediate; the firmware passes through pausing and stopping
    states first.
    """
    job_id = _job_id(job_suffix)
    if job_id is None:
        return _page(req, 400)
    state = core.state
    printer = state["printer"]
    if job_id != printer["job_id"]:
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

async def _files(core: "MockCore", req: Request, inp: "Input", entry: dict) -> Response:
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
        return await _upload(core, req, filename, inp, entry)
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
    return kind, core.storage.resolve(path) if kind is not None else path


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
    """delete_file (prusa_api_helpers.cpp:101-113) and remove_file (print_utils.cpp:84-117).

    A PUT in progress doesn't block deleting its destination: the body goes to
    a temporary file that is renamed into place at the end (gcode_upload.cpp:
    142-145). "File is being transferred" only comes from Connect's partial
    transfers (transfer_file_check.cpp:10-29), which the mock doesn't model.
    """
    kind, path = _lookup(core, filename)
    if kind == "file" and being_printed(core, path):
        return _page(req, 409, "File is busy")
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
    """create_folder (prusa_api_helpers.cpp:115-126): mkdir, where EEXIST is 409 and any
    other error 500. A name FatFS refuses is FR_INVALID_NAME (ff.c:2822-2828); a
    control character or a backslash would make another name, as for uploads."""
    if not core.state["storage"]["available"]:
        return _page(req, 500, "Error creating directory")
    if _name_not_modeled(filename):
        return _not_modeled(req)
    path = _as_text(filename)
    if path is None or any(_bad_component(c) for c in filename.split("/")):
        return _page(req, 500, "Error creating directory")
    result = core.storage.mkdir(path)
    if result == "exists":
        return _page(req, 409, "Already exists")
    if result == "error":
        return _page(req, 500, "Error creating directory")
    return _page(req, 201)


def _bad_component(component: str) -> bool:
    """FatFS refuses the name: not UTF-8, or a character a long name can't hold (ff.c:2822-2828).

    stat() and mkdir() fail on it, and rename() reports ENOTDIR, the errno of
    FR_INVALID_NAME (src/buddy/filesystem_fatfs.cpp:70-113).
    """
    text = _as_text(component)
    return text is None or any(ch in _LFN_INVALID for ch in text)


def _name_not_modeled(filename: str) -> bool:
    """FatFS ends a name at a control character and splits folders at a backslash
    (ff.c:51, 2826), so the file would land under another name; not modeled."""
    return any(ord(ch) < 0x20 or ch == "\\" for ch in filename)


def _split(filename: str) -> tuple[list[str], str]:
    """Folder components and file name of a decoded /usb/... path."""
    *folders, base = filename[len("/usb/"):].split("/")
    return folders, base


# try_rename's answer when rename() fails with anything but EISDIR or ENOTDIR
# (gcode_upload.cpp:224-238), as when every numbered short name is taken: FR_DENIED, EACCES.
_NO_SHORT_NAME = "Filename contains invalid characters"


def _check_filename(core: "MockCore", req: Request, filename: str) -> Optional[tuple[int, str]]:
    """GcodeUpload::check_filename (gcode_upload.cpp:334-377), run on the first body bytes."""
    folders, base = _split(filename)
    if not filename[len("/usb/"):].lower().endswith(PRINTABLE):
        return 415, "Not a GCODE"
    if folders:  # file_dir_exists (gcode_upload.cpp:167-189)
        if any(_bad_component(c) for c in folders):
            return None  # stat fails, so the folder counts as missing: checked at the end
        kind = core.storage.kind(_as_text("/usb/" + "/".join(folders)))
        if kind == "file":
            return 409, "Conflicting upload path"
        if kind is None:
            return None  # the folder doesn't exist yet: checked at the end
    if req.overwrite:
        if not _bad_component(base) and being_printed(core, _as_text(filename)):
            return 409, "File is busy"
        return None
    # A check file renamed to the name and back (try_rename, gcode_upload.cpp:192-239)
    if _bad_component(base):
        return 404, "Destination directory does not exist"
    if core.storage.kind(_as_text(filename)) is not None:
        return 409, "File already exists"
    if not core.storage.short_name_free(_as_text(filename)):
        return 422, _NO_SHORT_NAME
    return None


def _finish_upload(core: "MockCore", req: Request, filename: str, data: bytes, entry: dict) -> Response:
    """PutTransfer::done (gcode_upload.cpp:257-315) and the reply (server.cpp:931-941).

    Errors after the body has been read are plain text with Connection: close.
    """
    def fail(status: int, message: str) -> Response:
        return wire.status_page(status, CLOSE, False, message)

    folders, base = _split(filename)
    # make_dirs (src/common/path_utils.cpp:35-51): mkdir fails on a name FatFS refuses
    if any(_bad_component(c) for c in folders):
        return fail(500, "Failed to create a folder")
    folder = _as_text("/".join(["/usb"] + folders))
    if not core.storage.make_dirs(f"{folder}/{base}"):  # takes the file's path
        return fail(500, "Failed to create a folder")
    # try_rename (gcode_upload.cpp:192-239): stat finds nothing, rename gets ENOTDIR
    if _bad_component(base):
        return fail(404, "Destination directory does not exist")
    path = _as_text(filename)
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
    if stored is None:
        return fail(422, _NO_SHORT_NAME)
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


async def _upload(core: "MockCore", req: Request, filename: str, inp: "Input", entry: dict) -> Response:
    """PUT upload: GcodeUpload::start (gcode_upload.cpp:116-151) and step (:379-413), then
    the transfer slot (server.cpp:709-800) and PutTransfer::done.

    A stall of ACTIVE_TIMEOUT gets the bare 408 goodbye; the transfer's own
    "Connection timeout" result is dropped (server.cpp:481-521, 709-730). A
    client close before the body is "Truncated body"; after part of it, the
    transfer slot answers "Connection closed by client".
    """
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
    if _name_not_modeled(filename):
        return _not_modeled(req)

    owner = object()
    core.upload_path, core.upload_owner = _as_text(filename) or filename, owner
    data = bytearray()
    try:
        remaining = req.content_length
        try:
            first = await inp.read(min(remaining, 65536), wire.ACTIVE_TIMEOUT_S)
        except asyncio.TimeoutError:
            entry["error"] = "body timed out"
            return wire.goodbye()
        if not first:
            entry["error"] = "client closed before the body"
            return wire.status_page(400, ERROR_CLOSE, json_errors, "Truncated body")
        error = _check_filename(core, req, filename)
        if error is not None:
            entry["body_len"] = len(first)
            return wire.status_page(error[0], ERROR_CLOSE, json_errors, error[1])
        data += first
        remaining -= len(first)
        while remaining > 0:
            try:
                chunk = await inp.read(min(remaining, 65536), wire.ACTIVE_TIMEOUT_S)
            except asyncio.TimeoutError:
                entry["body_len"] = len(data)
                entry["error"] = "body timed out"
                return wire.goodbye()
            if not chunk:
                entry["body_len"] = len(data)
                entry["error"] = "client closed mid-body"
                return wire.status_page(400, CLOSE, False, "Connection closed by client")
            data += chunk
            remaining -= len(chunk)
        entry["body_len"] = len(data)
        entry["body_sha256"] = hashlib.sha256(data).hexdigest()
        return _finish_upload(core, req, filename, bytes(data), entry)
    finally:
        if core.upload_owner is owner:
            core.upload_path = core.upload_owner = None


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
