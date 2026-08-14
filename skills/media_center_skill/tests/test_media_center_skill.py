from __future__ import annotations

import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from handlers import main
from media_center.catalog import MediaCenterRepository, SCHEMA_VERSION


def _resource(resource_id: str = "clip.mp4", *, source: str = "media_server") -> dict:
    suffix = Path(resource_id).suffix.lower()
    mime = "audio/mpeg" if suffix == ".mp3" else "image/jpeg" if suffix in {".jpg", ".jpeg"} else "video/mp4"
    return {
        "schema": "adaos.media.resource.v1",
        "id": resource_id,
        "resource_id": resource_id,
        "source": source,
        "name": resource_id,
        "mime_type": mime,
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


def test_playable_filter_excludes_images_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()

    repo.scan_resources([_resource("clip.mp4"), _resource("song.mp3"), _resource("poster.jpg")])
    playable = repo.list_items(media_kind="playable", sort="title", limit=20)["items"]

    assert [item["media_kind"] for item in playable] == ["video", "audio"]
    assert {item["resource_id"] for item in playable} == {"clip.mp4", "song.mp3"}


def test_library_defaults_to_playable_media(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()

    repo.scan_resources([_resource("clip.mp4"), _resource("song.mp3"), _resource("poster.jpg")])
    payload = main.library(auto_scan=False, sort="title", limit=20)

    assert [item["media_kind"] for item in payload["items"]] == ["video", "audio"]
    assert {item["resource_id"] for item in payload["items"]} == {"clip.mp4", "song.mp3"}


def test_incremental_root_scan_does_not_mark_existing_media_server_rows_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()

    repo.scan_resources([_resource("old.mp4")], source="media_server")
    repo.scan_resources([_resource("new.mp4")], source="media_server", mark_missing=False)
    available = repo.list_items(sort="title")["items"]

    assert {item["resource_id"] for item in available} == {"old.mp4", "new.mp4"}


def test_import_folder_registers_playable_files_without_copying(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    movie = media_dir / "movie.mp4"
    image = media_dir / "poster.jpg"
    movie.write_bytes(b"video")
    image.write_bytes(b"image")

    def register(path: Path, *, root: dict):
        resource = _resource(path.name)
        resource["source_path"] = str(path.resolve())
        resource["metadata"] = {"storage_mode": "reference"}
        return resource, None

    monkeypatch.setattr(main, "_register_media_file_descriptor", register)

    result = main.import_folder(path=str(media_dir), limit=20)
    listing = main.library(media_kind="playable", auto_scan=False, limit=20)

    assert result["ok"] is True
    assert result["registered_count"] == 1
    assert result["roots"][0]["path"] == str(media_dir.resolve())
    assert [item["resource_id"] for item in listing["items"]] == ["movie.mp4"]
    assert listing["items"][0]["source_path"] == str(movie.resolve())
    assert listing["items"][0]["resource"]["metadata"]["storage_mode"] == "reference"


def test_reference_registration_keeps_original_media_bytes(monkeypatch, tmp_path):
    media_dir = tmp_path / "library"
    media_dir.mkdir()
    movie = media_dir / "movie.mp4"
    movie.write_bytes(b"original-video")
    monkeypatch.setenv("ADAOS_MEDIA_REFERENCE_DB_PATH", str(tmp_path / "state" / "media_references.sqlite3"))

    descriptor, error = main._register_media_file_descriptor(
        movie,
        root={"id": "root-1", "path": str(media_dir)},
    )

    assert error is None
    assert descriptor is not None
    assert descriptor["path"] == str(movie.resolve())
    assert descriptor["source_path"] == str(movie.resolve())
    assert descriptor["metadata"]["storage_mode"] == "reference"
    assert descriptor["content_path"].startswith("/api/node/media/resources/content/ref_")
    assert list(tmp_path.rglob("*.mp4")) == [movie]


def test_playback_queue_puts_selection_first_and_clamps_to_ten(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()
    repo.scan_resources([_resource(f"clip-{index:02d}.mp4") for index in range(15)])
    selected = next(item for item in repo.list_items(limit=15)["items"] if item["resource_id"] == "clip-12.mp4")

    payload = main.playback_queue(
        item_id=selected["id"],
        media_kind="playable",
        sort="title",
        limit=100,
    )

    assert payload["ok"] is True
    assert payload["selected_item_id"] == selected["id"]
    assert payload["items"][0]["id"] == selected["id"]
    assert len(payload["items"]) == 10
    assert payload["pagination"] == {
        "limit": 10,
        "offset": 0,
        "next_offset": None,
        "has_more": False,
    }
    assert payload["capabilities"]["playlist"]["max_items"] == 10


def test_scan_roots_without_active_roots_returns_human_i18n_error(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))

    result = main.scan_roots(limit=20)

    assert result["ok"] is False
    assert result["error"] == "no_active_media_roots"
    assert result["message"] == "No active media folders are configured."
    assert result["human_message_i18n"] == {
        "key": "runtime.media_center.error.no_active_media_roots",
    }
    assert result["human_message"]
    assert result["roots"] == []


def test_skill_declares_media_center_i18n_resources() -> None:
    manifest = (SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8")
    webui = (SKILL_ROOT / "webui.json").read_text(encoding="utf-8")

    assert "webui:" in manifest
    assert "file: webui.json" in manifest
    assert '"media_center.i18n.en"' in webui
    assert '"path": "i18n/en.json"' in webui
    assert '"media_center.i18n.ru"' in webui
    assert '"path": "i18n/ru.json"' in webui
