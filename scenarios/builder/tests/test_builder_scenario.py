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


def test_about_preserves_readme_cas_and_explicit_owned_image_drafts() -> None:
    webui = _load("webui.json")
    widgets = _by_id(webui)
    about = widgets["design-readme"]
    assert about["dataSource"]["name"] == "builder_sdk_control_skill.get_about"
    assert about["dataSource"]["preserveLastValue"] is False
    assert about["inputs"]["stateBindings"] == {"readmeText": "text", "readmeDigest": "digest"}
    fields = {row["key"] for row in about["inputs"]["fields"]}
    assert {"text", "owner_name", "owner_ref", "owner_status"} <= fields
    save = widgets["design-readme-form"]["actions"][0]
    assert save["params"]["expected_digest"] == "$state.readmeDigest"
    generated = widgets["readme-generated-draft"]["actions"][0]
    assert generated["params"]["expected_digest"] == "$state.readmeGeneration.context.base_digest"
    image = widgets["about-icon-generate"]["actions"][0]
    assert image["on"] == "submit" and image["requestIdParam"] == "request_id"
    assert image["params"]["model"] == "$event.values.model"
    assert widgets["about-icon-preview"]["inputs"]["imageKey"] == "media"
    history = widgets["about-icon-history"]
    assert history["dataSource"]["name"] == "builder_sdk_control_skill.list_icon_drafts"
    assert history["dataSource"]["params"]["object_id"] == "$state.selectedProjectId"
    assert history["actions"][0]["target"] == "builder_sdk_control_skill.get_icon_generation"
    for node in _walk(webui):
        if isinstance(node.get("action"), dict):
            assert node["action"].get("target") != "builder_sdk_control_skill.generate_icon"
    for locale in ("en", "ru"):
        strings = _load(f"assets/i18n/workbench-live-{locale}.json")
        for node in _walk(webui):
            if str(node.get("key", "")).startswith("builder.about."):
                assert node["key"] in strings


def test_ui_preserves_accepted_two_area_surface_and_operational_modals() -> None:
    webui = _load("webui.json")
    page = webui["ui"]["application"]["desktop"]["pageSchema"]
    ids = set(_by_id(webui))

    assert page["layout"]["version"] == 2
    assert page["layout"]["pattern"] == "collection"
    assert [region["id"] for region in page["layout"]["regions"]] == ["main", "conversation"]
    assert {"llmModel", "model", "reasoning_effort", "project-picker-table"} <= ids
    assert page["initialState"]["viewProfile"] == "basic"
    assert "samples" not in page["initialState"]
    assert "confirm-subscription-update" in webui["ui"]["application"]["modals"]
    assert "prototype-review" in webui["ui"]["application"]["modals"]
    assert page["meta"]["builder"]["functional"] is True
    assert page["meta"]["builder"]["binding_mode"] == "skill"


def test_prototype_approval_collects_evidence_and_uses_canonical_acceptance() -> None:
    webui = _load("webui.json")
    widgets = _by_id(webui)
    modals = webui["ui"]["application"]["modals"]
    review = modals["prototype-review"]["schema"]["widgets"][0]

    approval_actions = [
        action
        for widget_id, event in (
            ("design-primary-actions", "click:accept"),
            ("project-tree", "click:stabilize"),
        )
        for action in widgets[widget_id]["actions"]
        if action.get("on") == event
    ]
    assert len(approval_actions) == 2
    assert all(action["type"] == "openModal" for action in approval_actions)
    assert all(action["params"]["modalId"] == "prototype-review" for action in approval_actions)

    fields = {item["id"] for item in review["inputs"]["fields"]}
    assert fields == {
        "behavior_evidence_ref",
        "compact_screenshot_ref",
        "wide_screenshot_ref",
    }
    accept = next(action for action in review["actions"] if action["type"] == "callSkill")
    assert accept["target"] == "builder_sdk_control_skill.accept_prototype"
    assert accept["params"]["expected_generation"] == "$state.workflowGeneration"
    assert {item["breakpoint"] for item in accept["params"]["visual_checks"]} == {
        "compact",
        "wide",
    }
    assert "builder_sdk_control_skill.push_project" not in {
        action.get("target") for action in approval_actions
    }


