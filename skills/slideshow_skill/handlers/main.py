from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import shutil
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import skill_memory
from adaos.sdk.data.skill_env import skill_env_path
from adaos.sdk.io import stream_publish, telegram_photo
from adaos.sdk.redevice import ReDeviceBridge, compact_endpoint, list_endpoints as sdk_list_endpoints, select_transport
from adaos.services.agent_context import get_ctx
from adaos.services.media_library import media_file_path
from PIL import Image, ImageOps

try:
    from adaos.services.yjs.webspace import default_webspace_id
except Exception:  # pragma: no cover
    def default_webspace_id() -> str:
        return "default"


_ENDPOINTS_RECEIVER = "slideshow_skill.endpoints"
_PREVIEW_RECEIVER = "slideshow_skill.preview"
_FOLDERS_RECEIVER = "slideshow_skill.folders"
_SESSION_RECEIVER = "slideshow_skill.session"
_COMMAND_RECEIVER = "slideshow_skill.command"
_INDEX_RECEIVER = "slideshow_skill.index"
_SUPPORTED = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
_ENDPOINT_SIZE = (480, 300)
_WIDGET_SIZE = (720, 405)
_WIDGET_IMAGE_BUDGET_BYTES = 60_000
_MAX_SCAN = 2000
_MAX_CONTROL_SCAN = 240
_MAX_ENDPOINT_CURRENT = 4
_MAX_ENDPOINT_FAVORITES = 20
_MAX_FOLDER_STREAM_ITEMS = 250
_INLINE_CONTENT_BUDGET_BYTES = 45_000
_INDEX_BATCH_SIZE = 500
_INDEX_PUBLISH_INTERVAL_S = 1.5
_STATE_KEY = "slideshow_skill.state"
_INDEX_META_KEY = "slideshow_skill.photo_index"
_INDEX_STATUS_KEY = "slideshow_skill.index_status"
_COMMAND_STATE_KEY = "slideshow_skill.command_state"
_LAST_MEDIA_KEY = "slideshow_skill.last_media"
_POLL_INTERVAL_S = 2.5
_SNAPSHOT_DEBOUNCE_S = 1.0
_log = logging.getLogger("skills.slideshow_skill")
_poll_lock = threading.Lock()
_poll_thread: threading.Thread | None = None
_poll_stop = threading.Event()
_poll_webspace_id = ""
_index_lock = threading.Lock()
_index_thread: threading.Thread | None = None
_index_stop = threading.Event()
_stream_lock = threading.Lock()
_last_stream_fingerprints: dict[tuple[str, str], str] = {}
_snapshot_seen_at: dict[tuple[str, str], float] = {}
_active_receivers_by_webspace: dict[str, set[str]] = {}


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _count_label(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except Exception:
        return "0"


def _memory_get(key: str, default: Any = None) -> Any:
    try:
        return skill_memory.get(key, default)
    except Exception:
        return default


def _memory_set(key: str, value: Any) -> None:
    try:
        skill_memory.set(key, value)
    except Exception:
        _log.debug("failed to write skill memory key=%s", key, exc_info=True)


def _source_dir(source_dir: str | None = None) -> Path:
    token = _text(source_dir)
    return Path(token or os.environ.get("SLIDESHOW_SOURCE_DIR") or r"C:\Users\Zver\Pictures")


def _internal_data_dir() -> Path:
    override = os.environ.get("SLIDESHOW_DATA_DIR")
    if override:
        base = Path(override)
    else:
        try:
            env_path = skill_env_path()
            data_root = env_path.parents[1] if env_path.parent.name == "db" else env_path.parent
            base = data_root / "internal" / "slideshow_skill"
        except Exception:
            base = Path(__file__).resolve().parents[1] / ".skill_state"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _index_path() -> Path:
    return _internal_data_dir() / "photos.sqlite3"


def _thumb_dir(path: Path) -> Path:
    base = path.parent / ".adaos-thumbs"
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except Exception:
        fallback = _internal_data_dir() / "thumbs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _default_state() -> dict[str, Any]:
    return {
        "selected_codes": [],
        "sync": True,
        "mode": "sequential",
        "scope": "all",
        "display_mode": "fit",
        "fullscreen": True,
        "running": False,
        "interval_ms": 7000,
        "last_service_tick_at": 0,
        "current_index": 0,
        "favorites": [],
        "source_dir": str(_source_dir()),
        "selected_folder": "",
        "last_event_by_code": {},
        "endpoint_index_by_code": {},
    }


def _load_state() -> dict[str, Any]:
    data = _memory_get(_STATE_KEY, {})
    state = _default_state()
    if isinstance(data, Mapping):
        state.update(dict(data))
    state["selected_codes"] = _unique_texts(state.get("selected_codes"))
    state["favorites"] = _unique_texts(state.get("favorites"))
    state["sync"] = bool(state.get("sync", True))
    state["fullscreen"] = bool(state.get("fullscreen", True))
    state["running"] = bool(state.get("running", False))
    if _text(state.get("mode")) not in {"sequential", "random"}:
        state["mode"] = "sequential"
    if _text(state.get("scope")) not in {"all", "favorites"}:
        state["scope"] = "all"
    if _text(state.get("display_mode")) not in {"fit", "crop"}:
        state["display_mode"] = "fit"
    state["source_dir"] = str(_source_dir(_text(state.get("source_dir"))))
    state["selected_folder"] = _text(state.get("selected_folder"))
    try:
        state["interval_ms"] = max(1500, min(60000, int(state.get("interval_ms") or 7000)))
    except Exception:
        state["interval_ms"] = 7000
    try:
        state["current_index"] = max(0, int(state.get("current_index") or 0))
    except Exception:
        state["current_index"] = 0
    try:
        state["last_service_tick_at"] = max(0.0, float(state.get("last_service_tick_at") or 0))
    except Exception:
        state["last_service_tick_at"] = 0
    if not isinstance(state.get("last_event_by_code"), Mapping):
        state["last_event_by_code"] = {}
    if not isinstance(state.get("endpoint_index_by_code"), Mapping):
        state["endpoint_index_by_code"] = {}
    return state


def _save_state(state: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    payload["selected_codes"] = _unique_texts(payload.get("selected_codes"))
    payload["favorites"] = _unique_texts(payload.get("favorites"))
    _memory_set(_STATE_KEY, payload)
    return payload


def _unique_texts(value: Any) -> list[str]:
    raw = list(value) if isinstance(value, list) else str(value or "").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        token = _text(item)
        folded = token.casefold()
        if not token or folded in seen:
            continue
        seen.add(folded)
        out.append(token)
    return out


def _node_id() -> str:
    return os.environ.get("ADAOS_NODE_ID") or os.environ.get("NODE_ID") or f"node:{socket.gethostname().lower()}"


def _owner() -> dict[str, str]:
    node_id = _node_id()
    return {"node_id": node_id, "skill_id": "slideshow_skill", "target": f"{node_id}:slideshow_skill"}


def _json_fingerprint(value: Any) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    except Exception:
        raw = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


def _stream_key(webspace_id: str | None, receiver: str) -> tuple[str, str]:
    return (_text(webspace_id) or default_webspace_id(), _text(receiver))


def _remember_receiver(webspace_id: str | None, receiver: str) -> None:
    token = _text(receiver)
    if not token:
        return
    ws = _text(webspace_id) or default_webspace_id()
    with _stream_lock:
        _active_receivers_by_webspace.setdefault(ws, set()).add(token)


def _forget_receiver(webspace_id: str | None, receiver: str) -> None:
    token = _text(receiver)
    if not token:
        return
    ws = _text(webspace_id) or default_webspace_id()
    key = (ws, token)
    with _stream_lock:
        receivers = _active_receivers_by_webspace.get(ws)
        if receivers is not None:
            receivers.discard(token)
            if not receivers:
                _active_receivers_by_webspace.pop(ws, None)
        _last_stream_fingerprints.pop(key, None)
        _snapshot_seen_at.pop(key, None)


def _consume_snapshot_request(webspace_id: str | None, receiver: str) -> bool:
    key = _stream_key(webspace_id, receiver)
    now = time.monotonic()
    with _stream_lock:
        last = float(_snapshot_seen_at.get(key) or 0.0)
        _snapshot_seen_at[key] = now
    return last <= 0 or now - last >= _SNAPSHOT_DEBOUNCE_S


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:24]


def _content_ref(path: Path) -> str:
    return f"content:sha256:{_fingerprint(path)}"


def _connect_index() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_index_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS photos (
            content_ref TEXT PRIMARY KEY,
            source_path TEXT UNIQUE NOT NULL,
            root_dir TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            top_folder TEXT NOT NULL,
            source_name TEXT NOT NULL,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime INTEGER NOT NULL,
            favorite INTEGER NOT NULL DEFAULT 0,
            hidden INTEGER NOT NULL DEFAULT 0,
            indexed_at REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_root_mtime ON photos(root_dir, mtime DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_root_folder ON photos(root_dir, top_folder, mtime DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_root_favorite ON photos(root_dir, favorite, mtime DESC)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS roots (
            root_dir TEXT PRIMARY KEY,
            indexed_at REAL NOT NULL,
            photo_count INTEGER NOT NULL
        )
        """
    )
    return conn


def _top_folder(root: Path, path: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except Exception:
        rel = Path(path.name)
    parts = rel.parts
    return parts[0] if len(parts) > 1 else ""


def _rel_path(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return path.name


def _index_meta(root: Path) -> dict[str, Any]:
    try:
        with _connect_index() as conn:
            row = conn.execute("SELECT indexed_at, photo_count FROM roots WHERE root_dir = ?", (str(root),)).fetchone()
    except Exception:
        return {}
    if row is None:
        return {}
    return {"root_dir": str(root), "indexed_at": float(row["indexed_at"]), "photo_count": int(row["photo_count"])}


def _index_status(root: Path | None = None) -> dict[str, Any]:
    data = _memory_get(_INDEX_STATUS_KEY, {})
    status = dict(data) if isinstance(data, Mapping) else {}
    requested_source_dir = str(root) if root is not None else ""
    saved_source_dir = _text(status.get("source_dir"))
    if requested_source_dir and saved_source_dir and saved_source_dir.casefold() != requested_source_dir.casefold():
        status = {}
    source_dir = _text(status.get("source_dir")) or requested_source_dir
    meta = _index_meta(_source_dir(source_dir)) if source_dir else {}
    if not status:
        status = {
            "ok": bool(meta),
            "status": "ready" if meta else "idle",
            "source_dir": source_dir,
            "visited_files": 0,
            "indexed_count": int(meta.get("photo_count") or 0),
            "photo_count": int(meta.get("photo_count") or 0),
            "folder_count": 0,
            "started_at": None,
            "updated_at": _now(),
            "completed_at": None,
        }
    if _text(status.get("status")) == "running":
        with _index_lock:
            alive = _index_thread is not None and _index_thread.is_alive()
        if not alive:
            status["status"] = "interrupted"
            status["message"] = "Indexing was interrupted. Press Refresh index to resume."
            _memory_set(_INDEX_STATUS_KEY, status)
    if meta and _text(status.get("status")) not in {"running", "canceling"}:
        status["photo_count"] = int(meta.get("photo_count") or status.get("photo_count") or 0)
        status["indexed_count"] = int(meta.get("photo_count") or status.get("indexed_count") or 0)
    status["value"] = _count_label(status.get("indexed_count") or status.get("photo_count"))
    status["label"] = _text(status.get("status")) or "idle"
    status["description"] = _text(status.get("message")) or _index_message(status)
    status["color"] = {
        "running": "primary",
        "ready": "success",
        "completed": "success",
        "failed": "danger",
        "interrupted": "warning",
        "canceling": "warning",
        "canceled": "warning",
    }.get(_text(status.get("status")), "warning")
    return status


def _index_message(status: Mapping[str, Any]) -> str:
    state = _text(status.get("status")) or "idle"
    source = _text(status.get("source_dir"))
    indexed = _count_label(status.get("indexed_count") or status.get("photo_count"))
    visited = _count_label(status.get("visited_files"))
    folders = _count_label(status.get("folder_count"))
    if state == "running":
        return f"Indexing {source}: {indexed} photos, {folders} folders, {visited} files visited."
    if state in {"completed", "ready"}:
        return f"Index ready for {source}: {indexed} photos, {folders} folders."
    if state == "failed":
        return f"Index failed for {source}: {_text(status.get('error'))}"
    if state == "canceled":
        return f"Index canceled for {source}: {indexed} photos kept."
    if state == "interrupted":
        return f"Index interrupted for {source}: press Refresh index to resume."
    if source:
        return f"Index idle for {source}: press Refresh index."
    return "Index idle."


def _set_index_status(payload: Mapping[str, Any], webspace_id: str | None = None) -> dict[str, Any]:
    status = dict(payload)
    status["updated_at"] = _now()
    status["message"] = _index_message(status)
    _memory_set(_INDEX_STATUS_KEY, status)
    normalized = _index_status(_source_dir(status.get("source_dir")))
    _publish(_INDEX_RECEIVER, normalized, webspace_id)
    return normalized


def _ensure_index(root: Path, *, force: bool = False) -> dict[str, Any]:
    root = root.expanduser()
    if not root.exists():
        return {"ok": False, "error": "source_dir_missing", "root_dir": str(root), "photo_count": 0}
    existing = _index_meta(root)
    if existing:
        return {"ok": True, **existing, "source": "cache"}
    return {"ok": False, "error": "index_missing", "root_dir": str(root), "photo_count": 0, "source": "none"}


def _upsert_index_row(conn: sqlite3.Connection, root: Path, path: Path, scan_started: float) -> str | None:
    if path.suffix.lower() not in _SUPPORTED:
        return None
    try:
        stat = path.stat()
        ref = _content_ref(path)
        existing = conn.execute(
            """
            SELECT favorite, hidden FROM photos
            WHERE root_dir = ? AND (source_path = ? OR content_ref = ?)
            ORDER BY CASE WHEN source_path = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (str(root), str(path), ref, str(path)),
        ).fetchone()
        favorite = int(existing["favorite"]) if existing is not None else 0
        hidden = int(existing["hidden"]) if existing is not None else 0
        conn.execute(
            "DELETE FROM photos WHERE root_dir = ? AND (source_path = ? OR content_ref = ?)",
            (str(root), str(path), ref),
        )
        conn.execute(
            """
            INSERT INTO photos (
                content_ref, source_path, root_dir, rel_path, top_folder,
                source_name, ext, size, mtime, favorite, hidden, indexed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref,
                str(path),
                str(root),
                _rel_path(root, path),
                _top_folder(root, path),
                path.name,
                path.suffix.lower(),
                int(stat.st_size),
                int(stat.st_mtime),
                favorite,
                hidden,
                scan_started,
            ),
        )
        return ref
    except Exception:
        _log.debug("failed to index slideshow photo path=%s", path, exc_info=True)
        return None


def _scan_index(root: Path, *, job_id: str, webspace_id: str | None = None) -> dict[str, Any]:
    root = root.expanduser()
    if not root.exists():
        return _set_index_status(
            {
                "ok": False,
                "job_id": job_id,
                "status": "failed",
                "source_dir": str(root),
                "error": "source_dir_missing",
                "visited_files": 0,
                "indexed_count": 0,
                "photo_count": 0,
                "folder_count": 0,
                "started_at": _now(),
                "completed_at": _now(),
            },
            webspace_id,
        )

    started = time.time()
    started_at = _now()
    scan_started = time.time()
    visited_files = 0
    indexed_count = 0
    folders: set[str] = set()
    last_publish = 0.0
    _set_index_status(
        {
            "ok": True,
            "job_id": job_id,
            "status": "running",
            "source_dir": str(root),
            "visited_files": 0,
            "indexed_count": 0,
            "photo_count": int(_index_meta(root).get("photo_count") or 0),
            "folder_count": 0,
            "started_at": started_at,
            "completed_at": None,
        },
        webspace_id,
    )

    try:
        with _connect_index() as conn:
            for current, dirs, names in os.walk(str(root)):
                dirs[:] = [name for name in dirs if name != ".adaos-thumbs"]
                if _index_stop.is_set():
                    conn.commit()
                    return _set_index_status(
                        {
                            "ok": True,
                            "job_id": job_id,
                            "status": "canceled",
                            "source_dir": str(root),
                            "visited_files": visited_files,
                            "indexed_count": indexed_count,
                            "photo_count": indexed_count,
                            "folder_count": len(folders),
                            "started_at": started_at,
                            "completed_at": _now(),
                        },
                        webspace_id,
                    )
                for name in names:
                    visited_files += 1
                    path = Path(current) / name
                    ref = _upsert_index_row(conn, root, path, scan_started)
                    if ref:
                        indexed_count += 1
                        folder = _top_folder(root, path)
                        if folder:
                            folders.add(folder)
                    if indexed_count and indexed_count % _INDEX_BATCH_SIZE == 0:
                        conn.commit()
                    now = time.time()
                    if now - last_publish >= _INDEX_PUBLISH_INTERVAL_S:
                        last_publish = now
                        _set_index_status(
                            {
                                "ok": True,
                                "job_id": job_id,
                                "status": "running",
                                "source_dir": str(root),
                                "visited_files": visited_files,
                                "indexed_count": indexed_count,
                                "photo_count": indexed_count,
                                "folder_count": len(folders),
                                "started_at": started_at,
                                "completed_at": None,
                            },
                            webspace_id,
                        )
            conn.commit()
            conn.execute("DELETE FROM photos WHERE root_dir = ? AND indexed_at != ?", (str(root), scan_started))
            row = conn.execute("SELECT COUNT(*) AS count FROM photos WHERE root_dir = ? AND hidden = 0", (str(root),)).fetchone()
            indexed_count = int(row["count"] or 0) if row is not None else indexed_count
            conn.execute(
                "INSERT OR REPLACE INTO roots(root_dir, indexed_at, photo_count) VALUES (?, ?, ?)",
                (str(root), scan_started, indexed_count),
            )
            conn.commit()
    except Exception as exc:
        _log.exception("slideshow photo index failed root=%s", root)
        return _set_index_status(
            {
                "ok": False,
                "job_id": job_id,
                "status": "failed",
                "source_dir": str(root),
                "error": str(exc),
                "visited_files": visited_files,
                "indexed_count": indexed_count,
                "photo_count": indexed_count,
                "folder_count": len(folders),
                "started_at": started_at,
                "completed_at": _now(),
                "duration_s": round(time.time() - started, 3),
            },
            webspace_id,
        )

    meta = {
        "ok": True,
        "root_dir": str(root),
        "indexed_at": scan_started,
        "photo_count": indexed_count,
        "duration_s": round(time.time() - started, 3),
        "source": "scan",
    }
    _memory_set(_INDEX_META_KEY, meta)
    status = _set_index_status(
        {
            "ok": True,
            "job_id": job_id,
            "status": "completed",
            "source_dir": str(root),
            "visited_files": visited_files,
            "indexed_count": indexed_count,
            "photo_count": indexed_count,
            "folder_count": len(folders),
            "started_at": started_at,
            "completed_at": _now(),
            "duration_s": round(time.time() - started, 3),
        },
        webspace_id,
    )
    state = _load_state()
    if _text(state.get("source_dir")) == str(root):
        _publish(_FOLDERS_RECEIVER, _folders_payload(state), webspace_id)
        _publish(_PREVIEW_RECEIVER, _preview_payload(state, 48), webspace_id)
        _publish(_SESSION_RECEIVER, _session_payload(state, _files_for_state(state, _MAX_CONTROL_SCAN)), webspace_id)
    return status


def _start_index_job(root: Path, *, webspace_id: str | None = None) -> dict[str, Any]:
    global _index_thread
    root = root.expanduser()
    job_id = "idx:" + hashlib.sha256(f"{root}:{time.time()}".encode("utf-8")).hexdigest()[:16]
    with _index_lock:
        if _index_thread is not None and _index_thread.is_alive():
            current = _index_status()
            if _text(current.get("source_dir")).casefold() == str(root).casefold():
                return current
            return {**current, "ok": False, "error": "indexer_busy", "requested_source_dir": str(root)}
        _index_stop.clear()
        _index_thread = threading.Thread(
            target=_scan_index,
            kwargs={"root": root, "job_id": job_id, "webspace_id": webspace_id},
            name="slideshow-photo-index",
            daemon=True,
        )
        _index_thread.start()
    return _index_status(root)


def _cancel_index_job(webspace_id: str | None = None) -> dict[str, Any]:
    with _index_lock:
        running = _index_thread is not None and _index_thread.is_alive()
    if running:
        _index_stop.set()
        status = _index_status()
        status["status"] = "canceling"
        return _set_index_status(status, webspace_id)
    return _index_status()


def dispose(reason: str | None = None, **_: Any) -> dict[str, Any]:
    _poll_stop.set()
    _index_stop.set()
    with _stream_lock:
        active_receiver_total = sum(len(items) for items in _active_receivers_by_webspace.values())
        _active_receivers_by_webspace.clear()
        _last_stream_fingerprints.clear()
        _snapshot_seen_at.clear()
    return {
        "ok": True,
        "reason": _text(reason) or "dispose",
        "active_receiver_total": active_receiver_total,
        "updated_at": _now(),
    }


def on_quarantine(
    ttl_s: float | None = None,
    reason: str | None = None,
    metrics: Mapping[str, Any] | None = None,
    webspace_id: str | None = None,
    owner: Mapping[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    result = dispose(reason="quarantine")
    incident = {
        "schema": "adaos.slideshow_skill.quarantine.v1",
        "event": "skill.quarantine",
        "reason": _text(reason) or "unknown",
        "ttl_s": ttl_s,
        "webspace_id": _text(webspace_id) or None,
        "owner": dict(owner or {}),
        "metrics": dict(metrics or {}),
        "updated_at": _now(),
    }
    try:
        log_dir = _internal_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "quarantine.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(incident, ensure_ascii=False, default=str) + "\n")
    except Exception:
        _log.debug("failed to write slideshow quarantine incident", exc_info=True)
    return {**result, "incident": incident}


def _query_photo_records(
    state: Mapping[str, Any],
    *,
    limit: int | None = None,
    favorites_only: bool | None = None,
) -> list[dict[str, Any]]:
    root = _source_dir(_text(state.get("source_dir")))
    _ensure_index(root)
    selected_folder = _text(state.get("selected_folder"))
    fav_only = bool(favorites_only if favorites_only is not None else _text(state.get("scope")) == "favorites")
    clauses = ["root_dir = ?", "hidden = 0"]
    params: list[Any] = [str(root)]
    if selected_folder:
        clauses.append("top_folder = ?")
        params.append(selected_folder)
    if fav_only:
        clauses.append("favorite = 1")
    sql = "SELECT * FROM photos WHERE " + " AND ".join(clauses) + " ORDER BY mtime DESC, source_name ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
    try:
        with _connect_index() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except Exception:
        _log.debug("failed to query slideshow index", exc_info=True)
        return []


def _photo_files(source_dir: str | None = None, limit: int | None = None) -> list[Path]:
    state = _load_state()
    if source_dir:
        state["source_dir"] = str(_source_dir(source_dir))
    return [Path(row["source_path"]) for row in _query_photo_records(state, limit=limit)]


def _files_for_state(state: Mapping[str, Any], limit: int | None = None) -> list[Path]:
    return [Path(row["source_path"]) for row in _query_photo_records(state, limit=limit)]


def _favorite_files_for_state(state: Mapping[str, Any], limit: int | None = None) -> list[Path]:
    return [Path(row["source_path"]) for row in _query_photo_records(state, limit=limit, favorites_only=True)]


def _favorite_refs(root: Path) -> list[str]:
    try:
        with _connect_index() as conn:
            rows = conn.execute(
                "SELECT content_ref FROM photos WHERE root_dir = ? AND favorite = 1 ORDER BY mtime DESC",
                (str(root),),
            ).fetchall()
        return [str(row["content_ref"]) for row in rows]
    except Exception:
        return []


def _set_favorite(root: Path, content_ref: str, favorite: bool) -> None:
    try:
        with _connect_index() as conn:
            conn.execute(
                "UPDATE photos SET favorite = ? WHERE root_dir = ? AND content_ref = ?",
                (1 if favorite else 0, str(root), content_ref),
            )
    except Exception:
        _log.debug("failed to update slideshow favorite ref=%s", content_ref, exc_info=True)


def _set_hidden(root: Path, content_ref: str, hidden: bool) -> None:
    try:
        with _connect_index() as conn:
            conn.execute(
                "UPDATE photos SET hidden = ? WHERE root_dir = ? AND content_ref = ?",
                (1 if hidden else 0, str(root), content_ref),
            )
    except Exception:
        _log.debug("failed to update slideshow hidden ref=%s", content_ref, exc_info=True)


def _is_favorite(root: Path, content_ref: str) -> bool:
    try:
        with _connect_index() as conn:
            row = conn.execute(
                "SELECT favorite FROM photos WHERE root_dir = ? AND content_ref = ?",
                (str(root), content_ref),
            ).fetchone()
        return bool(row and int(row["favorite"]) == 1)
    except Exception:
        return False


def _folder_items(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    root = _source_dir(_text(state.get("source_dir")))
    _ensure_index(root)
    selected = _text(state.get("selected_folder"))
    items = [{"id": "", "title": "All photos", "subtitle": str(root), "selected": selected == "", "selectable": True}]
    total_folders = 0
    try:
        with _connect_index() as conn:
            count_row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM (
                    SELECT top_folder FROM photos
                    WHERE root_dir = ? AND hidden = 0 AND top_folder != ''
                    GROUP BY top_folder
                )
                """,
                (str(root),),
            ).fetchone()
            total_folders = int(count_row["count"] or 0) if count_row is not None else 0
            rows = conn.execute(
                """
                SELECT top_folder, COUNT(*) AS count,
                       SUM(CASE WHEN favorite = 1 THEN 1 ELSE 0 END) AS favorites
                FROM photos
                WHERE root_dir = ? AND hidden = 0 AND top_folder != ''
                GROUP BY top_folder
                ORDER BY top_folder ASC
                LIMIT ?
                """,
                (str(root), max(1, _MAX_FOLDER_STREAM_ITEMS - 1)),
            ).fetchall()
    except Exception:
        rows = []
    for row in rows:
        folder = str(row["top_folder"] or "")
        count = int(row["count"] or 0)
        favorites = int(row["favorites"] or 0)
        items.append(
            {
                "id": folder,
                "title": folder,
                "subtitle": f"{count} photos | {favorites} favorites",
                "count": count,
                "favorites": favorites,
                "selected": folder == selected,
                "selectable": True,
            }
        )
    if total_folders > len(rows):
        items.append(
            {
                "id": "__more__",
                "title": "More folders not shown",
                "subtitle": f"{total_folders - len(rows)} more top-level folders. Narrow the source root if needed.",
                "count": total_folders - len(rows),
                "favorites": 0,
                "selected": False,
                "selectable": False,
            }
        )
    return items


def _thumbnail(path: Path, size: tuple[int, int], label: str) -> tuple[Path, bool]:
    cache_path = _thumb_dir(path) / f"{_fingerprint(path)}-{label}.jpg"
    if cache_path.exists():
        return cache_path, True
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        if getattr(image, "is_animated", False):
            image.seek(0)
        image.thumbnail(size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", image.size, "black")
        if image.mode in {"RGBA", "LA"}:
            canvas.paste(image, mask=image.getchannel("A"))
        else:
            canvas.paste(image.convert("RGB"))
        if label.startswith("endpoint-cache"):
            quality = 62
        else:
            quality = 62 if label.startswith("endpoint") else _thumbnail_quality(label, default=78)
        canvas.save(cache_path, "JPEG", quality=quality, optimize=True)
    return cache_path, False


def _thumbnail_quality(label: str, *, default: int) -> int:
    token = str(label or "")
    marker = "-q"
    if marker not in token:
        return default
    try:
        value = int(token.rsplit(marker, 1)[-1].split("-", 1)[0])
        return max(40, min(90, value))
    except Exception:
        return default


def _widget_thumbnail(path: Path) -> tuple[Path, bool]:
    # Keep browser stream payloads under the declared 98 KB receiver budget.
    # The best cached candidate wins, but detailed photos degrade gracefully.
    options = [
        ((720, 405), 78),
        ((640, 360), 74),
        ((560, 315), 72),
        ((480, 270), 70),
        ((384, 216), 68),
    ]
    last: tuple[Path, bool] | None = None
    for size, quality in options:
        thumb, cached = _thumbnail(path, size, f"widget-v4-{size[0]}x{size[1]}-q{quality}")
        last = (thumb, cached)
        try:
            if thumb.stat().st_size <= _WIDGET_IMAGE_BUDGET_BYTES:
                return thumb, cached
        except Exception:
            return thumb, cached
    return last if last is not None else _thumbnail(path, _WIDGET_SIZE, "widget-v4")


def _data_uri(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _api_token() -> str:
    token = _text(os.environ.get("ADAOS_TOKEN"))
    if token:
        return token
    try:
        return _text(get_ctx().config.token) or "dev-local-token"
    except Exception:
        return "dev-local-token"


def _media_content_url(filename: str) -> str:
    token = _api_token()
    query = f"?token={quote(token)}" if token else ""
    return f"/api/node/media/files/content/{quote(filename)}{query}"


def _publish_media_file(path: Path, content_ref: str, *, variant: str = "widget") -> dict[str, Any]:
    suffix = "".join(ch for ch in _text(variant).lower() if ch.isalnum() or ch in {"-", "_"}) or "media"
    filename = f"slideshow-{hashlib.sha256(_text(content_ref).encode('utf-8')).hexdigest()[:24]}-{suffix}.jpg"
    try:
        target = media_file_path(filename)
        if not target.exists() or target.stat().st_size != path.stat().st_size:
            shutil.copyfile(path, target)
        payload = {
            "ok": True,
            "filename": target.name,
            "path": str(target),
            "url": _media_content_url(target.name),
            "mime": "image/jpeg",
            "size_bytes": int(target.stat().st_size),
            "content_ref": content_ref,
            "route": "node_media_file",
        }
        _memory_set(_LAST_MEDIA_KEY, payload)
        return payload
    except Exception as exc:
        _log.debug("failed to publish slideshow media file", exc_info=True)
        return {"ok": False, "error": str(exc), "content_ref": content_ref}


def _content_item(path: Path) -> dict[str, Any]:
    thumb, cached = _thumbnail(path, _ENDPOINT_SIZE, "endpoint-cache-v6")
    ref = _content_ref(path)
    media = _publish_media_file(thumb, ref, variant="endpoint")
    return {
        "content_ref": ref,
        "source_path": str(path),
        "source_name": path.name,
        "title": path.stem,
        "mime": "image/jpeg",
        "cached": cached,
        "thumbnail_path": str(thumb),
        "thumbnail_bytes": thumb.stat().st_size,
        "content_url": _text(media.get("url")),
        "media": media,
        "data_uri": _data_uri(thumb),
    }


def _preview_item(path: Path) -> dict[str, Any]:
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    size = int(stat.st_size)
    ref = _content_ref(path)
    root = _source_dir(_load_state().get("source_dir"))
    return {
        "id": ref,
        "content_ref": ref,
        "title": path.name,
        "source_name": path.name,
        "source_path": str(path),
        "favorite": _is_favorite(root, ref),
        "size": size,
        "size_label": f"{size // 1024} KB" if size >= 1024 else f"{size} B",
        "modified_at": modified,
        "modified": modified[:19].replace("T", " "),
    }


def _selected_photos(files: list[Path], state: Mapping[str, Any]) -> list[Path]:
    return files


def _current_photo(files: list[Path], state: Mapping[str, Any]) -> Path | None:
    selected = _selected_photos(files, state)
    if not selected:
        return None
    index = int(state.get("current_index") or 0) % len(selected)
    return selected[index]


def _current_index(state: Mapping[str, Any], *, code: str | None = None) -> int:
    token = _text(code)
    if token and not state.get("sync"):
        by_code = _mapping(state.get("endpoint_index_by_code"))
        try:
            return max(0, int(by_code.get(token) or state.get("current_index") or 0))
        except Exception:
            return 0
    try:
        return max(0, int(state.get("current_index") or 0))
    except Exception:
        return 0


def _set_current_index(state: dict[str, Any], index: int, *, code: str | None = None) -> None:
    token = _text(code)
    if token and not state.get("sync"):
        by_code = dict(state.get("endpoint_index_by_code") or {})
        by_code[token] = max(0, int(index))
        state["endpoint_index_by_code"] = by_code
        return
    state["current_index"] = max(0, int(index))


def _endpoint_window(files: list[Path], state: Mapping[str, Any], *, code: str | None = None) -> list[Path]:
    selected = _selected_photos(files, state)
    if not selected:
        return []
    index = _current_index(state, code=code) % len(selected)
    if len(selected) <= _MAX_ENDPOINT_CURRENT:
        return selected[index:] + selected[:index]
    if _text(state.get("mode")) == "random":
        pool = [item for i, item in enumerate(selected) if i != index]
        random.shuffle(pool)
        return [selected[index], *pool[: _MAX_ENDPOINT_CURRENT - 1]]
    return [selected[(index + offset) % len(selected)] for offset in range(_MAX_ENDPOINT_CURRENT)]


def _content_items_for_window(window: list[Path]) -> tuple[list[dict[str, Any]], int, bool]:
    items: list[dict[str, Any]] = []
    content_bytes = 0
    budget_limited = False
    for path in window:
        item = _content_item(path)
        item_bytes = int(item.get("thumbnail_bytes") or 0)
        if items and content_bytes + item_bytes > _INLINE_CONTENT_BUDGET_BYTES:
            budget_limited = True
            break
        items.append(item)
        content_bytes += item_bytes
    return items, content_bytes, budget_limited


def _advance(state: dict[str, Any], files: list[Path], step: int) -> dict[str, Any]:
    return _advance_for_code(state, files, step)


def _advance_for_code(state: dict[str, Any], files: list[Path], step: int, *, code: str | None = None) -> dict[str, Any]:
    selected = _selected_photos(files, state)
    if not selected:
        _set_current_index(state, 0, code=code)
        return state
    if _text(state.get("mode")) == "random" and step != 0:
        _set_current_index(state, random.randrange(0, len(selected)), code=code)
    else:
        _set_current_index(state, (_current_index(state, code=code) + step) % len(selected), code=code)
    return state


def _toggle_current_favorite(state: dict[str, Any], files: list[Path]) -> dict[str, Any]:
    current = _current_photo(files, state)
    if current is None:
        return state
    ref = _content_ref(current)
    root = _source_dir(_text(state.get("source_dir")))
    _set_favorite(root, ref, not _is_favorite(root, ref))
    state["favorites"] = _favorite_refs(root)
    return state


def _toggle_favorite_ref(state: dict[str, Any], ref: str) -> dict[str, Any]:
    token = _text(ref)
    if not token:
        return state
    root = _source_dir(_text(state.get("source_dir")))
    _set_favorite(root, token, not _is_favorite(root, token))
    state["favorites"] = _favorite_refs(root)
    return state


def _set_current_favorite(state: dict[str, Any], files: list[Path], favorite: bool) -> dict[str, Any]:
    current = _current_photo(files, state)
    if current is None:
        return state
    root = _source_dir(_text(state.get("source_dir")))
    _set_favorite(root, _content_ref(current), favorite)
    state["favorites"] = _favorite_refs(root)
    return state


def _hide_ref(state: dict[str, Any], ref: str) -> dict[str, Any]:
    token = _text(ref)
    if not token:
        return state
    root = _source_dir(_text(state.get("source_dir")))
    _set_hidden(root, token, True)
    state["favorites"] = [item for item in _unique_texts(state.get("favorites")) if item != token]
    return state


def _hide_current_photo(state: dict[str, Any], files: list[Path]) -> dict[str, Any]:
    current = _current_photo(files, state)
    if current is None:
        return state
    return _hide_ref(state, _content_ref(current))


def _selected_endpoint_label(selected_codes: list[str]) -> str:
    if not selected_codes:
        return "No endpoint"
    try:
        devices = _load_devices()
        items = [compact_endpoint(item, selected_codes=set(selected_codes)) for item in devices]
        by_code = {_text(item.get("code")): _text(item.get("title")) for item in items}
        labels = [by_code.get(code) or code for code in selected_codes]
        return ", ".join(label for label in labels if label) or ", ".join(selected_codes)
    except Exception:
        return ", ".join(selected_codes)


def _session_payload(state: Mapping[str, Any], files: list[Path], *, last_command: Mapping[str, Any] | None = None) -> dict[str, Any]:
    current = _current_photo(files, state)
    image: dict[str, Any] = {"src": "", "mime": "image/jpeg"}
    title = "No photo"
    content_ref = ""
    media: dict[str, Any] = {}
    if current is not None:
        thumb, _cached = _widget_thumbnail(current)
        title = current.name
        content_ref = _content_ref(current)
        media = _publish_media_file(thumb, content_ref, variant="widget")
        image = {
            "src": _text(media.get("url")) if media.get("ok") else "",
            "mime": "image/jpeg",
            "route": media.get("route") or "node_media_file",
            "content_ref": content_ref,
        }
    selected_codes = _unique_texts(state.get("selected_codes"))
    header = _selected_endpoint_label(selected_codes)
    root = _source_dir(_text(state.get("source_dir")))
    favorites = _favorite_refs(root)
    filtered_count = len(_selected_photos(files, state))
    return {
        "ok": bool(current),
        "title": title,
        "subtitle": f"{header} | {state.get('mode')} | {state.get('scope')}",
        "value": title,
        "label": header,
        "description": f"{filtered_count} photos, {len(favorites)} favorites",
        "image": image,
        "media": media,
        "frame": {"label": f"{(int(state.get('current_index') or 0) + 1) if files else 0}/{filtered_count}"},
        "status": {"label": "sync" if state.get("sync") else "independent", "color": "success" if state.get("sync") else "warning"},
        "content_ref": content_ref,
        "selected_codes": selected_codes,
        "sync": bool(state.get("sync")),
        "sync_value": "sync_on" if state.get("sync") else "sync_off",
        "mode": state.get("mode"),
        "scope": state.get("scope"),
        "display_mode": state.get("display_mode"),
        "fullscreen": bool(state.get("fullscreen")),
        "fullscreen_value": "fullscreen_on" if state.get("fullscreen") else "fullscreen_off",
        "running": bool(state.get("running")),
        "run_value": "start" if state.get("running") else "stop",
        "selected_folder": _text(state.get("selected_folder")),
        "source_dir": str(root),
        "buttons": [
            {"id": "prev", "label": "Prev"},
            {"id": "next", "label": "Next"},
            {"id": "fav", "label": "Fav"},
        ],
        "last_command": dict(last_command or {}),
        "updated_at": _now(),
    }


def _publish(receiver: str, payload: Mapping[str, Any], webspace_id: str | None = None, *, force: bool = False) -> None:
    token = _text(receiver)
    if not token:
        return
    ws = _text(webspace_id) or default_webspace_id()
    fingerprint = _json_fingerprint(payload)
    key = (ws, token)
    with _stream_lock:
        if not force and _last_stream_fingerprints.get(key) == fingerprint:
            return
        _last_stream_fingerprints[key] = fingerprint
    try:
        stream_publish(token, dict(payload), _meta={"webspace_id": ws})
    except Exception:
        _log.debug("failed to publish slideshow stream receiver=%s", token, exc_info=True)


def _load_devices() -> list[dict[str, Any]]:
    return sdk_list_endpoints(sync_registry=True)


def _select_device(devices: list[Mapping[str, Any]], code: str | None = None) -> Mapping[str, Any] | None:
    if code:
        for item in devices:
            if _text(item.get("code")) == _text(code):
                return item
        return None
    admitted = [item for item in devices if _text(item.get("state")) in {"approved", "consumed"}]
    if not admitted:
        return devices[0] if devices else None
    admitted.sort(key=lambda item: 0 if compact_endpoint(item).get("online_state") == "online" else 1)
    return admitted[0]


def _endpoint_payload(devices: list[dict[str, Any]], state: Mapping[str, Any]) -> dict[str, Any]:
    selected_codes = set(_unique_texts(state.get("selected_codes")))
    items = [compact_endpoint(item, selected_codes=selected_codes) for item in devices]
    selected_items = [
        {
            "id": f"selected:{item['code']}",
            "title": item["title"],
            "subtitle": f"{item['online_state']} | seen {item['last_seen']} | code {item['code']}",
            "content": item,
        }
        for item in items
        if item.get("selected")
    ]
    if not selected_items:
        selected_items = [
            {
                "id": "selected:none",
                "title": "No endpoint selected",
                "subtitle": "Refresh endpoints, then add one or more admitted ReDevice endpoints.",
            }
        ]
    return {
        "ok": True,
        "selected_codes": list(selected_codes),
        "selected_items": selected_items,
        "items": items,
        "owner": _owner(),
        "source_dir": state.get("source_dir"),
        "selected_folder": _text(state.get("selected_folder")),
        "sync": bool(state.get("sync")),
        "mode": state.get("mode"),
        "scope": state.get("scope"),
        "display_mode": state.get("display_mode"),
        "fullscreen": bool(state.get("fullscreen")),
        "running": bool(state.get("running")),
        "updated_at": _now(),
    }


def _command_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    results = list(payload.get("results") or [])
    title = "Slideshow command queued" if payload.get("ok") else "Slideshow command failed"
    return [
        {
            "id": _text(payload.get("command_id")) or "last-command",
            "title": title,
            "subtitle": f"{len(results)} endpoints | {payload.get('item_count', 0)} cached photos",
            "content": dict(payload),
        }
    ]


def _empty_command_payload() -> dict[str, Any]:
    return {
        "ok": False,
        "command_id": "",
        "command_items": [],
        "items": [],
        "updated_at": _now(),
    }


def _remember_command_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    compact = dict(payload)
    if "command_items" not in compact:
        compact["command_items"] = _command_items(compact)
    _memory_set(_COMMAND_STATE_KEY, compact)
    return compact


def _last_command_payload() -> dict[str, Any]:
    data = _memory_get(_COMMAND_STATE_KEY, {})
    if isinstance(data, Mapping):
        payload = dict(data)
        payload.setdefault("command_items", _command_items(payload))
        return payload
    return _empty_command_payload()


def _build_command(
    pair_code: str,
    items: list[dict[str, Any]],
    state: Mapping[str, Any],
    *,
    autoplay: bool,
    transport: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    command_id = "cmd:slideshow:" + hashlib.sha256(f"{pair_code}:{_now()}".encode("utf-8")).hexdigest()[:16]
    owner = _owner()
    transport_payload = dict(transport or {})
    return {
        "command_id": command_id,
        "type": "display.render_surface",
        "owner": owner,
        "transport": transport_payload,
        "active_app": {
            "app_id": "slideshow_skill",
            "skill_id": "slideshow_skill",
            "label": "Slideshow",
            "fullscreen": bool(state.get("fullscreen")),
            "owner": owner,
        },
        "binding": {
            "binding_id": f"bind:slideshow:{pair_code}",
            "role": "slideshow_frame",
            "owner": owner,
            "sync_group_id": "slideshow_skill:default" if state.get("sync") else f"slideshow_skill:{pair_code}",
            "events": {
                "next": {"target": owner["target"], "handler": "slideshow.next"},
                "favorite_toggle": {"target": owner["target"], "handler": "slideshow.favorite_toggle"},
                "hide_item": {"target": owner["target"], "handler": "slideshow.hide_item"},
            },
        },
        "payload": {
            "surface_ref": "slideshow.viewer",
            "surface_id": f"surface:slideshow:{command_id.split(':')[-1]}",
            "active_app": {
                "app_id": "slideshow_skill",
                "skill_id": "slideshow_skill",
                "label": "Slideshow",
                "fullscreen": bool(state.get("fullscreen")),
            },
            "fullscreen": bool(state.get("fullscreen")),
            "display_mode": _text(state.get("display_mode")) or "fit",
            "autoplay": bool(autoplay),
            "interval_ms": int(state.get("interval_ms") or 7000),
            "sync": bool(state.get("sync")),
            "mode": state.get("mode"),
            "scope": state.get("scope"),
            "transport": transport_payload,
            "cache_policy": {
                "max_current_items": _MAX_ENDPOINT_CURRENT,
                "max_favorite_items": 0,
                "receiver_cache_items": _MAX_ENDPOINT_CURRENT,
                "target_current_items": _MAX_ENDPOINT_CURRENT,
                "inline_content_budget_bytes": _INLINE_CONTENT_BUDGET_BYTES,
            },
            "items": items,
            "controls": {
                "hardware": {"volume_up": "next", "volume_down": "favorite_toggle"},
                "touch": {"tap": "favorite_toggle"},
            },
        },
    }


def _send_to_selected(
    state: Mapping[str, Any],
    files: list[Path],
    *,
    code: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    devices = _load_devices()
    configured_codes = _unique_texts(state.get("selected_codes"))
    target_codes = _unique_texts([code]) if code else configured_codes
    if not target_codes:
        device = _select_device(devices)
        target_codes = [_text(device.get("code"))] if device else []
        if target_codes and isinstance(state, dict):
            state["selected_codes"] = target_codes
            _save_state(state)
    if not target_codes:
        return {"ok": False, "error": "no_redevice_endpoint"}
    if not _endpoint_window(files, state, code=target_codes[0] if target_codes else None):
        return {"ok": False, "error": "no_supported_photos", "source_dir": state.get("source_dir")}
    bridge = ReDeviceBridge()
    devices_by_code = {_text(item.get("code")): item for item in devices if _text(item.get("code"))}
    results: list[dict[str, Any]] = []
    transports: dict[str, Any] = {}
    items_by_code: dict[str, list[dict[str, Any]]] = {}
    first_items: list[dict[str, Any]] = []
    first_content_bytes = 0
    any_budget_limited = False
    first_command_id = ""
    for pair_code in target_codes:
        window = _endpoint_window(files, state, code=pair_code)
        items, content_bytes, budget_limited = _content_items_for_window(window)
        if not items:
            results.append(
                {
                    "code": pair_code,
                    "ok": False,
                    "error": "no_supported_photos",
                    "command_id": "",
                    "state": "failed",
                    "item_count": 0,
                    "content_bytes": 0,
                    "transport": {},
                }
            )
            continue
        items_by_code[pair_code] = items
        first_items = first_items or items
        first_content_bytes = first_content_bytes or content_bytes
        any_budget_limited = any_budget_limited or budget_limited
        transport = select_transport(
            devices_by_code.get(pair_code, {}),
            intent="display.slideshow",
            content_bytes=content_bytes,
            allow_root_relay=True,
        )
        transports[pair_code] = transport
        # The skill owns slideshow sequencing. Endpoint-side autoplay is kept off
        # so legacy devices do not loop over a small local cache window.
        command = _build_command(pair_code, items, state, autoplay=False, transport=transport)
        first_command_id = first_command_id or _text(command.get("command_id"))
        res = bridge.send_command(pair_code, command)
        queued = _mapping(res.get("command"))
        results.append(
            {
                "code": pair_code,
                "ok": bool(res.get("ok")),
                "error": res.get("error"),
                "command_id": queued.get("command_id") or command.get("command_id"),
                "state": queued.get("state"),
                "item_count": len(items),
                "content_bytes": content_bytes,
                "cache_budget_limited": budget_limited,
                "transport": transport,
            }
        )
    payload = {
        "ok": any(bool(item.get("ok")) for item in results),
        "command_id": first_command_id,
        "source_dir": state.get("source_dir"),
        "selected_codes": configured_codes or target_codes,
        "target_codes": target_codes,
        "item_count": len(first_items),
        "cache_target": _MAX_ENDPOINT_CURRENT,
        "cache_budget_limited": any_budget_limited,
        "content_bytes": first_content_bytes,
        "items": [{"source_name": item["source_name"], "thumbnail_path": item["thumbnail_path"], "cached": item["cached"]} for item in first_items],
        "items_by_code": {
            pair_code: [
                {"source_name": item["source_name"], "thumbnail_path": item["thumbnail_path"], "cached": item["cached"]}
                for item in pair_items
            ]
            for pair_code, pair_items in items_by_code.items()
        },
        "transport": next(iter(transports.values()), {}),
        "transports": transports,
        "results": results,
        "updated_at": _now(),
    }
    payload["command_items"] = _command_items(payload)
    payload = _remember_command_payload(payload)
    _publish(_COMMAND_RECEIVER, payload, webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, files, last_command=payload), webspace_id)
    return payload


def _stop_selected(
    state: Mapping[str, Any],
    *,
    code: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    devices = _load_devices()
    selected_codes = _unique_texts([code] if code else state.get("selected_codes"))
    if not selected_codes:
        device = _select_device(devices)
        selected_codes = [_text(device.get("code"))] if device else []
    bridge = ReDeviceBridge()
    results: list[dict[str, Any]] = []
    first_command_id = ""
    for pair_code in selected_codes:
        command_id = "cmd:slideshow-stop:" + hashlib.sha256(f"{pair_code}:{_now()}".encode("utf-8")).hexdigest()[:16]
        first_command_id = first_command_id or command_id
        command = {
            "command_id": command_id,
            "type": "display.clear_surface",
            "owner": _owner(),
            "payload": {
                "surface_ref": "slideshow.viewer",
                "surface_id": f"surface:slideshow:{command_id.split(':')[-1]}",
                "active_app": None,
                "fullscreen": False,
                "items": [],
            },
        }
        res = bridge.send_command(pair_code, command)
        queued = _mapping(res.get("command"))
        results.append(
            {
                "code": pair_code,
                "ok": bool(res.get("ok")),
                "error": res.get("error"),
                "command_id": queued.get("command_id") or command_id,
                "state": queued.get("state"),
            }
        )
    files = _files_for_state(state, _MAX_CONTROL_SCAN)
    payload = {
        "ok": any(bool(item.get("ok")) for item in results) if results else True,
        "command_id": first_command_id,
        "source_dir": state.get("source_dir"),
        "selected_codes": selected_codes,
        "item_count": 0,
        "items": [],
        "results": results,
        "updated_at": _now(),
    }
    payload["command_items"] = _command_items(payload)
    payload = _remember_command_payload(payload)
    _publish(_COMMAND_RECEIVER, payload, webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, files, last_command=payload), webspace_id)
    return payload


def _apply_root_events(
    state: dict[str, Any],
    devices: list[Mapping[str, Any]],
    files: list[Path],
    *,
    webspace_id: str | None = None,
    broadcast: bool = False,
) -> dict[str, Any]:
    selected = set(_unique_texts(state.get("selected_codes")))
    last_by_code = dict(state.get("last_event_by_code") or {})
    changed = False
    refresh_codes: set[str] = set()
    for item in devices:
        code = _text(item.get("code"))
        if not code or code not in selected:
            continue
        event = _mapping(item.get("last_event"))
        if _text(event.get("type")) != "endpoint.surface.event":
            continue
        event_id = f"{event.get('observed_at')}:{event.get('action')}:{event.get('item_ref')}"
        if not event_id or last_by_code.get(code) == event_id:
            continue
        last_by_code[code] = event_id
        action = _text(event.get("action"))
        if action == "next":
            _advance_for_code(state, files, 1, code=None if state.get("sync") else code)
            state["last_service_tick_at"] = time.time()
            changed = True
            if state.get("running"):
                if state.get("sync"):
                    refresh_codes.update(selected)
                else:
                    refresh_codes.add(code)
        elif action == "favorite_toggle":
            _toggle_favorite_ref(state, _text(event.get("item_ref")))
            changed = True
        elif action == "hide_item":
            _hide_ref(state, _text(event.get("item_ref")))
            changed = True
            if state.get("running"):
                if state.get("sync"):
                    refresh_codes.update(selected)
                else:
                    refresh_codes.add(code)
    state["last_event_by_code"] = last_by_code
    if changed:
        _save_state(state)
        if broadcast and state.get("sync"):
            _send_to_selected(state, _files_for_state(state, _MAX_CONTROL_SCAN), webspace_id=webspace_id)
        elif broadcast and refresh_codes:
            fresh_files = _files_for_state(state, _MAX_CONTROL_SCAN)
            for target_code in sorted(refresh_codes):
                _send_to_selected(state, fresh_files, code=target_code, webspace_id=webspace_id)
    return state


def _apply_service_tick(
    state: dict[str, Any],
    files: list[Path],
    *,
    webspace_id: str | None = None,
) -> bool:
    if not state.get("running"):
        return False
    if not _unique_texts(state.get("selected_codes")):
        return False
    if not _selected_photos(files, state):
        return False
    now = time.time()
    interval_s = max(1.5, min(60.0, float(state.get("interval_ms") or 7000) / 1000.0))
    last_tick = float(state.get("last_service_tick_at") or 0)
    if last_tick <= 0:
        state["last_service_tick_at"] = now
        _save_state(state)
        return False
    if now - last_tick < interval_s:
        return False
    _advance(state, files, 1)
    state["last_service_tick_at"] = now
    _save_state(state)
    _send_to_selected(state, files, webspace_id=webspace_id)
    return True


def _poll_once(webspace_id: str | None = None) -> None:
    try:
        state = _load_state()
        if not _unique_texts(state.get("selected_codes")):
            return
        devices = _load_devices()
        files = _files_for_state(state, _MAX_CONTROL_SCAN)
        state = _apply_root_events(state, devices, files, webspace_id=webspace_id, broadcast=True)
        _apply_service_tick(state, files, webspace_id=webspace_id)
        _publish(_SESSION_RECEIVER, _session_payload(state, _files_for_state(state, _MAX_CONTROL_SCAN)), webspace_id)
        _publish(_ENDPOINTS_RECEIVER, _endpoint_payload(devices, state), webspace_id)
    except Exception:
        _log.debug("slideshow root event poll failed", exc_info=True)


@subscribe("sys.ready")
def on_sys_ready(evt: Any) -> None:
    state = _load_state()
    if state.get("running") or _unique_texts(state.get("selected_codes")):
        _ensure_polling(default_webspace_id())


def _poll_loop() -> None:
    while not _poll_stop.wait(_POLL_INTERVAL_S):
        _poll_once(_poll_webspace_id or None)


def _ensure_polling(webspace_id: str | None = None) -> None:
    global _poll_thread, _poll_webspace_id
    if webspace_id:
        _poll_webspace_id = _text(webspace_id)
    with _poll_lock:
        if _poll_thread is not None and _poll_thread.is_alive():
            return
        _poll_stop.clear()
        _poll_thread = threading.Thread(target=_poll_loop, name="slideshow-root-poll", daemon=True)
        _poll_thread.start()


@tool
def activate_slideshow_runtime(webspace_id: str | None = None, **_payload: Any) -> dict[str, Any]:
    state = _load_state()
    selected = _unique_texts(state.get("selected_codes"))
    should_poll = bool(state.get("running") or selected)
    if should_poll:
        _ensure_polling(webspace_id or default_webspace_id())
        _poll_once(webspace_id or default_webspace_id())
    return {
        "ok": True,
        "polling": should_poll,
        "selected_codes": selected,
        "running": bool(state.get("running")),
    }


@tool
def list_slideshow_photos(
    limit: int = 24,
    source_dir: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    if source_dir:
        state["source_dir"] = str(_source_dir(source_dir))
        _save_state(state)
    _ensure_index(_source_dir(state.get("source_dir")))
    payload = _preview_payload(state, limit)
    _publish(_PREVIEW_RECEIVER, payload, webspace_id)
    _publish(_FOLDERS_RECEIVER, _folders_payload(state), webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, _files_for_state(state, _MAX_CONTROL_SCAN)), webspace_id)
    _publish(_INDEX_RECEIVER, _index_status(_source_dir(state.get("source_dir"))), webspace_id)
    return payload


@tool
def set_slideshow_source(
    source_dir: str,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    next_root = _source_dir(source_dir)
    previous_root = _text(state.get("source_dir"))
    state["source_dir"] = str(next_root)
    if previous_root.casefold() != str(next_root).casefold():
        state["selected_folder"] = ""
        state["current_index"] = 0
    state = _save_state(state)
    files = _files_for_state(state, _MAX_CONTROL_SCAN)
    devices = _load_devices()
    _publish(_ENDPOINTS_RECEIVER, _endpoint_payload(devices, state), webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, files), webspace_id)
    _publish(_FOLDERS_RECEIVER, _folders_payload(state), webspace_id)
    _publish(_PREVIEW_RECEIVER, _preview_payload(state, 48), webspace_id)
    status = _index_status(next_root)
    _publish(_INDEX_RECEIVER, status, webspace_id)
    return {"ok": True, "source_dir": str(next_root), "index": _ensure_index(next_root), "status": status, "updated_at": _now()}


@tool
def get_slideshow_index_status(
    source_dir: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    if source_dir:
        state["source_dir"] = str(_source_dir(source_dir))
        state = _save_state(state)
    status = _index_status(_source_dir(state.get("source_dir")))
    _publish(_INDEX_RECEIVER, status, webspace_id)
    return status


@tool
def get_slideshow_folders(
    source_dir: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    if source_dir:
        state["source_dir"] = str(_source_dir(source_dir))
        state = _save_state(state)
    payload = _folders_payload(state)
    _publish(_FOLDERS_RECEIVER, payload, webspace_id, force=True)
    _publish(_INDEX_RECEIVER, payload.get("status") if isinstance(payload.get("status"), Mapping) else _index_status(_source_dir(state.get("source_dir"))), webspace_id)
    return payload


@tool
def cancel_slideshow_photo_index(webspace_id: str | None = None) -> dict[str, Any]:
    status = _cancel_index_job(webspace_id)
    return status


def _folders_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    meta = _ensure_index(_source_dir(state.get("source_dir")))
    status = _index_status(_source_dir(state.get("source_dir")))
    return {
        "ok": bool(meta.get("ok", True)),
        "source_dir": str(_source_dir(state.get("source_dir"))),
        "selected_folder": _text(state.get("selected_folder")),
        "index": meta,
        "status": status,
        "items": _folder_items(state),
        "updated_at": _now(),
    }


def _preview_payload(state: Mapping[str, Any], limit: int = 48) -> dict[str, Any]:
    files = _files_for_state(state, limit)
    return {
        "ok": True,
        "source_dir": str(_source_dir(state.get("source_dir"))),
        "selected_folder": _text(state.get("selected_folder")),
        "count": len(files),
        "items": [_preview_item(p) for p in files],
        "index": _ensure_index(_source_dir(state.get("source_dir"))),
        "status": _index_status(_source_dir(state.get("source_dir"))),
        "updated_at": _now(),
    }


@tool
def refresh_slideshow_photo_index(
    source_dir: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    if source_dir:
        state["source_dir"] = str(_source_dir(source_dir))
        state = _save_state(state)
    status = _start_index_job(_source_dir(state.get("source_dir")), webspace_id=webspace_id)
    preview = _preview_payload(state, 48)
    preview["folders"] = _folder_items(state)
    preview["status"] = status
    _publish(_FOLDERS_RECEIVER, _folders_payload(state), webspace_id)
    _publish(_PREVIEW_RECEIVER, preview, webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, _files_for_state(state, _MAX_CONTROL_SCAN)), webspace_id)
    _publish(_INDEX_RECEIVER, status, webspace_id)
    return preview


@tool
def select_slideshow_folder(
    folder: str | None = None,
    source_dir: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    if source_dir:
        state["source_dir"] = str(_source_dir(source_dir))
    folder_token = _text(folder)
    state["selected_folder"] = "" if folder_token == "__more__" else folder_token
    state["current_index"] = 0
    state = _save_state(state)
    preview = _preview_payload(state, 48)
    _publish(_FOLDERS_RECEIVER, _folders_payload(state), webspace_id)
    _publish(_PREVIEW_RECEIVER, preview, webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, _files_for_state(state, _MAX_CONTROL_SCAN)), webspace_id)
    _publish(_INDEX_RECEIVER, _index_status(_source_dir(state.get("source_dir"))), webspace_id)
    return preview


@tool
def toggle_redevice_endpoint(
    code: str | None = None,
    webspace_id: str | None = None,
    source_dir: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    if source_dir:
        state["source_dir"] = str(_source_dir(source_dir))
    token = _text(code)
    selected = _unique_texts(state.get("selected_codes"))
    if token in selected:
        selected = [item for item in selected if item != token]
    elif token:
        selected.append(token)
    state["selected_codes"] = selected
    state = _save_state(state)
    devices = _load_devices()
    payload = _endpoint_payload(devices, state)
    _publish(_ENDPOINTS_RECEIVER, payload, webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, _files_for_state(state, _MAX_CONTROL_SCAN)), webspace_id)
    _ensure_polling(webspace_id)
    return payload


@tool
def select_redevice_endpoint(
    code: str | None = None,
    webspace_id: str | None = None,
    source_dir: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    if source_dir:
        state["source_dir"] = str(_source_dir(source_dir))
    devices = _load_devices()
    device = _select_device(devices, code)
    state["selected_codes"] = [_text(device.get("code"))] if device else []
    state = _save_state(state)
    payload = _endpoint_payload(devices, state)
    _publish(_ENDPOINTS_RECEIVER, payload, webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, _files_for_state(state, _MAX_CONTROL_SCAN)), webspace_id)
    _ensure_polling(webspace_id)
    return payload


@tool
def start_redevice_slideshow(
    code: str | None = None,
    limit: int = 10,
    webspace_id: str | None = None,
    source_dir: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    if source_dir:
        state["source_dir"] = str(_source_dir(source_dir))
    if code and code not in _unique_texts(state.get("selected_codes")):
        state["selected_codes"] = _unique_texts([*state.get("selected_codes", []), code])
    state["running"] = True
    state["last_service_tick_at"] = time.time()
    state = _save_state(state)
    files = _files_for_state(state, min(_MAX_CONTROL_SCAN, max(limit, _MAX_ENDPOINT_CURRENT + _MAX_ENDPOINT_FAVORITES)))
    _ensure_polling(webspace_id)
    return _send_to_selected(state, files, code=code, webspace_id=webspace_id)


@tool
def control_redevice_slideshow(
    action: str,
    code: str | None = None,
    webspace_id: str | None = None,
    source_dir: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    if source_dir:
        state["source_dir"] = str(_source_dir(source_dir))
    files = _files_for_state(state, _MAX_CONTROL_SCAN)
    token = _text(action).lower()
    if token in {"next", "forward"}:
        _advance(state, files, 1)
    elif token in {"prev", "previous", "back"}:
        _advance(state, files, -1)
    elif token in {"fav", "favorite", "favorite_toggle"}:
        _toggle_current_favorite(state, files)
    elif token in {"favorite_on", "fav_on"}:
        _set_current_favorite(state, files, True)
    elif token in {"favorite_off", "fav_off", "unfavorite"}:
        _set_current_favorite(state, files, False)
    elif token in {"hide", "hide_item", "down"}:
        _hide_current_photo(state, files)
        files = _files_for_state(state, _MAX_CONTROL_SCAN)
    elif token in {"send_telegram", "telegram", "tg"}:
        current = _current_photo(files, state)
        if current is None:
            return {"ok": False, "error": "no_current_photo"}
        result = telegram_photo(
            str(current),
            caption=f"Slideshow: {current.name}",
            _meta={"webspace_id": webspace_id or default_webspace_id(), "route_id": "telegram"},
        )
        return {"ok": bool(result.get("ok")), "telegram": dict(result), "source_name": current.name}
    elif token == "sync_on":
        state["sync"] = True
    elif token == "sync_off":
        state["sync"] = False
    elif token == "random":
        state["mode"] = "random"
    elif token == "sequential":
        state["mode"] = "sequential"
    elif token == "favorites":
        state["scope"] = "favorites"
        state["current_index"] = 0
    elif token == "all":
        state["scope"] = "all"
        state["current_index"] = 0
    elif token == "fullscreen_on":
        state["fullscreen"] = True
    elif token == "fullscreen_off":
        state["fullscreen"] = False
    elif token == "fit":
        state["display_mode"] = "fit"
    elif token == "crop":
        state["display_mode"] = "crop"
    elif token == "start":
        state["running"] = True
        state["last_service_tick_at"] = time.time()
    elif token == "stop":
        state["running"] = False
        state["last_service_tick_at"] = 0
    else:
        return {"ok": False, "error": "unknown_action", "action": action}
    state = _save_state(state)
    _ensure_polling(webspace_id)
    if token == "stop":
        return _stop_selected(state, code=code, webspace_id=webspace_id)
    return _send_to_selected(state, files, code=code, webspace_id=webspace_id)


@tool
def rename_redevice_endpoint(
    code: str,
    display_name: str | None = None,
    aliases: str | list[str] | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    alias_list = _unique_texts(aliases)
    result = ReDeviceBridge().update_profile(code, display_name=display_name, aliases=alias_list)
    devices = _load_devices()
    state = _load_state()
    payload = _endpoint_payload(devices, state)
    _publish(_ENDPOINTS_RECEIVER, payload, webspace_id)
    return {"ok": bool(result.get("ok")), "result": result}


@tool
def refresh_redevice_slideshow_state(
    code: str | None = None,
    webspace_id: str | None = None,
    source_dir: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    if source_dir:
        state["source_dir"] = str(_source_dir(source_dir))
    devices = _load_devices()
    if code and code not in _unique_texts(state.get("selected_codes")):
        state["selected_codes"] = _unique_texts([*state.get("selected_codes", []), code])
    files = _files_for_state(state, _MAX_CONTROL_SCAN)
    state = _apply_root_events(state, devices, files, webspace_id=webspace_id, broadcast=True)
    state = _save_state(state)
    endpoint_payload = _endpoint_payload(devices, state)
    session_payload = _session_payload(state, files)
    _publish(_ENDPOINTS_RECEIVER, endpoint_payload, webspace_id)
    _publish(_SESSION_RECEIVER, session_payload, webspace_id)
    _publish(_FOLDERS_RECEIVER, _folders_payload(state), webspace_id)
    _publish(_INDEX_RECEIVER, _index_status(_source_dir(state.get("source_dir"))), webspace_id)
    _ensure_polling(webspace_id)
    return {**endpoint_payload, "session": session_payload}


def _event_payload(evt: Any) -> Mapping[str, Any]:
    payload = getattr(evt, "payload", evt)
    return payload if isinstance(payload, Mapping) else {}


_STREAM_RECEIVERS = (
    _ENDPOINTS_RECEIVER,
    _PREVIEW_RECEIVER,
    _FOLDERS_RECEIVER,
    _SESSION_RECEIVER,
    _COMMAND_RECEIVER,
    _INDEX_RECEIVER,
)


def _event_webspace_id(payload: Mapping[str, Any]) -> str:
    return _text(payload.get("webspace_id") or payload.get("workspace_id")) or default_webspace_id()


def _event_receiver(payload: Mapping[str, Any]) -> str:
    return _text(payload.get("receiver"))


def _matches_receiver(payload: Mapping[str, Any]) -> bool:
    receiver = _event_receiver(payload)
    return receiver in {*_STREAM_RECEIVERS, "slideshow_skill.*"}


def _requested_receivers(payload: Mapping[str, Any]) -> list[str]:
    receiver = _event_receiver(payload)
    if receiver == "slideshow_skill.*":
        return list(_STREAM_RECEIVERS)
    return [receiver] if receiver in _STREAM_RECEIVERS else []


def _publish_receiver_snapshot(receiver: str, state: Mapping[str, Any], webspace_id: str) -> None:
    if receiver == _ENDPOINTS_RECEIVER:
        devices = _load_devices()
        _publish(receiver, _endpoint_payload(devices, state), webspace_id, force=True)
        return
    if receiver == _SESSION_RECEIVER:
        files = _files_for_state(state, _MAX_CONTROL_SCAN)
        _publish(receiver, _session_payload(state, files, last_command=_last_command_payload()), webspace_id, force=True)
        return
    if receiver == _FOLDERS_RECEIVER:
        _publish(receiver, _folders_payload(state), webspace_id, force=True)
        return
    if receiver == _PREVIEW_RECEIVER:
        _publish(receiver, _preview_payload(state, 48), webspace_id, force=True)
        return
    if receiver == _COMMAND_RECEIVER:
        _publish(receiver, _last_command_payload(), webspace_id, force=True)
        return
    if receiver == _INDEX_RECEIVER:
        _publish(receiver, _index_status(_source_dir(state.get("source_dir"))), webspace_id, force=True)


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = _event_payload(evt)
    receivers = _requested_receivers(payload)
    if not receivers:
        return
    webspace_id = _event_webspace_id(payload)
    state = _load_state()
    for receiver in receivers:
        _remember_receiver(webspace_id, receiver)
        if not _consume_snapshot_request(webspace_id, receiver):
            continue
        _publish_receiver_snapshot(receiver, state, webspace_id)
    if state.get("running") or _unique_texts(state.get("selected_codes")):
        _ensure_polling(webspace_id)


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = _event_payload(evt)
    receiver = _event_receiver(payload)
    if not _matches_receiver(payload):
        return
    webspace_id = _event_webspace_id(payload)
    action = _text(payload.get("action")).lower() or "subscribed"
    if receiver == "slideshow_skill.*":
        receivers = list(_STREAM_RECEIVERS)
    else:
        receivers = [receiver]
    for item in receivers:
        if action in {"unsubscribed", "removed", "release"}:
            _forget_receiver(webspace_id, item)
        else:
            _remember_receiver(webspace_id, item)
    state = _load_state()
    if action not in {"unsubscribed", "removed", "release"} and (
        state.get("running") or _unique_texts(state.get("selected_codes"))
    ):
        _ensure_polling(webspace_id)
