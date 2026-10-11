"""HTTP/1.1 request parsing and response framing, matching the firmware.

Request side: lib/WUI/nhttp/req_parser.cpp and the automaton generated from
utils/gen-automata/http.py, parts.py and http_server.py. Response side:
lib/WUI/nhttp/headers.cpp (write_headers), status_page.cpp, send_json.cpp and
src/common/http/chunked.h.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import render

MAX_URL_LEN = 168  # src/common/http/types.h:154
IDLE_TIMEOUT_S = 60.0  # Server::IDLE_TIMEOUT (server.h:161)
ACTIVE_TIMEOUT_S = 10.0  # Server::ACTIVE_TIMEOUT (server.h:168)
BUFF_SIZE = 1024  # Server::BUFF_SIZE = TCP_MSS (server.h:156, lwipopts.h:44)
KNOWN_METHODS = ("GET", "POST", "PUT", "DELETE", "HEAD")

# Connection handling of a response (http::ConnectionHandling plus ErrorClose).
KEEP = "keep"  # Connection: keep-alive
CLOSE = "close"  # Connection: close, close after writing
ERROR_CLOSE = "error_close"  # Connection: close, half-close, drain input, close (handler.h:129-131)

# What the firmware writes when an active connection times out while it still
# wants input. It writes sizeof() of the literal, so the NUL goes out too, and
# then closes (server.cpp:481-521).
GOODBYE = b"HTTP/1.1 408 Request Timeout\r\n\r\n\x00"

_HWS = frozenset(b" \t")  # HorizWhitespace
_SPACE = frozenset(b" \t\n\v\f\r")  # Whitespace (isspace)
_LF, _CR, _COLON = 10, 13, 58
_STORE_MAX = 64 * 1024  # bytes of one name or value kept for the log; the rest is parsed and dropped

_METHODS = {m.lower().encode(): m for m in KNOWN_METHODS}

# How the value parser of each known header (http_server.py:4-15) reads the
# bytes after the colon:
# _RAW: read_header_value, joined without fallthrough, so the first byte after
#   the colon and spaces belongs to the value even when it is CR or LF. A
#   continuation line's first byte does too. Unknown headers read this way.
# _KEYWORD: keyworded_header, joined with fallthrough: a line end right after
#   the colon ends the header. Continuation lines are read as _RAW.
# _BOUNDARY: read_boundary, fallthrough both after the colon and on
#   continuation lines.
# _AUTH: authorization_header, no continuation lines.
_RAW, _KEYWORD, _BOUNDARY, _AUTH = "raw", "keyword", "boundary", "auth"
_HEADERS = {
    b"x-api-key": _RAW,
    b"authorization": _AUTH,
    b"content-length": _RAW,
    b"if-none-match": _RAW,
    b"print-after-upload": _KEYWORD,
    b"overwrite": _KEYWORD,
    b"create-folder": _KEYWORD,
    b"content-type": _BOUNDARY,
    b"connection": _KEYWORD,
    b"accept": _KEYWORD,
}


def _lower(b: int) -> int:
    return b + 32 if 65 <= b <= 90 else b


def _is_prefix(cand: bytes, words) -> bool:
    return any(w.startswith(cand) for w in words)


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
    url_len: int = 0  # bytes of the URL as sent

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


class HeadParser:
    """Request head reader that follows the firmware's generated automaton.

    The structure is request() in utils/gen-automata/http.py; the events are
    RequestParser::event (req_parser.cpp:68-218).

    * The method runs to a space or tab. A byte that leaves the keyword trie is
      eaten into an unknown method, even a space, CR or LF (parts.py:68-78), so
      a stray CRLF before "GET" makes an unknown method (405).
    * The URL runs to the next space or tab, CR and LF included.
    * Then "HTTP/" in any case, a digit, and digits and dots. Any other byte
      there has no transition: 400 and close (req_parser.cpp:236-240).
    * A line ends at LF, CR LF, or a CR followed by anything else, which stays
      unread (newline(), parts.py:102-122).
    * Header names go through a trie of the headers the parser knows. A byte
      that leaves the trie, a colon included, starts an unknown header whose
      name runs to the next colon, across line ends.
    * A line starting with a space or tab continues the header.
    * An empty line ends the head; after a bare CR the next byte is the first
      byte of the body.

    Authorization is simplified: its scheme runs to the first space or tab and
    a quoted parameter to its closing quote, both across line ends; otherwise
    the header ends at the line end.
    """

    def __init__(self) -> None:
        self.state = "method"
        self.done = False
        self.error: Optional[int] = None
        self.consumed = 0
        self.method: Optional[str] = None
        self._method = bytearray()
        self._method_unknown = False
        self._url = bytearray()
        self._url_len = 0
        self._http = 0
        self._version = bytearray()
        self.version_major = 0
        self.version_minor = 0
        self.headers: list[tuple[str, str]] = []
        self._name = bytearray()
        self._name_low = b""
        self._name_mode = "trie"
        self._kind = _RAW
        self._value = bytearray()

    def feed(self, data: bytes) -> int:
        """Read bytes until the head is complete; returns how many were used."""
        i, n = 0, len(data)
        while i < n and not self.done:
            if self._step(data[i]):
                i += 1
        self.consumed += i
        return i

    def eof(self) -> None:
        """The client closed before the head was complete: 400 (req_parser.cpp:242-246)."""
        if not self.done:
            self.error = 400
            self.done = True

    # --- automaton -------------------------------------------------------------

    def _fail(self) -> bool:
        self.error = 400
        self.done = True
        return False

    def _step(self, b: int) -> bool:
        """One byte; False means it was not consumed (a fallthrough or the end)."""
        st = self.state
        if st == "method":
            if self._method_unknown:
                if b in _HWS:
                    self.state = "meth_sp"
                elif len(self._method) < _STORE_MAX:
                    self._method.append(b)
                return True
            low = bytes(_lower(c) for c in self._method)
            if b in _HWS and low in _METHODS:
                self.method = _METHODS[low]
                self.state = "meth_sp"
                return True
            if not _is_prefix(low + bytes([_lower(b)]), _METHODS):
                self._method_unknown = True
            self._method.append(b)
            return True
        if st == "meth_sp":
            if b in _HWS:
                return True
            self.state = "url"
            self._url_byte(b)
            return True
        if st == "url":
            if b in _HWS:
                self.state = "url_sp"
            else:
                self._url_byte(b)
            return True
        if st == "url_sp":
            if b in _HWS:
                return True
            if _lower(b) == ord("h"):
                self.state, self._http = "http", 0
                return True
            return self._fail()
        if st == "http":
            if _lower(b) != b"ttp/"[self._http]:
                return self._fail()
            self._http += 1
            if self._http == 4:
                self.state = "slash"
            return True
        if st == "slash":
            if not 48 <= b <= 57:
                return self._fail()
            self._version_event(b)
            self.state = "version"
            return True
        if st == "version":
            if b == 46 or 48 <= b <= 57:
                self._version_event(b)
                return True
            if b == _LF:
                self.state = "h_start"
                return True
            if b == _CR:
                self.state = "line_cr"
                return True
            return self._fail()
        if st == "line_cr":
            self.state = "h_start"
            return b == _LF
        if st == "h_start":
            if b == _LF:
                self.done = True
                return True
            if b == _CR:
                self.state = "h_cr"
                return True
            self._name, self._name_low, self._name_mode = bytearray(), b"", "trie"
            self.state = "h_name"
            return False
        if st == "h_cr":
            self.done = True
            return b == _LF
        if st == "h_name":
            return self._name_byte(b)
        if st == "h_sep":
            if b in _HWS:
                return True
            self._value = bytearray()
            if self._kind == _AUTH:
                self.state = "a_scheme"
                return False
            self.state = "h_val"
            if self._kind == _RAW:
                self._value_byte(b)
                return True
            return False
        if st == "h_val":
            if b == _LF:
                self.state = "h_end"
            elif b == _CR:
                self.state = "h_val_cr"
            else:
                self._value_byte(b)
            return True
        if st == "h_val_cr":
            self.state = "h_end"
            return b == _LF
        if st == "h_end":
            if b in _HWS and self._kind != _AUTH:
                self.state = "h_cont"
                return True
            self._end_header()
            self.state = "h_start"
            return False
        if st == "h_cont":
            if b in _HWS:
                return True
            self.state = "h_val"
            if self._kind == _BOUNDARY:
                return False
            self._value_byte(b)
            return True
        return self._auth_step(st, b)

    def _auth_step(self, st: str, b: int) -> bool:
        if st == "a_scheme":
            if b in _HWS:
                self.state = "a_param"
            self._value_byte(b)
            return True
        if st == "a_param":
            if b == _LF:
                self.state = "h_end"
                return True
            if b == _CR:
                self.state = "h_val_cr"
                return True
            if b not in _HWS:
                self.state = "a_name"
            self._value_byte(b)
            return True
        if st == "a_name":
            if b in _SPACE:
                self.state = "a_param"
                return False
            if b == ord("="):
                self.state = "a_sep"
            self._value_byte(b)
            return True
        if st == "a_sep":
            if b in _SPACE or b == ord(","):
                if b not in _HWS:
                    self.state = "a_end"
                    return False
            elif b == ord('"'):
                self.state = "a_quoted"
            else:
                self.state = "a_unquoted"
            self._value_byte(b)
            return True
        if st == "a_quoted":
            if b == ord('"'):
                self.state = "a_end"
            self._value_byte(b)
            return True
        if st == "a_unquoted":
            if b in _SPACE or b == ord(","):
                self.state = "a_end"
                return False
            self._value_byte(b)
            return True
        # a_end
        if b in _HWS or b == ord(","):
            self._value_byte(b)
            return True
        self.state = "a_param"
        return False

    def _name_byte(self, b: int) -> bool:
        mode = self._name_mode
        if mode == "unknown":
            if b == _COLON:
                self._start_value(None)
            elif len(self._name) < _STORE_MAX:
                self._name.append(b)
            return True
        if mode == "term":  # a complete known name followed by spaces
            if b == _COLON:
                self._start_value(self._name_low)
                return True
            if b not in _HWS:
                self._name_mode = "unknown"
            self._name.append(b)
            return True
        if self._name_low in _HEADERS:
            if b == _COLON:
                self._start_value(self._name_low)
                return True
            if b in _HWS:
                self._name_mode = "term"
                self._name.append(b)
                return True
        cand = self._name_low + bytes([_lower(b)])
        if _is_prefix(cand, _HEADERS):
            self._name_low = cand
        else:
            self._name_mode = "unknown"
        self._name.append(b)
        return True

    def _start_value(self, known: Optional[bytes]) -> None:
        self._kind = _HEADERS[known] if known is not None else _RAW
        self.state = "h_sep"

    def _value_byte(self, b: int) -> None:
        if len(self._value) < _STORE_MAX:
            self._value.append(b)

    def _end_header(self) -> None:
        name = self._name.decode("latin-1").rstrip(" \t")
        self.headers.append((name, self._value.decode("latin-1")))

    def _url_byte(self, b: int) -> None:
        self._url_len += 1
        if len(self._url) < _STORE_MAX:
            self._url.append(b)

    def _version_event(self, b: int) -> None:
        """Version digits into the parser's 2- and 4-bit fields (req_parser.cpp:181-190)."""
        self._version.append(b)
        if b == 46:
            self.version_major, self.version_minor = self.version_minor & 0x3, 0
        else:
            self.version_minor = (self.version_minor * 10 + b - 48) & 0xF

    # --- result ------------------------------------------------------------------

    def request(self) -> Request:
        """The parsed request; call once ``done`` is set."""
        known = self.method is not None
        req = Request(method=self.method if known else self._method.decode("latin-1"),
                      target=self._url.decode("latin-1"),
                      version=("HTTP/" + self._version.decode("latin-1")) if self._version else "",
                      method_known=known, version_major=self.version_major, version_minor=self.version_minor)
        if self.state in ("h_end", "h_val", "h_val_cr", "h_cont") or self.state.startswith("a_"):
            self._end_header()  # a header cut short by the end of input
            self.state = "h_start"
        req.headers = list(self.headers)
        req.url_len = self._url_len
        if self._url_len > MAX_URL_LEN:
            req.error = 414
        _apply_headers(req)
        if self.error is not None:
            req.error = self.error
        return req


