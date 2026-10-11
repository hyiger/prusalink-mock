"""Shipped scenarios and validation against firmware limits."""

from __future__ import annotations

import copy
import json
from importlib import resources

import pytest

from prusalink_mock import state
from prusalink_mock.state import ScenarioError, merge_patch, normalize

SHIPPED = sorted(p.name[:-5] for p in resources.files("prusalink_mock").joinpath("scenarios").iterdir()
                 if p.name.endswith(".json"))


def test_expected_scenarios_ship():
    assert set(SHIPPED) == {"indx-hy8", "indx-hy8-hf06", "indx-gaps", "indx-no-docks", "indx-stock-fw",
                            "coreone-stock", "indx-printing", "login-disabled", "mini404-3tool"}


@pytest.mark.parametrize("name", SHIPPED)
def test_scenario_is_valid_and_named_after_its_file(name):
    st = state.load(name)
    assert st["name"] == name and st["description"]
    assert normalize(st) == st  # normalized state is a valid document


def test_default_is_the_users_indx():
    st = state.load(state.DEFAULT_SCENARIO)
    assert state.printer_version(st) == "7.10.0"
    assert {t["nozzle_diameter"] for t in st["tools"].values()} == {0.4}
    assert all(t["high_flow"] and not t["hardened"] for t in st["tools"].values())
    loaded = [t["loaded"] for t in st["tools"].values()]
    assert any(loaded) and not all(loaded)
    assert all(t["vendor"] in state.PRESET_VENDORS and t["color"] in state.PRESET_COLORS
               for t in st["tools"].values() if t["loaded"])


def test_hf06_variant():
    assert {t["nozzle_diameter"] for t in state.load("indx-hy8-hf06")["tools"].values()} == {0.6}


def test_scenario_from_a_file(tmp_path):
    doc = state.load_document("indx-gaps")
    doc["name"] = "mine"
    path = tmp_path / "mine.json"
    path.write_text(json.dumps(doc))
    assert state.load(str(path))["name"] == "mine"


def test_unknown_scenario():
    with pytest.raises(ScenarioError, match="unknown scenario"):
        state.load("no-such-scenario")


def _with_tool(**fields):
    doc = state.load_document("indx-hy8")
    tool = {"loaded": True, "material": "PLA", "vendor": "", "color": "",
            "nozzle_diameter": 0.4, "hardened": False, "high_flow": True}
    tool.update(fields)
    doc["tools"]["1"] = tool
    return doc


@pytest.mark.parametrize("fields,path", [
    ({"material": "ABCDEFGH"}, "/tools/1/material"),
    ({"vendor": "V" * 16}, "/tools/1/vendor"),
    ({"color": "Teal", "color_hex": "#00808G"}, "/tools/1/color_hex"),
    ({"color": "Teal"}, "/tools/1/color_hex"),
    ({"color": "Red", "color_hex": "#ff0000"}, "/tools/1/color_hex"),
    ({"color_hex": "#FF0000"}, "/tools/1/color_hex"),
    ({"color": "Teal", "color_hex": ""}, "/tools/1/color_hex"),
    ({"loaded": False}, "/tools/1"),
    ({"material": ""}, "/tools/1/material"),
    ({"nozzle_diameter": 3}, "/tools/1/nozzle_diameter"),
    ({"enabled": True}, "/tools/1/enabled"),
    ({"vendor": "nul\0"}, "/tools/1/vendor"),
])
def test_tool_limits(fields, path):
    with pytest.raises(ScenarioError) as exc:
        normalize(_with_tool(**fields))
    assert any(e.startswith(path) for e in exc.value.errors), exc.value.errors


def test_limits_count_utf8_bytes():
    normalize(_with_tool(vendor="V" * 15, material="PCTG-HF"))
    with pytest.raises(ScenarioError):
        normalize(_with_tool(vendor="Žluťoučký kůň"))  # 13 characters, 18 bytes


