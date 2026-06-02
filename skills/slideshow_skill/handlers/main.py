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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import skill_memory
from adaos.sdk.data.skill_env import skill_env_path
from adaos.sdk.io import stream_publish
from adaos.sdk.redevice import ReDeviceBridge, compact_endpoint, list_endpoints as sdk_list_endpoints
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
_SUPPORTED = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
_ENDPOINT_SIZE = (1280, 800)
_WIDGET_SIZE = (420, 260)
_MAX_SCAN = 2000
_MAX_CONTROL_SCAN = 240
_MAX_ENDPOINT_CURRENT = 10
_MAX_ENDPOINT_FAVORITES = 20
_STATE_KEY = "slideshow_skill.state"
_INDEX_META_KEY = "slideshow_skill.photo_index"
_POLL_INTERVAL_S = 2.5
_log = logging.getLogger("skills.slideshow_skill")
_poll_lock = threading.Lock()
_poll_thread: threading.Thread | None = None
_poll_stop = threading.Event()
_poll_webspace_id = ""


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


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
        "current_index": 0,
        "favorites": [],
        "source_dir": str(_source_dir()),
        "selected_folder": "",
        "last_event_by_code": {},
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
    if not isinstance(state.get("last_event_by_code"), Mapping):
        state["last_event_by_code"] = {}
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


