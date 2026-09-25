from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def _widgets(webui: dict) -> list[dict]:
    return webui["ui"]["application"]["desktop"]["pageSchema"]["widgets"]


def _widget(webui: dict, widget_id: str) -> dict:
    return next(item for item in _widgets(webui) if item.get("id") == widget_id)


def _actions(widget: dict, event: str) -> list[dict]:
    return [item for item in widget.get("actions", []) if item.get("on") == event]


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def test_scenario_json_declares_webui_manifest_only() -> None:
    scenario = _load("scenario.json")
    webui = _load("webui.json")

    assert scenario["ui"] == {"manifest": "webui.json"}
    assert webui["ui"]["application"]["desktop"]["pageSchema"]["meta"]["builder"] == {
        "proto": "006",
        "ui_revision": "006",
        "scenario_id": "web_desktop",
    }


def test_home_uses_pinned_only_authoritative_application_projection() -> None:
    webui = _load("webui.json")
    home = _widget(webui, "desktop-icons")

    assert home["type"] == "collection.grid"
    assert home["dataSource"] == {
        "kind": "y",
        "transform": "desktop.icons",
        "excludeIds": [
            "web_desktop",
            "scenario:web_desktop",
            "adaos_connect_app",
            "skill_preview",
            "scenario:skill_preview",
        ],
    }
    assert home["inputs"]["tileMinWidth"] == 132


def test_home_launches_applications_with_scenario_navigation_not_modals() -> None:
    webui = _load("webui.json")
    home = _widget(webui, "desktop-icons")
    action = _actions(home, "select")[0]

    assert action["type"] == "callHost"
    assert action["target"] == "desktop.scenario.set"
    assert action["enabledIf"] == "$state.homeCustomizeMode !== true"
    assert action["params"] == {
        "scenario_id": "$event.scenario_id",
        "webspace_id": "$client.webspaceId",
    }
    assert not any(
        node.get("type") == "navigate"
        and node.get("params", {}).get("surface") == "modal"
        for node in _walk(webui)
    )


def test_desktop_does_not_embed_applications_or_users_access_management() -> None:
    webui = _load("webui.json")
    forbidden_widget_ids = {
        "apps-query",
        "apps-catalog",
        "apps-details",
        "apps-marketplace",
        "access-query",
        "access-members",
        "access-details",
        "access-actions",
    }
    nav = _widget(webui, "primary-nav")

    assert not (forbidden_widget_ids & {item.get("id") for item in _widgets(webui)})
    assert {button["id"] for button in nav["inputs"]["buttons"]} == {
        "home",
        "devices",
        "chat",
        "activity",
        "settings",
        "system",
        "dev",
    }
    assert "invite-person" not in webui["ui"]["application"].get("modals", {})


def test_settings_navigation_and_start_destination_match_current_desktop() -> None:
    webui = _load("webui.json")
    nav = _widget(webui, "primary-nav")
    settings_nav = _widget(webui, "settings-sections")
    account = _widget(webui, "settings-form-account")
    start_destination = next(
        field
        for field in account["inputs"]["fields"]
        if field["id"] == "start_destination"
    )
    nav_ids = {button["id"] for button in nav["inputs"]["buttons"]}

    assert settings_nav["visibleIf"] == "$state.activeTab === 'settings'"
    assert {item["value"] for item in start_destination["options"]} == nav_ids
    assert "apps" not in {item["value"] for item in start_destination["options"]}


