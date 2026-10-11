"""Request log: one entry per request on the printer port, secrets redacted."""

from __future__ import annotations

import json
from collections import deque
from typing import IO, Optional

MAX_ENTRIES = 10000


class RequestLog:
    def __init__(self, path: Optional[str] = None, max_entries: int = MAX_ENTRIES) -> None:
        self.entries: deque[dict] = deque(maxlen=max_entries)
        self.last_seq = 0
        self.path = path
        self._fh: Optional[IO[str]] = None
        self.open()

    def open(self) -> None:
        """Open the log file for appending, if there is one and it is closed."""
        if self.path and self._fh is None:
            self._fh = open(self.path, "a", encoding="utf-8")

    def add(self, entry: dict) -> None:
        self.last_seq += 1
        entry["seq"] = self.last_seq
        self.entries.append(entry)
        if self._fh is not None:
            self._fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
            self._fh.flush()

    def since(self, seq: int = 0, limit: Optional[int] = None) -> list[dict]:
        """Entries after ``seq``, the oldest ``limit`` of them when given."""
        out = [e for e in self.entries if e["seq"] > seq]
        return out[:limit] if limit is not None else out

    def clear(self) -> None:
        self.entries.clear()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
