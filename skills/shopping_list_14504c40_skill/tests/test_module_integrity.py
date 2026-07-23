from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml
from adaos.services.skill.validation import SkillValidationService


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADAOS_SKILL_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("recipe_handler", ROOT / "handlers" / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_list_search_and_read_seed_recipes(handler) -> None:
    listed = handler.list_recipes()
    assert listed["ok"] and listed["count"] == 2 and not listed["empty"]
    found = handler.search_recipes("свекольный")
    assert [row["id"] for row in found["items"]] == ["borscht"]
    assert handler.get_recipe("caesar")["recipe"]["servings"] == 2
    assert handler.get_recipe("missing")["error"] == "recipe_not_found"


def test_create_favorite_and_atomic_persistence(handler) -> None:
    created = handler.create_recipe("Каша", "Завтраки", 10, "Легко", 1, "Крупа — 100 г\nВода — 200 мл", "Сварить\nПодать")
    assert created["ok"] and len(created["recipe"]["ingredients"]) == 2
    recipe_id = created["recipe"]["id"]
    assert handler.set_favorite(recipe_id, True) == {"ok": True, "recipe_id": recipe_id, "favorite": True}
    assert handler.list_recipes(favorites_only=True)["count"] == 2
    assert (Path(handler._store_path())).is_file()
    assert list(Path(handler._store_path()).parent.glob("*.tmp")) == []


def test_shopping_list_requires_positive_confirmed_quantity(handler) -> None:
    rejected = handler.add_ingredients_to_shopping_list("caesar", 0)
    assert not rejected["ok"] and rejected["added_count"] == 0
    added = handler.add_ingredients_to_shopping_list("caesar", 2.5)
    assert added["ok"] and added["added_count"] == 4
    shopping = handler.get_shopping_list()
    assert shopping["count"] == 4
    assert {row["quantity_multiplier"] for row in shopping["items"]} == {2.5}


def test_manifest_exports_typed_recipe_tools_and_validates() -> None:
    manifest = yaml.safe_load((ROOT / "skill.yaml").read_text(encoding="utf-8"))
    expected = {"list_recipes", "search_recipes", "get_recipe", "create_recipe", "set_favorite", "add_ingredients_to_shopping_list", "get_shopping_list"}
    assert set(manifest["exports"]["tools"]) == expected
    assert {tool["name"] for tool in manifest["tools"]} == expected
    report = SkillValidationService(None).validate_path(ROOT, install_mode=True)  # type: ignore[arg-type]
    assert report.ok, [(issue.code, issue.message) for issue in report.issues]
