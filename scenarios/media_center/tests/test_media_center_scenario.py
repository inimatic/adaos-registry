from __future__ import annotations

import json
from pathlib import Path


SCENARIO_ROOT = Path(__file__).resolve().parents[1]


def test_media_center_ui_declares_runtime_i18n_resources_and_long_import_timeouts() -> None:
    webui = json.loads((SCENARIO_ROOT / "webui.json").read_text(encoding="utf-8"))
    app = webui["ui"]["application"]
    resources = app["resources"]

    assert resources["media_center.i18n.en"]["role"] == "i18n"
    assert resources["media_center.i18n.en"]["locale"] == "en"
    assert resources["media_center.i18n.ru"]["role"] == "i18n"
    assert resources["media_center.i18n.ru"]["locale"] == "ru"

    page = app["desktop"]["pageSchema"]
    actions = [
        action
        for widget in page["widgets"]
        for action in widget.get("actions", [])
        if action.get("target") in {
            "media_center_skill.import_folder",
            "media_center_skill.scan_roots",
        }
    ]
    assert {action["target"]: action["timeoutMs"] for action in actions} == {
        "media_center_skill.import_folder": 600000,
        "media_center_skill.scan_roots": 600000,
    }

    library_sources = [
        widget["dataSource"]
        for widget in page["widgets"]
        if widget.get("dataSource", {}).get("name") == "media_center_skill.library"
    ]
    catalog_source = next(source for source in library_sources if source["params"].get("query") == "$state.mediaSearch")
    assert catalog_source["params"]["media_kind"] == {
        "kind": "expression",
        "op": "if",
        "condition": "$state.mediaKind",
        "then": "$state.mediaKind",
        "else": "playable",
    }

    en = json.loads((SCENARIO_ROOT / "assets" / "i18n" / "en.json").read_text(encoding="utf-8"))
    ru = json.loads((SCENARIO_ROOT / "assets" / "i18n" / "ru.json").read_text(encoding="utf-8"))
    key = "runtime.media_center.error.no_active_media_roots"
    assert en[key]
    assert ru[key]
