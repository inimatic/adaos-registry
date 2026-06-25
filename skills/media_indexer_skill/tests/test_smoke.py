from __future__ import annotations

import asyncio
import importlib
import json
import pathlib
import sys
import types
from types import SimpleNamespace

import pytest
import yaml


SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


def test_manifest_declares_runtime_contracts() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))

    assert manifest["name"] == "media_indexer_skill"
    assert "requirements.txt" not in {path.name for path in SKILL_ROOT.iterdir()}
    assert manifest.get("dependencies") == ["shazamio"]
    assert not (manifest.get("models") or {}).get("artifacts")
    assert "media_indexer.action" in manifest["events"]["subscribe"]
    assert "webio.stream.snapshot.requested" in manifest["events"]["subscribe"]
    assert any(route["route"] == "stream" and route["receiver"] == "media_indexer.operations" for route in manifest["data_routes"])
    assert manifest["lifecycle"]["rehydrate"] == "rehydrate"
    assert manifest["lifecycle"]["dispose"] == "dispose"


def test_webui_declares_compact_yjs_and_stream_receiver() -> None:
    webui = json.loads((SKILL_ROOT / "webui.json").read_text(encoding="utf-8"))

    assert webui["ydoc_defaults"]["data/media_indexer"]["form"]["directory"] == ""
    assert "D:\\diploma_final\\demo_media" not in json.dumps(webui)
    assert webui["ydoc_defaults"]["data/media_indexer"]["form"]["query"] == ""
    defaults = webui["ydoc_defaults"]["data/media_indexer"]
    assert defaults["overview"]["label"] == "Library overview"
    assert defaults["diagnostics"]["label"] == "Model diagnostics"
    assert defaults["diagnostics"]["summary"]["label"] == "Indexed media"
    assert defaults["library"] == []
    receiver = webui["webio"]["receivers"]["media_indexer.operations"]
    assert receiver["mode"] == "replace"
    assert receiver["snapshotPolicy"] == "on_subscribe"
    schema = webui["registry"]["modals"]["media_indexer_modal"]["schema"]
    assert schema["layout"]["pattern"] == "split"
    library_widget = next(widget for widget in schema["widgets"] if widget["id"] == "media-indexer-library")
    assert library_widget["type"] == "ui.table"
    assert [button["id"] for button in library_widget["inputs"]["buttons"]] == ["play"]
    actions_widget = next(widget for widget in schema["widgets"] if widget["id"] == "media-indexer-controls")
    assert [button["id"] for button in actions_widget["inputs"]["buttons"]] == ["scan_selected"]
    results_widget = next(
        widget
        for widget in schema["widgets"]
        if widget["id"] == "media-indexer-results"
    )
    assert results_widget["area"] == "bottom"
    assert results_widget["inputs"]["titleKey"] == "title"
    assert results_widget["inputs"]["subtitleKey"] == "subtitle"
    assert results_widget["inputs"]["detailsPath"] == "details_text"
    assert [button["id"] for button in results_widget["inputs"]["buttons"]] == ["play"]
    player_widget = next(widget for widget in schema["widgets"] if widget["id"] == "media-indexer-player")
    assert player_widget["type"] == "media.videoBrowser"
    assert player_widget["inputs"]["readOnly"] is True


def test_scanner_finds_supported_media_without_hashing(tmp_path: pathlib.Path) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "clip.mp4").write_bytes(b"not a real mp4")
    (media_dir / "notes.txt").write_text("ignored", encoding="utf-8")

    from lib.scanner import DirectoryScanner

    inventory = DirectoryScanner(str(media_dir), compute_hashes=False).scan()

    assert [item.name for item in inventory["video"]] == ["clip.mp4"]
    assert "audio" not in inventory


def test_handler_import_is_passive_and_search_without_index_does_not_load_models(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(tmp_path / "skill_env.json"))
    monkeypatch.setenv("MEDIA_INDEXER_DATA_DIR", str(tmp_path / "data"))
    y_py = types.ModuleType("y_py")
    y_py.YDoc = object
    monkeypatch.setitem(sys.modules, "y_py", y_py)
    ystore = types.ModuleType("ypy_websocket.ystore")
    ystore.BaseYStore = object
    ystore.YDocNotFound = FileNotFoundError
    monkeypatch.setitem(sys.modules, "ypy_websocket", types.ModuleType("ypy_websocket"))
    monkeypatch.setitem(sys.modules, "ypy_websocket.ystore", ystore)

    main = importlib.import_module("handlers.main")
    main.dispose()

    result = main.search_media("anything")

    assert result["status"] == "error"
    assert result["results"] == []
    assert main._state["vector_db"] is None