def _ensure_index(root: Path, *, force: bool = False) -> dict[str, Any]:
    root = root.expanduser()
    if not root.exists():
        return {"ok": False, "error": "source_dir_missing", "root_dir": str(root), "photo_count": 0}
    existing = _index_meta(root)
    if existing and not force:
        return {"ok": True, **existing, "source": "cache"}

    started = time.time()
    now = time.time()
    seen: set[str] = set()
    count = 0
    with _connect_index() as conn:
        for current, dirs, names in os.walk(str(root)):
            dirs[:] = [name for name in dirs if name != ".adaos-thumbs"]
            for name in names:
                path = Path(current) / name
                if path.suffix.lower() not in _SUPPORTED:
                    continue
                try:
                    stat = path.stat()
                    ref = _content_ref(path)
                    seen.add(ref)
                    conn.execute(
                        """
                        INSERT INTO photos (
                            content_ref, source_path, root_dir, rel_path, top_folder,
                            source_name, ext, size, mtime, favorite, hidden, indexed_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
                            (SELECT favorite FROM photos WHERE content_ref = ?), 0
                        ), COALESCE(
                            (SELECT hidden FROM photos WHERE content_ref = ?), 0
                        ), ?)
                        ON CONFLICT(content_ref) DO UPDATE SET
                            source_path = excluded.source_path,
                            root_dir = excluded.root_dir,
                            rel_path = excluded.rel_path,
                            top_folder = excluded.top_folder,
                            source_name = excluded.source_name,
                            ext = excluded.ext,
                            size = excluded.size,
                            mtime = excluded.mtime,
                            indexed_at = excluded.indexed_at
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
                            ref,
                            ref,
                            now,
                        ),
                    )
                    count += 1
                except Exception:
                    _log.debug("failed to index slideshow photo path=%s", path, exc_info=True)
        stale_rows = conn.execute("SELECT content_ref FROM photos WHERE root_dir = ?", (str(root),)).fetchall()
        for row in stale_rows:
            if row["content_ref"] not in seen:
                conn.execute("DELETE FROM photos WHERE content_ref = ?", (row["content_ref"],))
        conn.execute(
            "INSERT OR REPLACE INTO roots(root_dir, indexed_at, photo_count) VALUES (?, ?, ?)",
            (str(root), now, count),
        )
    meta = {"ok": True, "root_dir": str(root), "indexed_at": now, "photo_count": count, "duration_s": round(time.time() - started, 3), "source": "scan"}
    _memory_set(_INDEX_META_KEY, meta)
    return meta


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
    items = [{"id": "", "title": "All photos", "subtitle": str(root), "selected": selected == ""}]
    try:
        with _connect_index() as conn:
            rows = conn.execute(
                """
                SELECT top_folder, COUNT(*) AS count,
                       SUM(CASE WHEN favorite = 1 THEN 1 ELSE 0 END) AS favorites
                FROM photos
                WHERE root_dir = ? AND hidden = 0 AND top_folder != ''
                GROUP BY top_folder
                ORDER BY top_folder ASC
                """,
                (str(root),),
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
        quality = 78 if label == "endpoint" else 70
        canvas.save(cache_path, "JPEG", quality=quality, optimize=True)
    return cache_path, False


def _data_uri(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _content_item(path: Path) -> dict[str, Any]:
    thumb, cached = _thumbnail(path, _ENDPOINT_SIZE, "endpoint")
    ref = _content_ref(path)
    return {
        "content_ref": ref,
        "source_path": str(path),
        "source_name": path.name,
        "title": path.stem,
        "mime": "image/jpeg",
        "cached": cached,
        "thumbnail_path": str(thumb),
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


def _endpoint_window(files: list[Path], state: Mapping[str, Any]) -> list[Path]:
    selected = _selected_photos(files, state)
    if not selected:
        return []
    index = int(state.get("current_index") or 0) % len(selected)
    current: list[Path] = []
    for offset in range(min(_MAX_ENDPOINT_CURRENT, len(selected))):
        current.append(selected[(index + offset) % len(selected)])
    fav_paths = _favorite_files_for_state(state, _MAX_ENDPOINT_FAVORITES)
    by_ref: dict[str, Path] = {}
    for path in [*current, *fav_paths]:
        by_ref[_content_ref(path)] = path
    return list(by_ref.values())


def _advance(state: dict[str, Any], files: list[Path], step: int) -> dict[str, Any]:
    selected = _selected_photos(files, state)
    if not selected:
        state["current_index"] = 0
        return state
    if _text(state.get("mode")) == "random" and step != 0:
        state["current_index"] = random.randrange(0, len(selected))
    else:
        state["current_index"] = (int(state.get("current_index") or 0) + step) % len(selected)
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


def _session_payload(state: Mapping[str, Any], files: list[Path], *, last_command: Mapping[str, Any] | None = None) -> dict[str, Any]:
    current = _current_photo(files, state)
    image: dict[str, Any] = {"src": "", "mime": "image/jpeg"}
    title = "No photo"
    content_ref = ""
    if current is not None:
        thumb, _cached = _thumbnail(current, _WIDGET_SIZE, "widget")
        image = {"src": _data_uri(thumb), "mime": "image/jpeg"}
        title = current.name
        content_ref = _content_ref(current)
    selected_codes = _unique_texts(state.get("selected_codes"))
    header = ", ".join(selected_codes) if selected_codes else "No endpoint"
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


def _publish(receiver: str, payload: Mapping[str, Any], webspace_id: str | None = None) -> None:
    try:
        stream_publish(receiver, dict(payload), _meta={"webspace_id": _text(webspace_id) or default_webspace_id()})
    except Exception:
        _log.debug("failed to publish slideshow stream receiver=%s", receiver, exc_info=True)


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


def _build_command(pair_code: str, items: list[dict[str, Any]], state: Mapping[str, Any], *, autoplay: bool) -> dict[str, Any]:
    command_id = "cmd:slideshow:" + hashlib.sha256(f"{pair_code}:{_now()}".encode("utf-8")).hexdigest()[:16]
    owner = _owner()
    return {
        "command_id": command_id,
        "type": "display.render_surface",
        "owner": owner,
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
            "cache_policy": {
                "max_current_items": _MAX_ENDPOINT_CURRENT,
                "max_favorite_items": _MAX_ENDPOINT_FAVORITES,
                "receiver_cache_items": _MAX_ENDPOINT_CURRENT + _MAX_ENDPOINT_FAVORITES,
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
    selected_codes = _unique_texts([code] if code else state.get("selected_codes"))
    if code and selected_codes and isinstance(state, dict):
        state["selected_codes"] = selected_codes
        _save_state(state)
    if not selected_codes:
        device = _select_device(devices)
        selected_codes = [_text(device.get("code"))] if device else []
        if selected_codes and isinstance(state, dict):
            state["selected_codes"] = selected_codes
            _save_state(state)
    if not selected_codes:
        return {"ok": False, "error": "no_redevice_endpoint"}
    window = _endpoint_window(files, state)
    if not window:
        return {"ok": False, "error": "no_supported_photos", "source_dir": state.get("source_dir")}
    items = [_content_item(path) for path in window]
    bridge = ReDeviceBridge()
    results: list[dict[str, Any]] = []
    first_command_id = ""
    for pair_code in selected_codes:
        autoplay = bool(state.get("running")) and (not state.get("sync") or pair_code == selected_codes[0])
        command = _build_command(pair_code, items, state, autoplay=autoplay)
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
            }
        )
    payload = {
        "ok": any(bool(item.get("ok")) for item in results),
        "command_id": first_command_id,
        "source_dir": state.get("source_dir"),
        "selected_codes": selected_codes,
        "item_count": len(items),
        "items": [{"source_name": item["source_name"], "thumbnail_path": item["thumbnail_path"], "cached": item["cached"]} for item in items],
        "results": results,
        "updated_at": _now(),
    }
    payload["command_items"] = _command_items(payload)
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
        if action == "next" and state.get("sync"):
            _advance(state, files, 1)
            changed = True
        elif action == "favorite_toggle":
            _toggle_favorite_ref(state, _text(event.get("item_ref")))
            changed = True
    state["last_event_by_code"] = last_by_code
    if changed:
        _save_state(state)
        if broadcast and state.get("sync"):
            _send_to_selected(state, _files_for_state(state, _MAX_CONTROL_SCAN), webspace_id=webspace_id)
    return state


def _poll_once(webspace_id: str | None = None) -> None:
    try:
        state = _load_state()
        if not state.get("sync") or not _unique_texts(state.get("selected_codes")):
            return
        devices = _load_devices()
        files = _files_for_state(state, _MAX_CONTROL_SCAN)
        state = _apply_root_events(state, devices, files, webspace_id=webspace_id, broadcast=True)
        _publish(_SESSION_RECEIVER, _session_payload(state, _files_for_state(state, _MAX_CONTROL_SCAN)), webspace_id)
        _publish(_ENDPOINTS_RECEIVER, _endpoint_payload(devices, state), webspace_id)
    except Exception:
        _log.debug("slideshow root event poll failed", exc_info=True)


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
    files = _files_for_state(state, limit)
    payload = {
        "ok": True,
        "source_dir": str(_source_dir(state.get("source_dir"))),
        "selected_folder": _text(state.get("selected_folder")),
        "count": len(files),
        "items": [_preview_item(p) for p in files],
        "updated_at": _now(),
    }
    _publish(_PREVIEW_RECEIVER, payload, webspace_id)
    _publish(_FOLDERS_RECEIVER, _folders_payload(state), webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, _files_for_state(state, _MAX_CONTROL_SCAN)), webspace_id)
    return payload


def _folders_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    meta = _ensure_index(_source_dir(state.get("source_dir")))
    return {
        "ok": bool(meta.get("ok", True)),
        "source_dir": str(_source_dir(state.get("source_dir"))),
        "selected_folder": _text(state.get("selected_folder")),
        "index": meta,
        "items": _folder_items(state),
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
    meta = _ensure_index(_source_dir(state.get("source_dir")), force=True)
    files = _files_for_state(state, 48)
    preview = {
        "ok": bool(meta.get("ok")),
        "source_dir": str(_source_dir(state.get("source_dir"))),
        "selected_folder": _text(state.get("selected_folder")),
        "count": len(files),
        "items": [_preview_item(p) for p in files],
        "folders": _folder_items(state),
        "index": meta,
        "updated_at": _now(),
    }
    _publish(_FOLDERS_RECEIVER, _folders_payload(state), webspace_id)
    _publish(_PREVIEW_RECEIVER, preview, webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, _files_for_state(state, _MAX_CONTROL_SCAN)), webspace_id)
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
    state["selected_folder"] = _text(folder)
    state["current_index"] = 0
    state = _save_state(state)
    files = _files_for_state(state, 48)
    preview = {
        "ok": True,
        "source_dir": str(_source_dir(state.get("source_dir"))),
        "selected_folder": _text(state.get("selected_folder")),
        "count": len(files),
        "items": [_preview_item(p) for p in files],
        "updated_at": _now(),
    }
    _publish(_FOLDERS_RECEIVER, _folders_payload(state), webspace_id)
    _publish(_PREVIEW_RECEIVER, preview, webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, _files_for_state(state, _MAX_CONTROL_SCAN)), webspace_id)
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
    elif token == "stop":
        state["running"] = False
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
    _ensure_polling(webspace_id)
    return {**endpoint_payload, "session": session_payload}


def _event_payload(evt: Any) -> Mapping[str, Any]:
    payload = getattr(evt, "payload", evt)
    return payload if isinstance(payload, Mapping) else {}


def _matches_receiver(payload: Mapping[str, Any]) -> bool:
    receiver = _text(payload.get("receiver"))
    return receiver in {_ENDPOINTS_RECEIVER, _PREVIEW_RECEIVER, _FOLDERS_RECEIVER, _SESSION_RECEIVER, _COMMAND_RECEIVER, "slideshow_skill.*"}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = _event_payload(evt)
    if not _matches_receiver(payload):
        return
    webspace_id = _text(payload.get("webspace_id") or payload.get("workspace_id")) or default_webspace_id()
    state = _load_state()
    files = _files_for_state(state, _MAX_CONTROL_SCAN)
    devices = _load_devices()
    state = _apply_root_events(state, devices, files, webspace_id=webspace_id, broadcast=True)
    _publish(_ENDPOINTS_RECEIVER, _endpoint_payload(devices, state), webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, files), webspace_id)
    _publish(_FOLDERS_RECEIVER, _folders_payload(state), webspace_id)
    _ensure_polling(webspace_id)


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = _event_payload(evt)
    if _matches_receiver(payload):
        on_webio_stream_snapshot_requested(evt)
