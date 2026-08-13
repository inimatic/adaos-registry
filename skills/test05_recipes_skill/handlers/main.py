from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from adaos.sdk.core.decorators import tool


DEFAULT_RECIPES = (
    {
        "id": "r1",
        "title": "Куриный салат с авокадо",
        "cooking_time_minutes": 20,
        "description": "Легкий салат с сочной курицей и авокадо.",
        "ingredients": ["Куриное филе", "Авокадо", "Листья салата"],
        "steps": ["Приготовить курицу.", "Нарезать продукты.", "Смешать и заправить."],
        "favorite": True,
    },
    {
        "id": "r2",
        "title": "Паста альфредо",
        "cooking_time_minutes": 25,
        "description": "Сливочный соус с пармезаном и чесноком.",
        "ingredients": ["Паста", "Сливки", "Пармезан", "Чеснок"],
        "steps": ["Сварить пасту.", "Приготовить соус.", "Смешать."],
        "favorite": False,
    },
    {
        "id": "r3",
        "title": "Шакшука",
        "cooking_time_minutes": 15,
        "description": "Яйца в томатном соусе с перцем и специями.",
        "ingredients": ["Томаты", "Яйца", "Перец", "Лук"],
        "steps": ["Обжарить овощи.", "Добавить томаты.", "Приготовить яйца в соусе."],
        "favorite": True,
    },
    {
        "id": "r4",
        "title": "Овсяные панкейки",
        "cooking_time_minutes": 30,
        "description": "Полезные панкейки на завтрак.",
        "ingredients": ["Овсяные хлопья", "Яйца", "Молоко"],
        "steps": ["Смешать тесто.", "Обжарить панкейки."],
        "favorite": False,
    },
)


class RecipeValidationError(ValueError):
    """Raised when recipe input does not satisfy the public contract."""


def lang_res() -> dict[str, str]:
    return {}


def _storage_path() -> Path:
    configured = os.getenv("ADAOS_RECIPES_STORE")
    if configured:
        return Path(configured).expanduser()
    data_dir = os.getenv("ADAOS_SKILL_DATA_DIR") or os.getenv("ADAOS_DATA_DIR")
    root = Path(data_dir).expanduser() if data_dir else Path.home() / ".adaos" / "data"
    return root / "test05_recipes_skill" / "recipes.json"


def _initial_state() -> dict[str, Any]:
    return {"schema_version": 1, "recipes": [dict(item) for item in DEFAULT_RECIPES]}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_state() -> dict[str, Any]:
    path = _storage_path()
    if not path.exists():
        state = _initial_state()
        _write_state(path, state)
        return state
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read recipe storage: {exc}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("recipes"), list):
        raise RuntimeError("Recipe storage has an invalid format")
    return state


def _public_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    result = dict(recipe)
    minutes = int(result["cooking_time_minutes"])
    result["time"] = f"{minutes} мин"
    result["preview"] = str(result.get("description") or "")
    result["favoriteLabel"] = "В избранном" if result.get("favorite") else "В избранное"
    result["favoriteIcon"] = "star" if result.get("favorite") else "star-outline"
    return result


def _text_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.splitlines() if part.strip()]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [item.strip() for item in value if item.strip()]
    raise RecipeValidationError(f"{field} must be a string or a list of strings")


@tool(summary="List recipes with optional text search and favorite filter.", side_effects="read_only")
def list_recipes(query: str = "", favorites_only: bool = False) -> dict[str, Any]:
    normalized = str(query or "").strip().casefold()
    recipes = []
    for recipe in _load_state()["recipes"]:
        if favorites_only and not recipe.get("favorite", False):
            continue
        searchable = " ".join(
            [str(recipe.get("title", "")), str(recipe.get("description", "")), *recipe.get("ingredients", [])]
        ).casefold()
        if normalized and normalized not in searchable:
            continue
        recipes.append(_public_recipe(recipe))
    recipes.sort(key=lambda item: (str(item["title"]).casefold(), str(item["id"])))
    return {"ok": True, "recipes": recipes, "count": len(recipes)}


@tool(summary="Get one recipe by id.", side_effects="read_only")
def get_recipe(recipe_id: str) -> dict[str, Any]:
    wanted = str(recipe_id or "").strip()
    for recipe in _load_state()["recipes"]:
        if recipe.get("id") == wanted:
            return {"ok": True, "recipe": _public_recipe(recipe)}
    return {"ok": False, "error": "not_found", "message": "Рецепт не найден"}


@tool(summary="Add a recipe to local storage.", side_effects="local_write")
def add_recipe(
    title: str,
    cooking_time_minutes: int,
    description: str = "",
    ingredients: Any = None,
    steps: Any = None,
) -> dict[str, Any]:
    clean_title = str(title or "").strip()
    if not clean_title:
        raise RecipeValidationError("title is required")
    if isinstance(cooking_time_minutes, bool):
        raise RecipeValidationError("cooking_time_minutes must be a positive integer")
    try:
        minutes = int(cooking_time_minutes)
    except (TypeError, ValueError) as exc:
        raise RecipeValidationError("cooking_time_minutes must be a positive integer") from exc
    if minutes <= 0:
        raise RecipeValidationError("cooking_time_minutes must be a positive integer")

    state = _load_state()
    recipe = {
        "id": f"recipe-{uuid4().hex}",
        "title": clean_title,
        "cooking_time_minutes": minutes,
        "description": str(description or "").strip(),
        "ingredients": _text_list(ingredients, "ingredients"),
        "steps": _text_list(steps, "steps"),
        "favorite": False,
    }
    state["recipes"].append(recipe)
    _write_state(_storage_path(), state)
    return {"ok": True, "recipe": _public_recipe(recipe)}


@tool(summary="Toggle or explicitly set a recipe favorite flag.", side_effects="local_write")
def set_favorite(recipe_id: str, favorite: bool | None = None) -> dict[str, Any]:
    wanted = str(recipe_id or "").strip()
    state = _load_state()
    for recipe in state["recipes"]:
        if recipe.get("id") == wanted:
            recipe["favorite"] = not bool(recipe.get("favorite")) if favorite is None else bool(favorite)
            _write_state(_storage_path(), state)
            return {"ok": True, "recipe": _public_recipe(recipe)}
    return {"ok": False, "error": "not_found", "message": "Рецепт не найден"}