def test_home_customization_preserves_application_and_widget_reorder_context() -> None:
    webui = _load("webui.json")
    home = _widget(webui, "desktop-icons")
    widgets = _widget(webui, "desktop-widgets")

    assert home["dataSource"] == {
        "kind": "y",
        "transform": "desktop.icons",
        "excludeIds": [
            "web_desktop",
            "scenario:web_desktop",
            "adaos_connect_app",
            "skill_preview",
            "scenario:skill_preview",
        ],
    }
    assert widgets["dataSource"] == {"kind": "y", "transform": "desktop.widgets"}
    assert home["id"] == "desktop-icons"
    assert widgets["id"] == "desktop-widgets"
    assert home["inputs"]["customization"] == {
        "stateKey": "homeCustomizeMode",
        "mode": "compactContextual",
        "reorder": True,
        "unpin": True,
    }
    assert widgets["inputs"]["customization"] == home["inputs"]["customization"]
    assert {item["id"] for item in home["inputs"]["headerActions"]} == {
        "customize",
        "finish_customize",
    }
    assert all(item["kind"] == "icon" for item in home["inputs"]["headerActions"])
    assert {item["id"] for item in home["inputs"]["itemActions"]} == {"move", "unpin"}
    assert {item["id"] for item in widgets["inputs"]["itemActions"]} == {
        "move",
        "unpin",
    }
    home_mutations = [
        item
        for item in home["actions"]
        if item.get("target")
        in {"applications.reorder_home", "applications.set_home_pin"}
    ]
    widget_mutations = [
        item
        for item in widgets["actions"]
        if item.get("target") == "web_desktop_runtime_skill.update_home_item"
    ]

    assert {item["on"] for item in home_mutations} == {"move", "click:unpin"}
    assert {item["type"] for item in home_mutations} == {"callMcp"}
    assert {item["params"]["application_id"] for item in home_mutations} == {
        "$event.id"
    }
    assert next(item for item in home_mutations if item["on"] == "move")["target"] == (
        "applications.reorder_home"
    )
    assert next(item for item in home_mutations if item["on"] == "move")["params"] == {
        "application_id": "$event.id",
        "to_index": "$event.currentIndex",
        "webspace_id": "$client.webspaceId",
    }
    assert next(
        item for item in home_mutations if item["on"] == "click:unpin"
    )["target"] == "applications.set_home_pin"
    assert next(
        item for item in home_mutations if item["on"] == "click:unpin"
    )["params"] == {
        "application_id": "$event.id",
        "pinned": False,
        "webspace_id": "$client.webspaceId",
    }
    assert not any(
        key in item["params"]
        for item in home_mutations
        for key in ("items", "installed", "installed_state", "uninstall")
    )
    assert all(
        item["invalidates"] == ["web_desktop.applications", "web_desktop.home_widgets"]
        for item in home_mutations
    )
    assert {item["on"] for item in widget_mutations} == {"move", "click:unpin"}
    assert {item["params"]["item_type"] for item in widget_mutations} == {"widget"}
    assert all(
        item["invalidates"] == ["web_desktop.home_widgets"]
        for item in widget_mutations
    )


def test_device_pairing_is_owned_by_desktop_runtime_component() -> None:
    webui = _load("webui.json")
    actions = _widget(webui, "devices-actions")["actions"]
    prepare = [
        item
        for item in actions
        if item.get("id") in {
            "preparePairBrowser",
            "preparePairTelegram",
            "preparePairNode",
        }
    ]

    assert len(prepare) == 3
    assert {item["target"] for item in prepare} == {
        "web_desktop_runtime_skill.prepare_connection"
    }
    assert {item["params"]["mode"] for item in prepare} == {
        "browser",
        "telegram",
        "node",
    }
    assert {tuple(item["invalidates"]) for item in prepare} == {
        ("users_access.summary",)
    }
    assert not any(
        "adaos_connect" in str(node.get("target", ""))
        for node in _walk(webui)
    )


