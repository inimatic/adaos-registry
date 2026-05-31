from __future__ import annotations

import importlib
import json
import pathlib
import sys
import types
from types import SimpleNamespace

import yaml


SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


def test_manifest_declares_runtime_contracts() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))

    assert manifest["name"] == "media_indexer_skill"
    assert "requirements.txt" not in {path.name for path in SKILL_ROOT.iterdir()}
    assert "faiss-cpu==1.13.2" in manifest["dependencies"]
    assert "torch==2.10.0" in manifest["dependencies"]
    weights = manifest["models"]["artifacts"]["weights"]
    assert weights["path"] == "ml/weights/model2.pt"
    assert weights["install_path"] == "data/files/models/model2.pt"
    assert "media_indexer.action" in manifest["events"]["subscribe"]
    assert "webio.stream.snapshot.requested" in manifest["events"]["subscribe"]
    assert any(route["route"] == "stream" and route["receiver"] == "media_indexer.operations" for route in manifest["data_routes"])
    assert manifest["lifecycle"]["rehydrate"] == "rehydrate"
    assert manifest["lifecycle"]["dispose"] == "dispose"


def test_webui_declares_compact_yjs_and_stream_receiver() -> None:
    webui = json.loads((SKILL_ROOT / "webui.json").read_text(encoding="utf-8"))

    assert webui["ydoc_defaults"]["data/media_indexer"]["form"]["directory"] == ""
    assert "D:\\diploma_final\\demo_media" not in json.dumps(webui)
    defaults = webui["ydoc_defaults"]["data/media_indexer"]
    assert defaults["diagnostics"]["label"] == "Model diagnostics"
    receiver = webui["webio"]["receivers"]["media_indexer.operations"]
    assert receiver["mode"] == "replace"
    assert receiver["snapshotPolicy"] == "on_subscribe"
    results_widget = next(
        widget
        for widget in webui["registry"]["modals"]["media_indexer_modal"]["schema"]["widgets"]
        if widget["id"] == "media-indexer-results"
    )
    assert results_widget["inputs"]["titleKey"] == "title"
    assert results_widget["inputs"]["subtitleKey"] == "subtitle"


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
                "enricher": SimpleNamespace(enrich=lambda *_args, **_kwargs: {}),
                "vector_db": fake_vector,
            }
        )

    monkeypatch.setattr(main, "DirectoryScanner", OneFileScanner)
    monkeypatch.setattr(main, "_ensure_initialized", fake_ensure_initialized)

    result = main._scan_and_index(str(media_dir))

    assert fake_vector.reset_called is True
    assert result["index"]["text_count"] == 1
    assert result["diagnostics"]["by_type"] == {"video": 1}
    assert result["diagnostics"]["ner_parsed"] == 0
    assert result["diagnostics"]["indexed_count"] == 1
