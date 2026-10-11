"""Scenario documents: loading, merging and validation against firmware limits.

A scenario is one flat JSON document. ``tools`` mirrors the /api/v1/tools body
field for field: a tool whose key is absent is a dock that is not calibrated
(the firmware omits it, tools_renderer.cpp:40-43), so a captured body can be
pasted in as is.
"""

from __future__ import annotations

import copy
import json
import re
from importlib import resources
from pathlib import Path
from typing import Any, Optional

SCHEMA = "prusalink-mock/scenario@1"
DEFAULT_SCENARIO = "indx-hy8"


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

# src/common/filament_library.cpp:19-60
PRESET_VENDORS = (
    "Generic", "Prusament", "Polymaker", "Bambu Lab", "eSun", "Elegoo", "Sunlu", "Overture",
    "Hatchbox", "Inland", "3D-Fuel", "Fillamentum", "Fiberlogy", "Spectrum", "Extrudr", "colorFabb",
)
PRESET_COLORS = {
    "Red": "#FF0000", "Green": "#008000", "Blue": "#0000FF", "Cyan": "#00FFFF",
    "Magenta": "#FF00FF", "Yellow": "#FFFF00", "Black": "#000000", "White": "#FFFFFF",
    "Orange": "#FFA500", "Brown": "#8B4513", "Gray": "#808080", "Purple": "#800080",
    "Pink": "#FFC0CB", "Silver": "#C0C0C0", "Natural": "#F5F5DC", "Gold": "#FFD700",
}
# src/common/filament_presets.cpp
PRESET_MATERIALS = ("PLA", "PETG", "ASA", "PC", "PVB", "ABS", "HIPS", "PP", "FLEX", "PA", "PPS", "PPA", "PVA", "BVOH")

MATERIAL_MAX = 7  # filament_name_buffer_size 8 (src/common/filament.hpp:30)
LIBRARY_NAME_MAX = 15  # filament_library_name_buffer_size 16 (filament_library.hpp:22)
PASSWORD_MAX = 15  # buffer 16 (constants.hpp:23)
HEX_RE = re.compile(r"^#[0-9A-F]{6}$")

TOOL_FIELDS = ("loaded", "material", "vendor", "color", "color_hex", "nozzle_diameter", "hardened", "high_flow")


def _scenario_dir():
    return resources.files("prusalink_mock").joinpath("scenarios")


def list_scenarios() -> list[dict[str, str]]:
    """Shipped scenarios as ``{name, description}``, sorted by name."""
    out = []
    for entry in _scenario_dir().iterdir():
        if entry.name.endswith(".json"):
            doc = json.loads(entry.read_text(encoding="utf-8"))
            out.append({"name": doc.get("name", entry.name[:-5]), "description": doc.get("description", "")})
    return sorted(out, key=lambda d: d["name"])


def load_document(name_or_path: str) -> dict[str, Any]:
    """Raw scenario document by shipped name or by file path."""
    path = Path(name_or_path)
    if path.suffix == ".json" or path.exists():
        if not path.is_file():
            raise ScenarioError([f"scenario file not found: {name_or_path}"])
        return json.loads(path.read_text(encoding="utf-8"))
    entry = _scenario_dir().joinpath(f"{name_or_path}.json")
    if not entry.is_file():
        known = ", ".join(s["name"] for s in list_scenarios())
        raise ScenarioError([f"unknown scenario {name_or_path!r} (shipped: {known})"])
    return json.loads(entry.read_text(encoding="utf-8"))


def load(name_or_path: str) -> dict[str, Any]:
    """Load and validate a scenario; returns the normalized state."""
    return normalize(load_document(name_or_path))


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 merge patch, except that ``tools`` is replaced wholesale."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif key == "tools":
            result[key] = copy.deepcopy(value)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def public(state: dict[str, Any]) -> dict[str, Any]:
    """State as served by the control API: everything but the password."""
    out = copy.deepcopy(state)
    out.pop("auth", None)
    return out


