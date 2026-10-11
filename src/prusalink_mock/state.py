"""Scenario documents: loading, merging and validation against firmware limits.

A scenario is one flat JSON document. ``tools`` mirrors the /api/v1/tools body
field for field: a tool whose key is absent is a dock that is not calibrated
(the firmware omits it, tools_renderer.cpp:40-43), so a captured body can be
pasted in as is.
"""

from __future__ import annotations

import copy
import json
import math
import re
import struct
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

SCHEMA = "prusalink-mock/scenario@1"
DEFAULT_SCENARIO = "indx-hy8"


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is out of range")
    return value


def loads_json(text: str) -> Any:
    """json.loads without NaN, Infinity or numbers that overflow to infinity, which Python
    accepts but would then be rendered as invalid JSON. Nesting too deep to parse is a
    ValueError too."""
    try:
        return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)
    except RecursionError:
        raise ValueError("nested too deeply") from None


class ScenarioError(ValueError):
    """A scenario failed validation. ``errors`` lists every problem found."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


# include/common/printer_model_data.hpp:67-149, EXTRUDERS in the Marlin
# configurations, LAN_HOSTNAME_DEF in src/common/config.h:16-30.
MODELS: dict[str, dict[str, Any]] = {
    "COREONE": {"version": "7.1.0", "tools": 1, "hostname": "prusa-core-one"},
    "COREONEINDX": {"version": "7.10.0", "tools": 8, "hostname": "prusa-core-one"},
    "COREONEL": {"version": "8.1.0", "tools": 1, "hostname": "prusa-core-one-l"},
    "COREONELINDX": {"version": "8.10.0", "tools": 8, "hostname": "prusa-core-one-l"},
    "MK4": {"version": "1.4.0", "tools": 1, "hostname": "prusa-mk4"},
    "MK4S": {"version": "1.4.1", "tools": 1, "hostname": "prusa-mk4"},
    "MINI": {"version": "2.1.0", "tools": 1, "hostname": "prusa-mini"},
    "XL": {"version": "3.1.0", "tools": 5, "hostname": "prusa-xl"},
}

# src/state/printer_state.cpp:600-624 (READY never appears: get_state(false))
PRINTER_STATES = ("IDLE", "BUSY", "PRINTING", "PAUSED", "FINISHED", "STOPPED", "ATTENTION", "ERROR")
JOB_STATES = ("PRINTING", "PAUSED")

# src/common/filament_library.cpp:19-61
PRESET_VENDORS = (
    "Generic", "Prusament", "Polymaker", "Bambu Lab", "eSun", "Elegoo", "Sunlu", "Overture",
    "Hatchbox", "Inland", "3D-Fuel", "Fillamentum", "Fiberlogy", "Spectrum", "Extrudr", "colorFabb",
)
PRESET_COLORS = {
    "Red": "#FF0000", "Green": "#008000", "Blue": "#0000FF", "Cyan": "#00FFFF",
    "Magenta": "#FF00FF", "Yellow": "#FFFF00", "Black": "#000000", "White": "#FFFFFF",
    "Orange": "#F8651B", "Brown": "#8B4513", "Gray": "#808080", "Purple": "#800080",
    "Pink": "#FFC0CB", "Silver": "#C0C0C0", "Natural": "#F5F5DC", "Gold": "#FFD700",
}
# src/common/filament_presets.cpp
PRESET_MATERIALS = ("PLA", "PETG", "ASA", "PC", "PVB", "ABS", "HIPS", "PP", "FLEX", "PA", "PPS", "PPA", "PVA", "BVOH")

MATERIAL_MAX = 7  # filament_name_buffer_size 8 (src/common/filament.hpp:30)
LIBRARY_NAME_MAX = 15  # filament_library_name_buffer_size 16 (filament_library.hpp:22)
PASSWORD_MAX = 15  # buffer 16 (constants.hpp:23)
HOSTNAME_MAX = 20  # char[HOSTNAME_LEN + 1] (netif_settings.h:18, basic_gets.cpp:201, 229)
SERIAL_MAX = 24  # serial_nr_t is std::array<char, 25> (src/module/otp/include/otp/types.hpp:68)
DISPLAY_NAME_MAX = 166  # char[max_filename_length] (basic_gets.cpp:410-414, filename_defs.hpp:21)
SFN_PATH_MAX = 102  # char[max_path_length] (basic_gets.cpp:411, filename_defs.hpp:26)
HEX_RE = re.compile(r"#[0-9A-F]{6}")
# filament_common.cpp:37-42 (isalnum in the C locale is ASCII only)
MATERIAL_RE = re.compile(r"[A-Za-z0-9_-]+")
# is_valid_name_char, filament_library.cpp:105-107
LIBRARY_NAME_RE = re.compile(r"[A-Za-z0-9 _\-.+&]+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Ranges of the firmware's C types that hold the numbers.
INT16 = (-0x8000, 0x7FFF)
UINT16 = (0, 0xFFFF)
UINT32 = (0, 0xFFFFFFFF)
INT32 = (-0x80000000, 0x7FFFFFFF)
FLT_MAX = struct.unpack("<f", b"\xff\xff\x7f\x7f")[0]
# FAT dates have a 7-bit year from 1980 (filesystem_fatfs.cpp:24-30, 152-164).
FAT_TIME_MIN = 315532800  # 1980-01-01T00:00:00Z
FAT_TIME_MAX = 4354819199  # 2107-12-31T23:59:59Z

# Far deeper than any scenario; keeps merge patches and copies clear of the recursion limit.
MAX_DEPTH = 32

TOOL_FIELDS = ("loaded", "material", "vendor", "color", "color_hex", "nozzle_diameter", "hardened", "high_flow")


def _scenario_dir():
    return resources.files("prusalink_mock").joinpath("scenarios")


def list_scenarios() -> list[dict[str, str]]:
    """Shipped scenarios as ``{name, description}``, sorted by name."""
    out = []
    for entry in _scenario_dir().iterdir():
        if entry.name.endswith(".json"):
            doc = loads_json(entry.read_text(encoding="utf-8"))
            out.append({"name": doc.get("name", entry.name[:-5]), "description": doc.get("description", "")})
    return sorted(out, key=lambda d: d["name"])


def load_document(name_or_path: str) -> Any:
    """Raw scenario document by shipped name or by file path.

    Every way of failing to read it, an unreadable file or a name too long for
    the file system included, is a ScenarioError.
    """
    try:
        path = Path(name_or_path)
        if path.suffix == ".json" or path.exists():
            if not path.is_file():
                raise ScenarioError([f"scenario file not found: {name_or_path}"])
            try:
                return loads_json(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise ScenarioError([f"{name_or_path}: {exc}"])
        entry = _scenario_dir().joinpath(f"{name_or_path}.json")
        shipped = entry.is_file()
    except OSError as exc:
        raise ScenarioError([f"{name_or_path}: {exc.strerror or exc}"])
    if not shipped:
        known = ", ".join(s["name"] for s in list_scenarios())
        raise ScenarioError([f"unknown scenario {name_or_path!r} (shipped: {known})"])
    return loads_json(entry.read_text(encoding="utf-8"))


def load(name_or_path: str) -> dict[str, Any]:
    """Load and validate a scenario; returns the normalized state."""
    return normalize(load_document(name_or_path))


def check_depth(doc: Any, limit: int = MAX_DEPTH) -> None:
    """Raise ScenarioError if objects and arrays nest deeper than ``limit``.

    Depth first and without recursion, so even a document that contains
    itself is caught quickly.
    """
    stack = [(doc, 0)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, dict):
            children = list(value.values())
        elif isinstance(value, (list, tuple)):
            children = list(value)
        else:
            continue
        if depth >= limit:
            raise ScenarioError([f"/: nested deeper than {limit} levels"])
        stack.extend((child, depth + 1) for child in children)


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 merge patch, except that ``tools`` is replaced wholesale."""
    check_depth(patch)
    return _merge(target, patch)


