from __future__ import annotations

import json
from pathlib import Path

import yaml


SCENARIO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = SCENARIO_ROOT.parents[1] / "skills" / "media_center_skill"
CONTROL_SKILL_ROOT = SCENARIO_ROOT.parents[1] / "skills" / "media_control_skill"
AGENT_SKILL_ROOT = SCENARIO_ROOT.parents[1] / "skills" / "media_library_agent"


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
    assert page["interaction"]["initialFocus"] == "widget:media-search"
    assert page["initialState"]["mediaPageSize"] == 30
    assert page["initialState"]["mediaFavoritesOnly"] is False
    assert page["initialState"]["mediaNavigation"] == "home"
    assert {
        "media-browse-toolbar",
        "media-profile-selector",
        "media-mobile-now-playing",
        "media-mobile-targets",
        "media-mobile-transport",
        "media-search",
        "media-home",
        "media-catalog",
        "media-collections",
        "media-folder-breadcrumbs",
        "media-folders",
        "media-playlists",
    } == set(widgets)
    assert all(widget["type"] != "media.videoBrowser" for widget in page["widgets"])

    assert widgets["media-search"]["inputs"]["commitMode"] == "manual"
    assert widgets["media-search"]["inputs"]["saveLabel"] == "Search"
    toolbar = widgets["media-browse-toolbar"]
    assert toolbar["inputs"]["variant"] == "adaptiveToolbar"
    assert toolbar["visibleIf"] == "$state.surfaceProfile != 'mobile_control'"
    toolbar_buttons = {button["id"]: button for button in toolbar["inputs"]["buttons"]}
    assert list(toolbar_buttons) == ["remote", "profile", "section", "layout", "settings"]
    assert toolbar_buttons["profile"]["selectedStateKey"] == "profileId"
    assert toolbar_buttons["section"]["selectedStateKey"] == "mediaNavigation"
    navigation_ids = [option["id"] for option in toolbar_buttons["section"]["options"]]
    assert navigation_ids == [
        "home", "movies", "series", "music", "audiobooks", "folders",
        "playlists", "favorites", "recent",
    ]
    assert [option["id"] for option in toolbar_buttons["layout"]["options"]] == [
        "list", "cards", "rail",
    ]
    assert toolbar_buttons["layout"]["options"][2]["label"] == "Carousel"
    assert {action["on"] for action in toolbar["actions"]} == {
        "click:remote", "select:profile", "select:section", "select:layout",
        "click:settings",
    }

    catalog = widgets["media-catalog"]
    assert catalog["type"] == "ui.list"
    assert catalog["dataSource"]["params"]["limit"] == "$state.mediaPageSize"
    assert catalog["dataSource"]["params"]["cursor"] == "$state.mediaCursor"
    assert catalog["dataSource"]["params"]["media_kind"] == "$state.mediaKind"
    assert catalog["dataSource"]["params"]["favorites_only"] == "$state.mediaFavoritesOnly"
    assert catalog["collection"] == {
        "display": "cards",
        "displayModeStateKey": "mediaDisplay",
        "focusGroup": "media-catalog",
        "virtualized": True,
        "cursor": {"enabled": True, "stateKey": "mediaCursor"},
    }
    assert catalog["actions"][0]["params"]["selectedMediaFavorite"] == "$event.favorite"
    assert [action["type"] for action in catalog["actions"] if action["on"] == "select"] == [
        "updateState",
        "openModal",
    ]
    assert catalog["actions"][1]["params"]["modalId"] == "media_center_player"
    assert widgets["media-home"]["dataSource"] == {
        "kind": "stream",
        "receiver": "media_center.library_state",
        "path": "home",
        "params": {
            "profile_id": "$state.profileId",
            "shared_surface": "$state.sharedSurface",
        },
        "scope": "workspace",
    }
    assert widgets["media-home"]["inputs"]["collectionKey"] == "items"
    assert widgets["media-home"]["inputs"]["loadingStatusKey"] == "state"
    empty_text_by_state = widgets["media-home"]["inputs"]["emptyTextByState"]
    assert {
        key: value
        for key, value in empty_text_by_state.items()
        if not key.endswith("_i18n")
    } == {
        "unconfigured": "Add a media folder in Settings to begin.",
        "indexing": "Your media folders are being indexed.",
        "empty": "No playable media was found in the configured folders.",
        "unavailable": "Library state is temporarily unavailable.",
    }
    assert {
        key.removesuffix("_i18n")
        for key in empty_text_by_state
        if key.endswith("_i18n")
    } == {"unconfigured", "indexing", "empty", "unavailable"}
    assert widgets["media-profile-selector"]["inputs"]["selectedStateKey"] == "profileId"
    assert widgets["media-profile-selector"]["visibleIf"] == (
        "$state.surfaceProfile == 'mobile_control'"
    )
    assert page["presentation"]["profileStateKey"] == "surfaceProfile"
    assert set(page["presentation"]["profiles"]) == {
        "desktop", "tv", "mobile_control", "embedded",
    }
    assert page["presentation"]["profiles"]["tv"] == {
        "inputMode": "dpad",
        "density": "ten_foot",
        "overscanPx": 36,
        "maxContentWidthPx": 1920,
    }
    assert page["initialStateQuery"]["map"] == {
        "surfaceProfile": "presentation_profile",
        "sharedSurface": "shared_surface",
    }
    assert widgets["media-mobile-now-playing"]["visibleIf"] == (
        "$state.surfaceProfile == 'mobile_control'"
    )
    assert widgets["media-mobile-targets"]["dataSource"]["name"] == (
        "media_control_skill.list_targets"
    )
    assert len(widgets["media-mobile-transport"]["actions"]) == 5


