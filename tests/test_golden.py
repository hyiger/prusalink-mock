"""The mini404-3tool scenario against real responses captured from the Mini404 emulator.

tests/golden/mini404-v6.9.2-hy.3 holds what curl saw (every hop of each Digest
exchange) from firmware v6.9.2-hy.3. The mock must send the same status lines,
headers and bodies; only nonce values may differ.
"""

from __future__ import annotations

import pytest

from conftest import GOLDEN
from httpraw import Conn, digest_request, nonce_masked
from prusalink_mock import MockPrinter
from prusalink_mock.auth import DigestClient


@pytest.fixture(scope="module")
def mini404():
    with MockPrinter("mini404-3tool", seed=3) as m:
        yield m


def golden_blocks(name: str) -> list[list[str]]:
    text = (GOLDEN / f"{name}.headers").read_bytes().decode("latin-1")
    return [nonce_masked(block.split("\r\n")) for block in text.split("\r\n\r\n") if block]


def golden_body(name: str) -> bytes:
    return (GOLDEN / f"{name}.body").read_bytes()


def assert_matches(name: str, responses, body: bool = True) -> None:
    assert [nonce_masked(r.header_lines()) for r in responses] == golden_blocks(name)
    if body:
        assert responses[-1].body == golden_body(name)


@pytest.mark.parametrize("name,target", [
    ("version", "/api/version"),
    ("v1-info", "/api/v1/info"),
    ("v1-storage", "/api/v1/storage"),
    ("v1-status", "/api/v1/status"),
    ("v1-tools", "/api/v1/tools"),
    ("v1-job", "/api/v1/job"),
    ("notfound-v1", "/api/v1/nope"),
    ("files-missing", "/api/v1/files/usb/does-not-exist.gcode"),
    ("files-no-storage", "/api/v1/files"),
])
def test_digest_get(mini404, name, target):
    with Conn("127.0.0.1", mini404.port) as conn:
        first, second = digest_request(conn, "GET", target, mini404.password)
    assert_matches(name, [first, second])


def test_digest_get_json_errors(mini404):
    with Conn("127.0.0.1", mini404.port) as conn:
        first, second = digest_request(conn, "GET", "/api/v1/nope", mini404.password,
                                       headers=[("Accept", "application/json")])
    assert_matches("notfound-v1-json", [first, second])


def test_http10_closes_and_sends_no_length(mini404):
    with Conn("127.0.0.1", mini404.port) as conn:
        first = conn.request("GET", "/api/v1/tools", version="HTTP/1.0")
        assert first.closed
        nonce = first.nonce
    with Conn("127.0.0.1", mini404.port) as conn:
        auth = DigestClient(mini404.password).header("GET", "/api/v1/tools", nonce)
        second = conn.request("GET", "/api/v1/tools", [("Authorization", auth)], version="HTTP/1.0")
        assert second.closed and not second.chunked
    assert_matches("v1-tools-http10", [first, second])


@pytest.mark.parametrize("name,headers", [
    ("unauth", []),
    ("unauth-json", [("Accept", "application/json")]),
    ("apikey-wrong", [("X-Api-Key", "wrong-key")]),
])
def test_unauthorized(mini404, name, headers):
    with Conn("127.0.0.1", mini404.port) as conn:
        assert_matches(name, [conn.request("GET", "/api/v1/tools", headers)])


def test_api_key(mini404):
    with Conn("127.0.0.1", mini404.port) as conn:
        assert_matches("apikey", [conn.request("GET", "/api/v1/tools", [("X-Api-Key", mini404.password)])])


@pytest.mark.parametrize("name,username,password", [
    ("wrong-password", "maker", "wrong-password"),
    ("wrong-user", "admin", None),
])
def test_wrong_credentials(mini404, name, username, password):
    with Conn("127.0.0.1", mini404.port) as conn:
        first = conn.request("GET", "/api/v1/tools")
        auth = DigestClient(password or mini404.password, username).header("GET", "/api/v1/tools", first.nonce)
        second = conn.request("GET", "/api/v1/tools", [("Authorization", auth)])
    assert_matches(name, [first, second])


def test_not_found_outside_api_needs_no_auth(mini404):
    with Conn("127.0.0.1", mini404.port) as conn:
        assert_matches("notfound-root", [conn.request("GET", "/nope")])


def test_head_on_get_only_route(mini404):
    # The firmware sends status page bodies even for HEAD, so curl drops the
    # connection after the 401 and asks again on a new one.
    with Conn("127.0.0.1", mini404.port) as conn:
        first = conn.request("HEAD", "/api/v1/tools")
        assert first.body == b"401: Unauthorized\n\n\n"
    with Conn("127.0.0.1", mini404.port) as conn:
        auth = DigestClient(mini404.password).header("HEAD", "/api/v1/tools", first.nonce)
        second = conn.request("HEAD", "/api/v1/tools", [("Authorization", auth)])
        assert second.closed and second.body == b"405: Method Not Allowed\n\n\n"
    assert_matches("head-tools", [first, second], body=False)