def _merge(target: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif key == "tools":
            result[key] = copy.deepcopy(value)
        else:
            result[key] = _merge(result.get(key), value)
    return result


def public(state: dict[str, Any]) -> dict[str, Any]:
    """State as served by the control API: everything but the password."""
    out = copy.deepcopy(state)
    out.pop("auth", None)
    return out


# --- values that follow the model ----------------------------------------------
# printer_version, tool_count and hostname are stored as None unless a scenario
# or a patch sets them, so they keep following the model when it changes.

def printer_version(state: dict[str, Any]) -> str:
    """PrinterModelInfo::current() version string (basic_gets.cpp:204, 219)."""
    p = state["printer"]
    return p["printer_version"] if p["printer_version"] is not None else MODELS[p["model"]]["version"]


def tool_count(state: dict[str, Any]) -> int:
    p = state["printer"]
    return p["tool_count"] if p["tool_count"] is not None else MODELS[p["model"]]["tools"]


def hostname(state: dict[str, Any]) -> str:
    p = state["printer"]
    return p["hostname"] if p["hostname"] is not None else MODELS[p["model"]]["hostname"]


# --- validation -------------------------------------------------------------

def _range(lo: Any, hi: Any, why: str) -> str:
    return f"must be in {lo}..{hi}" + (f" ({why})" if why else "")


def key_text(key: Any) -> str:
    """A key as an error message shows it. JSON keys are strings, but a Python caller's
    may be anything, and Python refuses to turn an int of more than 4300 digits into text."""
    if isinstance(key, str):
        return key
    if isinstance(key, int) and not isinstance(key, bool) and key.bit_length() <= 64:
        return str(key)
    return f"<{type(key).__name__}>"


class _V:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def err(self, path: str, msg: str) -> None:
        self.errors.append(f"{path}: {msg}")

    def obj(self, value: Any, path: str, allowed: tuple[str, ...]) -> dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.err(path, "must be an object")
            return {}
        for key in value:
            if key not in allowed:
                self.err(f"{path}/{key_text(key)}", "unknown key")
        return value

    def boolean(self, d: dict, key: str, path: str, default: Optional[bool] = None) -> Optional[bool]:
        if key not in d:
            if default is None:
                self.err(f"{path}/{key}", "required")
            return default
        if not isinstance(d[key], bool):
            self.err(f"{path}/{key}", "must be true or false")
            return default
        return d[key]

    def integer(self, d: dict, key: str, path: str, default: Optional[int], lo: int, hi: int,
                required: bool = False, why: str = "") -> Optional[int]:
        """An integer of a firmware C type; ``why`` names the type for the error."""
        if key not in d or d[key] is None:
            if required:
                self.err(f"{path}/{key}", "required")
            return default
        v = d[key]
        if isinstance(v, bool) or not isinstance(v, int):
            self.err(f"{path}/{key}", "must be an integer")
            return default
        if not lo <= v <= hi:
            self.err(f"{path}/{key}", _range(lo, hi, why))
            return default
        return v

    def _finite(self, value: Any, where: str) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.err(where, "must be a finite number")
            return None
        try:
            f = float(value)
        except OverflowError:  # an int too large for a double
            self.err(where, "out of range")
            return None
        if not math.isfinite(f):
            self.err(where, "must be a finite number")
            return None
        return f

    def number(self, d: dict, key: str, path: str, default: Optional[float] = None,
               lo: Optional[float] = None, hi: Optional[float] = None) -> Optional[float]:
        """A number the firmware keeps in a 32-bit float."""
        if key not in d:
            if default is None:
                self.err(f"{path}/{key}", "required")
            return default
        where = f"{path}/{key}"
        v = self._finite(d[key], where)
        if v is None:
            return default
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            self.err(where, f"must be in {lo}..{hi}")
            return default
        try:
            struct.pack("<f", v)
        except OverflowError:
            self.err(where, f"out of the range of a 32-bit float (+-{FLT_MAX:.7g})")
            return default
        return v

    def whole(self, d: dict, key: str, path: str, default: float, lo: int, hi: int, why: str) -> float:
        """A number the firmware keeps in a C integer type, given as 60 or 60.0; kept as a float."""
        if key not in d:
            return default
        where = f"{path}/{key}"
        v = self._finite(d[key], where)
        if v is None:
            return default
        if not v.is_integer():
            self.err(where, f"must be a whole number ({why})")
            return default
        if not lo <= v <= hi:
            self.err(where, _range(lo, hi, why))
            return default
        return v

    def optional_string(self, d: dict, key: str, path: str, **rules: Any) -> Optional[str]:
        """A string, or None when absent or null (the value then follows the model)."""
        if d.get(key) is None:
            return None
        return self.string(d, key, path, **rules)

    def string(self, d: dict, key: str, path: str, default: Optional[str] = None,
               max_bytes: Optional[int] = None, controls: bool = False,
               check: Optional[Callable[[str], Optional[str]]] = None) -> Optional[str]:
        """Text the firmware could hold: valid Unicode, no NUL and, unless ``controls``, no
        other control character either. jsonify_str passes those through unescaped
        (json_encode.cpp:21-29), so the printer port would send invalid JSON, but no
        string the firmware renders can contain one. ``check`` returns an error
        message for a value outside the field's own character set."""
        if key not in d:
            if default is None:
                self.err(f"{path}/{key}", "required")
            return default
        v = d[key]
        where = f"{path}/{key}"
        if not isinstance(v, str):
            self.err(where, "must be a string")
            return default
        try:
            size = len(v.encode("utf-8"))
        except UnicodeEncodeError:
            self.err(where, "must be valid Unicode text (no lone surrogates)")
            return default
        if "\0" in v:
            self.err(where, "must not contain NUL")
        elif not controls and _CONTROL_RE.search(v):
            self.err(where, "must not contain control characters")
        elif max_bytes is not None and size > max_bytes:
            self.err(where, f"longer than the firmware's {max_bytes} bytes")
        elif check is not None and v:
            problem = check(v)
            if problem:
                self.err(where, problem)
        return v

    def pair(self, d: dict, key: str, path: str, names: tuple[str, ...], default: float = 0.0) -> dict:
        sub = self.obj(d.get(key, {}), f"{path}/{key}", names)
        return {n: self.number(sub, n, f"{path}/{key}", default) for n in names}


def _material_name(name: str) -> Optional[str]:
    """FilamentType::can_be_renamed_to (filament_common.cpp:22-42), which every filament
    type name passes (filament.cpp:226)."""
    if name == "---" or not MATERIAL_RE.fullmatch(name):
        return "a filament type is named with 'a-zA-Z0-9_-' only, and not '---' (filament_common.cpp:32-42)"
    return None


def _library_name(presets: Iterable[str]) -> Callable[[str], Optional[str]]:
    """FilamentLibraryItem::can_be_renamed_to (filament_library.cpp:158-183): what a user
    vendor or color can be called. A name that matches a preset in any case is that preset."""
    def check(name: str) -> Optional[str]:
        if name[0] == " " or name[-1] == " " or not LIBRARY_NAME_RE.fullmatch(name):
            return ("a library name is 'a-zA-Z0-9 _-.+&' with no space at either end "
                    "(filament_library.cpp:105-107, 172-174)")
        for preset in presets:
            if preset != name and preset.lower() == name.lower():
                return (f"is the preset {preset!r}, spelled that way; no user entry can take a "
                        f"preset's name (filament_library.cpp:176-180)")
        return None
    return check


def _printer_version(text: str) -> Optional[str]:
    """``%i.%i.%i`` of the three uint8_t of PrinterVersion (basic_gets.cpp:219, printer_model.hpp:50-56)."""
    parts = text.split(".")
    if len(parts) != 3 or not all(re.fullmatch(r"0|[1-9][0-9]{0,2}", p) and int(p) <= 255 for p in parts):
        return "must be three numbers 0..255 like 7.10.0 (%i.%i.%i of uint8_t, basic_gets.cpp:219)"
    return None


def _tool(v: _V, raw: Any, path: str) -> dict:
    t = v.obj(raw, path, TOOL_FIELDS)
    loaded = v.boolean(t, "loaded", path, None)
    material = v.string(t, "material", path, "", MATERIAL_MAX, check=_material_name)
    vendor = v.string(t, "vendor", path, "", LIBRARY_NAME_MAX, check=_library_name(PRESET_VENDORS))
    color = v.string(t, "color", path, "", LIBRARY_NAME_MAX, check=_library_name(PRESET_COLORS))
    if "color_hex" in t:
        color_hex = v.string(t, "color_hex", path, "")
    elif color in PRESET_COLORS:
        color_hex = PRESET_COLORS[color]
    elif color:
        v.err(f"{path}/color_hex", f"required for the user color {color!r} (not a firmware preset)")
        color_hex = ""
    else:
        color_hex = ""
    if color_hex and not HEX_RE.fullmatch(color_hex):
        v.err(f"{path}/color_hex", "must match #RRGGBB with uppercase hex digits, or be empty")
    elif color in PRESET_COLORS and color_hex and color_hex != PRESET_COLORS[color]:
        v.err(f"{path}/color_hex", f"the preset {color} is always {PRESET_COLORS[color]} (filament_library.cpp:44-62)")
    if color_hex and not color:
        v.err(f"{path}/color_hex", "must be empty when no color is picked")
    if color and not color_hex:
        v.err(f"{path}/color_hex", "a picked color always reports its #RRGGBB")
    if loaded is False and (material or vendor or color or color_hex):
        v.err(path, "an unloaded tool reports empty material, vendor, color and color_hex")
    if loaded and not material:
        v.err(f"{path}/material", "a loaded tool needs a material")
    return {
        "loaded": bool(loaded),
        "material": material,
        "vendor": vendor,
        "color": color,
        "color_hex": color_hex,
        "nozzle_diameter": v.number(t, "nozzle_diameter", path, None, 0.1, 2.0),
        "hardened": v.boolean(t, "hardened", path, None),
        "high_flow": v.boolean(t, "high_flow", path, None),
    }


def _check_library(v: _V, tools: dict[str, dict]) -> None:
    """Tools pick their vendor and color from one library (filament_library.cpp:122-183).

    A user entry has one name, unique in any case, and a user color one RGB value, so
    tools that name the same entry agree on both. (The library's 8 user vendors and
    8 user colors are enough for 8 tools.)
    """
    for field, presets in (("vendor", PRESET_VENDORS), ("color", PRESET_COLORS)):
        seen: dict[str, tuple[str, str, str]] = {}  # lower-case name: (tool, name, hex)
        for key, tool in tools.items():
            name = tool[field]
            if not name or name in presets:
                continue
            first = seen.setdefault(name.lower(), (key, name, tool["color_hex"]))
            if first[1] != name:
                v.err(f"/tools/{key}/{field}", f"tool {first[0]} names the same user entry {first[1]!r}; "
                                               "an entry has one name")
            elif field == "color" and first[2] != tool["color_hex"]:
                v.err(f"/tools/{key}/color_hex", f"tool {first[0]} has the user color {name!r} as {first[2]}; "
                                                 "an entry has one color")


def password_errors(password: Any) -> list[str]:
    """What is wrong with a password, by the rules of /auth/password."""
    v = _V()
    v.string({"password": password}, "password", "", max_bytes=PASSWORD_MAX, controls=True)
    return v.errors


def normalize(doc: Any) -> dict[str, Any]:
    """Validate a scenario document and fill defaults; raises ScenarioError."""
    if not isinstance(doc, dict):
        raise ScenarioError(["/: a scenario must be a JSON object"])
    check_depth(doc)
    v = _V()
    d = v.obj(doc, "", ("schema", "name", "description", "firmware", "printer", "auth", "storage", "tools", "job"))
    if d.get("schema", SCHEMA) != SCHEMA:
        v.err("/schema", f"must be {SCHEMA!r}")
    out: dict[str, Any] = {
        "schema": SCHEMA,
        "name": v.string(d, "name", "", "custom"),
        "description": v.string(d, "description", "", ""),
    }

    fw = v.obj(d.get("firmware"), "/firmware", ("version", "has_filament_slots"))
    out["firmware"] = {
        "version": v.string(fw, "version", "/firmware", "6.9.2+0"),
        "has_filament_slots": v.boolean(fw, "has_filament_slots", "/firmware", False),
    }

    p = v.obj(d.get("printer"), "/printer", (
        "model", "printer_version", "tool_count", "hostname", "serial", "nozzle_diameter", "mmu",
        "min_extrusion_temp", "state", "active_tool", "job_id", "bed", "nozzle", "axis", "flow", "speed", "fans",
    ))
    model = v.string(p, "model", "/printer", "COREONEINDX")
    info = MODELS.get(model)
    if info is None:
        v.err("/printer/model", f"unknown model (known: {', '.join(MODELS)})")
        model, info = "COREONEINDX", MODELS["COREONEINDX"]
    tool_count_set = v.integer(p, "tool_count", "/printer", None, 1, 8)
    tool_count = tool_count_set if tool_count_set is not None else info["tools"]

    tools_raw = d.get("tools", {})
    tools: dict[str, dict] = {}
    if not isinstance(tools_raw, dict):
        v.err("/tools", "must be an object")
        tools_raw = {}
    for key, raw in tools_raw.items():
        # One digit: tool_count is at most 8, and int() of a long string is slow or refused.
        if not (isinstance(key, str) and len(key) == 1 and "1" <= key <= "8" and int(key) <= tool_count):
            v.err(f"/tools/{key_text(key)}", f"tool keys are the strings \"1\"..\"{tool_count}\" on this printer "
                                   f"(/printer/tool_count, {info['tools']} on the {model})")
            continue
        tools[key] = _tool(v, raw, f"/tools/{key}")
    out["tools"] = {k: tools[k] for k in sorted(tools, key=int)}
    _check_library(v, out["tools"])

    # /api/version and /api/v1/info report the nozzle of physical tool 0
    # (basic_gets.cpp:203, 231), the same config store entry as tool 1 in
    # /api/v1/tools (tools_renderer.cpp:22). While tool 1 is listed it wins; the
    # stored value stays when dock 1 is uncalibrated, as in the config store.
    nozzle_diameter = v.number(p, "nozzle_diameter", "/printer", 0.4, 0.1, 2.0)
    if "1" in tools and tools["1"]["nozzle_diameter"] is not None:
        nozzle_diameter = tools["1"]["nozzle_diameter"]
    state_name = v.string(p, "state", "/printer", "IDLE")
    if state_name not in PRINTER_STATES:
        v.err("/printer/state", f"must be one of {', '.join(PRINTER_STATES)}")
    active_default = 1 if tool_count == 1 else 0
    active = v.integer(p, "active_tool", "/printer", active_default, 0, tool_count)
    if tool_count == 1 and active != 1:
        v.err("/printer/active_tool", "a single-tool printer always has tool 1 active")
    elif tool_count > 1 and active and str(active) not in tools:
        v.err("/printer/active_tool", "must be 0 (no tool picked) or a tool listed in /tools")
    # marlin_vars.hpp:313-320, 354-355, 376-393, 416-417
    bed = v.obj(p.get("bed", {}), "/printer/bed", ("actual", "target"))
    nozzle = v.obj(p.get("nozzle", {}), "/printer/nozzle", ("actual", "target"))
    out["printer"] = {
        "model": model,
        "printer_version": v.optional_string(p, "printer_version", "/printer", check=_printer_version),
        "tool_count": tool_count_set,
        "hostname": v.optional_string(p, "hostname", "/printer", max_bytes=HOSTNAME_MAX),
        "serial": v.string(p, "serial", "/printer", "CZPX0000X000XC00000", SERIAL_MAX),
        "nozzle_diameter": nozzle_diameter,
        "mmu": v.boolean(p, "mmu", "/printer", False),
        "min_extrusion_temp": v.integer(p, "min_extrusion_temp", "/printer", 170, 0, 1000),
        "state": state_name,
        "active_tool": active,
        # marlin_vars job_id: the current job's id, kept after the job ends
        # (marlin_server.cpp:1396, 2313-2315, 3730)
        "job_id": v.integer(p, "job_id", "/printer", 0, *UINT16, why="uint16_t job_id"),
        "bed": {
            "actual": v.number(bed, "actual", "/printer/bed", 0.0),
            "target": v.whole(bed, "target", "/printer/bed", 0.0, *INT16, why="int16_t target_bed"),
        },
        "nozzle": {
            "actual": v.number(nozzle, "actual", "/printer/nozzle", 0.0),
            "target": v.whole(nozzle, "target", "/printer/nozzle", 0.0, *INT16, why="int16_t target_nozzle"),
        },
        "axis": v.pair(p, "axis", "/printer", ("x", "y", "z")),
        "flow": v.integer(p, "flow", "/printer", 100, *UINT16, why="uint16_t flow_factor"),
        "speed": v.integer(p, "speed", "/printer", 100, *UINT16, why="uint16_t print_speed"),
        "fans": {},
    }
    fans = v.obj(p.get("fans", {}), "/printer/fans", ("hotend", "print"))
    out["printer"]["fans"] = {
        "hotend": v.integer(fans, "hotend", "/printer/fans", 0, *UINT16, why="uint16_t heatbreak_fan_rpm"),
        "print": v.integer(fans, "print", "/printer/fans", 0, *UINT16, why="uint16_t print_fan_rpm"),
    }

    a = v.obj(d.get("auth"), "/auth", ("password",))
    out["auth"] = {"password": v.string(a, "password", "/auth", "mockKeyTest2345", PASSWORD_MAX, controls=True)}

    s = v.obj(d.get("storage"), "/storage", ("available",))
    out["storage"] = {"available": v.boolean(s, "available", "/storage", True)}

    job_raw = d.get("job")
    if job_raw is not None:
        j = v.obj(job_raw, "/job", ("id", "progress", "time_printing", "time_remaining", "filament_change_in", "file"))
        if "id" in j:
            v.err("/job/id", "the job's id is /printer/job_id, which outlives the job")
        f = v.obj(j.get("file"), "/job/file", ("sfn_path", "display_name", "size", "m_timestamp"))
        if "file" not in j:
            v.err("/job/file", "required")
        sfn_path = v.string(f, "sfn_path", "/job/file", "/usb/JOB.GCO", SFN_PATH_MAX)
        if not sfn_path.startswith("/usb/"):
            v.err("/job/file/sfn_path", "must start with /usb/")
        # time_to_end and time_to_pause equal to TIME_TO_END_INVALID are left out
        # (marlin_server_shared.h:9, status_renderer.cpp:40-45); here that is null.
        unknown_time = "uint32_t; 4294967295 is TIME_TO_END_INVALID, leave it out instead"
        job = {
            "progress": v.integer(j, "progress", "/job", 0, 0, 100, required=True),
            "time_printing": v.integer(j, "time_printing", "/job", 0, *UINT32, required=True,
                                       why="uint32_t print_duration, marlin_vars.hpp:323"),
            "time_remaining": v.integer(j, "time_remaining", "/job", None, 0, UINT32[1] - 1, why=unknown_time),
            "filament_change_in": v.integer(j, "filament_change_in", "/job", None, 0, UINT32[1] - 1,
                                            why=unknown_time),
            "file": {
                "sfn_path": sfn_path,
                "display_name": v.string(f, "display_name", "/job/file", "job.gcode", DISPLAY_NAME_MAX),
                # stat() of a FAT file: st_size is a 32-bit off_t, st_mtime a FAT date
                # (filesystem_fatfs.cpp:395-439, basic_gets.cpp:418-454)
                "size": v.integer(f, "size", "/job/file", None, 0, INT32[1], why="32-bit off_t st_size"),
                "m_timestamp": v.integer(f, "m_timestamp", "/job/file", None, FAT_TIME_MIN, FAT_TIME_MAX,
                                         why="a FAT date, 1980 to 2107"),
            },
        }
        if (job["file"]["size"] is None) != (job["file"]["m_timestamp"] is None):
            v.err("/job/file", "give both size and m_timestamp, or neither")
        out["job"] = job
    if state_name in JOB_STATES and "job" not in out:
        v.err("/job", f"required while the printer is {state_name}")
    if "job" in out and state_name not in JOB_STATES + ("ATTENTION",):
        v.err("/job", f"the firmware reports no job while {state_name}; remove it")

    if v.errors:
        raise ScenarioError(v.errors)
    return out
