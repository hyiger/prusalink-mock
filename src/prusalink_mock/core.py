"""The mock printer: listeners, connection handling, state and faults.

Everything runs on one asyncio loop, so state changes need no locks. Outside
code reaches it through the control port (control.py) or through
:class:`prusalink_mock.MockPrinter`, which runs the loop on a thread.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from typing import Any, Optional

from . import auth as authmod
from . import routes, state as statemod, wire
from .faults import FaultSet
from .reqlog import RequestLog
from .storage import Storage
from .wire import ERROR_CLOSE, KEEP, Request, Response

IDLE_TIMEOUT_S = 60.0  # Server::IDLE_TIMEOUT (server.h:161)
ACTIVE_TIMEOUT_S = 10.0  # Server::ACTIVE_TIMEOUT (server.h:168)
LINGER_MAX_S = 60.0
MAX_HEAD_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 512 * 1024 * 1024

HTML_PORTAL = (b"<!doctype html><html><head><title>Sign in</title></head>"
               b"<body><h1>Network login required</h1><p>Please sign in to continue.</p></body></html>\n")


def _url(host: str, port: int) -> str:
    if host in ("", "0.0.0.0", "127.0.0.1", "localhost", "::"):
        host = "127.0.0.1"
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


class MockCore:
    def __init__(self, scenario: str = statemod.DEFAULT_SCENARIO, *, host: str = "127.0.0.1", port: int = 8088,
                 control_host: str = "127.0.0.1", control_port: int = 8089, password: Optional[str] = None,
                 seed: Optional[int] = None, log_file: Optional[str] = None, epoch: Optional[float] = None,
                 frozen_clock: bool = False, max_upload_bytes: int = MAX_UPLOAD_BYTES) -> None:
        if password is not None and len(password.encode("utf-8")) > statemod.PASSWORD_MAX:
            raise statemod.ScenarioError([f"password: longer than the firmware's {statemod.PASSWORD_MAX} bytes"])
        self.password_override = password
        self.scenario_ref = scenario
        self.base_state = self._with_password(statemod.load(scenario))
        self.state: dict[str, Any] = copy.deepcopy(self.base_state)
        self.clock = authmod.Clock(frozen=frozen_clock, epoch=epoch)
        self.nonces = authmod.NonceSource(self.clock, seed)
        self.storage = Storage()
        self.faults = FaultSet()
        self.log = RequestLog(log_file)
        self.max_upload_bytes = max_upload_bytes
        self.upload_path: Optional[str] = None
        self.host, self.port = host, port
        self.control_host, self.control_port = control_host, control_port
        self._servers: list[asyncio.base_events.Server] = []
        self._tasks: set[asyncio.Task] = set()
        self._next_conn = 1
        self._t0 = time.monotonic()
        self._stop_requested: Optional[asyncio.Event] = None

    # --- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        from .control import handle_control

        self._stop_requested = asyncio.Event()
        printer = await asyncio.start_server(self._handle_printer, self.host, self.port)
        self._servers.append(printer)
        self.port = printer.sockets[0].getsockname()[1]
        control = await asyncio.start_server(lambda r, w: self._track(handle_control(self, r, w)),
                                             self.control_host, self.control_port)
        self._servers.append(control)
        self.control_port = control.sockets[0].getsockname()[1]

    async def _track(self, coro) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            await coro
        finally:
            self._tasks.discard(task)

    def request_stop(self) -> None:
        if self._stop_requested is not None:
            self._stop_requested.set()

    async def wait_stop_requested(self) -> None:
        assert self._stop_requested is not None
        await self._stop_requested.wait()

    async def stop(self) -> None:
        """Close the listeners and cancel every connection, hung ones included."""
        for server in self._servers:
            server.close()
        current = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        for server in self._servers:
            try:
                await asyncio.wait_for(server.wait_closed(), 5)
            except (asyncio.TimeoutError, Exception):
                pass
        self._servers = []
        self.log.close()

    @property
    def url(self) -> str:
        return _url(self.host, self.port)

    @property
    def control_url(self) -> str:
        return _url(self.control_host, self.control_port)

    @property
    def password(self) -> str:
        return self.state["auth"]["password"]

    # --- state operations (control API and MockPrinter) ------------------------

    def _with_password(self, st: dict) -> dict:
        if self.password_override is not None:
            st["auth"]["password"] = self.password_override
        return st

    def health(self) -> dict:
        return {"ok": True, "url": self.url, "control_url": self.control_url,
                "scenario": self.state["name"], "pid": os.getpid()}

    def get_state(self) -> dict:
        return statemod.public(self.state)

    def set_state(self, doc: Any) -> dict:
        """Replace the state. Without an ``auth`` key the current password is kept."""
        if isinstance(doc, dict) and "auth" not in doc:
            doc = dict(doc, auth={"password": self.password})
        self.state = statemod.normalize(doc)
        return self.get_state()

    def patch_state(self, patch: Any) -> dict:
        if not isinstance(patch, dict):
            raise statemod.ScenarioError(["/: a patch must be a JSON object"])
        self.state = statemod.normalize(statemod.merge_patch(self.state, patch))
        return self.get_state()

    def set_tool(self, key: str, tool: Any) -> dict:
        doc = copy.deepcopy(self.state)
        doc["tools"][str(key)] = tool
        self.state = statemod.normalize(doc)
        return self.get_state()

    def remove_tool(self, key: str) -> dict:
        doc = copy.deepcopy(self.state)
        if doc["tools"].pop(str(key), None) is None:
            raise KeyError(key)
        self.state = statemod.normalize(doc)
        return self.get_state()

    def load_scenario(self, name: str) -> dict:
        """Switch scenario. Files are cleared; faults and the request log stay."""
        base = self._with_password(statemod.load(name))
        self.base_state, self.scenario_ref = base, name
        self.state = copy.deepcopy(base)
        self.storage.clear()
        return self.get_state()

    def reset(self) -> dict:
        """Reload the scenario and clear faults, the log, files and the clock (a reboot)."""
        self.state = copy.deepcopy(self.base_state)
        self.storage.clear()
        self.faults.clear()
        self.log.clear()
        self.reboot()
        return self.get_state()

    def reboot(self) -> None:
        """New nonce random half and uptime 0, so every old nonce is stale."""
        self.clock.reboot()
        self.nonces.reboot()

    def advance_clock(self, seconds: float) -> None:
        self.clock.advance(seconds)

    # --- printer port ------------------------------------------------------------

    async def _handle_printer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        conn_id = self._next_conn
        self._next_conn += 1
        try:
            while True:
                lines = await self._read_head(reader)
                if lines is None:
                    break
                req = wire.parse_head(lines) if lines else Request(method="", target="", version="", error=400)
                if not await self._serve(req, reader, writer, conn_id):
                    break
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            pass
        finally:
            self._tasks.discard(task)
            try:
                writer.close()
            except Exception:
                pass

    async def _read_head(self, reader: asyncio.StreamReader) -> Optional[list[bytes]]:
        """Head lines without terminators; [] for an unparseable head; None on EOF or idle timeout."""
        lines: list[bytes] = []
        total = 0
        while True:
            timeout = ACTIVE_TIMEOUT_S if lines else IDLE_TIMEOUT_S
            try:
                line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                return None
            except asyncio.LimitOverrunError:
                return []
            total += len(line)
            if total > MAX_HEAD_BYTES:
                return []
            line = line[:-1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if not lines:
                lines.append(line)
                if not line:
                    return []
                continue
            if not line:
                return lines
            lines.append(line)

    def _entry(self, req: Request, conn_id: int) -> dict:
        return {
            "seq": 0,
            "t_ms": round((time.monotonic() - self._t0) * 1000, 1),
            "conn": conn_id,
            "method": req.method,
            "target": req.target,
            "path": req.path(),
            "version": req.version,
            "headers": [],  # filled, redacted, when the entry is logged
            "auth": None,
            "body_len": 0,
            "route": None,
            "status": None,
            "close": False,
            "fault": None,
        }

    async def _serve(self, req: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     conn_id: int) -> bool:
        """Answer one request; returns whether the connection stays open."""
        started = time.monotonic()
        entry = self._entry(req, conn_id)
        auth: Optional[authmod.AuthResult] = None
        try:
            resp: Optional[Response] = None
            rule = None
            if req.error is not None:
                entry["route"] = "invalid"
                handling = ERROR_CLOSE if req.error == 400 else req.plain_handling()
                resp = wire.status_page(req.error, handling, req.accepts_json)
            elif not req.method_known:
                entry["route"] = "invalid"
                resp = wire.status_page(405, req.page_handling(405), req.accepts_json, "Unrecognized method")
            else:
                if routes.is_protected(req.target):
                    auth = authmod.check(req.headers, req.method, req.target, self.password, self.nonces)
                    entry["auth"] = auth.log_dict()
                auth_state = "none" if auth is None or not auth.present else ("ok" if auth.ok else "fail")
                rule = self.faults.pick(req.method, req.path(), auth_state)
                action = rule.action if rule else {}
                kind = action.get("type")
                if rule:
                    entry["fault"] = rule.id
                if kind == "delay":
                    await asyncio.sleep(action["ms"] / 1000.0)
                elif kind == "hang":
                    entry["close"] = True
                    await self._hang(reader, float(action.get("seconds", 30.0)))
                    return False
                elif kind == "drop" and action["when"] == "before_response":
                    entry["close"] = True
                    await self._linger(reader, writer)
                    return False
                elif kind == "status":
                    resp = self._fault_status(req, action)
                if resp is None:
                    resp = await routes.route(self, req, auth, reader, entry)
                    if resp is None:
                        entry["close"] = True
                        return False
                if kind == "malformed":
                    resp = self._malformed(req, resp, action)
                if kind == "drop":  # mid_body
                    data = resp.wire()
                    cut = len(resp.head) + action.get("after_bytes", (len(data) - len(resp.head)) // 2)
                    entry.update(status=resp.status, close=True)
                    writer.write(data[:cut])
                    await self._linger(reader, writer)
                    return False
            entry["status"] = resp.status
            entry["close"] = resp.after != KEEP
            writer.write(resp.wire())
            await writer.drain()
            if resp.after == KEEP:
                return True
            if resp.after == ERROR_CLOSE:
                await self._linger(reader, writer)
            return False
        finally:
            entry["headers"] = [[n, authmod.redact_header(n, v, auth)] for n, v in req.headers]
            entry["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
            self.log.add(entry)

    async def _linger(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Half-close, then read and drop input until the client closes (handler.cpp:15-24).

        Closing with unread input would make the kernel send a reset, and the
        client would report a connection error instead of the HTTP status.
        """
        loop = asyncio.get_running_loop()
        try:
            await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
            deadline = loop.time() + LINGER_MAX_S
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    break
                data = await asyncio.wait_for(reader.read(65536), min(ACTIVE_TIMEOUT_S, left))
                if not data:
                    break
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass

    async def _hang(self, reader: asyncio.StreamReader, seconds: float) -> None:
        async def until_eof() -> None:
            while await reader.read(65536):
                pass

        try:
            await asyncio.wait_for(until_eof(), seconds)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass

    def _fault_status(self, req: Request, action: dict) -> Response:
        status = action["status"]
        if action.get("close") or req.has_body():
            handling = ERROR_CLOSE
        else:
            handling = req.page_handling(status)
        if "body" in action:
            body = action["body"].encode("utf-8")
            ctype = action.get("content_type", "text/plain")
            head = wire.write_headers(status, ctype, handling, len(body))
            return Response(status, head, body, after=handling, content_type=ctype)
        return wire.status_page(status, handling, req.accepts_json, action.get("message", ""))

    def _malformed(self, req: Request, resp: Response, action: dict) -> Response:
        mode = action["mode"]
        keep = resp.after == KEEP
        if mode == "html":
            out = wire.static_response(HTML_PORTAL, "text/html; charset=utf-8", keep)
        elif mode == "truncate":
            n = action.get("keep_bytes", len(resp.body) // 2)
            out = wire.json_response(resp.body[:n], keep)
        else:  # wrong_shape
            if "body" in action:
                body = json.dumps(action["body"], separators=(",", ":")).encode("utf-8")
            elif resp.is_json:
                body = b"[" + resp.body + b"]"
            else:
                body = b"[]"
            out = wire.json_response(body, keep)
        if resp.after == ERROR_CLOSE:
            out.after = ERROR_CLOSE
        return out
