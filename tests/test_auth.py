"""Authentication, checked against the firmware's own unit tests.

tests/unit/lib/WUI/nhttp/server_tests.cpp:323-628 in Prusa-Firmware-Buddy.
"""

from __future__ import annotations

import pytest

from prusalink_mock import auth
from prusalink_mock.auth import Clock, NonceSource, check, digest_response


def digest_headers(username: str, nonce: str, uri: str, response: str) -> list[tuple[str, str]]:
    """digest_auth_header() in server_tests.cpp."""
    return [("Authorization", f'Digest username="{username}", realm="Printer API", nonce="{nonce}", '
                              f'uri="{uri}", response="{response}"')]


class FixedNonces(NonceSource):
    """Nonce random half 0xaaaaaaaa and a settable uptime, as in the firmware tests."""

    def __init__(self, uptime: int = 0):
        clock = Clock(frozen=True)
        clock.advance(uptime)
        super().__init__(clock, seed=0)
        self.random = 0xAAAAAAAA


def test_firmware_vectors():
    assert digest_response("password", "GET", "/secret.html", "aaaaaaaa00000000") == "0cbf0ac5ca4c879c35a3b91430214a47"
    assert digest_response("password", "GET", "/secret.html", "dcd98b7102dd2f0e") == "285b9c363d25d31c5867bca2bd4121af"


def test_authenticated_digest():
    r = check(digest_headers("maker", "aaaaaaaa00000000", "/secret.html", "0cbf0ac5ca4c879c35a3b91430214a47"),
              "GET", "/secret.html", "password", FixedNonces())
    assert r.ok and r.scheme == "digest"


@pytest.mark.parametrize("headers", [
    [],
    digest_headers("maker", "not a valid nonce", "/secret.html", "1dd8be56e6996b274258d7412e671e5f"),
    digest_headers("invaliduser", "aaaaaaaa00000000aaaaaaa", "/secret.html", "1dd8be56e6996b274258d7412e671e5f"),
    digest_headers("invaliduser", "aaaaaaaa00000000", "/secret.html", "1dd8be56e6996b274258d7412e671e5f"),
], ids=["none", "invalid-nonce", "nonce-too-long", "wrong-user"])
def test_not_authenticated_digest(headers):
    r = check(headers, "GET", "/secret.html", "password", FixedNonces())
    assert not r.ok
    assert r.challenge == 'WWW-Authenticate: Digest realm="Printer API", nonce="aaaaaaaa00000000", stale=false\r\n'


@pytest.mark.parametrize("nonce,response", [
    ("aaaaaaaa00000000", "0cbf0ac5ca4c879c35a3b91430214a47"),  # expired: uptime is 0xfff
    ("dcd98b7102dd2f0e", "285b9c363d25d31c5867bca2bd4121af"),  # previous boot, right password
])
def test_stale_nonce(nonce, response):
    r = check(digest_headers("maker", nonce, "/secret.html", response), "GET", "/secret.html", "password",
              FixedNonces(uptime=0xFFF))
    assert not r.ok and r.stale
    assert r.challenge.endswith('nonce="aaaaaaaa00000fff", stale=true\r\n')


def test_stale_nonce_and_wrong_auth():
    r = check(digest_headers("invaliduser", "aaaaaaaa00000000", "/secret.html", "1dd8be56e6996b274258d7412e671e5f"),
              "GET", "/secret.html", "password", FixedNonces(uptime=0xFFF))
    assert not r.ok and not r.stale


def test_nonce_lifetime_is_300_seconds():
    nonces = FixedNonces(uptime=1000)
    nonce = f"{nonces.new():016x}"
    resp = digest_response("pw", "GET", "/api/version", nonce)
    headers = digest_headers("maker", nonce, "/api/version", resp)
    nonces.clock.advance(299)
    assert check(headers, "GET", "/api/version", "pw", nonces).ok
    nonces.clock.advance(1)
    r = check(headers, "GET", "/api/version", "pw", nonces)
    assert not r.ok and r.stale


def test_ha2_uses_the_raw_target_not_the_uri_parameter():
    nonces = FixedNonces()
    raw = "/api/v1/files/usb//a%20b.gcode"
    good = digest_response("pw", "PUT", raw, "aaaaaaaa00000000")
    assert check(digest_headers("maker", "aaaaaaaa00000000", "/whatever", good), "PUT", raw, "pw", nonces).ok
    collapsed = digest_response("pw", "PUT", "/api/v1/files/usb/a%20b.gcode", "aaaaaaaa00000000")
    assert not check(digest_headers("maker", "aaaaaaaa00000000", raw, collapsed), "PUT", raw, "pw", nonces).ok


@pytest.mark.parametrize("value", ["SECRET"])
def test_api_key(value):
    r = check([("X-Api-Key", value)], "GET", "/secret.html", "SECRET", FixedNonces())
    assert r.ok and r.scheme == "apikey"


