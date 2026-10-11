"""Raw HTTP/1.x client for wire-level assertions.

http.client and urllib normalize too much (header order, chunk sizes, Expect),
so the tests talk to the socket directly.
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from typing import Iterable, Optional

from prusalink_mock.auth import DigestClient


@dataclass
class RawResponse:
    status: int
    reason: str
    head: bytes
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    chunked: bool = False
    chunk_sizes: list[str] = field(default_factory=list)
    closed: bool = False
    reset: bool = False

    def header(self, name: str) -> Optional[str]:
        lname = name.lower()
        for n, v in self.headers:
            if n.lower() == lname:
                return v
        return None

    def header_lines(self) -> list[str]:
        """Status line and header lines, in order."""
        return self.head.decode("latin-1").split("\r\n")[:-2]

    @property
    def nonce(self) -> Optional[str]:
        m = re.search(r'nonce="([0-9a-f]{16})"', self.header("WWW-Authenticate") or "")
        return m.group(1) if m else None


class Conn:
    def __init__(self, host: str, port: int, timeout: float = 15.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        self.eof = False
        self.reset = False

    def __enter__(self) -> "Conn":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def _fill(self) -> bool:
        if self.eof:
            return False
        try:
            data = self.sock.recv(65536)
        except (ConnectionResetError, ConnectionAbortedError):
            self.reset = True
            self.eof = True
            return False
        if not data:
            self.eof = True
            return False
        self.buf += data
        return True

    def _until(self, sep: bytes) -> bytes:
        while sep not in self.buf:
            if not self._fill():
                raise EOFError(f"connection closed before {sep!r}; have {self.buf[:200]!r}")
        idx = self.buf.index(sep) + len(sep)
        out, self.buf = self.buf[:idx], self.buf[idx:]
        return out

    def _exactly(self, n: int) -> bytes:
        while len(self.buf) < n:
            if not self._fill():
                raise EOFError(f"connection closed with {len(self.buf)} of {n} bytes")
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_to_eof(self, timeout: float = 5.0) -> bytes:
        self.sock.settimeout(timeout)
        while self._fill():
            pass
        out, self.buf = self.buf, b""
        return out

    def read_response(self, no_body: bool = False) -> RawResponse:
        head = self._until(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        version, status, reason = (lines[0].split(" ", 2) + [""])[:3]
        headers = []
        for line in lines[1:]:
            if line:
                name, _, value = line.partition(":")
                headers.append((name, value.lstrip()))
        resp = RawResponse(int(status), reason, head, headers)
        te = (resp.header("Transfer-Encoding") or "").lower()
        cl = resp.header("Content-Length")
        if no_body:
            pass
        elif te == "chunked":
            resp.chunked = True
            body = b""
            while True:
                size_line = self._until(b"\r\n")[:-2].decode("latin-1")
                resp.chunk_sizes.append(size_line)
                size = int(size_line, 16)
                if size == 0:
                    self._until(b"\r\n")
                    break
                body += self._exactly(size)
                assert self._exactly(2) == b"\r\n", "chunk not followed by CRLF"
            resp.body = body
        elif cl is not None:
            resp.body = self._exactly(int(cl))
        else:
            resp.body = self.read_to_eof()
        if (resp.header("Connection") or "").lower() == "close":
            rest = self.read_to_eof()
            resp.body += rest if (cl is None and te != "chunked" and not no_body) else b""
            resp.closed = self.eof
        resp.reset = self.reset
        return resp

    def request(self, method: str, target: str, headers: Iterable[tuple[str, str]] = (), body: bytes = b"",
                version: str = "HTTP/1.1", no_body: Optional[bool] = None) -> RawResponse:
        lines = [f"{method} {target} {version}", "Host: 127.0.0.1"]
        # A value can be a conftest.Secret, revealed only here.
        lines += [f"{n}: {v.reveal() if hasattr(v, 'reveal') else v}" for n, v in headers]
        self.send(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        return self.read_response(no_body=False if no_body is None else no_body)


def digest_request(conn: Conn, method: str, target: str, password: str, headers: Iterable[tuple[str, str]] = (),
                   body: bytes = b"", username: str = "maker") -> tuple[RawResponse, RawResponse]:
    """The two legs of a Digest request on one connection (GET-like methods)."""
    headers = list(headers)
    first = conn.request(method, target, headers)
    assert first.status == 401, first.status
    auth = DigestClient(password, username).header(method, target, first.nonce)
    second = conn.request(method, target, headers + [("Authorization", auth)], body)
    return first, second


def nonce_masked(lines: list[str]) -> list[str]:
    return [re.sub(r'nonce="[0-9a-f]{16}"', 'nonce="<nonce>"', line) for line in lines]
