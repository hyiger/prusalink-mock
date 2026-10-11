"""In-memory USB drive: uploaded files and folders with FAT short names.

Lookups are case-insensitive, as on FAT. Every file and folder gets a short
name in its parent folder when it is created (sfn.short_name), so the paths
the firmware reports (``/usb/AB~1.GCO``) can be reproduced.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from .sfn import short_name

ROOT = "/usb"


def _key(path: str) -> str:
    return path.rstrip("/").upper() or ROOT.upper()


def _parent(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[0]


def _base(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[1]


@dataclass
class StoredFile:
    path: str  # long-name path as uploaded, e.g. /usb/a b.gcode
    sfn_path: str  # short-name path, e.g. /usb/AB~1.GCO
    data: bytes
    m_timestamp: int
    print_after_upload: bool = False
    overwrite: bool = False

    @property
    def size(self) -> int:
        return len(self.data)

    def describe(self) -> dict:
        return {
            "path": self.path,
            "sfn_path": self.sfn_path,
            "size": self.size,
            "m_timestamp": self.m_timestamp,
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "print_after_upload": self.print_after_upload,
            "overwrite": self.overwrite,
        }


@dataclass
class _Dir:
    path: str
    sfn_path: str


class Storage:
    def __init__(self) -> None:
        self.files: dict[str, StoredFile] = {}
        self.dirs: dict[str, _Dir] = {_key(ROOT): _Dir(ROOT, ROOT)}

    def clear(self) -> None:
        self.__init__()

    def kind(self, path: str) -> Optional[str]:
        """"file", "dir" or None."""
        k = _key(path)
        if k in self.files:
            return "file"
        if k in self.dirs:
            return "dir"
        return None

    def get(self, path: str) -> Optional[StoredFile]:
        return self.files.get(_key(path))

    def _short_names_in(self, dir_path: str) -> set[str]:
        parent = _key(dir_path)
        names = {_base(f.sfn_path) for f in self.files.values() if _key(_parent(f.path)) == parent}
        names |= {_base(d.sfn_path) for k, d in self.dirs.items()
                  if k != _key(ROOT) and _key(_parent(d.path)) == parent}
        return names

    def mkdir(self, path: str) -> str:
        """Create one folder whose parent exists. Returns "ok", "exists" or "error"."""
        path = path.rstrip("/")
        if self.kind(path) is not None:
            return "exists"
        parent = _parent(path)
        if self.kind(parent) != "dir":
            return "error"
        sfn = short_name(_base(path), self._short_names_in(parent))
        self.dirs[_key(path)] = _Dir(path, f"{self.dirs[_key(parent)].sfn_path}/{sfn}")
        return "ok"

    def make_dirs(self, file_path: str) -> bool:
        """make_dirs (src/common/path_utils.cpp:35-51) for the parents of a file."""
        parts = file_path.split("/")
        current = ROOT
        for part in parts[2:-1]:
            if not part:
                continue
            current = f"{current}/{part}"
            kind = self.kind(current)
            if kind == "file":
                return False
            if kind is None and self.mkdir(current) != "ok":
                return False
        return True

    def store(self, path: str, data: bytes, m_timestamp: int, *, print_after_upload: bool,
              overwrite: bool) -> StoredFile:
        """Place a file whose parent folder exists, replacing any file of that name."""
        k = _key(path)
        self.files.pop(k, None)  # frees its short name for the replacement
        parent = _parent(path)
        sfn = short_name(_base(path), self._short_names_in(parent))
        entry = StoredFile(path, f"{self.dirs[_key(parent)].sfn_path}/{sfn}", data, m_timestamp,
                           print_after_upload, overwrite)
        self.files[k] = entry
        return entry

    def remove(self, path: str) -> bool:
        """Delete a file or an empty folder."""
        k = _key(path)
        if k in self.files:
            del self.files[k]
            return True
        if k in self.dirs and k != _key(ROOT):
            if self._short_names_in(path):
                return False
            del self.dirs[k]
            return True
        return False

    def find_short(self, path: str) -> Optional[StoredFile]:
        """A file by its short-name path (FAT accepts either name)."""
        target = path.rstrip("/").upper()
        for f in self.files.values():
            if f.sfn_path.upper() == target:
                return f
        return None

    def find_rel(self, rel: str) -> Optional[StoredFile]:
        """A file by its path relative to the drive root, long or short names."""
        path = f"{ROOT}/{rel.strip('/')}"
        return self.get(path) or self.find_short(path)

    def listing(self) -> list[dict]:
        return [f.describe() for f in self.files.values()]
