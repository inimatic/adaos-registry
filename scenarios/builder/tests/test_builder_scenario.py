from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONTROL_SKILL_ROOT = ROOT.parents[1] / "skills" / "builder_sdk_control_skill"


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


def test_descriptors_are_aligned_and_declare_real_dependencies() -> None:
    scenario = _load("scenario.json")
    manifest = yaml.safe_load((ROOT / "scenario.yaml").read_text(encoding="utf-8"))

    assert scenario["version"] == manifest["version"]
    assert tuple(int(part) for part in scenario["version"].split(".")[:2]) >= (0, 2)
    assert scenario["depends"] == manifest["depends"] == ["builder_sdk_control_skill"]
    assert scenario["runtime"]["skills"]["required"] == manifest["runtime"]["skills"]["required"]


def test_scenario_and_standalone_webui_are_identical() -> None:
    scenario = _load("scenario.json")
    webui = _load("webui.json")

    assert scenario["ui"] == webui["ui"]
    assert webui["generated_by"] == "builder_sdk_control_skill"
    assert scenario["ui"]["application"]["desktop"]["pageSchema"]["meta"]["builder"]["functional"] is True


def test_revision_032_preserves_the_approved_029_structure() -> None:
    prototype = _load("ui_revisions/029.json")["after_webui"]
    current = _load("webui.json")
    revision = _load("ui_revisions/032.json")
    prototype_page = prototype["ui"]["application"]["desktop"]["pageSchema"]
    current_page = current["ui"]["application"]["desktop"]["pageSchema"]
    prototype_widgets = {item["id"]: item for item in prototype_page["widgets"]}
    current_widgets = {item["id"]: item for item in current_page["widgets"]}

    assert current_page["layout"] == prototype_page["layout"]
    assert current_page["initialState"]["activeView"] == "files"
    assert set(prototype_widgets) <= set(current_widgets)
    for widget_id, prototype_widget in prototype_widgets.items():
        assert current_widgets[widget_id]["type"] == prototype_widget["type"]
        assert current_widgets[widget_id]["area"] == prototype_widget["area"]
    assert set(prototype["ui"]["application"]["modals"]) <= set(current["ui"]["application"]["modals"])
    assert revision["patch"] == {"operation": "runtime_binding_corrections", "base_revision": "031"}
    assert revision["after_webui"] == current
    assert (ROOT / "ui_revisions" / "current.txt").read_text(encoding="utf-8").strip() == "032"


def test_builder_static_and_dynamic_i18n_contract_is_complete() -> None:
    scenario = _load("scenario.json")
    webui = _load("webui.json")
    ru = _load("assets/i18n/ru.json")
    en = _load("assets/i18n/en.json")
    resources = webui["ui"]["application"]["resources"]
    referenced: set[str] = set()

    for node in _walk(webui["ui"]["application"]):
        for field, spec in node.items():
            if field.endswith("_i18n") and isinstance(spec, dict) and spec.get("key"):
                referenced.add(spec["key"])
    referenced.add(scenario["title_i18n"]["key"])

    assert scenario["supported_locales"] == ["en", "ru"]
    assert scenario["ui"]["application"]["resources"] == resources
    assert set(resources) == {"builder.i18n.en", "builder.i18n.ru"}
    assert resources["builder.i18n.en"]["locale"] == "en"
    assert resources["builder.i18n.ru"]["locale"] == "ru"
    assert resources["builder.i18n.en"]["role"] == resources["builder.i18n.ru"]["role"] == "i18n"
    assert set(ru) == set(en)
    assert len(ru) == 144
    assert referenced <= set(ru)
    assert "scenario.builder.title" in referenced
    assert ru["scenario.builder.title"] == "Builder — рабочее место разработки"
    assert en["scenario.builder.title"] == "Builder — development workspace"


def test_builder_uses_live_skill_data_instead_of_mock_project_data() -> None:
    webui = _load("webui.json")
    sources = [node["dataSource"] for node in _walk(webui) if "dataSource" in node]
    skill_sources = {source["name"] for source in sources if source.get("kind") == "skill"}

    assert not any(source.get("kind") == "static" for source in sources)
    assert skill_sources == {
        "builder_sdk_control_skill.get_automation",
        "builder_sdk_control_skill.get_lifecycle",
        "builder_sdk_control_skill.get_llm_options",
        "builder_sdk_control_skill.get_preview",
        "builder_sdk_control_skill.get_prompt_context",
        "builder_sdk_control_skill.get_project",
        "builder_sdk_control_skill.list_changes",
        "builder_sdk_control_skill.list_project_file_tree",
        "builder_sdk_control_skill.list_project_objects",
        "builder_sdk_control_skill.list_projects",
        "builder_sdk_control_skill.list_templates",
        "builder_sdk_control_skill.read_project_file",
    }


