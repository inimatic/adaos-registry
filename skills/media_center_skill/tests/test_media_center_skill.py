from __future__ import annotations

import contextvars
import json
import sqlite3
import sys
import threading
import time
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import yaml


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from media_center.catalog import MediaCenterRepository, SCHEMA_VERSION  # noqa: E402
from media_center.background import MediaCenterBackgroundRuntime  # noqa: E402
import media_center.catalog as catalog_module  # noqa: E402
import media_center.coordinator as coordinator_module  # noqa: E402
from media_center.coordinator import MediaCatalogCoordinator  # noqa: E402
from media_center.enrichment import (  # noqa: E402
    DeterministicLocalProvider,
    MediaEnrichmentWorker,
    MetadataProviderError,
    MusicBrainzMetadataProvider,
    OpenLibraryMetadataProvider,
    TmdbMetadataProvider,
    default_metadata_providers,
    metadata_provider_configuration,
)
from media_center.sync import MediaAgentSyncWorker  # noqa: E402
from media_center.topology import MediaCenterTopology  # noqa: E402


_HANDLER_SPEC = importlib.util.spec_from_file_location(
    "media_center_skill_handlers_main", SKILL_ROOT / "handlers" / "main.py"
)
assert _HANDLER_SPEC and _HANDLER_SPEC.loader
main = importlib.util.module_from_spec(_HANDLER_SPEC)
_HANDLER_SPEC.loader.exec_module(main)


def test_deployment_operation_status_is_exported_by_the_skill_contract() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))
    exported = set(manifest["exports"]["tools"])
    definitions = {item["name"]: item for item in manifest["tools"]}

    assert "deployment_operation_status" in exported
    assert definitions["deployment_operation_status"]["entry"] == (
        "handlers.main:deployment_operation_status"
    )
    assert definitions["deployment_operation_status"]["side_effects"] == "none"


def test_background_runtime_reuses_and_disposes_process_owned_workers() -> None:
    runtime = MediaCenterBackgroundRuntime()
    disposed: list[tuple[str, float]] = []

    class Worker:
        def __init__(self, name: str):
            self.name = name

        def dispose(self, *, timeout: float = 5.0) -> dict[str, object]:
            disposed.append((self.name, timeout))
            return {"stopped": True, "worker": self.name}

        def status(self) -> dict[str, object]:
            return {"state": "idle", "revision": 2, "worker": self.name}

    first = runtime.agent_sync_worker("catalog-a", lambda: Worker("sync-a"))
    same = runtime.agent_sync_worker("catalog-a", lambda: Worker("unused"))
    second = runtime.agent_sync_worker("catalog-b", lambda: Worker("sync-b"))
    runtime.enrichment_worker("catalog-b", lambda: Worker("enrichment-b"))

    assert first is same
    assert second is not first
    assert disposed == [("sync-a", 0.2)]
    assert runtime.agent_sync_status()["worker"] == "sync-b"

    receipt = runtime.dispose(timeout=1.5)

    assert disposed == [
        ("sync-a", 0.2),
        ("sync-b", 1.5),
        ("enrichment-b", 1.5),
    ]
    assert runtime.agent_sync_status() == {"state": "stopped", "revision": 0}
    assert receipt["stopped"] is True


def test_background_runtime_restarts_running_enrichment_when_configuration_changes() -> (
    None
):
    runtime = MediaCenterBackgroundRuntime()
    events: list[str] = []

    class Worker:
        def __init__(self, name: str, state: str):
            self.name = name
            self.state = state

        def dispose(self, *, timeout: float = 5.0) -> dict[str, object]:
            events.append(f"dispose:{self.name}:{timeout}")
            self.state = "idle"
            return {"stopped": True}

        def ensure_started(self) -> bool:
            events.append(f"start:{self.name}")
            self.state = "running"
            return True

        def status(self) -> dict[str, object]:
            return {"state": self.state}

    first = runtime.enrichment_worker(
        "catalog:revision-1", lambda: Worker("first", "running")
    )
    second = runtime.enrichment_worker(
        "catalog:revision-2", lambda: Worker("second", "idle")
    )

    assert second is not first
    assert second.status()["state"] == "running"
    assert events == ["dispose:first:0.2", "start:second"]
    runtime.dispose(timeout=1.0)


def test_background_runtime_bootstrap_is_async_and_process_owned() -> None:
    runtime = MediaCenterBackgroundRuntime()
    entered = threading.Event()
    release = threading.Event()

    def bootstrap() -> None:
        entered.set()
        release.wait(2.0)

    assert runtime.ensure_bootstrap_started("catalog-a", bootstrap) is True
    assert entered.wait(1.0) is True
    assert runtime.bootstrap_status()["running"] is True
    assert runtime.ensure_bootstrap_started("catalog-a", bootstrap) is False

    release.set()
    receipt = runtime.dispose(timeout=2.0)

    assert receipt["stopped"] is True
    assert receipt["bootstrap"]["stopped"] is True


def test_rehydrate_defers_runtime_workers_until_sys_ready(monkeypatch) -> None:
    started: list[str] = []

    class Worker:
        def __init__(self, name: str):
            self.name = name

        def ensure_started(self) -> bool:
            started.append(self.name)
            return True

        def status(self) -> dict[str, object]:
            return {"state": "idle", "revision": 0}

    class Repository:
        def summary(self) -> dict[str, object]:
            pytest.fail("rehydrate must not scan the catalog summary")

        def facets(self) -> dict[str, object]:
            pytest.fail("rehydrate must not scan catalog facets")

    class Catalog:
        def catalog_revision(self) -> int:
            return 42

    repository = Repository()
    catalog = Catalog()
    monkeypatch.setattr(main, "_repository", lambda: repository)
    monkeypatch.setattr(main, "_coordinator", lambda _repo=None: catalog)
    monkeypatch.setattr(
        main,
        "_run_agent_sync",
        lambda *_args, **_kwargs: pytest.fail("sync must be deferred"),
    )
    monkeypatch.setattr(
        main, "_agent_sync_runtime", lambda _catalog=None: Worker("sync")
    )
    monkeypatch.setattr(
        main, "_enrichment_runtime", lambda _catalog=None: Worker("enrichment")
    )
    monkeypatch.setattr(
        main, "_publish_library_snapshot", lambda *_args, **_kwargs: None
    )

    result = main.rehydrate()

    assert started == []
    assert result["agent_sync"]["deferred"] is True
    assert result["agent_sync"]["mode"] == "background_cursor_catchup"
    assert result["agent_sync"]["activation"] == "sys.ready"
    assert result["agent_sync"]["worker_started"] is False
    assert result["enrichment"]["activation"] == "sys.ready"
    assert result["enrichment"]["worker_started"] is False
    assert result["catalog_revision"] == 42


def test_sys_ready_schedules_catalog_bootstrap_without_running_it_inline(
    monkeypatch, tmp_path
) -> None:
    scheduled: list[tuple[str, object]] = []
    started: list[str] = []

    class Runtime:
        def ensure_bootstrap_started(self, key, callback) -> bool:
            scheduled.append((key, callback))
            return True

    class Worker:
        def __init__(self, name: str):
            self.name = name

        def ensure_started(self) -> bool:
            started.append(self.name)
            return True

    catalog = object()
    monkeypatch.setattr(main, "background_runtime", lambda: Runtime())
    monkeypatch.setattr(main, "default_db_path", lambda: tmp_path / "catalog.sqlite3")
    monkeypatch.setattr(main, "_coordinator", lambda: catalog)
    monkeypatch.setattr(
        main, "_agent_sync_runtime", lambda _catalog=None: Worker("sync")
    )
    monkeypatch.setattr(
        main, "_enrichment_runtime", lambda _catalog=None: Worker("enrichment")
    )
    monkeypatch.setattr(
        main, "_publish_library_snapshot", lambda *_args: started.append("library")
    )
    monkeypatch.setattr(
        main, "_publish_operation_snapshot", lambda *_args: started.append("operations")
    )

    main.on_sys_ready(None)

    assert started == []
    assert len(scheduled) == 1
    scheduled[0][1]()
    assert started == ["library", "operations", "sync", "enrichment"]


def test_catalog_change_during_bootstrap_does_not_open_the_catalog(
    monkeypatch, tmp_path
) -> None:
    scheduled: list[tuple[str, object]] = []

    class Runtime:
        def ensure_bootstrap_started(self, key, callback) -> bool:
            scheduled.append((key, callback))
            return False

    db_path = (tmp_path / "catalog.sqlite3").resolve()
    monkeypatch.setattr(main, "_coordinator_cached", None)
    monkeypatch.setattr(main, "_coordinator_path", "")
    monkeypatch.setattr(main, "default_db_path", lambda: db_path)
    monkeypatch.setattr(main, "background_runtime", lambda: Runtime())
    monkeypatch.setattr(
        main,
        "_coordinator",
        lambda *_args, **_kwargs: pytest.fail(
            "catalog event must not race runtime bootstrap"
        ),
    )

    main.on_agent_catalog_changed(None)

    assert scheduled == [(str(db_path), main._start_live_runtime)]


def test_schema_lock_never_falls_through_to_full_migration(
    monkeypatch, tmp_path
) -> None:
    db_path = tmp_path / "media_center.sqlite3"
    db_path.write_bytes(b"catalog")
    calls = 0

    def locked_connect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise catalog_module.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(catalog_module.sqlite3, "connect", locked_connect)

    with pytest.raises(RuntimeError, match="media_center_schema_state_unavailable"):
        MediaCenterRepository(db_path)

    assert calls == 4


def test_coordinator_schema_lock_never_falls_through_to_migration(
    monkeypatch, tmp_path
) -> None:
    db_path = tmp_path / "media_center.sqlite3"
    db_path.write_bytes(b"catalog")
    repository = object.__new__(MediaCenterRepository)
    repository.db_path = db_path
    migration_attempted = False

    def locked_connect(*_args, **_kwargs):
        raise catalog_module.sqlite3.OperationalError("database is locked")

    def repository_connect():
        nonlocal migration_attempted
        migration_attempted = True
        pytest.fail("schema uncertainty must not start coordinator migration")

    monkeypatch.setattr(catalog_module.sqlite3, "connect", locked_connect)
    repository.connect = repository_connect
    coordinator = object.__new__(MediaCatalogCoordinator)
    coordinator.repository = repository

    with pytest.raises(
        RuntimeError,
        match="media_center_coordinator_schema_state_unavailable",
    ):
        coordinator.ensure_schema()

    assert migration_attempted is False


def test_repository_connect_keeps_default_sync_during_lock(
    monkeypatch, tmp_path
) -> None:
    statements: list[str] = []

    class Connection:
        row_factory = None
        closed = False

        def execute(self, statement, *_args):
            statements.append(statement)
            if statement == "PRAGMA synchronous=NORMAL":
                raise catalog_module.sqlite3.OperationalError("database is locked")
            return self

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(
        catalog_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )
    repository = object.__new__(MediaCenterRepository)
    repository.db_path = tmp_path / "media_center.sqlite3"

    assert repository.connect() is connection
    assert connection.closed is False
    assert statements[-1] == "PRAGMA synchronous=NORMAL"


def test_coordinator_cache_fast_path_does_not_repeat_schema_check(
    monkeypatch, tmp_path
) -> None:
    db_path = (tmp_path / "media_center.sqlite3").resolve()
    cached = object()
    monkeypatch.setattr(main, "_coordinator_cached", cached)
    monkeypatch.setattr(main, "_coordinator_path", str(db_path))
    monkeypatch.setattr(main, "default_db_path", lambda: db_path)
    monkeypatch.setattr(
        main,
        "MediaCenterRepository",
        lambda *_args, **_kwargs: pytest.fail("cached coordinator must be reused"),
    )

    assert main._coordinator() is cached


def _resource(resource_id: str = "clip.mp4", *, source: str = "media_server") -> dict:
    suffix = Path(resource_id).suffix.lower()
    mime = (
        "audio/mpeg"
        if suffix == ".mp3"
        else "image/jpeg"
        if suffix in {".jpg", ".jpeg"}
        else "video/mp4"
    )
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
                "metadata": {
                    "storage_mode": "reference",
                    "folder_segments": list(Path(folder).parts),
                },
            },
            "metadata": {
                "storage_mode": "reference",
                "folder_segments": list(Path(folder).parts),
                "media_library_root_path": "/mnt/library",
            },
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


def test_build_queue_keeps_legacy_reference_rows_playable(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repository = MediaCenterRepository()
    repository.scan_resources([_resource("legacy-reference.mp4")])
    catalog = MediaCatalogCoordinator(repository)
    item = catalog.list_items(media_kind="video")["items"][0]

    queue = catalog.build_queue(
        source_type="item",
        source_id=item["id"],
        limit=10,
    )

    assert queue["count"] == 1
    assert queue["items"][0]["compatibility_mode"] == "legacy_catalog_row"
    assert queue["items"][0]["content_path"] == (
        "/api/node/media/files/content/legacy-reference.mp4"
    )
    assert queue["items"][0]["routed_content_path"] == (
        "/media/files/content/legacy-reference.mp4"
    )
    assert queue["items"][0]["size_bytes"] == 1024
    assert queue["items"][0]["modified_at"] == "2026-08-11T10:00:00+00:00"
    assert queue["items"][0]["route"]["fallback"]["reason"] == (
        "legacy_reference_source"
    )


def test_playback_queue_includes_effective_control_settings(monkeypatch):
    queue = {
        "ok": True,
        "playback_control": {"schema": "adaos.playback.endpoint_control.v1"},
        "items": [{"id": "movie-1"}],
    }
    monkeypatch.setattr(
        main,
        "_coordinator",
        lambda: SimpleNamespace(build_queue=lambda **_kwargs: dict(queue)),
    )
    monkeypatch.setattr(
        main,
        "_invoke_skill",
        lambda *args, **kwargs: (
            {"ok": True, "settings": {"autoplay": False, "auto_fullscreen": True}},
            "",
        ),
    )

    result = main.build_playback_queue(
        source_type="item",
        source_id="movie-1",
        profile_id="alice",
        limit=10,
    )

    assert result["playback_control"]["settings"] == {
        "autoplay": False,
        "auto_fullscreen": True,
    }


def test_play_on_creates_a_durable_remote_session_and_sends_play(monkeypatch):
    monkeypatch.setattr(
        main,
        "build_playback_queue",
        lambda **_kwargs: {
            "ok": True,
            "items": [
                {"id": "episode-1", "title": "Episode 1", "route": {}},
                {"id": "episode-2", "title": "Episode 2", "route": {}},
            ],
            "initial_index": 1,
            "playback_control": {
                "queue_source": {"type": "collection", "id": "series-1"}
            },
        },
    )
    calls = []

    def invoke(skill, method, params, **_kwargs):
        calls.append((skill, method, params))
        if method == "now_playing":
            return {"ok": True, "items": []}, ""
        if method == "create_session":
            return {
                "ok": True,
                "session": {"id": "session-tv", "revision": 1},
            }, ""
        if method == "command":
            return {
                "ok": True,
                "session": {"id": "session-tv", "revision": 2},
                "command": {"command": "play"},
            }, ""
        raise AssertionError(method)

    monkeypatch.setattr(main, "_invoke_skill", invoke)

    result = main.play_on(
        target_id="target-tv",
        source_type="collection",
        source_id="series-1",
        start_item_id="episode-2",
        profile_id="alice",
    )

    assert result["ok"] is True
    assert result["queue_count"] == 2
    assert [method for _skill, method, _params in calls] == [
        "now_playing",
        "create_session",
        "command",
    ]
    assert calls[1][2]["active_index"] == 1
    assert calls[1][2]["queue_source"] == {
        "type": "collection",
        "id": "series-1",
    }
    assert calls[2][2]["session_id"] == "session-tv"


def test_play_on_requires_a_target_without_masking_the_skill_error():
    result = main.play_on(target_id="", source_id="movie-1")

    assert result == {
        "ok": False,
        "schema": main.SCHEMA_VERSION,
        "error": "playback_target_required",
        "message": "Choose an online playback device.",
    }


def test_play_on_selects_requested_item_in_existing_remote_session(monkeypatch):
    monkeypatch.setattr(
        main,
        "build_playback_queue",
        lambda **_kwargs: {
            "ok": True,
            "items": [
                {"id": "episode-1", "title": "Episode 1", "route": {}},
                {"id": "episode-2", "title": "Episode 2", "route": {}},
            ],
            "initial_index": 1,
            "playback_control": {
                "queue_source": {"type": "collection", "id": "series-1"}
            },
        },
    )
    calls = []

    def invoke(skill, method, params, **_kwargs):
        calls.append((skill, method, params))
        if method == "now_playing":
            return {
                "ok": True,
                "items": [
                    {
                        "id": "session-tv",
                        "target_id": "target-tv",
                        "revision": 4,
                        "queue_revision": 2,
                    }
                ],
            }, ""
        if method == "update_queue":
            return {
                "ok": True,
                "session": {"id": "session-tv", "revision": 5},
            }, ""
        if method == "command":
            return {
                "ok": True,
                "session": {"id": "session-tv", "revision": 6},
                "command": {"command": "play"},
            }, ""
        raise AssertionError(method)

    monkeypatch.setattr(main, "_invoke_skill", invoke)

    result = main.play_on(
        target_id="target-tv",
        source_type="collection",
        source_id="series-1",
        start_item_id="episode-2",
        profile_id="alice",
    )

    assert result["ok"] is True
    assert [method for _skill, method, _params in calls] == [
        "now_playing",
        "update_queue",
        "command",
    ]
    assert calls[1][2]["active_index"] == 1
    assert calls[2][2]["expected_revision"] == 5


def test_catalog_page_queue_preserves_current_query_sort_and_start_item(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            *(
                _agent_delta(index, f"Music/Album/Track {index:02d}.mp3")
                for index in range(1, 36)
            )
        )
    )
    visible = catalog.list_items(
        query="Track", media_kind="audio", sort="title", limit=30
    )["items"]
    start_item_id = visible[12]["id"]

    queue = catalog.build_queue(
        source_type="catalog",
        source_id="current-page",
        source_context={
            "query": "Track",
            "media_kind": "audio",
            "sort": "title",
            "sort_direction": "asc",
        },
        start_item_id=start_item_id,
        limit=500,
    )

    assert queue["ok"] is True
    assert queue["count"] == 30
    assert [item["item_id"] for item in queue["items"]] == [
        item["id"] for item in visible
    ]
    assert queue["initial_item_id"] == start_item_id
    assert queue["initial_index"] == 12
    assert queue["source"] == {
        "type": "catalog",
        "id": "current-page",
        "ownership": "catalog_page_snapshot",
        "context": {
            "query": "Track",
            "media_kind": "audio",
            "sort": "title",
            "sort_direction": "asc",
        },
    }


def test_library_auto_scan_uses_sdk_discovery_boundary(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    monkeypatch.setattr(
        main,
        "_discover_resources",
        lambda source="all", limit=5000: ([_resource("song.mp3")], {"ok": True}),
    )
    monkeypatch.setattr(
        main,
        "_sync_agents",
        lambda *_args, **_kwargs: {"ok": False, "error": "agent_unavailable"},
    )
    monkeypatch.setattr(
        main,
        "_agent_sync_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(ensure_started=lambda: True),
    )

    payload = main.library(auto_scan=True, limit=20)

    assert payload["ok"] is True
    assert payload["scan"]["discovered_count"] == 1
    assert (
        payload["runtime"]["resource_boundary"]
        == "adaos.sdk.io.media.list_media_resources"
    )
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
    assert any(
        item["resource_id"] == "old.mp4" and item["missing"] for item in all_items
    )


def test_playable_filter_excludes_images_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()

    repo.scan_resources(
        [_resource("clip.mp4"), _resource("song.mp3"), _resource("poster.jpg")]
    )
    playable = repo.list_items(media_kind="playable", sort="title", limit=20)["items"]

    assert [item["media_kind"] for item in playable] == ["video", "audio"]
    assert {item["resource_id"] for item in playable} == {"clip.mp4", "song.mp3"}


def test_kind_and_favorites_filters_are_exact(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()
    repo.scan_resources(
        [_resource("clip.mp4"), _resource("song.mp3"), _resource("poster.jpg")]
    )
    clip = next(
        item
        for item in repo.list_items(limit=20)["items"]
        if item["resource_id"] == "clip.mp4"
    )
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

    repo.scan_resources(
        [_resource("clip.mp4"), _resource("song.mp3"), _resource("poster.jpg")]
    )
    payload = main.library(auto_scan=False, sort="title", limit=20)

    assert [item["media_kind"] for item in payload["items"]] == ["video", "audio"]
    assert {item["resource_id"] for item in payload["items"]} == {
        "clip.mp4",
        "song.mp3",
    }


def test_library_summary_projection_is_bounded_to_card_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()
    repo.scan_resources([_resource("clip.mp4")])

    payload = main.library(
        auto_scan=False,
        projection="summary",
        sort="title",
        limit=20,
    )

    assert payload["projection"] == "summary"
    assert payload["items"][0]["resource_id"] == "clip.mp4"
    assert "resource" not in payload["items"][0]
    assert "metadata" not in payload["items"][0]


def test_incremental_root_scan_does_not_mark_existing_media_server_rows_missing(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()

    repo.scan_resources([_resource("old.mp4")], source="media_server")
    repo.scan_resources(
        [_resource("new.mp4")], source="media_server", mark_missing=False
    )
    available = repo.list_items(sort="title")["items"]

    assert {item["resource_id"] for item in available} == {"old.mp4", "new.mp4"}


def test_discovery_excludes_legacy_media_center_copies(monkeypatch):
    from adaos.sdk.io import media as media_sdk

    legacy = _resource("media-center-0123456789abcdef01234567-import.mp4")
    current = _resource("movie.mp4")
    current["metadata"] = {"storage_mode": "reference"}
    monkeypatch.setattr(
        media_sdk, "list_media_resources", lambda **_: [legacy, current]
    )

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
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
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
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())

    with catalog.repository.connect() as connection:
        catalog_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(catalog_items)")
        }
        variant_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(media_variants)")
        }
        membership_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(collection_memberships)")
        }

    assert "idx_media_center_catalog_variant" in catalog_indexes
    assert "idx_media_center_root_visibility" in catalog_indexes
    assert "idx_media_center_variant_work" in variant_indexes
    assert "idx_media_center_membership_variant" in membership_indexes