def test_chat_uses_registered_state_bound_general_channel() -> None:
    webui = _load("webui.json")
    page = webui["ui"]["application"]["desktop"]["pageSchema"]
    primary_nav = _widget(webui, "primary-nav")
    selector = _widget(webui, "chat-channel-selector")
    agent_selector = _widget(webui, "chat-agent-selector")
    voice = _widget(webui, "chat-voice-input")
    chat = _widget(webui, "desktop-chat")

    assert page["initialState"]["chatChannel"] == "general"
    assert page["initialState"]["chatAgentId"] == "agent:core:general"
    assert selector["inputs"]["buttons"][0] == {
        "id": "general",
        "label": "General",
        "label_i18n": "web_desktop.chat.channel.general",
    }
    assert _actions(selector, "click:general")[0]["params"] == {
        "chatChannel": "general",
        "chatAgentId": "agent:core:general",
    }
    assert _actions(selector, "click:general")[1] == {
        "on": "click:general",
        "type": "callHost",
        "target": "dialog.channel.select",
        "params": {
            "channel_id": "general",
            "webspace_id": "$client.webspaceId",
        },
    }
    assert _actions(primary_nav, "click:chat")[1] == {
        "on": "click:chat",
        "type": "callHost",
        "target": "dialog.channel.select",
        "params": {
            "channel_id": "general",
            "webspace_id": "$client.webspaceId",
        },
    }
    assert chat["dataSource"]["params"]["dialog_channel_id"] == "$state.chatChannel"
    assert chat["dataSource"]["params"]["active_agent_id"] == "$state.chatAgentId"
    assert chat["inputs"]["meta"]["dialog_channel_id"] == "$state.chatChannel"
    assert chat["inputs"]["meta"]["active_agent_id"] == "$state.chatAgentId"
    assert agent_selector["visibleIf"].endswith("$state.chatChannel === 'family'")
    assert {item["id"] for item in agent_selector["inputs"]["buttons"]} == {
        "agent:conversation_companions:arseni",
        "agent:conversation_companions:nika",
        "agent:conversation_companions:mira",
    }
    assert voice["type"] == "ui.voiceInput"
    assert voice["inputs"]["meta"]["active_agent_id"] == "$state.chatAgentId"
    assert chat["inputs"]["sendCommand"] == "voice.chat.user"
    assert chat["inputs"]["sendResultStateKey"] == "lastChatSend"
    assert chat["inputs"]["sendPendingStateKey"] == "chatSendPending"
    assert chat["inputs"]["sendErrorStateKey"] == "chatSendError"
    assert chat["inputs"]["resultDelivery"] == "conversation"
    assert chat["inputs"]["retry"] == {
        "enabled": True,
        "command": "voice.chat.user",
    }
    assert {
        "lastChatSend": {},
        "chatSendPending": False,
        "chatSendError": "",
    }.items() <= page["initialState"].items()


def test_owned_operational_surfaces_use_real_tool_or_admitted_mcp_sources() -> None:
    webui = _load("webui.json")
    allowed_sources = {
        "web_desktop_runtime_skill.list_devices",
        "web_desktop_runtime_skill.get_system_overview",
        "web_desktop_runtime_skill.list_developments",
        "web_desktop_runtime_skill.get_preferences",
    }

    for node in _walk(webui):
        source = node.get("dataSource")
        if not isinstance(source, dict):
            continue
        if source.get("kind") == "skill":
            assert source["name"] in allowed_sources
            assert source["cacheTtlMs"] == 0
            assert source["preserveLastValue"] is True
        if source.get("kind") == "mcp":
            assert source["toolId"] in {
                "users_access.summary",
                "users_access.current_profile",
            }
            if source["toolId"] == "users_access.summary":
                assert source["dryRun"] is True
        if source.get("kind") == "y":
            assert source.get("transform") in {"desktop.icons", "desktop.widgets"}


def test_settings_writes_use_authoritative_profile_and_scoped_preference_tools() -> None:
    webui = _load("webui.json")
    forms = [
        item
        for item in _widgets(webui)
        if item.get("type") == "ui.form" and item.get("id", "").startswith("settings-")
    ]

    assert forms
    for form in forms:
        source = form["dataSource"]
        if form["id"] == "settings-form-account":
            assert source["toolId"] == "users_access.current_profile"
            submit_actions = [item for item in form["actions"] if item["type"] == "callMcp"]
            assert submit_actions[0]["target"] == "users_access.update_current_profile"
            assert submit_actions[0]["invalidates"] == ["users_access.current_profile"]
        else:
            assert source["name"] == "web_desktop_runtime_skill.get_preferences"
            submit_actions = [item for item in form["actions"] if item["type"] == "callSkill"]
            assert submit_actions
            for action in submit_actions:
                assert action["target"] == "web_desktop_runtime_skill.update_preferences"
                assert action["invalidates"] == ["web_desktop.preferences"]


def test_compact_master_record_lists_keep_direct_detail_activation() -> None:
    webui = _load("webui.json")
    variants = webui["ui"]["application"]["desktop"]["pageSchema"]["layout"]["variants"]

    detail_variants = [
        variant
        for variant in variants
        if variant["id"] in {"devices-variant", "activity-variant", "development-variant"}
    ]
    assert detail_variants
    for variant in detail_variants:
        assert variant["interaction"]["rowActivation"] == "open-detail"
        assert any(
            region["presentation"].get("compact") == "sheet"
            for region in variant["regions"]
        )
