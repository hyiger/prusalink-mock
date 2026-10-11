"""In-memory USB drive: uploaded files and folders with FAT short names.

Lookups are case-insensitive, as on FAT. Every file and folder gets a short
name in its parent folder when it is created (sfn.short_name), so the paths
the firmware reports (``/usb/AB~1.GCO``) can be reproduced. As FatFS matches
each path component against both names (dir_find), every lookup accepts a
short name in place of a long one.
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

    def _alias(self, parent: str, name: str) -> Optional[str]:
        """Long-name path of the file or folder in ``parent`` whose short name is ``name``."""
        parent_key, upper = _key(parent), name.upper()
        for f in self.files.values():
            if _key(_parent(f.path)) == parent_key and _base(f.sfn_path).upper() == upper:
                return f.path
        for k, d in self.dirs.items():
            if k != _key(ROOT) and _key(_parent(d.path)) == parent_key and _base(d.sfn_path).upper() == upper:
                return d.path
        return None

    def resolve(self, path: str) -> str:
        """``path`` with short-name components replaced by the long names they stand for.

        Components that name nothing are kept as given, so the result also serves for
        creating them.
        """
        current = ROOT
        for part in path.rstrip("/").split("/")[2:]:
            if not part:
                continue
            candidate = f"{current}/{part}"
            k = _key(candidate)
            if k in self.files or k in self.dirs:
                current = self.files[k].path if k in self.files else self.dirs[k].path
            else:
                current = self._alias(current, part) or candidate
        return current

    def kind(self, path: str) -> Optional[str]:
        """"file", "dir" or None."""
        k = _key(self.resolve(path))
        if k in self.files:
            return "file"
        if k in self.dirs:
            return "dir"
        return None

    def get(self, path: str) -> Optional[StoredFile]:
        return self.files.get(_key(self.resolve(path)))

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
        parent = self.resolve(_parent(path))
        if self.kind(parent) != "dir":
            return "error"
        path = f"{parent}/{_base(path)}"
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
            current = self.resolve(f"{current}/{part}")
            kind = self.kind(current)
            if kind == "file":
                return False
            if kind is None and self.mkdir(current) != "ok":
                return False
        return True

    def store(self, path: str, data: bytes, m_timestamp: int, *, print_after_upload: bool,
              overwrite: bool) -> StoredFile:
        """Place a file whose parent folder exists, replacing any file of that name.

        A file replaced through its short name gives way to one named as given, as on FAT.
        """
        self.files.pop(_key(self.resolve(path)), None)  # frees its short name for the replacement
        parent = self.resolve(_parent(path))
        path = f"{parent}/{_base(path)}"
        k = _key(path)
        sfn = short_name(_base(path), self._short_names_in(parent))
        entry = StoredFile(path, f"{self.dirs[_key(parent)].sfn_path}/{sfn}", data, m_timestamp,
                           print_after_upload, overwrite)
        self.files[k] = entry
        return entry

    def remove(self, path: str) -> bool:
        """Delete a file or an empty folder."""
        path = self.resolve(path)
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