def test_base_variant_reuses_catalog_descriptor_without_losing_playback(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Movies/Example.mp4", kind="video"))
    )
    item = catalog.list_items(media_kind="video", limit=1)["items"][0]

    with catalog.repository.connect() as connection:
        descriptors = connection.execute(
            "SELECT descriptor_json FROM media_variants WHERE derived=0"
        ).fetchall()
    plan = catalog.playback_plan(item["id"])

    assert [str(row["descriptor_json"]) for row in descriptors] == ["{}"]
    assert plan["ok"] is True
    assert plan["descriptor"]["resource_id"]
    assert plan["route"]["routed_path"] or plan["route"]["node_path"]


def test_coordinator_stores_source_routing_and_metadata_only_once(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "compact-catalog.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    delta = _agent_delta(1, "Movies/Example.mp4", kind="video")
    delta["source"]["metadata"].update({"title": "Example", "year": 2024})
    catalog.apply_agent_page(_agent_page(delta))
    item = catalog.list_items(media_kind="video", limit=1)["items"][0]
    catalog.record_metadata_claim(
        subject_ref=f"item:{item['id']}",
        field_name="text_embedding_v1",
        value=[0.1] * 48,
        provenance="media_center.deterministic_local.v1",
        confidence=1.0,
    )
    catalog.record_metadata_claim(
        subject_ref=f"item:{item['id']}",
        field_name="overview",
        value="External overview",
        provenance="media_center.tmdb.v1",
        confidence=0.9,
    )

    with catalog.repository.connect() as connection:
        row = connection.execute(
            "SELECT descriptor_json,metadata_json FROM catalog_items WHERE id=?",
            (item["id"],),
        ).fetchone()
        projection = connection.execute(
            "SELECT metadata_json,provenance_json "
            "FROM catalog_metadata_projection WHERE item_id=?",
            (item["id"],),
        ).fetchone()
    descriptor = json.loads(row["descriptor_json"])
    metadata = json.loads(row["metadata_json"])
    projected = json.loads(projection["metadata_json"])
    details = catalog.item_details(item["id"])["item"]

    assert "content_path" not in descriptor
    assert "resource_id" not in descriptor
    assert "storage_mode" not in metadata
    assert "folder_segments" not in metadata
    assert projected == {"overview": "External overview"}
    assert "text_embedding_v1" not in projected
    assert details["metadata"]["title"] == "Example"
    assert details["metadata"]["overview"] == "External overview"
    assert details["metadata_provenance"]["title"] == (
        "media_library_agent.source_metadata.v1"
    )
    assert details["metadata_provenance"]["overview"] == "media_center.tmdb.v1"


def test_storage_compaction_is_bounded_resumable_and_preserves_public_metadata(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "legacy-storage.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Movies/Legacy.mp4", kind="video"))
    )
    item = catalog.list_items(media_kind="video", limit=1)["items"][0]
    with catalog.repository.connect() as connection:
        connection.execute(
            "UPDATE catalog_items SET descriptor_json=?,metadata_json=? WHERE id=?",
            (
                json.dumps(
                    {
                        "schema": "adaos.media.resource.v1",
                        "resource_id": "duplicate-resource",
                        "content_path": "/api/node/media/duplicate",
                        "metadata": {"storage_mode": "reference"},
                    }
                ),
                json.dumps(
                    {
                        "title": "Legacy",
                        "folder_path": "Movies",
                        "storage_mode": "reference",
                    }
                ),
                item["id"],
            ),
        )
        connection.execute(
            "UPDATE catalog_metadata_projection "
            "SET metadata_json=?,provenance_json=? WHERE item_id=?",
            (
                json.dumps(
                    {
                        "title": "Legacy",
                        "folder_path": "Movies",
                        "overview": "Preserved",
                    }
                ),
                json.dumps(
                    {
                        "title": "media_library_agent.source_metadata.v1",
                        "folder_path": "media_library_agent.source_metadata.v1",
                        "overview": "media_center.tmdb.v1",
                    }
                ),
                item["id"],
            ),
        )
        connection.execute(
            "DELETE FROM coordinator_meta WHERE key='storage_compaction_state'"
        )
        connection.commit()

    batches = [catalog.compact_storage_batch(limit=10)]
    while not batches[-1]["complete"]:
        batches.append(catalog.compact_storage_batch(limit=10))
    details = catalog.item_details(item["id"])["item"]
    storage = catalog.storage_status()

    assert batches[-1]["complete"] is True
    assert all(batch["batch_scanned"] <= 10 for batch in batches)
    assert {"variants", "catalog", "projection_backfill", "projection"} & {
        batch["phase"] for batch in batches
    }
    assert storage["logical_compaction"]["phase"] == "complete"
    assert details["metadata"]["title"] == "Legacy"
    assert details["metadata"]["overview"] == "Preserved"
    with catalog.repository.connect() as connection:
        descriptor = json.loads(
            connection.execute(
                "SELECT descriptor_json FROM catalog_items WHERE id=?",
                (item["id"],),
            ).fetchone()[0]
        )
        projection = json.loads(
            connection.execute(
                "SELECT metadata_json FROM catalog_metadata_projection WHERE item_id=?",
                (item["id"],),
            ).fetchone()[0]
        )
    assert descriptor == {"schema": "adaos.media.resource.v1"}
    assert projection == {"overview": "Preserved"}


def test_storage_optimization_reports_settled_file_size(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "media-center-optimize.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    with catalog.repository.connect() as connection:
        connection.execute("CREATE TABLE storage_padding(payload BLOB)")
        connection.execute("INSERT INTO storage_padding VALUES (zeroblob(4194304))")
        connection.commit()
        connection.execute("DELETE FROM storage_padding")
        connection.commit()

    result = catalog.optimize_storage(reclaim=True)

    assert result["ok"] is True
    assert result["post_vacuum_checkpoint"][0] == 0
    assert result["after"]["db_bytes"] == catalog.repository.db_path.stat().st_size
    assert result["after"]["wal_bytes"] == 0
    assert result["reclaimed_bytes"] > 0


def test_agent_delta_retires_same_path_legacy_catalog_row(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
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
    all_items = catalog.list_items(media_kind="audio", include_missing=True, limit=30)[
        "items"
    ]

    assert applied["applied_count"] == 1
    assert len(available) == 1
    assert available[0]["agent_id"] == "agent-node-a"
    assert len(all_items) == 2
    assert next(item for item in all_items if not item["agent_id"])["missing"] is True


def test_agent_delta_retires_legacy_row_reached_through_path_alias(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()
    legacy = _resource("track-1.mp3")
    legacy["size_bytes"] = 1001
    legacy["source_path"] = r"\\server\music\Music\track-1.mp3"
    legacy["path"] = legacy["source_path"]
    repo.scan_resources([legacy], source="media_server", mark_missing=False)
    legacy_item = repo.list_items(media_kind="audio", limit=1)["items"][0]
    repo.set_favorite(legacy_item["id"], True)
    catalog = MediaCatalogCoordinator(repo)

    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/track-1.mp3")))
    available = catalog.list_items(media_kind="audio", limit=30)["items"]

    assert len(available) == 1
    assert available[0]["source_id"] == "source-1"
    assert available[0]["favorite"] is True


def test_schema_migration_retires_path_alias_without_losing_personal_state(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()
    legacy = _resource("track-1.mp3")
    legacy["size_bytes"] = 1001
    legacy["source_path"] = r"\\server\music\Music\track-1.mp3"
    legacy["path"] = legacy["source_path"]
    repo.scan_resources([legacy], source="media_server", mark_missing=False)
    catalog = MediaCatalogCoordinator(repo)
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/track-1.mp3")))
    all_items = catalog.list_items(media_kind="audio", include_missing=True, limit=30)[
        "items"
    ]
    legacy_id = next(item["id"] for item in all_items if not item["agent_id"])
    canonical_id = next(item["id"] for item in all_items if item["agent_id"])
    with repo.connect() as connection:
        connection.execute(
            "UPDATE catalog_items SET missing=0,favorite=1,play_count=5 WHERE id=?",
            (legacy_id,),
        )
        connection.execute(
            "UPDATE catalog_items SET favorite=0,play_count=0 WHERE id=?",
            (canonical_id,),
        )
        connection.execute(
            "INSERT INTO personal_media_state("
            "profile_id,item_id,favorite,play_count,updated_at"
            ") VALUES ('default',?,1,5,'2026-08-25T00:00:00Z')",
            (legacy_id,),
        )
        connection.execute(
            "INSERT INTO personal_media_state("
            "profile_id,item_id,favorite,play_count,updated_at"
            ") VALUES ('default',?,0,0,'2026-08-25T00:00:00Z')",
            (canonical_id,),
        )
        connection.execute(
            "UPDATE coordinator_meta SET value='previous' "
            "WHERE key='coordinator_schema_revision'"
        )
        connection.commit()

    migrated = MediaCatalogCoordinator(repo)
    available = migrated.list_items(media_kind="audio", limit=30)["items"]

    assert len(available) == 1
    assert available[0]["id"] == canonical_id
    assert available[0]["favorite"] is True
    assert available[0]["play_count"] == 5
    with repo.connect() as connection:
        personal = connection.execute(
            "SELECT favorite,play_count FROM personal_media_state "
            "WHERE profile_id='default' AND item_id=?",
            (canonical_id,),
        ).fetchone()
    assert personal is not None
    assert bool(personal["favorite"]) is True
    assert int(personal["play_count"]) == 5


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


def test_catalog_projection_redacts_local_paths_and_embedded_credentials(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    delta = _agent_delta(1, "Author/Book/001.mp3")
    descriptor = delta["source"]["descriptor"]
    descriptor["path"] = "/mnt/library/Author/Book/001.mp3"
    descriptor["content_ref"] = "root-a:/mnt/library/Author/Book/001.mp3"
    descriptor["direct_urls"] = ["http://node-a.local/media/ref-1?token=private-token"]
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
    monkeypatch.setenv(
        "ADAOS_MEDIA_REFERENCE_DB_PATH",
        str(tmp_path / "state" / "media_references.sqlite3"),
    )

    descriptor, error = main._register_media_file_descriptor(
        movie,
        root={"id": "root-1", "path": str(media_dir)},
    )

    assert error is None
    assert descriptor is not None
    assert descriptor["path"] == str(movie.resolve())
    assert descriptor["source_path"] == str(movie.resolve())
    assert descriptor["metadata"]["storage_mode"] == "reference"
    assert descriptor["content_path"].startswith(
        "/api/node/media/resources/content/ref_"
    )
    assert list(tmp_path.rglob("*.mp4")) == [movie]


def test_delete_root_removes_catalog_and_core_links_but_preserves_media(
    monkeypatch, tmp_path
):
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
    selected = next(
        item
        for item in repo.list_items(limit=15)["items"]
        if item["resource_id"] == "clip-12.mp4"
    )

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


def test_scan_roots_without_active_roots_returns_human_i18n_error(
    monkeypatch, tmp_path
):
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
    assert '"path": "assets/i18n/en.json"' in webui
    assert '"media_center.i18n.ru"' in webui
    assert '"path": "assets/i18n/ru.json"' in webui
    assert (SKILL_ROOT / "assets" / "i18n" / "en.json").is_file()
    assert (SKILL_ROOT / "assets" / "i18n" / "ru.json").is_file()


def test_coordinator_applies_agent_deltas_and_searches_folder_segments(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    page = _agent_page(
        _agent_delta(1, "Author Name/Important Book/001.mp3"),
        _agent_delta(2, "Artist/Album/02.mp3"),
        _agent_delta(3, "Classics/Аэроград. 1935.avi", kind="video"),
    )

    applied = catalog.apply_agent_page(page)
    by_folder = catalog.list_items(query="Important Book", media_kind="audio", limit=30)
    by_filename = catalog.list_items(query="001", media_kind="audio", limit=30)
    by_cyrillic_title = catalog.list_items(
        query="Аэроград", media_kind="video", limit=30
    )
    replay = catalog.apply_agent_page(page)

    assert applied["applied_count"] == 3
    assert [item["name"] for item in by_folder["items"]] == ["001.mp3"]
    assert [item["name"] for item in by_filename["items"]] == ["001.mp3"]
    assert [item["name"] for item in by_cyrillic_title["items"]] == [
        "Аэроград. 1935.avi"
    ]
    assert by_folder["ranking"] == {
        "version": "deterministic-fts-v2",
        "query_mode": "explicit_submit",
        "candidate_window_bounded": True,
        "candidate_limit": 96,
        "candidate_count": 1,
        "candidate_window_full": False,
    }
    assert by_folder["total_count_exact"] is False
    assert by_folder["partial"] is False
    assert replay["applied_count"] == 0
    assert replay["ignored_count"] == 3


def test_coordinator_keeps_unresolved_agent_sources_distinct(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "unresolved-sources.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    first = _agent_delta(1, "Video/First.avi", kind="video")
    second = _agent_delta(2, "Video/Second.avi", kind="video")
    for delta in (first, second):
        delta["source"]["resource_id"] = ""
        delta["source"]["descriptor"].pop("id")
        delta["source"]["descriptor"].pop("resource_id")

    applied = catalog.apply_agent_page(_agent_page(first, second))
    items = catalog.list_items(media_kind="video", limit=30)["items"]

    assert applied["applied_count"] == 2
    assert {item["name"] for item in items} == {"First.avi", "Second.avi"}
    with catalog.repository.connect() as connection:
        identities = connection.execute(
            "SELECT source,resource_id FROM catalog_items ORDER BY source"
        ).fetchall()
    assert [row["resource_id"] for row in identities] == ["", ""]
    assert len({row["source"] for row in identities}) == 2


def test_coordinator_projects_safe_versioned_artwork_url(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "artwork.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    delta = _agent_delta(1, "Artist/Album/01.mp3")
    delta["source"]["metadata"]["artwork"] = {
        "schema": "adaos.media.artwork.v1",
        "state": "ready",
        "provider_id": "media_library_agent.folder_artwork.v1",
        "source_kind": "folder",
        "exact_source_revision": 1,
        "exact_source_fingerprint": "fingerprint-1-1",
        "width": 720,
        "height": 720,
        "descriptor": {
            "browser_path": "/media/album-cover.jpg?token=secret",
            "source_path": "/mnt/private/Artist/Album/Cover.jpg",
        },
    }

    catalog.apply_agent_page(_agent_page(delta))
    artwork = catalog.list_items(media_kind="audio")["items"][0]["artwork"]

    assert artwork == {
        "schema": "adaos.media.artwork.v1",
        "state": "ready",
        "url": "/media/album-cover.jpg",
        "descriptor": {
            "schema": "adaos.media.resource.v1",
            "id": "",
            "resource_id": "",
            "name": "",
            "mime_type": "",
            "size_bytes": 0,
            "modified_at": "",
            "content_path": "",
            "routed_content_path": "/media/album-cover.jpg",
            "playback_id": "",
            "metadata": None,
        },
        "provider_id": "media_library_agent.folder_artwork.v1",
        "source_kind": "folder",
        "source_revision": 1,
        "source_fingerprint": "fingerprint-1-1",
        "width": 720,
        "height": 720,
        "error_code": "",
        "fallback_urls": [],
    }
    assert "/mnt/private" not in str(artwork)


def test_playback_observation_updates_profile_recent_once_per_bucket(monkeypatch):
    checkpoints = []
    published = []
    catalog = SimpleNamespace(
        checkpoint=lambda item_id, **kwargs: (
            checkpoints.append((item_id, kwargs)) or {"ok": True}
        )
    )
    monkeypatch.setattr(main, "_coordinator", lambda: catalog)
    monkeypatch.setattr(
        main,
        "_publish_library_snapshot",
        lambda *_args, **kwargs: published.append(kwargs),
    )
    main._PLAYBACK_OBSERVATION_CACHE.clear()
    event = {
        "item_id": "movie-1",
        "profile_id": "alice",
        "position_ms": 16_000,
        "duration_ms": 120_000,
        "state": "playing",
        "webspace_id": "desktop",
    }

    main.on_playback_observed(event)
    main.on_playback_observed({**event, "position_ms": 17_000})
    main.on_playback_observed({**event, "state": "stopped"})

    assert len(checkpoints) == 2
    assert checkpoints[0] == (
        "movie-1",
        {
            "profile_id": "alice",
            "position_ms": 16_000,
            "duration_ms": 120_000,
            "completed": False,
        },
    )
    assert checkpoints[1][1]["position_ms"] == 16_000
    assert len(published) == 2


def test_playback_observation_ignores_loading_and_coalesces_home_projection(
    monkeypatch,
):
    checkpoints = []
    published = []
    catalog = SimpleNamespace(
        checkpoint=lambda item_id, **kwargs: (
            checkpoints.append((item_id, kwargs)) or {"ok": True}
        )
    )
    monkeypatch.setattr(main, "_coordinator", lambda: catalog)
    monkeypatch.setattr(
        main,
        "_publish_library_snapshot",
        lambda *_args, **kwargs: published.append(kwargs),
    )
    main._PLAYBACK_OBSERVATION_CACHE.clear()
    base = {
        "item_id": "movie-1",
        "profile_id": "alice",
        "duration_ms": 180_000,
        "webspace_id": "desktop",
    }

    main.on_playback_observed({**base, "state": "loading", "position_ms": 0})
    for position_ms in (15_000, 30_000, 45_000, 60_000):
        main.on_playback_observed(
            {**base, "state": "playing", "position_ms": position_ms}
        )

    assert len(checkpoints) == 4
    assert len(published) == 2


def test_playback_observation_ignores_failure_before_media_started(monkeypatch):
    checkpoints = []
    catalog = SimpleNamespace(
        checkpoint=lambda item_id, **kwargs: (
            checkpoints.append((item_id, kwargs)) or {"ok": True}
        )
    )
    monkeypatch.setattr(main, "_coordinator", lambda: catalog)
    main._PLAYBACK_OBSERVATION_CACHE.clear()

    main.on_playback_observed(
        {
            "item_id": "unsupported-video",
            "profile_id": "alice",
            "position_ms": 0,
            "duration_ms": 0,
            "state": "error",
            "playback_confirmed": False,
        }
    )

    assert checkpoints == []


def test_playback_observation_leases_source_agent_pressure(monkeypatch):
    pressure_updates = []
    catalog = SimpleNamespace(
        source_binding=lambda **_kwargs: {"instance_id": "agent-instance-a"},
        checkpoint=lambda *_args, **_kwargs: {"ok": True},
    )

    class Topology:
        def invoke_agent(self, instance_id, operation, arguments, *, timeout_seconds):
            pressure_updates.append(
                (instance_id, operation, arguments, timeout_seconds)
            )
            return {"ok": True}

    monkeypatch.setattr(main, "_coordinator", lambda: catalog)
    monkeypatch.setattr(main, "_topology", lambda: Topology())
    monkeypatch.setattr(main, "_publish_library_snapshot", lambda *_a, **_k: True)
    main._PLAYBACK_OBSERVATION_CACHE.clear()
    main._PLAYBACK_PRESSURE_SESSIONS.clear()
    base = {
        "session_id": "session-a",
        "target_id": "tv-a",
        "item_id": "movie-1",
        "profile_id": "alice",
        "position_ms": 15_000,
        "duration_ms": 120_000,
        "playback_confirmed": True,
    }

    main.on_playback_observed({**base, "state": "playing"})
    main.on_playback_observed({**base, "state": "playing", "position_ms": 16_000})
    main.on_playback_observed({**base, "state": "paused"})

    assert [entry[2]["level"] for entry in pressure_updates] == [
        "playback",
        "normal",
    ]
    assert pressure_updates[0][2]["ttl_seconds"] == 120.0
    assert main._PLAYBACK_PRESSURE_SESSIONS == {}


def test_playback_pressure_session_cache_is_bounded(monkeypatch):
    catalog = SimpleNamespace(
        source_binding=lambda **_kwargs: {"instance_id": "agent-instance-a"}
    )
    monkeypatch.setattr(main, "_set_agent_resource_pressure", lambda *_a: None)
    main._PLAYBACK_PRESSURE_SESSIONS.clear()

    for index in range(main._PLAYBACK_PRESSURE_LIMIT + 5):
        main._reconcile_playback_pressure(
            catalog,
            {"session_id": f"session-{index}"},
            item_id=f"item-{index}",
            state="playing",
        )

    assert len(main._PLAYBACK_PRESSURE_SESSIONS) == main._PLAYBACK_PRESSURE_LIMIT


def test_coordinator_builds_typed_collections_and_bounded_cursor_pages(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    deltas = [
        _agent_delta(
            index,
            f"Series Name/Season 01/Series.Name.S01E{index:02d}.mp4",
            kind="video",
        )
        for index in range(1, 36)
    ]
    catalog.apply_agent_page(_agent_page(*deltas))

    first = catalog.list_items(media_kind="video", sort="title", limit=100)
    second = catalog.list_items(
        media_kind="video",
        sort="title",
        limit=30,
        cursor=first["pagination"]["next_cursor"],
    )
    collections = catalog.collections(kind="series")

    assert first["count"] == 30
    assert first["pagination"]["has_more"] is True
    assert second["count"] == 5
    assert {item["id"] for item in first["items"]}.isdisjoint(
        {item["id"] for item in second["items"]}
    )
    assert collections["items"][0]["title"] == "Series Name"
    assert collections["items"][0]["item_count"] == 35
    assert all(
        item["work_id"] and item["variant_id"] and item["collection_id"]
        for item in first["items"]
    )

    series = collections["items"][0]
    contents = catalog.collection_contents(series["id"], limit=30)
    continued = catalog.collection_contents(
        series["id"], limit=30, cursor=contents["pagination"]["next_cursor"]
    )

    assert contents["collection"]["id"] == series["id"]
    assert contents["collection"]["item_count"] == 35
    assert contents["count"] == 30
    assert contents["pagination"]["has_more"] is True
    assert continued["count"] == 5
    assert [item["name"] for item in contents["items"][:3]] == [
        "Series.Name.S01E01.mp4",
        "Series.Name.S01E02.mp4",
        "Series.Name.S01E03.mp4",
    ]
    assert continued["items"][-1]["name"] == "Series.Name.S01E35.mp4"
    assert contents["children"][0]["kind"] == "season"
    assert [
        {key: breadcrumb[key] for key in ("id", "title", "kind")}
        for breadcrumb in contents["breadcrumbs"]
    ] == [
        {
            "id": series["id"],
            "title": "Series Name",
            "kind": "series",
        }
    ]
    assert {item["id"] for item in contents["items"]}.isdisjoint(
        {item["id"] for item in continued["items"]}
    )


def test_collection_uses_a_ready_member_artwork_when_first_episode_has_none(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "collection-artwork.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    first = _agent_delta(1, "Show/Season 01/Show.S01E01.mp4", kind="video")
    second = _agent_delta(2, "Show/Season 01/Show.S01E02.mp4", kind="video")
    second["source"]["metadata"]["artwork"] = {
        "schema": "adaos.media.artwork.v1",
        "state": "ready",
        "provider_id": "media_library_agent.video_frame.v1",
        "source_kind": "generated_frame",
        "exact_source_revision": 1,
        "exact_source_fingerprint": "fingerprint-2-1",
        "width": 720,
        "height": 405,
        "descriptor": {"browser_path": "/media/show-episode-2.jpg"},
    }
    catalog.apply_agent_page(_agent_page(first, second))

    collection = catalog.collections(kind="series")["items"][0]

    assert collection["artwork"]["state"] == "ready"
    assert collection["artwork"]["url"] == "/media/show-episode-2.jpg"

    series_contents = catalog.collection_contents(collection["id"])
    season = series_contents["children"][0]
    season_contents = catalog.collection_contents(season["id"])

    assert series_contents["breadcrumbs"][0]["artwork"]["url"] == (
        "/media/show-episode-2.jpg"
    )
    assert season["artwork"]["url"] == "/media/show-episode-2.jpg"
    assert [
        breadcrumb["artwork"]["url"]
        for breadcrumb in season_contents["breadcrumbs"]
    ] == ["/media/show-episode-2.jpg", "/media/show-episode-2.jpg"]


def test_artwork_revision_preserves_series_identity_and_replaces_membership(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "series-artwork-revision.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    first = _agent_delta(
        1,
        "Black.Mirror.S07.1080p/Black.Mirror.S07E01.Common.People.mkv",
        kind="video",
    )
    second = _agent_delta(
        2,
        "Black.Mirror.S07.1080p/Black.Mirror.S07E02.Bete.Noire.mkv",
        kind="video",
    )
    catalog.apply_agent_page(_agent_page(first, second))
    before = catalog.collections(kind="series")["items"][0]
    updated = json.loads(json.dumps(first))
    updated.update({"id": "delta-3-2", "sequence": 3, "source_revision": 2})
    updated["source"].update({"revision": 2})
    updated["source"]["metadata"]["artwork"] = {
        "schema": "adaos.media.artwork.v1",
        "state": "ready",
        "provider_id": "media_library_agent.video_frame.v1",
        "source_kind": "generated_frame",
        "exact_source_revision": 1,
        "exact_source_fingerprint": "fingerprint-1-1",
        "width": 720,
        "height": 405,
        "descriptor": {"browser_path": "/media/black-mirror.jpg"},
    }

    catalog.apply_agent_page(_agent_page(updated))

    series = catalog.collections(kind="series")["items"]
    assert len(series) == 1
    assert series[0]["id"] == before["id"]
    assert series[0]["title"] == "Black Mirror"
    assert series[0]["item_count"] == 2
    assert series[0]["artwork"]["url"] == "/media/black-mirror.jpg"


def test_external_artwork_candidate_falls_back_for_items_and_collections(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "external-artwork.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Show/Season 01/Show.S01E01.mp4", kind="video"))
    )
    item = catalog.list_items(media_kind="video")["items"][0]
    catalog.record_metadata_claim(
        subject_ref=f"item:{item['id']}",
        field_name="artwork_candidates",
        value=[
            {
                "kind": "poster",
                "url": "https://image.tmdb.org/t/p/w500/poster.jpg?token=private",
                "provider": "tmdb",
            }
        ],
        provenance="media_center.tmdb.v1",
        confidence=0.9,
    )

    enriched = catalog.item_details(item["id"])["item"]
    collection = catalog.collections(kind="series")["items"][0]

    assert enriched["artwork"]["state"] == "ready"
    assert enriched["artwork"]["url"] == "https://image.tmdb.org/t/p/w500/poster.jpg"
    assert enriched["artwork"]["source_kind"] == "external_candidate"
    assert collection["artwork"]["url"] == enriched["artwork"]["url"]


def test_confirm_artwork_records_a_preferred_audited_choice(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "confirmed-artwork.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Music/Artist/Album/Track.mp3"))
    )
    item = catalog.list_items(media_kind="audio")["items"][0]
    catalog.record_metadata_claim(
        subject_ref=f"item:{item['id']}",
        field_name="artwork_candidates",
        value=[
            {
                "kind": "cover",
                "url": "https://coverartarchive.org/release/release-1/front-500",
            }
        ],
        provenance="media_center.musicbrainz.v1",
        confidence=0.9,
    )
    monkeypatch.setattr(main, "_publish_library_snapshot", lambda *_a, **_k: True)
    monkeypatch.setattr(main, "_publish_operation_snapshot", lambda *_a, **_k: True)

    result = main.review_item_artwork(
        item_id=item["id"], profile_id="default", action="confirm"
    )
    reviewed = catalog.item_details(item["id"])["item"]

    assert result["ok"] is True
    assert reviewed["metadata"]["artwork"]["confirmed"] is True
    assert reviewed["metadata"]["artwork_review"]["state"] == "confirmed"
    assert reviewed["metadata_provenance"]["artwork"] == "profile:default"


def test_audio_collection_artwork_is_inherited_by_sibling_tracks(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "album-artwork.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Music/Artist/Album/01.mp3"),
            _agent_delta(2, "Music/Artist/Album/02.mp3"),
        )
    )
    items = catalog.list_items(media_kind="audio")["items"]
    assert len({item["collection_id"] for item in items}) == 1
    collection_id = items[0]["collection_id"]

    catalog.record_metadata_claim(
        subject_ref=f"collection:{collection_id}",
        field_name="artwork",
        value={
            "schema": "adaos.media.artwork.v1",
            "state": "ready",
            "provider_id": "media_center.openlibrary.v1",
            "source_kind": "external_cached",
            "descriptor": {"browser_path": "/media/album.jpg"},
        },
        provenance="media_center.artwork_cache.v1",
        confidence=0.95,
    )

    inherited = [catalog.item_details(item["id"])["item"] for item in items]
    assert {item["artwork"]["url"] for item in inherited} == {"/media/album.jpg"}
    assert {item["metadata_provenance"]["artwork"] for item in inherited} == {
        "media_center.artwork_cache.v1"
    }


def test_background_operations_expose_a_direct_media_item_id(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "operation-item-id.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Music/Artist/Album/Track.mp3"))
    )
    operation = catalog.operations(limit=1)["items"][0]

    assert operation["subject_ref"] == f"item:{operation['item_id']}"
    assert operation["subject_title"] == "Track"


def test_collection_counts_only_available_logical_works_after_agent_rebind(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "series-agent-rebind.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    delta = _agent_delta(
        1,
        "Black Mirror/Season 07/Black.Mirror.S07E01.Common.People.mkv",
        kind="video",
    )
    compatibility_page = _agent_page(delta)
    compatibility_page["agent"] = {"id": "agent-local", "node_id": "local"}
    catalog.apply_agent_page(compatibility_page)

    canonical_page = _agent_page(delta)
    canonical_page["agent"] = {"id": "agent-node-a", "node_id": "node-a"}
    catalog.apply_agent_page(canonical_page)
    catalog.retire_unbound_agent_states(["agent-node-a"])

    series = catalog.collections(kind="series")["items"][0]
    contents = catalog.collection_contents(series["id"])

    assert series["item_count"] == 1
    assert contents["collection"]["item_count"] == 1
    assert contents["children"][0]["item_count"] == 1
    assert contents["count"] == 1
    assert contents["items"][0]["agent_id"] == "agent-node-a"


def test_filename_evidence_groups_inconsistently_named_season_folders(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "series-filename-evidence.sqlite3")
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(
                1,
                "MLP FIM 1 720p Dub/mlp fim s01e01 web dl rus.mkv",
                kind="video",
            ),
            _agent_delta(
                2,
                "MLP FIM 3 720p Dub/mlp fim s03e01 web dl rus.mkv",
                kind="video",
            ),
        )
    )

    series = catalog.collections(kind="series")["items"]
    contents = catalog.collection_contents(series[0]["id"])

    assert len(series) == 1
    assert series[0]["title"].casefold() == "mlp fim"
    assert {item["title"] for item in contents["children"]} == {
        "Season 1",
        "Season 3",
    }


