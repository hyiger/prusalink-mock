from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import pytest

from prusalink_mock import MockPrinter

TESTS = Path(__file__).parent
FIXTURES = TESTS / "fixtures" / "firmware"
GOLDEN = TESTS / "golden" / "mini404-v6.9.2-hy.3"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("prusalink-mock contract")
    group.addoption("--target", default=None,
                    help="run the contract tests against this base URL (Mini404 or a printer) instead of the mock")
    group.addoption("--password-file", default=None, help="file holding the target's password / API key")
    group.addoption("--allow-writes", action="store_true",
                    help="contract tests may upload and delete a small file on the target")


class Secret:
    """Keeps the password out of assertion messages and tracebacks.

    httpraw.Conn takes it as a header value and reveals it only when writing
    the request.
    """

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<secret>"

    __str__ = __repr__


@dataclass
class Target:
    host: str
    port: int
    password: Secret
    is_mock: bool
    allow_writes: bool
    mock: Optional[MockPrinter] = None


@pytest.fixture
def printer():
    """A fresh mock printer (default scenario, deterministic nonces)."""
    with MockPrinter(seed=1) as m:
        yield m


@pytest.fixture(scope="session")
def contract_target(request: pytest.FixtureRequest):
    url = request.config.getoption("--target")
    allow_writes = bool(request.config.getoption("--allow-writes"))
    if url is None:
        with MockPrinter() as m:
            yield Target("127.0.0.1", m.port, Secret(m.password), True, True, m)
        return
    parts = urlsplit(url if "://" in url else f"http://{url}")
    pw_file = request.config.getoption("--password-file")
    if not pw_file:
        pytest.exit("--target needs --password-file", returncode=4)
    with open(pw_file, encoding="utf-8-sig") as fh:
        password = Secret(fh.read().rstrip("\r\n"))
    yield Target(parts.hostname or "127.0.0.1", parts.port or 80, password, False, allow_writes)


@pytest.fixture(scope="session")
def curl() -> str:
    path = shutil.which("curl")
    if path is None:
        pytest.skip("curl is not installed")
    return path