def test_clarification_continuation_uses_supported_enablement_contract() -> None:
    actions = _by_id(_load("webui.json"))["model-clarification-actions"]
    button = next(row for row in actions["inputs"]["buttons"] if row["id"] == "resume")
    action = next(row for row in actions["actions"] if row["on"] == "click:resume")
    assert button["enabledIf"] == action["enabledIf"] == "$state.workbench.clarification.can_resume"
    assert "disabledIf" not in button
    assert action["params"]["expected_generation"] == "$state.workbench.clarification.generation"
    assert action["params"]["confirmed"] is True


def test_builder_observes_project_scoped_development_feedback() -> None:
    webui = _load("webui.json")
    widgets = _by_id(webui)
    tabs = widgets["design-workbench-views"]["inputs"]["buttons"]
    feedback_list = widgets["development-feedback-list"]
    feedback_detail = widgets["development-feedback-detail"]

    assert any(item["id"] == "development-feedback" for item in tabs)
    assert feedback_list["visibleIf"] == "$state.workbenchView === 'development-feedback'"
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
    assert labels["workflow_contract"] == "adaos.builder.workflow.v1"
    current = _by_id(webui)["design-current-work"]
    assert current["dataSource"]["name"] == "builder_sdk_control_skill.get_workbench"
    assert current["inputs"]["stateBindings"]["commands"] == "commands"
    text = json.dumps(webui, ensure_ascii=False)
    assert "Change 12" not in text
    assert "$state.samples" not in text


