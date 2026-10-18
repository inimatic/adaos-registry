import sqlite3
import sys
import time
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_center.catalog import MediaCenterRepository
from media_center.coordinator import MediaCatalogCoordinator
from media_center.read_models import BoundedReadExecutor, ReadModelUnavailable, interactive_read
from media_center.sync import MediaAgentSyncWorker
from media_center.sync_state import SyncStateStore
from media_center.background import MediaCenterBackgroundRuntime


def test_cold_read_does_not_create_or_migrate_a_database(tmp_path):
    path = tmp_path / "missing.sqlite3"
    result = interactive_read(lambda: MediaCenterRepository(path))()
    assert result["state"] == "loading" and not result["ready"]
    assert not path.exists()


def test_read_cannot_mutate_and_does_not_wait_for_writer(tmp_path):
    repo = MediaCenterRepository(tmp_path / "read.sqlite3")
    MediaCatalogCoordinator(repo)
    with repo.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE meta SET value=value")
        @interactive_read
        def read():
            with repo.connect() as connection:
                value = connection.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    connection.execute("DELETE FROM meta")
                return value
        started = time.monotonic()
        assert read() > 0
        assert time.monotonic() - started < 0.5
        writer.rollback()


def test_expensive_read_has_one_total_sql_deadline(tmp_path):
    repo = MediaCenterRepository(tmp_path / "read.sqlite3")
    @interactive_read
    def read():
        with repo.connect() as connection:
            return connection.execute("""WITH RECURSIVE n(x) AS
                (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000)
                SELECT sum(x) FROM n""").fetchone()
    started = time.monotonic()
    assert read()["state"] == "loading"
    assert time.monotonic() - started < 1.0


def test_sync_observations_survive_worker_recreation(tmp_path):
    repo = MediaCenterRepository(tmp_path / "sync.sqlite3")
    MediaCatalogCoordinator(repo)
    store = SyncStateStore(repo)
    result = {"ok": True, "applied_count": 3, "agents": [
        {"agent_id": "a", "last_sequence": 12, "ok": True}]}
    worker = MediaAgentSyncWorker(lambda: result, state_store=store)
    worker.run_once()
    first = worker.status()
    assert first["attempts"] == 1
    assert first["last_progress_at"] and first["last_success_at"]
    recreated = MediaAgentSyncWorker(lambda: {"ok": False, "error": "unavailable"}, state_store=store)
    recreated.run_once()
    state = recreated.status()
    assert state["attempts"] == 2 and state["consecutive_failures"] == 1
    assert state["last_success_at"] == first["last_success_at"]
    assert state["last_progress_at"] == first["last_progress_at"]
    assert state["error_disposition"] == "retry_backoff"


def test_snapshot_lane_coalesces_without_blocking_request_and_disposes():
    runtime = MediaCenterBackgroundRuntime()
    entered, release, final = threading.Event(), threading.Event(), threading.Event()
    observed = []
    def blocked():
        entered.set()
        assert release.wait(3)
        observed.append("first")
    started = time.monotonic()
    assert runtime.queue_projection("same-route", blocked)
    assert time.monotonic() - started < 0.5
    assert entered.wait(2)
    for value in range(100):
        runtime.queue_projection("same-route", lambda value=value: (observed.append(value), final.set()))
    release.set()
    assert final.wait(2)
    assert observed == ["first", 99]
    assert runtime.dispose(timeout=2)["projection"]["stopped"] is True


def test_blocked_io_has_bounded_caller_latency_and_no_unbounded_queue():
    lane = BoundedReadExecutor(workers=1)
    released = threading.Event()
    started = time.monotonic()
    with pytest.raises(ReadModelUnavailable, match="read_deadline"):
        lane.run(lambda: released.wait(3), timeout=0.02)
    assert time.monotonic() - started < 0.5
    for _ in range(20):
        with pytest.raises(ReadModelUnavailable, match="read_lane_busy"):
            lane.run(lambda: pytest.fail("replacement reader was admitted"))
    assert lane.dispose(timeout=0)["stopped"] is False
    released.set()
    assert lane.dispose(timeout=2)["stopped"] is True
