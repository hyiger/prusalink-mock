"""The mock printer: listeners, connection handling, state and faults.

Everything runs on one asyncio loop, so state changes need no locks. Outside
code reaches it through the control port (control.py) or through
:class:`prusalink_mock.MockPrinter`, which runs the loop on a thread.
"""

from __future__ import annotations

import asyncio
import copy
import os
import socket
import sys
import time
import traceback
from typing import Any, Optional

from . import auth as authmod
from . import routes, state as statemod, wire
from .faults import FaultRule, FaultSet
from .reqlog import RequestLog
from .storage import Storage
from .wire import ERROR_CLOSE, KEEP, Request, Response

LINGER_MAX_S = 60.0
MAX_UPLOAD_BYTES = 512 * 1024 * 1024

HTML_PORTAL = (b"<!doctype html><html><head><title>Sign in</title></head>"
               b"<body><h1>Network login required</h1><p>Please sign in to continue.</p></body></html>\n")


def _connect_address(server: asyncio.base_events.Server) -> tuple[str, int]:
    """An address a client can reach the server on, from the sockets it bound.

    A wildcard host, or a name with several addresses, gets one socket per
    address, each on its own port when the port was 0. IPv6 sockets are
    IPv6-only, so the URL has to name a socket of the right family: the IPv4
    one when there is one, otherwise IPv6.
    """
    socks = server.sockets
    chosen = next((s for s in socks if s.family == socket.AF_INET), socks[0])
    ip, port = chosen.getsockname()[:2]
    if chosen.family == socket.AF_INET:
        return ("127.0.0.1" if ip == "0.0.0.0" else ip), port
    return ("::1" if ip == "::" else ip.split("%", 1)[0]), port


def _url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


class Input:
    """What a client sent, with the bytes the head parser left over.

    Pipelined requests and the start of a body can arrive with the head.
    """

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.buf = bytearray()

    async def fill(self, timeout: float) -> bool:
        """Wait for more bytes; False at EOF. Raises asyncio.TimeoutError."""
        data = await asyncio.wait_for(self.reader.read(65536), timeout)
        if not data:
            return False
        self.buf += data
        return True

    async def read(self, n: int, timeout: float) -> bytes:
        """Up to n bytes; b"" at EOF. Raises asyncio.TimeoutError."""
        if not self.buf and not await self.fill(timeout):
            return b""
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


