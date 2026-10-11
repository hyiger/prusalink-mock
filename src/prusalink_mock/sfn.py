"""FAT 8.3 short names, generated the way FatFS does it.

The firmware reports files by their short name (``name``, ``refs``) and by
their long name (``display_name``). FatFS builds the short name in
``create_name`` and adds a numeric tail in ``gen_numname`` (ff.c); this module
follows both. Non-ASCII characters are not converted to an OEM code page:
they become ``_`` and make the name lossy.
"""

from __future__ import annotations

from typing import Iterable

_SFN_ILLEGAL = set('+,;=[]')


def _base(lfn: str) -> tuple[list[str], bool]:
    """11-character SFN field (8 body + 3 extension) and the "lossy" flag."""
    name = lfn.rstrip(" .")
    si = 0
    while si < len(name) and name[si] == " ":
        si += 1
    lossy = si > 0 or (si < len(name) and name[si] == ".")
    di = len(name)
    while di > 0 and name[di - 1] != ".":
        di -= 1
    fn = [" "] * 11
    i, ni = 0, 8
    while si < len(name):
        wc = name[si]
        si += 1
        if wc == " " or (wc == "." and si != di):
            lossy = True
            continue
        if i >= ni or si == di:
            if ni == 11:
                lossy = True
                break
            if si != di:
                lossy = True
            if si > di:
                break
            si, i, ni = di, 8, 11
            continue
        if ord(wc) >= 0x80 or wc in _SFN_ILLEGAL:
            wc = "_"
            lossy = True
        else:
            wc = wc.upper()
        fn[i] = wc
        i += 1
    return fn, lossy


def _numbered(fn: list[str], lfn: str, seq: int) -> list[str]:
    """``gen_numname``: put ``~N`` (or a hash after 5 collisions) into the body."""
    dst = list(fn)
    if seq > 5:
        sreg = seq
        data = lfn.encode("utf-16-le")
        for k in range(0, len(data), 2):
            wc = data[k] | (data[k + 1] << 8)
            for _ in range(16):
                sreg = (sreg << 1) + (wc & 1)
                wc >>= 1
                if sreg & 0x10000:
                    sreg ^= 0x11021
        seq = sreg
    ns = [""] * 8
    i = 7
    while True:
        c = seq % 16
        seq //= 16
        ns[i] = chr(c + 48) if c < 10 else chr(c + 55)
        i -= 1
        if not (i and seq):
            break
    ns[i] = "~"
    j = 0
    while j < i and dst[j] != " ":
        j += 1
    while True:
        if i < 8:
            dst[j] = ns[i]
            i += 1
        else:
            dst[j] = " "
        j += 1
        if j >= 8:
            break
    return dst


def _join(fn: list[str]) -> str:
    body = "".join(fn[:8]).rstrip()
    ext = "".join(fn[8:]).rstrip()
    return f"{body}.{ext}" if ext else body


def short_name(lfn: str, existing: Iterable[str]) -> str:
    """Short name for ``lfn`` in a directory whose short names are ``existing``."""
    taken = {name.upper() for name in existing}
    fn, lossy = _base(lfn)
    if not lossy:
        return _join(fn)
    for seq in range(1, 100):
        candidate = _join(_numbered(fn, lfn, seq))
        if candidate.upper() not in taken:
            return candidate
    raise FileExistsError("too many short name collisions")