def test_filename_parser_is_deterministic_without_optional_dependencies():
    coordinator_module.clear_filename_evidence_cache()

    evidence = coordinator_module._episode_filename_evidence(
        "Black.Mirror.S07E01.1080p.mkv"
    )

    assert evidence["title"] == "Black Mirror"
    assert evidence["season"] == 7
    assert evidence["episode"] == 1
    assert evidence["parser"] == "sxe-basename-v1"


def test_schema_migration_repairs_legacy_folder_scoped_series_identity(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "MEDIA_CENTER_DB_PATH", str(tmp_path / "series-identity-migration.sqlite3")
    )
    original_parser = coordinator_module._episode_filename_evidence
    monkeypatch.setattr(
        coordinator_module, "_episode_filename_evidence", lambda _name: {}
    )
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(
                1,
                "MLP FIM 1 720p Dub/mlp fim s01e01 web dl rus.mkv",
                kind="video",
            ),
            _agent_delta(
                2,
                "MLP FIM 3 720p Dub/mlp fim s03e01 web dl rus.mkv",
                kind="video",
            ),
        )
    )
    assert len(catalog.collections(kind="series")["items"]) == 2
    monkeypatch.setattr(
        coordinator_module, "_episode_filename_evidence", original_parser
    )
    with catalog.repository.connect() as connection:
        connection.execute(
            "UPDATE coordinator_meta SET value='legacy' "
            "WHERE key='video_series_identity_revision'"
        )
        connection.commit()

    result = catalog.ensure_schema(force=True)

    assert result["identity_repair"]["video_series"]["repaired_items"] == 2
    assert len(catalog.collections(kind="series")["items"]) == 1
    with catalog.repository.connect() as connection:
        indexes = {
            str(row["name"])
            for row in connection.execute("PRAGMA index_list(media_variants)")
        }
        plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT id,name,folder_path,metadata_json,node_id,source_id,
                work_id,variant_id,collection_id
            FROM catalog_items NOT INDEXED
            WHERE agent_id<>'' AND media_kind='video'
            """
        ).fetchall()
    assert "idx_media_center_variant_exact_source" in indexes
    assert any("SCAN catalog_items" in str(row["detail"]) for row in plan)
    assert all("TEMP B-TREE" not in str(row["detail"]) for row in plan)


def test_personal_state_is_profile_scoped_and_revisioned(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/Album/01.mp3")))
    item_id = catalog.list_items(media_kind="audio")["items"][0]["id"]

    alice = catalog.set_favorite(item_id, profile_id="alice", favorite=True)
    catalog.checkpoint(
        item_id, profile_id="alice", position_ms=45_000, duration_ms=180_000
    )
    alice_page = catalog.list_items(
        media_kind="audio", profile_id="alice", favorites_only=True
    )
    bob_page = catalog.list_items(
        media_kind="audio", profile_id="bob", favorites_only=True
    )

    assert alice["revision"] == 1
    assert alice_page["items"][0]["favorite"] is True
    assert alice_page["items"][0]["personal"]["resume_ms"] == 45_000
    assert bob_page["items"] == []


def test_profiles_enforce_query_playback_and_shared_surface_policy(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    clean = _agent_delta(1, "Movies/Family.mp4", kind="video")
    restricted = _agent_delta(2, "Movies/Restricted.mp4", kind="video")
    restricted["source"]["metadata"].update({"maturity_rating": 18, "explicit": True})
    catalog.apply_agent_page(_agent_page(clean, restricted))
    items = catalog.list_items(media_kind="video", profile_id="default", sort="title")
    restricted_item = next(
        item for item in items["items"] if item["title"] == "Restricted"
    )

    kids = catalog.list_items(media_kind="video", profile_id="kids", sort="title")
    denied = catalog.playback_plan(restricted_item["id"], profile_id="kids")
    personal = catalog.set_personal_state(
        restricted_item["id"], profile_id="default", rating=4, hidden=True
    )
    hidden = catalog.list_items(media_kind="video", profile_id="default")
    household_home = catalog.home(profile_id="household", limit=3, shared_surface=True)
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


def test_recommendations_are_bounded_explainable_and_support_opt_out(
    monkeypatch, tmp_path
):
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


def test_voice_request_uses_catalog_policy_and_existing_control_tools(
    monkeypatch, tmp_path
):
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


def test_compound_voice_request_is_bounded_and_requires_governed_approval(
    monkeypatch,
):
    monkeypatch.setattr(
        main,
        "_coordinator",
        lambda: (_ for _ in ()).throw(AssertionError("catalog must stay lazy")),
    )
    result = main.voice_request(
        text=("play the next episode in the living room and lower volume after 10 PM"),
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
    catalog.mark_agent_unavailable(
        "agent-node-a", node_id="node-a", reason="lease_expired"
    )

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
        schema = __import__("json").loads(
            (SKILL_ROOT / "schemas" / filename).read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(schema).validate(payload)


def test_twenty_thousand_item_catalog_remains_server_paged(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repo = MediaCenterRepository()
    rows = []
    for index in range(20_000):
        name = f"movie-{index:05d}.mp4"
        rows.append(
            (
                f"mc-{index:05d}",
                "media_server",
                name,
                name,
                f"Movie {index:05d}",
                "video",
                "video/mp4",
                1000 + index,
                "2026-08-19T00:00:00+00:00",
                f"/api/node/media/files/content/{name}",
                f"/media/files/content/{name}",
                "",
                f"/mnt/library/Movies/{name}",
                "{}",
                "{}",
                f"fp-{index}",
                "2026-08-19T00:00:00+00:00",
                "2026-08-19T00:00:00+00:00",
                0,
                0,
                0,
                "[]",
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


def test_search_indexes_use_catalog_rowids_for_addressed_updates(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repository = MediaCenterRepository()
    catalog = MediaCatalogCoordinator(repository)
    first = catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Movies/Example/movie.mp4", kind="video"))
    )
    assert first["applied_count"] == 1

    with repository.connect() as connection:
        row = connection.execute(
            "SELECT rowid AS catalog_rowid,id FROM catalog_items WHERE source_id='source-1'"
        ).fetchone()
        catalog_rowid = int(row["catalog_rowid"])
        assert (
            int(
                connection.execute(
                    "SELECT rowid FROM catalog_search "
                    "WHERE catalog_search MATCH 'example*'"
                ).fetchone()[0]
            )
            == catalog_rowid
        )
        assert (
            int(
                connection.execute(
                    "SELECT rowid FROM catalog_fuzzy_search "
                    "WHERE catalog_fuzzy_search MATCH 'exa'",
                ).fetchone()[0]
            )
            == catalog_rowid
        )

    updated = catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Movies/Example/movie.mp4", kind="video", revision=2)
        )
    )
    assert updated["applied_count"] == 1
    with repository.connect() as connection:
        indexed = connection.execute(
            "SELECT search_text FROM catalog_search WHERE rowid=?",
            (catalog_rowid,),
        ).fetchone()
        assert indexed is not None
        assert "Example" in str(indexed["search_text"])
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM catalog_search "
                "WHERE catalog_search MATCH 'example*'"
            ).fetchone()[0]
            == 1
        )
    assert catalog.list_items(query="Example", media_kind="video")["count"] == 1
    with repository.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM catalog_search").fetchone()[0] == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM catalog_fuzzy_search").fetchone()[
                0
            ]
            == 1
        )
        definitions = {
            str(row["name"]): str(row["sql"] or "")
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "content='catalog_items'" in definitions["catalog_search"]
        assert "content='catalog_items'" in definitions["catalog_fuzzy_search"]
        assert "tokenize='trigram'" in definitions["catalog_fuzzy_search"]
        assert "catalog_search_content" not in definitions
        assert "catalog_fuzzy_search_content" not in definitions


def test_diagnostics_never_count_scans_the_fts_virtual_table(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repository = MediaCenterRepository()
    catalog = MediaCatalogCoordinator(repository)
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Movies/Example/movie.mp4", kind="video"))
    )
    statements: list[str] = []
    original_connect = repository.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(repository, "connect", traced_connect)

    refreshed = catalog.refresh_search_index()
    diagnostics = catalog.diagnostics()

    assert refreshed["indexed_count"] == 1
    assert diagnostics["search"]["indexed_rows"] == 1
    normalized = [" ".join(statement.lower().split()) for statement in statements]
    assert not any(
        statement.startswith("select count(*) from catalog_search")
        for statement in normalized
    )


def test_status_and_collection_state_avoid_wide_catalog_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "compact-status.sqlite3"))
    repository = MediaCenterRepository()
    catalog = MediaCatalogCoordinator(repository)
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Movies/Example/movie.mp4", kind="video"))
    )

    def fail_wide_summary():
        raise AssertionError("wide catalog summary must not run on status paths")

    monkeypatch.setattr(repository, "summary", fail_wide_summary)
    monkeypatch.setattr(main, "_repository", lambda: repository)
    monkeypatch.setattr(main, "_coordinator", lambda _repository=None: catalog)
    monkeypatch.setattr(main, "_agent_sync_status", lambda: {"state": "idle"})
    monkeypatch.setattr(
        main,
        "_enrichment_runtime",
        lambda _catalog: SimpleNamespace(status=lambda: {"state": "idle"}),
    )

    compact = repository.compact_summary()
    state = catalog.collection_state(agent_sync={"state": "idle"})
    diagnostics = catalog.diagnostics()
    result = main.status()

    assert compact["available_count"] == 1
    assert compact["total_bytes_exact"] is False
    assert state["available_count"] == 1
    assert diagnostics["counts"]["sources"] == 1
    assert diagnostics["counts"]["works"] is None
    assert diagnostics["counts_exact"] is False
    assert result["summary"]["available_count"] == 1
    assert result["coordinator"]["counts"]["sources"] == 1
    assert result["facets"]["state"] == "deferred"
    assert result["storage"]["db_bytes"] > 0
    assert result["background_jobs"]["counts_by_kind"]


def test_search_rowid_migration_rebuilds_legacy_fts_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repository = MediaCenterRepository()
    catalog = MediaCatalogCoordinator(repository)
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Movies/Example/movie.mp4", kind="video"))
    )
    with repository.connect() as connection:
        item = connection.execute(
            "SELECT id,search_text FROM catalog_items WHERE source_id='source-1'"
        ).fetchone()
        connection.execute(
            "INSERT INTO catalog_search(catalog_search) VALUES ('delete-all')"
        )
        connection.execute(
            "INSERT INTO catalog_fuzzy_search(catalog_fuzzy_search) "
            "VALUES ('delete-all')"
        )
        connection.execute(
            "INSERT INTO catalog_search(rowid,search_text) VALUES (999999,?)",
            (str(item["search_text"]),),
        )
        connection.execute(
            "INSERT INTO catalog_fuzzy_search(rowid,fuzzy_text) VALUES (999999,?)",
            (str(item["search_text"]),),
        )
        connection.execute(
            "DELETE FROM coordinator_meta WHERE key='search_rowid_revision'"
        )
        connection.execute(
            "UPDATE coordinator_meta SET value='legacy' "
            "WHERE key='coordinator_schema_revision'"
        )
        connection.commit()

    migrated = MediaCatalogCoordinator(repository)
    pending = migrated.search_index_status()
    assert pending["phase"] == "pending"
    assert pending["partial"] is True
    assert migrated.list_items(query="Example", media_kind="video")["count"] == 1
    while True:
        batch = migrated.compact_search_index_batch(limit=10)
        if batch["complete"]:
            break
    with repository.connect() as connection:
        catalog_rowid = int(
            connection.execute(
                "SELECT rowid FROM catalog_items WHERE source_id='source-1'"
            ).fetchone()[0]
        )
        assert (
            int(
                connection.execute(
                    "SELECT rowid FROM catalog_search "
                    "WHERE catalog_search MATCH 'example*'"
                ).fetchone()[0]
            )
            == catalog_rowid
        )
        assert (
            int(
                connection.execute(
                    "SELECT rowid FROM catalog_fuzzy_search "
                    "WHERE catalog_fuzzy_search MATCH 'exa'"
                ).fetchone()[0]
            )
            == catalog_rowid
        )


def test_current_search_schema_preserves_completed_rowid_revision(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repository = MediaCenterRepository()
    catalog = MediaCatalogCoordinator(repository)

    with repository.connect() as connection:
        before = connection.execute(
            "SELECT value FROM coordinator_meta WHERE key='search_rowid_revision'"
        ).fetchone()[0]
        catalog._ensure_compact_search_schema(connection)
        after = connection.execute(
            "SELECT value FROM coordinator_meta WHERE key='search_rowid_revision'"
        ).fetchone()[0]

    assert after == before


def test_legacy_search_schema_is_deferred_and_rebuilt_in_bounded_batches(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repository = MediaCenterRepository()
    catalog = MediaCatalogCoordinator(repository)
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Movies/Example/movie.mp4", kind="video"))
    )
    with repository.connect() as connection:
        connection.executescript(
            """
            DROP TRIGGER IF EXISTS catalog_items_search_ai;
            DROP TRIGGER IF EXISTS catalog_items_search_ad;
            DROP TRIGGER IF EXISTS catalog_items_search_au;
            DROP TABLE catalog_search;
            DROP TABLE catalog_fuzzy_search;
            CREATE VIRTUAL TABLE catalog_search USING fts5(
                item_id UNINDEXED,text,
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE VIRTUAL TABLE catalog_fuzzy_search USING fts5(
                item_id UNINDEXED,tokens,tokenize='unicode61'
            );
            INSERT INTO catalog_search(item_id,text)
                SELECT id,search_text FROM catalog_items;
            INSERT INTO catalog_fuzzy_search(item_id,tokens)
                SELECT id,fuzzy_text FROM catalog_items;
            UPDATE coordinator_meta SET value='legacy'
                WHERE key='coordinator_schema_revision';
            INSERT OR REPLACE INTO coordinator_meta(key,value)
                VALUES ('search_rowid_revision','1');
            """
        )
        connection.commit()

    statements: list[str] = []
    original_connect = repository.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(repository, "connect", traced_connect)
    migrated = MediaCatalogCoordinator(repository)

    assert migrated.search_index_status()["phase"] == "pending"
    assert migrated.list_items(query="Example", media_kind="video")["count"] == 1
    activation_sql = "\n".join(statements).lower()
    assert "drop table catalog_search" not in activation_sql
    assert "alter table catalog_search" not in activation_sql

    first = migrated.compact_search_index_batch(limit=10)
    assert first["migrated_tables"] == [
        "catalog_search_legacy_1",
        "catalog_fuzzy_search_legacy_1",
    ]
    partial_search = migrated.list_items(query="Example", media_kind="video", limit=10)
    assert partial_search["count"] == 1
    assert partial_search["ranking"]["version"] == "legacy-fts-fallback-v1"
    while not first["complete"]:
        first = migrated.compact_search_index_batch(limit=10)
    assert migrated.search_index_status()["ready"] is True
    with repository.connect() as connection:
        assert (
            connection.execute(
                "SELECT rowid FROM catalog_search WHERE catalog_search MATCH 'example*'"
            ).fetchone()
            is not None
        )


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
            to_dict=lambda: {
                "deployment_id": deployment_id,
                "status": "ready",
                "digest": "plan-1",
            },
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
    assert (
        placements["skill:media_library_agent"].co_located_with
        == "skill:media_center_skill"
    )
    assert captured["expected_revision"] == 0


def test_deployment_status_projects_only_latest_component_activations(monkeypatch):
    from media_center import topology as topology_module

    monkeypatch.setattr(
        topology_module.deployment_sdk,
        "inspect",
        lambda deployment_id, limit: SimpleNamespace(
            to_dict=lambda: {
                "desired": {
                    "deployment_id": deployment_id,
                    "status": "planned",
                    "revision": 19,
                    "release_digest": f"sha256:{'a' * 64}",
                },
                "activations": [
                    {
                        "activation_id": "old-agent",
                        "node_id": "node-a",
                        "component_ref": "skill:media_library_agent",
                        "generation": 18,
                        "status": "inactive",
                    },
                    {
                        "activation_id": "current-agent",
                        "node_id": "node-a",
                        "component_ref": "skill:media_library_agent",
                        "generation": 19,
                        "status": "active",
                    },
                    {
                        "activation_id": "current-coordinator",
                        "node_id": "node-a",
                        "component_ref": "skill:media_center_skill",
                        "generation": 19,
                        "status": "active",
                    },
                    {
                        "activation_id": "removed-agent",
                        "node_id": "node-b",
                        "component_ref": "skill:media_library_agent",
                        "generation": 19,
                        "status": "removed",
                    },
                ],
                "operations": [],
                "activation_cursor": None,
                "operation_cursor": None,
            }
        ),
    )

    result = MediaCenterTopology().deployment_status(limit=50)

    assert result["nodes"] == [
        {
            "node_id": "node-a",
            "state": "active",
            "generation": 19,
            "components": [
                "skill:media_center_skill",
                "skill:media_library_agent",
            ],
            "agent": True,
        }
    ]


def test_media_topology_definition_is_idempotent_for_existing_datasets(monkeypatch):
    from media_center import topology as topology_module

    release_digest = "sha256:" + "a" * 64

    class DatasetValue:
        def __init__(self, dataset_id, desired_revision, contract):
            self.dataset_id = dataset_id
            self.desired_revision = desired_revision
            self.contract = contract

        def to_dict(self):
            return {
                "dataset_id": self.dataset_id,
                "desired_revision": self.desired_revision,
                "contract": self.contract,
            }

    existing = DatasetValue("media-files", 6, "media.files.v1")
    requested_group = SimpleNamespace(
        group_id="home",
        definition_id="agent",
        definition_version="1",
        desired_revision=6,
        to_dict=lambda: {"group_id": "home", "desired_revision": 6},
    )
    existing_group = SimpleNamespace(
        group_id="home",
        definition_id="agent",
        definition_version="1",
        desired_revision=6,
        to_dict=lambda: {"group_id": "home", "desired_revision": 6},
    )
    requested = {
        "media-files": DatasetValue("media-files", 6, "media.files.v1"),
        "media-catalog": DatasetValue("media-catalog", 1, "media.catalog.v1"),
    }
    defined = []
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "ServiceDefinition",
        SimpleNamespace(
            from_mapping=lambda value: SimpleNamespace(
                definition_id="agent",
                version="1",
                release_digest=release_digest,
            )
        ),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "ServiceGroup",
        SimpleNamespace(from_mapping=lambda value: requested_group),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "Dataset",
        SimpleNamespace(from_mapping=lambda value: requested[value["dataset_id"]]),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "define_service",
        lambda value: SimpleNamespace(to_dict=lambda: {"definition_id": "agent"}),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "define_group",
        lambda *args, **kwargs: pytest.fail("exact group must not be redefined"),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "inspect",
        lambda **kwargs: SimpleNamespace(
            groups=(existing_group,),
            datasets=(existing,),
            cursors={"groups": None, "datasets": None},
        ),
    )

    def define_dataset(value, *, expected_revision):
        defined.append((value.dataset_id, expected_revision))
        return value

    monkeypatch.setattr(
        topology_module.distributed_sdk, "define_dataset", define_dataset
    )
    monkeypatch.setattr(
        topology_module.deployment_sdk,
        "inspect",
        lambda deployment_id, *, limit: SimpleNamespace(
            to_dict=lambda: {
                "desired": {"release_digest": release_digest},
            }
        ),
    )

    result = MediaCenterTopology().define_topology(
        service_definition={"definition_id": "agent"},
        service_group={"group_id": "home"},
        datasets=[{"dataset_id": "media-files"}, {"dataset_id": "media-catalog"}],
        expected_group_revision=6,
    )

    assert result["ok"] is True
    assert defined == [("media-catalog", 0)]
    assert [item["dataset_id"] for item in result["datasets"]] == [
        "media-files",
        "media-catalog",
    ]


def test_media_topology_rejects_package_digest_before_any_mutation(monkeypatch):
    from media_center import topology as topology_module

    project_release = "sha256:" + "a" * 64
    package_digest = "sha256:" + "b" * 64
    mutations = []
    monkeypatch.setattr(
        topology_module.deployment_sdk,
        "inspect",
        lambda deployment_id, *, limit: SimpleNamespace(
            to_dict=lambda: {"desired": {"release_digest": project_release}}
        ),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "define_service",
        lambda value: mutations.append(("service", value)),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "define_group",
        lambda value, **kwargs: mutations.append(("group", value)),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "define_dataset",
        lambda value, **kwargs: mutations.append(("dataset", value)),
    )

    with pytest.raises(RuntimeError, match="media_center_topology_release_mismatch"):
        MediaCenterTopology().define_topology(
            service_definition={
                "schema": "adaos.distributed.service_definition.v1",
                "definition_id": "media-library-agent",
                "version": "2",
                "release_digest": package_digest,
                "compatible_components": ["skill:media_library_agent"],
                "provided_contracts": ["media.catalog.v1"],
                "topology_mode": "multi_instance",
                "protocol_version": "1",
                "required_capabilities": ["media.catalog"],
                "trust_class": "trusted",
                "adapter_contracts": ["adaos.distributed.adapter.v1"],
                "health_protocol": "adaos.health.v1",
                "drain_protocol": "adaos.drain.v1",
            },
            service_group={
                "schema": "adaos.distributed.service_group.v1",
                "group_id": "media-library-home",
                "definition_id": "media-library-agent",
                "definition_version": "2",
                "desired_generation": 2,
                "desired_instances": 2,
                "authority_policy": "singleton_fenced",
                "placement": {"mode": "selected_nodes", "node_ids": ["node-a"]},
                "linked_datasets": [],
                "route_policy": {"allow_partial": True},
                "desired_revision": 2,
                "observed_revision": 0,
                "status": "pending",
            },
            datasets=[],
            expected_group_revision=1,
        )

    assert mutations == []


def test_media_topology_requires_release_overlap_before_any_mutation(monkeypatch):
    from media_center import topology as topology_module

    old_release = "sha256:" + "a" * 64
    new_release = "sha256:" + "b" * 64
    mutations = []
    current_group = SimpleNamespace(
        group_id="media-library-home",
        definition_id="media-library-agent",
        definition_version="1",
        desired_revision=1,
        to_dict=lambda: {"version": "1"},
    )
    monkeypatch.setattr(
        topology_module.deployment_sdk,
        "inspect",
        lambda deployment_id, *, limit: SimpleNamespace(
            to_dict=lambda: {"desired": {"release_digest": new_release}}
        ),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "inspect",
        lambda **kwargs: SimpleNamespace(
            groups=(current_group,),
            datasets=(),
            cursors={"groups": None, "datasets": None},
        ),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "get_service_definition",
        lambda definition_id, version: SimpleNamespace(release_digest=old_release),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "define_service",
        lambda value: mutations.append(("service", value)),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "define_group",
        lambda value, **kwargs: mutations.append(("group", value)),
    )

    with pytest.raises(
        RuntimeError,
        match="media_center_topology_release_overlap_required",
    ):
        MediaCenterTopology().define_topology(
            service_definition={
                "schema": "adaos.distributed.service_definition.v2",
                "definition_id": "media-library-agent",
                "version": "2",
                "release_digest": new_release,
                "compatible_release_digests": [],
                "compatible_components": ["skill:media_library_agent"],
                "provided_contracts": ["media.catalog.v1"],
                "topology_mode": "multi_instance",
                "protocol_version": "1",
                "required_capabilities": ["media.catalog"],
                "trust_class": "trusted",
                "adapter_contracts": ["adaos.distributed.adapter.v1"],
                "health_protocol": "adaos.health.v1",
                "drain_protocol": "adaos.drain.v1",
            },
            service_group={
                "schema": "adaos.distributed.service_group.v1",
                "group_id": "media-library-home",
                "definition_id": "media-library-agent",
                "definition_version": "2",
                "desired_generation": 2,
                "desired_instances": 2,
                "authority_policy": "singleton_fenced",
                "placement": {"mode": "selected_nodes", "node_ids": ["node-a"]},
                "linked_datasets": [],
                "route_policy": {"allow_partial": True},
                "desired_revision": 2,
                "observed_revision": 0,
                "status": "pending",
            },
            datasets=[],
            expected_group_revision=1,
        )

    assert mutations == []


def test_media_topology_status_exposes_versioned_release_overlap(monkeypatch):
    from media_center import topology as topology_module

    group = SimpleNamespace(
        definition_id="media-library-agent",
        definition_version="2",
    )
    definition = SimpleNamespace(
        to_dict=lambda: {
            "schema": "adaos.distributed.service_definition.v2",
            "definition_id": "media-library-agent",
            "version": "2",
            "release_digest": "sha256:" + "b" * 64,
            "compatible_release_digests": ["sha256:" + "a" * 64],
        }
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "inspect",
        lambda **kwargs: SimpleNamespace(
            groups=(group,),
            to_dict=lambda: {
                "schema": "adaos.distributed.inspection.v1",
                "groups": [],
            },
        ),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "get_service_definition",
        lambda definition_id, version: definition,
    )

    status = MediaCenterTopology().distributed_status(limit=50)

    assert status["ok"] is True
    assert status["definitions"] == [definition.to_dict()]
    assert status["definition_errors"] == []
    assert status["partial"] is False


def test_deployment_status_consumes_bounded_history_and_orders_newest_operation(
    monkeypatch,
):
    from media_center import topology as topology_module

    calls = []

    def fake_inspect(
        deployment_id,
        *,
        limit,
        activation_cursor=None,
        operation_cursor=None,
    ):
        calls.append((activation_cursor, operation_cursor, limit))
        if activation_cursor:
            activations = [
                {
                    "activation_id": "current-agent",
                    "node_id": "node-a",
                    "component_ref": "skill:media_library_agent",
                    "generation": 26,
                    "status": "active",
                    "updated_at": "2026-08-21T00:02:00+00:00",
                }
            ]
            operations = []
            next_activation = None
            next_operation = "operation-page-2"
        elif operation_cursor:
            activations = []
            operations = [
                {
                    "operation_id": "new-operation",
                    "state": "succeeded",
                    "updated_at": "2026-08-21T00:03:00+00:00",
                }
            ]
            next_activation = "activation-page-2"
            next_operation = None
        else:
            activations = [
                {
                    "activation_id": "old-agent",
                    "node_id": "node-a",
                    "component_ref": "skill:media_library_agent",
                    "generation": 20,
                    "status": "inactive",
                    "updated_at": "2026-08-20T00:01:00+00:00",
                }
            ]
            operations = [
                {
                    "operation_id": "old-operation",
                    "state": "succeeded",
                    "updated_at": "2026-08-20T00:03:00+00:00",
                }
            ]
            next_activation = "activation-page-2"
            next_operation = "operation-page-2"
        return SimpleNamespace(
            to_dict=lambda: {
                "desired": {
                    "deployment_id": deployment_id,
                    "status": "planned",
                    "revision": 26,
                    "release_digest": f"sha256:{'a' * 64}",
                    "placements": [],
                },
                "activations": activations,
                "operations": operations,
                "activation_cursor": next_activation,
                "operation_cursor": next_operation,
            }
        )

    monkeypatch.setattr(topology_module.deployment_sdk, "inspect", fake_inspect)

    result = MediaCenterTopology().deployment_status(limit=50)

    assert result["nodes"] == [
        {
            "node_id": "node-a",
            "state": "active",
            "generation": 26,
            "components": ["skill:media_library_agent"],
            "agent": True,
        }
    ]
    assert [item["operation_id"] for item in result["operations"]] == [
        "new-operation",
        "old-operation",
    ]
    assert result["history_truncated"] is False
    assert calls == [
        (None, None, 50),
        ("activation-page-2", None, 50),
        (None, "operation-page-2", 50),
    ]


def test_deployment_apply_submits_durable_operation_and_reads_status(monkeypatch):
    from media_center import topology as topology_module

    captured = {}
    operation = SimpleNamespace(
        state="running",
        operation_id="deploymentop.media-center",
        to_dict=lambda: {
            "operation_id": "deploymentop.media-center",
            "state": "running",
        },
    )

    def fake_submit(plan_digest, *, idempotency_key, approvals):
        captured.update(
            {
                "plan_digest": plan_digest,
                "idempotency_key": idempotency_key,
                "approvals": approvals,
            }
        )
        return operation

    monkeypatch.setattr(topology_module.deployment_sdk, "submit", fake_submit)
    monkeypatch.setattr(
        topology_module.deployment_sdk,
        "get_operation",
        lambda operation_id: operation,
    )

    submitted = MediaCenterTopology().apply_deployment(
        "sha256:" + "a" * 64,
        idempotency_key="media-center-rollout-1",
    )
    observed = MediaCenterTopology().deployment_operation_status(
        "deploymentop.media-center"
    )

    assert submitted["ok"] is True
    assert submitted["accepted"] is True
    assert captured == {
        "plan_digest": "sha256:" + "a" * 64,
        "idempotency_key": "media-center-rollout-1",
        "approvals": ("remote_install",),
    }
    assert observed["operation"]["state"] == "running"


def test_repository_connection_context_commits_rolls_back_and_closes(
    tmp_path: Path,
) -> None:
    repository = MediaCenterRepository(tmp_path / "connection-lifecycle.sqlite3")

    with repository.connect() as committed:
        committed.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('connection_test', 'committed')"
        )
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        committed.execute("SELECT 1")

    with pytest.raises(RuntimeError, match="rollback"):
        with repository.connect() as rolled_back:
            rolled_back.execute(
                "UPDATE meta SET value='rolled-back' WHERE key='connection_test'"
            )
            raise RuntimeError("rollback")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        rolled_back.execute("SELECT 1")

    with repository.connect() as verifier:
        value = verifier.execute(
            "SELECT value FROM meta WHERE key='connection_test'"
        ).fetchone()[0]
    assert value == "committed"


def test_media_topology_exposes_reviewed_plan_apply_and_fenced_handoff(monkeypatch):
    from media_center import topology as topology_module

    captured = {}
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "plan_replica_change",
        lambda partition_id, **kwargs: SimpleNamespace(
            status="ready",
            to_dict=lambda: {
                "partition_id": partition_id,
                "plan_digest": "sha256:plan",
                **kwargs,
            },
        ),
    )

    def fake_apply(plan_digest, *, idempotency_key, approvals):
        captured["apply"] = (plan_digest, idempotency_key, approvals)
        return SimpleNamespace(
            state="succeeded",
            to_dict=lambda: {"status": "succeeded"},
        )

    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "get_plan",
        lambda plan_digest: SimpleNamespace(
            plan_digest=plan_digest,
            required_approvals=("authority_handoff",),
        ),
    )
    monkeypatch.setattr(topology_module.distributed_sdk, "apply_plan", fake_apply)
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "get_operation",
        lambda operation_id: SimpleNamespace(
            to_dict=lambda: {"operation_id": operation_id, "state": "running"}
        ),
    )

    def fake_handoff(partition_id, target_instance_id, **kwargs):
        captured["handoff"] = (partition_id, target_instance_id, kwargs)
        return SimpleNamespace(to_dict=lambda: {"epoch": 4})

    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "handoff_authority",
        fake_handoff,
    )
    topology = MediaCenterTopology()

    planned = topology.plan_topology_change(
        "catalog-home",
        action="handoff",
        source_instance_id="agent-a",
        target_instance_id="agent-b",
        replica_role="authority",
    )
    applied = topology.apply_topology_change(
        "sha256:plan",
        idempotency_key="apply-1",
    )
    operation_status = topology.topology_operation_status("topology-1")
    handed_off = topology.handoff_authority(
        "catalog-home",
        "agent-b",
        expected_partition_revision=3,
        expected_epoch=3,
        operation_id="handoff-1",
    )

    assert planned["ok"] is True
    assert planned["plan"]["target_instance_id"] == "agent-b"
    assert applied["ok"] is True
    assert captured["apply"] == (
        "sha256:plan",
        "apply-1",
        ("authority_handoff",),
    )
    assert operation_status == {
        "ok": True,
        "operation": {"operation_id": "topology-1", "state": "running"},
    }
    assert handed_off == {"ok": True, "lease": {"epoch": 4}}
    assert captured["handoff"][2]["expected_partition_revision"] == 3
    assert captured["handoff"][2]["expected_epoch"] == 3


def test_media_topology_owns_agent_membership_and_commits_remote_observation(
    monkeypatch,
):
    from media_center import topology as topology_module

    captured = {}
    parsed_instance = object()
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "ServiceInstance",
        SimpleNamespace(from_mapping=lambda value: parsed_instance),
    )

    def fake_register(instance, *, expected_revision, lease_seconds):
        captured["register"] = (instance, expected_revision, lease_seconds)
        return SimpleNamespace(
            to_dict=lambda: {"instance_id": "agent-b", "revision": 1}
        )

    monkeypatch.setattr(topology_module.distributed_sdk, "register", fake_register)
    partition_value = SimpleNamespace(
        partition_id="catalog-home",
        revision=1,
        to_dict=lambda: {"partition_id": "catalog-home", "revision": 1},
    )
    replica_value = SimpleNamespace(
        replica_id="catalog-home-agent-b",
        revision=1,
        to_dict=lambda: {"replica_id": "catalog-home-agent-b", "revision": 1},
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "Partition",
        SimpleNamespace(from_mapping=lambda value: partition_value),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "Replica",
        SimpleNamespace(from_mapping=lambda value: replica_value),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "put_partition",
        lambda value, *, expected_revision: (
            captured.update(partition_expected=expected_revision) or value
        ),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "observe_replica",
        lambda value, *, expected_revision: (
            captured.update(replica_expected=expected_revision) or value
        ),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "inspect",
        lambda **kwargs: SimpleNamespace(partitions=(), replicas=(), cursors={}),
    )
    topology = MediaCenterTopology()
    topology.invoke_agent = lambda *args, **kwargs: {
        "ok": True,
        "partition": {"partition_id": "catalog-home"},
        "replica": {"replica_id": "catalog-home-agent-b"},
        "external_media_copied": False,
    }

    registered = topology.register_agent({"instance_id": "agent-b"}, lease_seconds=999)
    observed = topology.observe_agent_topology(
        "agent-b",
        partition={"partition_id": "catalog-home"},
        replica={"replica_id": "catalog-home-agent-b"},
    )

    assert registered["instance"]["instance_id"] == "agent-b"
    assert captured["register"] == (parsed_instance, 0, 600)
    assert captured["partition_expected"] == 0
    assert captured["replica_expected"] == 0
    assert observed["external_media_copied"] is False


def test_media_topology_reuses_unchanged_partition_and_advances_replica(
    monkeypatch,
):
    from media_center import topology as topology_module

    def partition(value):
        payload = dict(value)
        return SimpleNamespace(
            partition_id=payload["partition_id"],
            revision=int(payload["revision"]),
            to_dict=lambda: dict(payload),
        )

    def replica(value):
        payload = dict(value)
        return SimpleNamespace(
            replica_id=payload["replica_id"],
            revision=int(payload["revision"]),
            to_dict=lambda: dict(payload),
        )

    partition_payload = {"partition_id": "catalog-home", "revision": 5}
    current_partition = partition(partition_payload)
    current_replica = replica({"replica_id": "replica-a", "revision": 3})
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "Partition",
        SimpleNamespace(from_mapping=partition),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "Replica",
        SimpleNamespace(from_mapping=replica),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "inspect",
        lambda **kwargs: SimpleNamespace(
            partitions=(current_partition,),
            replicas=(current_replica,),
            cursors={},
        ),
    )
    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "put_partition",
        lambda *args, **kwargs: pytest.fail("unchanged partition must not be written"),
    )
    captured = {}

    def observe_replica(value, *, expected_revision):
        captured["revision"] = value.revision
        captured["expected_revision"] = expected_revision
        return value

    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "observe_replica",
        observe_replica,
    )
    topology = MediaCenterTopology()
    topology.invoke_agent = lambda *args, **kwargs: {
        "ok": True,
        "partition": {"partition_id": "catalog-home", "revision": 1},
        "replica": {"replica_id": "replica-a", "revision": 1},
        "external_media_copied": False,
    }

    observed = topology.observe_agent_topology(
        "agent-a",
        partition={"partition_id": "catalog-home"},
        replica={"replica_id": "replica-a"},
    )

    assert observed["partition"] == partition_payload
    assert captured == {"revision": 4, "expected_revision": 3}


def test_media_topology_explains_only_declared_media_datasets(monkeypatch):
    from media_center import topology as topology_module

    captured = {}

    def fake_explain_route(dataset_id, partition_ids):
        captured["route"] = (dataset_id, partition_ids)
        return {"dataset_id": dataset_id, "eligible": []}

    monkeypatch.setattr(
        topology_module.distributed_sdk,
        "explain_route",
        fake_explain_route,
    )
    topology = MediaCenterTopology()

    explained = topology.explain_route(["catalog-home"] * 101)

    assert explained["dataset_id"] == "media-catalog-authority"
    assert captured["route"] == (
        "media-catalog-authority",
        ["catalog-home"] * 100,
    )
    with pytest.raises(ValueError, match="media_center_dataset_not_supported"):
        topology.explain_route([], dataset_id="media-center-sources")


def test_distributed_agent_sync_tracks_independent_cursors_and_partial_state(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))

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


def test_distributed_agent_sync_adapts_to_bounded_result_envelope(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    observed_limits = []

    class BoundedTopology:
        def agent_instances(self, *, limit=100):
            return [{"instance_id": "instance-a", "node_id": "node-a"}][:limit]

        def invoke_agent(self, instance_id, operation, arguments, *, timeout_seconds):
            observed_limits.append(arguments["limit"])
            if arguments["limit"] > 125:
                raise RuntimeError("service_invocation_result_too_large")
            page = _agent_page(_agent_delta(1, "Music/track.mp3"))
            page["agent"] = {"id": "agent-a", "node_id": "node-a"}
            page["has_more"] = False
            return page

    monkeypatch.setattr(main, "_topology", lambda: BoundedTopology())
    catalog = MediaCatalogCoordinator(MediaCenterRepository())

    result = main._sync_agents(catalog, max_pages=1, limit=1000)

    assert result["ok"] is True
    assert result["agents"][0]["effective_page_limit"] == 125
    assert result["agents"][0]["page_limit_backoffs"] == 3
    assert observed_limits == [1000, 500, 250, 125]
    assert result["applied_count"] == 1


def test_agent_sync_worker_continues_bounded_cursor_catchup() -> None:
    completed = threading.Event()
    published = []
    calls = 0
    skill_identity = contextvars.ContextVar("test_media_center_skill_identity")
    skill_identity.set("media_center_skill")

    def sync():
        nonlocal calls
        assert skill_identity.get() == "media_center_skill"
        calls += 1
        has_more = calls < 2
        if not has_more:
            completed.set()
        return {
            "ok": True,
            "mode": "distributed",
            "agent_count": 2,
            "applied_count": 100,
            "has_more": has_more,
        }

    worker = MediaAgentSyncWorker(
        sync,
        publish=lambda: published.append(skill_identity.get()),
        catchup_seconds=0.05,
        poll_seconds=30,
    )
    try:
        assert worker.ensure_started() is True
        assert completed.wait(2.0) is True
    finally:
        worker.dispose()

    assert calls == 2
    assert published == ["media_center_skill", "media_center_skill"]
    assert worker.status()["state"] == "idle"
    assert worker.status()["last_result"]["has_more"] is False


def test_agent_sync_worker_does_not_wake_or_republish_for_unchanged_reads() -> None:
    completed = threading.Event()
    calls = 0
    published = []

    def sync():
        nonlocal calls
        calls += 1
        completed.set()
        return {
            "ok": True,
            "mode": "distributed",
            "agent_count": 1,
            "applied_count": 0,
            "has_more": False,
        }

    worker = MediaAgentSyncWorker(
        sync,
        publish=lambda: published.append(calls),
        poll_seconds=30,
    )
    try:
        assert worker.ensure_started() is True
        assert completed.wait(2.0) is True
        assert worker.ensure_started() is False
        time.sleep(0.1)
        assert calls == 1
        assert published == [1]

        completed.clear()
        assert worker.ensure_started(wake=True) is False
        assert completed.wait(2.0) is True
    finally:
        worker.dispose()

    assert calls == 2
    assert published == [1]


def test_complete_distributed_sync_retires_local_compatibility_state(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))

    class DistributedTopology:
        def agent_instances(self, *, limit=100):
            return [{"instance_id": "instance-a", "node_id": "node-a"}][:limit]

        def invoke_agent(self, instance_id, operation, arguments, *, timeout_seconds):
            assert instance_id == "instance-a"
            assert operation == "pull_deltas"
            page = _agent_page(_agent_delta(1, "Music/track.mp3"))
            page["agent"] = {"id": "agent-a", "node_id": "node-a"}
            page["has_more"] = False
            return page

    monkeypatch.setattr(main, "_topology", lambda: DistributedTopology())
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    local_page = _agent_page(_agent_delta(1, "Music/track.mp3"))
    local_page["agent"] = {"id": "agent-local", "node_id": "local"}
    catalog.apply_agent_page(local_page)

    result = main._sync_agents(catalog, max_pages=1, limit=100)

    assert result["ok"] is True
    assert result["retired_compatibility"]["retired_agent_ids"] == ["agent-local"]
    assert result["retired_compatibility"]["retired_source_count"] == 1
    assert [item["agent_id"] for item in result["participation"]["agents"]] == [
        "agent-a"
    ]
    assert [item["agent_id"] for item in catalog.list_items()["items"]] == ["agent-a"]


def test_incomplete_distributed_sync_preserves_local_compatibility_state(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))

    class DistributedTopology:
        def agent_instances(self, *, limit=100):
            return [{"instance_id": "instance-a", "node_id": "node-a"}][:limit]

        def invoke_agent(self, instance_id, operation, arguments, *, timeout_seconds):
            page = _agent_page(_agent_delta(1, "Music/track.mp3"))
            page["agent"] = {"id": "agent-a", "node_id": "node-a"}
            page["has_more"] = True
            return page

    monkeypatch.setattr(main, "_topology", lambda: DistributedTopology())
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    local_page = _agent_page(_agent_delta(1, "Music/track.mp3"))
    local_page["agent"] = {"id": "agent-local", "node_id": "local"}
    catalog.apply_agent_page(local_page)

    result = main._sync_agents(catalog, max_pages=1, limit=100)

    assert result["retired_compatibility"]["deferred"] is True
    assert {item["agent_id"] for item in result["participation"]["agents"]} == {
        "agent-a",
        "agent-local",
    }


def test_local_agent_sync_resumes_from_its_durable_cursor(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))

    class LocalTopology:
        def agent_instances(self, *, limit=100):
            return []

    observed_cursors = []

    def invoke_agent(operation, arguments, *, timeout):
        if operation == "status":
            assert arguments == {"compact": True}
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
        page = _agent_page(_agent_delta(sequence, f"Music/track-{sequence}.mp3"))
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
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/track.mp3")))
    item_id = catalog.list_items(media_kind="audio")["items"][0]["id"]
    published = []

    import adaos.sdk.io as sdk_io

    monkeypatch.setattr(
        sdk_io,
        "stream_variable_publish",
        lambda receiver, value, **kwargs: published.append((receiver, value, kwargs)),
    )
    monkeypatch.setattr(main, "_coordinator", lambda repository=None: catalog)
    monkeypatch.setattr(main, "_agent_sync_status", lambda: {"state": "idle"})

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
    assert published[-1][1]["collection_state"]["state"] == "updating"
    assert published[-1][1]["home"]["state"] == "updating"
    assert published[-1][2]["_meta"] == {
        "webspace_id": "desktop",
        "params": {"profile_id": "alice", "shared_surface": False},
    }


def test_library_snapshot_sequence_advances_across_revision_planes(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/first.mp3")))
    item_id = catalog.list_items(media_kind="audio")["items"][0]["id"]
    catalog.set_favorite(item_id, profile_id="alice", favorite=True)
    catalog.set_favorite(item_id, profile_id="alice", favorite=False)
    published = []

    import adaos.sdk.io as sdk_io

    monkeypatch.setattr(
        sdk_io,
        "stream_variable_publish",
        lambda receiver, value, **kwargs: published.append((receiver, value, kwargs)),
    )
    monkeypatch.setattr(main, "_agent_sync_status", lambda: {"state": "idle"})

    main._publish_library_snapshot(catalog, profile_id="alice")
    catalog.apply_agent_page(_agent_page(_agent_delta(2, "Music/second.mp3")))
    main._publish_library_snapshot(catalog, profile_id="alice")

    assert published[0][1]["catalog_revision"] == 1
    assert published[0][1]["personal_revision"] == 2
    assert published[1][1]["catalog_revision"] == 2
    assert published[1][1]["personal_revision"] == 2
    assert published[1][2]["seq"] > published[0][2]["seq"]


def test_profile_revision_advances_for_changes_to_distinct_items(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Music/first.mp3"),
            _agent_delta(2, "Music/second.mp3"),
        )
    )
    items = catalog.list_items(media_kind="audio", limit=10)["items"]

    catalog.set_favorite(items[0]["id"], profile_id="alice", favorite=True)
    first_revision = catalog.profile_revision("alice")
    catalog.set_favorite(items[1]["id"], profile_id="alice", favorite=True)

    assert first_revision == 1
    assert catalog.profile_revision("alice") == 2


def test_home_snapshot_cache_reuses_ready_projection_and_refreshes_status(tmp_path):
    calls = []

    class Catalog:
        repository = SimpleNamespace(db_path=tmp_path / "media_center.sqlite3")

        def home(self, **kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "profile_id": kwargs["profile_id"],
                "shared_surface": kwargs["shared_surface"],
                "items": [{"id": "item-1", "title": "Movie"}],
            }

    main._HOME_SNAPSHOT_CACHE.clear()
    catalog = Catalog()
    first = main._cached_home_snapshot(
        catalog,
        profile_id="alice",
        shared_surface=False,
        catalog_revision=4,
        personal_revision=2,
        collection_state={"state": "updating", "active_operation_count": 3},
    )
    second = main._cached_home_snapshot(
        catalog,
        profile_id="alice",
        shared_surface=False,
        catalog_revision=4,
        personal_revision=2,
        collection_state={"state": "ready", "active_operation_count": 0},
    )

    assert len(calls) == 1
    assert first["state"] == "updating"
    assert second["state"] == "ready"
    assert second["items"] == [{"id": "item-1", "title": "Movie"}]


def test_home_stream_projection_keeps_routes_without_metadata_payload() -> None:
    item = {
        "id": "movie-1",
        "title": "Movie",
        "media_kind": "video",
        "quality": {"height": 2160, "bitrate": 80_000_000, "codec": "hevc"},
        "metadata": {"plot": "x" * 20_000},
        "metadata_provenance": {"plot": {"witness": "y" * 20_000}},
        "personal": {"resume_ms": 123_000, "history": ["z" * 20_000]},
        "artwork": {
            "state": "ready",
            "url": "/media/files/content/poster.jpg",
            "descriptor": {
                "schema": "adaos.media.resource.v1",
                "resource_id": "poster.jpg",
                "name": "poster.jpg",
                "mime_type": "image/jpeg",
                "content_path": "/api/node/media/files/content/poster.jpg",
                "routed_content_path": "/media/files/content/poster.jpg",
                "metadata": {"provenance": "p" * 20_000},
                "delivery": {"diagnostics": "d" * 20_000},
            },
            "source_fingerprint": "f" * 512,
        },
        "shelf_id": "movies",
        "shelf_title": "Movies",
        "queue_source_type": "item",
        "queue_source_id": "movie-1",
    }

    compact = main._compact_home_item(item)

    assert compact["quality"] == {"height": 2160}
    assert compact["artwork"]["descriptor"] == {
        "resource_id": "poster.jpg",
        "name": "poster.jpg",
        "mime_type": "image/jpeg",
        "content_path": "/api/node/media/files/content/poster.jpg",
        "routed_content_path": "/media/files/content/poster.jpg",
    }
    assert "metadata" not in compact
    assert "personal" not in compact
    assert len(json.dumps(compact, separators=(",", ":")).encode("utf-8")) < 750


def test_library_snapshot_request_preserves_receiver_params(monkeypatch):
    published = []
    coordinator = object()
    monkeypatch.setattr(main, "_coordinator", lambda repository=None: coordinator)
    monkeypatch.setattr(
        main,
        "_publish_library_snapshot",
        lambda catalog, **kwargs: published.append((catalog, kwargs)),
    )

    main.on_media_center_snapshot_requested(
        {
            "receiver": "media_center.library_state",
            "webspace_id": "television",
            "params": {"profile_id": "household", "shared_surface": True},
        }
    )

    assert published == [
        (
            coordinator,
            {
                "profile_id": "household",
                "shared_surface": True,
                "webspace_id": "television",
                "reuse_ready": True,
            },
        )
    ]


def test_operation_snapshot_request_is_workspace_scoped(monkeypatch):
    published = []
    coordinator = object()
    monkeypatch.setattr(main, "_coordinator", lambda repository=None: coordinator)
    monkeypatch.setattr(
        main,
        "_publish_operation_snapshot",
        lambda catalog, **kwargs: published.append((catalog, kwargs)),
    )

    main.on_media_center_snapshot_requested(
        {
            "receiver": "media_center.operation_state",
            "webspace_id": "desktop",
        }
    )

    assert published == [(coordinator, {"webspace_id": "desktop"})]


def test_library_snapshot_publish_failure_is_observable(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())

    import adaos.sdk.io as sdk_io

    monkeypatch.setattr(
        sdk_io,
        "stream_variable_publish",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bridge down")),
    )

    with caplog.at_level("ERROR", logger="adaos.skill.media_center"):
        published = main._publish_library_snapshot(
            catalog,
            profile_id="household",
            shared_surface=True,
            webspace_id="television",
        )

    assert published is False
    assert "library snapshot publish failed" in caplog.text
    assert "profile=household" in caplog.text
    assert "webspace=television" in caplog.text


def test_hierarchical_collections_and_folder_browse_are_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Shows/Example/Season 2/Example.S02E03.mp4", kind="video"),
            _agent_delta(2, "Music/Album/Disc 2/04 Track.mp3"),
            _agent_delta(3, "Books/Novel/Part 1/001.mp3"),
            _agent_delta(4, "Shows/Example Extras/Trailer.mp4", kind="video"),
        )
    )

    kinds = {item["kind"] for item in catalog.collections(limit=30)["items"]}
    roots = catalog.folders(limit=30)
    root = catalog.folders(agent_id="agent-node-a", root_id="root-a", limit=1)
    second = catalog.folders(
        agent_id="agent-node-a",
        root_id="root-a",
        limit=1,
        cursor=root["pagination"]["next_cursor"],
    )
    nested = catalog.folders(
        agent_id="agent-node-a",
        root_id="root-a",
        parent="Shows/Example",
        limit=30,
    )
    leaf = catalog.folders(
        agent_id="agent-node-a",
        root_id="root-a",
        parent="Shows/Example/Season 2",
        limit=30,
    )

    assert {"series", "season", "album", "disc", "audiobook", "book_part"} <= kinds
    assert roots["count"] == 1
    assert roots["items"][0]["name"] == "library"
    assert roots["items"][0]["path"] == "/"
    assert roots["items"][0]["queue_ref"] == "agent-node-a:root-a:"
    assert root["count"] == 1
    assert root["pagination"]["has_more"] is True
    assert second["items"][0]["path"] != root["items"][0]["path"]
    assert nested["items"][0]["path"] == "Shows/Example/Season 2"
    assert nested["folder_count"] == 1
    assert (
        nested["items"][0]["queue_ref"] == "agent-node-a:root-a:Shows/Example/Season 2"
    )
    assert nested["breadcrumbs"][0] == {
        "name": "Folders",
        "name_i18n": {"key": "runtime.media_center.ui.folders"},
        "agent_id": "",
        "root_id": "",
        "path": "",
        "root": True,
    }
    assert nested["breadcrumbs"][-1] == {
        "name": "Example",
        "agent_id": "agent-node-a",
        "root_id": "root-a",
        "path": "Shows/Example",
        "root": False,
    }
    assert leaf["folder_count"] == 0
    assert leaf["file_count"] == 1
    assert leaf["items"][0]["entry_type"] == "media"
    assert leaf["items"][0]["media_kind"] == "video"
    assert leaf["items"][0]["queue_source_type"] == "folder"
    assert leaf["items"][0]["queue_source_id"] == (
        "agent-node-a:root-a:Shows/Example/Season 2"
    )
    _validate_schema("folder-node.v1.schema.json", nested["items"][0])

    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(
                1,
                "Shows/Example/Season 2/Example.S02E03.mp4",
                kind="video",
                revision=2,
                operation="removed",
            )
        )
    )
    removed_leaf = catalog.folders(
        agent_id="agent-node-a",
        root_id="root-a",
        parent="Shows/Example/Season 2",
        limit=30,
    )
    assert removed_leaf["items"] == []
    assert removed_leaf["total_count"] == 0


def test_home_exposes_bounded_flattened_shelf_items(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
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


def test_home_history_shelves_are_personal_index_driven(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Movies/Example.mp4", kind="video"))
    )
    item_id = catalog.list_items(sort="title", limit=1)["items"][0]["id"]

    initial = {item["id"]: item for item in catalog.home(limit=3)["shelves"]}
    assert initial["continue"]["items"] == []
    assert initial["recent"]["items"] == []

    catalog.checkpoint(
        item_id,
        profile_id="default",
        position_ms=30_000,
        duration_ms=120_000,
    )
    active = {item["id"]: item for item in catalog.home(limit=3)["shelves"]}
    assert [item["id"] for item in active["continue"]["items"]] == [item_id]
    assert [item["id"] for item in active["recent"]["items"]] == [item_id]

    catalog.checkpoint(
        item_id,
        profile_id="default",
        position_ms=120_000,
        duration_ms=120_000,
        completed=True,
    )
    completed = {item["id"]: item for item in catalog.home(limit=3)["shelves"]}
    assert completed["continue"]["items"] == []
    assert [item["id"] for item in completed["recent"]["items"]] == [item_id]
    with catalog.repository.connect() as connection:
        indexes = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA index_list(personal_media_state)"
            ).fetchall()
        }
    assert {
        "idx_personal_media_recent_item",
        "idx_personal_media_continue",
        "idx_personal_media_favorite",
    } <= indexes


def test_collection_state_distinguishes_configuration_indexing_and_empty(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    page = _agent_page()
    page["library_state"] = {
        "root_count": 0,
        "source_count": 0,
        "available_count": 0,
        "active_job_count": 0,
        "failed_job_count": 0,
    }
    catalog.apply_agent_page(page, instance_id="instance-a")

    assert catalog.collection_state(agent_sync={"state": "idle"})["state"] == (
        "unconfigured"
    )

    page["library_state"] = {
        **page["library_state"],
        "root_count": 1,
        "active_job_count": 1,
    }
    catalog.apply_agent_page(page, instance_id="instance-a")
    indexing = catalog.collection_state(agent_sync={"state": "idle"})

    assert indexing["state"] == "indexing"
    assert indexing["configured"] is True
    assert indexing["active_operation_count"] == 1

    page["library_state"] = {
        **page["library_state"],
        "active_job_count": 0,
    }
    catalog.apply_agent_page(page, instance_id="instance-a")

    assert catalog.collection_state(agent_sync={"state": "idle"})["state"] == ("empty")


def test_playlists_are_profile_scoped_ordered_and_revision_safe(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Music/Album/01.mp3"),
            _agent_delta(2, "Music/Album/02.mp3"),
            _agent_delta(3, "Music/Album/03.mp3"),
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
    appended = catalog.add_playlist_item(
        playlist_id,
        profile_id="alice",
        item_id=items[2]["id"],
    )

    assert [item["id"] for item in page["items"]] == [
        items[1]["id"],
        items[0]["id"],
    ]
    assert denied["error"] == "playlist_not_found"
    assert conflict["error"] == "playlist_revision_conflict"
    assert updated["playlist"]["revision"] == 2
    assert appended["added"] is True
    assert appended["playlist"]["revision"] == 3
    assert catalog.get_playlist(playlist_id, profile_id="bob")["ok"] is True
    _validate_schema("playlist.v1.schema.json", updated["playlist"])


def test_playback_plan_selects_endpoint_compatible_variant_and_route(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    high = _agent_delta(1, "Movies/UHD/Example.mp4", kind="video")
    high["source"]["metadata"]["technical"] = {
        "height": 2160,
        "bitrate": 24_000_000,
        "codec": "hevc",
    }
    high["source"]["descriptor"]["direct_urls"] = ["http://node-a.local/media/ref-1"]
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
    assert plan["compatibility"]["mode"] == "direct"
    assert plan["compatibility"]["ready"] is True
    _validate_schema("playback-plan.v2.schema.json", plan)
    _validate_schema("playback-route.v1.schema.json", plan["route"])


def test_audio_identity_uses_folder_context_and_migrates_existing_collisions(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repository = MediaCenterRepository()
    catalog = MediaCatalogCoordinator(repository)
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Audiobooks/Author A/Book A/01/0.mp3"),
            _agent_delta(2, "Audiobooks/Author B/Book B/01/0.mp3"),
        )
    )
    items = catalog.list_items(media_kind="audio", sort="title", limit=30)["items"]
    by_source = {item["source_id"]: item for item in items}
    source_two_variant_id = by_source["source-2"]["variant_id"]

    assert by_source["source-1"]["work_id"] != by_source["source-2"]["work_id"]
    assert (
        by_source["source-1"]["collection_id"] != by_source["source-2"]["collection_id"]
    )
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
            "DELETE FROM coordinator_meta WHERE key='audio_context_identity_revision'"
        )
        connection.commit()

    migrated = MediaCatalogCoordinator(repository)
    repaired = migrated.list_items(media_kind="audio", sort="title", limit=30)["items"]
    repaired_by_source = {item["source_id"]: item for item in repaired}

    assert (
        repaired_by_source["source-1"]["work_id"]
        != repaired_by_source["source-2"]["work_id"]
    )
    assert (
        repaired_by_source["source-1"]["collection_id"]
        != repaired_by_source["source-2"]["collection_id"]
    )
    assert (
        migrated.playback_plan(repaired_by_source["source-2"]["id"])["source_id"]
        == "source-2"
    )
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
        for item in migrated.list_items(media_kind="audio", sort="title", limit=30)[
            "items"
        ]
        if item["source_id"] == "source-2"
    )
    assert update["applied_count"] == 1
    assert updated_source_two["variant_id"] == source_two_variant_id


def test_replicated_audio_path_remains_one_work_with_multiple_variants(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
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

    items = catalog.list_items(media_kind="audio", sort="title", limit=30)["items"]
    assert len({item["work_id"] for item in items}) == 1
    assert catalog.playback_plan(items[0]["id"])["decision"]["candidate_count"] == 2


def test_derived_rendition_is_a_hidden_exact_source_variant(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
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
    _validate_schema("playback-plan.v2.schema.json", plan)


def test_federated_deep_search_is_bounded_policy_filtered_and_observable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
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
                "items": [
                    source["source"]
                    | {"match": {"stage": "agent_technical_fts", "rank": -1.0}}
                ],
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
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
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
        endpoint_capabilities={"codecs": ["hevc"], "containers": ["mkv"]},
        force=True,
    )

    assert result["status"] == "queued"
    assert result["source_binding"] == {
        "agent_id": "agent-node-a",
        "node_id": "node-a",
        "instance_id": "instance-a",
    }
    assert captured["operation"] == "plan_rendition"
    assert captured["arguments"]["source_id"] == "source-1"
    assert captured["arguments"]["force"] is True


def test_rendition_operations_exposes_bounded_compact_agent_projection(monkeypatch):
    captured = {}

    def invoke_agent(operation, arguments, *, timeout):
        captured.update(operation=operation, arguments=arguments, timeout=timeout)
        return (
            {
                "ok": True,
                "items": [
                    {
                        "id": "rendition-1",
                        "source_id": "source-1",
                        "source_name": "Legacy movie.avi",
                        "source_relative_path": "Movies/Legacy movie.avi",
                        "media_kind": "video",
                        "profile": "browser-mp4-v1",
                        "status": "running",
                        "priority": 25,
                        "requested_at": "2026-08-25T10:00:00+00:00",
                        "started_at": "2026-08-25T10:01:00+00:00",
                        "output_bytes": 123456,
                        "source_size_bytes": 987654,
                        "target": {"abr_ladder": [1, 2, 3], "private": "large"},
                        "output": {"path": "/private/rendition.mp4"},
                    }
                ],
                "resource_pressure": "playback",
                "artwork": {
                    "state": "running",
                    "active_job_count": 2,
                    "examined_count": 50,
                    "queued_count": 12,
                    "last_run_at": "2026-08-25T10:02:00+00:00",
                    "sources": {
                        "total": 100,
                        "pending": 2,
                        "ready": 70,
                        "failed": 3,
                        "unavailable": 25,
                    },
                },
                "job_retention": {
                    "artwork_queue_window": 4,
                    "artwork_queue_removed": 120,
                    "artwork_queue_overflow": 8,
                    "rendition_history_removed": 30,
                    "scan_history_removed": 12,
                    "migration_history_removed": 2,
                    "complete": False,
                    "updated_at": "2026-08-25T10:03:00+00:00",
                },
            },
            "",
        )

    monkeypatch.setattr(main, "_invoke_agent", invoke_agent)

    result = main.rendition_operations(source_id="source-1", limit=500)

    assert result["ok"] is True
    assert result["bounded"] is True
    assert result["count"] == 1
    assert result["resource_pressure"] == "playback"
    assert result["artwork"] == {
        "schema": "adaos.media_center.artwork_operation.v1",
        "state": "running",
        "active_job_count": 2,
        "examined_count": 50,
        "queued_count": 12,
        "ready_count": 70,
        "pending_count": 2,
        "failed_count": 3,
        "unavailable_count": 25,
        "total_count": 100,
        "last_run_at": "2026-08-25T10:02:00+00:00",
        "last_completed_at": "",
    }
    assert result["job_retention"] == {
        "schema": "adaos.media_center.job_retention.v1",
        "state": "ready",
        "artwork_queue_window": 4,
        "artwork_queue_removed": 120,
        "artwork_queue_overflow": 8,
        "rendition_history_removed": 30,
        "scan_history_removed": 12,
        "migration_history_removed": 2,
        "complete": False,
        "updated_at": "2026-08-25T10:03:00+00:00",
    }
    assert result["items"] == [
        {
            "schema": "adaos.media_center.rendition_operation.v1",
            "id": "rendition-1",
            "source_id": "source-1",
            "source_name": "Legacy movie.avi",
            "source_relative_path": "Movies/Legacy movie.avi",
            "media_kind": "video",
            "profile": "browser-mp4-v1",
            "status": "running",
            "priority": 25,
            "requested_at": "2026-08-25T10:00:00+00:00",
            "started_at": "2026-08-25T10:01:00+00:00",
            "finished_at": "",
            "output_bytes": 123456,
            "source_size_bytes": 987654,
            "cancel_requested": False,
            "error": None,
        }
    ]
    assert captured == {
        "operation": "list_rendition_jobs",
        "arguments": {"source_id": "source-1", "limit": 100},
        "timeout": 20.0,
    }


def test_rendition_operations_settles_with_observable_error(monkeypatch):
    monkeypatch.setattr(
        main,
        "_invoke_agent",
        lambda *_args, **_kwargs: (None, "agent_timeout"),
    )

    result = main.rendition_operations(limit=30)

    assert result["ok"] is False
    assert result["error"] == "rendition_operations_unavailable"
    assert result["items"] == []
    assert result["retryable"] is True


def test_profile_customizes_home_order_view_density_and_target(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
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
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))

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
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
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
    selected_episode = collection_queue["items"][20]["item_id"]
    selected_collection_queue = catalog.build_queue(
        source_type="collection",
        source_id=collection["id"],
        limit=500,
        start_item_id=selected_episode,
    )
    folder_queue = catalog.build_queue(
        source_type="folder",
        source_id="agent-node-a:Shows/Example/Season 01",
        limit=5,
    )

    assert playlist_queue["count"] == 35
    assert [item["item_id"] for item in playlist_queue["items"]] == ordered
    assert collection_queue["count"] == 35
    assert selected_collection_queue["initial_item_id"] == selected_episode
    assert selected_collection_queue["initial_index"] == 20
    assert folder_queue["count"] == 5
    assert folder_queue["limit"] == 5
    _validate_schema("queue-source.v1.schema.json", playlist_queue)


def test_catalog_keyset_cursor_matches_legacy_offset_without_gaps(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            *[
                _agent_delta(
                    index,
                    f"Shows/Keyset/Example Episode {index:03d}.mp4",
                    kind="video",
                )
                for index in range(1, 96)
            ]
        )
    )

    first = catalog.list_items(media_kind="video", sort="title", limit=30)
    second = catalog.list_items(
        media_kind="video",
        sort="title",
        limit=30,
        cursor=first["pagination"]["next_cursor"],
    )
    legacy_second = catalog.list_items(
        media_kind="video",
        sort="title",
        limit=30,
        offset=30,
    )
    assert [item["id"] for item in second["items"]] == [
        item["id"] for item in legacy_second["items"]
    ]

    item_ids = [item["id"] for item in first["items"]]
    cursor = first["pagination"]["next_cursor"]
    while cursor:
        page = catalog.list_items(
            media_kind="video", sort="title", limit=30, cursor=cursor
        )
        item_ids.extend(item["id"] for item in page["items"])
        cursor = page["pagination"]["next_cursor"]
    assert len(item_ids) == 95
    assert len(set(item_ids)) == 95

    search_ids: list[str] = []
    cursor = ""
    while True:
        page = catalog.list_items(
            query="Example",
            media_kind="video",
            sort="title",
            limit=30,
            cursor=cursor,
        )
        search_ids.extend(item["id"] for item in page["items"])
        cursor = str(page["pagination"]["next_cursor"] or "")
        if not cursor:
            break
    assert len(search_ids) == 95
    assert len(set(search_ids)) == 95


def test_catalog_search_bounds_broad_window_but_finds_rare_late_match(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    monkeypatch.delenv("MEDIA_CENTER_SEARCH_CANDIDATE_LIMIT", raising=False)
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            *[
                _agent_delta(
                    index,
                    f"Shows/Window/Window Episode {index:03d}.mp4",
                    kind="video",
                )
                for index in range(1, 301)
            ]
        )
    )

    broad_ids: list[str] = []
    cursor = ""
    first = None
    while True:
        page = catalog.list_items(
            query="Window", media_kind="video", limit=30, cursor=cursor
        )
        first = first or page
        broad_ids.extend(item["id"] for item in page["items"])
        cursor = str(page["pagination"]["next_cursor"] or "")
        if not cursor:
            break

    assert len(broad_ids) == 96
    assert len(set(broad_ids)) == 96
    assert first["ranking"]["candidate_window_full"] is True
    assert first["total_count_exact"] is False

    late = catalog.list_items(query="Window Episode 300", media_kind="video", limit=30)
    assert [item["name"] for item in late["items"]] == ["Window Episode 300.mp4"]
    assert late["ranking"]["candidate_count"] == 1


def test_catalog_search_filters_media_kind_before_bounded_candidate_window(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    monkeypatch.delenv("MEDIA_CENTER_SEARCH_CANDIDATE_LIMIT", raising=False)
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            *[
                _agent_delta(
                    index,
                    f"Music Videos/Video {index:03d}.mp4",
                    kind="video",
                )
                for index in range(1, 301)
            ],
            *[
                _agent_delta(
                    300 + index,
                    f"Audiobooks/Folder Music/Track {index:03d}.mp3",
                    kind="audio",
                )
                for index in range(1, 11)
            ],
        )
    )

    page = catalog.list_items(query="Music", media_kind="audio", limit=30, sort="title")

    assert page["count"] == 10
    assert {item["media_kind"] for item in page["items"]} == {"audio"}
    assert all("Folder Music" in item["folder_path"] for item in page["items"])
    assert page["ranking"]["candidate_count"] == 10


def test_source_revision_coalesces_queued_enrichment_and_bounds_history(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repository = MediaCenterRepository()
    catalog = MediaCatalogCoordinator(repository)

    for revision in range(1, 31):
        result = catalog.apply_agent_page(
            _agent_page(
                _agent_delta(
                    1,
                    "Music/Changing Track.mp3",
                    revision=revision,
                )
            )
        )
        assert result["ok"] is True

    with repository.connect() as connection:
        rows = connection.execute(
            """
            SELECT kind,status,COUNT(*) AS count FROM media_background_jobs
            GROUP BY kind,status ORDER BY kind,status
            """
        ).fetchall()
    counts = {(row["kind"], row["status"]): int(row["count"]) for row in rows}

    for kind in ("metadata_enrichment", "embedding", "fingerprint"):
        assert counts[(kind, "queued")] == 1
    assert sum(counts.values()) == 3


def test_catalog_corrections_are_audited_reversible_and_non_destructive(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
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
    _validate_schema("catalog-correction.v1.schema.json", correction["correction"])
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
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Author/Book/001.mp3")))
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


def test_optional_provider_failure_completes_local_enrichment_with_warning(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))

    class UnavailableProvider:
        provider_id = "fixture.external.v1"
        supported_jobs = frozenset({"metadata_enrichment"})

        @staticmethod
        def accepts(_subject, *, job_kind):
            return job_kind == "metadata_enrichment"

        @staticmethod
        def claims(_subject, *, job_kind):
            raise MetadataProviderError("fixture_upstream_unavailable")

    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/Artist/Track.mp3")))
    worker = MediaEnrichmentWorker(
        catalog,
        providers=[DeterministicLocalProvider(), UnavailableProvider()],
        poll_seconds=0.2,
    )

    result = worker.run_once()
    operation = catalog.operations(limit=1)["items"][0]

    assert result["status"] == "completed"
    assert result["provider_warnings"] == [
        {
            "provider_id": "fixture.external.v1",
            "error_code": "fixture_upstream_unavailable",
        }
    ]
    assert operation["status"] == "completed"
    assert operation["progress"]["provider_warnings"] == result["provider_warnings"]


def test_manual_metadata_override_rejects_and_reverses_an_external_match(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Lectures/AI systems.mp4", kind="video"))
    )
    item = catalog.list_items(media_kind="video")["items"][0]
    subject = f"item:{item['id']}"
    catalog.record_metadata_claim(
        subject_ref=subject,
        field_name="title",
        value="Incorrect documentary",
        provenance="media_center.tmdb.v1",
        confidence=0.95,
    )

    correction = catalog.apply_correction(
        operation="metadata",
        subject_ref=subject,
        values={
            "title": "AI systems lecture",
            "overview": "A manually reviewed lecture.",
            "reject_providers": ["media_center.tmdb.v1"],
        },
        actor_ref="profile:alice",
    )
    corrected = catalog.item_details(item["id"], profile_id="alice")["item"]
    reversed_result = catalog.reverse_correction(
        correction["correction"]["id"], actor_ref="profile:alice"
    )
    restored = catalog.item_details(item["id"], profile_id="alice")["item"]

    assert correction["ok"] is True
    assert corrected["title"] == "AI systems lecture"
    assert corrected["metadata"]["overview"] == "A manually reviewed lecture."
    assert corrected["library_path"] == "Lectures/AI systems.mp4"
    assert reversed_result["ok"] is True
    assert restored["title"] == "Incorrect documentary"


def test_local_nfo_claims_drive_public_metadata_search_and_precedence(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    delta = _agent_delta(1, "Movies/opaque-file.mkv", kind="video")
    delta["source"]["metadata"].update(
        {
            "title": "NFO Canonical Title",
            "year": 2024,
            "genres": ["Science Fiction", "Drama"],
            "rating": 8.1,
            "local_nfo": {
                "schema": "adaos.media.local_nfo.v1",
                "state": "ready",
                "values": {
                    "title": "NFO Canonical Title",
                    "year": 2024,
                    "genres": ["Science Fiction", "Drama"],
                    "rating": 8.1,
                },
            },
        }
    )
    catalog.apply_agent_page(_agent_page(delta))

    nfo_item = catalog.list_items(query="Canonical", media_kind="video")["items"][0]
    assert nfo_item["title"] == "NFO Canonical Title"
    assert nfo_item["year"] == 2024
    assert nfo_item["genres"] == ["Science Fiction", "Drama"]
    assert nfo_item["metadata_provenance"]["title"] == (
        "media_library_agent.local_nfo.v1"
    )

    catalog.record_metadata_claim(
        subject_ref=f"item:{nfo_item['id']}",
        field_name="title",
        value="External Lower Priority",
        provenance="media_center.tmdb.v1",
        confidence=1.0,
    )
    assert catalog.list_items(media_kind="video")["items"][0]["title"] == (
        "NFO Canonical Title"
    )
    catalog.record_metadata_claim(
        subject_ref=f"item:{nfo_item['id']}",
        field_name="title",
        value="User Preferred Title",
        provenance="profile:default",
        confidence=0.5,
        preferred=True,
    )
    resolved = catalog.list_items(query="Preferred", media_kind="video")["items"][0]
    assert resolved["title"] == "User Preferred Title"
    assert resolved["metadata_provenance"]["title"] == "profile:default"
    details = catalog.item_details(resolved["id"], profile_id="default")
    assert details["item"]["title"] == "User Preferred Title"
    assert details["item"]["year"] == 2024
    assert details["enrichment"]["metadata_revision"] >= 1


def test_metadata_facets_filters_and_plex_style_sorts_are_server_bounded(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    deltas = []
    for sequence, title, year, rating, genres in (
        (1, "Old Drama", 2001, 7.0, ["Drama"]),
        (2, "New Science", 2022, 9.2, ["Science Fiction"]),
        (3, "Newer Drama", 2015, 8.4, ["Drama"]),
    ):
        delta = _agent_delta(sequence, f"Movies/{title}.mp4", kind="video")
        delta["source"]["metadata"].update(
            {"title": title, "year": year, "rating": rating, "genres": genres}
        )
        deltas.append(delta)
    catalog.apply_agent_page(_agent_page(*deltas))

    facets = catalog.metadata_facets(
        dimension="genre",
        media_kind="video",
        profile_id="default",
        include_all=True,
    )
    assert facets["items"][0]["option_value"] == ""
    assert facets["items"][0]["label_i18n"] == {
        "key": "runtime.media_center.ui.all_genres"
    }
    assert {item["value"]: item["count"] for item in facets["items"][1:]} == {
        "Drama": 2,
        "Science Fiction": 1,
    }
    drama = catalog.list_items(
        media_kind="video", genre="Drama", sort="year", sort_direction="desc"
    )
    assert [item["title"] for item in drama["items"]] == [
        "Newer Drama",
        "Old Drama",
    ]
    first = catalog.list_items(media_kind="video", sort="rating", limit=2)
    second = catalog.list_items(
        media_kind="video",
        sort="rating",
        limit=2,
        cursor=first["pagination"]["next_cursor"],
    )
    assert [item["title"] for item in first["items"]] == [
        "New Science",
        "Newer Drama",
    ]
    assert [item["title"] for item in second["items"]] == ["Old Drama"]


def test_release_date_claim_populates_year_projection_and_facet(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Movies/Movie.mp4", kind="video"))
    )
    item = catalog.list_items(media_kind="video")["items"][0]

    catalog.record_metadata_claim(
        subject_ref=f"item:{item['id']}",
        field_name="release_date",
        value="2001-11-16",
        provenance="media_center.tmdb.v1",
        confidence=0.9,
    )

    enriched = catalog.list_items(media_kind="video")["items"][0]
    years = catalog.metadata_facets(
        dimension="year", media_kind="video", include_all=False
    )["items"]
    assert enriched["year"] == 2001
    assert years[0]["option_value"] == 2001


def test_metadata_provider_configuration_explains_managed_provider_state():
    statuses = {
        item["provider_id"]: item
        for item in metadata_provider_configuration(
            {
                "external_enabled": True,
                "musicbrainz_enabled": True,
                "tmdb_enabled": True,
                "locale": "ru-RU",
            },
            tmdb_credential_configured=False,
        )
    }

    assert statuses["media_center.deterministic_local.v1"]["enabled"] is True
    assert statuses["media_center.tmdb.v1"]["enabled"] is True
    assert statuses["media_center.tmdb.v1"]["ready"] is False
    assert statuses["media_center.tmdb.v1"]["state"] == "credentials_missing"
    assert statuses["media_center.tmdb.v1"]["reason"] == "credentials_missing"
    assert statuses["media_center.tmdb.v1"]["language"] == "ru-RU"
    assert statuses["media_center.musicbrainz.v1"]["enabled"] is True


def test_operation_snapshot_uses_authoritative_provider_configuration():
    runtime = {
        "state": "running",
        "providers": [
            {
                "provider_id": "media_center.tmdb.v1",
                "state": "credentials_missing",
                "reason": "credentials_missing",
                "request_count": 7,
            },
            {
                "provider_id": "media_center.musicbrainz.v1",
                "state": "degraded",
                "last_error": "musicbrainz_request_timeout",
                "failure_count": 2,
            },
        ],
    }
    configured = [
        {
            "provider_id": "media_center.tmdb.v1",
            "enabled": True,
            "ready": True,
            "state": "ready",
            "reason": "configured",
        },
        {
            "provider_id": "media_center.musicbrainz.v1",
            "enabled": True,
            "state": "ready",
            "reason": "configured",
        },
    ]

    merged = main._authoritative_enrichment_runtime(runtime, configured)
    providers = {item["provider_id"]: item for item in merged["providers"]}

    assert providers["media_center.tmdb.v1"]["state"] == "ready"
    assert providers["media_center.tmdb.v1"]["reason"] == "configured"
    assert providers["media_center.tmdb.v1"]["request_count"] == 7
    assert providers["media_center.musicbrainz.v1"]["state"] == "degraded"
    assert providers["media_center.musicbrainz.v1"]["reason"] == (
        "musicbrainz_request_timeout"
    )


def test_tmdb_secret_store_failure_is_not_reported_as_missing_credentials():
    providers = {
        item["provider_id"]: item
        for item in main._metadata_provider_configuration(
            {
                "external_enabled": True,
                "musicbrainz_enabled": True,
                "tmdb_enabled": True,
                "locale": "ru-RU",
            },
            {
                "configured": None,
                "state": "unavailable",
                "reason": "secret_store_unavailable",
            },
        )
    }

    assert providers["media_center.tmdb.v1"]["state"] == "unavailable"
    assert providers["media_center.tmdb.v1"]["reason"] == "secret_store_unavailable"
    assert providers["media_center.musicbrainz.v1"]["state"] == "ready"


def test_tmdb_secret_cache_survives_transient_store_failure(monkeypatch):
    import adaos.sdk.data.secrets as secrets

    original_cache = main._tmdb_credential_cache
    original_initialized = main._tmdb_credential_cache_initialized
    try:
        main._tmdb_credential_cache = ""
        main._tmdb_credential_cache_initialized = False
        monkeypatch.setattr(secrets, "get", lambda *_args, **_kwargs: "token")
        assert main._read_tmdb_credential_state()["state"] == "ready"

        def unavailable(*_args, **_kwargs):
            raise RuntimeError("keyring unavailable")

        monkeypatch.setattr(secrets, "get", unavailable)
        state = main._read_tmdb_credential_state()

        assert state == {
            "value": "token",
            "configured": True,
            "state": "degraded",
            "reason": "secret_store_temporarily_unavailable",
        }
    finally:
        main._tmdb_credential_cache = original_cache
        main._tmdb_credential_cache_initialized = original_initialized


def test_tmdb_provider_sends_only_normalized_evidence_and_caches_details():
    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if "/search/" in url:
                return Response(
                    {
                        "results": [
                            {
                                "id": 671,
                                "title": "Harry Potter and the Philosopher's Stone",
                                "original_title": "Harry Potter and the Philosopher's Stone",
                                "release_date": "2001-11-16",
                                "poster_path": "/poster.jpg",
                                "popularity": 100,
                            }
                        ]
                    }
                )
            return Response(
                {
                    "id": 671,
                    "title": "Harry Potter and the Philosopher's Stone",
                    "release_date": "2001-11-16",
                    "poster_path": "/poster.jpg",
                    "genres": [{"name": "Fantasy"}],
                    "runtime": 152,
                    "vote_average": 7.9,
                    "credits": {
                        "cast": [
                            {
                                "name": "Daniel Radcliffe",
                                "character": "Harry",
                                "order": 0,
                            }
                        ]
                    },
                    "videos": {
                        "results": [
                            {"site": "YouTube", "type": "Trailer", "key": "trailer-1"}
                        ]
                    },
                    "external_ids": {"imdb_id": "tt0241527"},
                    "release_dates": {"results": []},
                }
            )

    session = Session()
    provider = TmdbMetadataProvider(
        credential="secret-token",
        session=session,
        minimum_interval_seconds=0.1,
    )
    subject = {
        "subject_ref": "item:item-a",
        "title": "Harry.Potter.2001.1080p.BluRay.mkv",
        "media_kind": "video",
        "folder_path": "/private/movies/Harry Potter",
        "descriptor": {"path": "/private/movies/Harry Potter/movie.mkv"},
        "metadata": {},
    }

    first = provider.claims(subject, job_kind="metadata_enrichment")
    second = provider.claims(subject, job_kind="metadata_enrichment")

    assert first == second
    assert len(session.calls) == 2
    url, request = session.calls[0]
    assert url.endswith("/search/movie")
    assert request["params"] == {
        "query": "Harry Potter",
        "language": "en-US",
        "include_adult": "false",
        "page": 1,
        "year": 2001,
    }
    assert "/private" not in repr(request)
    assert request["headers"]["Authorization"] == "Bearer secret-token"
    assert {claim["field_name"] for claim in first} >= {
        "tmdb_id",
        "title",
        "genres",
        "actors",
        "trailers",
        "artwork_candidates",
    }
    assert provider.status()["cache_hit_count"] == 2

    api_key_session = Session()
    api_key_provider = TmdbMetadataProvider(
        credential="0123456789abcdef0123456789abcdef",
        session=api_key_session,
        minimum_interval_seconds=0.1,
    )
    api_key_provider.claims(subject, job_kind="metadata_enrichment")
    api_key_request = api_key_session.calls[0][1]
    assert "Authorization" not in api_key_request["headers"]
    assert api_key_request["params"]["api_key"] == ("0123456789abcdef0123456789abcdef")
    assert api_key_provider.validate()["ok"] is True
    assert api_key_session.calls[-1][0].endswith("/authentication")
    assert api_key_provider.status()["credential_kind"] == "api_key"


def test_musicbrainz_provider_is_rate_limited_cached_and_audio_only():
    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "recordings": [
                    {
                        "id": "recording-1",
                        "title": "Track",
                        "score": 100,
                        "artist-credit": [{"artist": {"name": "Artist"}}],
                        "releases": [
                            {"id": "release-1", "title": "Album", "date": "2020"}
                        ],
                        "genres": [],
                        "tags": [
                            {"name": "Alternative rock", "count": 12},
                            {"name": "Rock", "count": 42},
                            {"name": "rock", "count": 3},
                        ],
                    }
                ]
            }

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    provider = MusicBrainzMetadataProvider(
        session=session, minimum_interval_seconds=1.0
    )
    subject = {
        "subject_ref": "item:audio-a",
        "title": "Track.mp3",
        "media_kind": "audio",
        "metadata": {"artists": ["Artist"], "album": "Album"},
    }
    first = provider.claims(subject, job_kind="metadata_enrichment")
    second = provider.claims(subject, job_kind="metadata_enrichment")

    assert first == second
    assert len(session.calls) == 1
    assert provider.status()["request_count"] == 1
    assert provider.status()["last_success_at"] is not None
    assert session.calls[0][1]["headers"]["User-Agent"].startswith("AdaOS-MediaCenter")
    assert {claim["field_name"] for claim in first} >= {
        "title",
        "artists",
        "album",
        "genres",
        "artwork_candidates",
    }
    genre_claim = next(claim for claim in first if claim["field_name"] == "genres")
    assert genre_claim["value"] == ["Rock", "Alternative rock"]
    assert (
        provider.claims(
            {
                "subject_ref": "item:numeric",
                "title": "3 08 01 04.mp3",
                "media_kind": "audio",
                "metadata": {},
            },
            job_kind="metadata_enrichment",
        )
        == []
    )
    assert len(session.calls) == 1
    assert (
        provider.accepts(
            {
                "subject_ref": "item:audiobook",
                "title": "Chapter 01.mp3",
                "folder_path": "\u0410\u0443\u0434\u0438\u043e\u043a\u043d\u0438\u0433\u0438/Author/Book",
                "media_kind": "audio",
                "metadata": {},
            },
            job_kind="metadata_enrichment",
        )
        is False
    )


def test_path_identity_supplies_artist_and_album_for_numbered_audio():
    provider = DeterministicLocalProvider()

    claims = provider.claims(
        {
            "subject_ref": "item:bb-king-09",
            "title": "09.mp3",
            "folder_path": "B.B King/Live at the Regal/01",
            "media_kind": "audio",
            "metadata": {},
        },
        job_kind="metadata_enrichment",
    )
    values = {claim["field_name"]: claim["value"] for claim in claims}

    assert values["artists"] == ["B.B King"]
    assert values["album"] == "Live at the Regal"


def test_path_identity_treats_audiobook_volume_as_part_not_book_title():
    provider = DeterministicLocalProvider()

    claims = provider.claims(
        {
            "subject_ref": "item:gone-with-the-wind-volume-2",
            "title": "03.mp3",
            "folder_path": "\u0410\u0443\u0434\u0438\u043e\u043a\u043d\u0438\u0433\u0438/\u0423\u043d\u0435\u0441\u0451\u043d\u043d\u044b\u0435 \u0432\u0435\u0442\u0440\u043e\u043c/Tom2",
            "media_kind": "audio",
            "metadata": {},
        },
        job_kind="metadata_enrichment",
    )
    values = {claim["field_name"]: claim["value"] for claim in claims}

    assert "artists" not in values
    assert (
        values["album"]
        == "\u0423\u043d\u0435\u0441\u0451\u043d\u043d\u044b\u0435 \u0432\u0435\u0442\u0440\u043e\u043c"
    )
    assert (
        values["audiobook_title"]
        == "\u0423\u043d\u0435\u0441\u0451\u043d\u043d\u044b\u0435 \u0432\u0435\u0442\u0440\u043e\u043c"
    )


def test_path_identity_treats_numbered_part_directory_as_audiobook_part():
    provider = DeterministicLocalProvider()

    claims = provider.claims(
        {
            "subject_ref": "item:ordinary-story-part-2",
            "title": "41-Part 2.mp3",
            "folder_path": (
                "\u0410\u0443\u0434\u0438\u043e\u043a\u043d\u0438\u0433\u0438/\u0413\u043e\u043d\u0447\u0430\u0440\u043e\u0432 \u0418 \u0410/"
                "\u041e\u0431\u044b\u043a\u043d\u043e\u0432\u0435\u043d\u043d\u0430\u044f \u0438\u0441\u0442\u043e\u0440\u0438\u044f(\u0447\u0438\u0442.\u041d.\u0421\u0430\u0432\u0438\u0446\u043a\u0438\u0439)/02-Part 2"
            ),
            "media_kind": "audio",
            "metadata": {},
        },
        job_kind="metadata_enrichment",
    )
    values = {claim["field_name"]: claim["value"] for claim in claims}

    assert values["artists"] == ["\u0413\u043e\u043d\u0447\u0430\u0440\u043e\u0432 \u0418 \u0410"]
    assert values["audiobook_title"] == "\u041e\u0431\u044b\u043a\u043d\u043e\u0432\u0435\u043d\u043d\u0430\u044f \u0438\u0441\u0442\u043e\u0440\u0438\u044f"


def test_audiobook_reclassification_suppresses_stale_musicbrainz_claims(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(
                1,
                "\u0410\u0443\u0434\u0438\u043e\u043a\u043d\u0438\u0433\u0438/\u0413\u043e\u043d\u0447\u0430\u0440\u043e\u0432 \u0418 \u0410/"
                "\u041e\u0431\u044b\u043a\u043d\u043e\u0432\u0435\u043d\u043d\u0430\u044f \u0438\u0441\u0442\u043e\u0440\u0438\u044f(\u0447\u0438\u0442.\u041d.\u0421\u0430\u0432\u0438\u0446\u043a\u0438\u0439)/02-Part 2/41-Part 2.mp3",
            )
        )
    )
    item = catalog.list_items(media_kind="audio", limit=1)["items"][0]
    catalog.record_metadata_claim(
        subject_ref=f"item:{item['id']}",
        field_name="album",
        value="Tom Clancy's Enemy Contact",
        provenance="media_center.musicbrainz.v1",
        confidence=0.95,
    )
    worker = MediaEnrichmentWorker(
        catalog,
        providers=(DeterministicLocalProvider(),),
    )

    for _attempt in range(12):
        worker.run_once()
        details = catalog.item_details(item["id"])
        if details["item"]["metadata"].get("audiobook_title"):
            break

    details = catalog.item_details(item["id"])["item"]
    assert details["metadata"].get("album") == "\u041e\u0431\u044b\u043a\u043d\u043e\u0432\u0435\u043d\u043d\u0430\u044f \u0438\u0441\u0442\u043e\u0440\u0438\u044f"
    assert details["metadata"].get("artists") == ["\u0413\u043e\u043d\u0447\u0430\u0440\u043e\u0432 \u0418 \u0410"]
    assert (
        details["metadata_provenance"].get("album")
        != "media_center.musicbrainz.v1"
    )
    with catalog.repository.connect() as connection:
        rejection = connection.execute(
            "SELECT reason,active FROM metadata_rejections "
            "WHERE subject_ref=? AND provenance=?",
            (f"item:{item['id']}", "media_center.musicbrainz.v1"),
        ).fetchone()
    assert rejection is not None
    assert rejection["reason"] == "media_kind_incompatible"
    assert bool(rejection["active"]) is True


def test_openlibrary_provider_uses_audiobook_path_once_per_book():
    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "docs": [
                    {
                        "key": "/works/OL123W",
                        "title": "\u0421\u0432\u0435\u0442\u043b\u044b\u0439 \u043b\u0438\u043a \u0441\u043c\u0435\u0440\u0442\u0438",
                        "author_name": [
                            "\u0410\u043b\u0435\u043a\u0441\u0430\u043d\u0434\u0440\u0430 \u041c\u0430\u0440\u0438\u043d\u0438\u043d\u0430"
                        ],
                        "first_publish_year": 1996,
                        "cover_i": 12345,
                        "subject": ["Detective fiction"],
                        "edition_count": 12,
                    }
                ]
            }

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    provider = OpenLibraryMetadataProvider(session=session)
    subject = {
        "subject_ref": "item:audiobook-1",
        "collection_id": "book-light-face-of-death",
        "title": "11_08.mp3",
        "folder_path": (
            "!\u0410\u0443\u0434\u0438\u043e\u043a\u043d\u0438\u0433\u0438/\u0410\u0423\u0414\u0418\u041e\u041a\u041d\u0418\u0413\u0410 (\u041a,\u041b,\u041c,\u041d,\u041e,\u041f,\u0420)/"
            "\u041c\u0430\u0440\u0438\u043d\u0438\u043d\u0430 \u0410\u043b\u0435\u043a\u0441\u0430\u043d\u0434\u0440\u0430/\u0421\u0432\u0435\u0442\u043b\u044b\u0439 \u043b\u0438\u043a \u0441\u043c\u0435\u0440\u0442\u0438 "
            "(\u0447\u0438\u0442.\u0415.\u0422\u0435\u0440\u043d\u043e\u0432\u0441\u043a\u0438\u0439)/11"
        ),
        "media_kind": "audio",
        "metadata": {},
    }

    first = provider.claims(subject, job_kind="metadata_enrichment")
    second = provider.claims(subject, job_kind="metadata_enrichment")
    values = {claim["field_name"]: claim["value"] for claim in first}

    assert first == second
    assert len(session.calls) == 1
    assert (
        session.calls[0][1]["params"]["title"]
        == "\u0421\u0432\u0435\u0442\u043b\u044b\u0439 \u043b\u0438\u043a \u0441\u043c\u0435\u0440\u0442\u0438"
    )
    assert (
        session.calls[0][1]["params"]["author"]
        == "\u041c\u0430\u0440\u0438\u043d\u0438\u043d\u0430 \u0410\u043b\u0435\u043a\u0441\u0430\u043d\u0434\u0440\u0430"
    )
    assert (
        values["album"]
        == "\u0421\u0432\u0435\u0442\u043b\u044b\u0439 \u043b\u0438\u043a \u0441\u043c\u0435\u0440\u0442\u0438"
    )
    assert values["artwork_candidates"][0]["url"].startswith(
        "https://covers.openlibrary.org/b/id/12345-L.jpg"
    )
    assert {claim["subject_ref"] for claim in first} == {
        "collection:book-light-face-of-death"
    }
    lookup = values["openlibrary_lookup"]
    assert lookup["state"] == "matched"
    assert (
        provider.accepts(
            subject | {"metadata": {"openlibrary_lookup": lookup}},
            job_kind="metadata_enrichment",
        )
        is False
    )
    assert provider.status()["cache_hit_count"] == 1


def test_openlibrary_provider_persists_collection_no_match_and_skips_numeric_noise():
    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"docs": []}

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    provider = OpenLibraryMetadataProvider(session=session)
    subject = {
        "subject_ref": "item:audiobook-no-match",
        "collection_id": "book-no-match",
        "title": "11_08.mp3",
        "folder_path": "\u0410\u0443\u0434\u0438\u043e\u043a\u043d\u0438\u0433\u0438/\u0410\u0432\u0442\u043e\u0440/\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u0430\u044f \u043a\u043d\u0438\u0433\u0430/11",
        "media_kind": "audio",
        "metadata": {},
    }

    claims = provider.claims(subject, job_kind="metadata_enrichment")

    assert len(session.calls) == 1
    assert claims[0]["subject_ref"] == "collection:book-no-match"
    assert claims[0]["field_name"] == "openlibrary_lookup"
    assert claims[0]["value"]["state"] == "no_match"
    assert (
        provider.accepts(
            subject | {"metadata": {"openlibrary_lookup": claims[0]["value"]}},
            job_kind="metadata_enrichment",
        )
        is False
    )
    assert (
        provider.accepts(
            {
                "subject_ref": "item:numeric-audiobook",
                "title": "04.mp3",
                "folder_path": "\u0410\u0443\u0434\u0438\u043e\u043a\u043d\u0438\u0433\u0438/4/4 16/04",
                "media_kind": "audio",
                "metadata": {},
            },
            job_kind="metadata_enrichment",
        )
        is False
    )


def test_musicbrainz_tls_failure_opens_a_bounded_circuit_breaker():
    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, _url, **_kwargs):
            self.calls += 1
            raise requests.exceptions.SSLError("fixture handshake failed")

    session = Session()
    provider = MusicBrainzMetadataProvider(session=session)
    subject = {
        "subject_ref": "item:audio-a",
        "title": "Bohemian Rhapsody.mp3",
        "media_kind": "audio",
        "metadata": {"artists": ["Queen"]},
    }

    with pytest.raises(MetadataProviderError) as first:
        provider.claims(subject, job_kind="metadata_enrichment")
    with pytest.raises(MetadataProviderError) as second:
        provider.claims(subject, job_kind="metadata_enrichment")

    assert first.value.code == "musicbrainz_request_failed"
    assert second.value.code == "musicbrainz_temporarily_unavailable"
    assert session.calls == 1
    assert provider.status()["request_count"] == 1
    assert provider.status()["state"] == "degraded"
    assert provider.status()["last_error"] == "musicbrainz_tls_handshake_failed"
    assert provider.status()["retry_after_seconds"] > 0


def test_external_metadata_providers_follow_managed_settings():
    assert [provider.provider_id for provider in default_metadata_providers()] == [
        "media_center.deterministic_local.v1"
    ]

    settings = {
        "external_enabled": True,
        "musicbrainz_enabled": True,
        "tmdb_enabled": True,
        "locale": "ru-RU",
    }
    assert [
        provider.provider_id
        for provider in default_metadata_providers(settings, tmdb_credential="token")
    ] == [
        "media_center.deterministic_local.v1",
        "media_center.tmdb.v1",
        "media_center.musicbrainz.v1",
        "media_center.openlibrary.v1",
    ]
    missing_credential = metadata_provider_configuration(
        settings, tmdb_credential_configured=False
    )[1]
    assert missing_credential["enabled"] is True
    assert missing_credential["ready"] is False
    assert missing_credential["state"] == "credentials_missing"


def test_metadata_settings_are_durable_and_default_to_managed_enrichment(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())

    initial = catalog.metadata_settings()["settings"]
    assert initial["external_enabled"] is True
    assert initial["musicbrainz_enabled"] is True
    assert initial["tmdb_enabled"] is True

    updated = catalog.set_metadata_settings(
        {"musicbrainz_enabled": False, "locale": "en-US"}
    )
    assert updated["changed"] is True
    restored = MediaCatalogCoordinator(MediaCenterRepository()).metadata_settings()[
        "settings"
    ]
    assert restored["musicbrainz_enabled"] is False
    assert restored["locale"] == "en-US"
    assert restored["revision"] == 1


def test_metadata_credential_revision_requeues_current_catalog_once(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repository = MediaCenterRepository()
    catalog = MediaCatalogCoordinator(repository)
    catalog.apply_agent_page(
        _agent_page(_agent_delta(1, "Movies/Movie.2024.mp4", kind="video"))
    )

    updated = catalog.set_metadata_settings({}, force_revision=True)
    requeued = catalog.requeue_metadata_enrichment()

    assert updated["changed"] is True
    assert updated["settings"]["revision"] == 1
    assert requeued["queued_count"] == 1
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT status,COUNT(*) AS count FROM media_background_jobs "
            "WHERE kind='metadata_enrichment' GROUP BY status"
        ).fetchall()
    assert {row["status"]: int(row["count"]) for row in rows} == {"queued": 1}


def test_large_catalog_background_jobs_stay_inside_admission_windows(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    monkeypatch.setenv("MEDIA_CENTER_METADATA_ENRICHMENT_QUEUE_WINDOW", "64")
    monkeypatch.setenv("MEDIA_CENTER_FINGERPRINT_QUEUE_WINDOW", "32")
    monkeypatch.setenv("MEDIA_CENTER_EMBEDDING_QUEUE_WINDOW", "32")
    catalog = MediaCatalogCoordinator(MediaCenterRepository())

    for start in range(1, 1001, 100):
        catalog.apply_agent_page(
            _agent_page(
                *(
                    _agent_delta(index, f"Music/Large/{index:05d}.mp3")
                    for index in range(start, start + 100)
                )
            )
        )

    counts = catalog.background_job_counts_by_kind()
    assert catalog.repository.compact_summary()["total_count"] == 1000
    assert counts["metadata_enrichment"]["queued"] <= 64
    assert counts["fingerprint"]["queued"] <= 32
    assert counts["embedding"]["queued"] <= 32


def test_background_queue_compaction_preserves_window_and_cursor_refill(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    monkeypatch.setenv("MEDIA_CENTER_METADATA_ENRICHMENT_QUEUE_WINDOW", "32")
    repository = MediaCenterRepository()
    catalog = MediaCatalogCoordinator(repository)
    catalog.apply_agent_page(
        _agent_page(
            *(
                _agent_delta(index, f"Movies/{index:04d}.mp4", kind="video")
                for index in range(1, 101)
            )
        )
    )
    with repository.connect() as connection:
        now = catalog_module.now_iso()
        connection.executemany(
            "INSERT INTO media_background_jobs("
            "id,kind,subject_ref,status,priority,created_at,updated_at"
            ") VALUES (?, 'metadata_enrichment', ?, 'queued', 900, ?, ?)",
            (
                (f"legacy-{index}", f"item:legacy-{index}", now, now)
                for index in range(100)
            ),
        )
        connection.commit()

    compacted = catalog.compact_background_job_queue(batch_size=1000)
    with repository.connect() as connection:
        queued = int(
            connection.execute(
                "SELECT COUNT(*) FROM media_background_jobs "
                "WHERE kind='metadata_enrichment' AND status='queued'"
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE media_background_jobs SET status='completed' "
            "WHERE kind='metadata_enrichment' AND status='queued'"
        )
        connection.commit()
    refilled = catalog.refill_background_job_windows(force=True)

    assert compacted["complete"] is True
    assert queued == 32
    assert refilled["kinds"]["metadata_enrichment"]["admitted"] > 0
    assert (
        catalog.background_job_counts_by_kind()["metadata_enrichment"]["queued"] <= 32
    )


def test_large_legacy_queue_uses_bounded_counts_and_resumable_reset(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    repository = MediaCenterRepository()
    catalog = MediaCatalogCoordinator(repository)
    with repository.connect() as connection:
        now = catalog_module.now_iso()
        connection.executemany(
            "INSERT INTO media_background_jobs("
            "id,kind,subject_ref,status,priority,created_at,updated_at"
            ") VALUES (?, 'metadata_enrichment', ?, 'queued', 900, ?, ?)",
            (
                (f"legacy-{index}", f"item:legacy-{index}", now, now)
                for index in range(6001)
            ),
        )
        connection.execute("DROP INDEX idx_media_center_background_kind_status")
        connection.commit()

    state = catalog.operation_state(limit=1)
    diagnostics = catalog.diagnostics()
    compacted = catalog.compact_background_job_queue(batch_size=100)

    assert state["counts"]["queued"] == 256
    assert state["count_state"] | {"index": {}} == {
        "complete": False,
        "scope": "active_sample",
        "lower_bound": True,
        "sample_limit": 256,
        "by_kind": False,
        "index": {},
    }
    assert diagnostics["background_job_count_state"]["scope"] == "active_sample"
    assert diagnostics["background_jobs"]["queued"] == 256
    assert compacted["complete"] is False
    assert compacted["admission_paused"] is True
    assert compacted["state"]["scanned"] == 100
    assert compacted["removed"]["legacy_queued"] == 100


def test_enrichment_maintenance_runs_one_bounded_lane_per_tick():
    calls = []

    class Coordinator:
        def compact_search_index_batch(self, *, limit):
            calls.append(("search", limit))
            return {"complete": False}

        def compact_storage_batch(self, *, limit):
            calls.append(("storage", limit))
            return {"complete": False}

        def compact_background_job_queue(self, *, batch_size):
            calls.append(("queue", batch_size))
            return {"complete": True, "admission_paused": False}

    worker = MediaEnrichmentWorker(Coordinator())

    worker._run_maintenance_lane()
    worker._run_maintenance_lane()
    worker._run_maintenance_lane()

    assert calls == [("queue", 1000), ("search", 100), ("storage", 1)]


def test_enrichment_worker_runs_all_eligible_providers(monkeypatch, tmp_path):
    class ExternalProvider:
        provider_id = "test.external.v1"
        supported_jobs = frozenset({"metadata_enrichment"})

        @staticmethod
        def claims(subject, *, job_kind):
            assert job_kind == "metadata_enrichment"
            return [
                {
                    "subject_ref": subject["subject_ref"],
                    "field_name": "external_id",
                    "value": "external-1",
                    "confidence": 0.8,
                }
            ]

        @staticmethod
        def status():
            return {
                "provider_id": "test.external.v1",
                "enabled": True,
                "state": "ready",
            }

    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Movies/Movie.mp4")))
    subject_ref = catalog.operations(limit=10)["items"][0]["subject_ref"]
    worker = MediaEnrichmentWorker(
        catalog,
        providers=(
            default_metadata_providers()[0],
            ExternalProvider(),
        ),
    )

    result = worker.run_once()
    claims = catalog.metadata_claims(subject_ref, limit=30)
    operation = catalog.operations(limit=10)["items"][0]

    assert result["status"] == "completed"
    assert operation["provider_id"] == (
        "media_center.deterministic_local.v1,test.external.v1"
    )
    assert {item["provenance"] for item in claims["items"]} == {
        "media_center.deterministic_local.v1",
        "test.external.v1",
    }


def test_enrichment_worker_routes_external_providers_by_media_kind(
    monkeypatch, tmp_path
):
    class VideoProvider:
        provider_id = "test.video.v1"
        supported_jobs = frozenset({"metadata_enrichment"})

        @staticmethod
        def accepts(subject, *, job_kind):
            return subject["media_kind"] == "video"

        @staticmethod
        def claims(subject, *, job_kind):
            raise AssertionError("audio job must not reach video provider")

    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/Track.mp3")))
    worker = MediaEnrichmentWorker(
        catalog,
        providers=(default_metadata_providers()[0], VideoProvider()),
    )

    result = worker.run_once()
    operation = catalog.operations(limit=1)["items"][0]

    assert result["status"] == "completed"
    assert operation["provider_id"] == "media_center.deterministic_local.v1"


def test_storage_maintenance_fence_pauses_enrichment_and_agent_sync(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/Track.mp3")))
    worker = MediaEnrichmentWorker(catalog)
    catalog.set_storage_maintenance(True)

    assert worker.run_once() is None
    assert catalog.background_job_counts()["queued"] > 0
    assert main._run_agent_sync(catalog)["mode"] == "storage_maintenance"

    catalog.set_storage_maintenance(False)
    assert worker.run_once() is not None


def test_enrichment_worker_coalesces_publication_and_exposes_pacing(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
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


def test_enrichment_worker_publishes_full_snapshot_only_when_queue_settles(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(_agent_page(_agent_delta(1, "Music/one.mp3")))
    progress = []
    settled = []
    worker = MediaEnrichmentWorker(
        catalog,
        publish=lambda: progress.append("progress"),
        publish_settled=lambda: settled.append("settled"),
        poll_seconds=0.2,
        work_interval_seconds=0.02,
        publish_interval_seconds=300,
    )

    worker.ensure_started()
    deadline = time.monotonic() + 5
    while not settled and time.monotonic() < deadline:
        time.sleep(0.02)
    worker.dispose(timeout=2)

    assert progress
    assert settled == ["settled"]


def test_enrichment_start_defers_maintenance_to_worker_thread() -> None:
    recovered = threading.Event()
    release = threading.Event()

    class Coordinator:
        def recover_stale_background_jobs(self):
            recovered.set()
            release.wait(2.0)
            return {"ok": True}

        def prune_terminal_background_jobs(self, **_kwargs):
            pytest.fail("startup must not prune terminal jobs")

        def claim_background_job(self):
            return None

    worker = MediaEnrichmentWorker(Coordinator(), providers=(), poll_seconds=30)

    assert worker.ensure_started() is True
    assert recovered.wait(1.0) is True
    release.set()
    assert worker.dispose(timeout=2.0)["stopped"] is True


def test_enrichment_loop_retries_transient_repository_lock() -> None:
    retried = threading.Event()
    calls = 0

    class Coordinator:
        def recover_stale_background_jobs(self):
            return {"ok": True}

        def claim_background_job(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise catalog_module.sqlite3.OperationalError("database is locked")
            retried.set()
            return None

    worker = MediaEnrichmentWorker(Coordinator(), providers=(), poll_seconds=0.2)

    assert worker.ensure_started() is True
    assert retried.wait(2.0) is True
    status = worker.status()
    assert status["state"] == "running"
    assert status["loop_failure_count"] == 1
    assert worker.dispose(timeout=2.0)["stopped"] is True


def test_background_job_indexes_recovery_and_exact_active_count(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
    catalog = MediaCatalogCoordinator(MediaCenterRepository())
    catalog.apply_agent_page(
        _agent_page(
            _agent_delta(1, "Music/one.mp3"),
            _agent_delta(2, "Music/two.mp3"),
        )
    )
    first = catalog.claim_background_job()
    second = catalog.claim_background_job()
    assert first is not None and second is not None
    with catalog.repository.connect() as connection:
        connection.execute(
            "UPDATE media_background_jobs SET updated_at='2000-01-01T00:00:00+00:00' "
            "WHERE id IN (?, ?)",
            (first["id"], second["id"]),
        )
        connection.execute(
            "UPDATE media_background_jobs SET attempts=3 WHERE id=?",
            (second["id"],),
        )
        connection.commit()

    recovered = catalog.recover_stale_background_jobs(stale_seconds=60)
    state = catalog.operation_state(limit=1)
    with catalog.repository.connect() as connection:
        indexes = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA index_list(media_background_jobs)"
            ).fetchall()
        }
        claim_plan = " ".join(
            str(row["detail"])
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN SELECT * FROM media_background_jobs
                WHERE status='queued' AND attempts<3
                ORDER BY priority,created_at LIMIT 1
                """
            ).fetchall()
        )
        recent_plan = " ".join(
            str(row["detail"])
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN SELECT * FROM media_background_jobs
                ORDER BY updated_at DESC LIMIT 30
                """
            ).fetchall()
        )
        operations_join_plan = " ".join(
            str(row["detail"])
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT j.id,c.title
                FROM media_background_jobs j
                LEFT JOIN catalog_items c ON c.id=substr(j.subject_ref,6)
                ORDER BY j.updated_at DESC,j.id DESC LIMIT 30
                """
            ).fetchall()
        )
        migration_plan = " ".join(
            str(row["detail"])
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN UPDATE media_background_jobs
                SET priority=CASE WHEN EXISTS (
                    SELECT 1 FROM catalog_items c
                    WHERE c.id=substr(media_background_jobs.subject_ref,6)
                      AND c.media_kind='audio'
                ) THEN 150 ELSE 200 END
                WHERE kind='metadata_enrichment' AND status='queued'
                """
            ).fetchall()
        )

    assert recovered == {
        "ok": True,
        "retried": 1,
        "failed": 1,
        "stale_seconds": 60.0,
    }
    assert state["active_count"] == state["counts"]["queued"]
    assert {
        "idx_media_center_background_claim",
        "idx_media_center_background_kind_status",
        "idx_media_center_background_recent",
    } <= indexes
    assert "idx_media_center_background_claim" in claim_plan
    assert "idx_media_center_background_recent" in recent_plan
    assert "sqlite_autoindex_catalog_items_1" in operations_join_plan
    assert "sqlite_autoindex_catalog_items_1" in migration_plan
    assert state["counts_by_kind"]["metadata_enrichment"]["queued"] >= 1
    assert state["storage"]["allocated_bytes"] >= state["storage"]["db_bytes"]


def test_library_stream_snapshot_is_compact(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CENTER_DB_PATH", str(tmp_path / "media_center.sqlite3"))
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

    result = catalog.discovery_search("Sherlok Holms", profile_id="default", limit=5)

    assert result["items"][0]["name"] == "01.mp3"
    assert result["items"][0]["deep_match"]["stage"] == ("coordinator_local_discovery")
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
