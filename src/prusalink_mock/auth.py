"""PrusaLink authentication as the firmware implements it.

Source: lib/WUI/nhttp/req_parser.cpp (header events at :115-174, Digest check
at :309-429) and the header automaton in utils/gen-automata/http.py.

* One secret serves as the Digest password (user ``maker``, realm
  ``Printer API``) and as the ``X-Api-Key`` value. An empty secret disables
  login: nothing authenticates.
* Digest is RFC 2069 (no qop). HA2 hashes the raw request target. Only the
  ``nonce`` and ``response`` parameters are read; their hex digits are shifted
  into fixed-width integers, so only the last 16 / 32 digits count.
* The auth mode is whichever of X-Api-Key / Digest parameters the parser sees
  last; with neither, it is Digest. Basic auth carries no Digest parameters
  and so gets the Digest challenge.
"""

from __future__ import annotations

import hashlib
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

REALM = "Printer API"
USERNAME = "maker"
NONCE_LIFETIME_S = 300  # src/common/http/types.h:164

_HEX = "0123456789abcdefABCDEF"


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def digest_response(password: str, method: str, target: str, nonce: str, username: str = USERNAME) -> str:
    """RFC 2069 response the firmware expects (req_parser.cpp:392-412)."""
    ha1 = md5_hex(f"{username}:{REALM}:".encode("latin-1") + password.encode("utf-8"))
    ha2 = md5_hex(method.encode("latin-1") + b":" + target.encode("latin-1"))
    return md5_hex(f"{ha1}:{nonce}:{ha2}".encode("latin-1"))


