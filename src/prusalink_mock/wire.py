"""HTTP/1.1 request parsing and response framing, matching the firmware.

Request side: lib/WUI/nhttp/req_parser.cpp and the generated automaton
(utils/gen-automata/http.py, http_server.py). Response side:
lib/WUI/nhttp/headers.cpp (write_headers), status_page.cpp, send_json.cpp and
src/common/http/chunked.h.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import render

MAX_URL_LEN = 168  # src/common/http/types.h:154
BUFF_SIZE = 1024  # Server::BUFF_SIZE = TCP_MSS (server.h:156, lwipopts.h:44)
KNOWN_METHODS = ("GET", "POST", "PUT", "DELETE", "HEAD")

# Connection handling of a response (http::ConnectionHandling plus ErrorClose).
KEEP = "keep"  # Connection: keep-alive
CLOSE = "close"  # Connection: close, close after writing
ERROR_CLOSE = "error_close"  # Connection: close, half-close, drain input, close (handler.h:129-131)

_REQUEST_LINE = re.compile(r"^([^ \t]+)[ \t]+([^ \t]+)[ \t]+[Hh][Tt][Tt][Pp]/([0-9][0-9.]*)$")


@dataclass
class Request:
    method: str  # upper-case for known methods, raw token otherwise
    target: str  # raw request target, latin-1 decoded
    version: str  # e.g. "HTTP/1.1"
    headers: list[tuple[str, str]] = field(default_factory=list)
    error: Optional[int] = None  # parse error status (400, 414, 431)
    method_known: bool = True
    version_major: int = 0
    version_minor: int = 0
    connection: Optional[str] = None  # "close" | "keep-alive"
    content_length: Optional[int] = None
    accepts_json: bool = False
    print_after_upload: bool = False
    overwrite: bool = False
    create_folder: bool = False
    boundary: str = ""
    if_none_match: int = 0

    def can_keep_alive(self) -> bool:
        """RequestParser::can_keep_alive (req_parser.cpp:265-267)."""
        return self.connection == "keep-alive" or (
            self.version_major == 1 and self.version_minor >= 1 and self.connection != "close"
        )

    def has_body(self) -> bool:
        """http::has_body (types.h:41-56): only POST and PUT."""
        return self.method in ("POST", "PUT")

    def page_handling(self, status: int) -> str:
        """Close handling of StatusPage(status, parser) (status_page.cpp:27-33)."""
        if self.has_body() and status >= 300:
            return ERROR_CLOSE
        return KEEP if self.can_keep_alive() else CLOSE

    def plain_handling(self) -> str:
        """RequestParser::status_page_handling."""
        return KEEP if self.can_keep_alive() else CLOSE

    def path(self) -> str:
        """Target without the query, duplicate slashes collapsed (for logs and fault rules)."""
        return re.sub(r"/{2,}", "/", self.target.split("?", 1)[0])

    def header(self, name: str) -> Optional[str]:
        lname = name.lower()
        value = None
        for n, v in self.headers:
            if n.lower() == lname:
                value = v
        return value


def _prefix(value: str, keyword: str) -> bool:
    """keyworded_header: case-insensitive prefix match after leading whitespace."""
    return value.lstrip(" \t").lower().startswith(keyword)


def parse_head(lines: list[bytes]) -> Request:
    """Build a Request from the head lines (CR/LF already stripped)."""
    text = [line.decode("latin-1") for line in lines]
    m = _REQUEST_LINE.match(text[0]) if text else None
    if not m:
        return Request(method="", target="", version="", error=400)
    token, target, ver = m.group(1), m.group(2), m.group(3)
    req = Request(method=token.upper() if token.upper() in KNOWN_METHODS else token,
                  target=target, version=f"HTTP/{ver}")
    req.method_known = token.upper() in KNOWN_METHODS
    # Version digits as the parser accumulates them into 2- and 4-bit fields.
    major, minor = 0, 0
    for ch in ver:
        if ch == ".":
            major, minor = minor & 0x3, 0
        else:
            minor = (minor * 10 + int(ch)) & 0xF
    req.version_major, req.version_minor = major, minor
    if len(target) > MAX_URL_LEN:
        req.error = 414

    headers: list[tuple[str, str]] = []
    for line in text[1:]:
        if line[:1] in (" ", "\t") and headers:
            name, value = headers[-1]
            headers[-1] = (name, value + line.lstrip(" \t"))
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        headers.append((name.rstrip(" \t"), value.lstrip(" \t")))
    req.headers = headers

    for name, value in headers:
        lname = name.lower()
        if lname == "connection":
            if _prefix(value, "close"):
                req.connection = "close"
            elif _prefix(value, "keep-alive"):
                req.connection = "keep-alive"
        elif lname == "content-length":
            # Digits are accumulated across every Content-Length header.
            cl = req.content_length or 0
            for ch in value:
                cl = (cl * 10 + (ord(ch) - 48)) & 0xFFFFFFFFFFFFFFFF
            req.content_length = cl
        elif lname == "accept":
            if _prefix(value, "application/json"):
                req.accepts_json = True
        elif lname == "print-after-upload":
            if _prefix(value, "true") or _prefix(value, "1") or _prefix(value, "?1"):
                req.print_after_upload = True
        elif lname == "overwrite":
            if _prefix(value, "?1"):
                req.overwrite = True
        elif lname == "create-folder":
            if _prefix(value, "?1"):
                req.create_folder = True
        elif lname == "if-none-match":
            for ch in value:
                if ch.isdigit():
                    req.if_none_match = (req.if_none_match * 10 + int(ch)) & 0xFFFFFFFF
        elif lname == "content-type":
            for part in value.split(";")[1:]:
                part = part.lstrip(" \t")
                if part.lower().startswith("boundary="):
                    boundary = re.split(r"[ \t;]", part[len("boundary="):], maxsplit=1)[0]
                    req.boundary = boundary
                    if req.error is None and len(boundary) > MAX_URL_LEN - len(target):
                        req.error = 431
    return req


# --- responses ---------------------------------------------------------------

def write_headers(status: int, content_type: str, handling: str, content_length: Optional[int] = None,
                  etag: Optional[int] = None, extra: tuple[str, ...] = (), chunked: bool = False) -> bytes:
    """write_headers (headers.cpp:64-103): fixed order, no Date or Server."""
    out = [
        f"HTTP/1.1 {status} {render.status_text(status)}\r\n",
        f"Content-Type: {content_type}\r\n",
        f"Connection: {'keep-alive' if handling == KEEP else 'close'}\r\n",
    ]
    if content_length is not None:
        out.append(f"Content-Length: {content_length}\r\n")
    if chunked:
        out.append("Transfer-Encoding: chunked\r\n")
    if etag is not None:
        out.append(f'ETag: "{etag}"\r\n')
    out.extend(extra)
    out.append("\r\n")
    return "".join(out).encode("latin-1")


def frame_chunked(head_len: int, body: bytes) -> bytes:
    """Chunked body with the firmware's ``%04X`` sizes and 1024-byte packets."""
    out = bytearray()
    avail = BUFF_SIZE - head_len
    if avail <= 8:  # MIN_CHUNK_SIZE: headers go out alone
        avail = BUFF_SIZE
    pos = 0
    while pos < len(body):
        piece = body[pos:pos + min(avail - 8, 0xFFFF)]
        out += b"%04X\r\n" % len(piece) + piece + b"\r\n"
        pos += len(piece)
        avail = BUFF_SIZE
    out += b"0\r\n\r\n"
    return bytes(out)