class MockCore:
    def __init__(self, scenario: str = statemod.DEFAULT_SCENARIO, *, host: str = "127.0.0.1", port: int = 8088,
                 control_host: str = "127.0.0.1", control_port: int = 8089, password: Optional[str] = None,
                 seed: Optional[int] = None, log_file: Optional[str] = None, epoch: Optional[float] = None,
                 frozen_clock: bool = False, max_upload_bytes: int = MAX_UPLOAD_BYTES) -> None:
        if password is not None and statemod.password_errors(password):
            raise statemod.ScenarioError(statemod.password_errors(password))
        for name, value in (("port", port), ("control_port", control_port)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
                raise ValueError(f"{name} must be 0..65535, not {value!r}")
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
        self.upload_owner: Optional[object] = None
        self.generation = 0  # moves on when a reset or a scenario switch drops the connections
        self.host, self.port = host, port
        self.control_host, self.control_port = control_host, control_port
        self.connect_host, self.control_connect_host = host, control_host
        self._servers: list[asyncio.base_events.Server] = []
        self._tasks: set[asyncio.Task] = set()
        self._printer_tasks: set[asyncio.Task] = set()
        self._next_conn = 1
        self._t0 = time.monotonic()
        self._stop_requested: Optional[asyncio.Event] = None

    # --- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Bind both ports; RuntimeError if they are bound already. stop() allows a new start()."""
        from .control import handle_control

        if self._servers:
            raise RuntimeError("the mock is already started")
        self._stop_requested = asyncio.Event()
        self.log.open()
        try:
            printer = await asyncio.start_server(self._handle_printer, self.host, self.port)
            self._servers.append(printer)
            self.connect_host, self.port = _connect_address(printer)
            control = await asyncio.start_server(lambda r, w: self._track(handle_control(self, r, w)),
                                                 self.control_host, self.control_port)
            self._servers.append(control)
            self.control_connect_host, self.control_port = _connect_address(control)
        except BaseException:
            for server in self._servers:
                server.close()
                await server.wait_closed()
            self._servers = []
            self.log.close()
            raise

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
        return _url(self.connect_host, self.port)

    @property
    def control_url(self) -> str:
        return _url(self.control_connect_host, self.control_port)

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

    def _keep_password(self, doc: dict, auth: Any) -> dict:
        """``doc`` with the current password unless ``auth`` sets one.

        GET /_mock/state leaves the password out, so a document without one keeps it,
        and so does ``"auth": null``: a test can't switch the password by accident.
        """
        if auth is None or (isinstance(auth, dict) and auth.get("password") is None):
            current = doc.get("auth") if isinstance(doc.get("auth"), dict) else {}
            doc = dict(doc, auth=dict(current, password=self.password))
        return doc

    def set_state(self, doc: Any) -> dict:
        """Replace the state. Unless the document sets a password, the current one is kept."""
        if isinstance(doc, dict):
            doc = self._keep_password(doc, doc.get("auth"))
        self.state = statemod.normalize(doc)
        return self.get_state()

    def patch_state(self, patch: Any) -> dict:
        """Merge patch the state. Unless the patch sets a password, the current one is kept."""
        if not isinstance(patch, dict):
            raise statemod.ScenarioError(["/: a patch must be a JSON object"])
        merged = statemod.merge_patch(self.state, patch)
        self.state = statemod.normalize(self._keep_password(merged, patch.get("auth")))
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
        """Switch to another printer: connections drop and files are cleared; faults and the log stay."""
        base = self._with_password(statemod.load(name))
        self._drop_connections()
        self.base_state, self.scenario_ref = base, name
        self.state = copy.deepcopy(base)
        self.storage.clear()
        return self.get_state()

    def reset(self) -> dict:
        """Reload the scenario and clear faults, the log, files and the clock (a reboot).

        Like a reboot it drops every printer connection, so a request still in
        flight can't answer, log or store a file after the reset.
        """
        self._drop_connections()
        self.state = copy.deepcopy(self.base_state)
        self.storage.clear()
        self.faults.clear()
        self.log.clear()
        self.reboot()
        return self.get_state()

    def _drop_connections(self) -> None:
        self.generation += 1
        self.upload_path = self.upload_owner = None
        try:
            current = asyncio.current_task()
        except RuntimeError:  # no running loop, so no connections either
            current = None
        for task in list(self._printer_tasks):
            if task is not current:
                task.cancel()

    def reboot(self) -> None:
        """New nonce random half and uptime 0, so every old nonce is stale."""
        self.clock.reboot()
        self.nonces.reboot()

    def advance_clock(self, seconds: float) -> None:
        """Move the uptime and wall clock on; ValueError leaves the clock as it was."""
        self.clock.advance(seconds)

    # --- printer port ------------------------------------------------------------

    async def _handle_printer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        self._printer_tasks.add(task)
        conn_id = self._next_conn
        self._next_conn += 1
        inp = Input(reader)
        try:
            while True:
                req = await self._read_request(inp, writer, conn_id)
                if req is None or not await self._serve(req, inp, writer, conn_id):
                    break
        except (ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception:  # a bug in the mock: leave a trace rather than drop the client silently
            print("prusalink-mock: unexpected error on a printer connection", file=sys.stderr)
            traceback.print_exc()
        finally:
            self._tasks.discard(task)
            self._printer_tasks.discard(task)
            try:
                writer.close()
            except Exception:
                pass

    async def _read_request(self, inp: Input, writer: asyncio.StreamWriter, conn_id: int) -> Optional[Request]:
        """Read one request head; None closes the connection.

        A connection with no request under way is idle: after IDLE_TIMEOUT, or
        when the client closes, it is closed without a word. From the first
        byte of a request it is active (server.cpp:536-560): a stall of
        ACTIVE_TIMEOUT gets the 408 goodbye (server.cpp:481-521), and a client
        close gets 400 (req_parser.cpp:242-246).
        """
        parser = wire.HeadParser()
        while True:
            if inp.buf:
                del inp.buf[:parser.feed(bytes(inp.buf))]
                if parser.done:
                    return parser.request()
            started = parser.consumed > 0
            try:
                more = await inp.fill(wire.ACTIVE_TIMEOUT_S if started else wire.IDLE_TIMEOUT_S)
            except asyncio.TimeoutError:
                if started:
                    req = parser.request()
                    entry = self._entry(req, conn_id)
                    entry.update(route="invalid", status=408, close=True, error="request head timed out")
                    self._log(entry, req, None, time.monotonic())
                    writer.write(wire.GOODBYE)
                    await writer.drain()
                return None
            if not more:
                if not started:
                    return None
                parser.eof()
                return parser.request()

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

    def _log(self, entry: dict, req: Request, auth: Optional[authmod.AuthResult], started: float) -> None:
        entry["headers"] = [[authmod.scrub(n, self.password), authmod.redact_header(n, v, auth, self.password)]
                            for n, v in req.headers]
        # The password can also come in the target, a file name or text the client sent
        for key, value in entry.items():
            if key != "headers":
                entry[key] = authmod.scrub_value(value, self.password)
        entry["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        self.log.add(entry)

    async def _serve(self, req: Request, inp: Input, writer: asyncio.StreamWriter, conn_id: int) -> bool:
        """Answer one request; returns whether the connection stays open.

        The log entry is added before the response goes out, so a client that
        has the response finds the entry; lingering and hanging come after.
        """
        started = time.monotonic()
        generation = self.generation
        entry = self._entry(req, conn_id)
        auth: Optional[authmod.AuthResult] = None
        logged = False

        def commit() -> None:
            nonlocal logged
            if not logged and generation == self.generation:  # nothing from before a reset
                self._log(entry, req, auth, started)
            logged = True

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
                    entry["auth"] = auth.log_dict(self.password)
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
                    commit()
                    await self._hang(inp, float(action.get("seconds", 30.0)))
                    return False
                elif kind == "drop" and action["when"] == "before_response":
                    entry["close"] = True
                    commit()
                    await self._linger(inp, writer)
                    return False
                elif kind == "status":
                    resp = self._fault_status(req, action)
                if resp is None:
                    resp = await self._route(req, auth, inp, entry)
                if kind == "malformed":
                    resp = self._malformed(req, resp, rule)
                if kind == "drop":  # mid_body
                    data = resp.wire()
                    cut = len(resp.head) + action.get("after_bytes", (len(data) - len(resp.head)) // 2)
                    entry.update(status=resp.status, close=True)
                    commit()
                    writer.write(data[:cut])
                    await self._linger(inp, writer)
                    return False
            entry["status"] = resp.status
            entry["close"] = resp.after != KEEP
            commit()
            writer.write(resp.wire())
            await writer.drain()
            if resp.after == KEEP:
                return True
            if resp.after == ERROR_CLOSE:
                await self._linger(inp, writer)
            return False
        except (ConnectionError, OSError) as exc:
            if not logged:  # the client reset the connection mid-request
                entry["close"] = True
                entry.setdefault("error", "connection reset by the client" if isinstance(exc, ConnectionResetError)
                                 else f"connection error: {exc}")
            raise
        finally:
            commit()

    async def _route(self, req: Request, auth: Optional[authmod.AuthResult], inp: Input, entry: dict) -> Response:
        try:
            return await routes.route(self, req, auth, inp, entry)
        except ConnectionError:  # the client went away; _serve logs it
            raise
        except Exception as exc:  # a bug in the mock: answer 500 rather than close silently
            print("prusalink-mock: unexpected error while answering a request", file=sys.stderr)
            traceback.print_exc()
            entry["error"] = f"prusalink-mock bug: {type(exc).__name__}: {exc}"
            return wire.status_page(500, ERROR_CLOSE, req.accepts_json, "prusalink-mock bug, see its output")

    async def _linger(self, inp: Input, writer: asyncio.StreamWriter) -> None:
        """Half-close, then read and drop input until the client closes (handler.cpp:15-24).

        Closing with unread input would make the kernel send a reset, and the
        client would report a connection error instead of the HTTP status.
        """
        loop = asyncio.get_running_loop()
        inp.buf.clear()
        try:
            await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
            deadline = loop.time() + LINGER_MAX_S
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    break
                data = await asyncio.wait_for(inp.reader.read(65536), min(wire.ACTIVE_TIMEOUT_S, left))
                if not data:
                    break
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass

    async def _hang(self, inp: Input, seconds: float) -> None:
        async def until_eof() -> None:
            while await inp.reader.read(65536):
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

    def _malformed(self, req: Request, resp: Response, rule: FaultRule) -> Response:
        action = rule.action
        mode = action["mode"]
        keep = resp.after == KEEP
        if mode == "html":
            out = wire.static_response(HTML_PORTAL, "text/html; charset=utf-8", keep)
        elif mode == "truncate":
            n = action.get("keep_bytes", len(resp.body) // 2)
            out = wire.json_response(resp.body[:n], keep)
        else:  # wrong_shape
            if rule.body_json is not None:
                body = rule.body_json
            elif resp.is_json:
                body = b"[" + resp.body + b"]"
            else:
                body = b"[]"
            out = wire.json_response(body, keep)
        if resp.after == ERROR_CLOSE:
            out.after = ERROR_CLOSE
        return out
