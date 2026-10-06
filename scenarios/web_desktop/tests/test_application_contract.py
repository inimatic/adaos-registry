import json
from pathlib import Path

import yaml


SCENARIO_DIR = Path(__file__).resolve().parents[1]


def test_management_declares_runtime_dependencies_and_locales() -> None:
    manifest = yaml.safe_load((SCENARIO_DIR / "scenario.yaml").read_text(encoding="utf-8"))

    assert manifest["id"] == "web_desktop"
    assert manifest["type"] == "desktop"
    assert set(manifest["supported_locales"]) == {"en", "ru"}
    assert "web_desktop_runtime_skill" in manifest["depends"]


def test_management_ui_is_a_valid_application_document() -> None:
    document = json.loads((SCENARIO_DIR / "webui.json").read_text(encoding="utf-8"))

    assert isinstance(document, dict)
    assert isinstance(document.get("ui"), dict)
    assert document["ui"].get("application")
