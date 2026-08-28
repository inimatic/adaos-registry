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
    assert page["playbackEndpoint"] == {
        "schema": "adaos.playback.endpoint_provider.v1",
        "adapter": {
            "skill": "media_control_skill",
            "inbox_method": "endpoint_inbox",
            "open_session_method": "open_endpoint_session",
            "pull_commands_method": "pull_commands",
            "reconcile_method": "reconcile_endpoint",
        },
        "heartbeat_interval_ms": 15000,
        "command_poll_interval_ms": 3000,
        "queue_window_limit": 30,
    }
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
        "media-collection-toolbar",
        "media-collection-breadcrumbs",
        "media-collection-children",
        "media-collection-items",
        "media-folder-breadcrumbs",
        "media-folders",
        "media-playlists",
    } == set(widgets)
    assert all(widget["type"] != "media.videoBrowser" for widget in page["widgets"])

    assert widgets["media-search"]["inputs"]["commitMode"] == "manual"
    assert widgets["media-search"]["inputs"]["saveLabel"] == "Search"
    assert widgets["media-search"]["inputs"]["clearable"] is True
    assert widgets["media-search"]["inputs"]["clearLabel"] == "Reset"
    search_action = widgets["media-search"]["actions"][0]
    assert search_action["on"] == "change"
    assert search_action["params"] == {
        "mediaSearch": "$event.value",
        "mediaSection": "catalog",
        "mediaKind": "playable",
        "mediaCollectionKind": "",
        "mediaCollectionId": "",
        "mediaCollectionTitle": "",
        "mediaFavoritesOnly": False,
        "mediaSort": "title",
        "mediaGenre": "",
        "mediaYear": None,
        "mediaRatingMin": None,
        "mediaContentRating": "",
        "mediaFolderAgentId": "",
        "mediaFolderRootId": "",
        "mediaFolderParent": "",
        "mediaCursor": "",
    }
    toolbar = widgets["media-browse-toolbar"]
    assert toolbar["inputs"]["variant"] == "adaptiveToolbar"
    assert toolbar["visibleIf"] == "$state.surfaceProfile != 'mobile_control'"
    toolbar_buttons = {button["id"]: button for button in toolbar["inputs"]["buttons"]}
    assert list(toolbar_buttons) == [
        "remote", "profile", "section", "layout", "filters", "settings", "target"
    ]
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
        "click:filters", "click:settings", "select:target",
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
    assert catalog["actions"][0]["params"]["playbackSourceType"] == "catalog"
    assert catalog["actions"][0]["params"]["playbackSourceContext"] == {
        "query": "$state.mediaSearch",
        "media_kind": "$state.mediaKind",
        "favorites_only": "$state.mediaFavoritesOnly",
        "sort": "$state.mediaSort",
        "sort_direction": "$state.mediaSortDirection",
        "genre": "$state.mediaGenre",
        "year": "$state.mediaYear",
        "rating_min": "$state.mediaRatingMin",
        "content_rating": "$state.mediaContentRating",
        "cursor": "$state.mediaCursor",
    }
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
    assert widgets["media-home"]["inputs"]["selectEventKey"] == "queue_source_type"
    home_actions = {
        action["on"]: action
        for action in widgets["media-home"]["actions"]
        if action["type"] == "updateState"
    }
    assert home_actions["select:folder"]["params"] == {
        "mediaNavigation": "folders",
        "mediaSection": "folders",
        "mediaFolderAgentId": "$event.agent_id",
        "mediaFolderRootId": "$event.root_id",
        "mediaFolderParent": "$event.path",
        "mediaCursor": "",
        "selectedMediaItemId": None,
        "selectedMediaFavorite": False,
    }
    assert widgets["media-folders"]["dataSource"]["params"] == {
        "agent_id": "$state.mediaFolderAgentId",
        "root_id": "$state.mediaFolderRootId",
        "parent": "$state.mediaFolderParent",
        "profile_id": "$state.profileId",
        "limit": "$state.mediaPageSize",
        "cursor": "$state.mediaCursor",
    }
    assert {
        action["on"]
        for action in widgets["media-home"]["actions"]
        if action["type"] == "openModal"
    } == {"select:item", "select:playlist"}
    assert home_actions["select:collection"]["params"]["mediaSection"] == (
        "collection"
    )
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
    assert [
        button["id"]
        for button in widgets["media-mobile-transport"]["inputs"]["buttons"]
    ] == ["previous", "toggle", "next", "stop"]
    assert widgets["media-mobile-transport"]["actions"][1]["params"]["action"] == (
        "toggle"
    )

    collection_widgets = {
        widget_id: widgets[widget_id]
        for widget_id in (
            "media-collection-toolbar",
            "media-collection-breadcrumbs",
            "media-collection-children",
            "media-collection-items",
        )
    }
    assert all(
        widget["visibleIf"] == "$state.mediaSection == 'collection'"
        for widget in collection_widgets.values()
    )
    for widget_id in (
        "media-collection-breadcrumbs",
        "media-collection-children",
        "media-collection-items",
    ):
        source = collection_widgets[widget_id]["dataSource"]
        assert source["name"] == "media_center_skill.collection_contents"
        assert source["params"]["limit"] == "$state.mediaPageSize"
        assert source["params"]["cursor"] == "$state.mediaCursor"
    assert collection_widgets["media-collection-items"]["collection"] == {
        "display": "cards",
        "displayModeStateKey": "mediaDisplay",
        "focusGroup": "media-collection-items",
        "virtualized": True,
        "cursor": {"enabled": True, "stateKey": "mediaCursor"},
    }
    assert [
        action["type"]
        for action in collection_widgets["media-collection-items"]["actions"]
        if action["on"] == "select"
    ] == ["updateState", "openModal"]
    collection_select = collection_widgets["media-collection-items"]["actions"][0]
    assert collection_select["params"]["playbackSourceType"] == "collection"
    assert collection_select["params"]["playbackSourceId"] == (
        "$state.mediaCollectionId"
    )


