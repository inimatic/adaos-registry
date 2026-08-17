from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import device_access
from adaos.sdk.data import skill_memory
from adaos.sdk.data.skill_env import skill_env_path
from adaos.sdk.io import stream_publish, telegram_photo
from adaos.sdk.io.media import (
    browser_media_descriptor,
    cached_image_variant,
    direct_media_base_urls,
    publish_media_file as sdk_publish_media_file,
)
from adaos.sdk.redevice import (
    choose_endpoint as sdk_choose_endpoint,
    compact_endpoint,
    list_endpoints as sdk_list_endpoints,
    select_transport,
    with_local_content_route,
)

try:
    from adaos.services.yjs.webspace import default_webspace_id
except Exception:  # pragma: no cover
    def default_webspace_id() -> str:
        return "default"


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name) or "").strip() or default)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(str(os.environ.get(name) or "").strip() or default)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


_ENDPOINTS_RECEIVER = "slideshow_skill.endpoints"
_PREVIEW_RECEIVER = "slideshow_skill.preview"
_FOLDERS_RECEIVER = "slideshow_skill.folders"
_SESSION_RECEIVER = "slideshow_skill.session"
_COMMAND_RECEIVER = "slideshow_skill.command"
_INDEX_RECEIVER = "slideshow_skill.index"
_SUPPORTED = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
_ENDPOINT_SIZE = (480, 300)
_WIDGET_SIZE = (720, 405)
_FULLSCREEN_SIZE = (3840, 2160)
_WIDGET_IMAGE_BUDGET_BYTES = 60_000
_MAX_SCAN = 2000
_MAX_CONTROL_SCAN = 240
_MAX_ENDPOINT_CURRENT = 4
_MAX_ENDPOINT_FAVORITES = 20
_MAX_FOLDER_STREAM_ITEMS = 250
_MAX_DEVICE_CACHE_ITEMS = _env_int("SLIDESHOW_REDEVICE_DEVICE_CACHE_ITEMS", 1200, 1, 5000)
_REDEVICE_CACHE_BATCH_ITEMS = _env_int("SLIDESHOW_REDEVICE_CACHE_BATCH_ITEMS", 18, 1, 30)
_REDEVICE_CACHE_BATCH_DELAY_S = _env_float("SLIDESHOW_REDEVICE_CACHE_BATCH_DELAY_S", 0.25, 0.0, 5.0)
_INLINE_CONTENT_BUDGET_BYTES = 45_000
_INDEX_BATCH_SIZE = 500
_INDEX_PUBLISH_INTERVAL_S = 1.5
_INDEX_YIELD_EVERY_FILES = 200
_INDEX_YIELD_S = 0.001
_INDEX_STALE_AFTER_S = 15.0
_STATE_KEY = "slideshow_skill.state"
_INDEX_META_KEY = "slideshow_skill.photo_index"
_INDEX_STATUS_KEY = "slideshow_skill.index_status"
_COMMAND_STATE_KEY = "slideshow_skill.command_state"
_DEVICE_CACHE_STATE_KEY = "slideshow_skill.device_cache_state"
_LAST_MEDIA_KEY = "slideshow_skill.last_media"
_POLL_INTERVAL_S = 2.5
_SNAPSHOT_DEBOUNCE_S = 1.0
_SURFACE_REASSERT_INTERVAL_S = 20.0
_REDEVICE_COMMAND_TTL_S = _env_int("SLIDESHOW_REDEVICE_COMMAND_TTL_S", 120, 15, 600)
_REDEVICE_LIST_TIMEOUT_S = _env_float("SLIDESHOW_REDEVICE_LIST_TIMEOUT_S", 5.0, 1.0, 20.0)
_REDEVICE_COMMAND_HTTP_TIMEOUT_S = _env_float("SLIDESHOW_REDEVICE_COMMAND_HTTP_TIMEOUT_S", 4.0, 1.0, 12.0)
_REDEVICE_CACHE_MIN_FREE_FRACTION = 0.20
_VOLATILE_STREAM_KEYS = {"updated_at"}
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
_prewarm_lock = threading.Lock()
_prewarm_jobs: set[str] = set()
_device_cache_lock = threading.Lock()
_device_cache_jobs: dict[str, float] = {}
_media_lock = threading.Lock()
_index_schema_lock = threading.Lock()
_index_schema_ready_path = ""
_index_read_lock = threading.RLock()
_index_read_connection: sqlite3.Connection | None = None
_index_read_connection_path = ""
_folder_cache_lock = threading.Lock()
_folder_cache: dict[tuple[str, str, int, int], list[dict[str, Any]]] = {}


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _iso_age_s(value: Any) -> float | None:
    token = _text(value)
    if not token:
        return None
    try:
        updated_at = datetime.fromisoformat(token)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(tz=timezone.utc) - updated_at).total_seconds())
    except Exception:
        return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _voice_action(value: Any, text: Any = None) -> str:
    token = _text(value).lower()
    raw = _text(text).casefold()
    if token:
        return token
    checks = (
        ("send_telegram", ("телеграм", "telegram", "tg", "отправ")),
        ("favorite", ("фаворит", "избран", "favorite", "fav")),
        ("stop", ("стоп", "останов", "stop", "pause", "пауза")),
        ("prev", ("предыдущ", "назад", "previous", "prev", "back")),
        ("next", ("следующ", "дальше", "next", "forward")),
        ("start", ("слайдшоу", "показывай", "покажи", "start", "play", "show")),
    )
    for action, needles in checks:
        if any(needle in raw for needle in needles):
            return action
    return ""


def _device_query_from_text(text: Any, action: str) -> str:
    raw = _text(text)
    if not raw:
        return ""
    folded = raw.casefold()
    for needle in (
        "следующая",
        "следующий",
        "дальше",
        "предыдущая",
        "предыдущий",
        "назад",
        "стоп",
        "останови",
        "показывай слайдшоу",
        "покажи слайдшоу",
        "слайдшоу",
        "отправь в телеграм",
        "в телеграм",
        "фаворит",
        "в избранное",
        "next",
        "previous",
        "prev",
        "stop",
        "start",
        "play",
        "telegram",
        "favorite",
    ):
        folded = folded.replace(needle, " ")
    return " ".join(folded.split())


def _count_label(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except Exception:
        return "0"


def _count_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _interval_ms(value: Any, default: int = 7000) -> int:
    try:
        raw = str(value or "").strip().lower()
        if raw.startswith("interval_"):
            raw = raw.split("_", 1)[1]
        if raw.endswith("ms"):
            raw = raw[:-2]
        elif raw.endswith("s"):
            raw = str(float(raw[:-1]) * 1000)
        parsed = int(float(raw or default))
    except Exception:
        parsed = int(default)
    return max(1500, min(60000, parsed))


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


def _folder_snapshot_path() -> Path:
    return _internal_data_dir() / "folders.v1.json"


def _with_folder_selection(items: list[dict[str, Any]], selected: str) -> list[dict[str, Any]]:
    return [
        {**dict(item), "selected": _text(item.get("id")) == selected}
        for item in items
        if isinstance(item, Mapping)
    ]


def _load_folder_snapshot(root: Path, selected: str) -> list[dict[str, Any]] | None:
    path = _folder_snapshot_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, Mapping) or int(payload.get("schema") or 0) != 1:
        return None
    if _text(payload.get("source_dir")).casefold() != str(root).casefold():
        return None
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > _MAX_FOLDER_STREAM_ITEMS + 1:
        return None
    items = [dict(item) for item in raw_items if isinstance(item, Mapping)]
    return _with_folder_selection(items, selected) if items else None


def _store_folder_snapshot(root: Path, items: list[dict[str, Any]]) -> None:
    path = _folder_snapshot_path()
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    payload = {
        "schema": 1,
        "source_dir": str(root),
        "updated_at": _now(),
        "items": [{**dict(item), "selected": False} for item in items],
    }
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
    except Exception:
        _log.debug("failed to persist slideshow folder snapshot path=%s", path, exc_info=True)
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass


def _invalidate_folder_cache(*, persistent: bool = True) -> None:
    with _folder_cache_lock:
        _folder_cache.clear()
    if persistent:
        try:
            _folder_snapshot_path().unlink(missing_ok=True)
        except Exception:
            _log.debug("failed to invalidate slideshow folder snapshot", exc_info=True)


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
        "last_device_cache_requested_at": 0,
        "last_device_cache_source_dir": "",
        "last_device_cache_selected_folder": "",
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
    state["interval_ms"] = _interval_ms(state.get("interval_ms"))
    try:
        state["current_index"] = max(0, int(state.get("current_index") or 0))
    except Exception:
        state["current_index"] = 0
    try:
        state["last_service_tick_at"] = max(0.0, float(state.get("last_service_tick_at") or 0))
    except Exception:
        state["last_service_tick_at"] = 0
    try:
        state["last_device_cache_requested_at"] = max(0.0, float(state.get("last_device_cache_requested_at") or 0))
    except Exception:
        state["last_device_cache_requested_at"] = 0
    state["last_device_cache_source_dir"] = _text(state.get("last_device_cache_source_dir"))
    state["last_device_cache_selected_folder"] = _text(state.get("last_device_cache_selected_folder"))
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


def _stream_fingerprint_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _stream_fingerprint_value(item)
            for key, item in value.items()
            if str(key) not in _VOLATILE_STREAM_KEYS
        }
    if isinstance(value, list):
        return [_stream_fingerprint_value(item) for item in value]
    return value


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


def _ensure_index_schema() -> None:
    global _index_schema_ready_path
    path = _index_path()
    path_key = str(path.resolve())
    if _index_schema_ready_path == path_key:
        return
    with _index_schema_lock:
        if _index_schema_ready_path == path_key:
            return
        if path.exists():
            try:
                probe = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
                try:
                    tables = {
                        str(row[0])
                        for row in probe.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('photos', 'roots')"
                        ).fetchall()
                    }
                finally:
                    probe.close()
                if tables == {"photos", "roots"}:
                    _index_schema_ready_path = path_key
                    return
            except Exception:
                pass
        conn = sqlite3.connect(str(path), timeout=30)
        try:
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
            conn.commit()
        finally:
            conn.close()
        _index_schema_ready_path = path_key


