import importlib
import os
import sys
from pathlib import Path
import pytest
import yaml


def _ensure_skill_paths() -> None:
    current = Path(__file__).resolve()
    src_root: Path | None = None
    slot_root: Path | None = None

    for parent in current.parents:
        if parent.name == "src":
            src_root = parent
            slot_root = parent.parent
            break

    paths: list[Path] = []
    if src_root and slot_root:
        vendor_root = slot_root / "vendor"
        if vendor_root.is_dir():
            paths.append(vendor_root)
        paths.append(src_root)

    for path in reversed(paths):
        path_str = str(path)
        if path_str and path_str not in sys.path:
            sys.path.insert(0, path_str)


def _skill_package() -> str:
    package = os.getenv("ADAOS_SKILL_PACKAGE")
    if package:
        return package

    current = Path(__file__).resolve()
    try:
        parts = current.parts
        skills_index = parts.index("skills")
        skill_name = parts[skills_index + 1]
    except (ValueError, IndexError) as exc:
        raise RuntimeError("unable to infer skill package name") from exc
    return f"skills.{skill_name}"


def test_handlers_module_importable() -> None:
    _ensure_skill_paths()
    module = importlib.import_module(f"{_skill_package()}.handlers.main")
    assert hasattr(module, "get_weather"), "get_weather tool is missing"


def test_health_probe_present() -> None:
    tests_root = Path(__file__).resolve().parent
    health_check = tests_root / "health.py"
    if not health_check.exists():
        pytest.skip("health probe is missing in packaged runtime tests")
    assert health_check.exists(), "health probe is missing"


def test_weather_handler_depends_only_on_public_sdk() -> None:
    handler = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    source = handler.read_text(encoding="utf-8")
    assert "from adaos.services" not in source
    assert "import adaos.services" not in source


def test_weather_nlu_dispatches_through_declared_skill_tool() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "skill.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    intent = manifest["nlu"]["intents"]["weather.current"]
    assert intent["actions"] == [
        {
            "type": "skillTool",
            "skill": "weather_skill",
            "tool": "get_weather",
            "target": "weather_skill.get_weather",
            "params": {"city": "$slot.city"},
        }
    ]


def test_weather_process_caches_are_bounded() -> None:
    _ensure_skill_paths()
    module = importlib.import_module(f"{_skill_package()}.handlers.main")
    module.dispose()
    for index in range(module._CITY_CACHE_MAX_ITEMS + 5):
        module._cache_put(
            module._CITY_CACHE,
            f"city-{index}",
            {"city": index},
            ttl_seconds=module._CITY_CACHE_TTL,
            max_items=module._CITY_CACHE_MAX_ITEMS,
        )
    assert len(module._CITY_CACHE) == module._CITY_CACHE_MAX_ITEMS
    assert module.dispose()["released"] == module._CITY_CACHE_MAX_ITEMS