def test_scan_action_uses_webspace_form_directory_when_payload_omits_directory(
    monkeypatch,
    tmp_path: pathlib.Path,
) -> None:
    async def run_case() -> None:
        monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(tmp_path / "skill_env.json"))
        monkeypatch.setenv("MEDIA_INDEXER_DATA_DIR", str(tmp_path / "data"))

        main = importlib.import_module("handlers.main")
        main.dispose()
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        memory: dict[str, dict] = {}
        projected: list[dict] = []
        seen: dict[str, str] = {}

        async def fake_read_directory(webspace_id: str | None, payload: dict) -> str:
            assert webspace_id == "ws-1"
            return str(media_dir)

        def fake_scan(directory: str, progress=None) -> dict:
            seen["directory"] = directory
            return {
                "status": "ok",
                "indexed_count": 1,
                "errors": [],
                "diagnostics": {
                    "indexed_count": 1,
                    "files_found": 1,
                    "by_type": {"video": 1},
                    "description": "Indexed 1 media files.",
                },
            }

        monkeypatch.setattr(main, "_safe_memory_get", lambda key, default=None: memory.get(key, default))
        monkeypatch.setattr(main, "_safe_memory_set", lambda key, value: memory.__setitem__(key, value))
        monkeypatch.setattr(main, "_read_directory_from_webspace_form", fake_read_directory)
        monkeypatch.setattr(main, "_scan_and_index", fake_scan)

        async def fake_project_snapshot(snapshot: dict, **_kwargs) -> None:
            projected.append(snapshot)

        monkeypatch.setattr(main, "_project_snapshot_async", fake_project_snapshot)
        monkeypatch.setattr(main, "_publish_operation", lambda *_args, **_kwargs: None)

        await main.on_media_indexer_action(SimpleNamespace(payload={"id": "scan", "webspace_id": "ws-1"}))

        assert seen["directory"] == str(media_dir)
        assert memory[main.SETTINGS_KEY]["selected_directory"] == str(media_dir)
        assert projected[-1]["status"]["value"] == "indexed"
        assert projected[-1]["form"]["directory"] == str(media_dir)

    asyncio.run(run_case())


def test_scan_action_projects_error_when_indexer_raises(monkeypatch, tmp_path: pathlib.Path) -> None:
    async def run_case() -> None:
        monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(tmp_path / "skill_env.json"))
        monkeypatch.setenv("MEDIA_INDEXER_DATA_DIR", str(tmp_path / "data"))

        main = importlib.import_module("handlers.main")
        main.dispose()
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        memory: dict[str, dict] = {}
        projected: list[dict] = []

        def fake_scan(directory: str, progress=None) -> dict:
            raise RuntimeError("model init failed")

        monkeypatch.setattr(main, "_safe_memory_get", lambda key, default=None: memory.get(key, default))
        monkeypatch.setattr(main, "_safe_memory_set", lambda key, value: memory.__setitem__(key, value))
        monkeypatch.setattr(main, "_scan_and_index", fake_scan)

        async def fake_project_snapshot(snapshot: dict, **_kwargs) -> None:
            projected.append(snapshot)

        monkeypatch.setattr(main, "_project_snapshot_async", fake_project_snapshot)
        monkeypatch.setattr(main, "_publish_operation", lambda *_args, **_kwargs: None)

        await main.on_media_indexer_action(
            SimpleNamespace(payload={"id": "scan", "directory": str(media_dir), "webspace_id": "ws-1"})
        )

        assert projected[-1]["status"]["value"] == "error"
        assert "model init failed" in projected[-1]["status"]["error"]
        assert main._state["scan_in_progress"] is False

    asyncio.run(run_case())


