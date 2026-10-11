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
from .state import DEFAULT_SCENARIO, ScenarioError, list_scenarios, password_errors


class _UsageError(Exception):
    """A bad option value; main() prints it and exits with 2."""


def _read_password(args: argparse.Namespace) -> Optional[str]:
    """The password from --password-file or --password, checked like a scenario's.

    The file may start with a UTF-8 byte order mark (PowerShell 5.1 writes one)
    and end with a line break. An empty file is refused: it would turn login off
    without a word, which is what --scenario login-disabled is for.
    """
    if args.password_file is not None:
        try:
            with open(args.password_file, encoding="utf-8-sig") as fh:
                password = fh.read().rstrip("\r\n")
        except UnicodeDecodeError:
            raise _UsageError(f"--password-file {args.password_file}: not UTF-8 text") from None
        if not password:
            raise _UsageError(f"--password-file {args.password_file} is empty "
                              "(for login disabled use --scenario login-disabled or --password '')")
        source = "--password-file"
    else:
        password, source = args.password, "--password"
    errors = password_errors(password) if password is not None else []
    if errors:
        raise _UsageError(f"{source}: {errors[0].split(': ', 1)[1]}")
    return password


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"must be a port number 0..65535, not {text!r}")
    return value


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


# Forwarded to the command as they arrive. Only signals that ask a process to
# end: nothing that makes a core dump or a crash report.
FORWARDED_SIGNALS = ("SIGTERM", "SIGHUP")
INTERRUPT_GRACE_S = 1.0
INTERRUPT_WAIT_S = 10.0
TERMINATE_WAIT_S = 5.0


def _wait_interrupted(proc: subprocess.Popen) -> int:
    """Wait for the command after plmock got SIGINT; returns its exit status.

    A Ctrl-C in a terminal reaches the whole process group, the command
    included, so it first gets a moment to end on its own. A SIGINT sent to
    plmock alone is then passed on, so the command ends either way. A command
    that still runs gets SIGTERM, and at last SIGKILL. Another Ctrl-C moves
    on to the next step.
    """
    steps = [(None, INTERRUPT_GRACE_S), (signal.SIGTERM, TERMINATE_WAIT_S)]
    if os.name == "posix":  # elsewhere Ctrl-C reaches every process on the console
        steps.insert(1, (signal.SIGINT, INTERRUPT_WAIT_S))
    for sig, timeout in steps:
        if sig is not None and proc.poll() is None:
            proc.send_signal(sig)
        try:
            return proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            continue
    proc.kill()
    return proc.wait()


def _end(proc: subprocess.Popen) -> None:
    """Stop the command if it still runs: SIGTERM, then SIGKILL if it doesn't end."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=TERMINATE_WAIT_S)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        proc.kill()
        proc.wait()


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
    previous: dict = {}
    try:
        env = dict(os.environ)
        env.update(PRUSALINK_MOCK_URL=printer.url, PRUSALINK_MOCK_CONTROL=printer.control_url,
                   PRUSALINK_MOCK_KEY=printer.password)
        try:
            proc = subprocess.Popen(cmd, env=env)
        except OSError as exc:
            print(f"plmock run: cannot start {cmd[0]}: {exc.strerror or exc}", file=sys.stderr)
            return 127
        # A signal meant to end plmock ends the command too, so it can't outlive
        # the mock. SIGINT arrives as KeyboardInterrupt, below.
        def forward(signum, frame):  # pragma: no cover - exercised by signals only
            proc.send_signal(signum)
        for name in FORWARDED_SIGNALS:
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                previous[sig] = signal.signal(sig, forward)
            except ValueError:
                break  # not the main thread
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            rc = _wait_interrupted(proc)
        return 128 - rc if rc < 0 else rc
    finally:
        if proc is not None:
            _end(proc)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
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
    serve.add_argument("--port", type=_port, default=8088, help="printer port (default: 8088, 0 = any free port)")
    serve.add_argument("--control-port", type=_port, default=8089,
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
    except _UsageError as exc:
        print(f"plmock: {exc}", file=sys.stderr)
        return 2
    except ScenarioError as exc:
        print("plmock: invalid scenario:", file=sys.stderr)
        for err in exc.errors:
            print(f"  {err}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"plmock: {exc}", file=sys.stderr)
        return 1
