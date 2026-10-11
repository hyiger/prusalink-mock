"""The plmock command line, run as a separate process from the checkout."""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"


def env() -> dict:
    e = dict(os.environ)
    e["PYTHONPATH"] = str(SRC) + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    for name in ("PRUSALINK_MOCK_URL", "PRUSALINK_MOCK_CONTROL", "PRUSALINK_MOCK_KEY"):
        e.pop(name, None)
    return e


def plmock(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "prusalink_mock", *args], env=env(), capture_output=True,
                          text=True, timeout=timeout)


def port_closed(url: str) -> bool:
    host, port = url.rsplit("/", 1)[-1].rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=2).close()
    except OSError:
        return True
    return False


def test_scenarios_command():
    r = plmock("scenarios")
    assert r.returncode == 0
    for name in ("indx-hy8", "indx-gaps", "indx-no-docks", "indx-stock-fw", "coreone-stock", "mini404-3tool"):
        assert name in r.stdout


def test_version():
    r = plmock("--version")
    assert r.returncode == 0 and "plmock 0.1.0" in r.stdout


CHILD = r"""
import json, os, sys, urllib.request
url, control, key = (os.environ[n] for n in ("PRUSALINK_MOCK_URL", "PRUSALINK_MOCK_CONTROL", "PRUSALINK_MOCK_KEY"))
req = urllib.request.Request(url + "/api/v1/tools", headers={"X-Api-Key": key})
tools = json.load(urllib.request.urlopen(req, timeout=10))["tools"]
health = json.load(urllib.request.urlopen(control + "/_mock/health", timeout=10))
print(json.dumps({"pid": os.getpid(), "url": url, "control": control, "tools": sorted(tools),
                  "scenario": health["scenario"]}))
sys.exit(int(sys.argv[1]))
"""


@pytest.mark.parametrize("code", [0, 3])
def test_run_wrapper(code):
    r = plmock("run", "--scenario", "indx-gaps", "--", sys.executable, "-c", CHILD, str(code))
    assert r.returncode == code, r.stderr
    info = json.loads(r.stdout.strip().splitlines()[-1])
    assert info["tools"] == ["1", "2", "3", "5"] and info["scenario"] == "indx-gaps"
    assert info["url"].startswith("http://127.0.0.1:") and info["control"].startswith("http://127.0.0.1:")
    assert port_closed(info["url"]) and port_closed(info["control"])
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(info["pid"], 0)


HANG_CHILD = r"""
import json, os, socket, urllib.request
control, key = os.environ["PRUSALINK_MOCK_CONTROL"], os.environ["PRUSALINK_MOCK_KEY"]
rule = json.dumps({"match": {"path": "/api/v1/tools"}, "action": {"type": "hang"}}).encode()
urllib.request.urlopen(urllib.request.Request(control + "/_mock/faults", data=rule, method="POST"), timeout=10)
req = urllib.request.Request(os.environ["PRUSALINK_MOCK_URL"] + "/api/v1/tools", headers={"X-Api-Key": key})
try:
    urllib.request.urlopen(req, timeout=1)
except (socket.timeout, TimeoutError, OSError):
    print("timed out")
"""


def test_run_wrapper_stops_with_a_hung_request():
    start = time.monotonic()
    r = plmock("run", "--", sys.executable, "-c", HANG_CHILD)
    assert r.returncode == 0, r.stderr
    assert "timed out" in r.stdout
    assert time.monotonic() - start < 25


def test_run_wrapper_errors():
    r = plmock("run", "--", "plmock-no-such-command")
    assert r.returncode == 127 and "cannot start" in r.stderr
    assert plmock("run").returncode == 2


def test_invalid_scenario_exits_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tools": {"1": {"loaded": True, "material": "TOOLONGNAME", "nozzle_diameter": 0.4,
                                               "hardened": False, "high_flow": True}}}))
    r = plmock("run", "--scenario", str(bad), "--", sys.executable, "-c", "pass")
    assert r.returncode == 2 and "/tools/1/material" in r.stderr


def _reader(proc: subprocess.Popen, lines: "queue.Queue[str]") -> None:
    for line in proc.stdout:
        lines.put(line)


def test_serve(tmp_path):
    secret = "serveKey12345"
    (tmp_path / "pw").write_text(secret + "\n")
    log = tmp_path / "requests.jsonl"
    proc = subprocess.Popen(
        [sys.executable, "-m", "prusalink_mock", "serve", "--scenario", "indx-no-docks", "--port", "0",
         "--control-port", "0", "--password-file", str(tmp_path / "pw"), "--log-file", str(log)],
        env=env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=_reader, args=(proc, lines), daemon=True).start()
    out = []
    try:
        while not any(line.startswith("Control API:") for line in out):
            out.append(lines.get(timeout=20))
        text = "".join(out)
        assert secret not in text and "from --password-file" in text
        url = next(l for l in out if l.startswith("PrusaLink mock")).split(": ", 1)[1].strip()
        control = next(l for l in out if l.startswith("Control API:")).split(": ", 1)[1].strip()
        assert url.startswith("http://127.0.0.1:") and control.startswith("http://127.0.0.1:")

        import urllib.request
        req = urllib.request.Request(url + "/api/v1/tools", headers={"X-Api-Key": secret})
        assert urllib.request.urlopen(req, timeout=10).read() == b'{"tools":{}}'
        urllib.request.urlopen(urllib.request.Request(control + "/_mock/shutdown", data=b"", method="POST"),
                               timeout=10).read()
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert entries and entries[0]["path"] == "/api/v1/tools" and entries[0]["status"] == 200
    assert secret not in log.read_text()