def test_scan_action_coalesces_lightweight_progress(monkeypatch, tmp_path: pathlib.Path) -> None:
    async def run_case() -> None:
        monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(tmp_path / "skill_env.json"))
        monkeypatch.setenv("MEDIA_INDEXER_DATA_DIR", str(tmp_path / "data"))

        main = importlib.import_module("handlers.main")
        main.dispose()
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        projected: list[dict] = []

        main._state["library_items"] = [{"title": "old", "details": {"large": "x" * 1000}}]
        main._state["last_results"] = [{"title": "old result"}]

        def fake_scan(directory: str, progress=None) -> dict:
            assert directory == str(media_dir)
            for idx in range(20):
                progress(
                    {
                        "value": "indexing",
                        "subtitle": f"{idx}/20 files",
                        "description": f"file-{idx}.mp4",
                        "indexed_count": idx,
                        "total_count": 20,
                    }
                )
            return {
                "status": "ok",
                "indexed_count": 20,
                "errors": [],
                "diagnostics": {
                    "indexed_count": 20,
                    "files_found": 20,
                    "by_type": {"video": 20},
                    "description": "Indexed 20 media files.",
                },
            }

        async def fake_project_snapshot(snapshot: dict, **_kwargs) -> None:
            projected.append(snapshot)

        monkeypatch.setattr(main, "_scan_and_index", fake_scan)
        monkeypatch.setattr(main, "_project_snapshot_async", fake_project_snapshot)
        monkeypatch.setattr(main, "_publish_operation", lambda *_args, **_kwargs: None)

        await main.on_media_indexer_action(
            SimpleNamespace(payload={"id": "scan", "directory": str(media_dir), "webspace_id": "ws-1"})
        )

        progress_snapshots = [item for item in projected if item["status"]["value"] == "indexing"]
        assert len(progress_snapshots) <= 2
        assert progress_snapshots
        for snapshot in progress_snapshots:
            assert snapshot["library"] == []
            assert snapshot["results"] == []
            assert snapshot["playback"]["items"] == []
        assert projected[-1]["status"]["value"] == "indexed"

    asyncio.run(run_case())


def test_play_action_keeps_directory_when_payload_path_is_media_file(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(tmp_path / "skill_env.json"))
    monkeypatch.setenv("MEDIA_INDEXER_DATA_DIR", str(tmp_path / "data"))

    main = importlib.import_module("handlers.main")
    main.dispose()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    clip = media_dir / "song.mp3"
    clip.write_bytes(b"audio")
    payload = {
        "full_path": str(clip),
        "real_file_name": clip.name,
        "display_title": "song",
        "ftype": "audio",
    }

    class FakeVectorDb:
        text_docs = [{"text": "song", "payload": payload}]
        image_docs = []

    projected: list[dict] = []

    async def fake_project_snapshot(snapshot: dict, **_kwargs) -> None:
        projected.append(snapshot)

    monkeypatch.setattr(main, "_project_snapshot_async", fake_project_snapshot)
    monkeypatch.setattr(main, "_publish_operation", lambda *_args, **_kwargs: None)
    main._state["selected_directory"] = str(clip)
    main._state["selected_query"] = "song"
    main._state["vector_db"] = FakeVectorDb()
    main._state["index_loaded"] = True

    asyncio.run(
        main.on_media_indexer_action(
            SimpleNamespace(payload={"id": "play", "path": str(clip), "webspace_id": "ws-1"})
        )
    )

    assert projected
    assert projected[-1]["status"]["value"] == "ready"
    assert projected[-1]["form"]["directory"] == str(media_dir)
    assert main._state["selected_directory"] == str(media_dir)
    assert projected[-1]["playback"]["items"][0]["content_path"].startswith("/api/node/media-indexer/content/")
    assert projected[-1]["playback"]["items"][0]["routed_content_path"].startswith("/media/media-indexer/content/")


def test_search_action_projects_compact_snapshots(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(tmp_path / "skill_env.json"))
    monkeypatch.setenv("MEDIA_INDEXER_DATA_DIR", str(tmp_path / "data"))

    main = importlib.import_module("handlers.main")
    main.dispose()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    clip = media_dir / "song.mp3"
    clip.write_bytes(b"audio")
    payload = {
        "full_path": str(clip),
        "real_file_name": clip.name,
        "display_title": "song",
        "ftype": "audio",
    }

    class FakeVectorDb:
        text_docs = [{"text": "song", "payload": payload}]
        image_docs = []

        def search(self, query: str, k: int = 5) -> list[dict]:
            return [{"score": 100.0, "type": "media/text", "payload": payload}]

    projected: list[dict] = []

    async def fake_project_snapshot(snapshot: dict, **_kwargs) -> None:
        projected.append(snapshot)

    monkeypatch.setattr(main, "_project_snapshot_async", fake_project_snapshot)
    monkeypatch.setattr(main, "_publish_operation", lambda *_args, **_kwargs: None)
    main._state["selected_directory"] = str(media_dir)
    main._state["selected_query"] = "song"
    main._state["library_items"] = [{"title": f"item-{idx}", "path": str(clip)} for idx in range(30)]
    main._state["vector_db"] = FakeVectorDb()
    main._state["index_loaded"] = True

    asyncio.run(
        main.on_media_indexer_action(
            SimpleNamespace(payload={"id": "search", "query": "song", "webspace_id": "ws-1"})
        )
    )

    assert [snapshot["status"]["value"] for snapshot in projected] == ["searching", "done"]
    assert all(snapshot["library"] == [] for snapshot in projected)
    assert projected[-1]["results"][0]["title"] == "song"
    assert projected[-1]["form"]["directory"] == str(media_dir)


