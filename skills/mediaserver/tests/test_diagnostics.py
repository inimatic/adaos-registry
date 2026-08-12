from __future__ import annotations

import json
import pathlib
import sys
from importlib import resources

import yaml
from jsonschema import Draft202012Validator


SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


def _find_repo_schema_path() -> pathlib.Path | None:
    marker = pathlib.Path("src") / "adaos" / "abi" / "webui.v1.schema.json"
    candidates = [
        pathlib.Path.cwd(),
        pathlib.Path.cwd() / "adaos",
        *SKILL_ROOT.parents,
        pathlib.Path("/root/adaos"),
    ]
    for root in candidates:
        if (root / marker).exists():
            return root / marker
    return None


REPO_SCHEMA_PATH = _find_repo_schema_path()
if REPO_SCHEMA_PATH is not None:
    repo_src = REPO_SCHEMA_PATH.parents[2]
    if str(repo_src) not in sys.path:
        sys.path.insert(0, str(repo_src))


def _load_webui_schema() -> dict:
    if REPO_SCHEMA_PATH is not None:
        return json.loads(REPO_SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        path = resources.files("adaos.abi").joinpath("webui.v1.schema.json")
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ModuleNotFoundError):
        raise FileNotFoundError("Cannot find adaos.abi webui.v1.schema.json")


def _load_skill_schema() -> dict:
    if REPO_SCHEMA_PATH is not None:
        path = REPO_SCHEMA_PATH.parents[3] / "src" / "adaos" / "services" / "skill" / "skill_schema.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    from adaos.services.skill.validation import SCHEMA_PATH

    return json.loads(pathlib.Path(SCHEMA_PATH).read_text(encoding="utf-8"))


def test_manifest_declares_diagnostics_tool() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))
    Draft202012Validator(_load_skill_schema()).validate(manifest)

    tools = {item["name"]: item for item in manifest["tools"]}
    routes = {item["surface"]: item for item in manifest["data_routes"]}
    assert "get_diagnostics" in tools
    assert tools["get_diagnostics"]["entry"] == "handlers.main:get_diagnostics"
    assert "media" in tools["get_diagnostics"]["output_schema"]["required"]
    assert "reliability" in tools["get_diagnostics"]["output_schema"]["required"]
    assert tools["get_snapshot"]["entry"] == "handlers.main:get_snapshot"
    assert tools["list_library_page"]["entry"] == "handlers.main:list_library_page"
    assert "max_items" not in routes["widget:mediaserver.summary"]["budget"]
    assert routes["widget:mediaserver.summary"]["projection_slot"] == "mediaserver.library_summary"
    assert routes["widget:mediaserver.summary"]["path"] == "data/media/library_summary"
    assert routes["modal:mediaserver.library_page"]["budget"]["max_items"] == 100
    assert routes["modal:mediaserver.library_page"]["budget"]["snapshot_policy"] == "manual"


def test_webui_declares_diagnostics_modal_without_full_library_source() -> None:
    webui = json.loads((SKILL_ROOT / "webui.json").read_text(encoding="utf-8"))
    Draft202012Validator(_load_webui_schema()).validate(webui)

    app_ids = {item["id"] for item in webui["apps"]}
    assert "mediaserver_diagnostics_app" in app_ids
    assert webui["widgets"][0]["dataSource"] == {
        "kind": "skill",
        "name": "mediaserver.list_library_page",
        "params": {"limit": 50, "source": "webui.widget"},
    }
    assert webui["widgets"][0]["inputs"]["showDiagnostics"] is False
    assert webui["widgets"][0]["inputs"]["autoSelectFirst"] is False

    media_modal = webui["registry"]["modals"]["mediaserver_modal"]["schema"]
    media_widget = media_modal["widgets"][0]
    assert media_widget["dataSource"] == {
        "kind": "skill",
        "name": "mediaserver.list_library_page",
        "params": {"limit": 50, "source": "webui.modal"},
    }
    assert media_widget["inputs"]["showDiagnostics"] is False
    assert media_widget["inputs"]["autoSelectFirst"] is False

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
        "media_library_summary",
        lambda: {"count": 2, "total_bytes": 200, "latest_modified_at": "2026-06-26T00:00:00+00:00"},
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
                                "slot": "mediaserver.library_summary",
                                "path": "data/media/library_summary",
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


def test_get_snapshot_projects_constant_size_summary_for_large_library(monkeypatch) -> None:
    from handlers import main

    projected: list[dict] = []
    monkeypatch.setattr(
        main,
        "media_library_summary",
        lambda: {
            "count": 500_000,
            "total_bytes": 8_000_000_000_000,
            "latest_modified_at": "2026-06-26T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        main,
        "media_runtime_snapshot",
        lambda items: {
            "available": True,
            "assessment": {"state": "ready", "reason": "media runtime ready"},
            "counts": {"file_total": len(items), "total_bytes": 0},
            "recommended_path": "direct_local_http",
            "paths": {"direct_local_http": {"ready": True}},
        },
    )
    monkeypatch.setattr(main, "media_capabilities", lambda: {"status": "ready", "notes": ["ok"]})
    monkeypatch.setattr(main, "list_media_files", lambda: (_ for _ in ()).throw(AssertionError("full list used")))
    monkeypatch.setattr(main, "_publish_snapshot", lambda snapshot, **_kwargs: projected.append(snapshot))

    payload = main.get_snapshot(webspace_id="desktop")
    serialized = json.dumps(payload)

    assert payload["schema"] == "mediaserver.library_summary.v1"
    assert payload["items"] == []
    assert payload["count"] == 500_000
    assert payload["library"]["route"]["name"] == "mediaserver.list_library_page"
    assert payload["library"]["projection"]["slot"] == "mediaserver.library_summary"
    assert payload["library"]["projection"]["path"] == "data/media/library_summary"
    assert "route_profiles" not in json.dumps(payload)
    assert len(serialized) < 20_000
    assert projected and projected[0]["items"] == []


def test_list_library_page_clamps_rows_and_keeps_summary(monkeypatch) -> None:
    from handlers import main

    def fake_page(**kwargs):
        assert kwargs["limit"] == 100
        return {
            "ok": True,
            "items": [{"name": f"clip-{idx}.mp4", "size_bytes": idx} for idx in range(100)],
            "pagination": {
                "limit": 100,
                "offset": 0,
                "cursor": "",
                "next_cursor": "cursor-2",
                "has_more": True,
                "total_count": 500_000,
                "scanned_count": 500_000,
            },
            "summary": {
                "count": 500_000,
                "total_bytes": 8_000_000_000_000,
                "query": "",
                "mime_type": "",
            },
        }

    monkeypatch.setattr(main, "list_media_files_page", fake_page)
    monkeypatch.setattr(main, "media_capabilities", lambda: {"status": "ready", "notes": []})
    monkeypatch.setattr(
        main,
        "media_runtime_snapshot",
        lambda items: {"available": True, "counts": {"file_total": len(items), "total_bytes": 0}},
    )

    payload = main.list_library_page(_payload={"limit": 500})

    assert payload["ok"] is True
    assert len(payload["items"]) == 100
    assert payload["pagination"]["next_cursor"] == "cursor-2"
    assert payload["pagination"]["total_count"] == 500_000
    assert payload["summary"]["value"] == 500_000
    assert "route_profiles" not in json.dumps(payload)
