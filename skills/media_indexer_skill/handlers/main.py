"""AdaOS media indexer skill handlers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List

import yaml

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet
from adaos.sdk.data import skill_memory
from adaos.sdk.data.context import clear_current_skill, set_current_skill
from adaos.sdk.data.skill_env import skill_env_path
from adaos.sdk.io.out import stream_publish
from adaos.services.agent_context import get_ctx

_SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

_SERVICE_SITE_PACKAGES_READY = False

from lib.enrichment import EnrichmentService
from lib.extractor import TechnicalMetadataExtractor
from lib.filename_parser import merge_entities, parse_filename
from lib.ner_predictor import NERPredictor, model_weights_status
from lib.scanner import DirectoryScanner
from lib.vector_db import VectorDatabase

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("invalid float env %s=%r, using %.1f", name, raw, default)
        return default


REQUIRES_DATA_PROJECTIONS = True
SCORE_THRESHOLD = 25.0
DEFAULT_QUERY = ""
SETTINGS_KEY = "media_indexer.settings"
INDEX_META_KEY = "media_indexer.index"
INDEX_SUMMARY_FILENAME = "summary.json"
PLAYBACK_INDEX_FILENAME = "playback.sqlite3"
OPERATION_RECEIVER = "media_indexer.operations"
MAX_RESULTS = 20
SNAPSHOT_LIBRARY_LIMIT = 25
PROGRESS_MIN_INTERVAL_SEC = 1.0
PROJECTION_TIMEOUT_SEC = _env_float("MEDIA_INDEXER_PROJECTION_TIMEOUT_SEC", 2.0)
PROGRESS_QUEUE_MAXSIZE = 1
STATUS_COLORS = {
    "ready": "#6EE7B7",
    "scanning": "#60A5FA",
    "loading": "#FBBF24",
    "indexing": "#60A5FA",
    "indexed": "#34D399",
    "searching": "#A78BFA",
    "done": "#6EE7B7",
    "busy": "#FBBF24",
    "error": "#FB7185",
}
STATUS_LABELS = {
    "ready": "Готово",
    "scanning": "Сканирование",
    "loading": "Загрузка",
    "indexing": "Индексация",
    "indexed": "Готово",
    "searching": "Поиск",
    "done": "Готово",
    "busy": "Занято",
    "error": "Ошибка",
}
TYPE_LABELS = {
    "audio": "аудио",
    "image": "изображения",
    "video": "видео",
    "media": "медиа",
    "media/text": "текстовые признаки",
}

# Keep the public UI English, matching the skill name and the rest of AdaOS.
STATUS_LABELS = {
    "ready": "Ready",
    "scanning": "Scanning",
    "loading": "Loading",
    "indexing": "Indexing",
    "indexed": "Indexed",
    "searching": "Searching",
    "done": "Done",
    "busy": "Busy",
    "error": "Error",
}
TYPE_LABELS = {
    "audio": "Audio",
    "image": "Image",
    "video": "Video",
    "media": "Media",
    "media/text": "Text match",
}

_state: Dict[str, Any] = {
    "scanner": None,
    "extractor": None,
    "ner": None,
    "enricher": None,
    "vector_db": None,
    "indexed_directory": None,
    "selected_directory": "",
    "selected_query": DEFAULT_QUERY,
    "index_loaded": False,
    "last_operation": None,
    "last_diagnostics": None,
    "library_items": [],
    "last_results": [],
    "playback": None,
    "scan_in_progress": False,
}


class _NoopNERPredictor:
    def extract_entities(self, text: str) -> Dict[str, str]:
        return {}


def _feature_enabled(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _light_mode_enabled() -> bool:
    return not _feature_enabled("MEDIA_INDEXER_ENABLE_ML")


def _technical_metadata_enabled() -> bool:
    return _feature_enabled("MEDIA_INDEXER_ENABLE_TECHNICAL_METADATA", default=False)


def _skill_version() -> str:
    env_version = str(os.getenv("ADAOS_SKILL_VERSION") or "").strip()
    if env_version:
        return env_version
    manifest_path = _SKILL_ROOT / "skill.yaml"
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return "0.0.0"
    return str(manifest.get("version") or "0.0.0")

def _event_payload(evt: Any) -> Dict[str, Any]:
    payload = getattr(evt, "payload", None) if hasattr(evt, "payload") else evt
    return payload if isinstance(payload, dict) else {}


def _safe_memory_get(key: str, default: Any = None) -> Any:
    try:
        return skill_memory.get(key, default)
    except Exception:
        return default


def _safe_memory_set(key: str, value: Any) -> None:
    try:
        skill_memory.set(key, value)
    except Exception:
        logger.debug("failed to write skill memory key=%s", key, exc_info=True)


def _directory_value_or_parent(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw.startswith("$"):
        return ""
    try:
        path = pathlib.Path(raw).expanduser()
        if path.exists() and path.is_file():
            return str(path.parent)
        if not path.exists() and _looks_lossy_path(raw):
            repaired = _directory_from_index_for_lossy_path(raw)
            if repaired:
                return repaired
    except Exception:
        return raw
    return raw


def _looks_lossy_path(value: str) -> bool:
    return "?" in value or "\ufffd" in value


def _directory_from_index_for_lossy_path(value: str) -> str:
    metadata = _read_persisted_index_metadata()
    indexed_directory = str(metadata.get("indexed_directory") or "").strip()
    if not indexed_directory:
        return ""
    try:
        indexed_root = pathlib.Path(indexed_directory).expanduser()
        if indexed_root.exists() and indexed_root.is_dir():
            return str(indexed_root)
    except Exception:
        return ""
    return ""


def _internal_data_dir() -> pathlib.Path:
    override = os.getenv("MEDIA_INDEXER_DATA_DIR")
    if override:
        path = pathlib.Path(override)
    else:
        def absolute_base(value: Any) -> pathlib.Path | None:
            raw = str(value or "").strip()
            if not raw:
                return None
            candidate = pathlib.Path(raw).expanduser()
            return candidate if candidate.is_absolute() else None

        env_base = absolute_base(os.getenv("ADAOS_BASE_DIR"))
        if env_base is not None:
            path = env_base / "state" / "media_indexer_skill" / "internal"
        else:
            candidates: List[pathlib.Path] = []
            try:
                ctx = get_ctx()
                base_dir_value = ctx.paths.base_dir()
                ctx_base = absolute_base(base_dir_value() if callable(base_dir_value) else base_dir_value)
                if ctx_base is not None:
                    candidates.append(ctx_base)
            except Exception:
                pass
            home_base = pathlib.Path.home().expanduser() / ".adaos"
            if home_base not in candidates:
                candidates.append(home_base)
            selected = candidates[0]
            for candidate in candidates:
                candidate_path = candidate / "state" / "media_indexer_skill" / "internal"
                if (candidate_path / "faiss" / "metadata.json").exists():
                    selected = candidate
                    break
            path = selected / "state" / "media_indexer_skill" / "internal"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _index_dir() -> pathlib.Path:
    path = _internal_data_dir() / "faiss"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _has_persisted_index() -> bool:
    path = _index_dir()
    metadata_path = path / INDEX_SUMMARY_FILENAME
    if not metadata_path.exists():
        metadata_path = path / "metadata.json"
    if not metadata_path.exists():
        return False
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to read persisted media index metadata", exc_info=True)
        return False
    if str(metadata.get("backend") or "faiss") == "lexical":
        return True
    return (path / "text.index").exists() and (path / "image.index").exists()


def _read_persisted_index_metadata() -> Dict[str, Any]:
    path = _index_dir()
    metadata_path = path / INDEX_SUMMARY_FILENAME
    if not metadata_path.exists():
        metadata_path = path / "metadata.json"
    if not metadata_path.exists():
        return {}
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to read persisted media index metadata", exc_info=True)
        return {}
    if not isinstance(metadata, dict):
        return {}
    payload = {
        "indexed_directory": "",
        "indexed_count": int(metadata.get("total_count") or metadata.get("text_count") or 0),
        "index_dir": str(path),
        "restored_from": "skill_data",
        **metadata,
    }
    return payload


def _compact_index_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(metadata, dict) or not metadata:
        return {}
    compact = dict(metadata)
    compact.pop("text_docs", None)
    compact.pop("image_docs", None)
    compact.setdefault("indexed_count", int(metadata.get("indexed_count") or metadata.get("total_count") or metadata.get("text_count") or 0))
    compact.setdefault("text_count", int(metadata.get("text_count") or compact.get("indexed_count") or 0))
    compact.setdefault("image_count", int(metadata.get("image_count") or 0))
    compact.setdefault("total_count", int(metadata.get("total_count") or compact.get("indexed_count") or 0))
    return compact


def _index_metadata() -> Dict[str, Any]:
    if _has_persisted_index():
        restored = _read_persisted_index_metadata()
        if restored:
            compact = _compact_index_metadata(restored)
            stored = _safe_memory_get(INDEX_META_KEY, {})
            if compact != stored:
                _safe_memory_set(INDEX_META_KEY, compact)
            return compact
    stored = _safe_memory_get(INDEX_META_KEY, {})
    if isinstance(stored, dict) and stored:
        compact = _compact_index_metadata(stored)
        if compact != stored:
            _safe_memory_set(INDEX_META_KEY, compact)
        return compact
    return {}


def _write_index_sidecars(metadata: Dict[str, Any]) -> None:
    compact = _compact_index_metadata(metadata)
    (_index_dir() / INDEX_SUMMARY_FILENAME).write_text(
        json.dumps(compact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    playback_path = _index_dir() / PLAYBACK_INDEX_FILENAME
    temporary_path = playback_path.with_suffix(".tmp")
    temporary_path.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary_path)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE items (playback_id TEXT PRIMARY KEY, name TEXT NOT NULL, full_path TEXT NOT NULL, payload_json TEXT NOT NULL)"
        )
        connection.execute("CREATE INDEX idx_items_name ON items(name)")
        connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            ("indexed_directory", str(metadata.get("indexed_directory") or "")),
        )
        rows = []
        for doc_key in ("text_docs", "image_docs"):
            for doc in metadata.get(doc_key) or []:
                payload = doc.get("payload") if isinstance(doc, dict) else None
                if not isinstance(payload, dict):
                    continue
                full_path = str(payload.get("full_path") or "").strip()
                if not full_path:
                    continue
                playback_id = str(payload.get("playback_id") or _playback_id(full_path))
                name = str(payload.get("real_file_name") or pathlib.Path(full_path).name)
                playback_payload = {
                    **payload,
                    "playback_id": playback_id,
                    "real_file_name": name,
                    "full_path": full_path,
                }
                rows.append((playback_id, name, full_path, json.dumps(playback_payload, ensure_ascii=False)))
                if len(rows) >= 1000:
                    connection.executemany("INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?)", rows)
                    rows.clear()
        if rows:
            connection.executemany("INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?)", rows)
        connection.commit()
    finally:
        connection.close()
    temporary_path.replace(playback_path)


def _settings() -> Dict[str, Any]:
    stored = _safe_memory_get(SETTINGS_KEY, {})
    settings = dict(stored) if isinstance(stored, dict) else {}
    settings.setdefault("default_directory", "")
    settings.setdefault("selected_directory", "")
    settings.setdefault("selected_query", DEFAULT_QUERY)
    settings.setdefault("k", 5)
    repaired = False
    for key in ("default_directory", "selected_directory"):
        current = settings.get(key)
        normalized = _directory_value_or_parent(current)
        if normalized != current:
            settings[key] = normalized
            repaired = True
    if repaired:
        _safe_memory_set(SETTINGS_KEY, settings)
    return settings


def _save_settings(**updates: Any) -> Dict[str, Any]:
    settings = _settings()
    for key, value in updates.items():
        if value is not None:
            if key in {"default_directory", "selected_directory"}:
                value = _directory_value_or_parent(value)
            settings[key] = value
    _safe_memory_set(SETTINGS_KEY, settings)
    return settings


def _target_context(payload: Dict[str, Any]) -> tuple[bool, str | None]:
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    target_node_id = str(
        payload.get("target_node_id")
        or payload.get("node_id")
        or meta.get("target_node_id")
        or meta.get("node_target_id")
        or ""
    ).strip()
    try:
        local_node_id = str(getattr(get_ctx().config, "node_id", "") or "").strip()
    except Exception:
        local_node_id = ""
    if target_node_id and local_node_id and target_node_id != local_node_id:
        return False, None
    raw_ws = payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id") or meta.get("workspace_id")
    return True, str(raw_ws).strip() if raw_ws else None


def _load_skill_data_projections() -> None:
    try:
        ctx = get_ctx()
        try:
            existing = ctx.projections.resolve("subnet", "media_indexer.snapshot")
        except Exception:
            existing = []
        if existing:
            return
        skills_root = ctx.paths.skills_workspace_dir()
        skills_root = skills_root() if callable(skills_root) else skills_root
        manifest_path = pathlib.Path(skills_root) / "media_indexer_skill" / "skill.yaml"
        if not manifest_path.exists():
            return
        spec = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        entries = spec.get("data_projections") or []
        if isinstance(entries, list) and entries:
            ctx.projections.load_entries(entries)
    except Exception:
        logger.debug("failed to load media_indexer_skill data_projections", exc_info=True)


def _current_form(directory: str | None = None, query: str | None = None, k: int | None = None) -> Dict[str, Any]:
    settings = _settings()
    selected_directory = directory if directory is not None else (
        _state.get("selected_directory") or settings.get("selected_directory") or settings.get("default_directory") or ""
    )
    selected_directory = _directory_value_or_parent(selected_directory)
    selected_query = query if query is not None else (_state.get("selected_query") or settings.get("selected_query") or DEFAULT_QUERY)
    return {"directory": selected_directory, "query": selected_query, "k": int(k or settings.get("k") or 5)}


def _clean_directory_value(value: Any) -> str:
    return _directory_value_or_parent(value)


def _directory_from_payload(payload: Dict[str, Any], *, include_path: bool = False) -> str:
    keys = ("directory", "path", "value") if include_path else ("directory", "value")
    for key in keys:
        if key in payload:
            raw = _clean_directory_value(payload.get(key))
            if raw:
                return raw
    form = payload.get("form")
    if isinstance(form, dict):
        return _clean_directory_value(form.get("directory"))
    return ""


def _payload_has_directory(payload: Dict[str, Any], *, include_path: bool = False) -> bool:
    return bool(_directory_from_payload(payload, include_path=include_path))


def _resolve_directory(payload: Dict[str, Any], *, include_path: bool = False) -> str:
    raw = _directory_from_payload(payload, include_path=include_path)
    if not raw:
        raw = str(_current_form().get("directory") or "").strip()
    return raw


def _get_nested_value(root: Any, parts: List[str]) -> Any:
    current = root
    for part in parts:
        if current is None:
            return None
        if hasattr(current, "get"):
            current = current.get(part)
        else:
            return None
    if hasattr(current, "to_json"):
        try:
            return current.to_json()
        except Exception:
            return current
    return current


def _target_node_candidates(payload: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    for key in ("target_node_id", "node_id", "nodeId"):
        value = payload.get(key) or meta.get(key)
        if value:
            candidates.append(str(value))
    try:
        node_id = getattr(get_ctx().config, "node_id", None)
        if node_id:
            candidates.append(str(node_id))
    except Exception:
        pass
    return list(dict.fromkeys(candidates))


async def _read_directory_from_webspace_form(webspace_id: str | None, payload: Dict[str, Any]) -> str:
    if not webspace_id:
        return ""
    try:
        from adaos.services.yjs.doc import async_read_ydoc
    except Exception:
        logger.debug("Yjs reader is not available for media indexer form lookup", exc_info=True)
        return ""

    paths: List[List[str]] = []
    for node_id in _target_node_candidates(payload):
        paths.append(["nodes", node_id, "media_indexer", "form"])
    paths.append(["media_indexer", "form"])

    try:
        async with async_read_ydoc(webspace_id) as doc:
            data = doc.get_map("data")
            for path in paths:
                form = _get_nested_value(data, path)
                if isinstance(form, dict):
                    directory = await asyncio.to_thread(
                        _clean_directory_value,
                        form.get("directory"),
                    )
                    if directory:
                        return directory
    except Exception:
        logger.debug("Failed to read media indexer form from Yjs", exc_info=True)
    return ""


def _resolve_query(payload: Dict[str, Any]) -> str:
    raw = str(payload.get("query") or "").strip()
    if raw.startswith("$"):
        raw = ""
    return raw or str(_current_form().get("query") or DEFAULT_QUERY).strip()


def _action_selection(payload: Dict[str, Any], *, include_path: bool) -> tuple[str, str, bool]:
    return (
        _resolve_directory(payload, include_path=include_path),
        _resolve_query(payload),
        _payload_has_directory(payload, include_path=include_path),
    )


def _persist_action_selection(*, directory: str, query: str, k: int) -> Dict[str, Any]:
    _state["selected_directory"] = directory
    _state["selected_query"] = query
    _save_settings(selected_directory=directory, selected_query=query, k=k)
    return _current_form(directory=directory, query=query, k=k)


def _resolve_playback_action(payload: Dict[str, Any]) -> tuple[str, Dict[str, Any] | None, str, bool]:
    if _has_persisted_index():
        _ensure_initialized(load_index=True)
    selected_path = _path_from_action_payload(payload)
    selected_payload, playback_path = _resolve_playback_payload(selected_path)
    exists = pathlib.Path(playback_path).is_file() if playback_path else False
    return selected_path, selected_payload, playback_path, exists


def _status_payload(
    *,
    value: str,
    subtitle: str,
    description: str,
    error: str = "",
    indexed_count: int | None = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "value": value,
        "display_value": STATUS_LABELS.get(str(value).lower(), value),
        "label": "Media Indexer",
        "subtitle": subtitle,
        "description": description,
        "error": error,
        "color": STATUS_COLORS.get(str(value).lower(), "#93C5FD"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if indexed_count is not None:
        payload["indexed_count"] = int(indexed_count)
    return payload


def _empty_overview() -> Dict[str, Any]:
    return {
        "value": "No index",
        "label": "Media Indexer",
        "subtitle": "Video, audio and image semantic search",
        "description": "Choose a local media folder, build the index, then search by title, artist, filename clues or media type.",
        "color": "#93C5FD",
    }


def _overview_payload(status: Dict[str, Any], diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    indexed = int(diagnostics.get("indexed_count") or 0)
    found = int(diagnostics.get("files_found") or indexed or 0)
    by_type = diagnostics.get("by_type") if isinstance(diagnostics.get("by_type"), dict) else {}
    video_count = int(by_type.get("video") or 0)
    audio_count = int(by_type.get("audio") or 0)
    image_count = int(by_type.get("image") or 0)
    status_value = str(status.get("value") or "ready").lower()
    if status_value in {"scanning", "loading", "indexing"}:
        value = STATUS_LABELS.get(status_value, "Indexing")
        subtitle = str(status.get("subtitle") or "Building semantic index")
    elif indexed:
        value = f"{indexed} files"
        subtitle = "Media library is ready"
    else:
        value = "No index"
        subtitle = "Waiting for scan"
    description = str(status.get("description") or "").strip()
    if indexed and status_value not in {"scanning", "loading", "indexing"}:
        if _light_mode_enabled():
            description = f"{video_count} video, {audio_count} audio, {image_count} images. Search uses filenames, metadata and a lightweight lexical index."
        else:
            description = f"{video_count} video, {audio_count} audio, {image_count} images. Search is powered by NER, enrichment metadata and embeddings."
    return {
        "value": value,
        "label": "Library overview",
        "subtitle": subtitle,
        "description": description or _empty_overview()["description"],
        "color": STATUS_COLORS.get(status_value, diagnostics.get("color") or "#93C5FD"),
    }


def _snapshot_payload_with_form(
    *,
    status: Dict[str, Any],
    form: Dict[str, Any],
    results: List[Dict[str, Any]] | None = None,
    diagnostics: Dict[str, Any] | None = None,
    include_library: bool = True,
    include_results: bool = True,
    include_playback: bool = True,
) -> Dict[str, Any]:
    diagnostics_payload = diagnostics or _state.get("last_diagnostics") or _empty_diagnostics()
    result_items = list(results if results is not None else (_state.get("last_results") or []))[:MAX_RESULTS] if include_results else []
    playback = (
        _state.get("playback") or _empty_playback_snapshot()
    ) if include_playback else _empty_playback_snapshot()
    payload = {
        "status": status,
        "overview": _overview_payload(status, diagnostics_payload),
        "form": form,
        "results": result_items,
        "playback": playback,
        "diagnostics": diagnostics_payload,
        "library": list(_state.get("library_items") or [])[:SNAPSHOT_LIBRARY_LIMIT] if include_library else [],
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return payload


def _snapshot_payload(
    *,
    status: Dict[str, Any],
    form: Dict[str, Any] | None = None,
    results: List[Dict[str, Any]] | None = None,
    diagnostics: Dict[str, Any] | None = None,
    include_library: bool = True,
    include_results: bool = True,
    include_playback: bool = True,
) -> Dict[str, Any]:
    return _snapshot_payload_with_form(
        status=status,
        form=form or _current_form(),
        results=results,
        diagnostics=diagnostics,
        include_library=include_library,
        include_results=include_results,
        include_playback=include_playback,
    )


def _progress_snapshot_payload(*, status: Dict[str, Any], form: Dict[str, Any]) -> Dict[str, Any]:
    return _snapshot_payload_with_form(
        status=status,
        form=form,
        include_library=False,
        include_results=False,
        include_playback=False,
    )


def _project_snapshot(snapshot: Dict[str, Any], *, webspace_id: str | None = None) -> None:
    pushed = False
    try:
        _load_skill_data_projections()
        pushed = set_current_skill("media_indexer_skill")
        ctx_subnet.set("media_indexer.snapshot", snapshot, webspace_id=webspace_id)
    except Exception:
        logger.warning("failed to project media_indexer.snapshot", exc_info=True)
    finally:
        if pushed:
            clear_current_skill()


async def _project_snapshot_async(snapshot: Dict[str, Any], *, webspace_id: str | None = None) -> None:
    pushed = False
    try:
        await asyncio.to_thread(_load_skill_data_projections)
        pushed = set_current_skill("media_indexer_skill")
        set_async = getattr(ctx_subnet, "set_async", None)
        if callable(set_async):
            await asyncio.wait_for(
                set_async("media_indexer.snapshot", snapshot, webspace_id=webspace_id),
                timeout=max(0.1, PROJECTION_TIMEOUT_SEC),
            )
        else:
            await asyncio.wait_for(
                asyncio.to_thread(_project_snapshot, snapshot, webspace_id=webspace_id),
                timeout=max(0.1, PROJECTION_TIMEOUT_SEC),
            )
        status = snapshot.get("status") if isinstance(snapshot.get("status"), dict) else {}
        form = snapshot.get("form") if isinstance(snapshot.get("form"), dict) else {}
        logger.info(
            "projected media_indexer.snapshot webspace=%s status=%s directory=%s",
            webspace_id or "default",
            status.get("value") or "-",
            form.get("directory") or "",
        )
    except asyncio.TimeoutError:
        logger.warning(
            "timed out projecting media_indexer.snapshot webspace=%s timeout_sec=%.1f",
            webspace_id or "default",
            PROJECTION_TIMEOUT_SEC,
        )
    except Exception:
        logger.warning("failed to project media_indexer.snapshot", exc_info=True)
    finally:
        if pushed:
            clear_current_skill()


def _publish_operation(value: Dict[str, Any], *, webspace_id: str | None = None) -> None:
    status_value = str(value.get("value") or "ready").lower()
    payload = {
        "label": "Media Indexer",
        "display_value": STATUS_LABELS.get(status_value, value.get("value") or "ready"),
        "color": STATUS_COLORS.get(status_value, "#93C5FD"),
        **value,
        "updated_at": time.time(),
    }
    _state["last_operation"] = payload
    try:
        stream_publish(
            OPERATION_RECEIVER,
            payload,
            _meta={"webspace_id": webspace_id} if webspace_id else None,
        )
    except Exception:
        logger.debug("failed to publish media indexer operation stream", exc_info=True)


def _ensure_service_site_packages() -> None:
    global _SERVICE_SITE_PACKAGES_READY

    if _SERVICE_SITE_PACKAGES_READY:
        return
    _SERVICE_SITE_PACKAGES_READY = True

    parts = _SKILL_ROOT.parts
    try:
        slots_index = parts.index("slots")
    except ValueError:
        return

    runtime_root = pathlib.Path(*parts[:slots_index])
    venv_root = runtime_root / "venv"
    py_tag = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [
        venv_root / "lib" / py_tag / "site-packages",
        venv_root / "Lib" / "site-packages",
    ]
    for site_packages in candidates:
        if not site_packages.exists():
            continue
        site_path = str(site_packages)
        if site_path not in sys.path:
            sys.path.insert(1, site_path)
            logger.info("Added media_indexer service site-packages: %s", site_path)
        return


def _runtime_root_from_skill_root() -> pathlib.Path | None:
    parts = _SKILL_ROOT.parts
    try:
        slots_index = parts.index("slots")
    except ValueError:
        return None
    return pathlib.Path(*parts[:slots_index])


def _service_python() -> str:
    runtime_root = _runtime_root_from_skill_root()
    if runtime_root is None:
        return sys.executable
    candidates = [
        runtime_root / "venv" / "bin" / "python",
        runtime_root / "venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _worker_env() -> Dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    paths = [str(_SKILL_ROOT)]
    try:
        vendor = _SKILL_ROOT.parents[2] / "vendor"
        if vendor.is_dir():
            paths.append(str(vendor))
    except Exception:
        pass
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _run_json_worker(script: pathlib.Path, request: Dict[str, Any], *, timeout_sec: float) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            [_service_python(), str(script)],
            input=json.dumps(request, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_worker_env(),
            timeout=max(1.0, float(timeout_sec)),
        )
    except Exception as exc:
        logger.warning("Media indexer worker failed to start %s: %s", script.name, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "results": {}}
    if proc.returncode != 0:
        logger.warning(
            "Media indexer worker %s exited with %s: %s",
            script.name,
            proc.returncode,
            (proc.stderr or proc.stdout or "").strip()[:2000],
        )
        return {"ok": False, "error": (proc.stderr or proc.stdout or "").strip(), "results": {}}
    try:
        return json.loads(proc.stdout or "{}")
    except Exception as exc:
        logger.warning("Media indexer worker %s returned invalid JSON: %s", script.name, exc)
        return {"ok": False, "error": "invalid_json", "results": {}}


def _run_ner_worker(all_files: List[tuple[Any, str]]) -> Dict[str, Dict[str, Any]]:
    if not _feature_enabled("MEDIA_INDEXER_ENABLE_NER"):
        return {}
    script = _SKILL_ROOT / "workers" / "ner_worker.py"
    if not script.exists():
        return {}
    request = {
        "items": [
            {"name": getattr(media, "name", ""), "path": getattr(media, "full_path", ""), "type": ftype}
            for media, ftype in all_files
        ]
    }
    timeout = float(os.getenv("MEDIA_INDEXER_NER_TIMEOUT_SEC") or 180)
    result = _run_json_worker(script, request, timeout_sec=timeout)
    if not result.get("ok"):
        logger.warning("NER worker unavailable: %s", result.get("error") or "unknown_error")
        return {}
    raw = result.get("results") if isinstance(result.get("results"), dict) else {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _run_audio_id_worker(all_files: List[tuple[Any, str]]) -> Dict[str, Dict[str, Any]]:
    if not _feature_enabled("MEDIA_INDEXER_ENABLE_AUDIO_ID"):
        return {}
    audio_files = [str(getattr(media, "full_path", "")) for media, ftype in all_files if ftype == "audio"]
    audio_files = [path for path in audio_files if path]
    if not audio_files:
        return {}
    script = _SKILL_ROOT / "workers" / "audio_id_worker.py"
    if not script.exists():
        return {}
    try:
        max_files = int(os.getenv("MEDIA_INDEXER_AUDIO_ID_MAX_FILES") or 20)
    except Exception:
        max_files = 20
    request = {
        "files": audio_files,
        "cache_path": str(_internal_data_dir() / "audio_id_cache.json"),
        "max_files": max(1, max_files),
        "per_file_timeout_sec": float(os.getenv("MEDIA_INDEXER_AUDIO_ID_TIMEOUT_SEC") or 30),
        "total_timeout_sec": float(os.getenv("MEDIA_INDEXER_AUDIO_ID_TOTAL_TIMEOUT_SEC") or 240),
    }
    result = _run_json_worker(script, request, timeout_sec=float(request["total_timeout_sec"]) + 5.0)
    if not result.get("ok"):
        logger.warning("Audio ID worker unavailable: %s", result.get("error") or "unknown_error")
        return {}
    raw = result.get("results") if isinstance(result.get("results"), dict) else {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _ensure_initialized(*, load_index: bool = False) -> None:
    _ensure_service_site_packages()
    if _state["vector_db"] is None:
        logger.info("Initializing media_indexer_skill ML components")
        _state["scanner"] = None
        _state["extractor"] = TechnicalMetadataExtractor()
        if _feature_enabled("MEDIA_INDEXER_ENABLE_NER_INLINE"):
            _state["ner"] = NERPredictor()
        else:
            logger.info("Inline filename NER disabled; rule parser and optional worker NER remain available.")
            _state["ner"] = _NoopNERPredictor()
        _state["enricher"] = EnrichmentService()
        _state["vector_db"] = VectorDatabase()
        _state["index_loaded"] = False
    if load_index and not _state.get("index_loaded"):
        loaded = _state["vector_db"].load(_index_dir())
        _state["index_loaded"] = bool(loaded.get("loaded"))
        if loaded.get("loaded"):
            restored_metadata = _read_persisted_index_metadata()
            restored_diagnostics = _diagnostics_from_index_metadata(restored_metadata)
            if restored_diagnostics:
                _state["last_diagnostics"] = restored_diagnostics
            if not (_index_dir() / PLAYBACK_INDEX_FILENAME).exists() or not (_index_dir() / INDEX_SUMMARY_FILENAME).exists():
                vector_db = _state["vector_db"]
                _write_index_sidecars({
                    **restored_metadata,
                    "text_docs": list(getattr(vector_db, "text_docs", None) or []),
                    "image_docs": list(getattr(vector_db, "image_docs", None) or []),
                })
            logger.info("Loaded persisted media index: %s", loaded)


def _persist_index(directory: str, indexed_count: int) -> Dict[str, Any]:
    vector_db = _state.get("vector_db")
    if vector_db is None:
        return {"saved": False, "reason": "not_initialized"}
    metadata = vector_db.save(_index_dir())
    payload = {
        "indexed_directory": directory,
        "indexed_count": indexed_count,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "index_dir": str(_index_dir()),
        **metadata,
    }
    try:
        (_index_dir() / "metadata.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_index_sidecars(payload)
    except Exception:
        logger.warning("failed to update persisted media index metadata with playback root", exc_info=True)
    compact = _compact_index_metadata(payload)
    _safe_memory_set(INDEX_META_KEY, compact)
    _state["index_loaded"] = True
    return compact


def _clear_persisted_index(directory: str) -> Dict[str, Any]:
    path = _index_dir()
    for filename in ("metadata.json", INDEX_SUMMARY_FILENAME, PLAYBACK_INDEX_FILENAME, "text.index", "image.index"):
        try:
            (path / filename).unlink(missing_ok=True)
        except Exception:
            logger.debug("failed to remove persisted media index file=%s", filename, exc_info=True)
    payload = {
        "indexed_directory": directory,
        "indexed_count": 0,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "index_dir": str(path),
        "cleared": True,
    }
    _safe_memory_set(INDEX_META_KEY, payload)
    vector_db = _state.get("vector_db")
    if vector_db is not None and hasattr(vector_db, "reset"):
        vector_db.reset()
    _state["index_loaded"] = False
    return payload


def has_cyrillic(text: str) -> bool:
    return bool(text) and bool(re.search(r"[\u0400-\u04FF]", text))


def _flatten_inventory(inventory: Dict[str, List[Any]]) -> List[tuple[Any, str]]:
    all_files: List[tuple[Any, str]] = []
    for m_type, m_list in inventory.items():
        all_files.extend((media, m_type) for media in m_list)
    return all_files


def _build_display_title(stem: str, title: str, artist: str) -> str:
    if artist and title:
        return f"{artist} - {title}"
    if title:
        return title
    return stem


def _playback_id(path: str) -> str:
    return hashlib.sha256(str(path or "").encode("utf-8", errors="surrogatepass")).hexdigest()[:32]


def _guess_mime_type(path: str) -> str:
    suffix = pathlib.Path(path).suffix.lower()
    overrides = {
        ".mkv": "video/x-matroska",
        ".m4v": "video/mp4",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    if suffix in overrides:
        return overrides[suffix]
    guessed, _encoding = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _player_item_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    path = str(payload.get("full_path") or payload.get("path") or "").strip()
    target = pathlib.Path(path) if path else None
    size = 0
    modified_at = ""
    if target and target.exists():
        stat = target.stat()
        size = int(stat.st_size)
        modified_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime))
    playback_id = str(payload.get("playback_id") or _playback_id(path))
    name = str(payload.get("real_file_name") or (target.name if target else playback_id))
    return {
        "name": name,
        "title": _result_title(payload),
        "size_bytes": size,
        "mime_type": _guess_mime_type(path or name),
        "modified_at": modified_at,
        "content_path": f"/api/node/media-indexer/content/{playback_id}",
        "routed_content_path": f"/media/media-indexer/content/{playback_id}",
        "playback_id": playback_id,
        "source_path": path,
    }


def _empty_playback_snapshot() -> Dict[str, Any]:
    updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "ok": True,
        "items": [],
        "count": 0,
        "total_bytes": 0,
        "runtime": {"recommended_path": "direct_local_http"},
        "capabilities": {"notes": ["Select a media item from Media Indexer results to preview it."]},
        "updated_at": updated_at,
    }


def _playback_snapshot(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not payload:
        return _empty_playback_snapshot()
    updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    item = _player_item_from_payload(payload)
    return {
        "ok": True,
        "items": [item],
        "count": 1,
        "total_bytes": int(item.get("size_bytes") or 0),
        "runtime": {"recommended_path": "direct_local_http"},
        "capabilities": {
            "notes": ["Read-only preview from the indexed media directory."],
            "playback": {"direct_local": {"ready": True, "mode": "media_indexer_read_only"}},
        },
        "updated_at": updated_at,
    }


def _path_from_action_payload(payload: Dict[str, Any]) -> str:
    for key in ("path", "full_path", "source_path"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    for key in ("full_path", "path", "source_path"):
        value = str(nested.get(key) or "").strip()
        if value:
            return value
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    return str(details.get("path") or "").strip()


def _payload_by_path(path: str) -> Dict[str, Any] | None:
    needle = str(path or "").strip()
    if not needle:
        return None
    needle_name = pathlib.Path(needle).name
    fallback: Dict[str, Any] | None = None
    vector_db = _state.get("vector_db")
    docs = list(getattr(vector_db, "text_docs", None) or []) + list(getattr(vector_db, "image_docs", None) or [])
    for doc in docs:
        payload = doc.get("payload") if isinstance(doc.get("payload"), dict) else {}
        if str(payload.get("full_path") or "").strip() == needle:
            return payload
        payload_path = str(payload.get("full_path") or "").strip()
        payload_names = {
            str(payload.get("real_file_name") or "").strip(),
            pathlib.Path(payload_path).name if payload_path else "",
        }
        if needle_name and needle_name in payload_names and fallback is None:
            fallback = payload
    for item in _state.get("library_items") or []:
        if isinstance(item, dict) and str(item.get("path") or "").strip() == needle:
            return {
                "full_path": item.get("path"),
                "real_file_name": item.get("source"),
                "display_title": item.get("title"),
                "ftype": item.get("type"),
            }
        item_path = str(item.get("path") or "") if isinstance(item, dict) else ""
        item_name = str(item.get("source") or pathlib.Path(item_path).name) if isinstance(item, dict) else ""
        if needle_name and needle_name == item_name:
            return {
                "full_path": item.get("path"),
                "real_file_name": item.get("source"),
                "display_title": item.get("title"),
                "ftype": item.get("type"),
            }
    return fallback


def _resolve_playback_payload(path: str) -> tuple[Dict[str, Any] | None, str]:
    selected_path = str(path or "").strip()
    selected_payload = _payload_by_path(selected_path)
    if selected_payload is None and selected_path:
        selected_payload = {
            "full_path": selected_path,
            "real_file_name": pathlib.Path(selected_path).name,
            "display_title": pathlib.Path(selected_path).stem,
        }
    if not selected_payload:
        return None, ""
    playback_path = str(selected_payload.get("full_path") or selected_path).strip()
    return selected_payload, playback_path


def _empty_diagnostics() -> Dict[str, Any]:
    return {
        "value": "ready",
        "label": "Диагностика модели",
        "subtitle": "ожидает сканирования",
        "description": "Выберите папку, чтобы показать работу NER, распознавания музыки, OCR и метаданных.",
        "color": "#93C5FD",
        "files_found": 0,
        "indexed_count": 0,
        "by_type": {},
        "summary": {
            "value": "0",
            "label": "Медиатека",
            "subtitle": "ожидает сканирования",
            "description": "После сканирования здесь появится состав коллекции.",
            "color": "#93C5FD",
        },
        "ner": {
            "value": "0/0",
            "label": "Модель NER",
            "subtitle": "модель готова",
            "description": "Извлекает название, исполнителя, год и качество из имён файлов.",
            "color": "#93C5FD",
        },
        "audio": {
            "value": "0/0",
            "label": "Музыка",
            "subtitle": "ожидает распознавания",
            "description": "Распознаёт треки и добавляет их в семантический индекс.",
            "color": "#93C5FD",
        },
        "image": {
            "value": "0",
            "label": "Изображения",
            "subtitle": "OCR не запускался",
            "description": "Проверяет картинки на текст и добавляет визуальные признаки.",
            "color": "#93C5FD",
        },
        "metadata": {
            "value": "0",
            "label": "Полнота данных",
            "subtitle": "без предупреждений",
            "description": "Показывает, сколько файлов удалось обогатить техническими метаданными.",
            "color": "#6EE7B7",
        },
        "ner_parsed": 0,
        "shazam_matched": 0,
        "ocr_checked": 0,
        "ocr_text_found": 0,
        "technical_errors": 0,
    }


def _empty_diagnostics() -> Dict[str, Any]:
    return {
        "value": "ready",
        "label": "Model diagnostics",
        "subtitle": "Waiting for scan",
        "description": "Scan a folder to show NER coverage, audio recognition, OCR and metadata quality.",
        "color": "#93C5FD",
        "files_found": 0,
        "indexed_count": 0,
        "by_type": {},
        "summary": {
            "value": "0",
            "label": "Indexed media",
            "subtitle": "Waiting for scan",
            "description": "The indexed collection will appear after scanning.",
            "color": "#93C5FD",
        },
        "ner": {
            "value": "0/0",
            "label": "Filename NER",
            "subtitle": "Filename parser ready",
            "description": "Rule-based filename entities are always used; optional ML NER can be enabled with MEDIA_INDEXER_ENABLE_NER=1.",
            "color": "#93C5FD",
        },
        "audio": {
            "value": "0/0",
            "label": "Audio ID",
            "subtitle": "Waiting for recognition",
            "description": "Recognized tracks are added to the semantic index.",
            "color": "#93C5FD",
        },
        "image": {
            "value": "0",
            "label": "Image OCR",
            "subtitle": "Waiting for OCR",
            "description": "Images are checked for text and visual search payloads.",
            "color": "#93C5FD",
        },
        "metadata": {
            "value": "0/0",
            "label": "Metadata quality",
            "subtitle": "No warnings",
            "description": "Shows how much of the media library has complete technical metadata.",
            "color": "#6EE7B7",
        },
        "ner_parsed": 0,
        "shazam_matched": 0,
        "ocr_checked": 0,
        "ocr_text_found": 0,
        "technical_errors": 0,
    }


def _type_counts(inventory: Dict[str, List[Any]]) -> Dict[str, int]:
    return {str(m_type): len(m_list or []) for m_type, m_list in inventory.items()}


def _has_ner_entities(ner_result: Dict[str, Any]) -> bool:
    return any(str(ner_result.get(key) or "").strip() for key in ("title", "year", "quality", "artist"))


def _scan_diagnostics(
    *,
    type_counts: Dict[str, int],
    files_found: int,
    indexed_count: int,
    ner_parsed: int,
    shazam_matched: int,
    ocr_checked: int,
    ocr_text_found: int,
    technical_errors: int,
    error_count: int,
    technical_metadata_enabled: bool = True,
    audio_id_enabled: bool = True,
) -> Dict[str, Any]:
    type_text = ", ".join(f"{TYPE_LABELS.get(name, name)}: {count}" for name, count in sorted(type_counts.items())) or "медиа не найдено"
    audio_count = int(type_counts.get("audio") or 0)
    image_count = int(type_counts.get("image") or 0)
    video_count = int(type_counts.get("video") or 0)
    description_parts = [
        f"{type_text}",
        f"NER: {ner_parsed}/{files_found}",
    ]
    if audio_count:
        description_parts.append(f"музыка распознана: {shazam_matched}/{audio_count}")
    if ocr_checked:
        description_parts.append(f"OCR: проверено {ocr_checked}, текст найден {ocr_text_found}")
    if technical_errors:
        description_parts.append(f"неполные метаданные: {technical_errors}")
    if error_count:
        description_parts.append(f"ошибки индексации: {error_count}")
    files_color = "#34D399" if files_found and indexed_count == files_found and not error_count else "#FBBF24"
    ner_color = "#34D399" if files_found and ner_parsed else "#93C5FD"
    audio_color = "#34D399" if audio_id_enabled and audio_count and shazam_matched == audio_count else "#93C5FD"
    image_color = "#34D399" if ocr_checked else "#93C5FD"
    metadata_color = "#FB7185" if technical_errors or error_count else "#6EE7B7"
    metadata_ok = max(0, indexed_count - technical_errors - error_count)
    return {
        "value": str(indexed_count),
        "label": "Диагностика модели",
        "subtitle": f"{indexed_count}/{files_found} файлов в индексе",
        "description": "; ".join(description_parts),
        "color": files_color,
        "files_found": files_found,
        "indexed_count": indexed_count,
        "by_type": type_counts,
        "summary": {
            "value": str(indexed_count),
            "label": "Медиатека",
            "subtitle": f"{indexed_count}/{files_found} файлов готово",
            "description": f"{video_count} видео, {audio_count} аудио, {image_count} изображения",
            "color": files_color,
        },
        "ner": {
            "value": f"{ner_parsed}/{files_found}",
            "label": "Модель NER",
            "subtitle": "покрытие дообученной модели",
            "description": "Извлечены название, исполнитель, год или качество из имён файлов.",
            "color": ner_color,
        },
        "audio": {
            "value": f"{shazam_matched}/{audio_count}",
            "label": "Музыка",
            "subtitle": "распознавание треков",
            "description": "Найденные треки добавлены в семантический индекс.",
            "color": audio_color,
        },
        "image": {
            "value": str(ocr_checked),
            "label": "Изображения",
            "subtitle": f"найдено текстовых фрагментов: {ocr_text_found}",
            "description": "Картинки проверены OCR и добавлены в визуальный поиск.",
            "color": image_color,
        },
        "metadata": {
            "value": f"{metadata_ok}/{indexed_count}" if indexed_count else "0/0",
            "label": "Полнота данных",
            "subtitle": f"{technical_errors + error_count} предупреждений",
            "description": f"{metadata_ok} файлов с полными данными, {technical_errors} без части тех. метаданных",
            "color": metadata_color,
        },
        "ner_parsed": ner_parsed,
        "shazam_matched": shazam_matched,
        "ocr_checked": ocr_checked,
        "ocr_text_found": ocr_text_found,
        "technical_errors": technical_errors,
        "error_count": error_count,
    }


def _scan_diagnostics(
    *,
    type_counts: Dict[str, int],
    files_found: int,
    indexed_count: int,
    ner_parsed: int,
    shazam_matched: int,
    ocr_checked: int,
    ocr_text_found: int,
    technical_errors: int,
    error_count: int,
    technical_metadata_enabled: bool = True,
    audio_id_enabled: bool = True,
) -> Dict[str, Any]:
    audio_count = int(type_counts.get("audio") or 0)
    image_count = int(type_counts.get("image") or 0)
    video_count = int(type_counts.get("video") or 0)
    type_text = f"{video_count} video, {audio_count} audio, {image_count} images"
    files_color = "#34D399" if files_found and indexed_count == files_found and not error_count else "#FBBF24"
    ner_color = "#34D399" if files_found and ner_parsed else "#93C5FD"
    audio_color = "#34D399" if audio_id_enabled and audio_count and shazam_matched == audio_count else "#93C5FD"
    image_color = "#34D399" if ocr_checked else "#93C5FD"
    metadata_color = "#FB7185" if technical_errors or error_count else "#6EE7B7"
    if not technical_metadata_enabled:
        metadata_color = "#93C5FD"
    metadata_ok = max(0, indexed_count - technical_errors - error_count)
    warnings = technical_errors + error_count
    ner_enabled = _feature_enabled("MEDIA_INDEXER_ENABLE_NER")
    ner_value = f"{ner_parsed}/{files_found}"
    ner_subtitle = "Rules + custom model" if ner_enabled else "Filename parser"
    ner_description = (
        "Filename entities came from the rule parser plus optional custom model output."
        if ner_enabled
        else "Rule-based parser extracted title, artist, year or quality from filenames."
    )
    diagnostics_description = (
        f"{type_text}; NER {ner_parsed}/{files_found}; audio matches {shazam_matched}/{audio_count}; OCR checked {ocr_checked}"
    )
    if not technical_metadata_enabled:
        diagnostics_description += "; technical metadata disabled"
    if not audio_id_enabled:
        diagnostics_description += "; audio ID disabled"
    return {
        "value": str(indexed_count),
        "label": "Model diagnostics",
        "subtitle": f"{indexed_count}/{files_found} files indexed",
        "description": diagnostics_description,
        "color": files_color,
        "files_found": files_found,
        "indexed_count": indexed_count,
        "by_type": type_counts,
        "summary": {
            "value": str(indexed_count),
            "label": "Indexed media",
            "subtitle": f"{indexed_count}/{files_found} ready",
            "description": type_text,
            "color": files_color,
        },
        "ner": {
            "value": ner_value,
            "label": "Filename NER",
            "subtitle": ner_subtitle,
            "description": ner_description,
            "color": ner_color,
        },
        "audio": {
            "value": f"{shazam_matched}/{audio_count}" if audio_id_enabled else "off",
            "label": "Audio ID",
            "subtitle": "Track recognition" if audio_id_enabled else "Disabled for safe MVP scan",
            "description": (
                "Recognized tracks are enriched and searchable."
                if audio_id_enabled
                else "Shazamio recognition is installed but opt-in because it can be slow and network-dependent."
            ),
            "color": audio_color,
        },
        "image": {
            "value": str(ocr_checked),
            "label": "Image OCR",
            "subtitle": f"{ocr_text_found} text fragments",
            "description": "Images are checked for OCR text and visual search payloads.",
            "color": image_color,
        },
        "metadata": {
            "value": (f"{metadata_ok}/{indexed_count}" if indexed_count else "0/0") if technical_metadata_enabled else "off",
            "label": "Metadata quality",
            "subtitle": f"{warnings} warnings" if technical_metadata_enabled else "Disabled for safe MVP scan",
            "description": (
                f"{metadata_ok} files have complete metadata; {technical_errors} have partial technical metadata."
                if technical_metadata_enabled
                else "Technical metadata extraction is opt-in because ffprobe can block on slow or damaged media."
            ),
            "color": metadata_color,
        },
        "ner_parsed": ner_parsed,
        "shazam_matched": shazam_matched,
        "ocr_checked": ocr_checked,
        "ocr_text_found": ocr_text_found,
        "technical_errors": technical_errors,
        "error_count": error_count,
    }


def _diagnostics_from_index_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    if not metadata:
        return {}
    docs = list(metadata.get("text_docs") or []) + list(metadata.get("image_docs") or [])
    indexed_count = int(metadata.get("indexed_count") or metadata.get("total_count") or len(docs) or 0)
    if indexed_count <= 0:
        return {}
    type_counts = {"video": 0, "audio": 0, "image": 0}
    ner_parsed = 0
    shazam_matched = 0
    ocr_checked = 0
    ocr_text_found = 0
    for doc in docs:
        payload = doc.get("payload") if isinstance(doc, dict) and isinstance(doc.get("payload"), dict) else {}
        ftype = str(payload.get("ftype") or payload.get("type") or "").strip().lower()
        if ftype in type_counts:
            type_counts[ftype] += 1
        if any(payload.get(key) not in (None, "", "---") for key in ("ner_title", "artist", "year", "quality")):
            ner_parsed += 1
        enriched = payload.get("enriched") if isinstance(payload.get("enriched"), dict) else {}
        if enriched.get("shazam_title") or enriched.get("shazam_subtitle"):
            shazam_matched += 1
        if ftype == "image":
            ocr_checked += 1
            if enriched.get("ocr_text"):
                ocr_text_found += 1
    return _scan_diagnostics(
        type_counts=type_counts,
        files_found=indexed_count,
        indexed_count=indexed_count,
        ner_parsed=ner_parsed,
        shazam_matched=shazam_matched,
        ocr_checked=ocr_checked,
        ocr_text_found=ocr_text_found,
        technical_errors=0,
        error_count=0,
        technical_metadata_enabled=_feature_enabled("MEDIA_INDEXER_ENABLE_TECHNICAL_METADATA"),
        audio_id_enabled=_feature_enabled("MEDIA_INDEXER_ENABLE_AUDIO_ID"),
    )


def _best_results_by_path(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    best: Dict[str, Dict[str, Any]] = {}
    for result in results:
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        key = str(payload.get("full_path") or payload.get("real_file_name") or result.get("text") or id(result))
        current = best.get(key)
        if current is None or float(result.get("score") or 0.0) > float(current.get("score") or 0.0):
            best[key] = result
    deduped = list(best.values())
    deduped.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return deduped


def _result_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else {}


def _result_title(payload: Dict[str, Any]) -> str:
    return str(
        payload.get("display_title")
        or payload.get("title")
        or payload.get("real_file_name")
        or pathlib.Path(str(payload.get("full_path") or "")).name
        or "Медиафайл"
    )


def _result_subtitle(result: Dict[str, Any], payload: Dict[str, Any]) -> str:
    raw_type = str(payload.get("ftype") or payload.get("type") or result.get("type") or "media")
    parts = [
        TYPE_LABELS.get(raw_type, raw_type),
        f"точность {float(result.get('score') or 0.0):.1f}",
    ]
    for key in ("year", "quality", "artist"):
        value = str(payload.get(key) or "").strip()
        if value and value != "---":
            parts.append(value)
    match_type = str(result.get("type") or "").strip()
    if match_type:
        parts.append(f"совпадение: {TYPE_LABELS.get(match_type, match_type)}")
    return " • ".join(parts)


def _result_details(result: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "file": payload.get("real_file_name") or pathlib.Path(str(payload.get("full_path") or "")).name,
        "path": payload.get("full_path") or "",
        "type": payload.get("ftype") or payload.get("type") or "",
        "score": float(result.get("score") or 0.0),
            "match_type": result.get("type") or "",
            "summary": _result_subtitle(result, payload),
            "ner": {
                "title": payload.get("ner_title") or payload.get("display_title") or "",
                "year": payload.get("year") or "",
            "quality": payload.get("quality") or "",
            "artist": payload.get("artist") or "",
        },
        "technical_metadata": payload.get("technical_metadata") or {},
        "enriched": payload.get("enriched") or {},
    }


def _result_title(payload: Dict[str, Any]) -> str:
    return str(
        payload.get("display_title")
        or payload.get("title")
        or payload.get("real_file_name")
        or pathlib.Path(str(payload.get("full_path") or "")).name
        or "Untitled media"
    )


def _result_subtitle(result: Dict[str, Any], payload: Dict[str, Any]) -> str:
    raw_type = str(payload.get("ftype") or payload.get("type") or result.get("type") or "media")
    parts = [TYPE_LABELS.get(raw_type, raw_type), f"score {float(result.get('score') or 0.0):.1f}"]
    for key in ("year", "quality", "artist"):
        value = str(payload.get(key) or "").strip()
        if value and value != "---":
            parts.append(value)
    match_type = str(result.get("type") or "").strip()
    if match_type:
        parts.append(f"match: {TYPE_LABELS.get(match_type, match_type)}")
    return " | ".join(parts)


def _result_details(result: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    technical = payload.get("technical_metadata") if isinstance(payload.get("technical_metadata"), dict) else {}
    enriched = payload.get("enriched") if isinstance(payload.get("enriched"), dict) else {}
    metadata_bits = []
    for key in ("width", "height", "duration_seconds", "image_format", "audio_codec", "video_codec"):
        value = technical.get(key)
        if value not in (None, "", 0, "---"):
            metadata_bits.append(f"{key}: {value}")
    enrichment_bits = []
    if enriched.get("shazam_title"):
        enrichment_bits.append(f"Shazam: {enriched.get('shazam_title')}")
    if enriched.get("shazam_subtitle"):
        enrichment_bits.append(f"Artist: {enriched.get('shazam_subtitle')}")
    if enriched.get("ocr_text"):
        enrichment_bits.append("OCR text found")
    return {
        "file": payload.get("real_file_name") or pathlib.Path(str(payload.get("full_path") or "")).name,
        "path": payload.get("full_path") or "",
        "type": payload.get("ftype") or payload.get("type") or "",
        "score": float(result.get("score") or 0.0),
        "match_type": result.get("type") or "",
        "summary": _result_subtitle(result, payload),
        "ner": {
            "title": payload.get("ner_title") or payload.get("display_title") or "",
            "year": payload.get("year") or "",
            "quality": payload.get("quality") or "",
            "artist": payload.get("artist") or "",
        },
        "metadata": ", ".join(metadata_bits) or "No extra technical metadata",
        "enrichment": ", ".join(enrichment_bits) or "No external enrichment",
    }


def _result_details_text(result: Dict[str, Any], payload: Dict[str, Any]) -> str:
    details = _result_details(result, payload)
    ner = details["ner"]
    ner_bits = []
    if ner.get("title"):
        ner_bits.append(f"title: {ner['title']}")
    if ner.get("artist"):
        ner_bits.append(f"artist: {ner['artist']}")
    if ner.get("year") and ner.get("year") != "---":
        ner_bits.append(f"year: {ner['year']}")
    if ner.get("quality") and ner.get("quality") != "---":
        ner_bits.append(f"quality: {ner['quality']}")
    return "\n".join(
        [
            f"File: {details['file']}",
            f"Path: {details['path']}",
            f"Matched by: {TYPE_LABELS.get(str(details['match_type']), details['match_type'])}",
            f"Score: {details['score']:.1f}",
            f"NER: {', '.join(ner_bits) if ner_bits else 'no filename entities'}",
            f"Metadata: {details['metadata']}",
            f"Enrichment: {details['enrichment']}",
        ]
    )


def _library_item(payload: Dict[str, Any], *, signals: List[str]) -> Dict[str, Any]:
    media_type = str(payload.get("ftype") or payload.get("type") or "media")
    year = str(payload.get("year") or "").strip()
    quality = str(payload.get("quality") or "").strip()
    extras = [value for value in (year, quality) if value and value != "---"]
    subtitle_parts = [TYPE_LABELS.get(media_type, media_type), *extras]
    signal_text = ", ".join(signals) if signals else "metadata"
    if signal_text:
        subtitle_parts.append(signal_text)
    return {
        "title": _result_title(payload),
        "subtitle": " | ".join(subtitle_parts),
        "type": TYPE_LABELS.get(media_type, media_type),
        "signals": signal_text,
        "source": payload.get("real_file_name") or pathlib.Path(str(payload.get("full_path") or "")).name,
        "path": payload.get("full_path") or "",
        "playback_id": payload.get("playback_id") or _playback_id(str(payload.get("full_path") or "")),
        "content_path": payload.get("content_path") or "",
        "mime_type": payload.get("mime_type") or _guess_mime_type(str(payload.get("full_path") or "")),
        "details": {
            "path": payload.get("full_path") or "",
            "ner_title": payload.get("ner_title") or "",
            "ner_source": payload.get("ner_source") or "",
            "artist": payload.get("artist") or "",
            "year": payload.get("year") or "",
            "quality": payload.get("quality") or "",
        },
    }


def _trim_for_log(value: Any, *, limit: int = 1000) -> Any:
    if isinstance(value, dict):
        return {str(k): _trim_for_log(v, limit=limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim_for_log(item, limit=limit) for item in value[:20]]
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "...<truncated>"
    return value


def _log_index_document(
    *,
    media_name: str,
    ftype: str,
    technical_metadata: Dict[str, Any],
    ner_result: Dict[str, Any],
    enriched: Dict[str, Any],
    payload: Dict[str, Any],
    index_text: str,
    timings: Dict[str, float],
) -> None:
    diagnostics = {
        "file": media_name,
        "type": ftype,
        "technical_metadata": technical_metadata,
        "ner": ner_result,
        "enriched": enriched,
        "payload": payload,
        "index_text": index_text,
        "timings_sec": {key: round(value, 3) for key, value in timings.items()},
    }
    logger.debug("Index document: %s", json.dumps(_trim_for_log(diagnostics), ensure_ascii=False, sort_keys=True))


@tool("scan_and_index")
def scan_and_index(directory: str) -> Dict[str, Any]:
    return _scan_and_index(directory)


def _scan_and_index(directory: str, progress: Callable[[Dict[str, Any]], None] | None = None) -> Dict[str, Any]:
    if not str(directory or "").strip():
        return {"status": "error", "indexed_count": 0, "errors": ["Set a media directory first."]}

    path = pathlib.Path(directory).expanduser()
    if not path.exists() or not path.is_dir():
        return {"status": "error", "indexed_count": 0, "errors": [f"Directory not found: {directory}"]}

    try:
        if progress:
            progress({"value": "scanning", "subtitle": "Reading directory", "description": f"Scanning {path}"})
        scanner = DirectoryScanner(str(path), compute_hashes=False)
        _state["scanner"] = scanner
        inventory = scanner.scan()
    except Exception as exc:
        logger.exception("Failed to scan directory %s", path)
        return {"status": "error", "indexed_count": 0, "errors": [str(exc)]}

    all_files = _flatten_inventory(inventory)
    type_counts = _type_counts(inventory)
    technical_metadata_enabled = _technical_metadata_enabled()
    audio_id_enabled = _feature_enabled("MEDIA_INDEXER_ENABLE_AUDIO_ID")
    if not all_files:
        _state["indexed_directory"] = str(path)
        _state["library_items"] = []
        _save_settings(selected_directory=str(path), default_directory=str(path))
        index_meta = _clear_persisted_index(str(path))
        diagnostics = _scan_diagnostics(
            type_counts=type_counts,
            files_found=0,
            indexed_count=0,
            ner_parsed=0,
            shazam_matched=0,
            ocr_checked=0,
            ocr_text_found=0,
            technical_errors=0,
            error_count=0,
            technical_metadata_enabled=technical_metadata_enabled,
            audio_id_enabled=audio_id_enabled,
        )
        _state["last_diagnostics"] = diagnostics
        return {"status": "ok", "indexed_count": 0, "errors": [], "index": index_meta, "diagnostics": diagnostics}

    logger.info("Preparing media index ML initialization for %s files", len(all_files))
    if progress:
        progress(
            {
                "value": "loading",
                "subtitle": f"{len(all_files)} media files found",
                "description": "Loading ML models before indexing.",
                "total_count": len(all_files),
            }
        )
    logger.info("Media index loading progress published; initializing ML components")
    _ensure_initialized(load_index=False)

    extractor = _state["extractor"]
    ner = _state["ner"]
    enricher = _state["enricher"]
    vector_db = _state["vector_db"]
    if hasattr(vector_db, "reset"):
        vector_db.reset()
    _state["last_results"] = []

    if progress:
        progress(
            {
                "value": "loading",
                "subtitle": "Parsing filenames",
                "description": "Extracting title, artist, year and quality from media filenames.",
                "total_count": len(all_files),
            }
        )
    ner_worker_results = _run_ner_worker(all_files)
    if progress and audio_id_enabled:
        progress(
            {
                "value": "loading",
                "subtitle": "Recognizing audio",
                "description": "Running bounded Shazam audio identification worker.",
                "total_count": len(all_files),
            }
        )
    audio_id_results = _run_audio_id_worker(all_files) if audio_id_enabled else {}

    errors: List[str] = []
    indexed = 0
    ner_parsed = 0
    shazam_matched = 0
    ocr_checked = 0
    ocr_text_found = 0
    technical_errors = 0
    library_items: List[Dict[str, Any]] = []

    total = len(all_files)
    for ordinal, (media, ftype) in enumerate(all_files, start=1):
        try:
            timings: Dict[str, float] = {}
            logger.info("Indexing media file: %s", media.name)
            if progress:
                progress(
                    {
                        "value": "indexing",
                        "subtitle": f"{ordinal}/{total} files",
                        "description": media.name,
                        "indexed_count": indexed,
                        "total_count": total,
                    }
                )
            started = time.perf_counter()
            if technical_metadata_enabled:
                technical = extractor.extract(media.full_path, ftype)
                technical_metadata = technical.to_dict() if hasattr(technical, "to_dict") else {}
            else:
                technical_metadata = {"status": "skipped"}
            timings["technical_metadata"] = time.perf_counter() - started

            started = time.perf_counter()
            rule_entities = parse_filename(media.name, ftype)
            inline_entities = ner.extract_entities(media.name)
            worker_entities = ner_worker_results.get(media.full_path) or ner_worker_results.get(media.name) or {}
            ner_result = merge_entities(rule_entities, inline_entities)
            ner_result = merge_entities(ner_result, worker_entities)
            timings["ner"] = time.perf_counter() - started
            if _has_ner_entities(ner_result):
                ner_parsed += 1
            title = ner_result.get("title") or ""
            year = ner_result.get("year") or "---"
            quality = ner_result.get("quality") or "---"
            artist = ner_result.get("artist") or ""

            started = time.perf_counter()
            enriched = dict(audio_id_results.get(media.full_path) or {})
            if not enriched:
                enriched = enricher.enrich(media.full_path, ftype)
            if ftype == "audio" and not audio_id_enabled:
                enriched = {
                    key: value
                    for key, value in enriched.items()
                    if not str(key).startswith("shazam_")
                }
            if ftype == "video" and title:
                enriched.update(enricher.enrich_video(title))
            timings["enrichment"] = time.perf_counter() - started
            if technical_metadata.get("status") == "error":
                technical_errors += 1
            if audio_id_enabled and ftype == "audio" and (enriched.get("shazam_title") or enriched.get("shazam_subtitle")):
                shazam_matched += 1
            if ftype == "image":
                ocr_checked += 1
                if enriched.get("ocr_text"):
                    ocr_text_found += 1

            stem = pathlib.Path(media.name).stem
            display_title = _build_display_title(stem, title, artist)
            payload = {
                "real_file_name": media.name,
                "display_title": display_title,
                "full_path": media.full_path,
                "playback_id": _playback_id(media.full_path),
                "content_path": f"/api/node/media-indexer/content/{_playback_id(media.full_path)}",
                "mime_type": _guess_mime_type(media.full_path),
                "type": ftype,
                "ftype": ftype,
                "title": display_title,
                "ner_title": title,
                "ner_source": ner_result.get("source") or "",
                "year": year,
                "quality": quality,
                "artist": artist,
                "technical_metadata": technical_metadata,
                "enriched": enriched,
            }
            signals: List[str] = []
            if _has_ner_entities(ner_result):
                signals.append("NER")
            if audio_id_enabled and ftype == "audio" and (enriched.get("shazam_title") or enriched.get("shazam_subtitle")):
                signals.append("Shazam")
            if ftype == "image":
                signals.append("OCR" if enriched.get("ocr_text") else "visual")
            if technical_metadata and technical_metadata.get("status") != "error":
                signals.append("metadata")

            index_text = ""
            started = time.perf_counter()
            if ftype == "image":
                index_text = " ".join(["photo image", stem])
                vector_db.add_image(media.full_path, payload)
                vector_db.add_text(" ".join(["photo image изображение фотография", stem]), payload)
            elif ftype == "audio":
                parts = ["music audio song track музыка аудио песня трек"]
                if artist:
                    parts.append(f"artist {artist} исполнитель {artist}")
                if title:
                    parts.append(f"title {title} название {title}")
                if quality and quality != "---":
                    parts.append(quality)
                if enriched.get("shazam_title"):
                    parts.append(f"shazam {enriched['shazam_title']}")
                if enriched.get("shazam_subtitle"):
                    parts.append(f"shazam artist {enriched['shazam_subtitle']}")
                if enriched.get("shazam_genre"):
                    parts.append(f"genre {enriched['shazam_genre']}")
                if has_cyrillic(stem):
                    parts.append("русская русский на русском")
                for key in ("audio_codec", "duration_seconds", "bit_rate", "sample_rate"):
                    if technical_metadata.get(key):
                        parts.append(f"{key} {technical_metadata[key]}")
                parts.append(stem)
                index_text = " ".join(filter(bool, parts))
                vector_db.add_text(index_text, payload)
            elif ftype == "video":
                parts = ["video movie film видео фильм кино"]
                if title:
                    parts.append(f"title {title} название {title}")
                if year != "---":
                    parts.append(f"year {year} год {year}")
                if quality != "---":
                    parts.append(quality)
                plot = (enriched.get("imdb") or {}).get("plot", "")
                if plot:
                    parts.append(plot)
                if has_cyrillic(stem):
                    parts.append("русское кино русский фильм")
                for key in ("video_codec", "duration_seconds", "bit_rate", "width", "height"):
                    if technical_metadata.get(key):
                        parts.append(f"{key} {technical_metadata[key]}")
                parts.append(stem)
                index_text = " ".join(filter(bool, parts))
                vector_db.add_text(index_text, payload)
            else:
                continue

            timings["embedding"] = time.perf_counter() - started
            _log_index_document(
                media_name=media.name,
                ftype=ftype,
                technical_metadata=technical_metadata,
                ner_result=ner_result,
                enriched=enriched,
                payload=payload,
                index_text=index_text,
                timings=timings,
            )

            library_items.append(_library_item(payload, signals=signals))
            indexed += 1
        except Exception as exc:
            logger.exception("Failed to index %s", getattr(media, "name", "unknown"))
            errors.append(f"{getattr(media, 'name', 'unknown')}: {exc}")
            if progress:
                progress(
                    {
                        "value": "indexing",
                        "subtitle": f"{ordinal}/{total} files",
                        "description": f"Skipped {getattr(media, 'name', 'unknown')}: {exc}",
                        "indexed_count": indexed,
                        "total_count": total,
                    }
                )

    _state["indexed_directory"] = str(path)
    _state["library_items"] = library_items[:SNAPSHOT_LIBRARY_LIMIT]
    _save_settings(selected_directory=str(path), default_directory=str(path))
    index_meta = _persist_index(str(path), indexed)
    diagnostics = _scan_diagnostics(
        type_counts=type_counts,
        files_found=total,
        indexed_count=indexed,
        ner_parsed=ner_parsed,
        shazam_matched=shazam_matched,
        ocr_checked=ocr_checked,
        ocr_text_found=ocr_text_found,
        technical_errors=technical_errors,
        error_count=len(errors),
        technical_metadata_enabled=technical_metadata_enabled,
        audio_id_enabled=audio_id_enabled,
    )
    _state["last_diagnostics"] = diagnostics
    return {"status": "ok", "indexed_count": indexed, "errors": errors, "index": index_meta, "diagnostics": diagnostics}


@tool("search_media")
def search_media(query: str, k: int = 5) -> Dict[str, Any]:
    if not query or not query.strip():
        return {"status": "ok", "results": []}

    if _state.get("scan_in_progress"):
        return {"status": "error", "results": [], "message": "Indexing is still in progress. Wait until the library is ready."}

    if _state["vector_db"] is None and not _has_persisted_index():
        return {"status": "error", "results": [], "message": "The index is empty. Scan a folder first."}

    _ensure_initialized(load_index=True)
    vector_db = _state.get("vector_db")
    has_docs = bool(getattr(vector_db, "text_docs", None) or getattr(vector_db, "image_docs", None))
    if not _state.get("index_loaded") and not has_docs:
        return {"status": "error", "results": [], "message": "The index is empty. Scan a folder first."}

    try:
        limit = max(1, min(MAX_RESULTS, int(k or 5)))
    except (TypeError, ValueError):
        limit = 5

    _save_settings(selected_query=query.strip(), k=limit)
    raw_results = _state["vector_db"].search(query.strip(), k=limit)
    valid_results = _best_results_by_path([result for result in raw_results if result.get("score", 0) >= SCORE_THRESHOLD])
    formatted = []
    for result in valid_results[:MAX_RESULTS]:
        payload = _result_payload(result)
        formatted.append(
            {
                "score": float(result.get("score", 0.0)),
                "path": payload.get("full_path", ""),
                "payload": payload,
                "title": _result_title(payload),
                "subtitle": _result_subtitle(result, payload),
                "description": str(payload.get("real_file_name") or ""),
                "details": _result_details(result, payload),
                "details_text": _result_details_text(result, payload),
                "match_type": result.get("type") or "",
            }
        )
    _state["last_results"] = formatted
    return {"status": "ok", "results": formatted}


@tool("play_media")
def play_media(path: str) -> Dict[str, Any]:
    _ensure_initialized(load_index=True)
    payload, playback_path = _resolve_playback_payload(path)
    if not payload or not playback_path or not pathlib.Path(playback_path).is_file():
        return {"status": "error", "message": "file_not_found", "playback": _playback_snapshot()}
    snapshot = _playback_snapshot(payload)
    _state["playback"] = snapshot
    return {"status": "ok", "playback": snapshot}


@tool("get_settings")
def get_settings() -> Dict[str, Any]:
    return {
        "status": "ok",
        "settings": _settings(),
        "index": _index_metadata(),
        "model_weights": model_weights_status(),
    }


@tool("rehydrate")
def rehydrate(webspace_id: str | None = None, **_: Any) -> Dict[str, Any]:
    if _has_persisted_index():
        _ensure_initialized(load_index=True)
    settings, index, snapshot = _rehydrated_snapshot()
    _project_snapshot(snapshot, webspace_id=webspace_id)
    return {"status": "ok", "settings": settings, "index": index, "snapshot": snapshot}


def _rehydrated_snapshot() -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Restore the compact read model without loading the runtime index."""
    settings = _settings()
    _state["selected_directory"] = settings.get("selected_directory") or settings.get("default_directory") or ""
    _state["selected_query"] = settings.get("selected_query") or DEFAULT_QUERY
    index = _index_metadata()
    indexed_count = int(index.get("indexed_count") or index.get("total_count") or 0)
    status = _status_payload(
        value="indexed" if indexed_count else "ready",
        subtitle=f"{indexed_count} files indexed" if indexed_count else "Waiting for scan",
        description=str(index.get("indexed_directory") or ""),
    )
    return settings, index, _snapshot_payload(status=status, form=_current_form())