def test_search_formats_results_and_dedupes_same_media_path(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(tmp_path / "skill_env.json"))
    monkeypatch.setenv("MEDIA_INDEXER_DATA_DIR", str(tmp_path / "data"))

    main = importlib.import_module("handlers.main")
    main.dispose()
    media_path = str(tmp_path / "media" / "cat.jpg")
    payload = {
        "full_path": media_path,
        "real_file_name": "cat.jpg",
        "display_title": "cat",
        "ftype": "image",
        "year": "---",
        "quality": "---",
        "artist": "",
        "ner_title": "",
        "technical_metadata": {"image_format": "JPEG"},
        "enriched": {},
    }

    class FakeVectorDb:
        text_docs = [{"text": "cat", "payload": payload}]
        image_docs = [{"text": "[VISUAL] cat.jpg", "payload": payload}]

        def search(self, query: str, k: int = 5) -> list[dict]:
            return [
                {"score": 55.0, "type": "media/text", "payload": payload},
                {"score": 72.0, "type": "image", "payload": payload},
            ]

    main._state["vector_db"] = FakeVectorDb()
    main._state["index_loaded"] = True

    result = main.search_media("cat", k=5)

    assert result["status"] == "ok"
    assert len(result["results"]) == 1
    item = result["results"][0]
    assert item["score"] == 72.0
    assert item["title"] == "cat"
    assert "score 72.0" in item["subtitle"]
    assert item["details"]["ner"]["title"] == "cat"
    assert "File: cat.jpg" in item["details_text"]
    assert "technical_metadata" not in item["details"]
    assert "enriched" not in item["details"]


def test_ner_weights_prefers_skill_runtime_models_dir(monkeypatch, tmp_path: pathlib.Path) -> None:
    model_dir = tmp_path / "data" / "files" / "models"
    model_dir.mkdir(parents=True)
    weights = model_dir / "model2.pt"
    weights.write_bytes(b"fake weights")
    monkeypatch.setenv("MEDIA_INDEXER_MODEL_DIR", str(model_dir))

    from lib.ner_predictor import model_weights_path, model_weights_status

    assert model_weights_path() == weights
    status = model_weights_status()
    assert status["path"] == str(weights)
    assert status["exists"] is True
    assert status["source"] == "skill_data_models"


def test_filename_parser_extracts_safe_demo_entities() -> None:
    from lib.filename_parser import parse_filename

    audio = parse_filename("01 Queen - Bohemian Rhapsody.mp3", "audio")
    assert audio["artist"] == "Queen"
    assert audio["title"] == "Bohemian Rhapsody"

    video = parse_filename("Inception.2010.1080p.BluRay.x264.mkv", "video")
    assert video["title"] == "Inception"
    assert video["year"] == "2010"
    assert video["quality"] == "1080p"


def test_rehydrate_restores_index_metadata_from_skill_data(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(tmp_path / "skill_env.json"))
    monkeypatch.setenv("MEDIA_INDEXER_DATA_DIR", str(tmp_path / "data"))
    index_dir = tmp_path / "data" / "faiss"
    index_dir.mkdir(parents=True)
    (index_dir / "text.index").write_bytes(b"text")
    (index_dir / "image.index").write_bytes(b"image")
    (index_dir / "metadata.json").write_text(
        json.dumps({"schema": 1, "text_count": 2, "image_count": 1, "total_count": 3}),
        encoding="utf-8",
    )

    main = importlib.import_module("handlers.main")
    main.dispose()

    result = main.rehydrate()

    assert result["index"]["restored_from"] == "skill_data"
    assert result["index"]["total_count"] == 3
    stored = json.loads((tmp_path / "skill_env.json").read_text(encoding="utf-8"))
    assert stored["media_indexer.index"]["index_dir"] == str(index_dir)


