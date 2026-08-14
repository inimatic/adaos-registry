from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parents[1]


def test_infrascope_declares_inventory_drilldown_and_inspector_flow() -> None:
    scenario = json.loads((ROOT / "scenario.json").read_text(encoding="utf-8"))
    skill_yaml = (WORKSPACE_ROOT / "skills" / "infrascope_skill" / "skill.yaml").read_text(encoding="utf-8")
    widgets = {item["id"]: item for item in scenario["ui"]["application"]["desktop"]["pageSchema"]["widgets"]}

    inventory = widgets["inventory-list"]
    incidents = widgets["overview-incidents"]
    operations = widgets["overview-operations"]
    summary = widgets["selected-object-summary"]
    mode = widgets["infrascope-mode"]
    inventory_tabs = widgets["infrascope-inventory-tabs"]
    inspector_tabs = widgets["inspector-tabs"]

    assert scenario["type"] == "desktop"
    assert inventory["visibleIf"] == "$state.infrascopeMode === 'inventory'"
    assert inventory["dataSource"]["kind"] == "stream"
    assert inventory["dataSource"]["receiver"] == "infrascope.inventory.$state.inventoryKind"
    assert "refreshMs" not in inventory.get("inputs", {})
    assert incidents["actions"][0]["params"]["inspectorTab"] == "incidents"
    assert operations["dataSource"]["kind"] == "stream"
    assert operations["dataSource"]["receiver"] == "infrascope.operations.active"
    assert summary["dataSource"]["kind"] == "stream"
    assert summary["dataSource"]["receiver"] == "infrascope.inspector.$state.selectedObjectId"
    assert widgets["overview-summary"]["dataSource"]["path"] == "data/infrascope/summary"
    assert mode["inputs"]["selectedStateKey"] == "infrascopeMode"
    assert inventory_tabs["inputs"]["selectedStateKey"] == "inventoryKind"
    assert inspector_tabs["inputs"]["selectedStateKey"] == "inspectorTab"
    assert "get_overview_summary" in skill_yaml
    assert "get_object_inspector" in skill_yaml
    assert "get_snapshot" in skill_yaml
    assert "refresh_snapshot" in skill_yaml
    assert "data_projections" in skill_yaml
    assert "infrascope.snapshot" in skill_yaml
    assert "device.registered" in skill_yaml
    assert "browser.session.changed" in skill_yaml
    assert "webrtc.peer.state.changed" in skill_yaml
    assert "workspace." in skill_yaml
    assert "user.profile.changed" in skill_yaml
    assert "capacity.changed" in skill_yaml
