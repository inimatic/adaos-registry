from __future__ import annotations

import json
from pathlib import Path

import yaml

from adaos.services.skill.manager import SkillManager
from adaos.services.skill.validation import SkillValidationService


SKILL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SKILL_ROOT.parents[1]
SCENARIO_ROOT = WORKSPACE_ROOT / "scenarios" / "streaming_recipe_book_eval"
EXPECTED_SIDE_EFFECTS = {
    "list_recipes": "none",
    "get_recipe": "none",
    "add_recipe": "local_write",
    "set_favorite": "local_write",
}


def _walk(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def test_scenario_skill_routes_are_discoverable_exports() -> None:
    """Guard the browser -> scenario -> manifest -> decorated handler route."""
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))
    scenario = json.loads((SCENARIO_ROOT / "scenario.json").read_text(encoding="utf-8"))
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))

    skill_id = SKILL_ROOT.name
    assert manifest["id"] == manifest["name"] == skill_id
    version = tuple(int(part) for part in manifest["version"].split(".")[:3])
    assert version >= (1, 0, 1)
    assert skill_id in scenario["depends"]
    assert skill_id in scenario["runtime"]["skills"]["required"]

    routed = set()
    for node in _walk(webui):
        source = node.get("dataSource") if isinstance(node, dict) else None
        if isinstance(source, dict) and source.get("kind") == "skill":
            routed.add(source["name"])
        if isinstance(node, dict) and node.get("type") == "callSkill":
            routed.add(node["target"])

    expected_prefix = f"{skill_id}."
    routed_tools = {route.removeprefix(expected_prefix) for route in routed}
    assert all(route.startswith(expected_prefix) for route in routed)
    assert routed_tools == set(manifest["exports"]["tools"])
    assert routed_tools == {tool["name"] for tool in manifest["tools"]}

    report = SkillValidationService(None).validate_path(SKILL_ROOT, install_mode=True)  # type: ignore[arg-type]
    assert report.ok, [(issue.code, issue.message) for issue in report.issues]


def test_handler_package_exports_every_public_tool(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(SKILL_ROOT))
    from handlers import add_recipe, get_recipe, list_recipes, set_favorite

    exported_tools = (add_recipe, get_recipe, list_recipes, set_favorite)
    for exported in exported_tools:
        assert callable(exported)
    assert {exported.__name__ for exported in exported_tools} == {
        "add_recipe", "get_recipe", "list_recipes", "set_favorite"
    }


def test_tool_side_effects_survive_runtime_manifest_discovery(tmp_path: Path) -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))
    declared = {tool["name"]: tool.get("side_effects") for tool in manifest["tools"]}
    assert declared == EXPECTED_SIDE_EFFECTS

    resolved_manifest = tmp_path / "resolved.manifest.json"
    resolved_manifest.write_text(json.dumps({"tools": {}}), encoding="utf-8")
    manager = SkillManager.__new__(SkillManager)
    discovered = manager._runtime_sync_manifest_tools(
        SKILL_ROOT.name, resolved_manifest, SKILL_ROOT
    )

    assert set(discovered) == set(EXPECTED_SIDE_EFFECTS)
    resolved = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    assert {
        name: resolved["tools"][name].get("side_effects")
        for name in EXPECTED_SIDE_EFFECTS
    } == EXPECTED_SIDE_EFFECTS


def test_runtime_storage_uses_adaos_durable_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(SKILL_ROOT))
    from handlers import main

    monkeypatch.delenv("ADAOS_SKILL_DATA_DIR", raising=False)
    runtime_store = tmp_path / ".runtime" / "streaming_recipe_book_eval_skill" / "v1.0" / "data" / "db" / "skill_env.json"
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(runtime_store))
    assert main._data_path() == runtime_store.parent / "recipes.json"