def test_empty_scan_clears_stale_skill_data_index(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(tmp_path / "skill_env.json"))
    monkeypatch.setenv("MEDIA_INDEXER_DATA_DIR", str(tmp_path / "data"))
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    index_dir = tmp_path / "data" / "faiss"
    index_dir.mkdir(parents=True)
    for filename in ("metadata.json", "text.index", "image.index"):
        (index_dir / filename).write_bytes(b"stale")

    main = importlib.import_module("handlers.main")
    reset_seen = {"called": False}
    main._state["vector_db"] = SimpleNamespace(reset=lambda: reset_seen.__setitem__("called", True))

    class EmptyScanner:
        def __init__(self, directory: str, compute_hashes: bool = False) -> None:
            self.directory = directory
            self.compute_hashes = compute_hashes

        def scan(self) -> dict:
            return {}

    monkeypatch.setattr(main, "DirectoryScanner", EmptyScanner)

    result = main._scan_and_index(str(media_dir))

    assert result["status"] == "ok"
    assert result["indexed_count"] == 0
    assert result["index"]["cleared"] is True
    assert not (index_dir / "metadata.json").exists()
    assert reset_seen["called"] is True
    stored = json.loads((tmp_path / "skill_env.json").read_text(encoding="utf-8"))
    assert stored["media_indexer.index"]["indexed_count"] == 0


def test_scan_resets_loaded_vector_index_before_reindexing(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(tmp_path / "skill_env.json"))
    monkeypatch.setenv("MEDIA_INDEXER_DATA_DIR", str(tmp_path / "data"))
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_path = media_dir / "clip.mp4"
    media_path.write_bytes(b"fake")

    main = importlib.import_module("handlers.main")

    class OneFileScanner:
        def __init__(self, directory: str, compute_hashes: bool = False) -> None:
            self.directory = directory
            self.compute_hashes = compute_hashes

        def scan(self) -> dict:
            return {"video": [SimpleNamespace(name="clip.mp4", full_path=str(media_path))]}

    class FakeVectorDb:
        def __init__(self) -> None:
            self.reset_called = False
            self.text_docs = [{"text": "old", "payload": {}}]
            self.image_docs = []

        def reset(self) -> None:
            self.reset_called = True
            self.text_docs = []
            self.image_docs = []

        def add_text(self, text: str, payload: dict) -> None:
            self.text_docs.append({"text": text, "payload": payload})

        def save(self, directory: str | pathlib.Path) -> dict:
            return {"schema": 1, "text_count": len(self.text_docs), "image_count": 0, "total_count": len(self.text_docs)}

    fake_vector = FakeVectorDb()

    def fake_ensure_initialized(*, load_index: bool = False) -> None:
        main._state.update(
            {
                "extractor": SimpleNamespace(extract=lambda *_args, **_kwargs: SimpleNamespace(to_dict=lambda: {})),
                "ner": SimpleNamespace(extract_entities=lambda _name: {}),
                "enricher": SimpleNamespace(enrich=lambda *_args, **_kwargs: {}, enrich_video=lambda *_args, **_kwargs: {}),
                "vector_db": fake_vector,
            }
        )

    monkeypatch.setattr(main, "DirectoryScanner", OneFileScanner)
    monkeypatch.setattr(main, "_ensure_initialized", fake_ensure_initialized)

    result = main._scan_and_index(str(media_dir))

    assert fake_vector.reset_called is True
    assert result["index"]["text_count"] == 1
    assert result["diagnostics"]["by_type"] == {"video": 1}
    assert result["diagnostics"]["ner_parsed"] == 1
    assert result["diagnostics"]["indexed_count"] == 1
    payload = fake_vector.text_docs[0]["payload"]
    assert payload["playback_id"]
    assert payload["content_path"].startswith("/api/node/media-indexer/content/")


