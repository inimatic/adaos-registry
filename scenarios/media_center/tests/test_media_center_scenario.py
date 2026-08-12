from __future__ import annotations

import json
from pathlib import Path

import yaml


SCENARIO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = SCENARIO_ROOT.parents[1] / "skills" / "media_center_skill"


def test_media_center_ui_keeps_runtime_i18n_in_skill_and_declares_long_import_timeouts() -> None:
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))
    app = webui["ui"]["application"]

    assert "resources" not in app
    assert "resources" not in webui

    page = app["desktop"]["pageSchema"]
    actions = [
        action
        for widget in page["widgets"]
        for action in widget.get("actions", [])
        if action.get("target") in {
            "media_center_skill.import_folder",
            "media_center_skill.scan_roots",
        }
    ]
    assert {action["target"]: action["timeoutMs"] for action in actions} == {
        "media_center_skill.import_folder": 600000,
        "media_center_skill.scan_roots": 600000,
    }

    library_sources = [
        widget["dataSource"]
        for widget in page["widgets"]
        if widget.get("dataSource", {}).get("name") == "media_center_skill.library"
    ]
    catalog_source = next(source for source in library_sources if source["params"].get("query") == "$state.mediaSearch")
    assert catalog_source["params"]["media_kind"] == {
        "kind": "expression",
        "op": "if",
        "condition": "$state.mediaKind",
        "then": "$state.mediaKind",
        "else": "playable",
    }


def _skill_data_sources(node: object) -> list[dict]:
    if isinstance(node, dict):
        data_source = node.get("dataSource")
        found = [data_source] if isinstance(data_source, dict) and data_source.get("kind") == "skill" else []
        for value in node.values():
            found.extend(_skill_data_sources(value))
        return found
    if isinstance(node, list):
        found: list[dict] = []
        for value in node:
            found.extend(_skill_data_sources(value))
        return found
    return []


def test_media_center_skill_data_sources_match_data_route_read_policies() -> None:
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))
    skill = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))
    policies = {
        route["tool"]: route["read_policy"]
        for route in skill["data_routes"]
        if str(route.get("route", "")).startswith("tool") and route.get("tool")
    }

    for data_source in _skill_data_sources(webui):
        tool_name = data_source["name"].split(".", 1)[1]
        policy = policies[tool_name]
        assert data_source["invalidationTags"] == policy["invalidation_tags"]
        assert data_source["preserveLastValue"] == policy["preserve_last_value"]
        assert data_source["maxRequestHz"] == policy["max_request_hz"]
