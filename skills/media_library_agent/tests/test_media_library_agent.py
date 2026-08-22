from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import sys
import time
import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from media_library_agent.repository import MediaLibraryAgentRepository  # noqa: E402
from media_library_agent.rendition import artwork_plan, rendition_plan  # noqa: E402
import media_library_agent.topology as topology_module  # noqa: E402
from media_library_agent.topology import LibraryAgentTopology  # noqa: E402
from media_library_agent.worker import MediaLibraryAgentWorker  # noqa: E402


_HANDLER_SPEC = importlib.util.spec_from_file_location(
    "media_library_agent_handlers_main", SKILL_ROOT / "handlers" / "main.py"
)
assert _HANDLER_SPEC and _HANDLER_SPEC.loader
main = importlib.util.module_from_spec(_HANDLER_SPEC)
_HANDLER_SPEC.loader.exec_module(main)
_WORKER_STATE_TIMEOUT_SECONDS = 20.0


def test_agent_declares_core_managed_membership_and_health_projection():
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))
    membership = manifest["service"]["membership"]
    health = main.status()

    assert membership["enabled"] is True
    assert membership["group_id"] == "media-library-home"
    assert membership["lease_seconds"] == 600
    assert health["distributed"]["health"]["ready"] is True
    assert health["distributed"]["pressure"]["state"] == "normal"


