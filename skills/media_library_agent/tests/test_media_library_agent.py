from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from handlers import main  # noqa: E402
from media_library_agent.repository import MediaLibraryAgentRepository  # noqa: E402
from media_library_agent.worker import MediaLibraryAgentWorker  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_LIBRARY_AGENT_DB_PATH", str(tmp_path / "agent.sqlite3"))
    monkeypatch.setenv("ADAOS_MEDIA_REFERENCE_DB_PATH", str(tmp_path / "references.sqlite3"))
    monkeypatch.setenv("ADAOS_NODE_ID", "node-test")
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
    assert result["storage"] == {"mode": "external_reference", "media_bytes_copied": False}
    job = _wait(result["job"]["id"])
    assert job["status"] == "completed"
    assert job["progress"]["processed_count"] == 1
    deltas = main.pull_deltas(limit=10)
    assert [item["source"]["name"] for item in deltas["items"]] == ["01.mp3"]
    assert deltas["items"][0]["source"]["metadata"]["folder_segments"] == ["Artist", "Album"]
    assert deltas["items"][0]["source"]["descriptor"]["metadata"]["storage_mode"] == "reference"
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


def test_duplicate_scan_request_returns_active_job(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    repository = MediaLibraryAgentRepository(tmp_path / "direct.sqlite3", node_id="node-a")
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
    repository = MediaLibraryAgentRepository(tmp_path / "pressure.sqlite3", node_id="node-a")
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
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and repository.get_job(job["id"])["status"] != "waiting_resources":
        time.sleep(0.01)
    assert repository.get_job(job["id"])["status"] == "waiting_resources"
    worker.set_resource_pressure("normal")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and repository.get_job(job["id"])["status"] != "completed":
        time.sleep(0.01)
    assert repository.get_job(job["id"])["status"] == "completed"
    worker.dispose()


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


def test_contract_examples_validate_against_strict_schemas(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    library = tmp_path / "library"
    library.mkdir()
    (library / "movie.mp4").write_bytes(b"video")
    imported = main.import_folder(path=str(library))
    job = _wait(imported["job"]["id"])
    delta = main.pull_deltas(limit=1)["items"][0]

    fixtures = {
        "media-library-root.v1.schema.json": imported["root"],
        "media-library-scan-job.v1.schema.json": job,
        "media-library-source-delta.v1.schema.json": delta,
    }
    for filename, payload in fixtures.items():
        schema = json.loads((SKILL_ROOT / "schemas" / filename).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(payload)
