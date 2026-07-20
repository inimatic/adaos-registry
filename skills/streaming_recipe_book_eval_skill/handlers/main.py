from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from adaos.sdk.core.decorators import tool


_LOCK = threading.RLock()
_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_SECONDS = 0.01
_CATEGORIES = {"breakfast", "soup", "main"}
_DEFAULT_IMAGE = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='960' height='640' viewBox='0 0 960 640'%3E%3Crect width='960' height='640' fill='%23f4ead7'/%3E%3Cpath d='M270 350h420c-18 120-95 180-210 180S288 470 270 350z' fill='%23d9825b'/%3E%3Ccircle cx='480' cy='275' r='115' fill='%23f7c873'/%3E%3C/svg%3E"
_SEED = [
    {"id": "bfk1", "title": "Овсянка с ягодами", "category": "breakfast", "prep_time_min": 10, "image": _DEFAULT_IMAGE, "image_alt": "Овсянка с ягодами", "ingredients": ["Овсяные хлопья — 60 г", "Молоко — 200 мл", "Ягоды — 80 г"], "steps": ["Доведите молоко до кипения.", "Варите хлопья 5 минут.", "Добавьте ягоды."], "favorite": False},
    {"id": "soup1", "title": "Томатный суп с базиликом", "category": "soup", "prep_time_min": 25, "image": _DEFAULT_IMAGE, "image_alt": "Томатный суп с базиликом", "ingredients": ["Томаты — 500 г", "Лук — 1 шт", "Базилик"], "steps": ["Обжарьте лук.", "Добавьте томаты и бульон.", "Измельчите и добавьте базилик."], "favorite": False},
    {"id": "main1", "title": "Куриная грудка с овощами", "category": "main", "prep_time_min": 30, "image": _DEFAULT_IMAGE, "image_alt": "Куриная грудка с овощами", "ingredients": ["Куриная грудка — 2 шт", "Перец — 1 шт", "Цукини — 1 шт"], "steps": ["Нарежьте овощи.", "Обжарьте курицу.", "Добавьте овощи и доведите до готовности."], "favorite": False},
]


def lang_res() -> dict[str, str]:
    return {"recipe.not_found": "Recipe not found", "recipe.invalid_category": "Unknown recipe category"}


def _data_path() -> Path:
    configured = os.getenv("ADAOS_SKILL_DATA_DIR", "").strip()
    if configured:
        root = Path(configured)
    else:
        # AdaOS points this at the runtime bucket's durable data/db store while
        # invoking a tool.  Keeping recipes beside that store makes data survive
        # source restaging and A/B slot activation.  The final fallback keeps
        # direct source-tree execution useful for local development.
        runtime_store = os.getenv("ADAOS_SKILL_ENV_PATH", "").strip()
        root = Path(runtime_store).parent if runtime_store else Path(__file__).resolve().parents[1] / "data"
    return root / "recipes.json"


def _normalize_lines(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip(" \t-*•") for line in str(value or "").splitlines() if line.strip(" \t-*•")]


def _decorate(recipe: dict[str, Any]) -> dict[str, Any]:
    item = dict(recipe)
    category_labels = {"breakfast": "Завтрак", "soup": "Суп", "main": "Основное"}
    item["time"] = f'{item["prep_time_min"]} мин'
    item["category_label"] = category_labels[item["category"]]
    item["meta"] = f'{item["category_label"]} • {item["time"]}'
    item["preview"] = ", ".join(item.get("ingredients", [])[:4])
    item["imageAlt"] = item.get("image_alt") or item["title"]
    return item


def _read() -> list[dict[str, Any]]:
    path = _data_path()
    with _LOCK:
        if not path.exists():
            _write(_SEED)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = list(_SEED)
        return [dict(item) for item in value if isinstance(item, dict)]


def _write(recipes: list[dict[str, Any]]) -> None:
    path = _data_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        fd, temp_name = tempfile.mkstemp(prefix="recipes-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(recipes, stream, ensure_ascii=False, indent=2)
            for attempt in range(_REPLACE_ATTEMPTS):
                try:
                    os.replace(temp_name, path)
                    break
                except PermissionError:
                    if attempt == _REPLACE_ATTEMPTS - 1:
                        raise
                    # Windows scanners and indexers can briefly hold the
                    # destination open. Keep the retry window short and
                    # bounded; a durable denial must remain visible.
                    time.sleep(_REPLACE_RETRY_SECONDS * (attempt + 1))
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _validated(category: str, prep_time_min: Any) -> tuple[str, int]:
    clean_category = str(category).strip().lower()
    if clean_category not in _CATEGORIES:
        raise ValueError("category must be breakfast, soup, or main")
    minutes = int(prep_time_min)
    if not 1 <= minutes <= 1440:
        raise ValueError("prep_time_min must be between 1 and 1440")
    return clean_category, minutes


@tool(summary="List and filter recipes.", side_effects="none")
def list_recipes(category: str = "all", max_time_min: int | None = None, query: str = "", favorites_only: bool = False) -> dict[str, Any]:
    needle = str(query).strip().casefold()
    items = []
    for recipe in _read():
        if category not in ("", "all") and recipe.get("category") != category:
            continue
        if max_time_min is not None and int(recipe.get("prep_time_min", 0)) > int(max_time_min):
            continue
        if favorites_only and not recipe.get("favorite", False):
            continue
        haystack = " ".join([str(recipe.get("title", "")), *recipe.get("ingredients", [])]).casefold()
        if needle and needle not in haystack:
            continue
        items.append(_decorate(recipe))
    return {"ok": True, "items": items, "count": len(items)}


@tool(summary="Get one complete recipe.", side_effects="none")
def get_recipe(recipe_id: str) -> dict[str, Any]:
    for recipe in _read():
        if recipe.get("id") == recipe_id:
            return {"ok": True, "recipe": _decorate(recipe)}
    return {"ok": False, "error": "recipe_not_found", "recipe_id": recipe_id}


@tool(summary="Add a recipe to skill-local storage.", side_effects="local_write")
def add_recipe(title: str, category: str, prep_time_min: int, ingredients: Any, steps: Any, image: str = "", image_alt: str = "") -> dict[str, Any]:
    clean_title = str(title).strip()
    if not clean_title:
        raise ValueError("title is required")
    clean_category, minutes = _validated(category, prep_time_min)
    with _LOCK:
        recipes = _read()
        slug = re.sub(r"[^a-z0-9]+", "-", clean_title.casefold()).strip("-")[:32] or "recipe"
        recipe = {"id": f"{slug}-{uuid.uuid4().hex[:8]}", "title": clean_title, "category": clean_category, "prep_time_min": minutes, "image": str(image).strip() or _DEFAULT_IMAGE, "image_alt": str(image_alt).strip() or clean_title, "ingredients": _normalize_lines(ingredients), "steps": _normalize_lines(steps), "favorite": False}
        recipes.append(recipe)
        _write(recipes)
    return {"ok": True, "recipe": _decorate(recipe), "items": [_decorate(item) for item in recipes]}


@tool(summary="Set or toggle a recipe favorite.", side_effects="local_write")
def set_favorite(recipe_id: str, favorite: bool | None = None) -> dict[str, Any]:
    with _LOCK:
        recipes = _read()
        for recipe in recipes:
            if recipe.get("id") == recipe_id:
                recipe["favorite"] = (not bool(recipe.get("favorite"))) if favorite is None else bool(favorite)
                _write(recipes)
                return {"ok": True, "recipe": _decorate(recipe), "items": [_decorate(item) for item in recipes]}
    return {"ok": False, "error": "recipe_not_found", "recipe_id": recipe_id}
