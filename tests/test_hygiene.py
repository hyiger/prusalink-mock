"""Repository and test-suite guarantees that aren't about the firmware."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FIXTURES, GOLDEN, TESTS
from prusalink_mock import MockPrinter

ROOT = TESTS.parent
PASSWORD = "Zq7SecretPw42"


def test_contract_failures_never_print_the_key(tmp_path):
    """Run the contract suite against a mock that fails it, and look for the key in the output."""
    pw_file = tmp_path / "pw"
    pw_file.write_text(PASSWORD + "\n")
    with MockPrinter(password=PASSWORD) as printer:
        printer.add_fault({"match": {"path": "/api/version", "auth": "ok"},
                           "action": {"type": "status", "status": 500}})
        printer.add_fault({"match": {"path": "/api/v1/info"}, "action": {"type": "drop", "when": "before_response"}})
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=long", "-m", "contract",
             f"--target={printer.url}", f"--password-file={pw_file}", str(TESTS / "test_contract.py")],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
    output = run.stdout + run.stderr
    assert run.returncode == 1 and "failed" in output
    for leak in (PASSWORD[:-1], PASSWORD.swapcase(), PASSWORD.lower()):
        assert leak not in output
    assert "<secret>" in output


def test_contract_asserts_hold_no_credentials():
    """pytest explains every call inside an assert, so credentials must be built outside them."""
    tree = ast.parse((TESTS / "test_contract.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            for call in ast.walk(node.test):
                if isinstance(call, ast.Call):
                    name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                    assert name not in ("key", "digest", "reveal", "DigestClient"), \
                        f"test_contract.py:{node.lineno} builds credentials inside an assert"


def test_captures_keep_their_line_endings():
    """A Windows checkout (core.autocrlf) would otherwise turn the LF bodies into CRLF."""
    git = shutil.which("git")
    if git is None or subprocess.run([git, "rev-parse"], cwd=ROOT, capture_output=True).returncode != 0:
        pytest.skip("not a git checkout")
    files = sorted(GOLDEN.glob("*.body")) + sorted(FIXTURES.glob("*.json"))
    out = subprocess.run([git, "check-attr", "text", "--", *map(str, files)], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    assert out.count(": text: unset") == len(files), out