def test_media_center_human_text_is_localized_by_skill_owned_dictionaries() -> None:
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))
    english = json.loads(
        (SKILL_ROOT / "assets" / "i18n" / "en.json").read_text(encoding="utf-8")
    )
    russian = json.loads(
        (SKILL_ROOT / "assets" / "i18n" / "ru.json").read_text(encoding="utf-8")
    )
    technical_placeholders = {
        "/mnt/media", "activation-id", "home", "node-id", "sha256:...",
    }
    human_fields = {
        "title", "label", "saveLabel", "emptyText", "loadingText",
        "errorText", "description", "confirmLabel", "cancelLabel",
    }
    referenced: set[str] = set()

    for item in _walk_dicts(webui["ui"]["application"]):
        for field in human_fields:
            fallback = item.get(field)
            if not isinstance(fallback, str) or not fallback or fallback in technical_placeholders:
                continue
            spec = item.get(f"{field}_i18n")
            assert isinstance(spec, dict), f"missing {field}_i18n for {fallback!r}"
            key = spec.get("key")
            assert isinstance(key, str) and key.startswith("runtime.media_center.ui.")
            referenced.add(key)
        for field, spec in item.items():
            if not field.endswith("_i18n") or not isinstance(spec, dict):
                continue
            key = spec.get("key")
            if isinstance(key, str) and key.startswith("runtime.media_center.ui."):
                referenced.add(key)

    assert referenced
    assert referenced <= set(english)
    assert referenced <= set(russian)
    assert all(english[key].strip() for key in referenced)
    assert all(russian[key].strip() for key in referenced)
    assert all(any(character.isalpha() for character in russian[key]) for key in referenced)
    assert sum(
        any("\u0400" <= character <= "\u04ff" for character in russian[key])
        for key in referenced
    ) >= len(referenced) - 5