def _prefix(value: str, keyword: str) -> bool:
    """keyworded_header: case-insensitive prefix match after leading whitespace."""
    return value.lstrip(" \t").lower().startswith(keyword)


def _apply_headers(req: Request) -> None:
    """Header events of RequestParser::event (req_parser.cpp:68-218), in arrival order."""
    for name, value in req.headers:
        lname = name.lower()
        if lname == "connection":
            if _prefix(value, "close"):
                req.connection = "close"
            elif _prefix(value, "keep-alive"):
                req.connection = "keep-alive"
        elif lname == "content-length":
            # Every byte of every Content-Length value is a digit to the parser, and
            # the sum is a size_t, 32 bits on the printer (req_parser.cpp:175-180, req_parser.h:54).
            cl = req.content_length or 0
            for ch in value:
                cl = (cl * 10 + (ord(ch) - 48)) & 0xFFFFFFFF
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
                if "0" <= ch <= "9":  # ASCII only, like the firmware's range check
                    req.if_none_match = (req.if_none_match * 10 + ord(ch) - 48) & 0xFFFFFFFF
        elif lname == "content-type":
            for part in value.split(";")[1:]:
                part = part.lstrip(" \t")
                if part.lower().startswith("boundary="):
                    # Every boundary byte is appended after the URL in the URL buffer and
                    # the count is never reset, so all boundary= values of all
                    # Content-Type headers add up (req_parser.cpp:107-114, req_parser.h:115-158).
                    req.boundary += re.split(r"[ \t;]", part[len("boundary="):], maxsplit=1)[0]
                    if len(req.boundary) > MAX_URL_LEN - min(req.url_len, MAX_URL_LEN):
                        req.error = 431


def parse_head(data: bytes) -> Request:
    """Parse a complete head; input that ends early counts as a client close."""
    parser = HeadParser()
    parser.feed(data)
    parser.eof()
    return parser.request()



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


def goodbye() -> Response:
    """The bare 408 of a connection that stalled while the firmware wanted input."""
    return Response(408, GOODBYE, after=CLOSE)
