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

import functools
import hashlib
import random
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .state import FAT_TIME_MAX, FAT_TIME_MIN

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
    """Uptime for nonces, and wall time for file timestamps.

    Real monotonic time by default. ``frozen`` makes time move only through
    :meth:`advance`, so test runs repeat exactly. A reboot restarts the uptime
    but not the wall time: on the printer that comes from the RTC and SNTP
    (src/common/sys_time.cpp, wui_api.cpp:246), and file times from FatFS's
    get_fattime() (src/buddy/fatfs.cpp:19).

    The wall time stays within the FAT date range (1980 to 2107), the only
    times a file on the USB drive can carry; that also keeps the uptime within
    the uint32 seconds of ticks_s.
    """

    def __init__(self, *, frozen: bool = False, epoch: Optional[float] = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self._monotonic = monotonic
        self.frozen = frozen
        start = min(max(time.time(), FAT_TIME_MIN), FAT_TIME_MAX) if epoch is None else epoch
        # Compared before any conversion: an int too large for a double can't be converted.
        if (isinstance(start, bool) or not isinstance(start, (int, float))
                or not FAT_TIME_MIN <= start <= FAT_TIME_MAX):
            raise ValueError(f"epoch must be a Unix time in {FAT_TIME_MIN}..{FAT_TIME_MAX}, "
                             "1980 to 2107 (the FAT date range)")
        self.epoch = float(int(start))  # wall time at the last boot
        self._boot = monotonic()
        self._offset = 0.0

    def _elapsed(self) -> float:
        return self._offset if self.frozen else (self._monotonic() - self._boot + self._offset)

    def uptime(self) -> int:
        """Seconds since boot, as uint32 (ticks_s)."""
        return int(self._elapsed()) & 0xFFFFFFFF

    def advance(self, seconds: float) -> None:
        """Move time on by ``seconds``.

        ValueError, with the clock unchanged, unless ``seconds`` is a number from 0
        up to what keeps the wall time within 2107.
        """
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("seconds must be a number")
        left = max(0, FAT_TIME_MAX - int(self.epoch + self._elapsed()))
        # Compared before any conversion: an int too large for a double can't be
        # converted, and NaN fails every comparison.
        if not 0 <= seconds <= left:
            raise ValueError(f"seconds must be in 0..{left}: the wall clock stops at the end of 2107, "
                             "the last FAT date")
        self._offset += float(seconds)

    def reboot(self) -> None:
        self.epoch = min(self.epoch + self._elapsed(), float(FAT_TIME_MAX))
        self._boot = self._monotonic()
        self._offset = 0.0

    def now(self) -> int:
        """Wall-clock seconds for file timestamps."""
        return min(int(self.epoch + self._elapsed()), FAT_TIME_MAX)


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

    def log_dict(self, password: str = "") -> dict:
        d = {"scheme": self.scheme, "present": self.present, "ok": self.ok}
        if self.username is not None:
            d["username"] = scrub(self.username, password)
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


# Schemes whose name is safe to log; any other first word could be the key itself.
_LOGGED_SCHEMES = ("basic", "bearer", "negotiate", "ntlm", "apikey", "token")


def scrub(text: str, password: str) -> str:
    """``text`` with every occurrence of the password replaced by <redacted>.

    Header values are latin-1 decoded bytes, so the password is matched as its
    UTF-8 bytes read the same way.
    """
    key = password.encode("utf-8").decode("latin-1")
    return text.replace(key, "<redacted>") if key else text


@functools.lru_cache(maxsize=8)
def _password_pattern(password: str) -> "re.Pattern[str]":
    """Each character of the password as itself, or as its UTF-8 bytes, each one read as
    latin-1 (how header values and raw targets are decoded) or percent-encoded in either
    case on its own, or a space as '+'."""
    parts = []
    for ch in password:
        raw = ch.encode("utf-8")
        alternatives = {re.escape(ch), "".join(
            f"(?:{re.escape(chr(b))}|%[{b >> 4:X}{b >> 4:x}][{b & 15:X}{b & 15:x}])" for b in raw)}
        if ch == " ":
            alternatives.add(r"\+")
        parts.append("(?:" + "|".join(sorted(alternatives)) + ")")
    return re.compile("".join(parts))


def _redact(text: str, passwords: Sequence[str]) -> str:
    """``text`` with every occurrence of any of the passwords replaced by one <redacted>.

    Occurrences are found overlapping, and overlapping or adjacent ones are merged
    before replacing, so no part of a password survives one that contains or overlaps
    it (oldsecret and oldsecretNEW, say)."""
    spans = []
    for password in passwords:
        if password:
            lookahead = re.compile(f"(?=({_password_pattern(password).pattern}))")
            spans.extend(m.span(1) for m in lookahead.finditer(text))
    if not spans:
        return text
    spans.sort()
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out, last = [], 0
    for start, end in merged:
        out += [text[last:start], "<redacted>"]
        last = end
    return "".join(out) + text[last:]


def scrub_value(value, *passwords: str):
    """``value`` with the passwords redacted from every string in it, for the request
    log: a client can send one in the target, a file name or a header, encoded or not."""
    if not any(passwords):
        return value
    if isinstance(value, str):
        return _redact(value, passwords)
    if isinstance(value, dict):
        return {k: scrub_value(v, *passwords) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_value(v, *passwords) for v in value]
    return value


def redact_header(name: str, value: str, auth: Optional[AuthResult], password: str = "") -> str:
    """Header value safe for the request log: never the key or the digest response.

    Authorization is rebuilt from its parameters, so text the client ran into it
    across line ends (another header, say) is scrubbed with the rest.
    """
    lname = name.lower()
    if lname == "x-api-key":
        if auth is not None and auth.scheme == "apikey":
            return "<redacted:match>" if auth.ok else "<redacted:mismatch>"
        return "<redacted>"
    if lname == "authorization":
        scheme = value.split(None, 1)[0] if value.strip() else ""
        if scheme.lower() != "digest":
            shown = scheme if scheme.lower() in _LOGGED_SCHEMES else ""
            return f"{shown} <redacted>".strip()
        parts = []
        for pname, pvalue in parse_auth_params(value):
            if pname.lower() == "response":
                pvalue = "<redacted>"
            parts.append(f'{scrub(pname, password)}="{scrub(pvalue, password)}"')
        return f"{scheme} " + ", ".join(parts)
    return scrub(value, password)


@dataclass
class DigestClient:
    """Minimal client-side helper used by tests and the contract suite."""

    password: str = field(repr=False)
    username: str = USERNAME

    def header(self, method: str, target: str, nonce: str, uri: Optional[str] = None) -> str:
        resp = digest_response(self.password, method, target, nonce, self.username)
        return (f'Digest username="{self.username}", realm="{REALM}", nonce="{nonce}", '
                f'uri="{uri if uri is not None else target}", response="{resp}"')
