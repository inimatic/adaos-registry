from __future__ import annotations

import json
import pathlib
import sys

import yaml
from jsonschema import Draft202012Validator


SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parents[3]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


def _load_webui_schema() -> dict:
    path = REPO_ROOT / "src" / "adaos" / "abi" / "webui.v1.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_declares_diagnostics_tool() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))

    tools = {item["name"]: item for item in manifest["tools"]}
    assert "get_diagnostics" in tools
    assert tools["get_diagnostics"]["entry"] == "handlers.main:get_diagnostics"
    assert "media" in tools["get_diagnostics"]["output_schema"]["required"]
    assert "reliability" in tools["get_diagnostics"]["output_schema"]["required"]


def test_webui_declares_diagnostics_modal_without_full_library_source() -> None:
    webui = json.loads((SKILL_ROOT / "webui.json").read_text(encoding="utf-8"))
    Draft202012Validator(_load_webui_schema()).validate(webui)

    app_ids = {item["id"] for item in webui["apps"]}
    assert "mediaserver_diagnostics_app" in app_ids

    modal = webui["registry"]["modals"]["mediaserver_diagnostics_modal"]["schema"]
    widgets = {item["id"]: item for item in modal["widgets"]}
    cards = widgets["mediaserver-diagnostic-cards"]
    assert cards["type"] == "ui.list"
    assert cards["dataSource"] == {
        "kind": "skill",
        "name": "mediaserver.get_diagnostics",
        "params": {"source": "webui.diagnostics"},
    }
    assert widgets["mediaserver-runtime-reliability"]["dataSource"] == {
        "kind": "y",
        "transform": "runtime.reliability",
    }
    assert "data/media/library" not in json.dumps(modal)


def test_get_diagnostics_returns_compact_operator_evidence(monkeypatch) -> None:
    from handlers import main

    monkeypatch.setattr(
        main,
        "list_media_files",
        lambda: [
            {"name": "clip-a.mp4", "size_bytes": 120},
            {"name": "clip-b.mp4", "size_bytes": 80},
        ],
    )
    monkeypatch.setattr(
        main,
        "media_runtime_snapshot",
        lambda items: {
            "available": True,
            "assessment": {"state": "ready", "reason": "media runtime ready"},
            "counts": {"file_total": len(items), "total_bytes": 200},
            "recommended_path": "direct_local_http",
            "paths": {"direct_local_http": {"ready": True}},
        },
    )
    monkeypatch.setattr(main, "_guard_cards", lambda runtime, webspace_id: [{"id": "guard:yjs_projection"}])
    monkeypatch.setattr(
        main,
        "_safe_reliability_payload",
        lambda webspace_id: (
            {
                "node": {"node_id": "hub-1"},
                "runtime": {
                    "yjs_projection_guard": {
                        "totals": {"guarded": 1},
                        "items": [
                            {
                                "owner": "skill:mediaserver",
                                "slot": "mediaserver.library",
                                "path": "data/media/library",
                                "payload_bytes": 70000,
                                "max_payload_bytes": 65536,
                                "reason": "payload_too_large",
                            }
                        ],
                    },
                    "yjs_pressure": {"observed_state": "ready", "reason": "healthy"},
                    "webio_stream_guard": {"totals": {"suppressed": 0, "throttled": 0}},
                    "state_sync": {"status": "ready"},
                },
            },
            None,
        ),
    )

    payload = main.get_diagnostics(webspace_id="desktop")

    assert payload["ok"] is True
    assert payload["schema"] == "mediaserver.diagnostics.v1"
    assert payload["media"]["projection"]["item_total"] == 2
    assert payload["media"]["projection"]["total_bytes"] == 200
    assert payload["reliability"]["guard_cards"] == [{"id": "guard:yjs_projection"}]
    assert {item["id"] for item in payload["items"]} >= {
        "mediaserver.full_list_projection_contract",
        "mediaserver.yjs_projection_guard",
        "mediaserver.media_runtime",
    }
    serialized = json.dumps(payload)
    assert "clip-a.mp4" not in serialized
    assert "clip-b.mp4" not in serialized
