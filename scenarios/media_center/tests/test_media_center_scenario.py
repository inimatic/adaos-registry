from __future__ import annotations

import json
from pathlib import Path

import yaml


SCENARIO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = SCENARIO_ROOT.parents[1] / "skills" / "media_center_skill"


def _walk_dicts(node: object) -> list[dict]:
    if isinstance(node, dict):
        found = [node]
        for value in node.values():
            found.extend(_walk_dicts(value))
        return found
    if isinstance(node, list):
        found: list[dict] = []
        for value in node:
            found.extend(_walk_dicts(value))
        return found
    return []


def _skill_data_sources(node: object) -> list[dict]:
    return [
        item["dataSource"]
        for item in _walk_dicts(node)
        if isinstance(item.get("dataSource"), dict) and item["dataSource"].get("kind") == "skill"
    ]


def test_media_center_ui_keeps_runtime_i18n_in_skill_and_declares_long_import_timeouts() -> None:
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))
    app = webui["ui"]["application"]

    assert "resources" not in app
    assert "resources" not in webui

    actions = [
        item
        for item in _walk_dicts(app)
        if item.get("type") == "callSkill"
        and item.get("target") in {
            "media_center_skill.import_folder",
            "media_center_skill.scan_roots",
        }
    ]
    assert {action["target"]: action["timeoutMs"] for action in actions} == {
        "media_center_skill.import_folder": 600000,
        "media_center_skill.scan_roots": 600000,
    }


def test_media_center_main_surface_is_compact_and_server_paged() -> None:
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))
    page = webui["ui"]["application"]["desktop"]["pageSchema"]
    widgets = {widget["id"]: widget for widget in page["widgets"]}

    assert page["layout"]["type"] == "single"
    assert page["initialState"]["mediaPageSize"] == 30
    assert set(widgets) == {
        "media-center-settings-action",
        "media-kind-tabs",
        "media-search",
        "media-search-actions",
        "media-catalog-table",
        "media-page-actions",
    }
    assert all(widget["type"] != "media.videoBrowser" for widget in page["widgets"])

    kind_ids = [button["id"] for button in widgets["media-kind-tabs"]["inputs"]["buttons"]]
    assert kind_ids == ["playable", "video", "audio"]
    assert widgets["media-search"]["inputs"]["commitMode"] == "manual"
    assert widgets["media-search"]["inputs"]["saveLabel"] == "Search"

    catalog = widgets["media-catalog-table"]
    assert catalog["dataSource"]["params"]["limit"] == "$state.mediaPageSize"
    assert catalog["dataSource"]["params"]["offset"] == "$state.mediaOffset"
    assert catalog["dataSource"]["params"]["media_kind"] == {
        "kind": "expression",
        "op": "if",
        "condition": "$state.mediaKind",
        "then": "$state.mediaKind",
        "else": "playable",
    }
    assert [action["type"] for action in catalog["actions"] if action["on"] == "select"] == [
        "updateState",
        "openModal",
    ]
    assert catalog["actions"][1]["params"]["modalId"] == "media_center_player"


def test_media_center_player_and_settings_are_ui_as_data_modals() -> None:
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))
    modals = webui["ui"]["application"]["modals"]

    player_widgets = {widget["id"]: widget for widget in modals["media_center_player"]["schema"]["widgets"]}
    player = player_widgets["media-center-player"]
    assert player["type"] == "media.videoBrowser"
    assert player["dataSource"]["name"] == "media_center_skill.playback_queue"
    assert player["dataSource"]["params"]["item_id"] == "$state.selectedMediaItemId"
    assert player["dataSource"]["params"]["limit"] == 10
    assert player["inputs"]["playlistLimit"] == 10
    assert player["inputs"]["autoSelectFirst"] is True
    assert player["inputs"]["showDiagnostics"] is False

    settings_ids = {
        widget["id"]
        for widget in modals["media_center_settings"]["schema"]["widgets"]
    }
    assert {
        "media-settings-actions",
        "media-center-summary",
        "media-root-path",
        "media-roots-actions",
        "media-roots-table",
        "media-source-tabs",
        "media-sort-tabs",
    } <= settings_ids


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


def test_media_center_folder_import_uses_reference_sdk_not_copy_publication() -> None:
    handler = (SKILL_ROOT / "handlers" / "main.py").read_text(encoding="utf-8")

    assert "from adaos.sdk.io.media import register_media_file" in handler
    assert "publish_media_file" not in handler
    assert '"storage_mode": "reference"' in handler
