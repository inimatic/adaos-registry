from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from adaos.sdk.core.decorators import tool


_LOCK = threading.RLock()
_SEED = [
    {"id": "caesar", "title": "Салат Цезарь", "category": "Салаты", "time_minutes": 30,
     "difficulty": "Легко", "servings": 2, "description": "Классический салат с курицей и пармезаном.",
     "ingredients": ["Курица — 200 г", "Салат романо — 150 г", "Пармезан — 40 г", "Гренки — 50 г"],
     "steps": ["Обжарьте курицу.", "Смешайте салат, гренки и пармезан.", "Добавьте соус."], "favorite": False},
    {"id": "borscht", "title": "Борщ", "category": "Супы", "time_minutes": 60,
     "difficulty": "Средне", "servings": 4, "description": "Свекольный суп на наваристом бульоне.",
     "ingredients": ["Говядина — 400 г", "Свекла — 2 шт.", "Капуста — 200 г", "Картофель — 3 шт."],
     "steps": ["Сварите бульон.", "Добавьте овощи.", "Доведите до готовности."], "favorite": True},
]


def _store_path() -> Path:
    root = Path(os.getenv("ADAOS_SKILL_DATA_DIR") or Path(__file__).resolve().parents[1] / ".data")
    return root / "recipes.json"


def _initial() -> dict[str, Any]:
    return {"version": 1, "recipes": [dict(row) for row in _SEED], "shopping_list": []}


def _read() -> dict[str, Any]:
    path = _store_path()
    if not path.exists():
        return _initial()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Local recipe storage could not be read") from exc
    if not isinstance(data, dict) or not isinstance(data.get("recipes"), list) or not isinstance(data.get("shopping_list"), list):
        raise RuntimeError("Local recipe storage has an invalid format")
    return data


def _write(data: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="recipes-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _lines(value: str | list[str]) -> list[str]:
    source = value if isinstance(value, list) else str(value or "").splitlines()
    return [str(item).strip() for item in source if str(item).strip()]


@tool(summary="List and filter locally stored recipes.", side_effects="read_only")
def list_recipes(query: str = "", category: str = "", difficulty: str = "", max_time_minutes: int | None = None,
                 favorites_only: bool = False, **_: Any) -> dict[str, Any]:
    with _LOCK:
        rows = _read()["recipes"]
    needle = str(query or "").strip().casefold()
    result = [dict(row) for row in rows if
              (not needle or needle in str(row.get("title", "")).casefold() or needle in str(row.get("description", "")).casefold())
              and (not category or row.get("category") == category)
              and (not difficulty or row.get("difficulty") == difficulty)
              and (max_time_minutes is None or int(row.get("time_minutes", 0)) <= int(max_time_minutes))
              and (not favorites_only or bool(row.get("favorite")))]
    return {"ok": True, "items": result, "count": len(result), "empty": not result}


@tool(summary="Search locally stored recipes.", side_effects="read_only")
def search_recipes(query: str, **filters: Any) -> dict[str, Any]:
    return list_recipes(query=query, **filters)


@tool(summary="Read one locally stored recipe.", side_effects="read_only")
def get_recipe(recipe_id: str, **_: Any) -> dict[str, Any]:
    with _LOCK:
        recipe = next((dict(row) for row in _read()["recipes"] if row.get("id") == recipe_id), None)
    return {"ok": recipe is not None, "recipe": recipe, "error": None if recipe else "recipe_not_found"}


@tool(summary="Create a recipe in local atomic storage.", side_effects="local_write")
def create_recipe(title: str, category: str, time_minutes: int, difficulty: str, servings: int,
                  ingredients: str | list[str], steps: str | list[str], description: str = "", favorite: bool = False,
                  **_: Any) -> dict[str, Any]:
    clean_title, ingredient_rows, step_rows = str(title or "").strip(), _lines(ingredients), _lines(steps)
    if not clean_title or not ingredient_rows or not step_rows:
        return {"ok": False, "error": "title_ingredients_steps_required"}
    if int(time_minutes) < 1 or int(servings) < 1:
        return {"ok": False, "error": "positive_time_and_servings_required"}
    recipe = {"id": uuid.uuid4().hex, "title": clean_title, "category": str(category).strip(),
              "time_minutes": int(time_minutes), "difficulty": str(difficulty).strip(), "servings": int(servings),
              "description": str(description).strip(), "ingredients": ingredient_rows, "steps": step_rows,
              "favorite": bool(favorite)}
    with _LOCK:
        data = _read(); data["recipes"].append(recipe); _write(data)
    return {"ok": True, "recipe": recipe}


@tool(summary="Set a recipe favorite flag.", side_effects="local_write")
def set_favorite(recipe_id: str, favorite: bool, **_: Any) -> dict[str, Any]:
    with _LOCK:
        data = _read()
        recipe = next((row for row in data["recipes"] if row.get("id") == recipe_id), None)
        if recipe is None:
            return {"ok": False, "error": "recipe_not_found"}
        recipe["favorite"] = bool(favorite); _write(data)
    return {"ok": True, "recipe_id": recipe_id, "favorite": bool(favorite)}


@tool(summary="Add a recipe's ingredients to the local shopping list.", side_effects="local_write")
def add_ingredients_to_shopping_list(recipe_id: str, quantity_multiplier: float = 1.0, **_: Any) -> dict[str, Any]:
    if float(quantity_multiplier) <= 0:
        return {"ok": False, "error": "positive_quantity_required", "added_count": 0}
    with _LOCK:
        data = _read(); recipe = next((row for row in data["recipes"] if row.get("id") == recipe_id), None)
        if recipe is None:
            return {"ok": False, "error": "recipe_not_found", "added_count": 0}
        additions = [{"id": uuid.uuid4().hex, "recipe_id": recipe_id, "name": item,
                      "quantity_multiplier": float(quantity_multiplier), "checked": False} for item in recipe["ingredients"]]
        data["shopping_list"].extend(additions); _write(data)
    return {"ok": True, "added_count": len(additions), "quantity_multiplier": float(quantity_multiplier), "items": additions}


@tool(summary="Read the local shopping list.", side_effects="read_only")
def get_shopping_list(**_: Any) -> dict[str, Any]:
    with _LOCK:
        items = [dict(row) for row in _read()["shopping_list"]]
    return {"ok": True, "items": items, "count": len(items), "empty": not items}