def test_media_center_human_text_is_localized_by_skill_owned_dictionaries() -> None:
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))
    english = json.loads(
        (SKILL_ROOT / "assets" / "i18n" / "en.json").read_text(encoding="utf-8")
    )
    russian = json.loads(
        (SKILL_ROOT / "assets" / "i18n" / "ru.json").read_text(encoding="utf-8")
    )
    scenario_english = json.loads(
        (SCENARIO_ROOT / "assets" / "i18n" / "en.json").read_text(encoding="utf-8")
    )
    scenario_russian = json.loads(
        (SCENARIO_ROOT / "assets" / "i18n" / "ru.json").read_text(encoding="utf-8")
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
            if (
                not isinstance(fallback, str)
                or not fallback
                or fallback.startswith("$")
                or fallback in technical_placeholders
            ):
                continue
            spec = item.get(f"{field}_i18n")
            assert isinstance(spec, dict), f"missing {field}_i18n for {fallback!r}"
            key = spec.get("key")
            assert isinstance(key, str)
            if key.startswith("scenario.media_center."):
                assert key in scenario_english
                assert key in scenario_russian
            else:
                assert key.startswith("runtime.media_center.ui.")
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
    manifest = yaml.safe_load(
        (SCENARIO_ROOT / "scenario.yaml").read_text(encoding="utf-8")
    )
    assert manifest["title_i18n"] == {
        "fallback": "Media Center",
        "key": "scenario.media_center.title",
    }


def test_media_center_player_and_settings_are_ui_as_data_modals() -> None:
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))
    modals = webui["ui"]["application"]["modals"]

    assert {
        modal_id: modal["scope"]
        for modal_id, modal in modals.items()
        } == {
            "media_center_player": "workspace",
            "media_center_play_on": "workspace",
            "media_center_add_to_playlist": "workspace",
            "media_center_metadata_edit": "workspace",
            "media_center_filters": "workspace",
        "media_center_remote": "workspace",
        "media_center_settings": "workspace",
            "media_center_metadata": "workspace",
            "media_center_storage_policy": "workspace",
            "media_center_delete_root": "workspace",
    }

    player_schema = modals["media_center_player"]["schema"]
    player_widgets = {widget["id"]: widget for widget in player_schema["widgets"]}
    assert modals["media_center_player"]["presentation"]["kind"] == "fullscreen"
    assert player_schema["layout"]["type"] == "split"
    assert {
        area["id"]: area["role"] for area in player_schema["layout"]["areas"]
    } == {"main": "main", "transport": "aux", "actions": "footer"}
    player = player_widgets["media-center-player"]
    assert player["type"] == "media.videoBrowser"
    assert player["dataSource"]["name"] == "media_center_skill.build_playback_queue"
    assert player["dataSource"]["scope"] == "workspace"
    assert player["dataSource"]["params"]["source_type"] == "$state.playbackSourceType"
    assert player["dataSource"]["params"]["source_id"] == "$state.playbackSourceId"
    assert player["dataSource"]["params"]["source_context"] == (
        "$state.playbackSourceContext"
    )
    assert player["dataSource"]["params"]["limit"] == 500
    assert player["dataSource"]["params"]["start_item_id"] == (
        "$state.selectedMediaItemId"
    )
    assert player["inputs"]["playlistLimit"] == 10
    assert player["inputs"]["autoSelectFirst"] is True
    assert player["inputs"]["showDiagnostics"] is False
    assert {
        "media-center-player-favorite",
        "media-center-player-unfavorite",
        "media-center-player-profile",
    } <= set(player_widgets)
    assert player_widgets["media-center-player-favorite"]["area"] == "actions"
    assert player_widgets["media-center-player-unfavorite"]["area"] == "actions"
    player_profile = player_widgets["media-center-player-profile"]
    assert player_profile["area"] == "transport"
    assert player["area"] == "transport"
    assert player_widgets["media-center-player-details"]["area"] == "main"
    assert player_profile["inputs"]["variant"] == "adaptiveToolbar"
    assert [option["value"] for option in player_profile["inputs"]["buttons"][0]["options"]] == [
        "default",
        "household",
        "kids",
    ]

    filter_widgets = {
        widget["id"]: widget
        for widget in modals["media_center_filters"]["schema"]["widgets"]
    }
    assert filter_widgets["media-filter-genre"]["dataSource"]["name"] == (
        "media_center_skill.metadata_facets"
    )
    assert filter_widgets["media-filter-year"]["dataSource"]["params"][
        "dimension"
    ] == "year"
    assert len(filter_widgets["media-sort-order"]["inputs"]["options"]) == 15

    settings_ids = {
        widget["id"]
        for widget in modals["media_center_settings"]["schema"]["widgets"]
    }
    assert {
        "media-settings-actions",
        "media-center-summary",
        "media-scan-progress",
        "media-root-path",
        "media-roots-table",
        "media-autoplay-settings",
        "media-fullscreen-settings",
        "media-profile-policy",
        "media-agent-performance",
        "media-playback-qoe",
        "media-deployment-plan-actions",
        "media-deployment-plan-digest",
        "media-deployment-activation",
        "media-deployment-agent-actions",
        "media-deployment-status",
    } <= settings_ids
    assert "media-metadata-operations" not in settings_ids
    assert "media-artwork-operation" not in settings_ids
    settings_actions = next(
        widget
        for widget in modals["media_center_settings"]["schema"]["widgets"]
        if widget["id"] == "media-settings-actions"
    )
    assert next(
        action
        for action in settings_actions["actions"]
        if action["on"] == "click:metadata"
    )["params"]["modalId"] == "media_center_metadata"
    metadata_widgets = {
        widget["id"]: widget
        for widget in modals["media_center_metadata"]["schema"]["widgets"]
    }
    rendition_operation = next(
        widget
        for widget in modals["media_center_metadata"]["schema"]["widgets"]
        if widget["id"] == "media-current-rendition-operation"
    )
    assert rendition_operation["dataSource"] == {
        "kind": "stream",
        "receiver": "media_library_agent.rendition_progress",
        "scope": "workspace",
    }
    assert metadata_widgets["media-rendition-operations"]["dataSource"]["name"] == (
        "media_center_skill.rendition_operations"
    )
    assert metadata_widgets["media-provider-artwork-cache"]["dataSource"]["path"] == (
        "runtime.artwork_cache"
    )
    assert metadata_widgets["media-background-admission"]["dataSource"]["path"] == (
        "runtime.queue_maintenance.admission"
    )
    assert metadata_widgets["media-job-retention"]["dataSource"]["path"] == (
        "job_retention"
    )
    assert modals["media_center_filters"]["presentation"]["kind"] == "drawer"
    assert metadata_widgets["media-metadata-operations"]["dataSource"] == {
        "kind": "stream",
        "receiver": "media_center.operation_state",
        "scope": "workspace",
    }
    operation_actions = metadata_widgets["media-metadata-operations"]["actions"]
    assert operation_actions[0]["params"]["selectedMediaItemId"] == (
        "$event.item_id"
    )
    assert operation_actions[0]["params"]["playbackSourceType"] == "item"
    assert operation_actions[0]["params"]["playbackSourceId"] == "$event.item_id"
    assert operation_actions[0]["params"]["playbackSourceContext"] == {}
    assert operation_actions[1] == {
        "on": "select",
        "type": "openModal",
        "params": {"modalId": "media_center_player"},
    }
    metadata_edit_widgets = {
        widget["id"]: widget
        for widget in modals["media_center_metadata_edit"]["schema"]["widgets"]
    }
    assert metadata_edit_widgets["media-metadata-artwork-url"]["type"] == (
        "input.text"
    )
    artwork_actions = metadata_edit_widgets["media-metadata-artwork-actions"]
    assert {
        action["target"]
        for action in artwork_actions["actions"]
        if action["type"] == "callSkill"
    } == {"media_center_skill.review_item_artwork"}
    settings_widgets = {
        widget["id"]: widget
        for widget in modals["media_center_settings"]["schema"]["widgets"]
    }
    provider_list = settings_widgets["media-metadata-settings-providers"]
    assert "path" not in provider_list["dataSource"]
    assert provider_list["inputs"]["collectionKey"] == "providers"
    assert provider_list["inputs"]["titleKey"] == "provider_id"
    assert provider_list["inputs"]["subtitleKey"] == "state"
    assert not {"primaryKey", "secondaryKey", "metaKey"} & set(
        provider_list["inputs"]
    )
    assert settings_widgets["media-tmdb-token"]["inputs"]["inputType"] == (
        "password"
    )
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
    assert {button["id"] for button in delete_column["buttons"]} == {
        "storage",
        "delete",
    }
    storage_widgets = {
        widget["id"]: widget
        for widget in modals["media_center_storage_policy"]["schema"]["widgets"]
    }
    assert storage_widgets["media-rendition-storage-mode"]["type"] == (
        "input.selector"
    )
    save_action = next(
        action
        for action in storage_widgets["media-storage-policy-actions"]["actions"]
        if action["on"] == "click:save" and action["type"] == "callSkill"
    )
    assert save_action["target"] == "media_center_skill.set_root_storage_policy"
    assert next(
        action
        for action in roots["actions"]
        if action["on"] == "click:storage" and action["type"] == "openModal"
    )["params"]["modalId"] == "media_center_storage_policy"
    assert next(
        action
        for action in roots["actions"]
        if action["on"] == "click:delete" and action["type"] == "openModal"
    )["params"]["modalId"] == "media_center_delete_root"
    scan_progress = settings_widgets["media-scan-progress"]
    assert scan_progress["dataSource"] == {
        "kind": "stream",
        "receiver": "media_library_agent.progress",
        "scope": "workspace",
    }
    assert {field["key"] for field in scan_progress["inputs"]["fields"]} == {
        "status", "root_label", "processed", "changes", "current_path", "error",
    }
    assert any(
        column["key"] == "last_scan_at"
        for column in roots["inputs"]["columns"]
    )

    delete_actions = modals["media_center_delete_root"]["schema"]["widgets"][1]["actions"]
    assert delete_actions[0]["target"] == "media_center_skill.delete_root"

    remote = {
        widget["id"]: widget
        for widget in modals["media_center_remote"]["schema"]["widgets"]
    }
    assert remote["media-targets"]["type"] == "input.selector"
    assert remote["media-targets"]["dataSource"]["name"] == "media_control_skill.list_targets"
    assert remote["media-targets"]["inputs"]["optionLabelPath"] == "control_label"
    assert remote["media-targets"]["inputs"]["optionMetaPaths"] == [
        "surface_context_label",
        "playback_summary",
        "authorization_label",
        "presence_state",
    ]
    assert remote["media-now-playing"]["inputs"]["subtitleKey"] == (
        "target_context_label"
    )
    assert remote["media-now-playing"]["dataSource"]["receiver"] == "media_control.now_playing"
    assert remote["media-now-playing"]["inputs"]["titleKey"] == "title"
    assert remote["media-remote-transport"]["actions"][1]["target"] == "media_control_skill.voice_command"
    assert [
        button["id"]
        for button in remote["media-remote-transport"]["inputs"]["buttons"]
    ] == ["previous", "toggle", "next", "stop"]
    assert "media-remote-close" not in remote


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