def test_delta_pages_include_compact_authoritative_library_state(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    repository = MediaLibraryAgentRepository(
        tmp_path / "delta-state.sqlite3", node_id="node-a"
    )
    repository.add_root(str(library))

    page = repository.pull_deltas(limit=10)

    assert page["library_state"] == {
        "root_count": 1,
        "source_count": 0,
        "available_count": 0,
        "active_job_count": 0,
        "failed_job_count": 0,
    }


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_LIBRARY_AGENT_DB_PATH", str(tmp_path / "agent.sqlite3"))
    monkeypatch.setenv(
        "ADAOS_MEDIA_REFERENCE_DB_PATH", str(tmp_path / "references.sqlite3")
    )
    monkeypatch.setenv("ADAOS_NODE_ID", "node-test")
    monkeypatch.setenv("MEDIA_LIBRARY_AGENT_EMBEDDED_WORKER", "1")
    monkeypatch.delenv("ADAOS_RUNTIME_PORT", raising=False)
    monkeypatch.delenv("ADAOS_SERVICE_SKILL", raising=False)
    yield
    try:
        main.dispose()
    except Exception:
        pass


def _wait(job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = main.scan_status(job_id=job_id)
        job = payload.get("job") or {}
        if job.get("status") in {"completed", "failed", "canceled"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"scan job {job_id} did not finish")


def test_progress_publication_is_nonblocking_bounded_and_coalesced(monkeypatch):
    main._stop_progress_publisher(timeout=1.0)
    started = threading.Event()
    release = threading.Event()
    delivered = []

    def blocking_delivery(payload, webspace_id):
        started.set()
        release.wait(2.0)
        delivered.append((dict(payload), webspace_id))

    monkeypatch.setattr(main, "_deliver_progress", blocking_delivery)
    main._publish_progress({"job_id": "scan-a", "sequence": 1}, "desktop")
    assert started.wait(1.0)

    started_at = time.monotonic()
    for sequence in range(2, 102):
        main._publish_progress({"job_id": "scan-a", "sequence": sequence}, "desktop")
    enqueue_duration = time.monotonic() - started_at
    status = main._progress_publisher_status()

    assert enqueue_duration < 0.25
    assert status["mode"] == "bounded_coalescing"
    assert status["pending_count"] == 1
    assert status["max_pending"] == 64
    assert status["inflight"] is True

    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if len(delivered) == 2:
            break
        time.sleep(0.01)
    main._stop_progress_publisher(timeout=1.0)

    assert [item[0]["sequence"] for item in delivered] == [1, 101]


def test_agent_reports_local_topology_without_writing_control_plane(tmp_path):
    source = (SKILL_ROOT / "media_library_agent" / "topology.py").read_text(
        encoding="utf-8"
    )
    assert "from adaos.sdk import distributed" in source
    assert "adaos.services" not in source
    library = tmp_path / "library"
    library.mkdir()
    repository = MediaLibraryAgentRepository(
        tmp_path / "agent.sqlite3", node_id="node-a"
    )
    root = repository.add_root(str(library))["root"]
    partition = {
        "schema": "adaos.distributed.partition.v1",
        "partition_id": f"media-files:{root['id']}",
        "dataset_id": "media-files",
        "selector": {"root_id": root["id"]},
        "desired_replicas": 1,
        "topology_generation": 1,
        "authority_lease_id": "lease-root-a",
        "authority_epoch": 1,
        "checkpoint": "untrusted",
        "status": "ready",
        "revision": 1,
    }
    replica = {
        "schema": "adaos.distributed.replica.v1",
        "replica_id": "replica-root-a",
        "partition_id": partition["partition_id"],
        "instance_id": "media-agent-node-a",
        "node_id": "node-a",
        "role": "authority",
        "lifecycle": "ready",
        "content_state": "non_empty",
        "authority_epoch": 1,
        "checkpoint": "untrusted",
        "source_ref": "untrusted",
        "freshness_seconds": 99,
        "item_count": 99,
        "byte_count": 99,
        "observed_at": "2026-08-19T00:00:00+00:00",
        "revision": 1,
    }
    result = LibraryAgentTopology().observe(repository, partition, replica)
    assert result["ok"] is True
    assert result["replica"]["replica_id"] == topology_module.stable_id(
        "replica",
        partition["partition_id"],
        replica["instance_id"],
        size=28,
    )
    assert result["replica"]["node_id"] == "node-a"
    assert result["replica"]["source_ref"] == f"media-root:{root['id']}"
    assert result["replica"]["checkpoint"].startswith("root:1:source:")
    assert result["external_media_copied"] is False


def test_agent_derives_catalog_witness_instead_of_trusting_caller(tmp_path):
    repository = MediaLibraryAgentRepository(
        tmp_path / "agent.sqlite3", node_id="node-a"
    )
    partition = {
        "schema": "adaos.distributed.partition.v1",
        "partition_id": "media-catalog-authority:home",
        "dataset_id": "media-catalog-authority",
        "selector": {"shard": "home"},
        "desired_replicas": 2,
        "topology_generation": 1,
        "authority_lease_id": None,
        "authority_epoch": 0,
        "checkpoint": "caller-controlled",
        "status": "ready",
        "revision": 1,
    }
    replica = {
        "schema": "adaos.distributed.replica.v1",
        "replica_id": "caller-controlled",
        "partition_id": partition["partition_id"],
        "instance_id": "media-agent-node-a",
        "node_id": "node-a",
        "role": "follower",
        "lifecycle": "ready",
        "content_state": "non_empty",
        "authority_epoch": 0,
        "checkpoint": "caller-controlled",
        "source_ref": "caller-controlled",
        "freshness_seconds": 99,
        "item_count": 99,
        "byte_count": 99,
        "observed_at": "2026-08-19T00:00:00+00:00",
        "revision": 1,
    }

    result = LibraryAgentTopology().observe(repository, partition, replica)

    assert result["ok"] is True
    assert result["partition"]["checkpoint"] == "caller-controlled"
    assert result["replica"]["checkpoint"] == "catalog:0"
    assert result["replica"]["source_ref"] == "catalog-state:home"
    assert result["replica"]["content_state"] == "empty"
    assert result["replica"]["item_count"] == 0
    assert result["replica"]["byte_count"] == 0
    assert result["replica"]["freshness_seconds"] == 0
    assert result["external_media_copied"] is False


def test_follower_reports_persisted_replica_without_rewriting_partition(tmp_path):
    repository = MediaLibraryAgentRepository(
        tmp_path / "agent.sqlite3", node_id="node-b"
    )
    repository.save_topology_replica_snapshot(
        "media-catalog-authority:home",
        checkpoint="catalog:42",
        content_witness="sha256:" + "b" * 64,
        payload_digest="sha256:" + "c" * 64,
        item_count=42,
        byte_count=2048,
        payload={"schema": "adaos.media_library.catalog_snapshot.v1", "items": []},
    )
    partition = {
        "schema": "adaos.distributed.partition.v1",
        "partition_id": "media-catalog-authority:home",
        "dataset_id": "media-catalog-authority",
        "selector": {"shard": "home"},
        "desired_replicas": 2,
        "topology_generation": 4,
        "authority_lease_id": "authority-home-7",
        "authority_epoch": 7,
        "checkpoint": "catalog:42",
        "status": "ready",
        "revision": 9,
    }
    replica = {
        "schema": "adaos.distributed.replica.v1",
        "replica_id": "replica-home-node-b",
        "partition_id": partition["partition_id"],
        "instance_id": "media-agent-node-b",
        "node_id": "node-b",
        "role": "follower",
        "lifecycle": "ready",
        "content_state": "unknown",
        "authority_epoch": 7,
        "checkpoint": None,
        "source_ref": None,
        "freshness_seconds": None,
        "item_count": None,
        "byte_count": None,
        "observed_at": "2026-08-19T00:00:00+00:00",
        "revision": 1,
    }

    result = LibraryAgentTopology().observe(repository, partition, replica)

    assert result["partition"]["checkpoint"] == "catalog:42"
    assert result["replica"]["checkpoint"] == "catalog:42"
    assert result["replica"]["content_state"] == "non_empty"
    assert result["replica"]["item_count"] == 42
    assert result["replica"]["byte_count"] == 2048


def test_authority_recovers_partition_witness_from_persisted_replica(tmp_path):
    repository = MediaLibraryAgentRepository(
        tmp_path / "agent.sqlite3", node_id="node-b"
    )
    repository.save_topology_replica_snapshot(
        "media-catalog-authority:home",
        checkpoint="catalog:42",
        content_witness="sha256:" + "b" * 64,
        payload_digest="sha256:" + "c" * 64,
        item_count=42,
        byte_count=2048,
        payload={"schema": "adaos.media_library.catalog_snapshot.v1", "items": []},
    )
    partition = {
        "schema": "adaos.distributed.partition.v1",
        "partition_id": "media-catalog-authority:home",
        "dataset_id": "media-catalog-authority",
        "selector": {"shard": "home"},
        "desired_replicas": 2,
        "topology_generation": 4,
        "authority_lease_id": "authority-home-8",
        "authority_epoch": 8,
        "checkpoint": "catalog:0",
        "status": "moving",
        "revision": 10,
    }
    replica = {
        "schema": "adaos.distributed.replica.v1",
        "replica_id": "replica-home-node-b",
        "partition_id": partition["partition_id"],
        "instance_id": "media-agent-node-b",
        "node_id": "node-b",
        "role": "authority",
        "lifecycle": "ready",
        "content_state": "unknown",
        "authority_epoch": 8,
        "checkpoint": None,
        "source_ref": None,
        "freshness_seconds": None,
        "item_count": None,
        "byte_count": None,
        "observed_at": "2026-08-19T00:00:00+00:00",
        "revision": 1,
    }

    result = LibraryAgentTopology().observe(repository, partition, replica)

    assert result["partition"]["checkpoint"] == "catalog:42"
    assert result["replica"]["checkpoint"] == "catalog:42"
    assert result["replica"]["role"] == "authority"
    assert result["replica"]["authority_epoch"] == 8


def test_repository_migrates_legacy_local_identity_once(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    database = tmp_path / "identity.sqlite3"
    legacy = MediaLibraryAgentRepository(database, node_id="local")
    root_id = legacy.add_root(str(library))["root"]["id"]
    operation, legacy_source = legacy.upsert_source(
        {
            "root_id": root_id,
            "relative_path": "Album/01.mp3",
            "folder_path": "Album",
            "name": "01.mp3",
            "media_kind": "audio",
            "mime_type": "audio/mpeg",
            "size_bytes": 5,
            "modified_ns": 1,
            "inode": 1,
            "fingerprint": "legacy-fingerprint",
            "resource_id": "media-ref",
            "descriptor": {"node_id": "local"},
            "metadata": {"agent": {"node_id": "local", "agent_id": legacy.agent_id}},
        },
        job_id="legacy-job",
    )
    assert operation == "added"

    migrated = MediaLibraryAgentRepository(database, node_id="node-a")

    assert migrated.list_roots()["items"][0]["node_id"] == "node-a"
    assert migrated.list_roots()["items"][0]["id"] == root_id
    source = migrated.get_source(legacy_source["id"])
    assert source is not None
    assert source["node_id"] == "node-a"
    delta = migrated.pull_deltas(limit=10)["items"][0]
    assert delta["node_id"] == "node-a"
    assert delta["agent_id"] == migrated.agent_id
    assert delta["source"]["node_id"] == "node-a"
    assert delta["source"]["descriptor"]["node_id"] == "node-a"
    assert delta["source"]["metadata"]["agent"] == {
        "node_id": "node-a",
        "agent_id": migrated.agent_id,
    }
    with pytest.raises(ValueError, match="repository_node_identity_mismatch"):
        MediaLibraryAgentRepository(database, node_id="node-b")


def test_repository_schema_initialization_is_concurrency_safe(tmp_path):
    database = tmp_path / "concurrent-schema.sqlite3"

    def initialize(_: int) -> dict:
        return MediaLibraryAgentRepository(database, node_id="node-a").ensure_schema()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(initialize, range(16)))

    assert all(result["ok"] is True for result in results)
    assert {result["schema_revision"] for result in results} == {"3"}
    with sqlite3.connect(database) as connection:
        rows = dict(connection.execute("SELECT key,value FROM agent_meta").fetchall())
        indexes = {
            str(row[1])
            for row in connection.execute("PRAGMA index_list(sources)").fetchall()
        }
    assert rows["node_id"] == "node-a"
    assert rows["database_schema_revision"] == "3"
    assert "idx_media_agent_sources_folder" in indexes


def test_current_schema_check_does_not_require_sqlite_writer_lock(tmp_path):
    database = tmp_path / "current-schema.sqlite3"
    repository = MediaLibraryAgentRepository(database, node_id="node-a")
    blocker = sqlite3.connect(database, timeout=1)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        result = repository.ensure_schema()
    finally:
        blocker.rollback()
        blocker.close()

    assert result["ok"] is True
    assert result["schema_revision"] == "3"
    assert time.monotonic() - started < 1.0


def test_repository_context_closes_sqlite_connection(tmp_path):
    repository = MediaLibraryAgentRepository(
        tmp_path / "closing-connection.sqlite3", node_id="node-a"
    )
    connection = repository.connect()

    with connection as managed:
        assert managed.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_runtime_prefers_sdk_node_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_LIBRARY_AGENT_DB_PATH", str(tmp_path / "sdk.sqlite3"))
    monkeypatch.setattr(
        main,
        "runtime_identity",
        lambda: {"node": {"node_id": "node-from-sdk"}},
    )

    repository, _worker = main._runtime()

    assert repository.node_id == "node-from-sdk"


def test_import_is_async_reference_only_and_excludes_images(tmp_path):
    library = tmp_path / "library"
    album = library / "Artist" / "Album"
    album.mkdir(parents=True)
    song = album / "01.mp3"
    poster = album / "cover.jpg"
    song.write_bytes(b"audio-data")
    poster.write_bytes(b"image-data")

    result = main.import_folder(path=str(library), include_images=False)

    assert result["ok"] is True
    assert result["asynchronous"] is True
    assert result["storage"] == {
        "mode": "external_reference",
        "media_bytes_copied": False,
    }
    job = _wait(result["job"]["id"])
    assert job["status"] == "completed"
    assert job["progress"]["processed_count"] == 1
    deltas = main.pull_deltas(limit=10)
    assert [item["source"]["name"] for item in deltas["items"]] == ["01.mp3"]
    assert deltas["items"][0]["source"]["metadata"]["folder_segments"] == [
        "Artist",
        "Album",
    ]
    assert deltas["items"][0]["source"]["metadata"]["technical"]["probe"] == "basic"
    assert (
        deltas["items"][0]["source"]["descriptor"]["metadata"]["storage_mode"]
        == "reference"
    )
    assert song.read_bytes() == b"audio-data"
    assert poster.read_bytes() == b"image-data"
    assert list(tmp_path.rglob("*.mp3")) == [song]


def test_incremental_scan_emits_only_changes_and_tombstones(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    song = library / "track.mp3"
    song.write_bytes(b"v1")
    imported = main.import_folder(path=str(library))
    _wait(imported["job"]["id"])
    first = main.pull_deltas(limit=10)
    assert [item["operation"] for item in first["items"]] == ["added"]

    unchanged = main.start_scan(root_id=imported["root"]["id"])
    _wait(unchanged["job"]["id"])
    replay = main.pull_deltas(cursor=first["next_cursor"], limit=10)
    assert replay["items"] == []

    time.sleep(0.01)
    song.write_bytes(b"version-two")
    changed = main.start_scan(root_id=imported["root"]["id"])
    _wait(changed["job"]["id"])
    update = main.pull_deltas(cursor=first["next_cursor"], limit=10)
    assert [item["operation"] for item in update["items"]] == ["updated"]
    assert update["items"][0]["source_revision"] == 2

    song.unlink()
    removed = main.start_scan(root_id=imported["root"]["id"], mode="reconcile")
    _wait(removed["job"]["id"])
    tombstone = main.pull_deltas(cursor=update["next_cursor"], limit=10)
    assert [item["operation"] for item in tombstone["items"]] == ["removed"]
    assert tombstone["items"][0]["source"]["present"] is False


def test_disabling_root_tombstones_sources_without_deleting_files(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    first_song = library / "01.mp3"
    second_song = library / "02.mp3"
    first_song.write_bytes(b"first")
    second_song.write_bytes(b"second")

    imported = main.import_folder(path=str(library))
    _wait(imported["job"]["id"])
    added = main.pull_deltas(limit=10)
    assert [item["operation"] for item in added["items"]] == ["added", "added"]

    disabled = main.remove_root(root_id=imported["root"]["id"])

    assert disabled["ok"] is True
    assert disabled["disabled"] is True
    assert disabled["deduplicated"] is False
    assert disabled["tombstoned_source_count"] == 2
    assert disabled["source_files_deleted"] is False
    assert main.list_roots()["items"] == []
    assert len(main.list_roots(include_disabled=True)["items"]) == 1
    tombstones = main.pull_deltas(cursor=added["next_cursor"], limit=10)
    assert [item["operation"] for item in tombstones["items"]] == [
        "removed",
        "removed",
    ]
    assert all(item["source"]["present"] is False for item in tombstones["items"])
    assert first_song.read_bytes() == b"first"
    assert second_song.read_bytes() == b"second"

    repeated = main.remove_root(root_id=imported["root"]["id"])
    assert repeated["ok"] is True
    assert repeated["deduplicated"] is True
    assert repeated["tombstoned_source_count"] == 0
    assert main.pull_deltas(cursor=tombstones["next_cursor"], limit=10)["items"] == []


def test_duplicate_scan_request_returns_active_job(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    repository = MediaLibraryAgentRepository(
        tmp_path / "direct.sqlite3", node_id="node-a"
    )
    root = repository.add_root(str(library))["root"]

    first = repository.create_job(root["id"])
    second = repository.create_job(root["id"])

    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["deduplicated"] is True
    assert second["job"]["id"] == first["job"]["id"]


def test_playback_pressure_pauses_worker_until_released(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "track.mp3").write_bytes(b"audio")
    repository = MediaLibraryAgentRepository(
        tmp_path / "pressure.sqlite3", node_id="node-a"
    )
    root = repository.add_root(str(library))["root"]
    job = repository.create_job(root["id"])["job"]

    def register(path, _root, metadata):
        return {
            "id": f"ref-{path.name}",
            "resource_id": f"ref-{path.name}",
            "name": path.name,
            "mime_type": "audio/mpeg",
            "source_path": str(path),
            "metadata": dict(metadata),
        }

    worker = MediaLibraryAgentWorker(repository, register=register, poll_seconds=0.01)
    worker.set_resource_pressure("playback")
    worker.ensure_started()
    deadline = time.monotonic() + _WORKER_STATE_TIMEOUT_SECONDS
    while (
        time.monotonic() < deadline
        and repository.get_job(job["id"])["status"] != "waiting_resources"
    ):
        time.sleep(0.01)
    assert repository.get_job(job["id"])["status"] == "waiting_resources"
    worker.set_resource_pressure("normal")
    deadline = time.monotonic() + _WORKER_STATE_TIMEOUT_SECONDS
    while (
        time.monotonic() < deadline
        and repository.get_job(job["id"])["status"] != "completed"
    ):
        time.sleep(0.01)
    assert repository.get_job(job["id"])["status"] == "completed"
    worker.dispose()


def test_resource_pressure_is_shared_across_agent_processes(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "track.mp3").write_bytes(b"audio")
    db_path = tmp_path / "shared-pressure.sqlite3"
    worker_repository = MediaLibraryAgentRepository(db_path, node_id="node-a")
    controller_repository = MediaLibraryAgentRepository(db_path, node_id="node-a")
    root = worker_repository.add_root(str(library))["root"]
    job = worker_repository.create_job(root["id"])["job"]
    worker = MediaLibraryAgentWorker(worker_repository, poll_seconds=0.01)

    controller_repository.set_resource_pressure("playback")
    worker.ensure_started()
    deadline = time.monotonic() + _WORKER_STATE_TIMEOUT_SECONDS
    while (
        time.monotonic() < deadline
        and worker_repository.get_job(job["id"])["status"] != "waiting_resources"
    ):
        time.sleep(0.01)
    assert worker_repository.get_job(job["id"])["status"] == "waiting_resources"

    controller_repository.set_resource_pressure("normal")
    deadline = time.monotonic() + _WORKER_STATE_TIMEOUT_SECONDS
    while (
        time.monotonic() < deadline
        and worker_repository.get_job(job["id"])["status"] != "completed"
    ):
        time.sleep(0.01)
    assert worker_repository.get_job(job["id"])["status"] == "completed"
    worker.dispose()


def test_root_runtime_only_queues_service_owned_work(monkeypatch, tmp_path):
    monkeypatch.setenv("ADAOS_RUNTIME_PORT", "8777")
    monkeypatch.setenv("MEDIA_LIBRARY_AGENT_EMBEDDED_WORKER", "0")
    library = tmp_path / "library"
    library.mkdir()
    (library / "track.mp3").write_bytes(b"audio")
    repository, worker = main._runtime()
    root = repository.add_root(str(library))["root"]
    queued = repository.create_job(root["id"])["job"]
    repository.claim_job(queued["id"])

    monkeypatch.setattr(
        worker,
        "ensure_started",
        lambda: (_ for _ in ()).throw(AssertionError("root runtime started worker")),
    )
    recovered = main.recover_interrupted_runtime()

    assert main._owns_background_worker() is False
    assert recovered["requeued_job_count"] == 0
    assert recovered["worker"] == {"running": False, "owner": "service_process"}
    assert repository.get_job(queued["id"])["status"] == "running"


def test_service_process_owns_background_worker(monkeypatch):
    monkeypatch.setenv("ADAOS_RUNTIME_PORT", "8777")
    monkeypatch.setenv("ADAOS_SERVICE_SKILL", "media_library_agent")

    assert main._owns_background_worker() is True


def test_root_lifecycle_defers_repository_ownership_to_service(monkeypatch):
    monkeypatch.setenv("ADAOS_RUNTIME_PORT", "8777")
    monkeypatch.setenv("MEDIA_LIBRARY_AGENT_EMBEDDED_WORKER", "0")
    monkeypatch.setattr(
        main,
        "_runtime",
        lambda: (_ for _ in ()).throw(
            AssertionError("root lifecycle opened the service-owned repository")
        ),
    )

    rehydrated = main.rehydrate()
    disposed = main.dispose()

    assert rehydrated == {
        "ok": True,
        "schema": main.SCHEMA_VERSION,
        "deferred": True,
        "deferred_to": "service_process",
        "worker": {"running": False, "owner": "service_process"},
    }
    assert disposed == {
        "ok": True,
        "schema": main.SCHEMA_VERSION,
        "disposed": False,
        "owner": "service_process",
        "deferred": True,
    }


def test_bounded_watcher_debounces_changes_into_incremental_scan(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "first.mp3").write_bytes(b"first")
    repository = MediaLibraryAgentRepository(
        tmp_path / "watch.sqlite3", node_id="node-a"
    )
    root = repository.add_root(str(library))["root"]
    schedule = repository.configure_schedule(
        root["id"],
        enabled=True,
        interval_seconds=604800,
        debounce_seconds=1,
        watch_enabled=True,
        watch_poll_seconds=5,
    )["schedule"]
    worker = MediaLibraryAgentWorker(repository)

    worker._poll_watch_schedules(force=True)
    (library / "second.mp3").write_bytes(b"second")
    worker._poll_watch_schedules(force=True)
    detected_at, overflow = worker._watch_pending[root["id"]]
    worker._watch_pending[root["id"]] = (detected_at - 2, overflow)
    worker._poll_watch_schedules(force=True)
    queued = repository.next_queued_job()

    assert schedule["watch_enabled"] is True
    assert schedule["watch_poll_seconds"] == 5
    assert queued["root_id"] == root["id"]
    assert queued["mode"] == "incremental"
    assert worker.watch_status()["pending_root_ids"] == []


def test_watcher_overflow_queues_authoritative_reconcile(monkeypatch, tmp_path):
    library = tmp_path / "large-library"
    library.mkdir()
    for index in range(110):
        (library / f"track-{index:03d}.mp3").write_bytes(b"audio")
    monkeypatch.setenv("MEDIA_LIBRARY_AGENT_WATCH_MAX_ENTRIES", "100")
    repository = MediaLibraryAgentRepository(
        tmp_path / "watch-overflow.sqlite3", node_id="node-a"
    )
    root = repository.add_root(str(library))["root"]
    repository.configure_schedule(
        root["id"],
        enabled=True,
        debounce_seconds=1,
        watch_enabled=True,
    )
    worker = MediaLibraryAgentWorker(repository)

    worker._poll_watch_schedules(force=True)
    detected_at, overflow = worker._watch_pending[root["id"]]
    worker._watch_pending[root["id"]] = (detected_at - 2, overflow)
    worker._poll_watch_schedules(force=True)

    assert overflow is True
    assert repository.next_queued_job()["mode"] == "reconcile"


def test_folder_browse_and_opaque_cursor_validation(tmp_path):
    library = tmp_path / "library"
    book = library / "Author" / "Book One"
    book.mkdir(parents=True)
    (book / "001.mp3").write_bytes(b"chapter")
    imported = main.import_folder(path=str(library))
    _wait(imported["job"]["id"])

    top = main.browse_folders(root_id=imported["root"]["id"])
    child = main.browse_folders(root_id=imported["root"]["id"], parent="Author")
    invalid = main.pull_deltas(cursor="not-a-cursor")

    assert top["items"][0]["name"] == "Author"
    assert child["items"][0]["name"] == "Book One"
    assert invalid["ok"] is False
    assert invalid["error"] == "invalid_cursor"


def test_root_overlap_is_rejected_without_touching_external_files(tmp_path):
    library = tmp_path / "library"
    nested = library / "nested"
    nested.mkdir(parents=True)
    source = nested / "track.mp3"
    source.write_bytes(b"audio")
    repository = MediaLibraryAgentRepository(
        tmp_path / "overlap.sqlite3", node_id="node-a"
    )

    parent = repository.add_root(str(library))
    overlap = repository.add_root(str(nested))

    assert parent["ok"] is True
    assert overlap["ok"] is False
    assert overlap["error"] == "root_path_overlap"
    assert overlap["overlap"]["root_id"] == parent["root"]["id"]
    assert source.read_bytes() == b"audio"


def test_scan_window_contract_fails_closed(tmp_path):
    library = tmp_path / "library"
    library.mkdir()

    invalid = main.add_root(
        path=str(library),
        scan_window={"start": "25:00", "end": "07:00", "days": [0]},
    )
    valid = main.add_root(
        path=str(library),
        scan_window={"start": "23:00", "end": "07:00", "days": [0, 1, 2, 3, 4]},
    )

    assert invalid["ok"] is False
    assert invalid["error"] == "root_scan_window_invalid"
    assert valid["root"]["scan_window"]["start"] == "23:00"


def test_folder_browse_is_server_paged_and_cursor_backed(tmp_path):
    library = tmp_path / "library"
    for name in ("Alpha", "Beta", "Gamma"):
        folder = library / name
        folder.mkdir(parents=True)
        (folder / "track.mp3").write_bytes(name.encode("ascii"))
    imported = main.import_folder(path=str(library))
    _wait(imported["job"]["id"])

    first = main.browse_folders(root_id=imported["root"]["id"], limit=1)
    second = main.browse_folders(
        root_id=imported["root"]["id"],
        limit=1,
        cursor=first["pagination"]["next_cursor"],
    )
    invalid = main.browse_folders(cursor="not-a-cursor")

    assert first["count"] == 1
    assert first["total_count"] == 3
    assert first["pagination"]["has_more"] is True
    assert second["items"][0]["name"] == "Beta"
    assert invalid["error"] == "invalid_cursor"


def test_contract_examples_validate_against_strict_schemas(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    library = tmp_path / "library"
    library.mkdir()
    (library / "movie.mp4").write_bytes(b"video")
    imported = main.import_folder(path=str(library))
    job = _wait(imported["job"]["id"])
    delta = main.pull_deltas(limit=1)["items"][0]
    folder = main.browse_folders(root_id=imported["root"]["id"])
    if not folder["items"]:
        nested = library / "Nested"
        nested.mkdir()
        (nested / "track.mp3").write_bytes(b"audio")
        rescanned = main.start_scan(root_id=imported["root"]["id"])
        _wait(rescanned["job"]["id"])
        folder = main.browse_folders(root_id=imported["root"]["id"])

    published = []
    repository = MediaLibraryAgentRepository(
        tmp_path / "progress.sqlite3", node_id="node-progress"
    )
    progress_root_path = tmp_path / "progress-library"
    progress_root_path.mkdir()
    (progress_root_path / "track.mp3").write_bytes(b"audio")
    progress_root = repository.add_root(str(progress_root_path))["root"]
    repository.create_job(progress_root["id"])

    def register(path, _root, metadata):
        return {
            "id": f"ref-{path.name}",
            "resource_id": f"ref-{path.name}",
            "name": path.name,
            "mime_type": "audio/mpeg",
            "source_path": str(path),
            "metadata": dict(metadata),
        }

    worker = MediaLibraryAgentWorker(
        repository,
        register=register,
        publish=lambda value, _webspace: published.append(value),
    )
    worker.run_once()

    fixtures = {
        "media-library-root.v1.schema.json": imported["root"],
        "media-library-scan-job.v1.schema.json": job,
        "media-library-source-delta.v1.schema.json": delta,
        "media-library-folder-node.v1.schema.json": folder["items"][0],
        "media-library-scan-progress.v1.schema.json": published[-1],
    }
    for filename, payload in fixtures.items():
        schema = json.loads(
            (SKILL_ROOT / "schemas" / filename).read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(schema).validate(payload)


def _topology_payload(root, *, phase="inspect", idempotency_key="phase-1"):
    instance = {
        "schema": "adaos.distributed.service_instance.v1",
        "instance_id": "media-agent-node-a",
        "group_id": "media-library-home",
        "node_id": "node-a",
        "activation_id": "activation-node-a",
        "release_digest": "sha256:" + "a" * 64,
        "component_ref": "skill:media_library_agent",
        "runtime_generation": 1,
        "protocol_version": "1",
        "topology_generation": 1,
        "lease_id": "lease-node-a",
        "status": "ready",
        "readiness": True,
        "health": {},
        "pressure": {},
        "capabilities": [],
        "endpoints": [],
        "observed_at": "2026-08-19T00:00:00+00:00",
        "revision": 1,
    }
    return {
        "schema": "adaos.distributed.topology_phase_request.v1",
        "target_node_id": "node-a",
        "selected_instance_id": instance["instance_id"],
        "operation_id": "topology-operation-1",
        "phase": phase,
        "authority_epoch": 0,
        "idempotency_key": idempotency_key,
        "partition": {
            "partition_id": f"media-sources:{root['id']}",
            "selector": {"root_id": root["id"]},
        },
        "dataset": {"consistency_profile": "external_authority"},
        "step": {"replica_role": "follower"},
        "source_instance": instance,
        "target_instance": None,
        "source_replica": None,
        "target_replica": None,
    }


def test_topology_phase_receipts_are_idempotent_and_do_not_copy_media(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "track.mp3").write_bytes(b"audio")
    repository = MediaLibraryAgentRepository(
        tmp_path / "topology.sqlite3",
        node_id="node-a",
    )
    root = repository.add_root(str(library))["root"]
    topology = LibraryAgentTopology()
    payload = _topology_payload(root)

    first = topology.execute_phase(repository, payload, resource_pressure="normal")
    repeated = topology.execute_phase(repository, payload, resource_pressure="normal")

    assert first == repeated
    assert first["receipt"]["external_media_copied"] is False
    assert repository.get_root(root["id"])["path"] == str(library.resolve())
    conflict = topology.execute_phase(
        repository,
        {**payload, "phase": "verify"},
        resource_pressure="normal",
    )
    assert conflict["error_code"] == "topology_phase_idempotency_conflict"


def test_topology_phase_rejects_external_root_on_wrong_node(tmp_path):
    repository = MediaLibraryAgentRepository(
        tmp_path / "wrong-node.sqlite3",
        node_id="node-b",
    )
    payload = _topology_payload(
        {"id": "root_missing"},
        phase="prepare",
    )
    payload["target_node_id"] = "node-b"
    payload["selected_instance_id"] = "media-agent-node-b"
    payload["source_instance"] = None
    payload["target_instance"] = {
        **_topology_payload({"id": "root_missing"})["source_instance"],
        "instance_id": "media-agent-node-b",
        "node_id": "node-b",
    }

    result = LibraryAgentTopology().execute_phase(
        repository,
        payload,
        resource_pressure="normal",
    )

    assert result["ok"] is False
    assert result["error_code"] == "external_root_not_present_on_target"


def test_replicated_topology_phase_preserves_observed_data_witness(tmp_path):
    repository = MediaLibraryAgentRepository(
        tmp_path / "replicated.sqlite3",
        node_id="node-a",
    )
    checkpoint = "sha256:" + "b" * 64
    previous = {
        "replica_id": topology_module.stable_id(
            "replica", "catalog-home", "media-agent-node-a", size=28
        ),
        "partition_id": "catalog-home",
        "instance_id": "media-agent-node-a",
        "node_id": "node-a",
        "role": "follower",
        "lifecycle": "ready",
        "content_state": "non_empty",
        "authority_epoch": 1,
        "checkpoint": checkpoint,
        "source_ref": "catalog-generation:7",
        "freshness_seconds": 3,
        "item_count": 42,
        "byte_count": 2048,
        "observed_at": "2026-08-19T00:00:00+00:00",
        "revision": 3,
    }
    payload = _topology_payload(
        {"id": "replicated"},
        phase="promote",
        idempotency_key="phase-replicated-promote",
    )
    payload["partition"] = {"partition_id": "catalog-home", "selector": {}}
    payload["dataset"] = {"consistency_profile": "single_authority"}
    payload["authority_epoch"] = 2
    payload["source_replica"] = previous

    result = LibraryAgentTopology().execute_phase(
        repository,
        payload,
        resource_pressure="normal",
    )

    assert result["ok"] is True
    assert result["receipt"]["checkpoint"] == checkpoint
    assert result["receipt"]["content_witness"] == checkpoint
    assert result["receipt"]["item_count"] == 42
    assert result["receipt"]["byte_count"] == 2048
    assert result["receipt"]["replica"]["role"] == "authority"
    assert result["receipt"]["replica"]["authority_epoch"] == 2
    assert result["receipt"]["replica"]["checkpoint"] == checkpoint
    assert result["receipt"]["replica"]["source_ref"] == "catalog-generation:7"


def test_replicated_topology_phase_rejects_empty_target_witness(tmp_path):
    repository = MediaLibraryAgentRepository(
        tmp_path / "empty-replica.sqlite3",
        node_id="node-b",
    )
    payload = _topology_payload(
        {"id": "replicated"},
        phase="verify",
        idempotency_key="phase-empty-target-verify",
    )
    source = dict(payload["source_instance"])
    target = {
        **source,
        "instance_id": "media-agent-node-b",
        "node_id": "node-b",
        "activation_id": "activation-node-b",
        "lease_id": "lease-node-b",
    }
    payload.update(
        target_node_id="node-b",
        selected_instance_id="media-agent-node-b",
        partition={"partition_id": "catalog-home", "selector": {}},
        dataset={"consistency_profile": "single_authority"},
        target_instance=target,
        source_replica={
            "replica_id": "replica-source",
            "partition_id": "catalog-home",
            "instance_id": "media-agent-node-a",
            "node_id": "node-a",
            "role": "follower",
            "lifecycle": "ready",
            "content_state": "non_empty",
            "authority_epoch": 0,
            "checkpoint": "catalog:10",
            "source_ref": "catalog-generation:10",
            "freshness_seconds": 0,
            "item_count": 42,
            "byte_count": 2048,
            "observed_at": "2026-08-19T00:00:00+00:00",
            "revision": 1,
        },
    )

    result = LibraryAgentTopology().execute_phase(
        repository,
        payload,
        resource_pressure="normal",
    )

    assert result == {
        "ok": False,
        "error_code": "topology_target_content_witness_mismatch",
    }


def test_small_catalog_snapshot_catches_up_target_without_media_copy(tmp_path):
    library = tmp_path / "source-library"
    library.mkdir()
    (library / "track.mp3").write_bytes(b"audio")
    source_repository = MediaLibraryAgentRepository(
        tmp_path / "snapshot-source.sqlite3",
        node_id="node-a",
    )
    root = source_repository.add_root(str(library))["root"]
    source_repository.create_job(root["id"], mode="full")
    source_worker = MediaLibraryAgentWorker(
        source_repository,
        register=lambda path, _root, metadata: {
            "resource_id": f"resource-{path.name}",
            "source_path": str(path),
            "mime_type": "audio/mpeg",
            "metadata": dict(metadata),
        },
    )
    source_worker.run_once()
    witness = source_repository.topology_root_witness(root["id"])
    assert witness is not None

    source_payload = _topology_payload(
        root,
        phase="snapshot",
        idempotency_key="small-snapshot-source",
    )
    source_payload["partition"] = {
        "partition_id": "catalog-small",
        "selector": {"root_id": root["id"]},
    }
    source_payload["dataset"] = {"consistency_profile": "single_authority"}
    source_result = LibraryAgentTopology().execute_phase(
        source_repository,
        source_payload,
        resource_pressure="normal",
    )
    inline_snapshot = source_result["receipt"]["inline_snapshot"]

    target_repository = MediaLibraryAgentRepository(
        tmp_path / "snapshot-target.sqlite3",
        node_id="node-b",
    )
    source_instance = dict(source_payload["source_instance"])
    target_instance = {
        **source_instance,
        "instance_id": "media-agent-node-b",
        "node_id": "node-b",
        "activation_id": "activation-node-b",
        "lease_id": "lease-node-b",
    }
    source_replica = {
        "instance_id": "media-agent-node-a",
        "checkpoint": witness["checkpoint"],
        "content_state": "non_empty",
        "item_count": witness["available"],
        "byte_count": witness["bytes"],
    }
    target_payload = {
        **source_payload,
        "target_node_id": "node-b",
        "selected_instance_id": "media-agent-node-b",
        "target_instance": target_instance,
        "source_replica": source_replica,
        "phase": "catch_up",
        "idempotency_key": "small-snapshot-target-catch-up",
        "phase_inputs": {"source_snapshot": inline_snapshot},
    }
    caught_up = LibraryAgentTopology().execute_phase(
        target_repository,
        target_payload,
        resource_pressure="normal",
    )
    verified = LibraryAgentTopology().execute_phase(
        target_repository,
        {
            **target_payload,
            "phase": "verify",
            "idempotency_key": "small-snapshot-target-verify",
            "phase_inputs": {},
        },
        resource_pressure="normal",
    )

    assert caught_up["ok"] is True
    assert verified["ok"] is True
    assert verified["receipt"]["checkpoint"] == witness["checkpoint"]
    assert verified["receipt"]["item_count"] == 1
    assert target_repository.summary()["source_count"] == 0
    assert target_repository.topology_replica_snapshot("catalog-small") is not None


def test_large_catalog_uses_chunked_data_plane_without_media_copy(tmp_path):
    source_repository = MediaLibraryAgentRepository(
        tmp_path / "large-source.sqlite3",
        node_id="node-a",
    )
    library = tmp_path / "large-library"
    library.mkdir()
    root = source_repository.add_root(str(library))["root"]
    for index in range(1001):
        source_repository.upsert_source(
            {
                "root_id": root["id"],
                "relative_path": f"Album/{index:04d}.mp3",
                "folder_path": "Album",
                "name": f"{index:04d}.mp3",
                "media_kind": "audio",
                "mime_type": "audio/mpeg",
                "size_bytes": 100 + index,
                "modified_ns": index + 1,
                "inode": index + 1,
                "fingerprint": f"fingerprint-{index}",
                "resource_id": f"resource-{index}",
                "descriptor": {"title": f"Track {index}"},
                "metadata": {"root_label": "Music"},
            },
            job_id="large-fixture",
        )
    witness = source_repository.topology_root_witness(root["id"])
    assert witness is not None
    source_payload = _topology_payload(
        root,
        phase="snapshot",
        idempotency_key="large-snapshot-source",
    )
    source_payload["partition"] = {
        "partition_id": "catalog-large",
        "selector": {"root_id": root["id"]},
    }
    source_payload["dataset"] = {"consistency_profile": "single_authority"}
    topology = LibraryAgentTopology()
    source_result = topology.execute_phase(
        source_repository,
        source_payload,
        resource_pressure="normal",
    )
    manifest = source_result["receipt"]["transfer_manifest"]
    assert manifest["payload_bytes"] > 0
    assert manifest["item_count"] == 1001
    assert manifest["external_media_copied"] is False

    cached_manifest = source_repository.prepare_topology_catalog_transfer(
        artifact_id="snapshot-redundant-export",
        root_id=root["id"],
    )
    assert cached_manifest == manifest
    assert not (
        source_repository.db_path.parent
        / "topology_transfers"
        / "snapshot-redundant-export.zlib"
    ).exists()

    target_repository = MediaLibraryAgentRepository(
        tmp_path / "large-target.sqlite3",
        node_id="node-b",
    )
    source_instance = dict(source_payload["source_instance"])
    target_instance = {
        **source_instance,
        "instance_id": "media-agent-node-b",
        "node_id": "node-b",
        "activation_id": "activation-node-b",
        "lease_id": "lease-node-b",
    }
    checkpoint = None
    transfer_id = "large-catalog-transfer"
    while True:
        read_result = topology.execute_transfer(
            source_repository,
            {
                **source_payload,
                "schema": "adaos.distributed.topology_transfer_request.v1",
                "direction": "read",
                "selected_instance_id": source_instance["instance_id"],
                "source_instance": source_instance,
                "target_instance": target_instance,
                "transfer_id": transfer_id,
                "manifest": manifest,
                "checkpoint": checkpoint,
                "max_bytes": 8 * 1024,
                "chunk": None,
            },
        )
        chunk = read_result["receipt"]
        write_result = topology.execute_transfer(
            target_repository,
            {
                **source_payload,
                "schema": "adaos.distributed.topology_transfer_request.v1",
                "target_node_id": "node-b",
                "direction": "write",
                "selected_instance_id": target_instance["instance_id"],
                "source_instance": source_instance,
                "target_instance": target_instance,
                "transfer_id": transfer_id,
                "manifest": manifest,
                "checkpoint": checkpoint,
                "max_bytes": 8 * 1024,
                "chunk": chunk,
            },
        )
        assert write_result["ok"] is True
        checkpoint = chunk["checkpoint"]
        if chunk["eof"]:
            assert (
                write_result["receipt"]["content_witness"] == manifest["payload_digest"]
            )
            break

    source_replica = {
        "instance_id": source_instance["instance_id"],
        "checkpoint": witness["checkpoint"],
        "content_state": "non_empty",
        "item_count": witness["available"],
        "byte_count": witness["bytes"],
    }
    caught_up = topology.execute_phase(
        target_repository,
        {
            **source_payload,
            "target_node_id": "node-b",
            "selected_instance_id": target_instance["instance_id"],
            "target_instance": target_instance,
            "source_replica": source_replica,
            "phase": "catch_up",
            "idempotency_key": "large-snapshot-target-catch-up",
            "phase_inputs": {
                "source_transfer": manifest,
                "transfer_receipt": {"state": "complete"},
            },
        },
        resource_pressure="normal",
    )
    snapshot = target_repository.topology_replica_snapshot("catalog-large")
    with target_repository.connect() as connection:
        replicated = connection.execute(
            "SELECT COUNT(*) FROM topology_replica_items WHERE partition_id=?",
            ("catalog-large",),
        ).fetchone()[0]

    assert caught_up["ok"] is True
    assert snapshot is not None and snapshot["item_count"] == 1001
    assert replicated == 1001
    assert target_repository.summary()["source_count"] == 0


def _rendition_source(repository, worker, library: Path) -> dict:
    root = repository.add_root(str(library))["root"]
    scan = repository.create_job(root["id"], mode="full")
    completed = worker.run_once()
    assert completed and completed["id"] == scan["job"]["id"]
    page = repository.pull_deltas(limit=10)
    return page["items"][0]["source"]


def test_rendition_is_bounded_derived_and_tied_to_exact_source(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    original = library / "legacy.mkv"
    original.write_bytes(b"original-media")
    repository = MediaLibraryAgentRepository(
        tmp_path / "rendition.sqlite3", node_id="node-a"
    )
    published = (
        tmp_path / "published" / "media-library-rendition-test-browser-mp4-v1.mp4"
    )

    def register(path, _root, metadata):
        return {
            "resource_id": "original-ref",
            "path": str(path),
            "source_path": str(path),
            "mime_type": "video/x-matroska",
            "metadata": {
                **metadata,
                "technical": {
                    "probe": "ffprobe",
                    "container": "mkv",
                    "codec": "hevc",
                    "height": 1080,
                },
            },
        }

    def transcode(_source, target, _job, *, cancelled):
        assert cancelled() is False
        target.write_bytes(b"browser-compatible")
        return {"size_bytes": target.stat().st_size, "mime_type": "video/mp4"}

    def publish(target, _job):
        published.parent.mkdir()
        published.write_bytes(target.read_bytes())
        return {
            "resource_id": published.name,
            "filename": published.name,
            "path": str(published),
            "mime_type": "video/mp4",
            "size_bytes": published.stat().st_size,
            "direct_urls": ["http://node-a/media/derived"],
            "metadata": {"namespace": "media-library-rendition"},
        }

    worker = MediaLibraryAgentWorker(
        repository,
        register=register,
        transcode=transcode,
        publish_derived=publish,
    )
    source = _rendition_source(repository, worker, library)
    plan = rendition_plan(
        source,
        endpoint_capabilities={
            "codecs": ["h264"],
            "mime_types": ["video/mp4"],
            "containers": ["mp4"],
            "max_video_height": 720,
        },
    )
    assert plan["required"] is True
    assert set(plan["reasons"]) == {
        "codec_not_supported",
        "mime_type_not_supported",
        "container_not_supported",
        "height_above_endpoint_limit",
    }
    queued = repository.create_rendition_job(
        source["id"], profile="browser-mp4-v1", target=plan["target"]
    )
    result = worker.run_once()
    assert result and result["id"] == queued["job"]["id"]
    assert result["status"] == "completed"
    changed = repository.get_source(source["id"])
    rendition = changed["metadata"]["derived_renditions"][0]
    assert rendition["exact_source_id"] == source["id"]
    assert rendition["exact_source_revision"] == source["revision"]
    assert rendition["exact_source_fingerprint"] == source["fingerprint"]
    assert rendition["descriptor"]["metadata"]["storage_mode"] == "derived_copy"
    assert published.read_bytes() == b"browser-compatible"
    assert original.read_bytes() == b"original-media"
    assert list((tmp_path / "renditions").glob("*")) == []

    schema_dir = SKILL_ROOT / "schemas"
    from jsonschema import Draft202012Validator

    Draft202012Validator(
        json.loads(
            (schema_dir / "media-library-rendition-plan.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
    ).validate(plan)
    Draft202012Validator(
        json.loads(
            (schema_dir / "media-library-rendition-job.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
    ).validate(result)


def test_folder_artwork_is_bounded_published_and_tied_to_exact_source(tmp_path):
    from PIL import Image
    from jsonschema import Draft202012Validator

    library = tmp_path / "library"
    album = library / "Artist" / "Album"
    album.mkdir(parents=True)
    original = album / "01.mp3"
    original.write_bytes(b"original-audio")
    cover = album / "Cover.png"
    Image.new("RGB", (1600, 1600), "red").save(cover)
    original_bytes = original.read_bytes()
    cover_bytes = cover.read_bytes()
    repository = MediaLibraryAgentRepository(
        tmp_path / "artwork.sqlite3", node_id="node-a"
    )
    published = tmp_path / "published" / "album-cover.jpg"

    def register(path, _root, metadata):
        return {
            "resource_id": "audio-ref",
            "path": str(path),
            "source_path": str(path),
            "mime_type": "audio/mpeg",
            "metadata": metadata,
        }

    def publish(target, _job):
        published.parent.mkdir(exist_ok=True)
        published.write_bytes(target.read_bytes())
        return {
            "resource_id": "album-cover",
            "filename": "album-cover.jpg",
            "path": str(published),
            "mime_type": "image/jpeg",
            "size_bytes": published.stat().st_size,
            "browser_path": "/media/album-cover.jpg",
            "metadata": {"namespace": "media-library-artwork"},
        }

    worker = MediaLibraryAgentWorker(
        repository,
        register=register,
        publish_derived=publish,
    )
    source = _rendition_source(repository, worker, library)
    queued = repository.next_queued_rendition_job()

    assert queued is not None and queued["profile"] == "artwork-card-v1"
    result = worker.run_once()
    assert result and result["status"] == "completed"
    changed = repository.get_source(source["id"])
    artwork = changed["metadata"]["artwork"]
    assert artwork["state"] == "ready"
    assert artwork["provider_id"] == "media_library_agent.folder_artwork.v1"
    assert artwork["source_kind"] == "folder"
    assert artwork["exact_source_id"] == source["id"]
    assert artwork["exact_source_revision"] == source["revision"]
    assert artwork["exact_source_fingerprint"] == source["fingerprint"]
    assert artwork["descriptor"]["browser_path"] == "/media/album-cover.jpg"
    assert 0 < artwork["width"] <= 720
    assert 0 < artwork["height"] <= 1080
    assert artwork_plan(changed)["required"] is False
    assert original.read_bytes() == original_bytes
    assert cover.read_bytes() == cover_bytes
    assert list((tmp_path / "renditions").glob("*")) == []

    rescan = repository.create_job(changed["root_id"], mode="full")
    rescanned = worker.run_once()
    assert rescanned and rescanned["id"] == rescan["job"]["id"]
    stable = repository.get_source(source["id"])
    assert stable is not None
    assert stable["metadata"]["artwork"] == artwork
    assert repository.next_queued_rendition_job() is None

    Draft202012Validator(
        json.loads(
            (
                SKILL_ROOT / "schemas" / "media-library-artwork-plan.v1.schema.json"
            ).read_text(encoding="utf-8")
        )
    ).validate(artwork_plan(changed))


def test_rendition_source_change_after_publish_is_not_advertised_and_is_cleaned(
    tmp_path,
):
    library = tmp_path / "library"
    library.mkdir()
    original = library / "legacy.mkv"
    original.write_bytes(b"v1")
    repository = MediaLibraryAgentRepository(
        tmp_path / "race.sqlite3", node_id="node-a"
    )
    published = tmp_path / "media-library-rendition-race.mp4"

    def register(path, _root, metadata):
        return {
            "resource_id": "source",
            "path": str(path),
            "source_path": str(path),
            "mime_type": "video/x-matroska",
            "metadata": {
                **metadata,
                "technical": {"codec": "hevc", "container": "mkv"},
            },
        }

    def transcode(_source, target, _job, *, cancelled):
        assert cancelled() is False
        target.write_bytes(b"derived")
        return {"size_bytes": 7}

    def publish(target, job):
        published.write_bytes(target.read_bytes())
        current = repository.get_source(job["source_id"])
        repository.upsert_source(
            {**current, "fingerprint": "changed-after-publish"},
            job_id="concurrent-scan",
        )
        return {
            "resource_id": published.name,
            "filename": published.name,
            "path": str(published),
            "mime_type": "video/mp4",
            "size_bytes": 7,
            "metadata": {"namespace": "media-library-rendition"},
        }

    worker = MediaLibraryAgentWorker(
        repository,
        register=register,
        transcode=transcode,
        publish_derived=publish,
    )
    source = _rendition_source(repository, worker, library)
    plan = rendition_plan(
        source,
        endpoint_capabilities={"codecs": ["h264"], "containers": ["mp4"]},
    )
    queued = repository.create_rendition_job(
        source["id"], profile="browser-mp4-v1", target=plan["target"]
    )
    result = worker.run_once()
    assert result["id"] == queued["job"]["id"]
    assert result["status"] == "invalidated"
    assert result["error"]["code"] == "source_changed"
    assert published.exists() is False
    changed = repository.get_source(source["id"])
    assert changed["metadata"].get("derived_renditions") in (None, [])


def test_queued_rendition_cancellation_is_terminal_without_worker(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "legacy.mkv").write_bytes(b"v1")
    repository = MediaLibraryAgentRepository(
        tmp_path / "cancel.sqlite3", node_id="node-a"
    )
    worker = MediaLibraryAgentWorker(
        repository,
        register=lambda path, _root, metadata: {
            "resource_id": "source",
            "path": str(path),
            "mime_type": "video/x-matroska",
            "metadata": metadata,
        },
    )
    source = _rendition_source(repository, worker, library)
    plan = rendition_plan(source, endpoint_capabilities={"mime_types": ["video/mp4"]})
    queued = repository.create_rendition_job(
        source["id"], profile="browser-mp4-v1", target=plan["target"]
    )
    canceled = repository.request_rendition_cancel(queued["job"]["id"])
    assert canceled["job"]["status"] == "canceled"
    assert canceled["job"]["finished_at"]
    remaining = repository.next_queued_rendition_job()
    assert remaining is not None and remaining["profile"] == "artwork-card-v1"


def test_agent_deep_search_covers_unicode_folders_and_technical_metadata(tmp_path):
    library = tmp_path / "library" / "Аудиокниги"
    library.mkdir(parents=True)
    (library / "01.mkv").write_bytes(b"v1")
    repository = MediaLibraryAgentRepository(
        tmp_path / "search.sqlite3", node_id="node-a"
    )
    worker = MediaLibraryAgentWorker(
        repository,
        register=lambda path, _root, metadata: {
            "resource_id": "source",
            "path": str(path),
            "mime_type": "video/x-matroska",
            "metadata": {
                **metadata,
                "technical": {"codec": "hevc", "container": "matroska"},
            },
        },
    )
    _rendition_source(repository, worker, library.parent)

    by_folder = repository.search_sources(query="Аудиокниги", limit=1)
    by_codec = repository.search_sources(query="hevc", limit=1)

    assert by_folder["items"][0]["name"] == "01.mkv"
    assert by_codec["items"][0]["match"]["stage"] == "agent_technical_fts"
    assert by_codec["has_more"] is False


def test_perceptual_sampling_is_opt_in_bounded_and_never_publishes_bytes(
    monkeypatch, tmp_path
):
    from media_library_agent import worker as worker_module

    source = tmp_path / "sample.mp4"
    source.write_bytes(b"original-media")
    monkeypatch.setenv("MEDIA_LIBRARY_AGENT_PERCEPTUAL_HASH_MODE", "ffmpeg")
    monkeypatch.setenv("MEDIA_LIBRARY_AGENT_PERCEPTUAL_HASH_TIMEOUT_SECONDS", "7")
    monkeypatch.setattr(worker_module.shutil, "which", lambda name: f"/{name}")
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout=b"normalized-samples")

    monkeypatch.setattr(worker_module.subprocess, "run", fake_run)

    result = MediaLibraryAgentWorker._technical_metadata(source, stat=source.stat())

    assert result["perceptual_hash_algorithm"] == "ffmpeg_sample_sha256_v1"
    assert result["perceptual_hash"]
    assert captured["kwargs"]["timeout"] == 7
    assert captured["command"][captured["command"].index("-threads") + 1] == "1"
    assert (
        captured["command"][captured["command"].index("-protocol_whitelist") + 1]
        == "file,pipe"
    )
    assert captured["command"][-1] == "pipe:1"
    assert source.read_bytes() == b"original-media"
