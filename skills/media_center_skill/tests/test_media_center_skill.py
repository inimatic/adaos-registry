from __future__ import annotations

import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from handlers import main
from media_center.catalog import MediaCenterRepository, SCHEMA_VERSION


def _resource(resource_id: str = "clip.mp4", *, source: str = "media_server") -> dict:
    return {
        "schema": "adaos.media.resource.v1",
        "id": resource_id,
        "resource_id": resource_id,
        "source": source,
        "name": "demo_clip.mp4",
        "mime_type": "video/mp4",
        "size_bytes": 1024,
        "modified_at": "2026-08-11T10:00:00+00:00",
        "content_path": f"/api/node/media/files/content/{resource_id}",
        "routed_content_path": f"/media/files/content/{resource_id}",
        "metadata": {"fixture": True},
    }


def test_catalog_scans_lists_and_plans_playback(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()

    scan = repo.scan_resources([_resource()])
    listing = repo.list_items()
    item = listing["items"][0]
    plan = repo.playback_plan(item["id"])
    favorite = repo.set_favorite(item["id"], favorite=True)

    assert scan["ok"] is True
    assert scan["schema"] == SCHEMA_VERSION
    assert listing["summary"]["available_count"] == 1
    assert item["source"] == "media_server"
    assert item["media_kind"] == "video"
    assert item["resource"]["schema"] == "adaos.media.resource.v1"
    assert plan["playback"]["preferred_path"] == "/media/files/content/clip.mp4"
    assert favorite["item"]["favorite"] is True


def test_library_auto_scan_uses_sdk_discovery_boundary(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    monkeypatch.setattr(main, "_discover_resources", lambda source="all", limit=5000: ([_resource("song.mp3")], {"ok": True}))

    payload = main.library(auto_scan=True, limit=20)

    assert payload["ok"] is True
    assert payload["scan"]["discovered_count"] == 1
    assert payload["runtime"]["resource_boundary"] == "adaos.sdk.io.media.list_media_resources"
    assert payload["items"][0]["playable"] is True


def test_catalog_marks_previous_rows_missing_for_scanned_source(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()

    repo.scan_resources([_resource("old.mp4")], source="media_server")
    repo.scan_resources([_resource("new.mp4")], source="media_server")
    available = repo.list_items()["items"]
    all_items = repo.list_items(include_missing=True)["items"]

    assert [item["resource_id"] for item in available] == ["new.mp4"]
    assert {item["resource_id"] for item in all_items} == {"old.mp4", "new.mp4"}
    assert any(item["resource_id"] == "old.mp4" and item["missing"] for item in all_items)