def test_preset_color_fills_color_hex():
    st = normalize(_with_tool(vendor="Prusament", color="Natural"))
    assert st["tools"]["1"]["color_hex"] == "#F5F5DC"


@pytest.mark.parametrize("change,path", [
    (lambda d: d["tools"].update({"9": d["tools"]["1"]}), "/tools/9"),
    (lambda d: d["printer"].update(model="COREONE", active_tool=1) or d["tools"].update({"2": d["tools"]["1"]}),
     "/tools/2"),
    (lambda d: d["printer"].update(state="PRINTING"), "/job"),
    (lambda d: d["printer"].update(state="READY"), "/printer/state"),
    (lambda d: d["tools"].pop("4") and d["printer"].update(active_tool=4), "/printer/active_tool"),
    (lambda d: d["printer"].update(model="ENDER3"), "/printer/model"),
    (lambda d: d.update(extends="indx-base"), "/extends"),
    (lambda d: d["auth"].update(password="x" * 16), "/auth/password"),
])
def test_document_limits(change, path):
    doc = state.load_document("indx-hy8")
    change(doc)
    with pytest.raises(ScenarioError) as exc:
        normalize(doc)
    assert any(e.startswith(path) for e in exc.value.errors), exc.value.errors


def test_job_only_while_printing():
    doc = state.load_document("indx-printing")
    doc["printer"]["state"] = "IDLE"
    with pytest.raises(ScenarioError, match="/job"):
        normalize(doc)


def test_job_file_time_has_the_firmwares_seconds():
    doc = state.load_document("indx-printing")
    doc["job"]["file"]["m_timestamp"] = 1767225645  # :45, which get_posix_time can't report
    with pytest.raises(ScenarioError, match="/job/file/m_timestamp"):
        normalize(doc)


def test_merge_patch():
    base = {"printer": {"bed": {"actual": 1.0, "target": 2.0}, "state": "IDLE"}, "tools": {"1": {}, "2": {}},
            "job": {"id": 1}}
    patched = merge_patch(base, {"printer": {"bed": {"actual": 5.0}}, "tools": {"3": {"x": 1}}, "job": None})
    assert patched == {"printer": {"bed": {"actual": 5.0, "target": 2.0}, "state": "IDLE"}, "tools": {"3": {"x": 1}}}
    assert base["tools"] == {"1": {}, "2": {}}  # input untouched


def test_captured_tools_body_pastes_in():
    captured = json.loads('{"tools":{"1":{"loaded":true,"material":"PETG","vendor":"Prusament","color":"Black",'
                          '"color_hex":"#000000","nozzle_diameter":0.40,"hardened":false,"high_flow":true},'
                          '"5":{"loaded":false,"material":"","vendor":"","color":"","color_hex":"",'
                          '"nozzle_diameter":0.40,"hardened":false,"high_flow":true}}}')
    doc = copy.deepcopy(state.load_document("indx-hy8"))
    doc["tools"] = captured["tools"]
    st = normalize(doc)
    assert list(st["tools"]) == ["1", "5"]


def test_job_id_lives_on_the_printer():
    doc = state.load_document("indx-printing")
    doc["job"]["id"] = 42
    with pytest.raises(ScenarioError, match="/printer/job_id"):
        normalize(doc)
    st = state.load("indx-printing")
    assert st["printer"]["job_id"] == 42 and "id" not in st["job"]
    assert state.load("indx-hy8")["printer"]["job_id"] == 0


def test_model_values_are_stored_only_when_set():
    st = state.load("indx-hy8")
    assert st["printer"]["printer_version"] is None and st["printer"]["tool_count"] is None
    assert (state.printer_version(st), state.tool_count(st), state.hostname(st)) == ("7.10.0", 8, "prusa-core-one")
    st = state.load("mini404-3tool")
    assert st["printer"]["tool_count"] == 3 and state.printer_version(st) == "7.1.0"