def test_all_browser_skill_calls_resolve_to_declared_tools() -> None:
    webui = _load("webui.json")
    skill_manifest = yaml.safe_load((CONTROL_SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))
    declared = {item["name"] for item in skill_manifest["tools"]}
    actions = [node for node in _walk(webui) if node.get("type") == "callSkill"]

    assert actions
    assert not any(node.get("type") == "callTool" for node in _walk(webui))
    for action in actions:
        skill_name, tool_name = action["target"].split(".", 1)
        if skill_name == "builder_sdk_control_skill":
            assert tool_name in declared
        else:
            assert (skill_name, tool_name) == ("builder_skill", "set_ui_revision_current")


def test_file_editing_and_project_selection_use_runtime_event_values() -> None:
    webui = _load("webui.json")
    nodes = {node.get("id"): node for node in _walk(webui) if node.get("id")}

    save = next(action for action in nodes["artifact-workbench"]["actions"] if action["type"] == "callSkill")
    assert save["target"] == "builder_sdk_control_skill.save_project_file"
    assert save["params"]["path"] == "$state.selectedFilePath"
    assert save["params"]["text"] == "$event.content"

    project_calls = [action for action in nodes["project-picker-list"]["actions"] if action["type"] == "callSkill"]
    assert project_calls == [
        {
            "on": "select",
            "type": "callSkill",
            "target": "builder_sdk_control_skill.select_preview",
            "params": {"object_type": "$event.object_type", "object_id": "$event.object_id"},
        }
    ]


def test_automation_and_publication_are_end_to_end_wired() -> None:
    webui = _load("webui.json")
    actions = [node for node in _walk(webui) if node.get("type") == "callSkill"]
    by_target: dict[str, list[dict]] = {}
    for action in actions:
        by_target.setdefault(action["target"], []).append(action)

    assert by_target["builder_sdk_control_skill.start_automation"][0]["params"]["implementation_brief"] == "$event.values.implementation_brief"
    assert by_target["builder_sdk_control_skill.submit_automation"][0]["params"]["text"] == "$event.values.text"
    publish_calls = by_target["builder_sdk_control_skill.publish_project"]
    assert {action["params"]["dry_run"] for action in publish_calls} == {True, False}
    assert len(by_target["builder_sdk_control_skill.push_project"]) == 2


def test_prompt_ide_capability_surface_is_not_lost() -> None:
    webui = _load("webui.json")
    sources = {
        node["dataSource"]["name"]
        for node in _walk(webui)
        if isinstance(node.get("dataSource"), dict) and node["dataSource"].get("kind") == "skill"
    }
    targets = {node["target"] for node in _walk(webui) if node.get("type") == "callSkill"}

    # Prompt IDE git-log presentation is replaced by the auditable Builder Change timeline.
    assert {
        "builder_sdk_control_skill.list_projects",
        "builder_sdk_control_skill.list_project_objects",
        "builder_sdk_control_skill.list_project_file_tree",
        "builder_sdk_control_skill.read_project_file",
        "builder_sdk_control_skill.list_templates",
        "builder_sdk_control_skill.get_project",
        "builder_sdk_control_skill.get_prompt_context",
        "builder_sdk_control_skill.get_llm_options",
        "builder_sdk_control_skill.list_changes",
    } <= sources
    assert {
        "builder_sdk_control_skill.save_project_file",
        "builder_sdk_control_skill.save_prompt_context",
        "builder_sdk_control_skill.append_prompt_addendum",
        "builder_sdk_control_skill.create_project",
        "builder_sdk_control_skill.select_preview",
        "builder_sdk_control_skill.set_llm_profile",
        "builder_sdk_control_skill.update_project_metadata",
        "builder_sdk_control_skill.set_workflow_state",
        "builder_sdk_control_skill.archive_project",
        "builder_sdk_control_skill.update_project",
        "builder_sdk_control_skill.push_project",
        "builder_sdk_control_skill.publish_project",
        "builder_sdk_control_skill.delete_project",
        "builder_sdk_control_skill.start_automation",
        "builder_sdk_control_skill.submit_automation",
    } <= targets


def test_builder_chat_remains_bound_to_builder_agent() -> None:
    webui = _load("webui.json")
    chat = next(node for node in _walk(webui) if node.get("id") == "builder-chat")

    assert chat["dataSource"]["kind"] == "stream"
    assert chat["inputs"]["meta"]["active_agent_id"] == "agent:builder_skill:builder"
    assert chat["inputs"]["sendCommand"] == "voice.chat.user"
