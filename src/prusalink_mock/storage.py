"""In-memory USB drive: uploaded files and folders with FAT short names.

Paths are looked up the way FatFS follows them (follow_path, create_name in
ff.c): each component loses its trailing dots and spaces, "." and ".." are dot
entries, case is folded one character at a time, and a short name stands for
its file or folder (dir_find). Every file and folder gets a short name in its
parent folder when it is created (sfn.short_name), so the paths the firmware
reports (``/usb/AB~1.GCO``) can be reproduced.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from .sfn import NoShortName, short_name

ROOT = "/usb"


def _upper(ch: str) -> str:
    """ff_wtoupper as cmp_lfn applies it (ff.c:1856-1882): one UTF-16 unit to one, so a
    character whose capital is two letters (ß, the fi ligature) stays as it is.
    Python's own mapping stands in for FatFS's table."""
    up = ch.upper()
    return up if len(up) == 1 and ord(ch) < 0x10000 else ch


def _fold(text: str) -> str:
    return "".join(_upper(ch) for ch in text)


def _key(path: str) -> str:
    return _fold(path.rstrip("/")) or ROOT.upper()


def _parent(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[0]


def _base(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[1]


def fat_name(component: str) -> Optional[str]:
    """create_name (ff.c:2806-2860) for one path component.

    "." and ".." are dot entries (FF_FS_RPATH 2, ffconf.h:144). Any other name
    loses its trailing dots and spaces, so "dir." and "dir " are "dir"; None
    when nothing is left ("...", " "), which FatFS refuses with FR_INVALID_NAME.
    """
    if component in (".", ".."):
        return component
    return component.rstrip(" .") or None


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
        parent_key, folded = _key(parent), _fold(name)
        for f in self.files.values():
            if _key(_parent(f.path)) == parent_key and _fold(_base(f.sfn_path)) == folded:
                return f.path
        for k, d in self.dirs.items():
            if k != _key(ROOT) and _key(_parent(d.path)) == parent_key and _fold(_base(d.sfn_path)) == folded:
                return d.path
        return None

    def _kind_of(self, resolved: str) -> Optional[str]:
        k = _key(resolved)
        if k in self.files:
            return "file"
        if k in self.dirs:
            return "dir"
        return None

    def resolve(self, path: str) -> Optional[str]:
        """The long-name path ``path`` stands for, as follow_path walks it (ff.c:3009-3077).

        Short names stand for their files and folders, "." for the folder it is
        in and ".." for that folder's parent; the root has no dot entries, so
        both stay there. Components that name nothing are kept, without their
        trailing dots and spaces, so the result also serves for creating them.
        None when FatFS can't follow the path: a name it refuses, or a dot entry
        after something that isn't a folder.
        """
        current, kind = ROOT, "dir"
        for part in path.rstrip("/").split("/")[2:]:
            if not part:
                continue
            name = fat_name(part)
            if name is None:
                return None
            if name in (".", ".."):
                if kind != "dir":
                    return None  # FR_NO_PATH
                if name == ".." and current != ROOT:
                    current = _parent(current)
                continue
            candidate = f"{current}/{name}"
            kind = self._kind_of(candidate) if kind == "dir" else None
            if kind == "file":
                current = self.files[_key(candidate)].path
            elif kind == "dir":
                current = self.dirs[_key(candidate)].path
            else:
                alias = self._alias(current, name) if self._kind_of(current) == "dir" else None
                current = alias or candidate
                kind = self._kind_of(current)
        return current

    def kind(self, path: str) -> Optional[str]:
        """"file", "dir" or None."""
        resolved = self.resolve(path)
        return None if resolved is None else self._kind_of(resolved)

    def get(self, path: str) -> Optional[StoredFile]:
        resolved = self.resolve(path)
        return None if resolved is None else self.files.get(_key(resolved))

    def _short_names_in(self, dir_path: str) -> set[str]:
        parent = _key(dir_path)
        names = {_base(f.sfn_path) for f in self.files.values() if _key(_parent(f.path)) == parent}
        names |= {_base(d.sfn_path) for k, d in self.dirs.items()
                  if k != _key(ROOT) and _key(_parent(d.path)) == parent}
        return names

    def short_name_free(self, path: str) -> bool:
        """Whether a new file at ``path`` would get a short name in its folder."""
        resolved = self.resolve(path)
        if resolved is None or self._kind_of(_parent(resolved)) != "dir":
            return True  # it fails for another reason first
        try:
            short_name(_base(resolved), self._short_names_in(_parent(resolved)))
        except NoShortName:
            return False
        return True

    def mkdir(self, path: str) -> str:
        """f_mkdir (ff.c:4992-5075). Returns "ok", "exists" or "error": no parent folder,
        a name FatFS refuses, or every numbered short name taken."""
        resolved = self.resolve(path)
        if resolved is None:
            return "error"
        if self._kind_of(resolved) is not None:
            return "exists"
        parent = _parent(resolved)
        if self._kind_of(parent) != "dir":
            return "error"
        try:
            sfn = short_name(_base(resolved), self._short_names_in(parent))
        except NoShortName:
            return "error"
        self.dirs[_key(resolved)] = _Dir(resolved, f"{self.dirs[_key(parent)].sfn_path}/{sfn}")
        return "ok"

    def make_dirs(self, file_path: str) -> bool:
        """make_dirs (src/common/path_utils.cpp:35-51) for the parents of a file:
        mkdir of each folder in turn, where "exists" is fine."""
        parts = file_path.split("/")
        current = ROOT
        for part in parts[2:-1]:
            if not part:
                continue
            folder = self.resolve(f"{current}/{part}")
            if folder is None:
                return False
            kind = self._kind_of(folder)
            if kind == "file":
                return False
            if kind is None and self.mkdir(folder) != "ok":
                return False
            current = folder
        return True

    def store(self, path: str, data: bytes, m_timestamp: int, *, print_after_upload: bool,
              overwrite: bool) -> Optional[StoredFile]:
        """Place a file whose parent folder exists, replacing any file of that name.

        A file replaced through its short name gives way to one named as given, as on FAT.
        None when every numbered short name is taken; as on the printer, which
        removes the old file before the rename fails (gcode_upload.cpp:203-237),
        a file it was to replace is gone.
        """
        resolved = self.resolve(path)
        assert resolved is not None, path
        self.files.pop(_key(resolved), None)  # frees its short name for the replacement
        parent = _parent(resolved)
        name = fat_name(_base(path)) or _base(path)
        try:
            sfn = short_name(name, self._short_names_in(parent))
        except NoShortName:
            return None
        entry = StoredFile(f"{parent}/{name}", f"{self.dirs[_key(parent)].sfn_path}/{sfn}", data, m_timestamp,
                           print_after_upload, overwrite)
        self.files[_key(entry.path)] = entry
        return entry

    def remove(self, path: str) -> bool:
        """Delete a file or an empty folder."""
        resolved = self.resolve(path)
        if resolved is None:
            return False
        k = _key(resolved)
        if k in self.files:
            del self.files[k]
            return True
        if k in self.dirs and k != _key(ROOT):
            if self._short_names_in(resolved):
                return False
            del self.dirs[k]
            return True
        return False

    def find_short(self, path: str) -> Optional[StoredFile]:
        """A file by its short-name path (FAT accepts either name)."""
        target = _fold(path.rstrip("/"))
        for f in self.files.values():
            if _fold(f.sfn_path) == target:
                return f
        return None

    def find_rel(self, rel: str) -> Optional[StoredFile]:
        """A file by its path relative to the drive root, long or short names."""
        path = f"{ROOT}/{rel.strip('/')}"
        return self.get(path) or self.find_short(path)

    def listing(self) -> list[dict]:
        return [f.describe() for f in self.files.values()]