def test_project_picker_lists_installed_projects_and_selects_once(monkeypatch) -> None:
    webui = _load("webui.json")
    page = webui["ui"]["application"]["desktop"]["pageSchema"]
    widgets = _by_id(webui)
    picker = widgets["project-picker-table"]

    assert picker["dataSource"] == {
        "kind": "skill",
        "name": "builder_sdk_control_skill.list_projects",
        "scope": "local",
        "params": {
            "limit": 500,
            "query": "$state.projectPickerQuery",
            "selected_object_type": "$state.selectedProjectKind",
            "selected_object_id": "$state.selectedProjectId",
            "include_archived": "$state.projectPickerArchived",
            "_meta": {"current_scenario": "builder"},
        },
        "cacheTtlMs": 0,
        "invalidationTags": ["builder.project.catalog"],
        "preserveLastValue": True,
    }
    assert picker["type"] == "ui.table"
    assert picker["inputs"]["refresh"] is True
    assert picker["inputs"]["preferences"]["filterStateKeys"] == ["projectPickerArchived"]
    assert widgets["project-picker-sample"]["inputs"]["value"] == "$state.projectPickerSample"
    assert widgets["project-picker-archived"]["inputs"]["value"] == "$state.projectPickerArchived"

    select_state = next(
        action["params"]
        for action in picker["actions"]
        if action.get("type") == "updateState"
    )
    assert select_state["selectedProjectKind"] == "$event.object_type"
    assert select_state["selectedProjectId"] == "$event.object_id"
    assert select_state["selectedObjectKind"] == "$event.target_object_type"
    assert select_state["selectedObjectId"] == "$event.target_object_id"

    state = {**page["initialState"], **webui["ui"]["application"]["modals"]["project-picker"]["schema"]["initialState"]}
    resolved_params = {
        key: state[value.removeprefix("$state.")] if isinstance(value, str) and value.startswith("$state.") else value
        for key, value in picker["dataSource"]["params"].items()
    }
    assert resolved_params == {
        "limit": 500,
        "query": "",
        "selected_object_type": "project",
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
        handler.project_catalog,
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
    assert widgets["project-picker-create"]["actions"][0]["params"]["modalId"] == "new-project"
    metadata_action = next(node for node in _walk(webui) if node.get("target") == "builder_sdk_control_skill.update_project_metadata")
    assert "builder.project.catalog" in metadata_action["invalidates"]


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
    assert "visibleIf" not in lifecycle
    assert process["type"] == "ui.list"
    assert process["dataSource"]["name"] == "builder_sdk_control_skill.get_process_stages"
    assert "selectedLifecycleStage" not in widgets["design-conversation-full-task"]["visibleIf"]
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
    assert widgets["publication-workspace-actions"]["visibleIf"] == "$state.workbenchView === 'deliveries'"
    assert any(item.get("target") == "builder_sdk_control_skill.submit_automation" for item in _walk(webui))
    lifecycle_buttons = {
        item["id"] for item in lifecycle["inputs"]["buttons"]
    }
    assert lifecycle_buttons == {
        "show-preview", "make-current", "stabilize",
        "go-automation", "go-publication",
    }
    # Stage availability comes from the owner projection, not a client state machine.
    assert process["inputs"]["disabledKey"] == "disabled"
    assert placement_action["params"]["placement_kind"] == "$event.placementKind"


def test_ui_revision_and_artifact_versions_have_explicit_non_stale_labels() -> None:
    webui = _load("webui.json")
    labels = webui["ui"]["application"]["desktop"]["pageSchema"]["meta"]["builder"]

    assert labels["accepted_design_revision"] == "071"
    header = _by_id(webui)["design-workbench-header"]
    assert header["inputs"]["statusDataSource"]["value"]["target_label"] == "$state.workbench.revision_label"
    assert labels["qualification"] == "pending"


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
    chat = widgets["design-conversation-full-task"]
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
    assert "$state.workbenchView === 'conversation'" in chat["visibleIf"]
    assert "$state.selectedLifecycleStage" not in chat["visibleIf"]
    assert widgets["design-current-work"]["dataSource"]["name"] == (
        "builder_sdk_control_skill.get_workbench"
    )
    process = next(button for button in widgets["design-workbench-header"]["inputs"]["buttons"] if button["id"] == "specimens")
    assert process["optionsDataSource"]["name"] == "builder_sdk_control_skill.get_process_stages"
    assert process["displaySelectedLabel"] is False
    assert widgets["design-conversation-side-task"]["inputs"]["sendCommand"] == "voice.chat.user"
    assert _load("webui.json")["ui"]["application"]["modals"]["automation"]["schema"]["widgets"]
    publication_actions = widgets["publication-workspace-actions"]["actions"]
    publication_targets = {
        item.get("target")
        for item in publication_actions
        if item.get("type") == "callSkill"
    }
    assert "builder_sdk_control_skill.push_project" in publication_targets
    assert any(
        item.get("type") == "openModal"
        and item.get("params", {}).get("modalId") == "confirm-trial-access"
        for item in publication_actions
    )
    trial_modal = _load("webui.json")["ui"]["application"]["modals"]["confirm-trial-access"]
    assert any(
        action.get("target") == "builder_sdk_control_skill.publish_project"
        for widget in trial_modal["schema"]["widgets"]
        for action in widget.get("actions", [])
    )


def test_long_project_title_has_a_full_text_surface_besides_the_compact_menu() -> None:
    widgets = _by_id(_load("webui.json"))
    header_buttons = widgets["design-workbench-header"]["inputs"]["buttons"]
    assert header_buttons[0]["label"] == "$state.applicationTitle"
    status = widgets["design-workbench-header"]["inputs"]["statusDataSource"]
    assert status["value"]["title"] == "$state.applicationTitle"
    assert len(widgets["design-current-work"]["inputs"]["fields"]) <= 3


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
    contract = {**contract, **contract["profiles"]["workbench"]}
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
    for previous, current in contract["forward_binding_replacements"].items():
        required_bindings.discard(previous)
        required_bindings.add(current)
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
    assert {"en", "ru", "workbench-live-en", "workbench-live-ru"} <= {path.stem for path in (ROOT / "assets/i18n").glob("*.json")}


def test_open_preview_uses_selected_application_not_unselected_global_binding() -> None:
    for name in ("webui.json", "scenario.json"):
        action = next(item for item in _walk(_load(name)) if item.get("target") == "builder_sdk_control_skill.open_preview")
        assert action["target"] == "builder_sdk_control_skill.open_preview"
        assert action["params"]["object_type"] == "$state.selectedProjectKind"
        assert action["params"]["object_id"] == "$state.selectedProjectId"
        assert action["openResultUrl"] is True


def test_creation_retains_template_flow_and_forwards_application_name() -> None:
    widgets = _by_id(_load("webui.json"))
    form = widgets["new-project-form"]
    fields = {field["id"]: field for field in form["inputs"]["fields"]}
    assert fields["title"]["required"] is True
    creation = next(action for action in form["actions"] if action.get("target", "").endswith(".create_project"))
    assert creation["params"]["title"] == "$event.values.title"
    assert creation["params"]["template"] == "$state.selectedTemplate"
    assert widgets["new-project-templates"]["dataSource"]["name"].endswith(".list_templates")
    assert all("workbenchView" not in action.get("params", {})
               for action in form["actions"] if action["on"].startswith("change:"))
    # The create SDK can publish the new selection before its response arrives.
    # A later form action must not erase the already loaded canonical projection.
    assert all(not {"current", "workbench", "commands", "builderConversationId", "builderThreadId"}
               .intersection(action.get("params", {})) for action in form["actions"]
               if action["type"] == "updateState")