class Clock:
    """Uptime source for nonces and file timestamps.

    Real monotonic time by default. ``frozen`` makes uptime move only through
    :meth:`advance`, so test runs repeat exactly.
    """

    def __init__(self, *, frozen: bool = False, epoch: Optional[float] = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self._monotonic = monotonic
        self.frozen = frozen
        self.epoch = int(time.time()) if epoch is None else int(epoch)
        self._boot = monotonic()
        self._offset = 0.0

    def uptime(self) -> int:
        """Seconds since boot, as uint32 (ticks_s)."""
        elapsed = self._offset if self.frozen else (self._monotonic() - self._boot + self._offset)
        return int(elapsed) & 0xFFFFFFFF

    def advance(self, seconds: float) -> None:
        self._offset += float(seconds)

    def reboot(self) -> None:
        self._boot = self._monotonic()
        self._offset = 0.0

    def now(self) -> int:
        """Wall-clock seconds for file timestamps: epoch at boot plus uptime."""
        return self.epoch + self.uptime()


class NonceSource:
    """``(boot_random32 << 32) | uptime_s`` (req_parser.cpp:309-325)."""

    def __init__(self, clock: Clock, seed: Optional[int] = None):
        self.clock = clock
        self._rng = random.Random(seed) if seed is not None else None
        self.random = 0
        self.reboot()

    def reboot(self) -> None:
        value = 0
        while value == 0:  # 0 means "not generated yet" on the firmware
            value = self._rng.getrandbits(32) if self._rng is not None else secrets.randbits(32)
        self.random = value

    def new(self) -> int:
        return (self.random << 32) | self.clock.uptime()

    def valid(self, nonce: int) -> bool:
        rnd, ts = nonce >> 32, nonce & 0xFFFFFFFF
        age = (self.clock.uptime() - ts) & 0xFFFFFFFF
        return rnd == self.random and age < NONCE_LIFETIME_S


@dataclass
class AuthResult:
    ok: bool
    scheme: str  # "digest" or "apikey": the mode the parser ended in
    present: bool  # any X-Api-Key or Authorization header was sent
    challenge: Optional[str] = None  # WWW-Authenticate header line (with CRLF) on failure
    stale: bool = False
    username: Optional[str] = None
    nonce: Optional[str] = None  # the nonce the client sent, as 16 hex digits

    def log_dict(self) -> dict:
        d = {"scheme": self.scheme, "present": self.present, "ok": self.ok}
        if self.username is not None:
            d["username"] = self.username
        if self.scheme == "digest" and not self.ok:
            d["stale"] = self.stale
        return d


def parse_auth_params(value: str) -> list[tuple[str, str]]:
    """Split ``Scheme k=v, k="v"`` the way the firmware automaton walks it.

    The scheme token (anything up to the first whitespace) is skipped. Quoted
    values end at the next quote with no escape handling; unquoted values end
    at whitespace or a comma.
    """
    i, n = 0, len(value)
    while i < n and value[i] not in " \t":
        i += 1
    out = []
    while i < n:
        while i < n and value[i] in " \t,":
            i += 1
        start = i
        while i < n and value[i] not in "= \t,":
            i += 1
        name = value[start:i]
        while i < n and value[i] in " \t":
            i += 1
        if i >= n or value[i] != "=":
            continue
        i += 1
        while i < n and value[i] in " \t":
            i += 1
        if i < n and value[i] == '"':
            i += 1
            start = i
            while i < n and value[i] != '"':
                i += 1
            out.append((name, value[start:i]))
            i += 1
        else:
            start = i
            while i < n and value[i] not in " \t,":
                i += 1
            out.append((name, value[start:i]))
    return out


@dataclass
class _ParseState:
    mode: str = "digest"
    nonce: int = 0
    response: int = 0
    key: tuple = ("mono",)  # ("mono",) | ("idx", n) | ("bool", ok)
    present: bool = False
    username: Optional[str] = None
    nonce_text: Optional[str] = None


def _parse(headers: Sequence[tuple[str, str]], password: str) -> _ParseState:
    """Replay the parser's auth events over the ordered header list."""
    st = _ParseState()
    key = password.encode("utf-8").decode("latin-1")
    for name, value in headers:
        lname = name.lower()
        if lname == "x-api-key":
            st.present = True
            # An empty value still feeds the line terminator into the key
            # matcher, so it fails ("Empty key" in server_tests.cpp).
            for ch in (value or "\r"):
                if st.mode != "apikey":
                    st.mode = "apikey"
                    st.key = ("mono",)
                if not key:
                    st.key = ("bool", False)
                    continue
                if st.key[0] == "mono":
                    st.key = ("idx", 0)
                if st.key[0] == "idx":
                    idx = st.key[1]
                    if idx >= len(key) or key[idx] != ch:
                        st.key = ("bool", False)
                    elif idx + 1 == len(key):
                        st.key = ("bool", True)
                    else:
                        st.key = ("idx", idx + 1)
                else:
                    st.key = ("bool", False)
        elif lname == "authorization":
            st.present = True
            for pname, pvalue in parse_auth_params(value):
                plow = pname.lower()
                if plow == "username":
                    st.username = pvalue
                if plow not in ("nonce", "response"):
                    continue
                for ch in pvalue:
                    if st.mode != "digest":
                        st.mode = "digest"
                        st.nonce = 0
                        st.response = 0
                    if ch not in _HEX:
                        continue
                    digit = int(ch, 16)
                    if plow == "nonce":
                        st.nonce = ((st.nonce << 4) | digit) & 0xFFFFFFFFFFFFFFFF
                    else:
                        st.response = ((st.response << 4) | digit) & ((1 << 128) - 1)
    if st.mode == "digest":
        st.nonce_text = f"{st.nonce:016x}"
    return st


def _digest_matches(st: _ParseState, password: str, method: str, target: str, nonce: int) -> bool:
    if not password:
        return False  # login disabled (req_parser.cpp:395-399)
    expected = digest_response(password, method, target, f"{nonce:016x}")
    return int(expected, 16) == st.response


def check(headers: Sequence[tuple[str, str]], method: str, target: str, password: str,
          nonces: NonceSource) -> AuthResult:
    """Authenticate one request (RequestParser::check_auth)."""
    st = _parse(headers, password)
    if st.mode == "apikey":
        ok = st.key == ("bool", True)
        return AuthResult(
            ok=ok,
            scheme="apikey",
            present=st.present,
            challenge=None if ok else f'WWW-Authenticate: ApiKey realm="{REALM}"\r\n',
        )
    result = AuthResult(ok=False, scheme="digest", present=st.present, username=st.username,
                        nonce=st.nonce_text if st.present else None)
    if nonces.valid(st.nonce):
        if _digest_matches(st, password, method, target, st.nonce):
            result.ok = True
            return result
        stale = False
    else:
        # A correct digest over an expired or previous-boot nonce means the
        # client knows the password: tell it to retry (req_parser.cpp:341-347).
        stale = _digest_matches(st, password, method, target, st.nonce)
    result.stale = stale
    result.challenge = (
        f'WWW-Authenticate: Digest realm="{REALM}", nonce="{nonces.new():016x}", '
        f'stale={"true" if stale else "false"}\r\n'
    )
    return result


def redact_header(name: str, value: str, auth: Optional[AuthResult]) -> str:
    """Header value safe for the request log: never the key or the digest response."""
    lname = name.lower()
    if lname == "x-api-key":
        if auth is not None and auth.scheme == "apikey":
            return "<redacted:match>" if auth.ok else "<redacted:mismatch>"
        return "<redacted>"
    if lname == "authorization":
        scheme = value.split(None, 1)[0] if value.strip() else ""
        if scheme.lower() != "digest":
            return f"{scheme} <redacted>".strip()
        parts = []
        for pname, pvalue in parse_auth_params(value):
            if pname.lower() == "response":
                pvalue = "<redacted>"
            parts.append(f'{pname}="{pvalue}"')
        return f"{scheme} " + ", ".join(parts)
    return value


@dataclass
class DigestClient:
    """Minimal client-side helper used by tests and the contract suite."""

    password: str = field(repr=False)
    username: str = USERNAME

    def header(self, method: str, target: str, nonce: str, uri: Optional[str] = None) -> str:
        resp = digest_response(self.password, method, target, nonce, self.username)
        return (f'Digest username="{self.username}", realm="{REALM}", nonce="{nonce}", '
                f'uri="{uri if uri is not None else target}", response="{resp}"')
