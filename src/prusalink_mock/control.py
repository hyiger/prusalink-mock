"""Control API: a small JSON HTTP API on its own localhost port.

Tests use it to change the printer's state, add faults, read the request log
and the uploaded files, and move the clock. Control traffic never shows up in
the printer's request log.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .faults import FaultError
from .state import ScenarioError, list_scenarios

if TYPE_CHECKING:  # pragma: no cover
    from .core import MockCore

READ_TIMEOUT_S = 30.0
MAX_BODY = 16 * 1024 * 1024

_REASONS = {200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request", 404: "Not Found",
            405: "Method Not Allowed", 413: "Payload Too Large", 422: "Unprocessable Entity",
            500: "Internal Server Error"}


class _Reply(Exception):
    def __init__(self, status: int, payload: Any):
        super().__init__(status)
        self.status, self.payload = status, payload


def _json_body(body: bytes) -> Any:
    if not body:
        raise _Reply(400, {"error": "expected a JSON body"})
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _Reply(400, {"error": f"invalid JSON: {exc}"})


def _validated(fn, *args) -> Any:
    try:
        return fn(*args)
    except ScenarioError as exc:
        raise _Reply(422, {"error": "validation failed", "errors": exc.errors})


def dispatch(core: "MockCore", method: str, target: str, body: bytes) -> tuple[int, Any]:
    """Route one control request; returns (status, JSON payload or raw bytes)."""
    parts = urlsplit(target)
    path = parts.path.rstrip("/") or "/"
    query = parse_qs(parts.query)
    segs = path.split("/")[1:]
    if not segs or segs[0] != "_mock":
        raise _Reply(404, {"error": "unknown endpoint"})
    rest = segs[1:]
    head = rest[0] if rest else ""

    def allow(*methods: str) -> None:
        if method not in methods:
            raise _Reply(405, {"error": f"use {' or '.join(methods)}"})

    if head == "health" and len(rest) == 1:
        allow("GET")
        return 200, core.health()
    if head == "state" and len(rest) == 1:
        allow("GET", "PUT", "PATCH")
        if method == "GET":
            return 200, core.get_state()
        if method == "PUT":
            return 200, _validated(core.set_state, _json_body(body))
        return 200, _validated(core.patch_state, _json_body(body))
    if head == "tools" and len(rest) == 2:
        allow("PUT", "DELETE")
        key = rest[1]
        if method == "PUT":
            return 200, _validated(core.set_tool, key, _json_body(body))
        try:
            return 200, _validated(core.remove_tool, key)
        except KeyError:
            raise _Reply(404, {"error": f"no tool {key}"})
    if head == "scenarios" and len(rest) == 1:
        allow("GET")
        return 200, {"scenarios": list_scenarios()}
    if head == "scenario" and len(rest) == 1:
        allow("POST")
        doc = _json_body(body)
        name = doc.get("name") if isinstance(doc, dict) else None
        if not isinstance(name, str):
            raise _Reply(400, {"error": 'expected {"name": "<scenario>"}'})
        return 200, _validated(core.load_scenario, name)
    if head == "reset" and len(rest) == 1:
        allow("POST")
        return 200, core.reset()
    if head == "requests" and len(rest) == 1:
        allow("GET", "DELETE")
        if method == "DELETE":
            core.log.clear()
            return 200, {"ok": True}
        try:
            since = int(query.get("since", ["0"])[0])
            limit = int(query["limit"][0]) if "limit" in query else None
        except ValueError:
            raise _Reply(400, {"error": "since and limit must be integers"})
        return 200, {"entries": core.log.since(since, limit), "last_seq": core.log.last_seq}
    if head == "files":
        if len(rest) == 1:
            allow("GET")
            return 200, {"files": core.storage.listing()}
        rel = unquote("/".join(rest[1:]))
        f = core.storage.find_rel(rel)
        allow("GET", "DELETE")
        if f is None:
            raise _Reply(404, {"error": f"no file {rel}"})
        if method == "DELETE":
            core.storage.remove(f.path)
            return 200, {"ok": True}
        return 200, f.data
    if head == "faults":
        if len(rest) == 1:
            allow("GET", "POST", "DELETE")
            if method == "GET":
                return 200, {"faults": [r.describe() for r in core.faults.rules]}
            if method == "DELETE":
                core.faults.clear()
                return 200, {"ok": True}
            try:
                rule = core.faults.add(_json_body(body))
            except FaultError as exc:
                raise _Reply(422, {"error": str(exc)})
            return 201, rule.describe()
        if len(rest) == 2:
            allow("DELETE")
            try:
                removed = core.faults.remove(int(rest[1]))
            except ValueError:
                removed = False
            if not removed:
                raise _Reply(404, {"error": f"no fault {rest[1]}"})
            return 200, {"ok": True}
    if head == "clock" and rest[1:] == ["advance"]:
        allow("POST")
        doc = _json_body(body)
        seconds = doc.get("seconds") if isinstance(doc, dict) else None
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds)
                or seconds < 0):
            raise _Reply(400, {"error": 'expected {"seconds": <non-negative number>}'})
        core.advance_clock(seconds)
        return 200, {"uptime": core.clock.uptime()}
    if head == "reboot" and len(rest) == 1:
        allow("POST")
        core.reboot()
        return 200, {"ok": True}
    if head == "shutdown" and len(rest) == 1:
        allow("POST")
        core.request_stop()
        return 200, {"ok": True}
    raise _Reply(404, {"error": "unknown endpoint"})


async def _read_request(reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> Optional[tuple[str, str, bytes]]:
    line = await asyncio.wait_for(reader.readline(), READ_TIMEOUT_S)
    if not line:
        return None
    parts = line.decode("latin-1").split()
    if len(parts) != 3:
        raise _Reply(400, {"error": "bad request line"})
    method, target = parts[0].upper(), parts[1]
    length = 0
    expect_continue = False
    while True:
        h = await asyncio.wait_for(reader.readline(), READ_TIMEOUT_S)
        h = h.rstrip(b"\r\n")
        if not h:
            break
        name, _, value = h.decode("latin-1").partition(":")
        name = name.strip().lower()
        if name == "content-length":
            try:
                length = int(value.strip())
            except ValueError:
                raise _Reply(400, {"error": "bad Content-Length"})
        elif name == "expect":
            expect_continue = value.strip().lower().startswith("100-continue")
    if length > MAX_BODY:
        raise _Reply(413, {"error": "body too large"})
    if expect_continue and length:
        # libcurl adds Expect to every PUT and waits a second for this
        # (RFC 9110, 10.1.1). The printer port never sends it, like the firmware.
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        await writer.drain()
    body = await asyncio.wait_for(reader.readexactly(length), READ_TIMEOUT_S) if length else b""
    return method, target, body


async def handle_control(core: "MockCore", reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        try:
            req = await _read_request(reader, writer)
            if req is None:
                return
            status, payload = dispatch(core, *req)
        except _Reply as reply:
            status, payload = reply.status, reply.payload
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return
        except Exception as exc:  # report bugs to the caller instead of dropping the connection
            status, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
        if isinstance(payload, bytes):
            body, ctype = payload, "application/octet-stream"
        else:
            body, ctype = (json.dumps(payload, indent=1) + "\n").encode("utf-8"), "application/json"
        head = (f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
        writer.write(head.encode("latin-1") + body)
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass
