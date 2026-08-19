from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from handlers import main  # noqa: E402
from media_center.catalog import MediaCenterRepository, SCHEMA_VERSION  # noqa: E402
from media_center.coordinator import MediaCatalogCoordinator  # noqa: E402
from media_center.topology import MediaCenterTopology  # noqa: E402


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

    migration = repo.ensure_schema()
    available = repo.list_items()["items"]
    all_items = repo.list_items(include_missing=True)["items"]

    assert migration["retired_legacy_count"] == 1
    assert available == []
    assert all_items[0]["missing"] is True


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

    assert first["count"] == 30
    assert first["total_count"] == 20_000
    assert first["pagination"]["has_more"] is True
    assert found["count"] == 1
    assert found["items"][0]["name"] == "movie-19999.mp4"
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
    assert nested["breadcrumbs"][-1] == {"name": "Example", "path": "Shows/Example"}
    _validate_schema("folder-node.v1.schema.json", nested["items"][0])


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