@pytest.mark.parametrize("value", ["", "Password!", "Secret", "SECRET.", "SECRE"],
                         ids=["empty", "wrong", "case", "extra", "missing"])
def test_api_key_rejected(value):
    r = check([("X-Api-Key", value)], "GET", "/secret.html", "SECRET", FixedNonces())
    assert not r.ok
    assert r.challenge == 'WWW-Authenticate: ApiKey realm="Printer API"\r\n'


@pytest.mark.parametrize("headers", [[("X-Api-Key", "")], [("X-Api-Key", "Password!")], [], []])
def test_no_password_configured_locks_everything(headers):
    r = check(headers, "GET", "/secret.html", "", FixedNonces())
    assert not r.ok


def test_empty_password_rejects_even_a_matching_digest():
    resp = digest_response("", "GET", "/api/version", "aaaaaaaa00000000")
    assert not check(digest_headers("maker", "aaaaaaaa00000000", "/api/version", resp), "GET", "/api/version", "",
                     FixedNonces()).ok


def test_last_auth_header_wins():
    nonces = FixedNonces()
    good = digest_headers("maker", "aaaaaaaa00000000", "/x", digest_response("pw", "GET", "/x", "aaaaaaaa00000000"))
    bad = digest_headers("maker", "aaaaaaaa00000000", "/x", "0" * 32)
    assert check([("X-Api-Key", "pw")] + bad, "GET", "/x", "pw", nonces).scheme == "digest"
    assert not check([("X-Api-Key", "pw")] + bad, "GET", "/x", "pw", nonces).ok
    assert check(bad + [("X-Api-Key", "pw")], "GET", "/x", "pw", nonces).ok
    assert check([("X-Api-Key", "nope")] + good, "GET", "/x", "pw", nonces).ok
    assert not check(good + [("X-Api-Key", "nope")], "GET", "/x", "pw", nonces).ok


def test_basic_auth_gets_the_digest_challenge():
    r = check([("Authorization", "Basic bWFrZXI6cHc=")], "GET", "/x", "pw", FixedNonces())
    assert not r.ok and r.scheme == "digest" and r.challenge.startswith("WWW-Authenticate: Digest ")


def test_basic_after_api_key_keeps_the_api_key_mode():
    """Basic carries no nonce= or response=, so it doesn't switch the mode."""
    r = check([("X-Api-Key", "pw"), ("Authorization", "Basic bWFrZXI6cHc=")], "GET", "/x", "pw", FixedNonces())
    assert r.ok and r.scheme == "apikey"


def test_cnonce_and_other_parameters_are_ignored():
    nonce = "aaaaaaaa00000000"
    resp = digest_response("pw", "GET", "/x", nonce)
    value = (f'Digest username="maker", realm="Printer API", nonce={nonce}, uri="/x", cnonce="ffffffffffffffff", '
             f'nc=00000001, qop=auth, algorithm=MD5, opaque="abc", response={resp}')
    assert check([("Authorization", value)], "GET", "/x", "pw", FixedNonces()).ok


def test_api_key_split_across_two_headers_matches():
    """A firmware quirk: a second X-Api-Key continues the first one's match."""
    assert check([("X-Api-Key", "pa"), ("X-Api-Key", "ss")], "GET", "/x", "pass", FixedNonces()).ok
    assert not check([("X-Api-Key", "pass"), ("X-Api-Key", "pass")], "GET", "/x", "pass", FixedNonces()).ok


def test_reboot_makes_old_nonces_stale():
    clock = Clock(frozen=True)
    nonces = NonceSource(clock, seed=7)
    nonce = f"{nonces.new():016x}"
    headers = digest_headers("maker", nonce, "/x", digest_response("pw", "GET", "/x", nonce))
    assert check(headers, "GET", "/x", "pw", nonces).ok
    nonces.reboot()
    r = check(headers, "GET", "/x", "pw", nonces)
    assert not r.ok and r.stale


def test_redaction_never_shows_secrets():
    r = check([("X-Api-Key", "pw")], "GET", "/x", "pw", FixedNonces())
    assert auth.redact_header("X-Api-Key", "pw", r) == "<redacted:match>"
    value = 'Digest username="maker", nonce="aaaaaaaa00000000", uri="/x", response="0123456789abcdef0123456789abcdef"'
    redacted = auth.redact_header("Authorization", value, None)
    assert "0123456789abcdef" not in redacted and 'response="<redacted>"' in redacted
    assert auth.redact_header("Authorization", "Basic bWFrZXI6cHc=", None) == "Basic <redacted>"


def test_reboot_keeps_the_wall_clock():
    clock = Clock(frozen=True, epoch=1_000_000)
    clock.advance(3600.5)
    assert clock.now() == 1_003_600 and clock.uptime() == 3600
    clock.reboot()
    assert clock.uptime() == 0 and clock.now() == 1_003_600
    clock.advance(10)
    assert clock.now() == 1_003_610


def test_digest_client_repr_hides_the_password():
    assert "s3cret" not in repr(auth.DigestClient("s3cret"))