# --- validation -------------------------------------------------------------

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
                self.err(f"{path}/{key}", "unknown key")
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

    def integer(self, d: dict, key: str, path: str, default: Optional[int] = None,
                lo: Optional[int] = None, hi: Optional[int] = None, required: bool = False) -> Optional[int]:
        if key not in d or d[key] is None:
            if required:
                self.err(f"{path}/{key}", "required")
            return default
        v = d[key]
        if isinstance(v, bool) or not isinstance(v, int):
            self.err(f"{path}/{key}", "must be an integer")
            return default
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            self.err(f"{path}/{key}", f"must be in {lo}..{hi}")
        return v

    def number(self, d: dict, key: str, path: str, default: Optional[float] = None,
               lo: Optional[float] = None, hi: Optional[float] = None) -> Optional[float]:
        if key not in d:
            if default is None:
                self.err(f"{path}/{key}", "required")
            return default
        v = d[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            self.err(f"{path}/{key}", "must be a number")
            return default
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            self.err(f"{path}/{key}", f"must be in {lo}..{hi}")
        return float(v)

    def string(self, d: dict, key: str, path: str, default: Optional[str] = None,
               max_bytes: Optional[int] = None) -> Optional[str]:
        if key not in d:
            if default is None:
                self.err(f"{path}/{key}", "required")
            return default
        v = d[key]
        if not isinstance(v, str):
            self.err(f"{path}/{key}", "must be a string")
            return default
        if "\0" in v:
            self.err(f"{path}/{key}", "must not contain NUL")
        if max_bytes is not None and len(v.encode("utf-8")) > max_bytes:
            self.err(f"{path}/{key}", f"longer than the firmware's {max_bytes} bytes")
        return v

    def pair(self, d: dict, key: str, path: str, names: tuple[str, ...], default: float = 0.0) -> dict:
        sub = self.obj(d.get(key, {}), f"{path}/{key}", names)
        return {n: self.number(sub, n, f"{path}/{key}", default) for n in names}


def _tool(v: _V, raw: Any, path: str) -> dict:
    t = v.obj(raw, path, TOOL_FIELDS)
    loaded = v.boolean(t, "loaded", path, None)
    material = v.string(t, "material", path, "", MATERIAL_MAX)
    vendor = v.string(t, "vendor", path, "", LIBRARY_NAME_MAX)
    color = v.string(t, "color", path, "", LIBRARY_NAME_MAX)
    if "color_hex" in t:
        color_hex = v.string(t, "color_hex", path, "")
    elif color in PRESET_COLORS:
        color_hex = PRESET_COLORS[color]
    elif color:
        v.err(f"{path}/color_hex", f"required for the user color {color!r} (not a firmware preset)")
        color_hex = ""
    else:
        color_hex = ""
    if color_hex and not HEX_RE.match(color_hex):
        v.err(f"{path}/color_hex", "must match #RRGGBB with uppercase hex digits, or be empty")
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


def normalize(doc: Any) -> dict[str, Any]:
    """Validate a scenario document and fill defaults; raises ScenarioError."""
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
        "min_extrusion_temp", "state", "active_tool", "bed", "nozzle", "axis", "flow", "speed", "fans",
    ))
    model = v.string(p, "model", "/printer", "COREONEINDX")
    info = MODELS.get(model)
    if info is None:
        v.err("/printer/model", f"unknown model (known: {', '.join(MODELS)})")
        info = MODELS["COREONEINDX"]
    tool_count = v.integer(p, "tool_count", "/printer", info["tools"], 1, 8)

    tools_raw = d.get("tools", {})
    tools: dict[str, dict] = {}
    if not isinstance(tools_raw, dict):
        v.err("/tools", "must be an object")
        tools_raw = {}
    for key, raw in tools_raw.items():
        if not re.fullmatch(r"[1-9][0-9]*", key) or int(key) > tool_count:
            v.err(f"/tools/{key}", f"tool keys are \"1\"..\"{tool_count}\" on this printer")
            continue
        tools[key] = _tool(v, raw, f"/tools/{key}")
    out["tools"] = {k: tools[k] for k in sorted(tools, key=int)}

    default_nozzle = tools["1"]["nozzle_diameter"] if "1" in tools and tools["1"]["nozzle_diameter"] else 0.4
    state_name = v.string(p, "state", "/printer", "IDLE")
    if state_name not in PRINTER_STATES:
        v.err("/printer/state", f"must be one of {', '.join(PRINTER_STATES)}")
    active_default = 1 if tool_count == 1 else 0
    active = v.integer(p, "active_tool", "/printer", active_default, 0, tool_count)
    if tool_count == 1 and active != 1:
        v.err("/printer/active_tool", "a single-tool printer always has tool 1 active")
    elif tool_count > 1 and active and str(active) not in tools:
        v.err("/printer/active_tool", "must be 0 (no tool picked) or a tool listed in /tools")
    out["printer"] = {
        "model": model,
        "printer_version": v.string(p, "printer_version", "/printer", info["version"]),
        "tool_count": tool_count,
        "hostname": v.string(p, "hostname", "/printer", info["hostname"]),
        "serial": v.string(p, "serial", "/printer", "CZPX0000X000XC00000"),
        "nozzle_diameter": v.number(p, "nozzle_diameter", "/printer", default_nozzle, 0.1, 2.0),
        "mmu": v.boolean(p, "mmu", "/printer", False),
        "min_extrusion_temp": v.integer(p, "min_extrusion_temp", "/printer", 170, 0, 1000),
        "state": state_name,
        "active_tool": active,
        "bed": v.pair(p, "bed", "/printer", ("actual", "target")),
        "nozzle": v.pair(p, "nozzle", "/printer", ("actual", "target")),
        "axis": v.pair(p, "axis", "/printer", ("x", "y", "z")),
        "flow": v.integer(p, "flow", "/printer", 100, 0, 65535),
        "speed": v.integer(p, "speed", "/printer", 100, 0, 65535),
        "fans": {},
    }
    fans = v.obj(p.get("fans", {}), "/printer/fans", ("hotend", "print"))
    out["printer"]["fans"] = {
        "hotend": v.integer(fans, "hotend", "/printer/fans", 0, 0, 65535),
        "print": v.integer(fans, "print", "/printer/fans", 0, 0, 65535),
    }

    a = v.obj(d.get("auth"), "/auth", ("password",))
    out["auth"] = {"password": v.string(a, "password", "/auth", "mockKeyTest2345", PASSWORD_MAX)}

    s = v.obj(d.get("storage"), "/storage", ("available",))
    out["storage"] = {"available": v.boolean(s, "available", "/storage", True)}

    job_raw = d.get("job")
    if job_raw is not None:
        j = v.obj(job_raw, "/job", ("id", "progress", "time_printing", "time_remaining", "filament_change_in", "file"))
        f = v.obj(j.get("file"), "/job/file", ("sfn_path", "display_name", "size", "m_timestamp"))
        if "file" not in j:
            v.err("/job/file", "required")
        sfn_path = v.string(f, "sfn_path", "/job/file", "/usb/JOB.GCO")
        if not sfn_path.startswith("/usb/"):
            v.err("/job/file/sfn_path", "must start with /usb/")
        job = {
            "id": v.integer(j, "id", "/job", 0, 0, 65535, required=True),
            "progress": v.integer(j, "progress", "/job", 0, 0, 100, required=True),
            "time_printing": v.integer(j, "time_printing", "/job", 0, 0, required=True),
            "time_remaining": v.integer(j, "time_remaining", "/job", None, 0),
            "filament_change_in": v.integer(j, "filament_change_in", "/job", None, 0),
            "file": {
                "sfn_path": sfn_path,
                "display_name": v.string(f, "display_name", "/job/file", "job.gcode"),
                "size": v.integer(f, "size", "/job/file", None, 0),
                "m_timestamp": v.integer(f, "m_timestamp", "/job/file", None, 0),
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
