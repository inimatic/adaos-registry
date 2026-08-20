from __future__ import annotations

import json
import sqlite3
import sys
import time
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from media_center.catalog import MediaCenterRepository, SCHEMA_VERSION  # noqa: E402
from media_center.coordinator import MediaCatalogCoordinator  # noqa: E402
from media_center.enrichment import MediaEnrichmentWorker  # noqa: E402
from media_center.topology import MediaCenterTopology  # noqa: E402


_HANDLER_SPEC = importlib.util.spec_from_file_location(
    "media_center_skill_handlers_main", SKILL_ROOT / "handlers" / "main.py"
)
assert _HANDLER_SPEC and _HANDLER_SPEC.loader
main = importlib.util.module_from_spec(_HANDLER_SPEC)
_HANDLER_SPEC.loader.exec_module(main)


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


def _agent_delta(
    sequence: int,
    relative_path: str,
    *,
    kind: str = "audio",
    revision: int = 1,
    operation: str = "added",
) -> dict:
    name = Path(relative_path).name
    source_id = f"source-{sequence}"
    mime = "audio/mpeg" if kind == "audio" else "video/mp4"
    folder = Path(relative_path).parent.as_posix()
    return {
        "schema": "adaos.media_library.source_delta.v1",
        "id": f"delta-{sequence}-{revision}",
        "sequence": sequence,
        "agent_id": "agent-node-a",
        "node_id": "node-a",
        "root_id": "root-a",
        "source_id": source_id,
        "operation": operation,
        "source_revision": revision,
        "job_id": "scan-a",
        "created_at": "2026-08-19T00:00:00+00:00",
        "source": {
            "schema": "adaos.media_library.source.v1",
            "id": source_id,
            "root_id": "root-a",
            "node_id": "node-a",
            "relative_path": relative_path,
            "folder_path": folder,
            "name": name,
            "media_kind": kind,
            "mime_type": mime,
            "size_bytes": 1000 + sequence,
            "modified_ns": sequence,
            "inode": sequence,
            "fingerprint": f"fingerprint-{sequence}-{revision}",
            "resource_id": f"ref-{sequence}",
            "descriptor": {
                "schema": "adaos.media.resource.v1",
                "id": f"ref-{sequence}",
                "resource_id": f"ref-{sequence}",
                "name": name,
                "mime_type": mime,
                "content_path": f"/api/node/media/resources/content/ref-{sequence}",
                "routed_content_path": f"/media/resources/content/ref-{sequence}",
                "source_path": f"/mnt/library/{relative_path}",
                "metadata": {"storage_mode": "reference", "folder_segments": list(Path(folder).parts)},
            },
            "metadata": {"storage_mode": "reference", "folder_segments": list(Path(folder).parts)},
            "present": operation != "removed",
            "first_seen_at": "2026-08-19T00:00:00+00:00",
            "last_seen_at": "2026-08-19T00:00:00+00:00",
            "revision": revision,
        },
    }


def _agent_page(*deltas: dict) -> dict:
    return {
        "ok": True,
        "schema": "adaos.media_library_agent.v1",
        "items": list(deltas),
        "count": len(deltas),
        "cursor": "",
        "next_cursor": f"cursor-{len(deltas)}",
        "has_more": False,
        "agent": {"id": "agent-node-a", "node_id": "node-a"},
    }


