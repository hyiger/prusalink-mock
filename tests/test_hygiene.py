"""Repository and test-suite guarantees that aren't about the firmware."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from conftest import FIXTURES, GOLDEN, TESTS

ROOT = TESTS.parent


def test_captures_keep_their_line_endings():
    """A Windows checkout (core.autocrlf) would otherwise turn the LF bodies into CRLF."""
    git = shutil.which("git")
    if git is None or subprocess.run([git, "rev-parse"], cwd=ROOT, capture_output=True).returncode != 0:
        pytest.skip("not a git checkout")
    files = sorted(GOLDEN.glob("*.body")) + sorted(FIXTURES.glob("*.json"))
    out = subprocess.run([git, "check-attr", "text", "--", *map(str, files)], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    assert out.count(": text: unset") == len(files), out