def _connect_index(*, read_only: bool = False) -> sqlite3.Connection:
    global _index_read_connection, _index_read_connection_path
    _ensure_index_schema()
    path = _index_path()
    if read_only:
        path_key = str(path.resolve())
        if _index_read_connection is not None and _index_read_connection_path == path_key:
            return _index_read_connection
        if _index_read_connection is not None:
            try:
                _index_read_connection.close()
            except Exception:
                pass
        conn = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=2,
            check_same_thread=False,
        )
        _index_read_connection = conn
        _index_read_connection_path = path_key
    else:
        conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _close_index_read_connection() -> None:
    global _index_read_connection, _index_read_connection_path
    with _index_read_lock:
        conn = _index_read_connection
        _index_read_connection = None
        _index_read_connection_path = ""
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@contextmanager
def _index_connection(*, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    if read_only:
        with _index_read_lock:
            yield _connect_index(read_only=True)
        return
    conn = _connect_index(read_only=read_only)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


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
        with _index_connection(read_only=True) as conn:
            row = conn.execute("SELECT indexed_at, photo_count FROM roots WHERE root_dir = ?", (str(root),)).fetchone()
    except Exception:
        return {}
    if row is None:
        return {}
    return {"root_dir": str(root), "indexed_at": float(row["indexed_at"]), "photo_count": int(row["photo_count"])}


def _index_status(root: Path | None = None, *, verify_liveness: bool = True) -> dict[str, Any]:
    data = _memory_get(_INDEX_STATUS_KEY, {})
    status = dict(data) if isinstance(data, Mapping) else {}
    requested_source_dir = str(root) if root is not None else ""
    saved_source_dir = _text(status.get("source_dir"))
    if requested_source_dir and saved_source_dir and saved_source_dir.casefold() != requested_source_dir.casefold():
        status = {}
    source_dir = _text(status.get("source_dir")) or requested_source_dir
    meta = _index_meta(_source_dir(source_dir)) if source_dir else {}
    meta_photo_count = _count_int(meta.get("photo_count"))
    if not status:
        status = {
            "ok": bool(meta),
            "status": "ready" if meta else "idle",
            "source_dir": source_dir,
            "visited_files": 0,
            "indexed_count": meta_photo_count,
            "photo_count": meta_photo_count,
            "display_count": meta_photo_count,
            "folder_count": 0,
            "started_at": None,
            "updated_at": _now(),
            "completed_at": None,
        }
    if verify_liveness and _text(status.get("status")) == "running":
        worker_pid = _count_int(status.get("worker_pid"))
        age_s = _iso_age_s(status.get("updated_at"))
        if age_s is not None and age_s <= _INDEX_STALE_AFTER_S:
            alive = True
        elif worker_pid and worker_pid != os.getpid() and age_s is None:
            alive = True
        else:
            with _index_lock:
                alive = _index_thread is not None and _index_thread.is_alive()
        if not alive:
            status["status"] = "interrupted"
            status["message"] = "Indexing was interrupted. Press Refresh index to resume."
            _memory_set(_INDEX_STATUS_KEY, status)
    if meta and _text(status.get("status")) not in {"running", "canceling"}:
        status["photo_count"] = meta_photo_count or _count_int(status.get("photo_count"))
        status["indexed_count"] = meta_photo_count or _count_int(status.get("indexed_count"))
    display_count = max(
        _count_int(status.get("display_count")),
        _count_int(status.get("photo_count")),
        _count_int(status.get("indexed_count")),
    )
    status["display_count"] = display_count
    status["value"] = _count_label(display_count)
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


def _active_index_status(root: Path | None = None) -> dict[str, Any]:
    status = _mapping(_memory_get(_INDEX_STATUS_KEY, {}))
    if _text(status.get("status")) not in {"running", "canceling"}:
        return {}
    requested_source_dir = str(root) if root is not None else ""
    saved_source_dir = _text(status.get("source_dir"))
    if requested_source_dir and saved_source_dir and saved_source_dir.casefold() != requested_source_dir.casefold():
        return {}
    age_s = _iso_age_s(status.get("updated_at"))
    if age_s is not None and age_s > _INDEX_STALE_AFTER_S:
        return {}
    return status


def _index_busy_for_state(state: Mapping[str, Any]) -> bool:
    return bool(_active_index_status(_source_dir(state.get("source_dir"))))


def _index_message(status: Mapping[str, Any]) -> str:
    state = _text(status.get("status")) or "idle"
    source = _text(status.get("source_dir"))
    indexed = _count_label(status.get("display_count") or status.get("indexed_count") or status.get("photo_count"))
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


def _set_index_status(
    payload: Mapping[str, Any],
    webspace_id: str | None = None,
    *,
    verify_liveness: bool = True,
) -> dict[str, Any]:
    status = dict(payload)
    if _text(status.get("status")) == "running":
        status["worker_pid"] = os.getpid()
    status["updated_at"] = _now()
    status["message"] = _index_message(status)
    _memory_set(_INDEX_STATUS_KEY, status)
    if verify_liveness:
        normalized = _index_status(_source_dir(status.get("source_dir")))
    else:
        normalized = _index_status(_source_dir(status.get("source_dir")), verify_liveness=False)
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
    previous_photo_count = _count_int(_index_meta(root).get("photo_count"))
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
            "photo_count": previous_photo_count,
            "display_count": previous_photo_count,
            "folder_count": 0,
            "started_at": started_at,
            "completed_at": None,
        },
        webspace_id,
    )

    try:
        with _index_connection() as conn:
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
                            "photo_count": max(indexed_count, previous_photo_count),
                            "display_count": max(indexed_count, previous_photo_count),
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
                    if visited_files % _INDEX_YIELD_EVERY_FILES == 0:
                        time.sleep(_INDEX_YIELD_S)
                    now = time.time()
                    if now - last_publish >= _INDEX_PUBLISH_INTERVAL_S:
                        last_publish = now
                        display_count = max(indexed_count, previous_photo_count)
                        _set_index_status(
                            {
                                "ok": True,
                                "job_id": job_id,
                                "status": "running",
                                "source_dir": str(root),
                                "visited_files": visited_files,
                                "indexed_count": indexed_count,
                                "photo_count": display_count,
                                "display_count": display_count,
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
                "photo_count": max(indexed_count, previous_photo_count),
                "display_count": max(indexed_count, previous_photo_count),
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
    _invalidate_folder_cache()
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
        _publish(
            _SESSION_RECEIVER,
            _session_payload(
                state,
                _files_for_state(state, _MAX_CONTROL_SCAN),
                webspace_id=webspace_id,
                schedule_prewarm=True,
                defer_media=_index_busy_for_state(state),
            ),
            webspace_id,
        )
        _schedule_pending_device_cache_after_index(root, scan_started, webspace_id)
    return status


def _start_index_job(root: Path, *, webspace_id: str | None = None) -> dict[str, Any]:
    global _index_thread
    root = root.expanduser()
    job_id = "idx:" + hashlib.sha256(f"{root}:{time.time()}".encode("utf-8")).hexdigest()[:16]

    def _busy_status() -> dict[str, Any]:
        current = _index_status()
        if _text(current.get("source_dir")).casefold() == str(root).casefold():
            return current
        return {**current, "ok": False, "error": "indexer_busy", "requested_source_dir": str(root)}

    with _index_lock:
        running = _index_thread is not None and _index_thread.is_alive()
    if running:
        return _busy_status()
    if not root.exists():
        return _scan_index(root, job_id=job_id, webspace_id=webspace_id)

    started_at = _now()
    previous_photo_count = _count_int(_index_meta(root).get("photo_count"))
    thread = threading.Thread(
        target=_scan_index,
        kwargs={"root": root, "job_id": job_id, "webspace_id": webspace_id},
        name="slideshow-photo-index",
        daemon=True,
    )
    with _index_lock:
        if _index_thread is not None and _index_thread.is_alive():
            running = True
        else:
            _index_stop.clear()
            _index_thread = thread
            running = False
    if running:
        return _busy_status()
    try:
        thread.start()
    except Exception as exc:
        with _index_lock:
            if _index_thread is thread:
                _index_thread = None
        return _set_index_status(
            {
                "ok": False,
                "job_id": job_id,
                "status": "failed",
                "source_dir": str(root),
                "error": str(exc),
                "visited_files": 0,
                "indexed_count": 0,
                "photo_count": previous_photo_count,
                "display_count": previous_photo_count,
                "folder_count": 0,
                "started_at": started_at,
                "completed_at": _now(),
            },
            webspace_id,
        )
    if not thread.is_alive():
        return _index_status(root)
    return _set_index_status(
        {
            "ok": True,
            "job_id": job_id,
            "status": "running",
            "source_dir": str(root),
            "visited_files": 0,
            "indexed_count": 0,
            "photo_count": previous_photo_count,
            "display_count": previous_photo_count,
            "folder_count": 0,
            "started_at": started_at,
            "completed_at": None,
        },
        webspace_id,
    )


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
    _close_index_read_connection()
    with _stream_lock:
        active_receiver_total = sum(len(items) for items in _active_receivers_by_webspace.values())
        _active_receivers_by_webspace.clear()
        _last_stream_fingerprints.clear()
        _snapshot_seen_at.clear()
    with _device_cache_lock:
        _device_cache_jobs.clear()
    _invalidate_folder_cache(persistent=False)
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
        with _index_connection(read_only=True) as conn:
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
        with _index_connection(read_only=True) as conn:
            rows = conn.execute(
                "SELECT content_ref FROM photos WHERE root_dir = ? AND favorite = 1 ORDER BY mtime DESC",
                (str(root),),
            ).fetchall()
        return [str(row["content_ref"]) for row in rows]
    except Exception:
        return []


def _favorite_count(root: Path) -> int:
    try:
        with _index_connection(read_only=True) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM photos WHERE root_dir = ? AND favorite = 1",
                (str(root),),
            ).fetchone()
        return int(row["count"] or 0) if row is not None else 0
    except Exception:
        return 0


def _set_favorite(root: Path, content_ref: str, favorite: bool) -> None:
    try:
        with _index_connection() as conn:
            conn.execute(
                "UPDATE photos SET favorite = ? WHERE root_dir = ? AND content_ref = ?",
                (1 if favorite else 0, str(root), content_ref),
            )
        _invalidate_folder_cache()
    except Exception:
        _log.debug("failed to update slideshow favorite ref=%s", content_ref, exc_info=True)


def _set_hidden(root: Path, content_ref: str, hidden: bool) -> None:
    try:
        with _index_connection() as conn:
            conn.execute(
                "UPDATE photos SET hidden = ? WHERE root_dir = ? AND content_ref = ?",
                (1 if hidden else 0, str(root), content_ref),
            )
        _invalidate_folder_cache()
    except Exception:
        _log.debug("failed to update slideshow hidden ref=%s", content_ref, exc_info=True)


def _is_favorite(root: Path, content_ref: str) -> bool:
    try:
        with _index_connection(read_only=True) as conn:
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
    try:
        stat = _index_path().stat()
        cache_key = (str(root), selected, int(stat.st_mtime_ns), int(stat.st_size))
        with _folder_cache_lock:
            cached = _folder_cache.get(cache_key)
            if cached is not None:
                return [dict(item) for item in cached]
    except Exception:
        cache_key = None
    persisted = _load_folder_snapshot(root, selected)
    if persisted is not None:
        if cache_key is not None:
            with _folder_cache_lock:
                _folder_cache.clear()
                _folder_cache[cache_key] = [dict(item) for item in persisted]
        return persisted
    items = [{"id": "", "title": "All photos", "subtitle": str(root), "selected": selected == "", "selectable": True}]
    total_folders = 0
    try:
        with _index_connection(read_only=True) as conn:
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
    if cache_key is not None:
        with _folder_cache_lock:
            _folder_cache.clear()
            _folder_cache[cache_key] = [dict(item) for item in items]
    _store_folder_snapshot(root, items)
    return items


def _thumbnail(path: Path, size: tuple[int, int], label: str) -> tuple[Path, bool]:
    if label.startswith("endpoint-cache"):
        quality = 62
    else:
        quality = 62 if label.startswith("endpoint") else _thumbnail_quality(label, default=78)
    return cached_image_variant(
        path,
        max_size=size,
        label=label,
        quality=quality,
        background="black",
        fallback_dir=_internal_data_dir() / "thumbs",
    )


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


def _fullscreen_image(path: Path, *, create: bool = True) -> tuple[Path, bool]:
    return cached_image_variant(
        path,
        max_size=_FULLSCREEN_SIZE,
        label=f"fullscreen-v1-{_FULLSCREEN_SIZE[0]}x{_FULLSCREEN_SIZE[1]}-q88",
        quality=88,
        background="black",
        fallback_dir=_internal_data_dir() / "thumbs",
        create=create,
    )


def _current_and_next_photos(files: list[Path], state: Mapping[str, Any]) -> tuple[Path | None, Path | None]:
    selected = _selected_photos(files, state)
    current = _current_photo(files, state)
    if current is None or len(selected) <= 1:
        return current, None
    try:
        current_idx = _current_index(state) % len(selected)
        next_photo = selected[(current_idx + 1) % len(selected)]
        return current, None if next_photo == current else next_photo
    except Exception:
        return current, None


def _fullscreen_media_descriptor(path: Path, *, create: bool = False) -> dict[str, Any]:
    content_ref = _content_ref(path)
    try:
        full_image, cached = _fullscreen_image(path, create=create)
        if not cached and not create:
            return {}
        media = _publish_media_file(full_image, content_ref, variant="fullscreen")
        if not media.get("ok"):
            return {}
        return browser_media_descriptor(media, content_ref=content_ref)
    except Exception:
        _log.debug("failed to prepare fullscreen slideshow image", exc_info=True)
        return {}


def _schedule_fullscreen_prewarm(
    state: Mapping[str, Any],
    files: list[Path],
    *,
    webspace_id: str | None = None,
) -> None:
    current, next_photo = _current_and_next_photos(files, state)
    paths = [item for item in (current, next_photo) if item is not None]
    if not paths:
        return
    ws = _text(webspace_id) or default_webspace_id()
    job_key = f"{ws}:{','.join(_content_ref(item) for item in paths)}"
    with _prewarm_lock:
        if job_key in _prewarm_jobs:
            return
        _prewarm_jobs.add(job_key)
    state_snapshot = dict(state)
    files_snapshot = list(files)
    thread = threading.Thread(
        target=_prewarm_fullscreen_worker,
        args=(job_key, state_snapshot, files_snapshot, ws),
        name="slideshow-fullscreen-prewarm",
        daemon=True,
    )
    thread.start()


def _prewarm_fullscreen_worker(
    job_key: str,
    state: Mapping[str, Any],
    files: list[Path],
    webspace_id: str,
) -> None:
    try:
        if _index_busy_for_state(state):
            return
        current, next_photo = _current_and_next_photos(files, state)
        expected_ref = _content_ref(current) if current is not None else ""
        if not _media_lock.acquire(blocking=False):
            return
        try:
            for path in (current, next_photo):
                if path is not None:
                    _fullscreen_media_descriptor(path, create=True)
        finally:
            _media_lock.release()
        latest_state = _load_state()
        latest_files = _files_for_state(latest_state, _MAX_CONTROL_SCAN)
        latest_current, _latest_next = _current_and_next_photos(latest_files, latest_state)
        if expected_ref and (latest_current is None or _content_ref(latest_current) != expected_ref):
            return
        payload = _session_payload(
            latest_state,
            latest_files,
            last_command=_last_command_payload(),
            webspace_id=webspace_id,
            schedule_prewarm=False,
            defer_media=_index_busy_for_state(latest_state),
        )
        _publish(_SESSION_RECEIVER, payload, webspace_id, force=True)
    except Exception:
        _log.debug("failed to prewarm slideshow fullscreen media", exc_info=True)
    finally:
        with _prewarm_lock:
            _prewarm_jobs.discard(job_key)


def _data_uri(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _api_token() -> str:
    token = _text(os.environ.get("ADAOS_TOKEN"))
    if token:
        return token
    return "dev-local-token"


def _publish_media_file(path: Path, content_ref: str, *, variant: str = "widget") -> dict[str, Any]:
    try:
        payload = sdk_publish_media_file(
            path,
            content_ref=content_ref,
            namespace="slideshow",
            variant=variant,
            mime="image/jpeg",
            api_token=_api_token(),
        )
        _memory_set(_LAST_MEDIA_KEY, payload)
        return payload
    except Exception as exc:
        _log.debug("failed to publish slideshow media file", exc_info=True)
        return {"ok": False, "error": str(exc), "content_ref": content_ref}


def _record_media_failure(path: Path, phase: str, exc: BaseException) -> None:
    try:
        ref = _content_ref(path)
    except Exception:
        ref = ""
    incident = {
        "schema": "adaos.slideshow_skill.media_failure.v1",
        "phase": _text(phase) or "media",
        "source_path": str(path),
        "source_name": path.name,
        "content_ref": ref,
        "error": str(exc),
        "updated_at": _now(),
    }
    try:
        log_dir = _internal_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "media_failures.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(incident, ensure_ascii=False, default=str) + "\n")
    except Exception:
        _log.debug("failed to write slideshow media failure", exc_info=True)
    _log.debug("slideshow media preparation failed path=%s phase=%s error=%s", path, phase, exc)


def _deferred_session_image(content_ref: str, *, reason: str) -> dict[str, Any]:
    return {
        "mime": "image/jpeg",
        "content_ref": content_ref,
        "deferred": True,
        "reason": reason,
    }


def _content_item(path: Path, *, include_inline: bool = True) -> dict[str, Any]:
    thumb, cached = _thumbnail(path, _ENDPOINT_SIZE, "endpoint-cache-v6")
    ref = _content_ref(path)
    content_hash = ref.rsplit(":", 1)[-1]
    media = _publish_media_file(thumb, ref, variant="endpoint")
    # Endpoint commands may be consumed by old native agents that cannot resolve
    # browser-relative hub paths. Only send concrete endpoint-reachable direct
    # URLs here; inline data_uri remains the deterministic fallback.
    candidates = [str(item or "").strip() for item in list(media.get("direct_urls") or []) if str(item or "").strip()]
    content_url = candidates[0] if candidates else ""
    item = {
        "content_ref": ref,
        "content_hash": content_hash,
        "cache_key": f"slideshow:v1:{content_hash}",
        "source_path": str(path),
        "source_name": path.name,
        "title": path.stem,
        "mime": "image/jpeg",
        "cached": cached,
        "thumbnail_path": str(thumb),
        "thumbnail_bytes": thumb.stat().st_size,
        "content_size_bytes": thumb.stat().st_size,
        "content_url": content_url,
        "content_url_candidates": candidates,
        "browser_content_path": _text(media.get("browser_path")),
        "delivery": _mapping(media.get("delivery")),
        "media": media,
    }
    if include_inline:
        item["data_uri"] = _data_uri(thumb)
    return item


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


def _content_items_for_window(
    window: list[Path],
    *,
    include_inline: bool = True,
    inline_budget_bytes: int = _INLINE_CONTENT_BUDGET_BYTES,
) -> tuple[list[dict[str, Any]], int, bool]:
    items: list[dict[str, Any]] = []
    content_bytes = 0
    budget_limited = False
    for path in window:
        try:
            item = _content_item(path, include_inline=include_inline)
        except Exception as exc:
            _record_media_failure(path, "endpoint_content", exc)
            continue
        item_bytes = int(item.get("thumbnail_bytes") or 0)
        if include_inline and items and content_bytes + item_bytes > inline_budget_bytes:
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
        return "Widget only"
    try:
        devices = _load_devices()
        items = [compact_endpoint(item, selected_codes=set(selected_codes)) for item in devices]
        by_code = {_text(item.get("code")): _text(item.get("title")) for item in items}
        labels = [by_code.get(code) or code for code in selected_codes]
        return ", ".join(label for label in labels if label) or ", ".join(selected_codes)
    except Exception:
        return ", ".join(selected_codes)


def _session_payload(
    state: Mapping[str, Any],
    files: list[Path],
    *,
    last_command: Mapping[str, Any] | None = None,
    webspace_id: str | None = None,
    schedule_prewarm: bool = False,
    defer_media: bool | None = None,
    defer_reason: str = "index_running",
    resolve_endpoint_label: bool = True,
) -> dict[str, Any]:
    selected = _selected_photos(files, state)
    current, next_photo = _current_and_next_photos(files, state)
    image: dict[str, Any] = {"src": "", "mime": "image/jpeg"}
    title = "No photo"
    content_ref = ""
    media: dict[str, Any] = {}
    needs_prewarm = False
    root = _source_dir(_text(state.get("source_dir")))
    media_deferred = bool(defer_media)
    if current is not None:
        title = current.name
        try:
            content_ref = _content_ref(current)
        except Exception as exc:
            _record_media_failure(current, "session_ref", exc)
            media = {"ok": False, "error": str(exc), "content_ref": ""}
            image = {
                "mime": "image/jpeg",
                "error": "media_unavailable",
            }
            content_ref = ""
        if not content_ref:
            pass
        elif media_deferred:
            image = _deferred_session_image(content_ref, reason=defer_reason)
        elif not _media_lock.acquire(blocking=False):
            image = _deferred_session_image(content_ref, reason="media_busy")
        else:
            try:
                thumb, _cached = _widget_thumbnail(current)
                media = _publish_media_file(thumb, content_ref, variant="widget")
                fullscreen_media = _fullscreen_media_descriptor(current, create=False)
                next_media = _fullscreen_media_descriptor(next_photo, create=False) if next_photo is not None else {}
                needs_prewarm = not bool(fullscreen_media) or bool(next_photo is not None and not next_media)
                image = {
                    "mime": "image/jpeg",
                    "route": media.get("browser_route") or media.get("route") or "hub_browser_media",
                    "media": browser_media_descriptor(media, content_ref=content_ref),
                    "fullscreen_media": fullscreen_media,
                    "next_media": next_media,
                    "node_src": _text(media.get("node_url") or media.get("url")) if media.get("ok") else "",
                    "content_ref": content_ref,
                }
            except Exception as exc:
                _record_media_failure(current, "session_widget", exc)
                media = {"ok": False, "error": str(exc), "content_ref": content_ref}
                image = {
                    "mime": "image/jpeg",
                    "content_ref": content_ref,
                    "error": "media_unavailable",
                }
            finally:
                _media_lock.release()
    selected_codes = _unique_texts(state.get("selected_codes"))
    if resolve_endpoint_label:
        header = _selected_endpoint_label(selected_codes)
    else:
        header = _text(state.get("selected_label")) or ", ".join(selected_codes) or "Widget only"
    favorite_count = _favorite_count(root)
    favorite = bool(content_ref and _is_favorite(root, content_ref))
    filtered_count = len(selected)
    payload = {
        "ok": bool(current),
        "title": title,
        "subtitle": f"{header} | {state.get('mode')} | {state.get('scope')}",
        "value": title,
        "label": header,
        "description": f"{filtered_count} photos, {favorite_count} favorites",
        "image": image,
        "media": media,
        "frame": {"label": f"{(int(state.get('current_index') or 0) + 1) if files else 0}/{filtered_count}"},
        "status": {"label": "sync" if state.get("sync") else "independent", "color": "success" if state.get("sync") else "warning"},
        "content_ref": content_ref,
        "favorite": favorite,
        "favorite_icon": "star-sharp" if favorite else "star-outline",
        "favorite_label": "Remove favorite" if favorite else "Add favorite",
        "selected_codes": selected_codes,
        "sync": bool(state.get("sync")),
        "sync_value": "sync_on" if state.get("sync") else "sync_off",
        "interval_ms": _interval_ms(state.get("interval_ms")),
        "interval_value": f"interval_{_interval_ms(state.get('interval_ms'))}",
        "mode": state.get("mode"),
        "scope": state.get("scope"),
        "display_mode": state.get("display_mode"),
        "fullscreen": bool(state.get("fullscreen")),
        "fullscreen_value": "fullscreen_on" if state.get("fullscreen") else "fullscreen_off",
        "running": bool(state.get("running")),
        "media_deferred": media_deferred or bool(image.get("deferred")),
        "run_value": "start" if state.get("running") else "stop",
        "selected_folder": _text(state.get("selected_folder")),
        "source_dir": str(root),
        "buttons": [
            {"id": "prev", "label": "Prev"},
            {"id": "next", "label": "Next"},
            {"id": "fav", "label": "Fav"},
        ],
        "last_command": _compact_command_payload(last_command),
        "updated_at": _now(),
    }
    if schedule_prewarm and needs_prewarm:
        _schedule_fullscreen_prewarm(state, files, webspace_id=webspace_id)
    return payload


def _publish(receiver: str, payload: Mapping[str, Any], webspace_id: str | None = None, *, force: bool = False) -> None:
    token = _text(receiver)
    if not token:
        return
    ws = _text(webspace_id) or default_webspace_id()
    fingerprint = _json_fingerprint(_stream_fingerprint_value(payload))
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
    return sdk_list_endpoints(sync_registry=True, timeout=_REDEVICE_LIST_TIMEOUT_S)


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


def _slideshow_endpoint_item(endpoint: Mapping[str, Any], selected_codes: set[str]) -> dict[str, Any]:
    compact = compact_endpoint(endpoint, selected_codes=selected_codes)
    return {
        "id": _text(compact.get("id")),
        "code": _text(compact.get("code")),
        "endpoint_id": _text(compact.get("endpoint_id")),
        "title": _text(compact.get("title")),
        "display_name": _text(compact.get("display_name")),
        "state": _text(compact.get("state")),
        "selected": bool(compact.get("selected")),
        "selected_label": _text(compact.get("selected_label")),
        "online_state": _text(compact.get("online_state")),
        "online": bool(compact.get("online")),
        "last_seen_age_s": compact.get("last_seen_age_s"),
        "last_seen": _text(compact.get("last_seen")),
        "zone_id": _text(compact.get("zone_id")),
        "trust_level": _text(compact.get("trust_level")),
        "selectable": bool(compact.get("selectable")),
        "cache_supported": _endpoint_supports_slideshow_cache(endpoint),
        "aliases": _unique_texts(compact.get("aliases")),
    }


def _endpoint_online_state(endpoint: Mapping[str, Any] | None) -> str:
    if not endpoint:
        return "unknown"
    return _text(compact_endpoint(endpoint).get("online_state")) or "unknown"


def _endpoint_accepts_commands(endpoint: Mapping[str, Any] | None) -> bool:
    return _endpoint_online_state(endpoint) == "online"


def _capability_available(capabilities: Mapping[str, Any], name: str) -> bool:
    value = capabilities.get(name)
    if isinstance(value, bool):
        return value
    item = _mapping(value)
    if not item:
        return False
    if "available" in item:
        return bool(item.get("available"))
    return True


def _endpoint_supports_slideshow_cache(endpoint: Mapping[str, Any] | None) -> bool:
    data = _mapping(endpoint)
    manifest = _mapping(data.get("endpoint_manifest"))
    diagnostic = _mapping(data.get("diagnostic_report"))
    for container in (
        _mapping(manifest.get("capabilities")),
        _mapping(diagnostic.get("capabilities")),
        _mapping(data.get("capabilities")),
    ):
        if _capability_available(container, "display.slideshow.cache"):
            return True
    return False


def _offline_result(pair_code: str, endpoint: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "code": pair_code,
        "ok": False,
        "error": "device_offline",
        "command_id": "",
        "state": "skipped",
        "item_count": 0,
        "content_bytes": 0,
        "cache_budget_limited": False,
        "online_state": _endpoint_online_state(endpoint),
        "transport": {},
    }


def _endpoint_device_ref(endpoint: Mapping[str, Any] | None, pair_code: str | None = None) -> str:
    data = _mapping(endpoint)
    endpoint_id = _text(data.get("endpoint_id") or data.get("id"))
    if endpoint_id:
        return f"redevice:{endpoint_id}"
    code = _text(pair_code or data.get("code") or data.get("pair_code"))
    return f"redevice:{code}" if code else ""


def _endpoint_media_session(
    endpoint: Mapping[str, Any] | None,
    pair_code: str,
    *,
    item_count: int,
    direct_candidate_count: int,
) -> dict[str, Any]:
    try:
        from adaos.services import endpoint_router

        return endpoint_router.build_media_session(
            endpoint=endpoint or {},
            code=pair_code,
            owner=_owner(),
            intent="display.slideshow",
            item_count=item_count,
            primary_transport="endpoint_media_pull",
            fallback_transport="root_relay_inline",
            inline_fallback=direct_candidate_count <= 0,
        )
    except Exception:
        return {
            "schema_version": "endpoint-media-session.v1",
            "intent": "display.slideshow",
            "primary_transport": "endpoint_media_pull",
            "fallback_transport": "root_relay_inline",
            "inline_fallback": direct_candidate_count <= 0,
            "item_count": int(item_count or 0),
        }


def _send_endpoint_command(
    pair_code: str,
    command: Mapping[str, Any],
    *,
    endpoint: Mapping[str, Any] | None = None,
    constraints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return device_access.send_endpoint_command(
        device_ref=_endpoint_device_ref(endpoint, pair_code) or None,
        code=pair_code,
        command=command,
        requested_by=_owner(),
        constraints=constraints,
        timeout=_REDEVICE_COMMAND_HTTP_TIMEOUT_S,
    )


def _endpoint_payload(devices: list[dict[str, Any]], state: Mapping[str, Any]) -> dict[str, Any]:
    selected_codes = set(_healed_pair_codes(devices, _unique_texts(state.get("selected_codes"))))
    items = [_slideshow_endpoint_item(item, selected_codes) for item in devices]
    selected_items = [
        {
            "id": f"selected:{item['code']}",
            "title": item["title"],
            "subtitle": f"{item['online_state']} | seen {item['last_seen']} | code {item['code']}",
            "content": {
                "code": item["code"],
                "title": item["title"],
                "online_state": item["online_state"],
                "last_seen": item["last_seen"],
            },
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


def _healed_pair_codes(devices: list[Mapping[str, Any]], codes: list[str]) -> list[str]:
    healed: list[str] = []
    for code in _unique_texts(codes):
        endpoint = sdk_choose_endpoint(list(devices), code)
        resolved = _text(_mapping(endpoint).get("code") or _mapping(endpoint).get("pair_code")) if endpoint else code
        if resolved and resolved not in healed:
            healed.append(resolved)
    return healed


def _command_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    results = list(payload.get("results") or [])
    if _text(payload.get("mode")) == "device_cache":
        status = _text(payload.get("status")) or ("queued" if payload.get("ok") else "failed")
        title = f"Device cache {status}"
        subtitle = (
            f"{len(payload.get('sent_codes') or [])} endpoints | "
            f"{payload.get('photo_count', 0)} photos | {payload.get('batch_count', 0)} batches"
        )
    elif _text(payload.get("playback_mode")) == "widget":
        title = "Slideshow active in widget" if payload.get("ok") else "Slideshow widget failed"
        subtitle = f"widget only | {payload.get('item_count', 0)} photos"
    elif payload.get("degraded") and _text(payload.get("error")) == "device_offline":
        title = "Slideshow endpoints offline"
        subtitle = f"{len(results)} endpoints | command skipped"
    else:
        title = "Slideshow command queued" if payload.get("ok") else "Slideshow command failed"
        subtitle = f"{len(results)} endpoints | {payload.get('item_count', 0)} cached photos"
    return [
        {
            "id": _text(payload.get("command_id")) or "last-command",
            "title": title,
            "subtitle": subtitle,
            "content": _compact_command_payload(payload, include_items=False),
        }
    ]


def _compact_transport(transport: Mapping[str, Any] | None) -> dict[str, Any]:
    data = _mapping(transport)
    content = _mapping(data.get("content"))
    return {
        "selected_transport": _text(data.get("selected_transport")),
        "degraded": bool(data.get("degraded")),
        "requires_root_relay": bool(data.get("requires_root_relay")),
        "legacy_safe": bool(data.get("legacy_safe")),
        "content_transport": _text(content.get("transport")),
        "content_state": _text(content.get("state")),
    }


def _compact_result(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "code": _text(item.get("code")),
        "ok": bool(item.get("ok")),
        "error": item.get("error"),
        "command_id": _text(item.get("command_id")),
        "state": _text(item.get("state")),
        "online_state": _text(item.get("online_state")),
        "batch_index": int(item.get("batch_index") or 0),
        "batch_total": int(item.get("batch_total") or 0),
        "item_count": int(item.get("item_count") or 0),
        "content_bytes": int(item.get("content_bytes") or 0),
        "cache_budget_limited": bool(item.get("cache_budget_limited")),
        "transport": _compact_transport(_mapping(item.get("transport"))),
    }


def _compact_command_payload(payload: Mapping[str, Any] | None, *, include_items: bool = True) -> dict[str, Any]:
    data = _mapping(payload)
    if not data:
        return {}
    compact = {
        "ok": bool(data.get("ok")),
        "error": data.get("error"),
        "degraded": bool(data.get("degraded")),
        "command_id": _text(data.get("command_id")),
        "source_dir": _text(data.get("source_dir")),
        "selected_folder": _text(data.get("selected_folder")),
        "selected_codes": _unique_texts(data.get("selected_codes")),
        "target_codes": _unique_texts(data.get("target_codes")),
        "sent_codes": _unique_texts(data.get("sent_codes")),
        "mode": _text(data.get("mode")),
        "status": _text(data.get("status")),
        "job_key": _text(data.get("job_key")),
        "photo_count": int(data.get("photo_count") or 0),
        "batch_count": int(data.get("batch_count") or 0),
        "item_count": int(data.get("item_count") or 0),
        "cache_target": int(data.get("cache_target") or 0),
        "cache_budget_limited": bool(data.get("cache_budget_limited")),
        "content_bytes": int(data.get("content_bytes") or 0),
        "direct_candidate_count": int(data.get("direct_candidate_count") or 0),
        "direct_media_ready": bool(data.get("direct_media_ready")),
        "transport": _compact_transport(_mapping(data.get("transport"))),
        "results": [_compact_result(_mapping(item)) for item in list(data.get("results") or [])[:16]],
        "skipped": [_compact_result(_mapping(item)) for item in list(data.get("skipped") or [])[:16]],
        "updated_at": _text(data.get("updated_at")),
    }
    if "endpoint_required" in data:
        compact["endpoint_required"] = bool(data.get("endpoint_required"))
    playback_mode = _text(data.get("playback_mode"))
    if playback_mode:
        compact["playback_mode"] = playback_mode
    if include_items:
        compact["items"] = [
            {
                "source_name": _text(item.get("source_name")),
                "cached": bool(item.get("cached")),
            }
            for item in list(data.get("items") or [])[:8]
            if isinstance(item, Mapping)
        ]
    return compact


def _empty_command_payload() -> dict[str, Any]:
    return {
        "ok": False,
        "command_id": "",
        "command_items": [],
        "items": [],
        "updated_at": _now(),
    }


def _remember_command_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    compact = _compact_command_payload(payload)
    if "command_items" not in compact:
        compact["command_items"] = _command_items(compact)
    _memory_set(_COMMAND_STATE_KEY, compact)
    return compact


def _last_command_payload() -> dict[str, Any]:
    data = _memory_get(_COMMAND_STATE_KEY, {})
    if isinstance(data, Mapping):
        payload = _compact_command_payload(data)
        payload.setdefault("command_items", _command_items(payload))
        return payload
    return _empty_command_payload()


def _slideshow_cache_policy(*, receiver_cache_items: int, inline_budget_bytes: int = _INLINE_CONTENT_BUDGET_BYTES) -> dict[str, Any]:
    receiver_items = max(1, min(_MAX_DEVICE_CACHE_ITEMS, int(receiver_cache_items or 1)))
    return {
        "schema": "redevice.slideshow.cache_policy.v1",
        "max_current_items": receiver_items,
        "max_favorite_items": 0,
        "receiver_cache_items": receiver_items,
        "target_current_items": receiver_items,
        "inline_content_budget_bytes": int(inline_budget_bytes),
        "command_ttl_sec": _REDEVICE_COMMAND_TTL_S,
        "receiver_disk_cache": True,
        "receiver_cache_min_free_fraction": _REDEVICE_CACHE_MIN_FREE_FRACTION,
    }


def _build_command(
    pair_code: str,
    items: list[dict[str, Any]],
    state: Mapping[str, Any],
    *,
    autoplay: bool,
    transport: Mapping[str, Any] | None = None,
    media_session: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    command_id = "cmd:slideshow:" + hashlib.sha256(f"{pair_code}:{_now()}".encode("utf-8")).hexdigest()[:16]
    owner = _owner()
    transport_payload = dict(transport or {})
    expires_at = int(time.time()) + _REDEVICE_COMMAND_TTL_S
    return {
        "command_id": command_id,
        "type": "display.render_surface",
        "ttl_sec": _REDEVICE_COMMAND_TTL_S,
        "expires_at": expires_at,
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
            "interval_ms": _interval_ms(state.get("interval_ms")),
            "sync": bool(state.get("sync")),
            "mode": state.get("mode"),
            "scope": state.get("scope"),
            "source_fingerprint": hashlib.sha256(
                f"{_text(state.get('source_dir'))}\n{_text(state.get('selected_folder'))}".encode("utf-8", errors="replace")
            ).hexdigest()[:16],
            "selected_folder": _text(state.get("selected_folder")),
            "current_index": _current_index(state, code=pair_code),
            "transport": transport_payload,
            "media_session": dict(media_session or {}),
            "cache_policy": _slideshow_cache_policy(receiver_cache_items=_MAX_ENDPOINT_CURRENT),
            "cache": {
                "schema": "redevice.image_cache.v1",
                "namespace": "slideshow_skill",
                "mode": "pull_or_inline",
                "min_free_fraction": _REDEVICE_CACHE_MIN_FREE_FRACTION,
            },
            "items": items,
            "controls": {
                "hardware": {"volume_up": "next", "volume_down": "favorite_toggle"},
                "touch": {"tap": "favorite_toggle"},
            },
        },
    }


def _folder_cache_files_for_state(state: Mapping[str, Any], limit: int | None = None) -> list[Path]:
    query_state = dict(state)
    query_state["scope"] = "all"
    return [
        Path(row["source_path"])
        for row in _query_photo_records(query_state, limit=limit or _MAX_DEVICE_CACHE_ITEMS, favorites_only=False)
    ]


def _content_batches(items: list[Path], size: int) -> list[list[Path]]:
    batch_size = max(1, int(size or 1))
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]


def _device_cache_batch_size(*, direct_media_ready: bool) -> int:
    return _REDEVICE_CACHE_BATCH_ITEMS if direct_media_ready else 1


def _device_cache_batches(items: list[Path], *, direct_media_ready: bool) -> list[list[Path]]:
    return _content_batches(items, _device_cache_batch_size(direct_media_ready=direct_media_ready))


def _prune_device_cache_jobs(now: float | None = None) -> None:
    current = float(now or time.time())
    for key, started_at in list(_device_cache_jobs.items()):
        try:
            age_s = current - float(started_at or 0)
        except Exception:
            age_s = _REDEVICE_COMMAND_TTL_S + 1
        if age_s > 3600:
            _device_cache_jobs.pop(key, None)
    while len(_device_cache_jobs) > 16:
        oldest = min(_device_cache_jobs.items(), key=lambda item: item[1])[0]
        _device_cache_jobs.pop(oldest, None)


def _build_cache_command(
    pair_code: str,
    items: list[dict[str, Any]],
    state: Mapping[str, Any],
    *,
    playlist_id: str,
    batch_index: int,
    batch_total: int,
    activate_when_complete: bool,
    transport: Mapping[str, Any] | None = None,
    media_session: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    command = _build_command(
        pair_code,
        items,
        state,
        autoplay=bool(activate_when_complete),
        transport=transport,
        media_session=media_session,
    )
    command["type"] = "display.slideshow.cache"
    payload = _mapping(command.get("payload"))
    payload["surface_ref"] = "slideshow.viewer"
    payload["cache_only"] = True
    payload["playlist_id"] = playlist_id
    payload["batch_index"] = max(0, int(batch_index))
    payload["batch_total"] = max(1, int(batch_total))
    payload["activate_when_complete"] = bool(activate_when_complete)
    payload["autoplay"] = bool(activate_when_complete)
    payload["interval_ms"] = _interval_ms(state.get("interval_ms"))
    payload["cache_policy"] = _slideshow_cache_policy(
        receiver_cache_items=max(1, len(items)),
        inline_budget_bytes=_INLINE_CONTENT_BUDGET_BYTES,
    )
    command["payload"] = payload
    return command


def _device_cache_payload(
    *,
    status: str,
    state: Mapping[str, Any],
    job_key: str,
    target_codes: list[str],
    sent_codes: list[str],
    skipped: list[Mapping[str, Any]] | None = None,
    results: list[Mapping[str, Any]] | None = None,
    photo_count: int = 0,
    batch_count: int = 0,
    error: str | None = None,
) -> dict[str, Any]:
    payload = {
        "ok": status != "failed",
        "status": status,
        "mode": "device_cache",
        "degraded": bool(skipped),
        "error": error,
        "job_key": job_key,
        "source_dir": state.get("source_dir"),
        "selected_folder": _text(state.get("selected_folder")),
        "target_codes": target_codes,
        "sent_codes": sent_codes,
        "skipped": [dict(item) for item in list(skipped or [])],
        "results": [dict(item) for item in list(results or [])],
        "photo_count": max(0, int(photo_count or 0)),
        "batch_count": max(0, int(batch_count or 0)),
        "cache_batch_items": _REDEVICE_CACHE_BATCH_ITEMS,
        "updated_at": _now(),
    }
    payload["command_items"] = _command_items(payload)
    return payload


def _schedule_device_cache_warm(
    state: Mapping[str, Any],
    files: list[Path],
    *,
    code: str | None = None,
    webspace_id: str | None = None,
    activate_when_complete: bool = True,
) -> dict[str, Any]:
    direct_media_ready = bool(direct_media_base_urls())
    batch_count = len(_device_cache_batches(files, direct_media_ready=direct_media_ready))
    devices = _load_devices()
    configured_codes = _unique_texts(state.get("selected_codes"))
    target_codes = _unique_texts([code]) if code else configured_codes
    if not target_codes:
        device = _select_device(devices)
        target_codes = [_text(device.get("code"))] if device else []
    target_codes = _healed_pair_codes(devices, target_codes)
    devices_by_code = {_text(item.get("code")): item for item in devices if _text(item.get("code"))}
    skipped: list[dict[str, Any]] = []
    sent_codes: list[str] = []
    for pair_code in target_codes:
        endpoint = devices_by_code.get(pair_code)
        if not _endpoint_accepts_commands(endpoint):
            skipped.append({**_offline_result(pair_code, endpoint), "reason": "device_offline"})
            continue
        if not _endpoint_supports_slideshow_cache(endpoint):
            skipped.append(
                {
                    "code": pair_code,
                    "ok": False,
                    "error": "cache_protocol_unsupported",
                    "state": "skipped",
                    "online_state": _endpoint_online_state(endpoint),
                }
            )
            continue
        sent_codes.append(pair_code)
    if not files:
        payload = _device_cache_payload(
            status="failed",
            state=state,
            job_key="",
            target_codes=target_codes,
            sent_codes=[],
            skipped=skipped,
            photo_count=0,
            batch_count=0,
            error="no_supported_photos",
        )
        _memory_set(_DEVICE_CACHE_STATE_KEY, payload)
        _publish(_COMMAND_RECEIVER, _remember_command_payload(payload), webspace_id, force=True)
        return payload
    if not sent_codes:
        payload = _device_cache_payload(
            status="unsupported" if skipped else "failed",
            state=state,
            job_key="",
            target_codes=target_codes,
            sent_codes=[],
            skipped=skipped,
            photo_count=len(files),
            batch_count=batch_count,
            error="cache_protocol_unsupported" if skipped else "no_redevice_endpoint",
        )
        _memory_set(_DEVICE_CACHE_STATE_KEY, payload)
        _publish(_COMMAND_RECEIVER, _remember_command_payload(payload), webspace_id, force=True)
        return payload
    source_fingerprint = hashlib.sha256(
        f"{state.get('source_dir')}\n{_text(state.get('selected_folder'))}\n{len(files)}\n{','.join(_content_ref(path) for path in files[:16])}".encode(
            "utf-8", errors="replace"
        )
    ).hexdigest()[:16]
    ws = _text(webspace_id) or default_webspace_id()
    job_key = f"{ws}:{','.join(sent_codes)}:{source_fingerprint}"
    with _device_cache_lock:
        _prune_device_cache_jobs()
        if job_key in _device_cache_jobs:
            payload = _device_cache_payload(
                status="already_running",
                state=state,
                job_key=job_key,
                target_codes=target_codes,
                sent_codes=sent_codes,
                skipped=skipped,
                photo_count=len(files),
                batch_count=batch_count,
            )
            _publish(_COMMAND_RECEIVER, _remember_command_payload(payload), webspace_id, force=True)
            return payload
        _device_cache_jobs[job_key] = time.time()
    payload = _device_cache_payload(
        status="queued",
        state=state,
        job_key=job_key,
        target_codes=target_codes,
        sent_codes=sent_codes,
        skipped=skipped,
        photo_count=len(files),
        batch_count=batch_count,
    )
    _memory_set(_DEVICE_CACHE_STATE_KEY, payload)
    _publish(_COMMAND_RECEIVER, _remember_command_payload(payload), webspace_id, force=True)
    thread = threading.Thread(
        target=_device_cache_warm_worker,
        args=(job_key, dict(state), list(files), sent_codes, devices_by_code, ws, bool(activate_when_complete)),
        name="slideshow-device-cache-warm",
        daemon=True,
    )
    thread.start()
    return payload


def _device_cache_warm_worker(
    job_key: str,
    state: Mapping[str, Any],
    files: list[Path],
    target_codes: list[str],
    devices_by_code: Mapping[str, Mapping[str, Any]],
    webspace_id: str,
    activate_when_complete: bool,
) -> None:
    results: list[dict[str, Any]] = []
    try:
        direct_media_ready = bool(direct_media_base_urls())
        batches = _device_cache_batches(files, direct_media_ready=direct_media_ready)
        playlist_id = "slideshow:playlist:" + hashlib.sha256(
            f"{state.get('source_dir')}\n{_text(state.get('selected_folder'))}\n{len(files)}\n{job_key}".encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()[:16]
        for pair_code in target_codes:
            endpoint = devices_by_code.get(pair_code, {})
            for batch_index, batch in enumerate(batches):
                include_inline = not direct_media_ready
                items, content_bytes, budget_limited = _content_items_for_window(
                    batch,
                    include_inline=include_inline,
                    inline_budget_bytes=_INLINE_CONTENT_BUDGET_BYTES,
                )
                if not items:
                    results.append(
                        {
                            "code": pair_code,
                            "ok": False,
                            "error": "no_cache_items",
                            "state": "failed",
                            "batch_index": batch_index,
                            "item_count": 0,
                        }
                    )
                    continue
                direct_candidate_count = sum(len(list(item.get("content_url_candidates") or [])) for item in items)
                endpoint_for_transport = endpoint
                if direct_candidate_count or direct_media_ready:
                    endpoint_for_transport = with_local_content_route(
                        endpoint_for_transport,
                        reason="slideshow_cache_media_candidates",
                    )
                transport = select_transport(
                    endpoint_for_transport,
                    intent="display.slideshow",
                    content_bytes=content_bytes,
                    allow_root_relay=True,
                )
                media_session = _endpoint_media_session(
                    endpoint_for_transport,
                    pair_code,
                    item_count=len(items),
                    direct_candidate_count=direct_candidate_count,
                )
                command = _build_cache_command(
                    pair_code,
                    items,
                    state,
                    playlist_id=playlist_id,
                    batch_index=batch_index,
                    batch_total=len(batches),
                    activate_when_complete=activate_when_complete,
                    transport=transport,
                    media_session=media_session,
                )
                res = _send_endpoint_command(pair_code, command, endpoint=endpoint)
                queued = _mapping(res.get("command"))
                results.append(
                    {
                        "code": pair_code,
                        "ok": bool(res.get("ok")),
                        "error": res.get("error"),
                        "command_id": queued.get("command_id") or command.get("command_id"),
                        "state": queued.get("state"),
                        "batch_index": batch_index,
                        "batch_total": len(batches),
                        "item_count": len(items),
                        "content_bytes": content_bytes,
                        "cache_budget_limited": budget_limited,
                        "transport": transport,
                    }
                )
                if _REDEVICE_CACHE_BATCH_DELAY_S > 0:
                    time.sleep(_REDEVICE_CACHE_BATCH_DELAY_S)
        delivered = any(bool(item.get("ok")) for item in results)
        status = "completed" if delivered else "failed"
        payload = _device_cache_payload(
            status=status,
            state=state,
            job_key=job_key,
            target_codes=target_codes,
            sent_codes=target_codes,
            results=results,
            photo_count=len(files),
            batch_count=len(batches),
            error=None if delivered else "cache_commands_failed",
        )
        _memory_set(_DEVICE_CACHE_STATE_KEY, payload)
        _publish(_COMMAND_RECEIVER, _remember_command_payload(payload), webspace_id, force=True)
    except Exception as exc:
        payload = _device_cache_payload(
            status="failed",
            state=state,
            job_key=job_key,
            target_codes=target_codes,
            sent_codes=target_codes,
            results=results,
            photo_count=len(files),
            error=str(exc),
        )
        _memory_set(_DEVICE_CACHE_STATE_KEY, payload)
        _publish(_COMMAND_RECEIVER, _remember_command_payload(payload), webspace_id, force=True)
        _log.debug("failed to warm ReDevice slideshow cache", exc_info=True)
    finally:
        with _device_cache_lock:
            _device_cache_jobs.pop(job_key, None)


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
        return _publish_widget_only_state(
            state,
            files,
            webspace_id=webspace_id,
            reason="no_redevice_endpoint",
        )
    target_codes = _healed_pair_codes(devices, target_codes)
    if configured_codes and isinstance(state, dict):
        healed_configured = _healed_pair_codes(devices, configured_codes)
        if healed_configured != configured_codes:
            state["selected_codes"] = healed_configured
            configured_codes = healed_configured
            _save_state(state)
    devices_by_code = {_text(item.get("code")): item for item in devices if _text(item.get("code"))}
    requested_target_codes = list(target_codes)
    results: list[dict[str, Any]] = []
    target_codes = []
    for pair_code in requested_target_codes:
        endpoint = devices_by_code.get(pair_code)
        if not _endpoint_accepts_commands(endpoint):
            results.append(_offline_result(pair_code, endpoint))
            continue
        target_codes.append(pair_code)
    if target_codes and not _endpoint_window(files, state, code=target_codes[0]):
        return {"ok": False, "error": "no_supported_photos", "source_dir": state.get("source_dir")}
    if isinstance(state, dict):
        now = time.time()
        state["last_surface_sync_at"] = now
        state["last_surface_target_codes"] = target_codes
        skipped = [item["code"] for item in results if item.get("state") == "skipped"]
        if skipped:
            state["last_surface_skipped_codes"] = skipped
        _save_state(state)
    transports: dict[str, Any] = {}
    media_sessions: dict[str, Any] = {}
    items_by_code: dict[str, list[dict[str, Any]]] = {}
    first_items: list[dict[str, Any]] = []
    first_content_bytes = 0
    any_budget_limited = False
    first_command_id = ""
    first_direct_candidate_count = 0
    direct_media_ready = bool(direct_media_base_urls())
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
        direct_candidate_count = sum(len(list(item.get("content_url_candidates") or [])) for item in items)
        first_direct_candidate_count = first_direct_candidate_count or direct_candidate_count
        endpoint_for_transport = devices_by_code.get(pair_code, {})
        if direct_candidate_count or direct_media_ready:
            endpoint_for_transport = with_local_content_route(
                endpoint_for_transport,
                reason="slideshow_command_media_candidates",
            )
        transport = select_transport(
            endpoint_for_transport,
            intent="display.slideshow",
            content_bytes=content_bytes,
            allow_root_relay=True,
        )
        transports[pair_code] = transport
        media_session = _endpoint_media_session(
            endpoint_for_transport,
            pair_code,
            item_count=len(items),
            direct_candidate_count=direct_candidate_count,
        )
        media_sessions[pair_code] = media_session
        # The skill owns slideshow sequencing. Endpoint-side autoplay is kept off
        # so legacy devices do not loop over a small local cache window.
        command = _build_command(pair_code, items, state, autoplay=False, transport=transport, media_session=media_session)
        first_command_id = first_command_id or _text(command.get("command_id"))
        res = _send_endpoint_command(pair_code, command, endpoint=devices_by_code.get(pair_code))
        queued = _mapping(res.get("command"))
        results.append(
            {
                "code": pair_code,
                "ok": bool(res.get("ok")),
                "error": res.get("error"),
                "command_id": queued.get("command_id") or command.get("command_id"),
                "state": queued.get("state"),
                "online_state": _endpoint_online_state(devices_by_code.get(pair_code)),
                "item_count": len(items),
                "content_bytes": content_bytes,
                "cache_budget_limited": budget_limited,
                "transport": transport,
            }
        )
    delivered = any(bool(item.get("ok")) for item in results)
    offline_only = (
        bool(results)
        and not target_codes
        and all(_text(item.get("error")) == "device_offline" for item in results)
    )
    payload = {
        "ok": delivered or offline_only,
        "degraded": offline_only,
        "error": "device_offline" if offline_only else None,
        "command_id": first_command_id,
        "source_dir": state.get("source_dir"),
        "selected_codes": configured_codes or requested_target_codes,
        "target_codes": requested_target_codes,
        "sent_codes": target_codes,
        "item_count": len(first_items),
        "cache_target": _MAX_ENDPOINT_CURRENT,
        "cache_budget_limited": any_budget_limited,
        "content_bytes": first_content_bytes,
        "direct_candidate_count": first_direct_candidate_count,
        "direct_media_ready": direct_media_ready,
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
        "media_session": next(iter(media_sessions.values()), {}),
        "media_sessions": media_sessions,
        "results": results,
        "updated_at": _now(),
    }
    payload["command_items"] = _command_items(payload)
    payload = _remember_command_payload(payload)
    _publish(_COMMAND_RECEIVER, payload, webspace_id)
    _publish(
        _SESSION_RECEIVER,
        _session_payload(
            state,
            files,
            last_command=payload,
            webspace_id=webspace_id,
            schedule_prewarm=True,
            defer_media=_index_busy_for_state(state),
        ),
        webspace_id,
    )
    return payload


def _publish_widget_only_state(
    state: Mapping[str, Any],
    files: list[Path],
    *,
    webspace_id: str | None = None,
    reason: str = "no_redevice_endpoint",
) -> dict[str, Any]:
    selected = _selected_photos(files, state)
    payload = {
        "ok": bool(selected),
        "error": None if selected else "no_supported_photos",
        "command_id": "",
        "source_dir": state.get("source_dir"),
        "selected_codes": _unique_texts(state.get("selected_codes")),
        "target_codes": [],
        "sent_codes": [],
        "item_count": len(selected),
        "cache_target": 0,
        "cache_budget_limited": False,
        "content_bytes": 0,
        "direct_candidate_count": 0,
        "direct_media_ready": bool(direct_media_base_urls()),
        "items": [],
        "results": [
            {
                "code": "",
                "ok": True,
                "error": None,
                "command_id": "",
                "state": "widget_only",
                "online_state": "not_required",
                "item_count": len(selected),
                "reason": reason,
            }
        ]
        if selected
        else [],
        "endpoint_required": False,
        "playback_mode": "widget",
        "updated_at": _now(),
    }
    compact = _remember_command_payload(payload)
    _publish(_COMMAND_RECEIVER, compact, webspace_id)
    _publish(
        _SESSION_RECEIVER,
        _session_payload(
            state,
            files,
            last_command=compact,
            webspace_id=webspace_id,
            schedule_prewarm=True,
            defer_media=_index_busy_for_state(state),
        ),
        webspace_id,
    )
    return compact


def _sync_running_surface(
    state: dict[str, Any],
    files: list[Path] | None = None,
    *,
    code: str | None = None,
    webspace_id: str | None = None,
    reason: str = "state_changed",
) -> dict[str, Any]:
    if not state.get("running"):
        return {}
    target_codes = _unique_texts([code] if code else state.get("selected_codes"))
    if not target_codes:
        return {}
    selected_files = files if files is not None else _files_for_state(state, _MAX_CONTROL_SCAN)
    if not _selected_photos(selected_files, state):
        state["last_surface_sync_reason"] = _text(reason) or "state_changed"
        state["last_surface_sync_at"] = time.time()
        _save_state(state)
        return _stop_selected(state, code=code, webspace_id=webspace_id)
    state["last_surface_sync_reason"] = _text(reason) or "state_changed"
    return _send_to_selected(state, selected_files, code=code, webspace_id=webspace_id)


def _active_app_conflicts(
    devices: list[Mapping[str, Any]],
    selected_codes: list[str],
) -> list[dict[str, str]]:
    selected = set(_unique_texts(selected_codes))
    conflicts: list[dict[str, str]] = []
    if not selected:
        return conflicts
    for item in devices:
        code = _text(item.get("code"))
        if code not in selected:
            continue
        active_app = _mapping(item.get("active_app"))
        owner = _text(active_app.get("skill_id") or active_app.get("app_id"))
        if owner and owner != "slideshow_skill":
            conflicts.append(
                {
                    "code": code,
                    "skill_id": owner,
                    "label": _text(active_app.get("label")) or owner,
                }
            )
    return conflicts


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
    devices_by_code = {_text(item.get("code")): item for item in devices if _text(item.get("code"))}
    results: list[dict[str, Any]] = []
    first_command_id = ""
    sent_codes: list[str] = []
    for pair_code in selected_codes:
        endpoint = devices_by_code.get(pair_code)
        if not _endpoint_accepts_commands(endpoint):
            results.append(_offline_result(pair_code, endpoint))
            continue
        sent_codes.append(pair_code)
        command_id = "cmd:slideshow-stop:" + hashlib.sha256(f"{pair_code}:{_now()}".encode("utf-8")).hexdigest()[:16]
        first_command_id = first_command_id or command_id
        expires_at = int(time.time()) + _REDEVICE_COMMAND_TTL_S
        command = {
            "command_id": command_id,
            "type": "display.clear_surface",
            "ttl_sec": _REDEVICE_COMMAND_TTL_S,
            "expires_at": expires_at,
            "owner": _owner(),
            "payload": {
                "surface_ref": "slideshow.viewer",
                "surface_id": f"surface:slideshow:{command_id.split(':')[-1]}",
                "active_app": None,
                "fullscreen": False,
                "cache_policy": {"command_ttl_sec": _REDEVICE_COMMAND_TTL_S},
                "items": [],
            },
        }
        res = _send_endpoint_command(pair_code, command, endpoint=endpoint)
        queued = _mapping(res.get("command"))
        results.append(
            {
                "code": pair_code,
                "ok": bool(res.get("ok")),
                "error": res.get("error"),
                "command_id": queued.get("command_id") or command_id,
                "state": queued.get("state"),
                "online_state": _endpoint_online_state(endpoint),
            }
        )
    files = _files_for_state(state, _MAX_CONTROL_SCAN)
    delivered = any(bool(item.get("ok")) for item in results)
    offline_only = (
        bool(results)
        and not sent_codes
        and all(_text(item.get("error")) == "device_offline" for item in results)
    )
    payload = {
        "ok": (delivered or offline_only) if results else True,
        "degraded": offline_only,
        "error": "device_offline" if offline_only else None,
        "command_id": first_command_id,
        "source_dir": state.get("source_dir"),
        "selected_codes": selected_codes,
        "target_codes": selected_codes,
        "sent_codes": sent_codes,
        "item_count": 0,
        "items": [],
        "results": results,
        "updated_at": _now(),
    }
    payload["command_items"] = _command_items(payload)
    payload = _remember_command_payload(payload)
    _publish(_COMMAND_RECEIVER, payload, webspace_id)
    _publish(
        _SESSION_RECEIVER,
        _session_payload(
            state,
            files,
            last_command=payload,
            webspace_id=webspace_id,
            schedule_prewarm=True,
            defer_media=_index_busy_for_state(state),
        ),
        webspace_id,
    )
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
        if _index_busy_for_state(state):
            return state
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
    devices: list[Mapping[str, Any]] | None = None,
    webspace_id: str | None = None,
) -> bool:
    if not state.get("running"):
        return False
    selected_codes = _unique_texts(state.get("selected_codes"))
    conflicts = _active_app_conflicts(devices or [], selected_codes)
    if conflicts:
        state["running"] = False
        state["last_surface_sync_reason"] = "paused_active_app_conflict"
        state["last_active_app_conflict"] = conflicts[0]
        state["last_service_tick_at"] = time.time()
        _save_state(state)
        return False
    if not _selected_photos(files, state):
        return False
    now = time.time()
    interval_s = max(1.5, min(60.0, float(state.get("interval_ms") or 7000) / 1000.0))
    last_tick = float(state.get("last_service_tick_at") or 0)
    if _index_busy_for_state(state):
        state["last_service_tick_at"] = now
        state["last_surface_sync_reason"] = "index_running_deferred"
        if last_tick <= 0 or now - last_tick >= interval_s:
            _save_state(state)
        return False
    if last_tick <= 0:
        state["last_service_tick_at"] = now
        _save_state(state)
        _sync_running_surface(state, files, webspace_id=webspace_id, reason="runtime_started")
        return True
    if now - last_tick < interval_s:
        last_sync = float(state.get("last_surface_sync_at") or 0)
        if now - last_sync >= _SURFACE_REASSERT_INTERVAL_S:
            _sync_running_surface(state, files, webspace_id=webspace_id, reason="periodic_reassert")
            return True
        return False
    _advance(state, files, 1)
    state["last_service_tick_at"] = now
    _save_state(state)
    _sync_running_surface(state, files, webspace_id=webspace_id, reason="service_tick")
    return True


def _poll_once(webspace_id: str | None = None) -> None:
    try:
        state = _load_state()
        selected_codes = _unique_texts(state.get("selected_codes"))
        running = bool(state.get("running"))
        if not running and not selected_codes:
            return
        devices = _load_devices() if selected_codes else []
        files = _files_for_state(state, _MAX_CONTROL_SCAN) if running else []
        state = _apply_root_events(state, devices, files, webspace_id=webspace_id, broadcast=running)
        if running:
            ticked = _apply_service_tick(state, files, devices=devices, webspace_id=webspace_id)
            if ticked and not selected_codes:
                _publish(
                    _SESSION_RECEIVER,
                    _session_payload(
                        state,
                        files,
                        webspace_id=webspace_id,
                        schedule_prewarm=True,
                        defer_media=_index_busy_for_state(state),
                    ),
                    webspace_id,
                )
        _publish(_ENDPOINTS_RECEIVER, _endpoint_payload(devices, state), webspace_id)
    except Exception:
        _log.debug("slideshow root event poll failed", exc_info=True)


@subscribe("sys.ready")
def on_sys_ready(evt: Any) -> None:
    state = _load_state()
    if state.get("running") or _unique_texts(state.get("selected_codes")):
        _ensure_polling(default_webspace_id())


def _pending_device_cache_request_matches(state: Mapping[str, Any], root: Path, scan_started: float) -> bool:
    try:
        requested_at = float(state.get("last_device_cache_requested_at") or 0)
    except Exception:
        requested_at = 0
    if requested_at <= 0 or requested_at < scan_started - 300 or time.time() - requested_at > 3600:
        return False
    requested_root = _text(state.get("last_device_cache_source_dir"))
    if requested_root and requested_root.casefold() != str(root).casefold():
        return False
    requested_folder = _text(state.get("last_device_cache_selected_folder"))
    return requested_folder == _text(state.get("selected_folder"))


def _schedule_pending_device_cache_after_index(root: Path, scan_started: float, webspace_id: str | None) -> None:
    state = _load_state()
    if not _pending_device_cache_request_matches(state, root, scan_started):
        return
    try:
        files = _folder_cache_files_for_state(state, _MAX_DEVICE_CACHE_ITEMS)
        if files:
            _schedule_device_cache_warm(state, files, webspace_id=webspace_id)
    except Exception:
        _log.debug("failed to schedule pending ReDevice slideshow cache", exc_info=True)


def _poll_loop() -> None:
    while not _poll_stop.wait(_POLL_INTERVAL_S):
        _poll_once(_poll_webspace_id or None)


def _ensure_polling(webspace_id: str | None = None, *, force: bool = False) -> bool:
    global _poll_thread, _poll_webspace_id
    service_owner = _text(os.environ.get("ADAOS_SERVICE_SKILL")) == "slideshow_skill"
    if not force and not service_owner:
        return False
    if webspace_id:
        _poll_webspace_id = _text(webspace_id)
    with _poll_lock:
        if _poll_thread is not None and _poll_thread.is_alive():
            return True
        _poll_stop.clear()
        _poll_thread = threading.Thread(target=_poll_loop, name="slideshow-root-poll", daemon=True)
        _poll_thread.start()
    return True


@tool
def activate_slideshow_runtime(webspace_id: str | None = None, **_payload: Any) -> dict[str, Any]:
    state = _load_state()
    selected = _unique_texts(state.get("selected_codes"))
    should_poll = bool(state.get("running") or selected)
    polling_local = False
    if should_poll:
        polling_local = _ensure_polling(webspace_id or default_webspace_id())
        if polling_local:
            _poll_once(webspace_id or default_webspace_id())
    return {
        "ok": True,
        "polling": should_poll,
        "polling_local": polling_local,
        "polling_owner": "service_process",
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
    _publish(
        _SESSION_RECEIVER,
        _session_payload(
            state,
            _files_for_state(state, _MAX_CONTROL_SCAN),
            webspace_id=webspace_id,
            schedule_prewarm=True,
            defer_media=_index_busy_for_state(state),
        ),
        webspace_id,
    )
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
    command = _sync_running_surface(state, files, webspace_id=webspace_id, reason="source_changed")
    devices = _load_devices()
    _publish(_ENDPOINTS_RECEIVER, _endpoint_payload(devices, state), webspace_id)
    _publish(
        _SESSION_RECEIVER,
        _session_payload(
            state,
            files,
            last_command=command or None,
            webspace_id=webspace_id,
            schedule_prewarm=True,
            defer_media=_index_busy_for_state(state),
        ),
        webspace_id,
    )
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
    root = _source_dir(state.get("source_dir"))
    status: dict[str, Any] | None = None
    meta = _ensure_index(root)
    if _text(meta.get("error")) == "index_missing" and not _active_index_status(root):
        status = _start_index_job(root, webspace_id=webspace_id)
    payload = _folders_payload(state, status=status, index_meta=meta)
    _publish(_FOLDERS_RECEIVER, payload, webspace_id, force=True)
    _publish(_INDEX_RECEIVER, payload.get("status") if isinstance(payload.get("status"), Mapping) else _index_status(_source_dir(state.get("source_dir"))), webspace_id)
    return payload


@tool
def cancel_slideshow_photo_index(webspace_id: str | None = None) -> dict[str, Any]:
    status = _cancel_index_job(webspace_id)
    return status


def _folders_payload(
    state: Mapping[str, Any],
    *,
    status: Mapping[str, Any] | None = None,
    index_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = _source_dir(state.get("source_dir"))
    meta = dict(index_meta or _ensure_index(root))
    status_payload = dict(status or _index_status(root))
    if not meta.get("ok") and status_payload.get("ok"):
        meta = {
            "ok": True,
            "root_dir": str(root),
            "photo_count": _count_int(status_payload.get("photo_count") or status_payload.get("display_count")),
            "source": "status",
            "status": _text(status_payload.get("status")) or "running",
        }
    return {
        "ok": bool(meta.get("ok", True)),
        "source_dir": str(root),
        "selected_folder": _text(state.get("selected_folder")),
        "index": meta,
        "status": status_payload,
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


def _index_refresh_payload(state: Mapping[str, Any], status: Mapping[str, Any]) -> dict[str, Any]:
    root = _source_dir(state.get("source_dir"))
    photo_count = _count_int(status.get("photo_count") or status.get("display_count"))
    return {
        "ok": bool(status.get("ok", True)),
        "source_dir": str(root),
        "selected_folder": _text(state.get("selected_folder")),
        "count": 0,
        "items": [],
        "folders": [],
        "index": {
            "ok": bool(status.get("ok", True)),
            "root_dir": str(root),
            "photo_count": photo_count,
            "source": "status",
        },
        "status": dict(status),
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
    _publish(_INDEX_RECEIVER, status, webspace_id)
    return _index_refresh_payload(state, status)


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
    files = _files_for_state(state, _MAX_CONTROL_SCAN)
    command = _sync_running_surface(state, files, webspace_id=webspace_id, reason="folder_changed")
    preview = _preview_payload(state, 48)
    _publish(_FOLDERS_RECEIVER, _folders_payload(state), webspace_id)
    _publish(_PREVIEW_RECEIVER, preview, webspace_id)
    _publish(
        _SESSION_RECEIVER,
        _session_payload(
            state,
            files,
            last_command=command or None,
            webspace_id=webspace_id,
            schedule_prewarm=True,
            defer_media=_index_busy_for_state(state),
        ),
        webspace_id,
    )
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
    removed_token = ""
    if token in selected:
        selected = [item for item in selected if item != token]
        removed_token = token
    elif token:
        selected.append(token)
    state["selected_codes"] = selected
    state = _save_state(state)
    files = _files_for_state(state, _MAX_CONTROL_SCAN)
    command: dict[str, Any] = {}
    if removed_token:
        _stop_selected(state, code=removed_token, webspace_id=webspace_id)
    command = _sync_running_surface(state, files, webspace_id=webspace_id, reason="endpoint_selection_changed")
    devices = _load_devices()
    payload = _endpoint_payload(devices, state)
    _publish(_ENDPOINTS_RECEIVER, payload, webspace_id)
    _publish(
        _SESSION_RECEIVER,
        _session_payload(
            state,
            files,
            last_command=command or None,
            webspace_id=webspace_id,
            schedule_prewarm=True,
            defer_media=_index_busy_for_state(state),
        ),
        webspace_id,
    )
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
    files = _files_for_state(state, _MAX_CONTROL_SCAN)
    command = _sync_running_surface(state, files, webspace_id=webspace_id, reason="endpoint_selected")
    payload = _endpoint_payload(devices, state)
    _publish(_ENDPOINTS_RECEIVER, payload, webspace_id)
    _publish(
        _SESSION_RECEIVER,
        _session_payload(
            state,
            files,
            last_command=command or None,
            webspace_id=webspace_id,
            schedule_prewarm=True,
            defer_media=_index_busy_for_state(state),
        ),
        webspace_id,
    )
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
    interval_ms: int | str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    if source_dir:
        state["source_dir"] = str(_source_dir(source_dir))
    token = _text(action).lower()
    if interval_ms is not None:
        state["interval_ms"] = _interval_ms(interval_ms)
    elif token.startswith("interval_"):
        state["interval_ms"] = _interval_ms(token)
    if token in {"cache_folder", "cache", "warm_cache", "device_cache"}:
        root = _source_dir(state.get("source_dir"))
        state["last_device_cache_requested_at"] = time.time()
        state["last_device_cache_source_dir"] = str(root)
        state["last_device_cache_selected_folder"] = _text(state.get("selected_folder"))
        state = _save_state(state)
        meta = _ensure_index(root)
        active_status = _active_index_status(root)
        if active_status or _text(meta.get("error")) == "index_missing":
            status = active_status or _start_index_job(root, webspace_id=webspace_id)
            payload = _device_cache_payload(
                status="indexing",
                state=state,
                job_key="",
                target_codes=_unique_texts([code]) if code else _unique_texts(state.get("selected_codes")),
                sent_codes=[],
                photo_count=_count_int(status.get("photo_count") or status.get("display_count")),
                batch_count=0,
            )
            _memory_set(_DEVICE_CACHE_STATE_KEY, payload)
            _publish(_COMMAND_RECEIVER, _remember_command_payload(payload), webspace_id, force=True)
            _publish(_INDEX_RECEIVER, status, webspace_id)
            _ensure_polling(webspace_id)
            return payload
        if not meta.get("ok"):
            payload = _device_cache_payload(
                status="failed",
                state=state,
                job_key="",
                target_codes=_unique_texts([code]) if code else _unique_texts(state.get("selected_codes")),
                sent_codes=[],
                photo_count=0,
                batch_count=0,
                error=_text(meta.get("error")) or "index_unavailable",
            )
            _memory_set(_DEVICE_CACHE_STATE_KEY, payload)
            _publish(_COMMAND_RECEIVER, _remember_command_payload(payload), webspace_id, force=True)
            return payload
        files = _folder_cache_files_for_state(state, _MAX_DEVICE_CACHE_ITEMS)
        _ensure_polling(webspace_id)
        return _schedule_device_cache_warm(state, files, code=code, webspace_id=webspace_id)
    files = _files_for_state(state, _MAX_CONTROL_SCAN)
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
        if not result.get("ok"):
            return {
                "ok": False,
                "error": _text(result.get("error")) or "telegram_photo_failed",
                "telegram": dict(result),
                "source_name": current.name,
            }
        return {"ok": True, "telegram": dict(result), "source_name": current.name}
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
        files = _files_for_state(state, _MAX_CONTROL_SCAN)
    elif token == "all":
        state["scope"] = "all"
        state["current_index"] = 0
        files = _files_for_state(state, _MAX_CONTROL_SCAN)
    elif token == "fullscreen_on":
        state["fullscreen"] = True
    elif token == "fullscreen_off":
        state["fullscreen"] = False
    elif token == "fit":
        state["display_mode"] = "fit"
    elif token == "crop":
        state["display_mode"] = "crop"
    elif token.startswith("interval_") or token in {"interval", "set_interval"}:
        state["interval_ms"] = _interval_ms(state.get("interval_ms"))
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
def voice_control_redevice_slideshow(
    action: str | None = None,
    device_name: str | None = None,
    text: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    resolved_action = _voice_action(action, text)
    if not resolved_action:
        return {"ok": False, "error": "voice_action_not_recognized", "text": _text(text)}
    query = _text(device_name) or _device_query_from_text(text, resolved_action)
    resolved = device_access.resolve_endpoint_device(query=query, assignment="slideshow") if query else {}
    if not resolved.get("ok") and query:
        resolved = device_access.resolve_endpoint_device(query=query, active_app="slideshow_skill")
    if not resolved.get("ok") and query:
        resolved = device_access.resolve_endpoint_device(query=query)
    state = _load_state()
    code = _text(resolved.get("code")) if resolved.get("ok") else ""
    if code:
        state["selected_codes"] = _unique_texts([code])
        state = _save_state(state)
        device_access.assign_endpoint(code=code, assignment="slideshow")
    elif not _unique_texts(state.get("selected_codes")):
        fallback = device_access.resolve_endpoint_device(assignment="slideshow")
        if not fallback.get("ok"):
            fallback = device_access.resolve_endpoint_device(active_app="slideshow_skill")
        if fallback.get("ok"):
            code = _text(fallback.get("code"))
            state["selected_codes"] = _unique_texts([code])
            state = _save_state(state)
    result = control_redevice_slideshow(
        resolved_action,
        code=code or None,
        webspace_id=webspace_id,
    )
    return {
        **dict(result),
        "voice_action": resolved_action,
        "device_query": query,
        "resolved_endpoint": {
            "ok": bool(resolved.get("ok")),
            "device_ref": resolved.get("device_ref"),
            "code": code,
        },
    }


@tool
def rename_redevice_endpoint(
    code: str,
    display_name: str | None = None,
    aliases: str | list[str] | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    alias_list = _unique_texts(aliases)
    result = device_access.update_endpoint_profile(code=code, display_name=display_name, aliases=alias_list)
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
    session_payload = _session_payload(
        state,
        files,
        webspace_id=webspace_id,
        schedule_prewarm=True,
        defer_media=_index_busy_for_state(state),
    )
    _publish(_ENDPOINTS_RECEIVER, endpoint_payload, webspace_id)
    _publish(_SESSION_RECEIVER, session_payload, webspace_id)
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
        index_busy = _index_busy_for_state(state)
        files = _files_for_state(state, _MAX_CONTROL_SCAN)
        _publish(
            receiver,
            _session_payload(
                state,
                files,
                last_command=_last_command_payload(),
                webspace_id=webspace_id,
                schedule_prewarm=False,
                defer_media=True,
                defer_reason="index_running" if index_busy else "snapshot_reconnect",
                resolve_endpoint_label=False,
            ),
            webspace_id,
            force=True,
        )
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
