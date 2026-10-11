"""Command line: ``plmock serve``, ``plmock run -- <cmd>`` and ``plmock scenarios``."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
from typing import Optional, Sequence

from . import __version__
from .api import MockPrinter
from .core import MockCore
from .state import DEFAULT_SCENARIO, ScenarioError, list_scenarios


def _read_password(args: argparse.Namespace) -> Optional[str]:
    if args.password_file is not None:
        with open(args.password_file, encoding="utf-8") as fh:
            return fh.read().rstrip("\r\n")
    return args.password


def _password_note(args: argparse.Namespace, password: str) -> str:
    if not password:
        return "login disabled (empty password)"
    if args.password_file is not None:
        return "from --password-file"
    if args.password is not None:
        return "from --password"
    return "the scenario's test key (see README)"


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--scenario", default=DEFAULT_SCENARIO,
                   help=f"shipped scenario name or path to a scenario JSON file (default: {DEFAULT_SCENARIO})")
    secret = p.add_mutually_exclusive_group()
    secret.add_argument("--password", help="API key and Digest password (overrides the scenario's)")
    secret.add_argument("--password-file", help="read the API key and Digest password from this file")
    p.add_argument("--log-file", help="append the request log to this file as JSON lines")
    p.add_argument("--seed", type=int, help="seed for the nonce random half (deterministic nonces)")
    p.add_argument("--frozen-clock", action="store_true",
                   help="uptime moves only through POST /_mock/clock/advance")


async def _serve(core: MockCore, args: argparse.Namespace) -> None:
    await core.start()
    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, core.request_stop)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: Ctrl-C arrives as KeyboardInterrupt
    print(f"PrusaLink mock, scenario {core.state['name']}: {core.url}", flush=True)
    print(f"User: maker. Password and API key: {_password_note(args, core.password)}", flush=True)
    print(f"Control API: {core.control_url}", flush=True)
    try:
        await core.wait_stop_requested()
    finally:
        await core.stop()


def cmd_serve(args: argparse.Namespace) -> int:
    core = MockCore(args.scenario, host=args.host, port=args.port, control_port=args.control_port,
                    password=_read_password(args), seed=args.seed, log_file=args.log_file,
                    frozen_clock=args.frozen_clock)
    try:
        asyncio.run(_serve(core, args))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("plmock run: give a command after --", file=sys.stderr)
        return 2
    printer = MockPrinter(args.scenario, password=_read_password(args), seed=args.seed,
                          log_file=args.log_file, frozen_clock=args.frozen_clock)
    printer.start()
    proc: Optional[subprocess.Popen] = None
    previous = None
    try:
        env = dict(os.environ)
        env.update(PRUSALINK_MOCK_URL=printer.url, PRUSALINK_MOCK_CONTROL=printer.control_url,
                   PRUSALINK_MOCK_KEY=printer.password)
        try:
            proc = subprocess.Popen(cmd, env=env)
        except OSError as exc:
            print(f"plmock run: cannot start {cmd[0]}: {exc.strerror or exc}", file=sys.stderr)
            return 127
        if hasattr(signal, "SIGTERM"):
            def forward(signum, frame):  # pragma: no cover - exercised by signals only
                proc.terminate()
            try:
                previous = signal.signal(signal.SIGTERM, forward)
            except ValueError:
                previous = None  # not the main thread
        while True:
            try:
                rc = proc.wait()
                break
            except KeyboardInterrupt:
                # The child got the same Ctrl-C; give it a moment to finish.
                try:
                    rc = proc.wait(timeout=10)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    proc.kill()
                    rc = proc.wait()
                break
        return 128 - rc if rc < 0 else rc
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        printer.stop()


def cmd_scenarios(args: argparse.Namespace) -> int:
    for s in list_scenarios():
        print(f"{s['name']:<16} {s['description']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plmock", description="Mock PrusaLink printer for slicer tests.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the mock in the foreground")
    _add_common(serve)
    serve.add_argument("--host", default="127.0.0.1", help="printer port address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8088, help="printer port (default: 8088, 0 = any free port)")
    serve.add_argument("--control-port", type=int, default=8089,
                       help="control API port on 127.0.0.1 (default: 8089, 0 = any free port)")
    serve.set_defaults(func=cmd_serve)

    run = sub.add_parser("run", help="run a command against a mock on free ports, then stop the mock",
                         usage="plmock run [options] -- <command> [args...]")
    _add_common(run)
    run.add_argument("cmd", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    run.set_defaults(func=cmd_run)

    scenarios = sub.add_parser("scenarios", help="list the shipped scenarios")
    scenarios.set_defaults(func=cmd_scenarios)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ScenarioError as exc:
        print("plmock: invalid scenario:", file=sys.stderr)
        for err in exc.errors:
            print(f"  {err}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"plmock: {exc}", file=sys.stderr)
        return 1
