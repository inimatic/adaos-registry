import json
from pathlib import Path


SCENARIO_DIR = Path(__file__).resolve().parents[1]


def test_current_builder_uses_trusted_related_workspaces():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    widget = next(n for n in _walk(document) if n.get("id") == "development-current-application")
    assert widget["dataSource"]["name"] == "web_desktop_runtime_skill.get_current_builder_application"
    assert widget["dataSource"]["preserveLastValue"] is False
    assert [a["id"] for a in widget["actions"]] == ["open_builder", "open_preview"]
    assert [f["key"] for f in widget["inputs"]["fields"]] == ["title", "stage", "revision"]
    for action, kind in zip(widget["actions"], ("builder", "preview")):
        prefix = "current" + kind.title()
        bindings = widget["inputs"]["stateBindings"]
        assert bindings[prefix + "Available"] == {"path": kind + ".available", "default": False}
        assert bindings[prefix + "WebspaceId"] == {"path": kind + ".webspace_id", "default": ""}
        assert action["type"] == "openWorkspace"
        assert "target" not in action
        assert action["params"] == {"workspaceId": "$state." + prefix + "WebspaceId"}
        assert action["enabledIf"] == f"$state.{prefix}Available === true && $state.{prefix}WebspaceId"


def test_development_usage_is_live_without_changing_chart_or_policy():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    widget = next(n for n in _walk(document) if n.get("id") == "development-subscription-spend")
    assert widget["dataSource"]["kind"] == "skill"
    assert widget["dataSource"]["name"] == "web_desktop_runtime_skill.get_system_overview"
    assert widget["dataSource"]["params"] == {"section": "system_bootstrap"}
    assert widget["type"] == "visual.multiMetricArc"
    assert widget["inputs"]["metricsPath"] == "usage_arc.metrics"
    assert widget["visibleIf"] == "$state.activeTab === 'dev' && $state.developmentAccess === true"
    assert widget["actions"] == []


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_secondary_sections_defer_all_skill_reads_until_materialization():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    widgets = [n for n in _walk(document) if n.get("id", "").startswith(
        ("devices-", "system-", "development-", "settings-"))]
    # Accordion lazy sources already wait for explicit expansion; this policy
    # governs mounted widget reads, including the initially selected tabs.
    sources = [widget["dataSource"] for widget in widgets
               if widget.get("dataSource", {}).get("kind") == "skill"]
    assert sources
    for source in sources:
        assert source.get("startPolicy") == "materialization_ready", source.get("name")


def test_preferences_reads_share_one_deduplicable_source():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    sources = [n for n in _walk(document)
               if n.get("name") == "web_desktop_runtime_skill.get_preferences"]
    assert len(sources) >= 2
    assert all(source == sources[0] for source in sources)


def test_node_rename_preserves_selected_member_binding_and_guard():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    widget = next(n for n in _walk(document) if n.get("id") == "system-node-inline-name")
    action = next(a for a in widget["actions"] if a["id"] == "save_node_name")
    assert action["type"] == "callSkill"
    assert action["target"] == "web_desktop_runtime_skill.rename_selected_node"
    assert action["params"] == {
        "display_name": "$state.selectedSystemNodeName",
        "target_node_id": "$state.selectedSystemNodeId",
    }
    assert action["resultStateKey"] == "lastSystemAction"
    assert action["invalidates"] == ["system.members"]
    button = next(b for b in widget["inputs"]["buttons"] if b["id"] == "save_node_name")
    assert button["enabledIf"] == (
        "$state.selectedSystemNode.id === $state.selectedSystemNodeId && "
        "$state.selectedSystemNode.active === true")


def test_update_controls_use_governed_selected_member_mutations() -> None:
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    nodes = list(_walk(document))
    for widget_id in ("system-update-actions", "system-autoupdate-enabled"):
        widget = next(node for node in nodes if node.get("id") == widget_id)
        for action in widget["actions"]:
            expected = "request_core_update" if action["id"] == "apply_update" else "set_core_autoupdate"
            assert action["type"] == "callSkill"
            assert action["target"] == "web_desktop_runtime_skill." + expected
            assert action["params"]["target_node_id"] == "$state.selectedSystemNodeId"
            assert "request_id" not in action["params"]
            if expected == "set_core_autoupdate":
                assert action["params"]["enabled"] == "$event.value"
        controls = widget["inputs"]["buttons"] + [
            f for f in widget["inputs"]["fields"] if f["id"] == "auto_update"]
        for control in controls:
            assert control["enabledIf"] == (
                "$state.selectedSystemNode.id === $state.selectedSystemNodeId && "
                "$state.selectedSystemNode.active === true")


def test_legacy_aggregate_runtime_toolbar_is_not_rendered() -> None:
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    toolbars = [node for node in _walk(document) if node.get("id") == "runtime-control-actions"]
    assert all(toolbar.get("visibleIf") == "false" for toolbar in toolbars)


