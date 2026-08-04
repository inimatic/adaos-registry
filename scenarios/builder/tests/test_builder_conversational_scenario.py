from __future__ import annotations

import ast
import json
from pathlib import Path

import yaml
from jsonschema import Draft7Validator, Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
CONTROL_SKILL_ROOT = ROOT.parents[1] / "skills" / "builder_sdk_control_skill"
ABI_ROOT = ROOT.parents[3] / "src" / "adaos" / "abi"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _walk(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def test_builder_is_conversation_first_and_uses_canonical_sources() -> None:
    scenario = yaml.safe_load((ROOT / "scenario.yaml").read_text(encoding="utf-8"))
    compatibility_projection = _load_json(ROOT / "scenario.json")
    webui = _load_json(ROOT / "webui.json")
    page = webui["ui"]["application"]["desktop"]["pageSchema"]

    assert scenario["ui"] == {"manifest": "webui.json"}
    assert scenario["depends"] == scenario["runtime"]["skills"]["required"]
    assert scenario["depends"] == [
        "builder_skill",
        "builder_sdk_control_skill",
        "voice_chat_skill",
    ]
    assert page["initialState"]["activeView"] == "conversation"
    assert page["initialState"]["conversationFocus"] == "scenario:builder"
    selected_file = page["initialState"]["selectedFilePath"]
    assert selected_file == "scenario.yaml"
    assert (ROOT / selected_file).is_file()
    assert compatibility_projection["ui"] == webui["ui"]
    for key in ("id", "name", "title", "description", "version", "depends", "runtime"):
        assert compatibility_projection[key] == scenario[key]


def test_builder_manifests_match_public_abi_schemas() -> None:
    scenario = yaml.safe_load((ROOT / "scenario.yaml").read_text(encoding="utf-8"))
    webui = _load_json(ROOT / "webui.json")
    scenario_schema = _load_json(ABI_ROOT / "scenario.schema.json")
    webui_schema = _load_json(ABI_ROOT / "webui.v1.schema.json")

    Draft7Validator(scenario_schema).validate(scenario)
    Draft202012Validator(webui_schema).validate(webui)


def test_builder_webui_calls_only_declared_control_tools_with_valid_parameters() -> None:
    webui = _load_json(ROOT / "webui.json")
    manifest = yaml.safe_load((CONTROL_SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))
    tools = {item["name"]: item for item in manifest["tools"]}
    checked = 0

    for node in _walk(webui):
        invocations = []
        if node.get("type") == "callSkill":
            invocations.append((node.get("target"), node.get("params") or {}))
        source = node.get("dataSource")
        if isinstance(source, dict) and source.get("kind") == "skill":
            invocations.append((source.get("name"), source.get("params") or {}))
        for target, params in invocations:
            if not isinstance(target, str) or not target.startswith("builder_sdk_control_skill."):
                continue
            checked += 1
            tool_name = target.split(".", 1)[1]
            assert tool_name in tools
            schema = tools[tool_name].get("input_schema") or {}
            properties = set(schema.get("properties") or {})
            required = set(schema.get("required") or [])
            supplied = set(params) - {"_meta"}
            assert supplied <= properties
            assert required <= supplied

    assert checked >= 20


def test_control_manifest_matches_handlers_and_exports_every_tool() -> None:
    manifest = yaml.safe_load((CONTROL_SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))
    registry = _load_json(ROOT.parents[1] / "registry.json")
    tree = ast.parse((CONTROL_SKILL_ROOT / "handlers" / "main.py").read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    declared = {item["name"]: item for item in manifest["tools"]}
    decorated: dict[str, str] = {}
    exported: set[str] = set()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Name)
                    and decorator.func.id == "tool"
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                ):
                    decorated[str(decorator.args[0].value)] = node.name
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            exported = {
                str(item.value) for item in node.value.elts if isinstance(item, ast.Constant)
            }

    assert set(declared) == set(decorated)
    assert set(decorated.values()) <= exported
    registry_entry = next(
        item for item in registry["skills"] if item["id"] == "builder_sdk_control_skill"
    )
    assert registry_entry["tools_count"] == len(declared)
    for tool_name, spec in declared.items():
        function_name = decorated[tool_name]
        assert spec["entry"] == f"handlers.main:{function_name}"
        node = functions[function_name]
        positional = list(node.args.posonlyargs) + list(node.args.args)
        defaults_at = len(positional) - len(node.args.defaults)
        function_parameters = {item.arg for item in positional} | {
            item.arg for item in node.args.kwonlyargs
        }
        function_required = {item.arg for item in positional[:defaults_at]} | {
            item.arg
            for item, default in zip(node.args.kwonlyargs, node.args.kw_defaults)
            if default is None
        }
        schema = spec.get("input_schema") or {}
        schema_parameters = set(schema.get("properties") or {})
        schema_required = set(schema.get("required") or [])
        assert schema_parameters <= function_parameters
        assert function_required <= schema_parameters
        assert schema_required <= function_parameters


def test_builder_i18n_covers_every_referenced_key() -> None:
    scenario = yaml.safe_load((ROOT / "scenario.yaml").read_text(encoding="utf-8"))
    webui = _load_json(ROOT / "webui.json")
    en = _load_json(ROOT / "assets" / "i18n" / "en.json")
    ru = _load_json(ROOT / "assets" / "i18n" / "ru.json")
    referenced = {scenario["title_i18n"]["key"]}
    for node in _walk(webui):
        for key, value in node.items():
            if key.endswith("_i18n") and isinstance(value, dict) and value.get("key"):
                referenced.add(value["key"])

    assert set(en) == set(ru)
    assert referenced <= set(en)


def test_builder_preserves_declared_functional_parity() -> None:
    webui = _load_json(ROOT / "webui.json")
    contract = _load_json(ROOT / "assets" / "builder_functional_parity.json")
    application = webui["ui"]["application"]
    widgets: set[str] = set()
    buttons: set[str] = set()
    bindings: set[str] = set()

    for node in _walk(application):
        if isinstance(node.get("id"), str):
            widgets.add(node["id"])
        for button in (node.get("inputs") or {}).get("buttons") or []:
            if isinstance(button, dict) and isinstance(button.get("id"), str):
                buttons.add(button["id"])
        if node.get("type") == "callSkill" and isinstance(node.get("target"), str):
            bindings.add(node["target"])
        source = node.get("dataSource")
        if isinstance(source, dict) and source.get("kind") == "skill":
            bindings.add(str(source.get("name") or ""))
        if isinstance(source, dict) and source.get("kind") == "stream":
            bindings.add(f"stream:{source.get('receiver') or source.get('name') or ''}")

    modals = set(application.get("modals") or {})
    assert set(contract["required_widget_ids"]) <= widgets
    assert set(contract["required_modal_ids"]) <= modals
    assert set(contract["required_bindings"]) <= bindings
    assert set(contract["forward_required_bindings"]) <= bindings
    assert set(contract["required_lifecycle_buttons"]) <= buttons
    assert not set(contract["forbidden_bindings"]) & bindings
