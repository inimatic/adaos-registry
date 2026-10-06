"""Parse bounded JSON projections; never grep serialized application documents."""
import ast
import json
from pathlib import Path
import re

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
SCENARIO = Path(__file__).resolve().parents[1]


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def document():
    return json.loads((SCENARIO / "webui.json").read_text(encoding="utf-8"))


def test_observers_use_canonical_data_root():
    observers = [node["observe"] for node in walk(document()) if "observe" in node]
    assert observers
    assert all(value == "dataRoot" for value in observers)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def assert_no_fabricated_operational_data(page):
    operational = [n for n in page["widgets"] if n["id"].startswith(("system-", "development-"))]
    forbidden = re.compile(r"prototype|\bmock\b|sn_6acf0c01|37%|61%|8\.2 / 16 GB|14 minutes|7 delivered|\b2 accepted\b|2 active · 1 awaiting review|slot [AB] ·|\b9[ -]skills\b", re.I)
    # Exact product command vocabulary may mention Prototype; operational copy
    # such as 'Archive (Prototype)' must still fail, including titles/actions.
    product_labels = {"Prototype", "Open Prototype", "Preview Prototype"}
    def inspect(value, identity, key=None):
        if isinstance(value, dict):
            for child_key, child in value.items():
                inspect(child, identity, child_key)
        elif isinstance(value, list):
            for child in value:
                inspect(child, identity, key)
        elif isinstance(value, str):
            if key in {"label", "title"} and value in product_labels:
                return
            assert not forbidden.search(value), (identity, key, value)
    for widget in operational:
        inspect(widget, widget["id"])
    inspect(page["initialState"], "initialState")
    assert "systemProtoNotice" not in page["initialState"]


def test_no_operational_prototype_or_fabricated_data():
    page = document()["ui"]["application"]["desktop"]["pageSchema"]
    assert_no_fabricated_operational_data(page)
    assert page["initialState"]["subnetDisplayName"] == ""
    assert page["initialState"]["isOwnerOrAdministrator"] is False
    assert page["initialState"]["developmentAccess"] is False


@pytest.mark.parametrize("copy", ["Archive (Prototype)", "Core update recommended / Prototype data", "9 skills", "37%", "61%", "8.2 / 16 GB", "7 delivered, 2 accepted", "2 accepted", "slot A ·", "sn_6acf0c01", "Prototype subnet", "mock subscription"])
@pytest.mark.parametrize("location", ["title", "state", "action"])
def test_guard_rejects_fabricated_copy_outside_data_sources(copy, location):
    page = {"widgets": [{"id": "system-test"}], "initialState": {}}
    if location == "state":
        page["initialState"]["summary"] = copy
    elif location == "action":
        page["widgets"][0]["actions"] = [{"label": copy}]
    else:
        page["widgets"][0]["title"] = copy
    with pytest.raises(AssertionError):
        assert_no_fabricated_operational_data(page)


def test_guard_allows_explicit_product_labels():
    assert_no_fabricated_operational_data({"widgets": [{"id": "development-builder", "actions": [{"label": "Open Prototype"}]}], "initialState": {}})


@pytest.mark.parametrize("target", sorted({n["target"] for n in walk(document()) if n.get("type") == "callSkill"}))
def test_every_call_skill_target_is_declared_and_exported(target):
    skill, name = target.split(".", 1)
    scenario = yaml.safe_load((SCENARIO / "scenario.yaml").read_text(encoding="utf-8"))
    assert skill in scenario["depends"], target
    path = ROOT / "skills" / skill / "skill.yaml"
    if not path.is_file():
        pytest.skip(f"Trusted worker must verify installed dependency export: {target}")
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    declarations = {t["name"]: t for t in manifest["tools"]}
    assert name in manifest["exports"]["tools"] and name in declarations, target
    entry, function = declarations[name]["entry"].split(":")
    tree = ast.parse((path.parent / entry.replace(".", "/")).with_suffix(".py").read_text(encoding="utf-8"))
    assert any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function and n.decorator_list for n in tree.body), target


def test_node_reads_are_bounded_and_preserve_last_known_values():
    widgets = {n["id"]: n for n in document()["ui"]["application"]["desktop"]["pageSchema"]["widgets"]}
    for identity in ("system-update-status-hub", "system-update-status-studio", "system-current-node-details", "system-attention-dashboard-tile"):
        source = widgets[identity]["dataSource"]
        assert source["params"]["target_node_id"] == "$state.selectedSystemNodeId"
        assert source["params"]["section"] == "node_dashboard"
        assert source["preserveLastValue"] is True
        assert source["maxRequestHz"] == 0.2
        assert "'archive'" not in widgets[identity]["visibleIf"]
    hardware_source = widgets["system-hardware-utilization"]["dataSource"]
    assert hardware_source["kind"] == "skill"
    assert hardware_source["name"] == "web_desktop_runtime_skill.get_system_overview"
    assert hardware_source["params"]["section"] == "node_dashboard"
    assert hardware_source["params"]["target_node_id"] == "$state.selectedSystemNodeId"
    assert "'archive'" not in widgets["system-hardware-utilization"]["visibleIf"]
    for identity in ("system-hardware-utilization-offline", "system-update-status-archive", "system-node-offline-name", "system-attention-dashboard-offline"):
        source = widgets[identity]["dataSource"]
        assert source["kind"] == "static"
        assert "target_node_id" not in str(source)


def test_subnet_save_is_a_persisted_mutation():
    widget = next(n for n in walk(document()) if n.get("id") == "system-subnet-form")
    action = next(a for a in widget["actions"] if a["id"] == "save_subnet_name")
    assert action["target"] == "web_desktop_runtime_skill.rename_subnet"
    assert action["invalidates"] == ["web_desktop.system", "system.members"]