@dataclass
class Response:
    status: int
    head: bytes
    body: bytes = b""
    chunked: bool = False
    after: str = KEEP  # KEEP, CLOSE or ERROR_CLOSE
    is_json: bool = False
    content_type: str = ""
    route: str = ""

    def wire(self) -> bytes:
        return self.head + (frame_chunked(len(self.head), self.body) if self.chunked else self.body)


def status_page(status: int, handling: str, json_content: bool, message: str = "",
                extra: tuple[str, ...] = (), etag: Optional[int] = None) -> Response:
    """StatusPage::step_impl (status_page.cpp:42-85). The body is sent even for HEAD."""
    body, ctype = render.status_page_body(status, message, json_content)
    head = write_headers(status, ctype, handling, len(body), etag, extra)
    return Response(status, head, body, after=handling, content_type=ctype)


def json_response(body: bytes, keep_alive: bool, status: int = 200, extra: tuple[str, ...] = ()) -> Response:
    """SendJson: chunked on keep-alive, close-delimited otherwise (send_json.h:51-53)."""
    handling = KEEP if keep_alive else CLOSE
    head = write_headers(status, "application/json", handling, extra=extra, chunked=keep_alive)
    return Response(status, head, body, chunked=keep_alive, after=handling, is_json=True,
                    content_type="application/json")


def static_response(body: bytes, content_type: str, keep_alive: bool) -> Response:
    """SendStaticMemory (static_mem.cpp): Content-Length, keep-alive when allowed."""
    handling = KEEP if keep_alive else CLOSE
    head = write_headers(200, content_type, handling, len(body))
    return Response(200, head, body, after=handling, is_json=content_type == "application/json",
                    content_type=content_type)