def test_installed_skills_reads_the_selected_member():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    widget = next(n for n in _walk(document) if n.get("id") == "system-current-node-details")
    source = widget["dataSource"]
    assert source["kind"] == "skill"
    assert source["name"] == "web_desktop_runtime_skill.get_system_overview"
    assert source["params"] == {"section": "node_dashboard", "target_node_id": "$state.selectedSystemNodeId"}
    assert source["preserveLastValue"] is True
    assert [field["path"] for field in widget["inputs"]["fields"]] == [
        "node_details.id", "node_details.status", "node_details.apps_count"]


def test_progressive_reads_keep_accepted_sections_and_load_on_open():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    widget = next(n for n in _walk(document) if n.get("id") == "system-progressive-details")
    records = widget["dataSource"]["value"]
    assert [(r["id"], r["expanded"]) for r in records] == [("recent", True), ("technical", False)]
    assert all(r["content"] == "" for r in records)
    for key, section in (("recent", "activity"), ("technical", "technical")):
        lazy = widget["inputs"]["lazyDataSources"][key]
        assert lazy["resultPath"] == "item"
        source = lazy["source"]
        assert source["name"] == "web_desktop_runtime_skill.get_system_overview"
        assert source["params"] == {"section": section, "target_node_id": "$state.selectedSystemNodeId"}
        assert source["preserveLastValue"] is True
        assert source["maxRequestHz"] == 0.2


def test_system_first_paint_coalesces_dashboard_reads():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    widgets = {n["id"]: n for n in _walk(document) if n.get("id")}
    subnet_sources = [
        widgets[identity]["dataSource"]
        for identity in ("system-member-tabs", "system-applications-summary", "system-subnet-details")
    ]
    assert all(source == subnet_sources[0] for source in subnet_sources)
    assert subnet_sources[0]["kind"] == "skill"
    assert subnet_sources[0]["name"] == "web_desktop_runtime_skill.get_system_overview"
    assert subnet_sources[0]["params"] == {"section": "system_bootstrap"}
    assert subnet_sources[0]["startPolicy"] == "materialization_ready"
    node_sources = [
        widgets[identity]["dataSource"]
        for identity in (
            "system-current-node-details", "system-update-status-hub",
            "system-update-status-studio", "system-attention-dashboard-tile",
        )
    ]
    expected = {"section": "node_dashboard", "target_node_id": "$state.selectedSystemNodeId"}
    assert all(source["params"] == expected for source in node_sources)
    hardware_source = widgets["system-hardware-utilization"]["dataSource"]
    assert hardware_source["kind"] == "skill"
    assert hardware_source["name"] == "web_desktop_runtime_skill.get_system_overview"
    assert hardware_source["params"] == {
        "section": "node_dashboard",
        "target_node_id": "$state.selectedSystemNodeId",
    }
    assert hardware_source["invalidationTags"] == [
        "web_desktop.system", "system.members", "system.hardware"]
    hardware_inputs = widgets["system-hardware-utilization"]["inputs"]
    assert hardware_inputs["metricsPath"] == "hardware.hardware_metrics"
    assert hardware_inputs["centerValuePath"] == "hardware.center.value"
    assert hardware_inputs["centerLabelPath"] == "hardware.center.label"
    assert hardware_inputs["subtitlePath"] == "hardware.subtitle"


def test_subscription_widget_has_one_read_path():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    widget = next(n for n in _walk(document) if n.get("id") == "system-subscription-tile")
    assert widget["dataSource"]["kind"] == "skill"
    assert widget["dataSource"]["name"] == "web_desktop_runtime_skill.get_system_overview"
    assert widget["dataSource"]["params"] == {"section": "system_bootstrap"}
    assert not any(action.get("on") == "mount" for action in widget["actions"])


def test_chat_scroll_is_confined_to_the_full_height_message_feed():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    page = document["ui"]["application"]["desktop"]["pageSchema"]
    chat = next(widget for widget in page["widgets"] if widget["type"] == "ui.chat")
    variants = [variant for variant in page["layout"]["variants"]
                if variant.get("when") == chat["visibleIf"]]
    assert len(variants) == 1
    layout = variants[0]
    assert layout["scroll"] == "regions"
    main = [region for region in layout["regions"] if region["role"] == "main"]
    assert len(main) == 1
    assert chat["area"] == main[0]["id"]
    assert chat["inputs"]["fillHeight"] is True
    assert [widget for widget in page["widgets"]
            if widget.get("area") == main[0]["id"]] == [chat]
    assert {region["role"] for region in layout["regions"]} == {
        "toolbar", "navigation", "main"}
    for region in layout["regions"]:
        assert region["scroll"] == "none"
        assert region["presentation"] == {"compact": "stack", "wide": "pane"}
    selectors = [widget for widget in page["widgets"]
                 if widget.get("type") == "navigation.tabs"
                 and widget.get("inputs", {}).get("selectedStateKey")
                 in {"chatChannel", "chatAgentId"}]
    assert selectors == []
    conversation = chat["inputs"]["conversation"]
    assert conversation["agent"]["mode"] == "select"
    assert conversation["history"] == {
        "mode": "auto", "allAgents": True, "defaultAll": True,
        "maxMessages": 200,
    }