def test_vector_db_uses_lexical_backend_by_default(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.delenv("MEDIA_INDEXER_ENABLE_ML", raising=False)
    monkeypatch.delenv("MEDIA_INDEXER_ENABLE_TEXT_EMBEDDINGS", raising=False)
    monkeypatch.delenv("MEDIA_INDEXER_ENABLE_IMAGE_EMBEDDINGS", raising=False)

    from lib.vector_db import VectorDatabase

    db = VectorDatabase()
    db.add_text("Queen Bohemian Rhapsody music audio", {"title": "Bohemian Rhapsody"})

    results = db.search("queen", k=5)
    assert results
    assert results[0]["payload"]["title"] == "Bohemian Rhapsody"
    assert results[0]["type"] == "media/text"

    metadata = db.save(tmp_path)
    assert metadata["backend"] == "lexical"
    assert not (tmp_path / "text.index").exists()

    restored = VectorDatabase()
    assert restored.load(tmp_path)["loaded"] is True
    assert restored.search("rhapsody", k=5)[0]["payload"]["title"] == "Bohemian Rhapsody"


def test_global_ml_flag_does_not_enable_heavy_media_indexer_features(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_INDEXER_ENABLE_ML", "1")
    monkeypatch.delenv("MEDIA_INDEXER_ENABLE_TEXT_EMBEDDINGS", raising=False)
    monkeypatch.delenv("MEDIA_INDEXER_ENABLE_IMAGE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("MEDIA_INDEXER_ENABLE_AUDIO_ID", raising=False)
    monkeypatch.delenv("MEDIA_INDEXER_ENABLE_NER", raising=False)

    main = importlib.import_module("handlers.main")
    from lib.vector_db import VectorDatabase

    assert main._feature_enabled("MEDIA_INDEXER_ENABLE_AUDIO_ID") is False
    assert main._feature_enabled("MEDIA_INDEXER_ENABLE_NER") is False
    db = VectorDatabase()
    assert db.text_embeddings_enabled is False
    assert db.faiss is None


def _load_media_indexer_library():
    module_path = next(
        (
            candidate / "src" / "adaos" / "services" / "media_indexer_library.py"
            for candidate in [SKILL_ROOT, *SKILL_ROOT.parents, pathlib.Path("/root/adaos")]
            if (candidate / "src" / "adaos" / "services" / "media_indexer_library.py").exists()
        ),
        None,
    )
    if module_path is not None:
        spec = importlib.util.spec_from_file_location("media_indexer_library_under_test", module_path)
        assert spec and spec.loader
        library = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(library)
    else:
        library = importlib.import_module("adaos.services.media_indexer_library")
    return library


def test_media_indexer_playback_resolver_uses_state_metadata_path(monkeypatch, tmp_path: pathlib.Path) -> None:
    library = _load_media_indexer_library()
    base_dir = tmp_path / "adaos"
    skills_dir = tmp_path / "skills"
    monkeypatch.delenv("MEDIA_INDEXER_DATA_DIR", raising=False)
    monkeypatch.setattr(
        library,
        "get_ctx",
        lambda: SimpleNamespace(
            paths=SimpleNamespace(
                base_dir=lambda: base_dir,
                skills_workspace_dir=lambda: skills_dir,
            )
        ),
    )

    assert base_dir / "state" / "media_indexer_skill" / "internal" / "faiss" / "metadata.json" in library._metadata_candidates()


def test_media_indexer_playback_resolver_requires_indexed_root(monkeypatch, tmp_path: pathlib.Path) -> None:
    library = _load_media_indexer_library()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    clip = media_dir / "clip.mp4"
    clip.write_bytes(b"fake")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"fake")
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "indexed_directory": str(media_dir),
                "text_docs": [
                    {"payload": {"playback_id": "a" * 32, "full_path": str(clip), "mime_type": "video/mp4"}},
                    {"payload": {"playback_id": "b" * 32, "full_path": str(outside), "mime_type": "video/mp4"}},
                ],
                "image_docs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(library, "_metadata_candidates", lambda: [metadata_path])

    resolved, payload = library.resolve_media_indexer_content("a" * 32)
    assert resolved == clip.resolve()
    assert payload["mime_type"] == "video/mp4"
    resolved_by_name, payload_by_name = library.resolve_media_indexer_content_by_name("clip.mp4")
    assert resolved_by_name == clip.resolve()
    assert payload_by_name["playback_id"] == "a" * 32
    with pytest.raises(PermissionError):
        library.resolve_media_indexer_content("b" * 32)
    with pytest.raises(PermissionError):
        library.resolve_media_indexer_content_by_name("outside.mp4")