def _validate_schema(filename: str, payload: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = __import__("json").loads(
        (SKILL_ROOT / "schemas" / filename).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(payload)


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
    monkeypatch.setattr(
        main,
        "_sync_agents",
        lambda *_args, **_kwargs: {"ok": False, "error": "agent_unavailable"},
    )

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


def test_kind_and_favorites_filters_are_exact(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()
    repo.scan_resources([_resource("clip.mp4"), _resource("song.mp3"), _resource("poster.jpg")])
    clip = next(item for item in repo.list_items(limit=20)["items"] if item["resource_id"] == "clip.mp4")
    repo.set_favorite(clip["id"], favorite=True)

    videos = main.library(media_kind="video", auto_scan=False, limit=20)["items"]
    audio = main.library(media_kind="audio", auto_scan=False, limit=20)["items"]
    favorites = main.library(
        media_kind="playable",
        favorites_only=True,
        auto_scan=False,
        limit=20,
    )["items"]

    assert [item["resource_id"] for item in videos] == ["clip.mp4"]
    assert [item["resource_id"] for item in audio] == ["song.mp3"]
    assert [item["resource_id"] for item in favorites] == ["clip.mp4"]


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


def test_discovery_excludes_legacy_media_center_copies(monkeypatch):
    from adaos.sdk.io import media as media_sdk

    legacy = _resource("media-center-0123456789abcdef01234567-import.mp4")
    current = _resource("movie.mp4")
    current["metadata"] = {"storage_mode": "reference"}
    monkeypatch.setattr(media_sdk, "list_media_resources", lambda **_: [legacy, current])

    resources, status = main._discover_resources()

    assert status == {"ok": True}
    assert [item["name"] for item in resources] == ["movie.mp4"]


def test_schema_retires_pre_reference_media_center_catalog_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    legacy = _resource("media-center-0123456789abcdef01234567-import.mp4")
    legacy["metadata"] = {"namespace": "media-center", "variant": "import"}
    repo = MediaCenterRepository()
    repo.scan_resources([legacy], source="media_server", mark_missing=False)

    migration = repo.ensure_schema(force=True)
    available = repo.list_items()["items"]
    all_items = repo.list_items(include_missing=True)["items"]

    assert migration["retired_legacy_count"] == 1
    assert available == []
    assert all_items[0]["missing"] is True


def test_current_schema_reopens_without_waiting_for_a_writer(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/track.mp3")))
    writer = sqlite3.connect(str(catalog.repository.db_path), timeout=1)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        reopened = MediaCatalogCoordinator(MediaCenterRepository())
        elapsed = time.monotonic() - started
        assert reopened.list_items(media_kind="audio", limit=1)["count"] == 1
    finally:
        writer.rollback()
        writer.close()

    assert elapsed < 1.0


def test_playback_variant_lookup_has_durable_indexes(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())

    with catalog.repository.connect() as connection:
        catalog_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(catalog_items)")
        }
        variant_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(media_variants)")
        }

    assert "idx_media_center_catalog_variant" in catalog_indexes
    assert "idx_media_center_variant_work" in variant_indexes


def test_agent_delta_retires_same_path_legacy_catalog_row(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    repo = MediaCenterRepository()
    legacy = _resource("legacy-track.mp3")
    legacy["source_path"] = "/mnt/library/Music/track-1.mp3"
    legacy["path"] = legacy["source_path"]
    repo.scan_resources([legacy], source="media_server", mark_missing=False)
    catalog = MediaCatalogCoordinator(repo)

    applied = catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Music/track-1.mp3"))
    )
    available = catalog.list_items(media_kind="audio", limit=30)["items"]
    all_items = catalog.list_items(
        media_kind="audio", include_missing=True, limit=30
    )["items"]

    assert applied["applied_count"] == 1
    assert len(available) == 1
    assert available[0]["agent_id"] == "agent-node-a"
    assert len(all_items) == 2
    assert next(item for item in all_items if not item["agent_id"])["missing"] is True


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
    assert "source_path" not in listing["items"][0]
    assert listing["items"][0]["resource"]["metadata"]["storage_mode"] == "reference"


def test_catalog_projection_redacts_local_paths_and_embedded_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    delta = _agent_delta(1, "Author/Book/001.mp3")
    descriptor = delta["source"]["descriptor"]
    descriptor["path"] = "/mnt/library/Author/Book/001.mp3"
    descriptor["content_ref"] = "root-a:/mnt/library/Author/Book/001.mp3"
    descriptor["direct_urls"] = [
        "http://node-a.local/media/ref-1?token=private-token"
    ]
    descriptor["content_url_candidates"] = [
        "http://user:password@node-a.local/media/ref-1"
    ]
    descriptor["content_path"] += "?token=private-token"
    descriptor["routed_content_path"] += "?token=private-token"
    descriptor["metadata"].update(
        {
            "media_center_root_path": "/mnt/library",
            "content_ref": "root-a:/mnt/library/Author/Book/001.mp3",
            "access_token": "private-token",
        }
    )
    catalog.apply_agent_page(_agent_page(delta), instance_id="instance-a")

    item = catalog.list_items(query="Author Book", media_kind="audio")["items"][0]
    serialized = __import__("json").dumps(item, sort_keys=True)
    plan = catalog.playback_plan(item["id"])
    serialized_plan = __import__("json").dumps(plan, sort_keys=True)

    assert item["folder_path"] == "Author/Book"
    assert item["content_path"] == "/api/node/media/resources/content/ref-1"
    assert item["routed_content_path"] == "/media/resources/content/ref-1"
    assert "source_path" not in item
    assert "/mnt/" not in serialized
    assert "private-token" not in serialized
    assert "content_ref" not in serialized
    assert "direct_urls" not in serialized
    assert plan["route"]["direct_candidates"] == ["http://node-a.local/media/ref-1"]
    assert plan["route"]["node_path"] == "/api/node/media/resources/content/ref-1"
    assert plan["route"]["routed_path"] == "/media/resources/content/ref-1"
    assert "/mnt/" not in serialized_plan
    assert "private-token" not in serialized_plan


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


def test_delete_root_removes_catalog_and_core_links_but_preserves_media(monkeypatch, tmp_path):
    from adaos.services import media_core

    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    reference_db = tmp_path / "state" / "media_references.sqlite3"
    monkeypatch.setenv("ADAOS_MEDIA_REFERENCE_DB_PATH", str(reference_db))
    media_dir = tmp_path / "library"
    media_dir.mkdir()
    movie = media_dir / "movie.mp4"
    movie.write_bytes(b"original-video")
    imported = main.import_folder(path=str(media_dir), limit=20)
    listing = main.library(auto_scan=False, limit=20)
    resource_id = listing["items"][0]["resource_id"]
    root_id = imported["root"]["id"]

    deleted = main.delete_root(root_id=root_id)

    assert deleted["ok"] is True
    assert deleted["deleted"] is True
    assert deleted["deleted_item_count"] == 1
    assert deleted["resource_cleanup"]["deleted_count"] == 1
    assert main.list_roots()["items"] == []
    assert main.library(auto_scan=False, include_missing=True, limit=20)["items"] == []
    assert movie.read_bytes() == b"original-video"
    with pytest.raises(FileNotFoundError, match="media_reference_not_found"):
        media_core.resolve_media_reference(resource_id, db_path=reference_db)


def test_delete_root_rejects_concurrent_folder_mutation(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    media_dir = tmp_path / "library"
    media_dir.mkdir()
    repo = MediaCenterRepository()
    root = repo.add_root(str(media_dir))["root"]

    with main._root_mutation_lease(repo):
        result = main.delete_root(root_id=root["id"])

    assert result["ok"] is False
    assert result["error"] == "media_root_operation_busy"
    assert result["retryable"] is True
    assert main.list_roots()["items"][0]["id"] == root["id"]


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


def test_coordinator_applies_agent_deltas_and_searches_folder_segments(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    page = _agent_page(
        _agent_delta(1, "Author Name/Important Book/001.mp3"),
        _agent_delta(2, "Artist/Album/02.mp3"),
    )

    applied = catalog.apply_agent_page(page)
    by_folder = catalog.list_items(query="Important Book", media_kind="audio", limit=30)
    by_filename = catalog.list_items(query="001", media_kind="audio", limit=30)
    replay = catalog.apply_agent_page(page)

    assert applied["applied_count"] == 2
    assert [item["name"] for item in by_folder["items"]] == ["001.mp3"]
    assert [item["name"] for item in by_filename["items"]] == ["001.mp3"]
    assert by_folder["ranking"] == {"version": "deterministic-fts-v1", "query_mode": "explicit_submit"}
    assert by_folder["partial"] is False
    assert replay["applied_count"] == 0
    assert replay["ignored_count"] == 2


def test_coordinator_builds_typed_collections_and_bounded_cursor_pages(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    deltas = [
        _agent_delta(index, f"Series Name/Season 01/Series.Name.S01E{index:02d}.mp4", kind="video")
        for index in range(1, 36)
    ]
    catalog.apply_agent_page(_agent_page(*deltas))

    first = catalog.list_items(media_kind="video", sort="title", limit=100)
    second = catalog.list_items(media_kind="video", sort="title", limit=30, cursor=first["pagination"]["next_cursor"])
    collections = catalog.collections(kind="series")

    assert first["count"] == 30
    assert first["pagination"]["has_more"] is True
    assert second["count"] == 5
    assert {item["id"] for item in first["items"]}.isdisjoint({item["id"] for item in second["items"]})
    assert collections["items"][0]["title"] == "Series Name"
    assert collections["items"][0]["item_count"] == 35
    assert all(item["work_id"] and item["variant_id"] and item["collection_id"] for item in first["items"])


def test_personal_state_is_profile_scoped_and_revisioned(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/Album/01.mp3")))
    item_id = catalog.list_items(media_kind="audio")["items"][0]["id"]

    alice = catalog.set_favorite(item_id, profile_id="alice", favorite=True)
    catalog.checkpoint(item_id, profile_id="alice", position_ms=45_000, duration_ms=180_000)
    alice_page = catalog.list_items(media_kind="audio", profile_id="alice", favorites_only=True)
    bob_page = catalog.list_items(media_kind="audio", profile_id="bob", favorites_only=True)

    assert alice["revision"] == 1
    assert alice_page["items"][0]["favorite"] is True
    assert alice_page["items"][0]["personal"]["resume_ms"] == 45_000
    assert bob_page["items"] == []


def test_profiles_enforce_query_playback_and_shared_surface_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    clean = _agent_delta(1, "Movies/Family.mp4", kind="video")
    restricted = _agent_delta(2, "Movies/Restricted.mp4", kind="video")
    restricted["source"]["metadata"].update(
        {"maturity_rating": 18, "explicit": True}
    )
    catalog.apply_agent_page(_agent_page(clean, restricted))
    items = catalog.list_items(media_kind="video", profile_id="default", sort="title")
    restricted_item = next(item for item in items["items"] if item["title"] == "Restricted")

    kids = catalog.list_items(media_kind="video", profile_id="kids", sort="title")
    denied = catalog.playback_plan(restricted_item["id"], profile_id="kids")
    personal = catalog.set_personal_state(
        restricted_item["id"], profile_id="default", rating=4, hidden=True
    )
    hidden = catalog.list_items(media_kind="video", profile_id="default")
    household_home = catalog.home(
        profile_id="household", limit=3, shared_surface=True
    )
    profile = catalog.get_profile("alice")["profile"]
    conflict = catalog.set_profile_policy(
        "alice", expected_revision=9, values={"maximum_maturity_rating": 16}
    )
    updated = catalog.set_profile_policy(
        "alice", expected_revision=1, values={"maximum_maturity_rating": 16}
    )

    assert [item["title"] for item in kids["items"]] == ["Family"]
    assert denied == {
        "ok": False,
        "error": "playback_policy_denied",
        "reason": "maturity_rating_exceeded",
        "item_id": restricted_item["id"],
        "profile_id": "kids",
        "profile_revision": 1,
    }
    assert personal["state"]["rating"] == 4
    assert personal["state"]["hidden"] is True
    assert restricted_item["id"] not in {item["id"] for item in hidden["items"]}
    assert {shelf["id"] for shelf in household_home["shelves"]}.isdisjoint(
        {"continue", "recent"}
    )
    assert profile["kind"] == "personal"
    assert conflict["error"] == "media_profile_revision_conflict"
    assert updated["profile"]["policy"]["maximum_maturity_rating"] == 16
    _validate_schema("profile.v1.schema.json", updated["profile"])


def test_recommendations_are_bounded_explainable_and_support_opt_out(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Music/Artist/Album/01 Favorite.mp3"),
            _agent_delta(2, "Music/Artist/Album/02 Suggested.mp3"),
            _agent_delta(3, "Books/Other/01 Unrelated.mp3"),
        )
    )
    items = catalog.list_items(media_kind="audio", sort="title")["items"]
    favorite = next(item for item in items if item["title"].endswith("Favorite"))
    catalog.set_favorite(favorite["id"], profile_id="alice", favorite=True)

    recommended = catalog.recommendations(profile_id="alice", limit=2)
    profile = catalog.get_profile("alice")["profile"]
    catalog.set_profile_policy(
        "alice",
        expected_revision=profile["revision"],
        values={"recommendations_enabled": False},
    )
    disabled = catalog.recommendations(profile_id="alice", limit=2)

    assert recommended["count"] == 2
    assert recommended["items"][0]["title"].endswith("Suggested")
    assert recommended["items"][0]["recommendation"]["reasons"] == [
        "preferred_media_kind:audio",
        "related_library_section:Music",
    ]
    assert recommended["privacy"]["external_provider"] is False
    assert disabled["enabled"] is False
    assert disabled["items"] == []


def test_voice_request_uses_catalog_policy_and_existing_control_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/Track.mp3")))
    monkeypatch.setattr(main, "_coordinator", lambda repository=None: catalog)
    calls = []

    def fake_invoke(skill_name, operation, arguments=None, *, timeout=15.0):
        calls.append((skill_name, operation, arguments))
        if operation == "list_targets":
            return {
                "ok": True,
                "items": [
                    {
                        "id": "target-tv",
                        "endpoint_id": "tv",
                        "label": "Living room",
                        "node_id": "node-a",
                        "kind": "tv",
                        "status": "available",
                        "capabilities": {"codecs": ["mp3"], "room_id": "living-room"},
                    }
                ],
            }, ""
        if operation == "create_session":
            return {"ok": True, "session": {"id": "session-1"}}, ""
        raise AssertionError(operation)

    monkeypatch.setattr(main, "_invoke_skill", fake_invoke)

    search = main.voice_request(intent="search", query="Track", profile_id="alice")
    played = main.voice_request(
        intent="play",
        query="Track",
        profile_id="alice",
        room_id="living-room",
    )

    assert search["ok"] is True
    assert search["visual_results"][0]["title"] == "Track"
    assert played["session"]["id"] == "session-1"
    assert played["resolved_target"]["id"] == "target-tv"
    assert [call[1] for call in calls] == ["list_targets", "create_session"]
    assert calls[-1][2]["actor_ref"] == "profile:alice"
    assert len(calls[-1][2]["queue"]) == 1


def test_compound_voice_request_is_bounded_and_requires_governed_approval():
    result = main.voice_request(
        text=(
            "play the next episode in the living room and "
            "lower volume after 10 PM"
        ),
        profile_id="alice",
        actor_ref="profile:alice",
    )

    assert result["ok"] is True
    assert result["status"] == "approval_required"
    workflow = result["workflow"]
    assert workflow["workflow_type"] == "media.compound_control"
    assert workflow["authority"] == "adaos.sdk.workflow"
    assert workflow["automatic_execution"] is False
    assert workflow["requires_confirmation"] is True
    assert workflow["step_count"] == 2
    assert workflow["steps"][0]["action"] == "resolve_and_play"
    assert workflow["steps"][1]["action"] == "volume"
    assert workflow["steps"][1]["schedule"] == {
        "kind": "local_time_not_before",
        "hour": 22,
        "minute": 0,
        "timezone_source": "target",
    }
    assert workflow["target_selector"]["room_id"] == "living room"


def test_unavailable_agent_makes_catalog_truthfully_partial(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/track.mp3")))
    catalog.mark_agent_unavailable("agent-node-a", node_id="node-a", reason="lease_expired")

    page = catalog.list_items(media_kind="audio")

    assert page["items"]
    assert page["partial"] is True
    assert page["participation"]["fresh"] is False
    assert page["participation"]["unavailable_agent_ids"] == ["agent-node-a"]


def test_coordinator_public_contracts_validate_strictly(monkeypatch, tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Artist/Album/01.mp3")))
    source = catalog.list_items(media_kind="audio")["items"][0]
    collection = catalog.collections()["items"][0]

    for filename, payload in (
        ("media-source.v1.schema.json", source),
        ("media-collection.v1.schema.json", collection),
    ):
        schema = __import__("json").loads((SKILL_ROOT / "schemas" / filename).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(payload)


def test_twenty_thousand_item_catalog_remains_server_paged(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()
    rows = []
    for index in range(20_000):
        name = f"movie-{index:05d}.mp4"
        rows.append(
            (
                f"mc-{index:05d}", "media_server", name, name, f"Movie {index:05d}", "video", "video/mp4", 1000 + index,
                "2026-08-19T00:00:00+00:00", f"/api/node/media/files/content/{name}", f"/media/files/content/{name}", "",
                f"/mnt/library/Movies/{name}", "{}", "{}", f"fp-{index}", "2026-08-19T00:00:00+00:00",
                "2026-08-19T00:00:00+00:00", 0, 0, 0, "[]",
            )
        )
    with repo.connect() as connection:
        connection.executemany(
            """
            INSERT INTO catalog_items(
                id,source,resource_id,name,title,media_kind,mime_type,size_bytes,modified_at,
                content_path,routed_content_path,playback_id,source_path,descriptor_json,metadata_json,
                fingerprint,indexed_at,last_seen_at,missing,favorite,play_count,tags_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        connection.commit()
    started = time.monotonic()
    catalog = MediaCatalogCoordinator(repo)

    first = catalog.list_items(media_kind="video", sort="title", limit=500)
    found = catalog.list_items(query="movie 19999", media_kind="video", limit=30)
    broad_first = catalog.list_items(query="movie", media_kind="video", limit=30)
    broad_repeat = catalog.list_items(query="movie", media_kind="video", limit=30)
    broad_second = catalog.list_items(
        query="movie",
        media_kind="video",
        limit=30,
        cursor=broad_first["pagination"]["next_cursor"],
    )

    assert first["count"] == 30
    assert first["total_count"] == 31
    assert first["total_count_exact"] is False
    assert first["total_count_lower_bound"] == 31
    assert first["pagination"]["has_more"] is True
    assert found["count"] == 1
    assert found["items"][0]["name"] == "movie-19999.mp4"
    assert [item["id"] for item in broad_first["items"]] == [
        item["id"] for item in broad_repeat["items"]
    ]
    assert not (
        {item["id"] for item in broad_first["items"]}
        & {item["id"] for item in broad_second["items"]}
    )
    assert broad_second["pagination"]["has_more"] is True
    assert time.monotonic() - started < 30


def test_media_topology_uses_public_sdk_and_builds_safe_default_placement(monkeypatch):
    from media_center import topology as topology_module

    source = (SKILL_ROOT / "media_center" / "topology.py").read_text(encoding="utf-8")
    assert "from adaos.sdk import deployment" in source
    assert "from adaos.sdk import distributed" in source
    assert "adaos.services" not in source

    captured = {}

    def fake_define(desired, *, expected_revision, reason):
        captured["desired"] = desired
        captured["expected_revision"] = expected_revision
        captured["reason"] = reason
        return desired

    monkeypatch.setattr(topology_module.deployment_sdk, "define", fake_define)
    monkeypatch.setattr(
        topology_module.deployment_sdk,
        "plan",
        lambda deployment_id: SimpleNamespace(
            status="ready",
            to_dict=lambda: {"deployment_id": deployment_id, "status": "ready", "digest": "plan-1"},
        ),
    )

    result = MediaCenterTopology().configure_deployment(
        release_digest=f"sha256:{'a' * 64}",
        subnet_id="subnet-home",
    )

    placements = {item.component_ref: item for item in captured["desired"].placements}
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert placements["skill:media_center_skill"].mode == "singleton"
    assert placements["skill:media_library_agent"].mode == "co_located_with"
    assert placements["skill:media_library_agent"].co_located_with == "skill:media_center_skill"
    assert captured["expected_revision"] == 0


def test_distributed_agent_sync_tracks_independent_cursors_and_partial_state(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )

    class FakeTopology:
        active = ["instance-a", "instance-b"]

        def agent_instances(self, *, limit=100):
            return [
                {
                    "instance_id": instance_id,
                    "node_id": instance_id.replace("instance", "node"),
                }
                for instance_id in self.active[:limit]
            ]

        def invoke_agent(
            self,
            instance_id,
            operation,
            arguments,
            *,
            timeout_seconds,
        ):
            assert operation == "pull_deltas"
            suffix = "a" if instance_id.endswith("a") else "b"
            page = _agent_page(
                _agent_delta(
                    1,
                    f"Library {suffix.upper()}/track-{suffix}.mp3",
                )
            )
            page["agent"] = {"id": f"agent-{suffix}", "node_id": f"node-{suffix}"}
            page["next_cursor"] = f"cursor-{suffix}"
            for delta in page["items"]:
                delta["agent_id"] = f"agent-{suffix}"
                delta["node_id"] = f"node-{suffix}"
            return page

    topology = FakeTopology()
    monkeypatch.setattr(main, "_topology", lambda: topology)
    catalog = MediaCatalogCoordinator(MediaCenterRepository())

    first = main._sync_agents(catalog, max_pages=2, limit=100)
    topology.active = ["instance-a"]
    second = main._sync_agents(catalog, max_pages=1, limit=100)

    assert first["ok"] is True
    assert first["mode"] == "distributed"
    assert first["agent_count"] == 2
    assert catalog.agent_binding("instance-a")["cursor"] == "cursor-a"
    assert catalog.agent_binding("instance-b")["cursor"] == "cursor-b"
    assert second["participation"]["partial"] is True
    assert second["participation"]["unavailable_agent_ids"] == ["agent-b"]


def test_local_agent_sync_resumes_from_its_durable_cursor(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )

    class LocalTopology:
        def agent_instances(self, *, limit=100):
            return []

    observed_cursors = []

    def invoke_agent(operation, arguments, *, timeout):
        if operation == "status":
            return (
                {
                    "ok": True,
                    "agent": {"id": "agent-local", "node_id": "node-local"},
                },
                "",
            )
        assert operation == "pull_deltas"
        cursor = str(arguments.get("cursor") or "")
        observed_cursors.append(cursor)
        sequence = 2 if cursor else 1
        page = _agent_page(
            _agent_delta(sequence, f"Music/track-{sequence}.mp3")
        )
        page["agent"] = {"id": "agent-local", "node_id": "node-local"}
        page["next_cursor"] = f"cursor-{sequence}"
        page["has_more"] = sequence == 1
        return page, ""

    monkeypatch.setattr(main, "_topology", lambda: LocalTopology())
    monkeypatch.setattr(main, "_invoke_agent", invoke_agent)
    catalog = MediaCatalogCoordinator(MediaCenterRepository())

    first = main._sync_agents(catalog, max_pages=1, limit=1)
    second = main._sync_agents(catalog, max_pages=1, limit=1)

    assert first["has_more"] is True
    assert second["has_more"] is False
    assert observed_cursors == ["", "cursor-1"]
    assert catalog.agent_cursor("agent-local") == "cursor-2"
    assert catalog.diagnostics()["counts"]["sources"] == 2


def test_personal_mutation_publishes_subscription_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/track.mp3")))
    item_id = catalog.list_items(media_kind="audio")["items"][0]["id"]
    published = []

    import adaos.sdk.io as sdk_io

    monkeypatch.setattr(
        sdk_io,
        "stream_variable_publish",
        lambda receiver, value, **kwargs: published.append(
            (receiver, value, kwargs)
        ),
    )
    monkeypatch.setattr(main, "_coordinator", lambda repository=None: catalog)

    result = main.set_favorite(
        item_id=item_id,
        profile_id="alice",
        favorite=True,
        webspace_id="desktop",
    )

    assert result["ok"] is True
    assert published[-1][0] == "media_center.library_state"
    assert published[-1][1]["profile_id"] == "alice"
    assert published[-1][1]["personal_revision"] == 1
    assert published[-1][2]["_meta"] == {"webspace_id": "desktop"}


def test_hierarchical_collections_and_folder_browse_are_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Shows/Example/Season 2/Example.S02E03.mp4", kind="video"),
            _agent_delta(2, "Music/Album/Disc 2/04 Track.mp3"),
            _agent_delta(3, "Books/Novel/Part 1/001.mp3"),
        )
    )

    kinds = {item["kind"] for item in catalog.collections(limit=30)["items"]}
    root = catalog.folders(limit=1)
    second = catalog.folders(limit=1, cursor=root["pagination"]["next_cursor"])
    nested = catalog.folders(parent="Shows/Example", limit=30)

    assert {"series", "season", "album", "disc", "audiobook", "book_part"} <= kinds
    assert root["count"] == 1
    assert root["pagination"]["has_more"] is True
    assert second["items"][0]["path"] != root["items"][0]["path"]
    assert nested["items"][0]["path"] == "Shows/Example/Season 2"
    assert nested["items"][0]["queue_ref"] == "agent-node-a:Shows/Example/Season 2"
    assert nested["breadcrumbs"][-1] == {"name": "Example", "path": "Shows/Example"}
    _validate_schema("folder-node.v1.schema.json", nested["items"][0])


def test_home_exposes_bounded_flattened_shelf_items(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Shows/Example/Season 01/Example.S01E01.mp4", kind="video"),
            _agent_delta(2, "Music/Album/01.mp3"),
        )
    )

    home = catalog.home(profile_id="alice", limit=2)

    assert home["items"]
    assert len(home["items"]) <= len(home["shelves"]) * 2
    assert all(item["shelf_id"] and item["shelf_title"] for item in home["items"])


def test_playlists_are_profile_scoped_ordered_and_revision_safe(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Music/Album/01.mp3"),
            _agent_delta(2, "Music/Album/02.mp3"),
        )
    )
    items = catalog.list_items(media_kind="audio", sort="title")["items"]

    created = catalog.create_playlist(
        profile_id="alice",
        title="Drive",
        visibility="private",
        item_ids=[items[1]["id"], items[0]["id"]],
    )
    playlist_id = created["playlist"]["id"]
    page = catalog.playlist_items(playlist_id, profile_id="alice", limit=30)
    denied = catalog.get_playlist(playlist_id, profile_id="bob")
    conflict = catalog.update_playlist(
        playlist_id,
        profile_id="alice",
        expected_revision=99,
        title="Wrong",
    )
    updated = catalog.update_playlist(
        playlist_id,
        profile_id="alice",
        expected_revision=1,
        visibility="household",
    )

    assert [item["id"] for item in page["items"]] == [
        items[1]["id"],
        items[0]["id"],
    ]
    assert denied["error"] == "playlist_not_found"
    assert conflict["error"] == "playlist_revision_conflict"
    assert updated["playlist"]["revision"] == 2
    assert catalog.get_playlist(playlist_id, profile_id="bob")["ok"] is True
    _validate_schema("playlist.v1.schema.json", updated["playlist"])


def test_playback_plan_selects_endpoint_compatible_variant_and_route(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    high = _agent_delta(1, "Movies/UHD/Example.mp4", kind="video")
    high["source"]["metadata"]["technical"] = {
        "height": 2160,
        "bitrate": 24_000_000,
        "codec": "hevc",
    }
    high["source"]["descriptor"]["direct_urls"] = [
        "http://node-a.local/media/ref-1"
    ]
    catalog.apply_agent_page(_agent_page(high), instance_id="instance-a")

    compatible = _agent_delta(2, "Movies/FHD/Example.mp4", kind="video")
    compatible["node_id"] = compatible["source"]["node_id"] = "node-b"
    compatible["agent_id"] = "agent-node-b"
    compatible["source"]["metadata"]["technical"] = {
        "height": 1080,
        "bitrate": 8_000_000,
        "codec": "h264",
    }
    compatible["source"]["descriptor"]["direct_urls"] = [
        "http://node-b.local/media/ref-2"
    ]
    page = _agent_page(compatible)
    page["agent"] = {"id": "agent-node-b", "node_id": "node-b"}
    catalog.apply_agent_page(page, instance_id="instance-b")

    item = catalog.list_items(media_kind="video", sort="title")["items"][0]
    plan = catalog.playback_plan(
        item["id"],
        endpoint_id="living-room-tv",
        endpoint_node_id="node-b",
        endpoint_capabilities={
            "codecs": ["h264"],
            "max_video_height": 1080,
            "max_bitrate": 10_000_000,
        },
        preferred_quality="fhd",
    )
    with catalog.repository.connect() as connection:
        high_variant_id = connection.execute(
            "SELECT variant_id FROM catalog_items WHERE source_id='source-1'"
        ).fetchone()[0]
    override = catalog.playback_plan(item["id"], variant_id=high_variant_id)
    invalid_override = catalog.playback_plan(
        item["id"], variant_id="variant-does-not-exist"
    )

    assert plan["ok"] is True
    assert plan["source_id"] == "source-2"
    assert plan["route"]["mode"] == "direct_agent_to_endpoint"
    assert plan["route"]["source_node_id"] == "node-b"
    assert plan["route"]["fallback"]["target_node_id"] == "node-b"
    assert "codec_supported" in plan["decision"]["reasons"]
    assert plan["decision"]["candidate_count"] == 2
    assert override["source_id"] == "source-1"
    assert invalid_override["error"] == "playback_source_unavailable"
    _validate_schema("playback-plan.v1.schema.json", plan)
    _validate_schema("playback-route.v1.schema.json", plan["route"])


def test_audio_identity_uses_folder_context_and_migrates_existing_collisions(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    repository = MediaCenterRepository()
    catalog = MediaCatalogCoordinator(repository)
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Audiobooks/Author A/Book A/01/0.mp3"),
            _agent_delta(2, "Audiobooks/Author B/Book B/01/0.mp3"),
        )
    )
    items = catalog.list_items(media_kind="audio", sort="title", limit=30)[
        "items"
    ]
    by_source = {item["source_id"]: item for item in items}
    source_two_variant_id = by_source["source-2"]["variant_id"]

    assert by_source["source-1"]["work_id"] != by_source["source-2"]["work_id"]
    assert by_source["source-1"]["collection_id"] != by_source["source-2"][
        "collection_id"
    ]
    for source_id in ("source-1", "source-2"):
        plan = catalog.playback_plan(by_source[source_id]["id"])
        assert plan["source_id"] == source_id
        assert plan["decision"]["candidate_count"] == 1

    with repository.connect() as connection:
        connection.execute(
            "UPDATE catalog_items SET work_id=?,collection_id=? WHERE source_id='source-2'",
            (
                by_source["source-1"]["work_id"],
                by_source["source-1"]["collection_id"],
            ),
        )
        connection.execute(
            "UPDATE media_variants SET work_id=? WHERE source_id='source-2'",
            (by_source["source-1"]["work_id"],),
        )
        connection.execute(
            "UPDATE coordinator_meta SET value='legacy' "
            "WHERE key='coordinator_schema_revision'"
        )
        connection.execute(
            "DELETE FROM coordinator_meta "
            "WHERE key='audio_context_identity_revision'"
        )
        connection.commit()

    migrated = MediaCatalogCoordinator(repository)
    repaired = migrated.list_items(media_kind="audio", sort="title", limit=30)[
        "items"
    ]
    repaired_by_source = {item["source_id"]: item for item in repaired}

    assert repaired_by_source["source-1"]["work_id"] != repaired_by_source[
        "source-2"
    ]["work_id"]
    assert repaired_by_source["source-1"]["collection_id"] != repaired_by_source[
        "source-2"
    ]["collection_id"]
    assert migrated.playback_plan(repaired_by_source["source-2"]["id"])[
        "source_id"
    ] == "source-2"
    update = migrated.apply_agent_page(
        _agent_page(
            _agent_delta(
                2,
                "Audiobooks/Author B/Book B/01/0.mp3",
                revision=2,
            )
        )
    )
    updated_source_two = next(
        item
        for item in migrated.list_items(
            media_kind="audio", sort="title", limit=30
        )["items"]
        if item["source_id"] == "source-2"
    )
    assert update["applied_count"] == 1
    assert updated_source_two["variant_id"] == source_two_variant_id


def test_replicated_audio_path_remains_one_work_with_multiple_variants(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Music/Artist/Album/01 Track.mp3")),
        instance_id="instance-a",
    )
    replica = _agent_delta(2, "Music/Artist/Album/01 Track.mp3")
    replica["agent_id"] = "agent-node-b"
    replica["node_id"] = replica["source"]["node_id"] = "node-b"
    page = _agent_page(replica)
    page["agent"] = {"id": "agent-node-b", "node_id": "node-b"}
    catalog.apply_agent_page(page, instance_id="instance-b")

    items = catalog.list_items(media_kind="audio", sort="title", limit=30)[
        "items"
    ]
    assert len({item["work_id"] for item in items}) == 1
    assert catalog.playback_plan(items[0]["id"])["decision"]["candidate_count"] == 2


def test_derived_rendition_is_a_hidden_exact_source_variant(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    original = _agent_delta(1, "Movies/Example.mkv", kind="video")
    original["source"]["mime_type"] = "video/x-matroska"
    original["source"]["metadata"]["technical"] = {
        "height": 1080,
        "codec": "hevc",
        "container": "mkv",
    }
    catalog.apply_agent_page(_agent_page(original), instance_id="instance-a")

    updated = _agent_delta(1, "Movies/Example.mkv", kind="video", revision=2)
    updated["source"]["mime_type"] = "video/x-matroska"
    updated["source"]["fingerprint"] = original["source"]["fingerprint"]
    updated["source"]["metadata"]["technical"] = {
        "height": 1080,
        "codec": "hevc",
        "container": "mkv",
    }
    updated["source"]["metadata"]["derived_renditions"] = [
        {
            "id": "rendition-source-1-browser",
            "profile": "browser-mp4-v1",
            "exact_source_id": "source-1",
            "exact_source_revision": 1,
            "exact_source_fingerprint": original["source"]["fingerprint"],
            "mime_type": "video/mp4",
            "descriptor": {
                "resource_id": "derived-ref",
                "mime_type": "video/mp4",
                "direct_urls": ["http://node-a.local/media/derived-ref"],
                "content_path": "/api/node/media/files/content/derived-ref",
                "routed_content_path": "/media/files/content/derived-ref",
                "metadata": {"storage_mode": "derived_copy"},
            },
            "quality": {
                "height": 720,
                "codec": "h264",
                "container": "mp4",
                "derived": True,
            },
            "size_bytes": 500,
            "created_at": "2026-08-20T00:00:00+00:00",
        }
    ]
    catalog.apply_agent_page(_agent_page(updated), instance_id="instance-a")

    listing = catalog.list_items(media_kind="video")
    plan = catalog.playback_plan(
        listing["items"][0]["id"],
        endpoint_capabilities={"codecs": ["h264"], "max_video_height": 720},
    )
    with catalog.repository.connect() as connection:
        variants = connection.execute(
            "SELECT source_id,derived FROM media_variants ORDER BY derived"
        ).fetchall()

    assert listing["total_count"] == 1
    assert [(row["source_id"], row["derived"]) for row in variants] == [
        ("source-1", 0),
        ("rendition-source-1-browser", 1),
    ]
    assert plan["source_id"] == "rendition-source-1-browser"
    assert plan["descriptor"]["metadata"]["storage_mode"] == "derived_copy"
    assert plan["decision"]["derived"] is True
    assert plan["decision"]["exact_source_id"] == "source-1"
    assert plan["decision"]["exact_source_revision"] == 1
    _validate_schema("playback-plan.v1.schema.json", plan)


def test_federated_deep_search_is_bounded_policy_filtered_and_observable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = main._coordinator()
    source = _agent_delta(1, "Архив/Книга/01.mkv", kind="video")
    source["source"]["metadata"]["technical"] = {
        "codec": "hevc",
        "container": "matroska",
    }
    catalog.apply_agent_page(_agent_page(source), instance_id="instance-a")

    class Topology:
        def agent_instances(self, *, limit):
            assert limit == 2
            return [{"instance_id": "instance-a", "node_id": "node-a"}]

        def invoke_agent(self, instance_id, operation, arguments, *, timeout_seconds):
            assert (instance_id, operation) == ("instance-a", "search_sources")
            assert arguments["query"] == "hevc"
            return {
                "ok": True,
                "items": [source["source"] | {"match": {"stage": "agent_technical_fts", "rank": -1.0}}],
                "has_more": False,
                "agent": {"id": "agent-node-a", "node_id": "node-a"},
            }

    monkeypatch.setattr(main, "_topology", lambda: Topology())
    result = main.deep_search(query="hevc", limit=5, max_agents=2)

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["items"][0]["source_id"] == "source-1"
    assert result["items"][0]["materialized"] is True
    assert result["items"][0]["deep_match"]["stage"] == "agent_technical_fts"
    assert result["stages"][-1]["status"] == "completed"
    assert result["partial"] is False


def test_coordinator_queues_rendition_on_exact_source_agent(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = main._coordinator()
    source = _agent_delta(1, "Movies/Legacy.mkv", kind="video")
    source["source"]["metadata"]["technical"] = {
        "codec": "hevc",
        "container": "mkv",
    }
    catalog.apply_agent_page(_agent_page(source), instance_id="instance-a")
    item = catalog.list_items(media_kind="video")["items"][0]
    captured = {}

    class Topology:
        def invoke_agent(self, instance_id, operation, arguments, *, timeout_seconds):
            captured.update(
                instance_id=instance_id,
                operation=operation,
                arguments=arguments,
                timeout_seconds=timeout_seconds,
            )
            return {
                "ok": True,
                "asynchronous": True,
                "job": {"id": "rendition-job-a", "status": "queued"},
            }

    monkeypatch.setattr(main, "_topology", lambda: Topology())
    result = main.ensure_rendition(
        item_id=item["id"],
        endpoint_capabilities={"codecs": ["h264"], "containers": ["mp4"]},
    )

    assert result["status"] == "queued"
    assert result["source_binding"] == {
        "agent_id": "agent-node-a",
        "node_id": "node-a",
        "instance_id": "instance-a",
    }
    assert captured["operation"] == "plan_rendition"
    assert captured["arguments"]["source_id"] == "source-1"


def test_profile_customizes_home_order_view_density_and_target(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    current = catalog.get_profile("default")["profile"]
    updated = catalog.set_profile_policy(
        "default",
        expected_revision=current["revision"],
        values={
            "home_row_order": ["folders", "movies"],
            "default_view": "list",
            "density": "comfortable",
            "default_target_id": "living-room-tv",
        },
    )["profile"]
    home = catalog.home(profile_id="default", limit=1)

    assert updated["policy"]["home_row_order"][:2] == ["folders", "movies"]
    assert updated["policy"]["default_view"] == "list"
    assert updated["policy"]["density"] == "comfortable"
    assert updated["policy"]["default_target_id"] == "living-room-tv"
    assert home["shelves"][0]["id"] == "folders"
    _validate_schema("profile.v1.schema.json", updated)


def test_diagnostic_export_is_bounded_redacted_and_proposes_reviewed_repair(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )

    class Topology:
        def deployment_status(self, deployment_id, *, limit):
            return {
                "deployment_id": deployment_id,
                "token": "private",
                "root_path": "/mnt/private",
            }

        def distributed_status(self, *, limit):
            return {"status": "ready", "direct_urls": ["http://private"]}

    monkeypatch.setattr(main, "_topology", lambda: Topology())
    monkeypatch.setattr(
        main,
        "_invoke_agent",
        lambda *_args, **_kwargs: ({"ok": True, "source_path": "/mnt/media"}, ""),
    )
    monkeypatch.setattr(
        main,
        "_invoke_skill",
        lambda *_args, **_kwargs: ({"ok": True, "password": "hidden"}, ""),
    )

    result = main.diagnostic_export(
        browser_diagnostics={"frame_ms": 12, "credential": "hidden"}
    )

    assert result["ok"] is True
    assert result["components"]["deployment"]["token"] == "[redacted]"
    assert result["components"]["deployment"]["root_path"] == "[redacted]"
    assert result["components"]["library_agent"]["source_path"] == "[redacted]"
    assert result["components"]["browser"]["credential"] == "[redacted]"
    assert result["privacy"]["automatic_repair"] is False
    assert result["repair_recommendations"][0]["review_required"] is True


def test_queue_builder_preserves_large_playlist_order_and_bounds_sources(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    deltas = [
        _agent_delta(
            index,
            f"Shows/Example/Season 01/Example.S01E{index:02d}.mp4",
            kind="video",
        )
        for index in range(1, 36)
    ]
    catalog.apply_agent_page(_agent_page(*deltas))
    items = catalog.list_items(media_kind="video", limit=30)["items"]
    next_page = catalog.list_items(
        media_kind="video",
        limit=30,
        cursor=catalog.list_items(media_kind="video", limit=30)["pagination"][
            "next_cursor"
        ],
    )["items"]
    ordered = [item["id"] for item in items + next_page]
    playlist = catalog.create_playlist(
        profile_id="alice", title="Season", item_ids=ordered
    )["playlist"]
    playlist_queue = catalog.build_queue(
        source_type="playlist",
        source_id=playlist["id"],
        profile_id="alice",
        limit=500,
    )
    collection = catalog.collections(kind="season")["items"][0]
    collection_queue = catalog.build_queue(
        source_type="collection", source_id=collection["id"], limit=500
    )
    folder_queue = catalog.build_queue(
        source_type="folder",
        source_id="agent-node-a:Shows/Example/Season 01",
        limit=5,
    )

    assert playlist_queue["count"] == 35
    assert [item["item_id"] for item in playlist_queue["items"]] == ordered
    assert collection_queue["count"] == 35
    assert folder_queue["count"] == 5
    assert folder_queue["limit"] == 5
    _validate_schema("queue-source.v1.schema.json", playlist_queue)


def test_catalog_corrections_are_audited_reversible_and_non_destructive(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Movies/First.mp4", kind="video"),
            _agent_delta(2, "Movies/Second.mp4", kind="video"),
        )
    )
    works = catalog.list_items(media_kind="video", sort="title")["items"]
    target = works[0]["work_id"]

    correction = catalog.apply_correction(
        operation="metadata",
        subject_ref=f"work:{target}",
        values={"canonical_title": "Corrected title"},
        actor_ref="profile:alice",
    )
    claims = catalog.metadata_claims(f"work:{target}")
    reversed_result = catalog.reverse_correction(
        correction["correction"]["id"], actor_ref="profile:alice"
    )

    assert correction["ok"] is True
    assert correction["source_deletion"] is False
    assert claims["items"][0]["provenance"] == "profile:alice"
    assert reversed_result["ok"] is True
    _validate_schema(
        "catalog-correction.v1.schema.json", correction["correction"]
    )
    with catalog.repository.connect() as connection:
        title = connection.execute(
            "SELECT canonical_title FROM media_works WHERE id=?", (target,)
        ).fetchone()[0]
    assert title != "Corrected title"

    canonical = works[1]["work_id"]
    merged = catalog.apply_correction(
        operation="merge",
        subject_ref=f"work:{target}",
        values={"canonical_work_id": canonical},
        actor_ref="profile:alice",
    )
    split = catalog.apply_correction(
        operation="split",
        subject_ref=f"work:{target}",
        values={},
        actor_ref="profile:alice",
    )
    restored = catalog.reverse_correction(
        split["correction"]["id"], actor_ref="profile:alice"
    )
    with catalog.repository.connect() as connection:
        alias = connection.execute(
            "SELECT alias_of FROM media_works WHERE id=?", (target,)
        ).fetchone()[0]
        active = connection.execute(
            "SELECT active FROM catalog_aliases WHERE canonical_id=?", (canonical,)
        ).fetchone()[0]
    assert merged["ok"] is True
    assert split["correction"]["before"] == {"alias_of": canonical}
    assert split["correction"]["after"] == {"alias_of": ""}
    assert restored["ok"] is True
    assert alias == canonical
    assert active == 1
    _validate_schema("catalog-correction.v1.schema.json", split["correction"])


def test_enrichment_worker_persists_provider_claims_and_terminal_progress(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Author/Book/001.mp3"))
    )
    queued = catalog.operations(limit=10)["items"]
    worker = MediaEnrichmentWorker(catalog, poll_seconds=0.2)

    result = worker.run_once()
    operations = catalog.operations(limit=10)["items"]
    claims = catalog.metadata_claims(queued[0]["subject_ref"], limit=30)

    assert result["status"] == "completed"
    assert operations[0]["status"] == "completed"
    assert operations[0]["provider_id"] == "media_center.deterministic_local.v1"
    assert operations[0]["progress"]["phase"] == "completed"
    assert {item["field_name"] for item in claims["items"]} >= {
        "title",
        "folder_keywords",
    }


def test_enrichment_worker_coalesces_publication_and_exposes_pacing(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Music/one.mp3"),
            _agent_delta(2, "Music/two.mp3"),
        )
    )
    published = []
    worker = MediaEnrichmentWorker(
        catalog,
        publish=lambda: published.append(time.monotonic()),
        work_interval_seconds=0.25,
        publish_interval_seconds=30,
    )

    assert worker.run_once() is not None
    assert worker.run_once() is not None

    assert len(published) == 1
    assert worker.work_interval_seconds == 0.25
    assert worker.publish_interval_seconds == 30


def test_library_stream_snapshot_is_compact(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            *(
                _agent_delta(index, f"Music/Album/{index:03d}.mp3")
                for index in range(1, 21)
            )
        )
    )
    published = []

    import adaos.sdk.io as sdk_io

    monkeypatch.setattr(
        sdk_io,
        "stream_variable_publish",
        lambda receiver, value, **kwargs: published.append(value),
    )

    main._publish_library_snapshot(catalog)

    snapshot = published[-1]
    assert len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) < 65536
    assert snapshot["home"]["items"]
    assert all("resource" not in item for item in snapshot["home"]["items"])
    assert all("descriptor" not in item for item in snapshot["home"]["items"])


def test_local_discovery_is_phonetic_semantic_and_resource_bounded(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "discovery.sqlite3"))
    monkeypatch.setenv("MEDIA_CENTER_DISCOVERY_MAX_CANDIDATES", "100")
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Аудиокниги/Шерлок Холмс/01.mp3"),
            _agent_delta(2, "Музыка/Совсем другое/02.mp3"),
        )
    )
    worker = MediaEnrichmentWorker(catalog, poll_seconds=0.2)
    while worker.run_once() is not None:
        pass

    result = catalog.discovery_search(
        "Sherlok Holms", profile_id="default", limit=5
    )

    assert result["items"][0]["name"] == "01.mp3"
    assert result["items"][0]["deep_match"]["stage"] == (
        "coordinator_local_discovery"
    )
    assert set(result["items"][0]["deep_match"]["reasons"]) & {
        "phonetic_overlap",
        "trigram_similarity",
        "local_text_embedding",
    }
    assert result["candidate_limit"] == 100
    assert result["bounded"] is True


def test_perceptual_duplicate_claims_never_merge_or_delete_sources(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "duplicates.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Movies/A/movie-a.mp4", kind="video"),
            _agent_delta(2, "Movies/B/movie-b.mp4", kind="video"),
        )
    )
    for item_id in (
        catalog.list_items(limit=30, sort="title")["items"][0]["id"],
        catalog.list_items(limit=30, sort="title")["items"][1]["id"],
    ):
        catalog.record_metadata_claim(
            subject_ref=f"item:{item_id}",
            field_name="perceptual_hash_v1",
            value="same-sampled-content",
            provenance="media_library_agent.ffmpeg_sample_sha256_v1",
            confidence=0.95,
        )

    result = catalog.duplicate_candidates(limit=10)

    candidate = next(
        item
        for item in result["items"]
        if item["evidence"] == "perceptual_sample_hash_v1"
    )
    assert candidate["candidate_count"] == 2
    assert candidate["disposition"] == "review_only"
    assert result["automatic_merge"] is False
    assert result["source_deletion"] is False
