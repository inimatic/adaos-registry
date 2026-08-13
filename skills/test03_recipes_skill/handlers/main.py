from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from adaos.sdk.core.decorators import tool
from adaos.sdk.data.skill_memory import get as memory_get
from adaos.sdk.data.skill_memory import set as memory_set


_RECIPES_KEY = "recipes.v1"
_FAVORITES_KEY = "favorites.v1"

_DEFAULT_RECIPES: tuple[dict[str, Any], ...] = (
    {"id": "r1", "title": "Салат с киноа и авокадо", "category": "Салаты", "preview": "Лёгкий и питательный салат с киноа, авокадо и свежими овощами.", "image": "mock-recipe-salad", "time": "20 мин", "timeMinutes": 20, "difficulty": "Легко", "popularity": 97, "addedAt": "2026-06-15"},
    {"id": "r2", "title": "Паста карбонара", "category": "Основные блюда", "preview": "Классическая итальянская паста с беконом, яйцом и сыром.", "image": "mock-recipe-carbonara", "time": "25 мин", "timeMinutes": 25, "difficulty": "Средне", "popularity": 92, "addedAt": "2026-07-01"},
    {"id": "r3", "title": "Куриное карри с кокосом", "category": "Основные блюда", "preview": "Ароматное карри на кокосовом молоке с рисом басмати.", "image": "mock-recipe-curry", "time": "40 мин", "timeMinutes": 40, "difficulty": "Средне", "popularity": 88, "addedAt": "2026-07-10"},
    {"id": "r4", "title": "Чизкейк без выпечки", "category": "Десерты", "preview": "Нежный чизкейк с ягодным соусом без выпечки.", "image": "mock-recipe-cheesecake", "time": "15 мин + охлаждение", "timeMinutes": 75, "difficulty": "Легко", "popularity": 90, "addedAt": "2026-07-20"},
)


def lang_res() -> dict[str, str]:
    return {}


def _recipes() -> list[dict[str, Any]]:
    stored = memory_get(_RECIPES_KEY)
    if not isinstance(stored, list):
        stored = [dict(item) for item in _DEFAULT_RECIPES]
        memory_set(_RECIPES_KEY, stored)
    return [dict(item) for item in stored if isinstance(item, dict)]


def _favorites(recipes: list[dict[str, Any]]) -> list[str]:
    valid_ids = {str(item.get("id")) for item in recipes}
    stored = memory_get(_FAVORITES_KEY)
    if not isinstance(stored, list):
        stored = ["r2"]
    cleaned = list(dict.fromkeys(str(item) for item in stored if str(item) in valid_ids))
    if cleaned != stored:
        memory_set(_FAVORITES_KEY, cleaned)
    return cleaned


def _snapshot() -> dict[str, Any]:
    recipes = _recipes()
    favorites = _favorites(recipes)
    return {"ok": True, "recipes": recipes, "favoriteIds": favorites, "count": len(recipes)}


@tool(summary="Load the durable recipe catalog and favorites.", side_effects="read_only")
def list_recipes() -> dict[str, Any]:
    return _snapshot()


@tool(summary="Add a recipe to the durable local catalog.", side_effects="local_write")
def add_recipe(
    title: str,
    category: str,
    time: int,
    difficulty: str,
    ingredients: str = "",
    steps: str = "",
    image: str = "",
    preview: str = "",
    added_at: str = "",
) -> dict[str, Any]:
    clean_title = str(title or "").strip()
    clean_category = str(category or "").strip()
    clean_difficulty = str(difficulty or "").strip()
    try:
        minutes = int(time)
    except (TypeError, ValueError) as exc:
        raise ValueError("time must be a positive integer") from exc
    if not clean_title or not clean_category or not clean_difficulty or minutes < 1:
        raise ValueError("title, category, difficulty and a positive time are required")

    recipes = _recipes()
    fingerprint = "\x1f".join((clean_title.casefold(), clean_category.casefold(), str(minutes)))
    base_id = "recipe-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
    existing_ids = {str(item.get("id")) for item in recipes}
    recipe_id = base_id
    suffix = 2
    while recipe_id in existing_ids:
        recipe_id = f"{base_id}-{suffix}"
        suffix += 1
    recipe = {
        "id": recipe_id,
        "title": clean_title,
        "category": clean_category,
        "preview": str(preview or steps or ingredients or "").strip(),
        "image": str(image or "").strip(),
        "time": f"{minutes} мин",
        "timeMinutes": minutes,
        "difficulty": clean_difficulty,
        "ingredients": str(ingredients or "").strip(),
        "steps": str(steps or "").strip(),
        "popularity": 0,
        "addedAt": str(added_at or date.today().isoformat()),
    }
    recipes.append(recipe)
    memory_set(_RECIPES_KEY, recipes)
    result = _snapshot()
    result["recipe"] = recipe
    return result


@tool(summary="Toggle a recipe in the durable favorites selection.", side_effects="local_write")
def toggle_favorite(recipe_id: str) -> dict[str, Any]:
    clean_id = str(recipe_id or "").strip()
    recipes = _recipes()
    if clean_id not in {str(item.get("id")) for item in recipes}:
        raise ValueError(f"unknown recipe id: {clean_id}")
    favorites = _favorites(recipes)
    if clean_id in favorites:
        favorites.remove(clean_id)
        is_favorite = False
    else:
        favorites.append(clean_id)
        is_favorite = True
    memory_set(_FAVORITES_KEY, favorites)
    result = _snapshot()
    result["recipeId"] = clean_id
    result["isFavorite"] = is_favorite
    return result
