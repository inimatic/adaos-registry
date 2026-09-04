from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
def _load(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8-sig"))


def _walk(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _by_id(webui: dict) -> dict[str, dict]:
    return {node["id"]: node for node in _walk(webui) if isinstance(node.get("id"), str)}


def test_scenario_yaml_is_the_projection_source_of_truth() -> None:
    scenario = _load("scenario.json")
    webui = _load("webui.json")
    manifest = yaml.safe_load((ROOT / "scenario.yaml").read_text(encoding="utf-8"))

    assert re.fullmatch(r"(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){2}", manifest["version"])
    datetime.fromisoformat(str(manifest["updated_at"]).replace("Z", "+00:00"))
    assert scenario["version"] == webui["ui"]["version"] == manifest["version"]
    assert scenario["updated_at"] == manifest["updated_at"]
    required = ["builder_skill", "builder_sdk_control_skill", "voice_chat_skill"]
    assert scenario["depends"] == manifest["depends"] == required
    assert scenario["runtime"]["skills"]["required"] == required
    assert manifest["runtime"]["skills"]["required"] == required
    assert manifest["ui"]["manifest"] == scenario["ui"]["manifest"] == "webui.json"
    assert "nlu" in scenario and "slots" in scenario


def test_builder_declares_companion_skill_runtime_bindings() -> None:
    manifest = yaml.safe_load((ROOT / "scenario.yaml").read_text(encoding="utf-8"))
    scenario = _load("scenario.json")

    required = ["builder_skill", "builder_sdk_control_skill", "voice_chat_skill"]
    assert manifest["depends"] == scenario["depends"] == required
    assert manifest["runtime"]["skills"]["required"] == required
    assert scenario["runtime"]["skills"]["required"] == required


def test_ui_preserves_stabilized_three_panel_surface_and_modals() -> None:
    webui = _load("webui.json")
    page = webui["ui"]["application"]["desktop"]["pageSchema"]
    ids = set(_by_id(webui))

    assert page["layout"]["type"] == "split"
    assert [area["id"] for area in page["layout"]["areas"]] == ["left", "center", "right"]
    assert {"llm-profile", "llmModel", "provider", "voice-input"} <= ids
    assert "confirm-subscription-update" in webui["ui"]["application"]["modals"]
    assert page["meta"]["builder"]["functional"] is True
    assert page["meta"]["builder"]["binding_mode"] == "skill"


def test_builder_observes_project_scoped_development_feedback() -> None:
    webui = _load("webui.json")
    widgets = _by_id(webui)
    tabs = widgets["node-views"]["inputs"]["buttons"]
    feedback_list = widgets["development-feedback-list"]
    feedback_detail = widgets["development-feedback-detail"]

    assert any(item["id"] == "feedback" for item in tabs)
    assert feedback_list["visibleIf"] == "$state.activeView === 'feedback'"
    assert feedback_list["dataSource"]["name"] == (
        "builder_sdk_control_skill.list_development_feedback"
    )
    assert feedback_list["dataSource"]["params"]["object_type"] == (
        "$state.selectedProjectKind"
    )
    assert feedback_list["dataSource"]["params"]["object_id"] == (
        "$state.selectedProjectId"
    )
    assert feedback_list["dataSource"]["maxRequestHz"] == 0.2
    assert {item["stateKey"] for item in feedback_list["inputs"]["filters"]} == {
        "developmentFeedbackStatus",
        "developmentFeedbackCategory",
        "developmentFeedbackSource",
        "developmentFeedbackRejectionClass",
    }
    feedback_filters = widgets["development-feedback-filters"]
    source_field = next(
        item
        for item in feedback_filters["inputs"]["fields"]
        if item["id"] == "feedback-source"
    )
    assert {item["value"] for item in source_field["options"]} >= {
        "",
        "codex",
        "validator",
        "pre_codex_llm",
        "human_review",
    }
    category_field = next(
        item
        for item in feedback_filters["inputs"]["fields"]
        if item["id"] == "feedback-category"
    )
    assert "result_rejected" in {
        item["value"] for item in category_field["options"]
    }
    rejection_field = next(
        item
        for item in feedback_filters["inputs"]["fields"]
        if item["id"] == "feedback-rejection-class"
    )
    assert {item["value"] for item in rejection_field["options"]} >= {
        "",
        "requirement_ambiguity",
        "builder_misread_user",
        "sdk_doc_ambiguity",
        "sdk_capability_gap",
        "weak_patch",
        "insufficient_validation",
    }
    assert feedback_detail["dataSource"]["name"] == (
        "builder_sdk_control_skill.get_development_feedback"
    )
    assert feedback_detail["dataSource"]["params"]["feedback_id"] == (
        "$state.selectedDevelopmentFeedbackId"
    )
    assert feedback_detail["dataSource"]["maxRequestHz"] == 0.2
    assert feedback_detail["dataSource"]["preserveLastValue"] is False
    assert {
        "contract_ref",
        "operation_id",
        "input_summary",
        "expected_behavior",
        "observed_behavior",
        "validation_result",
        "user_response",
    } <= {item["key"] for item in feedback_detail["inputs"]["fields"]}


def test_page_state_is_initialized_from_exact_builder_selection_projection() -> None:
    page = _load("webui.json")["ui"]["application"]["desktop"]["pageSchema"]

    assert page["initialStateSource"] == {
        "kind": "y",
        "path": "data/builder/selection",
        "mapping": {
            "selectedProjectKind": "object_type",
            "selectedProjectId": "object_id",
            "selectedProjectRef": "ref",
            "selectedProjectTitle": "title",
            "selectedObjectKind": "object_type",
            "selectedObjectId": "object_id",
            "project.title": "title",
            "project.description": "description",
            "project.type": "object_type",
            "builderTopicId": "conversation_topic_id",
            "builderThreadId": "conversation_thread_id",
        },
    }


def test_functional_builder_uses_real_contracts_and_explicit_preview_labels() -> None:
    webui = _load("webui.json")
    nodes = list(_walk(webui))
    actions = [node for node in nodes if node.get("type") in {"callSkill", "updateState", "openModal", "closeModal"}]
    sources = [node["dataSource"] for node in nodes if "dataSource" in node]
    labels = webui["ui"]["application"]["desktop"]["pageSchema"]["meta"]["builder"]

    assert actions and any(action["type"] == "callSkill" for action in actions)
    assert sources and any(source.get("kind") == "skill" for source in sources)
    assert all(value.startswith("builder_sdk_control_skill.") for value in labels["typed_contracts"].values())
    assert labels["proto"].startswith("proto:")
    assert labels["active"].startswith("active:")
    assert labels["public"].startswith("public:")


def test_project_picker_lists_installed_projects_and_selects_once(monkeypatch) -> None:
    webui = _load("webui.json")
    page = webui["ui"]["application"]["desktop"]["pageSchema"]
    widgets = _by_id(webui)
    picker = widgets["project-picker-list"]

    assert picker["dataSource"] == {
        "kind": "skill",
        "name": "builder_sdk_control_skill.list_projects",
        "scope": "local",
        "params": {
            "limit": 50,
            "selected_object_type": "$state.selectedProjectKind",
            "selected_object_id": "$state.selectedProjectId",
            "include_archived": "$state.projectPickerArchived",
            "_meta": {"current_scenario": "builder"},
        },
        "cacheTtlMs": 0,
        "invalidationTags": ["builder.project.catalog"],
        "preserveLastValue": True,
    }
    assert picker["inputs"]["disableImplicitScenarioSelect"] is True

    select_state = next(
        action["params"]
        for action in picker["actions"]
        if action.get("type") == "updateState"
    )
    assert select_state["selectedProjectKind"] == "$event.object_type"
    assert select_state["selectedProjectId"] == "$event.object_id"
    assert select_state["selectedObjectKind"] == "$event.target_object_type"
    assert select_state["selectedObjectId"] == "$event.target_object_id"

    state = page["initialState"]
    resolved_params = {
        key: state[value.removeprefix("$state.")] if isinstance(value, str) and value.startswith("$state.") else value
        for key, value in picker["dataSource"]["params"].items()
    }
    assert resolved_params == {
        "limit": 50,
        "selected_object_type": "scenario",
        "selected_object_id": "builder",
        "include_archived": False,
        "_meta": {"current_scenario": "builder"},
    }
    handler_path = ROOT.parents[1] / "skills" / "builder_sdk_control_skill" / "handlers" / "main.py"
    spec = importlib.util.spec_from_file_location("builder_picker_contract_handler", handler_path)
    assert spec and spec.loader
    handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler)
    monkeypatch.setattr(
        handler.compositions,
        "list_projects",
        lambda **_kwargs: [
            {
                "kind": "project",
                "id": "builder",
                "title": "Builder",
                "description": "Workbench",
                "version": "DEV",
                "components": {
                    "owned": [
                        {
                            "ref": "scenario:builder",
                            "role": "primary",
                        }
                    ]
                },
            }
        ],
    )
    projects = handler.list_projects(**resolved_params)
    assert projects
    assert projects[0]["id"] == "project:builder"
    assert projects[0]["primary_ref"] == "scenario:builder"
    assert projects[0]["current"] is True

    calls = [item for item in picker["actions"] if item.get("type") == "callSkill"]
    updates = [item for item in picker["actions"] if item.get("type") == "updateState"]
    assert len(calls) == 1
    assert calls[0]["target"] == "builder_sdk_control_skill.select_preview"
    assert calls[0]["scope"] == "local"
    assert len(updates) == 1
    assert updates[0]["params"]["selectedProjectId"] == "$event.object_id"
    assert updates[0]["params"]["builderTopicId"] == "$event.conversation_topic_id"
    assert updates[0]["params"]["builderThreadId"] == "$event.conversation_thread_id"
    assert any(item.get("type") == "closeModal" for item in picker["actions"])
    assert any(item.get("on") == "add" for item in picker["actions"])
    assert picker["inputs"]["addButtonFirst"] is True
    toggles = picker["inputs"]["toolbarToggles"]
    if isinstance(toggles, dict):
        toggles = [toggles]
    assert toggles[0]["stateKey"] == "projectPickerArchived"


def test_project_creation_selects_aggregate_and_primary_component() -> None:
    form = _by_id(_load("webui.json"))["new-project-form"]
    selected = next(
        action["params"]
        for action in form["actions"]
        if action.get("type") == "updateState"
        and "selectedProjectKind" in action.get("params", {})
    )

    assert selected["selectedProjectKind"] == "project"
    assert selected["selectedProjectId"] == "$event.values.object_id"
    assert selected["selectedProjectRef"] == "project:$event.values.object_id"
    assert selected["selectedObjectKind"] == "$event.values.object_type"
    assert selected["selectedObjectId"] == "$event.values.object_id"


def test_process_inspection_is_separate_from_the_canonical_conversation() -> None:
    webui = _load("webui.json")
    widgets = _by_id(webui)
    lifecycle = widgets["project-tree"]
    process = widgets["process-tree"]

    assert lifecycle["dataSource"]["kind"] == "skill"
    assert lifecycle["dataSource"]["name"] == "builder_sdk_control_skill.get_lifecycle"
    assert lifecycle["visibleIf"] == "$state.processPinned === true"
    assert process["dataSource"]["name"] == "builder_sdk_control_skill.get_process_tree"
    assert "selectedLifecycleStage" not in widgets["builder-chat"]["visibleIf"]
    assert any(
        action.get("target") == "builder_sdk_control_skill.inspect_process_ref"
        for action in process["actions"]
    )
    assert any(
        action.get("target") == "builder_sdk_control_skill.select_preview_target"
        for action in process["actions"]
    )
    process_buttons = {item["id"] for item in process["inputs"]["buttons"]}
    assert process_buttons == {"show-preview", "open-placement"}
    placement_action = next(
        action
        for action in process["actions"]
        if action.get("target")
        == "builder_sdk_control_skill.get_project_placement_navigation"
    )
    assert placement_action["openResultUrl"] is True
    assert placement_action["resultUrlPath"] == "preview_url"
    assert placement_action["resultPreferCurrentOrigin"] is True
    assert "automation" in widgets["automation-conversation-followup"]["visibleIf"]
    assert "publication" in widgets["publication-workspace-actions"]["visibleIf"]
    lifecycle_buttons = {
        item["id"] for item in lifecycle["inputs"]["buttons"]
    }
    assert lifecycle_buttons == {
        "show-preview", "make-current", "stabilize",
        "go-automation", "go-publication",
    }
    # Trial is a dependent delivery gate, never an independently mutable phase.
    source = json.dumps(webui, ensure_ascii=False).lower()
    assert "automation" in source and "trial" in source and "publication" in source


def test_ui_revision_and_artifact_versions_have_explicit_non_stale_labels() -> None:
    webui = _load("webui.json")
    labels = webui["ui"]["application"]["desktop"]["pageSchema"]["meta"]["builder"]

    assert labels["ui_revision"] == "058"
    assert labels["proto"] == "proto:058"
    assert labels["active"] == "active:current"
    assert labels["public"] == "public:current"
    assert "proto:045" not in json.dumps(webui, ensure_ascii=False)


def test_prototype_declares_no_network_device_or_credential_transport() -> None:
    webui = _load("webui.json")
    nodes = list(_walk(webui))

    forbidden_keys = {"endpoint", "headers", "token", "credential", "secret", "deviceId"}
    assert all(not (forbidden_keys & node.keys()) for node in nodes)
    assert all(node.get("transport") in {None, "none", "hub"} for node in nodes)
    assert not re.search(r"https?://", json.dumps(webui, ensure_ascii=False))


def test_durable_chat_abi_and_stage_surfaces_are_exact() -> None:
    manifest = yaml.safe_load((ROOT / "scenario.yaml").read_text(encoding="utf-8"))
    widgets = _by_id(_load("webui.json"))
    chat = widgets["builder-chat"]
    assert "voice_chat_skill" in manifest["runtime"]["skills"]["required"]
    assert chat["dataSource"] == {
        "kind": "stream",
        "receiver": "voice_chat.messages",
        "scope": "shared",
        "transport": "hub",
        "params": {
            "conversation_id": "$state.builderConversationId",
            "conversation_topic_id": "$state.builderTopicId",
            "dialog_channel_id": "builder",
        },
    }
    assert chat["inputs"]["sendCommand"] == "voice.chat.user"
    assert chat["inputs"]["meta"]["active_agent_id"] == "agent:builder_skill:builder"
    assert chat["actions"] == []
    assert "$state.activeView === 'conversation'" in chat["visibleIf"]
    assert "$state.selectedLifecycleStage" not in chat["visibleIf"]
    assert widgets["interaction-status"]["dataSource"]["name"] == (
        "builder_sdk_control_skill.get_interaction_frame"
    )
    assert any(
        action.get("params", {}).get("modalId") == "process"
        for action in widgets["context-actions"]["actions"]
    )
    for widget_id in (
        "chat-side-settings",
        "automation-conversation-start", "automation-conversation-followup",
        "automation-conversation-state", "automation-return-to-prototype",
        "publication-workspace-actions", "publication-workspace-history",
        "publication-workspace-status",
    ):
        assert "$state.activeView === 'conversation'" in widgets[widget_id]["visibleIf"]
    publication_targets = {
        item.get("target")
        for item in widgets["publication-workspace-actions"]["actions"]
        if item.get("type") == "callSkill"
    }
    assert "builder_sdk_control_skill.push_project" in publication_targets
    assert "builder_sdk_control_skill.publish_project" in publication_targets


def test_long_project_title_owns_the_header_and_context_moves_left() -> None:
    widgets = _by_id(_load("webui.json"))
    header_buttons = widgets["project-header"]["inputs"]["buttons"]
    left_buttons = widgets["left-actions"]["inputs"]["buttons"]

    assert [item["id"] for item in header_buttons] == ["project-label"]
    assert header_buttons[0]["label"] == "$state.selectedProjectTitle"
    left_by_id = {item["id"]: item for item in left_buttons}
    assert left_by_id["change-context"]["label"] == "$state.changeLabel"
    assert left_by_id["preview-context"]["label"] == "$state.previewViewingLabel"
    assert left_by_id["change-context"]["disabled"] is True
    assert left_by_id["preview-context"]["disabled"] is True


def test_no_deprecated_update_or_automatic_state_change_retry_surface() -> None:
    webui = _load("webui.json")
    calls = [node for node in _walk(webui) if node.get("type") == "callSkill"]
    targets = {str(node.get("target") or "") for node in calls}

    assert "builder_sdk_control_skill.update_project" not in targets
    assert not any(target.endswith(".pull_project") for target in targets)
    assert "builder_sdk_control_skill.update_project_metadata" in targets
    assert all("retry" not in node and "retries" not in node for node in calls)
    assert "confirm-subscription-update" in webui["ui"]["application"]["modals"]


def test_embedded_functional_parity_contract_is_satisfied() -> None:
    webui = _load("webui.json")
    contract = _load("assets/builder_functional_parity.json")
    application = webui["ui"]["application"]
    widgets = _by_id(webui)
    bindings = set()
    for node in _walk(webui):
        if node.get("type") == "callSkill" and node.get("target"):
            bindings.add(node["target"])
        if node.get("kind") == "skill" and node.get("name"):
            bindings.add(node["name"])
        if node.get("kind") == "stream" and node.get("receiver"):
            bindings.add(f"stream:{node['receiver']}")

    required_bindings = set(contract["required_bindings"])
    required_bindings.update(contract["forward_required_bindings"])
    assert set(contract["required_widget_ids"]) <= set(widgets)
    assert set(contract["required_modal_ids"]) <= set(application["modals"])
    assert required_bindings <= bindings
    assert not (set(contract["forbidden_bindings"]) & bindings)
    lifecycle_buttons = {
        item["id"] for item in widgets["project-tree"]["inputs"]["buttons"]
    }
    assert set(contract["required_lifecycle_buttons"]) <= lifecycle_buttons
    create_form = next(
        item
        for item in application["modals"]["new-project"]["schema"]["widgets"]
        if item.get("id") == "new-project-form"
    )
    kind_field = next(
        item for item in create_form["inputs"]["fields"] if item.get("id") == "object_type"
    )
    assert set(contract["required_project_kinds"]) <= {
        item["value"] for item in kind_field["options"]
    }


def test_all_localized_payloads_are_valid_utf8_without_replacement_characters() -> None:
    paths = [ROOT / "scenario.yaml", ROOT / "scenario.json", ROOT / "webui.json", *sorted((ROOT / "assets/i18n").glob("*.json"))]
    for path in paths:
        text = path.read_bytes().decode("utf-8")
        assert "\ufffd" not in text
    assert {path.stem for path in (ROOT / "assets/i18n").glob("*.json")} == {"en", "ru"}
