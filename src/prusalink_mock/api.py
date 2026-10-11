"""In-process API: run the mock on a background thread.

    from prusalink_mock import MockPrinter

    with MockPrinter("indx-gaps") as printer:
        printer.url, printer.password
        printer.patch_state({"tools": {...}})
        printer.add_fault({"match": {"path": "/api/v1/tools"}, "times": 1,
                           "action": {"type": "status", "status": 500}})
        printer.requests()
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Optional

from .core import MockCore
from .state import DEFAULT_SCENARIO

CALL_TIMEOUT_S = 30.0


class MockPrinter:
    """A mock PrusaLink printer on ephemeral 127.0.0.1 ports (by default)."""

    def __init__(self, scenario: str = DEFAULT_SCENARIO, *, host: str = "127.0.0.1", port: int = 0,
                 control_port: int = 0, password: Optional[str] = None, seed: Optional[int] = None,
                 log_file: Optional[str] = None, epoch: Optional[float] = None, frozen_clock: bool = False):
        self.core = MockCore(scenario, host=host, port=port, control_port=control_port, password=password,
                             seed=seed, log_file=log_file, epoch=epoch, frozen_clock=frozen_clock)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None

    # --- lifecycle -------------------------------------------------------------

    def start(self) -> "MockPrinter":
        ready = threading.Event()

        def run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            try:
                loop.run_until_complete(self.core.start())
            except BaseException as exc:  # surfaced to the caller of start()
                self._error = exc
                ready.set()
                loop.close()
                return
            ready.set()
            try:
                loop.run_until_complete(self.core.wait_stop_requested())
                loop.run_until_complete(self.core.stop())
            finally:
                loop.close()

        self._thread = threading.Thread(target=run, name="prusalink-mock", daemon=True)
        self._thread.start()
        ready.wait()
        if self._error is not None:
            self._thread.join()
            raise self._error
        return self

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self.core.request_stop)
            except RuntimeError:
                pass
        self._thread.join(timeout=30)
        self._thread = None

    def __enter__(self) -> "MockPrinter":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def stopped(self) -> bool:
        return self._thread is None or not self._thread.is_alive()

    # --- helpers -----------------------------------------------------------------

    def _call(self, fn: Callable, *args: Any) -> Any:
        if self._loop is None or self.stopped:
            raise RuntimeError("the mock is not running")

        async def call() -> Any:
            return fn(*args)

        return asyncio.run_coroutine_threadsafe(call(), self._loop).result(CALL_TIMEOUT_S)

    @property
    def url(self) -> str:
        return self.core.url

    @property
    def control_url(self) -> str:
        return self.core.control_url

    @property
    def host(self) -> str:
        return "127.0.0.1" if self.core.host in ("", "0.0.0.0", "localhost") else self.core.host

    @property
    def port(self) -> int:
        return self.core.port

    @property
    def password(self) -> str:
        return self._call(lambda: self.core.password)

    def state(self) -> dict:
        return self._call(self.core.get_state)

    def set_state(self, doc: dict) -> dict:
        return self._call(self.core.set_state, doc)

    def patch_state(self, patch: dict) -> dict:
        return self._call(self.core.patch_state, patch)

    def set_tool(self, key: int | str, tool: dict) -> dict:
        return self._call(self.core.set_tool, str(key), tool)

    def remove_tool(self, key: int | str) -> dict:
        return self._call(self.core.remove_tool, str(key))

    def load_scenario(self, name: str) -> dict:
        return self._call(self.core.load_scenario, name)

    def reset(self) -> dict:
        return self._call(self.core.reset)

    def add_fault(self, rule: dict) -> int:
        return self._call(lambda: self.core.faults.add(rule).id)

    def faults(self) -> list[dict]:
        return self._call(lambda: [r.describe() for r in self.core.faults.rules])

    def clear_faults(self) -> None:
        self._call(self.core.faults.clear)

    def requests(self, since: int = 0) -> list[dict]:
        return self._call(self.core.log.since, since)

    def clear_requests(self) -> None:
        self._call(self.core.log.clear)

    def files(self) -> list[dict]:
        return self._call(self.core.storage.listing)

    def file_bytes(self, rel_path: str) -> bytes:
        f = self._call(self.core.storage.find_rel, rel_path)
        if f is None:
            raise FileNotFoundError(rel_path)
        return f.data

    def advance_clock(self, seconds: float) -> None:
        self._call(self.core.advance_clock, seconds)

    def reboot(self) -> None:
        self._call(self.core.reboot)