def test_projects_preference_is_user_owned_and_opening_work_requires_confirmation():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    widgets = {node["id"]: node for node in _walk(document) if node.get("id")}
    settings = widgets["settings-form-applications"]
    field = next(item for item in settings["inputs"]["fields"]
                 if item.get("id") == "showProjects")
    assert field["type"] == "toggle"
    assert field["stateKey"] == "prefShowProjects"
    assert "Pinned projects" in field["description"]
    assert settings["dataSource"]["name"] == "web_desktop_runtime_skill.get_preferences"
    save = next(action for action in settings["actions"] if action["id"] == "saveApplications")
    assert save["target"] == "web_desktop_runtime_skill.update_preferences"

    continuation = widgets["home-continue-list"]
    action = next(item for item in continuation["actions"] if item["id"] == "open")
    assert "$event.title" in action["confirmation"]["message"]
    assert action["confirmation"]["confirmLabel"] == "Open $event.title"


def test_chat_agent_picker_is_owned_by_the_shared_conversation_surface():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    page = document["ui"]["application"]["desktop"]["pageSchema"]
    chat = next(widget for widget in page["widgets"] if widget["type"] == "ui.chat")
    assert not any(widget.get("id") in {"chat-agent-selector", "chat-channel-selector"}
                   for widget in page["widgets"])
    assert chat["inputs"]["activeAgentStateKey"] == "chatAgentId"
    assert chat["inputs"]["conversation"]["agent"] == {
        "mode": "select",
        "source": {
            "kind": "y",
            "path": "data/dialog",
            "observe": "dataRoot",
        },
    }


def test_avatar_change_autosaves_and_invalidates_the_profile():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    widget = next(n for n in _walk(document) if n.get("id") == "settings-form-account")
    actions = [a for a in widget["actions"] if a.get("on") == "change:avatar_ref"]
    assert len(actions) == 1
    action = actions[0]
    assert action["type"] == "callMcp"
    assert action["target"] == "users_access.update_current_profile"
    assert action["params"] == {"avatar_ref": "$event.value"}
    assert action["invalidates"] == ["users_access.current_profile"]
    assert widget["dataSource"]["invalidationTags"] == action["invalidates"]


def test_account_profile_uses_one_invalidation_aware_read_and_full_timezone_catalog():
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    widget = next(n for n in _walk(document) if n.get("id") == "settings-form-account")
    source = widget["dataSource"]
    assert source == {
        "kind": "mcp",
        "toolId": "users_access.current_profile",
        "resultPath": "response.result.profile",
        "cacheTtlMs": 0,
        "maxRequestHz": 1,
        "invalidationTags": ["users_access.current_profile"],
        "preserveLastValue": True,
    }
    timezone = next(field for field in widget["inputs"]["fields"]
                    if field.get("id") == "timezone")
    values = {option["value"] for option in timezone["options"]}
    assert {"Europe/Moscow", "Europe/Berlin", "Asia/Tokyo"} <= values
    assert len(values) >= 400


def test_ai_usage_widgets_present_reported_24h_7d_30d_windows(monkeypatch):
    import importlib.util

    # Cross-artifact presentation belongs here, not in the standalone skill suite.
    path = SCENARIO_DIR.parents[1] / "skills/web_desktop_runtime_skill/handlers/usage.py"
    spec = importlib.util.spec_from_file_location("management_usage_contract", path)
    usage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(usage)
    monkeypatch.setattr(usage.access, "require", lambda capability: None)
    monkeypatch.setattr(usage.subscriptions, "get_codex_usage_snapshot", lambda **kwargs: {
        "status": "ready", "used_tokens": 10, "used_7d_tokens": 50,
        "used_30d_tokens": 100,
    })
    result = usage.get_subscription_usage()
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))
    widgets = {n["id"]: n for n in _walk(document) if n.get("id")}
    for identity in ("system-subscription-tile", "development-subscription-spend"):
        widget = widgets[identity]
        assert widget["title"] == "AI usage"
        assert widget["type"] == "visual.multiMetricArc"
        assert widget["dataSource"]["kind"] == "skill"
        assert widget["dataSource"]["name"] == "web_desktop_runtime_skill.get_system_overview"
        assert widget["dataSource"]["params"] == {"section": "system_bootstrap"}
        for key, field in (("metricsPath", "metrics"), ("centerValuePath", "value"),
                           ("centerLabelPath", "label"), ("subtitlePath", "subtitle"),
                           ("descriptionPath", "description")):
            assert widget["inputs"][key] == "usage_arc." + field
            assert field in result["usage_arc"]
    arc = result["usage_arc"]
    assert [(m["label"], m["display"], m["value"]) for m in arc["metrics"]] == [
        ("Last 7 days", "50 tokens", 50.0),
        ("Last 30 days", "100 tokens", 100.0),
        ("Last 24 hours", "10 tokens", 10.0),
    ]
    assert (arc["label"], arc["value"]) == ("Last 30 days", "100 tokens")
