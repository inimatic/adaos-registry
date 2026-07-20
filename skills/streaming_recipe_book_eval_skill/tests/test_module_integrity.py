from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import yaml

from adaos.services.skill.validation import SkillValidationService


ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location("recipe_skill.handlers.main", ROOT / "handlers" / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_and_entrypoint_validate() -> None:
    manifest = yaml.safe_load((ROOT / "skill.yaml").read_text(encoding="utf-8"))
    assert set(manifest["exports"]["tools"]) == {"list_recipes", "get_recipe", "add_recipe", "set_favorite"}
    report = SkillValidationService(None).validate_path(ROOT, install_mode=True)  # type: ignore[arg-type]
    assert report.ok, [(issue.code, issue.message) for issue in report.issues]


def test_browser_tool_routes_are_exact_and_causally_bounded() -> None:
    manifest = yaml.safe_load((ROOT / "skill.yaml").read_text(encoding="utf-8"))
    routes = [route for route in manifest["data_routes"] if route["route"] == "tool/details"]

    assert {route["path"] for route in routes} == {"tool/list_recipes", "tool/get_recipe"}
    for route in routes:
        assert route["tool"] == route["path"].removeprefix("tool/")
        assert route["budget"]["max_payload_bytes"] > 0
        assert route["budget"]["max_items"] > 0
        assert route["read_policy"]["mode"] in {"cache_first", "stale_while_revalidate"}
        assert "targeted_invalidation" in route["read_policy"]["triggers"]
        assert route["read_policy"]["invalidation_tags"]
        assert route["read_policy"]["max_request_hz"] == 0.2
        assert route["read_policy"]["preserve_last_value"] is True
        assert "preserves the last successful value" in route["notes"]
        assert "causally bounded" in route["notes"]


def test_crud_filters_and_favorites(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_SKILL_DATA_DIR", str(tmp_path))
    module = _module()
    assert module.list_recipes(category="breakfast")["count"] == 1
    created = module.add_recipe("Быстрый омлет", "breakfast", 12, "Яйца\nСыр", "Взбить\nОбжарить")
    recipe_id = created["recipe"]["id"]
    assert module.get_recipe(recipe_id)["recipe"]["ingredients"] == ["Яйца", "Сыр"]
    assert module.list_recipes(max_time_min=15, query="омлет")["count"] == 1
    assert module.set_favorite(recipe_id)["recipe"]["favorite"] is True
    assert module.list_recipes(favorites_only=True)["items"][0]["id"] == recipe_id
    assert json.loads((tmp_path / "recipes.json").read_text(encoding="utf-8"))[-1]["id"] == recipe_id


def test_invalid_recipe_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_SKILL_DATA_DIR", str(tmp_path))
    module = _module()
    try:
        module.add_recipe("Bad", "dessert", 0, [], [])
    except ValueError:
        pass
    else:
        raise AssertionError("invalid category must fail")


def test_atomic_write_retries_transient_replace_denial(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_SKILL_DATA_DIR", str(tmp_path))
    module = _module()
    real_replace = os.replace
    calls = 0

    def transient_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("destination is briefly shared")
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", transient_replace)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    module._write([{"id": "retry-ok"}])

    assert calls == 2
    assert json.loads((tmp_path / "recipes.json").read_text(encoding="utf-8")) == [{"id": "retry-ok"}]
    assert list(tmp_path.glob("recipes-*.json")) == []


def test_atomic_write_raises_and_cleans_up_after_durable_denial(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_SKILL_DATA_DIR", str(tmp_path))
    existing = tmp_path / "recipes.json"
    existing.write_text('[{"id": "unchanged"}]', encoding="utf-8")
    module = _module()
    calls = 0

    def denied_replace(_source, _destination):
        nonlocal calls
        calls += 1
        raise PermissionError("destination remains shared")

    monkeypatch.setattr(module.os, "replace", denied_replace)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    try:
        module._write([{"id": "must-not-land"}])
    except PermissionError:
        pass
    else:
        raise AssertionError("durable replace denial must be raised")

    assert calls == module._REPLACE_ATTEMPTS
    assert json.loads(existing.read_text(encoding="utf-8")) == [{"id": "unchanged"}]
    assert list(tmp_path.glob("recipes-*.json")) == []
