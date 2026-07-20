from __future__ import annotations

import json
import importlib.resources as resources
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> dict:
    return json.loads((ROOT / "webui.json").read_text(encoding="utf-8"))


def _walk(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def test_scenario_descriptors_are_version_aligned() -> None:
    scenario = json.loads((ROOT / "scenario.json").read_text(encoding="utf-8"))
    yaml_version = next(
        line.split(":", 1)[1].strip()
        for line in (ROOT / "scenario.yaml").read_text(encoding="utf-8").splitlines()
        if line.startswith("version:")
    )
    assert scenario["version"] == yaml_version


def test_webui_is_valid_v1() -> None:
    document = _manifest()
    schema = json.loads(
        (resources.files("adaos.abi") / "webui.v1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(document)
    assert document["schema"] == "adaos.webui.v1"
    page = document["ui"]["application"]["desktop"]["pageSchema"]
    assert page["title"] == "\u0414\u043e\u043c\u0430\u0448\u043d\u044f\u044f \u043a\u043d\u0438\u0433\u0430 \u0440\u0435\u0446\u0435\u043f\u0442\u043e\u0432"
    seed = document["ydoc_defaults"]["data/streaming_recipe_book_eval/recipes"]
    assert seed[0]["title"] == "\u041e\u0432\u0441\u044f\u043d\u043a\u0430 \u0441 \u044f\u0433\u043e\u0434\u0430\u043c\u0438"


def test_actions_match_browser_runtime_contract() -> None:
    document = _manifest()
    actions = [node for node in _walk(document) if "on" in node and "type" in node]
    assert actions
    assert not any(action["type"] == "callTool" for action in actions)
    skill_actions = [action for action in actions if action["type"] == "callSkill"]
    assert {action["target"] for action in skill_actions} == {
        "streaming_recipe_book_eval_skill.add_recipe",
        "streaming_recipe_book_eval_skill.set_favorite",
    }
    encoded = json.dumps(document, ensure_ascii=False)
    assert "$form" not in encoded
    assert "resultPath" not in encoded
    assert "$event.values.title" in encoded


def test_catalog_and_details_use_typed_skill_data_sources() -> None:
    document = _manifest()
    sources = [node["dataSource"] for node in _walk(document) if "dataSource" in node]
    names = {source.get("name") for source in sources if source.get("kind") == "skill"}
    assert names == {
        "streaming_recipe_book_eval_skill.list_recipes",
        "streaming_recipe_book_eval_skill.get_recipe",
    }
    encoded = json.dumps(document, ensure_ascii=False)
    assert "catalogRevision" not in encoded
    assert "$client.nowMs" not in encoded


def test_add_resets_filters_that_could_hide_the_created_recipe() -> None:
    document = _manifest()
    form = next(node for node in _walk(document) if node.get("id") == "add-recipe-form")
    actions = form["actions"]
    assert actions[0]["type"] == "callSkill"
    assert actions[0]["target"] == "streaming_recipe_book_eval_skill.add_recipe"
    assert actions[1] == {
        "on": "submit",
        "type": "updateState",
        "params": {
            "activeCategory": "all",
            "filterTimeMax": None,
            "favoritesOnly": False,
            "searchQuery": "",
        },
    }


def test_mutations_rely_on_successful_call_skill_invalidation() -> None:
    document = _manifest()
    mutation_actions = [
        node for node in _walk(document)
        if node.get("type") == "callSkill" and node.get("target", "").endswith((".add_recipe", ".set_favorite"))
    ]
    assert mutation_actions
    assert all(set(action) >= {"on", "type", "target", "params"} for action in mutation_actions)
    assert all("recipe.catalog" in action.get("invalidates", []) for action in mutation_actions)