@tool("dispose")
def dispose() -> Dict[str, Any]:
    _state.update(
        {
            "scanner": None,
            "extractor": None,
            "ner": None,
            "enricher": None,
            "vector_db": None,
            "index_loaded": False,
            "last_diagnostics": None,
            "library_items": [],
            "last_results": [],
            "playback": None,
            "scan_in_progress": False,
        }
    )
    return {"status": "ok"}


@subscribe("sys.ready")
async def on_sys_ready(evt: Any) -> None:
    payload = _event_payload(evt)
    allowed, webspace_id = _target_context(payload)
    if not allowed:
        return
    _, _, snapshot = await asyncio.to_thread(_rehydrated_snapshot)
    await _project_snapshot_async(snapshot, webspace_id=webspace_id or "desktop")


@subscribe("media_indexer.action")
async def on_media_indexer_action(evt: Any) -> None:
    payload = _event_payload(evt)
    allowed, webspace_id = _target_context(payload)
    if not allowed:
        return

    action_id = str(payload.get("id") or payload.get("action") or "").strip().lower()
    directory_action = action_id in {"scan", "set_directory", "directory"}
    directory, query, payload_has_directory = await asyncio.to_thread(
        _action_selection,
        payload,
        include_path=directory_action,
    )
    try:
        k = max(1, min(MAX_RESULTS, int(payload.get("k") or 5)))
    except (TypeError, ValueError):
        k = 5

    if action_id == "scan" and not payload_has_directory:
        form_directory = await _read_directory_from_webspace_form(webspace_id, payload)
        if form_directory:
            directory = form_directory

    logger.info(
        "media_indexer.action received id=%s webspace=%s has_directory=%s directory=%s",
        action_id or "-",
        webspace_id or "default",
        payload_has_directory,
        directory or "",
    )
    form = await asyncio.to_thread(
        _persist_action_selection,
        directory=directory,
        query=query,
        k=k,
    )

    if action_id in {"set_directory", "directory"}:
        await _project_snapshot_async(
            _snapshot_payload_with_form(
                status=_status_payload(value="ready", subtitle="Directory selected", description=f"Media source: {directory or 'not set'}"),
                form=form,
                include_library=False,
                include_results=False,
                include_playback=False,
            ),
            webspace_id=webspace_id,
        )
        return

    if action_id in {"set_query", "query"}:
        await _project_snapshot_async(
            _snapshot_payload_with_form(
                status=_status_payload(value="ready", subtitle="Query selected", description=f"Query: {query or 'not set'}"),
                form=form,
                include_library=False,
                include_results=False,
                include_playback=False,
            ),
            webspace_id=webspace_id,
        )
        return

    if action_id in {"play", "preview"}:
        selected_path, selected_payload, playback_path, exists = await asyncio.to_thread(
            _resolve_playback_action,
            payload,
        )
        if not selected_payload or not exists:
            status = _status_payload(
                value="error",
                subtitle="Preview unavailable",
                description=f"File is not indexed or missing: {selected_path or 'no path'}",
                error="file_not_found",
            )
            await _project_snapshot_async(
                _snapshot_payload_with_form(status=status, form=form, include_library=False),
                webspace_id=webspace_id,
            )
            _publish_operation({"value": "error", "subtitle": status["subtitle"], "description": status["description"]}, webspace_id=webspace_id)
            return
        _state["playback"] = await asyncio.to_thread(_playback_snapshot, selected_payload)
        status = _status_payload(
            value="ready",
            subtitle="Preview selected",
            description=str(selected_payload.get("real_file_name") or pathlib.Path(playback_path or selected_path).name),
        )
        await _project_snapshot_async(
            _snapshot_payload_with_form(status=status, form=form, include_library=False),
            webspace_id=webspace_id,
        )
        _publish_operation({"value": "ready", "subtitle": status["subtitle"], "description": status["description"]}, webspace_id=webspace_id)
        return

    if action_id == "scan":
        if _state.get("scan_in_progress"):
            status = _status_payload(
                value="scanning",
                subtitle="Indexing is already running",
                description="Wait for the current scan to finish.",
            )
            await _project_snapshot_async(
                _snapshot_payload_with_form(
                    status=status,
                    form=form,
                    include_library=False,
                    include_results=False,
                    include_playback=False,
                ),
                webspace_id=webspace_id,
            )
            _publish_operation({"value": status["value"], "subtitle": status["subtitle"], "description": status["description"]}, webspace_id=webspace_id)
            return

        status = _status_payload(value="scanning", subtitle="Building media library", description=f"Scanning {directory or 'no directory'}")
        await _project_snapshot_async(
            _snapshot_payload_with_form(
                status=status,
                form=form,
                include_library=False,
                include_results=False,
                include_playback=False,
            ),
            webspace_id=webspace_id,
        )
        _publish_operation({"value": "scanning", "subtitle": "Building media library", "description": status["description"]}, webspace_id=webspace_id)
        _state["scan_in_progress"] = True
        loop = asyncio.get_running_loop()
        progress_event = asyncio.Event()
        progress_lock = threading.Lock()
        latest_progress_from_worker: Dict[str, Any] | None = None
        progress_notify_scheduled = False

        def progress(update: Dict[str, Any]) -> None:
            nonlocal latest_progress_from_worker, progress_notify_scheduled

            should_schedule = False
            with progress_lock:
                latest_progress_from_worker = dict(update)
                if not progress_notify_scheduled:
                    progress_notify_scheduled = True
                    should_schedule = True
            if not should_schedule:
                return

            def notify_latest() -> None:
                nonlocal progress_notify_scheduled
                progress_event.set()
                with progress_lock:
                    progress_notify_scheduled = False

            try:
                loop.call_soon_threadsafe(notify_latest)
            except RuntimeError:
                logger.debug("failed to enqueue media indexer progress", exc_info=True)

        def pop_latest_progress() -> Dict[str, Any] | None:
            nonlocal latest_progress_from_worker
            with progress_lock:
                update = latest_progress_from_worker
                latest_progress_from_worker = None
            return update

        async def emit_progress(update: Dict[str, Any]) -> None:
            progress_status = _status_payload(
                value=str(update.get("value") or "indexing"),
                subtitle=str(update.get("subtitle") or "Indexing files"),
                description=str(update.get("description") or ""),
                indexed_count=int(update["indexed_count"]) if update.get("indexed_count") is not None else None,
            )
            await _project_snapshot_async(
                _progress_snapshot_payload(status=progress_status, form=form),
                webspace_id=webspace_id,
            )
            _publish_operation(update, webspace_id=webspace_id)

        try:
            scan_task = asyncio.create_task(asyncio.to_thread(_scan_and_index, directory, progress))
            latest_progress: Dict[str, Any] | None = None
            last_progress_emit = 0.0
            emitted_progress = False
            while not scan_task.done():
                try:
                    await asyncio.wait_for(progress_event.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
                progress_event.clear()
                update = pop_latest_progress()
                if update is not None:
                    latest_progress = update
                now = time.monotonic()
                if latest_progress is not None and now - last_progress_emit >= PROGRESS_MIN_INTERVAL_SEC:
                    await emit_progress(latest_progress)
                    latest_progress = None
                    emitted_progress = True
                    last_progress_emit = now
            await asyncio.sleep(0)
            update = pop_latest_progress()
            if update is not None:
                latest_progress = update
            if latest_progress is not None and not emitted_progress:
                await emit_progress(latest_progress)
            result = await scan_task
        except Exception as exc:
            logger.exception("Media indexer scan failed")
            result = {
                "status": "error",
                "indexed_count": 0,
                "errors": [str(exc) or exc.__class__.__name__],
                "diagnostics": _empty_diagnostics(),
            }
        finally:
            _state["scan_in_progress"] = False
        errors = list(result.get("errors") or [])
        ok = str(result.get("status") or "").lower() == "ok"
        indexed_count = int(result.get("indexed_count") or 0)
        diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else _empty_diagnostics()
        _state["last_diagnostics"] = diagnostics
        final_status = _status_payload(
            value="indexed" if ok else "error",
            subtitle=f"Library ready: {indexed_count} files" if ok else "Scan failed",
            description=str(diagnostics.get("description") or "The semantic index is ready.") if ok else "; ".join(errors[:3]),
            error="" if ok else "; ".join(errors[:3]),
            indexed_count=indexed_count,
        )
        await _project_snapshot_async(
            _snapshot_payload_with_form(
                status=final_status,
                form=form,
                diagnostics=diagnostics,
                include_results=False,
                include_playback=False,
            ),
            webspace_id=webspace_id,
        )
        _publish_operation(
            {
                "value": final_status["value"],
                "subtitle": final_status["subtitle"],
                "description": final_status["description"],
                "indexed_count": indexed_count,
                "diagnostics": diagnostics,
            },
            webspace_id=webspace_id,
        )
        return

    if action_id == "search":
        if not query.strip():
            status = _status_payload(value="ready", subtitle="Enter a search query", description="Try: movie, music, Queen, Inception, sunset.")
            await _project_snapshot_async(
                _snapshot_payload_with_form(
                    status=status,
                    form=form,
                    include_library=False,
                    include_results=False,
                    include_playback=False,
                ),
                webspace_id=webspace_id,
            )
            _publish_operation({"value": "ready", "subtitle": status["subtitle"], "description": status["description"]}, webspace_id=webspace_id)
            return
        status = _status_payload(value="searching", subtitle="Semantic search", description=f"Searching for: {query}")
        await _project_snapshot_async(
            _snapshot_payload_with_form(
                status=status,
                form=form,
                include_library=False,
                include_results=False,
                include_playback=False,
            ),
            webspace_id=webspace_id,
        )
        _publish_operation({"value": "searching", "subtitle": "Semantic search", "description": status["description"]}, webspace_id=webspace_id)
        result = await asyncio.to_thread(search_media, query, k=k)
        results = list(result.get("results") or [])
        error = str(result.get("message") or "") if str(result.get("status") or "").lower() != "ok" else ""
        final_status = _status_payload(
            value="done" if not error else "error",
            subtitle=f"{len(results)} results",
            description=f"Query: {query}" if not error else error,
            error=error,
        )
        await _project_snapshot_async(
            _snapshot_payload_with_form(
                status=final_status,
                form=form,
                results=results,
                include_library=False,
                include_playback=False,
            ),
            webspace_id=webspace_id,
        )
        _publish_operation(
            {
                "value": final_status["value"],
                "subtitle": final_status["subtitle"],
                "description": final_status["description"],
                "result_count": len(results),
            },
            webspace_id=webspace_id,
        )


@subscribe("webio.stream.snapshot.requested")
async def on_stream_snapshot_requested(evt: Any) -> None:
    payload = _event_payload(evt)
    receiver = str(payload.get("receiver") or "").strip()
    if receiver != OPERATION_RECEIVER:
        return
    allowed, webspace_id = _target_context(payload)
    if not allowed:
        return
    _publish_operation(
        _state.get("last_operation")
            or {
                "value": "ready",
                "subtitle": "Waiting for action",
                "description": "Choose a folder, build the index, then search.",
            },
        webspace_id=webspace_id,
    )


class _HealthHandler(BaseHTTPRequestHandler):
    server_version = "AdaOSMediaIndexer/0.1"

    def log_message(self, _format: str, *args: Any) -> None:  # noqa: A003
        if os.getenv("MEDIA_INDEXER_HTTP_LOG", "").strip().lower() in {"1", "true", "yes", "on"}:
            super().log_message(_format, *args)

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self._json(404, {"ok": False, "error": "not_found"})
            return
        model_status = model_weights_status()
        self._json(
            200,
            {
                "ok": True,
                "service": "media_indexer_skill",
                "version": _skill_version(),
                "index_loaded": bool(_state.get("index_loaded")),
                "has_persisted_index": _has_persisted_index(),
                "model_weights": model_status,
            },
        )


if __name__ == "__main__":
    host = os.getenv("ADAOS_SERVICE_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("ADAOS_SERVICE_PORT", "18105") or "18105")
    except Exception:
        port = 18105
    logger.info("starting media_indexer_skill health service on %s:%s", host, port)
    ThreadingHTTPServer((host, port), _HealthHandler).serve_forever()