def test_media_center_player_and_settings_are_ui_as_data_modals() -> None:
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))
    modals = webui["ui"]["application"]["modals"]

    assert {
        modal_id: modal["scope"]
        for modal_id, modal in modals.items()
    } == {
        "media_center_player": "workspace",
        "media_center_remote": "workspace",
        "media_center_settings": "workspace",
        "media_center_delete_root": "workspace",
    }

    player_widgets = {widget["id"]: widget for widget in modals["media_center_player"]["schema"]["widgets"]}
    player = player_widgets["media-center-player"]
    assert player["type"] == "media.videoBrowser"
    assert player["dataSource"]["name"] == "media_center_skill.build_playback_queue"
    assert player["dataSource"]["scope"] == "workspace"
    assert player["dataSource"]["params"]["source_type"] == "$state.playbackSourceType"
    assert player["dataSource"]["params"]["source_id"] == "$state.playbackSourceId"
    assert player["dataSource"]["params"]["limit"] == 10
    assert player["inputs"]["playlistLimit"] == 10
    assert player["inputs"]["autoSelectFirst"] is True
    assert player["inputs"]["showDiagnostics"] is False
    assert {
        "media-center-player-favorite",
        "media-center-player-unfavorite",
    } <= set(player_widgets)

    settings_ids = {
        widget["id"]
        for widget in modals["media_center_settings"]["schema"]["widgets"]
    }
    assert {
        "media-settings-actions",
        "media-center-summary",
        "media-root-path",
        "media-roots-table",
        "media-autoplay-settings",
        "media-fullscreen-settings",
        "media-profile-policy",
        "media-metadata-operations",
        "media-artwork-operation",
        "media-agent-performance",
        "media-playback-qoe",
        "media-deployment-plan-actions",
        "media-deployment-plan-digest",
        "media-deployment-activation",
        "media-deployment-agent-actions",
        "media-deployment-status",
    } <= settings_ids
    artwork_operation = next(
        widget
        for widget in modals["media_center_settings"]["schema"]["widgets"]
        if widget["id"] == "media-artwork-operation"
    )
    assert artwork_operation["dataSource"] == {
        "kind": "stream",
        "receiver": "media_library_agent.rendition_progress",
        "scope": "workspace",
    }
    settings_widgets = {
        widget["id"]: widget
        for widget in modals["media_center_settings"]["schema"]["widgets"]
    }
    assert settings_widgets["media-metadata-operations"]["dataSource"] == {
        "kind": "stream",
        "receiver": "media_center.operation_state",
        "scope": "workspace",
    }
    for widget_id, setting_key in {
        "media-autoplay-settings": "autoplay",
        "media-fullscreen-settings": "auto_fullscreen",
    }.items():
        widget = settings_widgets[widget_id]
        assert widget["type"] == "input.toggle"
        assert widget["dataSource"]["name"] == "media_control_skill.get_settings"
        assert widget["inputs"]["valuePath"] == f"settings.{setting_key}"
        assert widget["actions"] == [
            {
                "on": "change",
                "type": "callSkill",
                "target": "media_control_skill.set_settings",
                "params": {
                    "profile_id": "$state.profileId",
                    "values": {setting_key: "$event.checked"},
                },
                "invalidates": ["media_control.settings"],
            }
        ]
    roots = next(
        widget
        for widget in modals["media_center_settings"]["schema"]["widgets"]
        if widget["id"] == "media-roots-table"
    )
    delete_column = next(column for column in roots["inputs"]["columns"] if column.get("kind") == "buttons")
    assert delete_column["buttons"][0]["id"] == "delete"
    assert roots["actions"][1]["params"]["modalId"] == "media_center_delete_root"

    delete_actions = modals["media_center_delete_root"]["schema"]["widgets"][1]["actions"]
    assert delete_actions[0]["target"] == "media_center_skill.delete_root"

    remote = {
        widget["id"]: widget
        for widget in modals["media_center_remote"]["schema"]["widgets"]
    }
    assert remote["media-targets"]["dataSource"]["name"] == "media_control_skill.list_targets"
    assert remote["media-now-playing"]["dataSource"]["receiver"] == "media_control.now_playing"
    assert remote["media-remote-transport"]["actions"][1]["target"] == "media_control_skill.voice_command"


def test_media_center_skill_data_source_params_use_supported_scalar_state_refs() -> None:
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))

    for data_source in _skill_data_sources(webui):
        for value in (data_source.get("params") or {}).values():
            assert not (isinstance(value, dict) and value.get("kind") == "expression")


def test_media_center_skill_data_sources_match_data_route_read_policies() -> None:
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))
    manifests = {
        "media_center_skill": yaml.safe_load(
            (SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8")
        ),
        "media_control_skill": yaml.safe_load(
            (CONTROL_SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8")
        ),
        "media_library_agent": yaml.safe_load(
            (AGENT_SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8")
        ),
    }
    policies = {
        (skill_name, route["tool"]): route["read_policy"]
        for skill_name, manifest in manifests.items()
        for route in manifest["data_routes"]
        if str(route.get("route", "")).startswith("tool") and route.get("tool")
    }

    for data_source in _skill_data_sources(webui):
        skill_name, tool_name = data_source["name"].split(".", 1)
        policy = policies[(skill_name, tool_name)]
        assert data_source["invalidationTags"] == policy["invalidation_tags"]
        assert data_source["preserveLastValue"] == policy["preserve_last_value"]
        assert data_source["maxRequestHz"] == policy["max_request_hz"]


def test_media_center_folder_import_uses_reference_sdk_not_copy_publication() -> None:
    handler = (SKILL_ROOT / "handlers" / "main.py").read_text(encoding="utf-8")

    assert "from adaos.sdk.io.media import register_media_file" in handler
    assert "publish_media_file" not in handler
    assert '"storage_mode": "reference"' in handler
