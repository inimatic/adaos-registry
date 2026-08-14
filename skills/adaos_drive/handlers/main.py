from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest, urlopen

from adaos.sdk import navigation as sdk_navigation
from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import skill_memory_get, skill_memory_set
from adaos.sdk.io.out import stream_publish
from adaos.services.drive_public_links import (
    build_root_public_content_url,
    build_root_public_list_url,
    issue_hub_token,
    issue_public_token,
    list_hub_public_links,
    register_hub_public_link,
    register_root_public_link,
    revoke_hub_public_link,
)
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import load_config
from adaos.services.skill.artifacts import resolve_skill_file_path, skill_files_dir
from adaos.services.webspace_id import coerce_webspace_id
from adaos.services.zone_hosts import DEFAULT_PUBLIC_APP_BASE_URL, canonical_zone_id, zone_public_base_url

_SKILL_NAME = "adaos_drive"
_RECEIVER = "adaos_drive.browser"
_DEFAULT_WEBSPACE_ID = "desktop"
_STATE_MEMORY_PREFIX = "adaos_drive.state.v1"
_MAX_ITEMS_PER_FOLDER = 500
_MAX_TREE_CHILDREN = 250
_MAX_PREVIEW_BYTES = 512 * 1024
_UPLOAD_PURPOSE = "uploads"
_DEFAULT_PUBLIC_DOWNLOAD_BASE_URL = DEFAULT_PUBLIC_APP_BASE_URL
_LOG = logging.getLogger(_SKILL_NAME)

_TEXT_EXTENSIONS = {
    ".bat",
    ".c",
    ".cfg",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".log",
    ".md",
    ".php",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
_ARCHIVE_EXTENSIONS = {".7z", ".bz2", ".gz", ".rar", ".tar", ".tgz", ".zip"}
_VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
_AUDIO_EXTENSIONS = {".flac", ".m4a", ".mp3", ".ogg", ".wav"}
_DOC_EXTENSIONS = {".doc", ".docx", ".odt", ".pdf", ".ppt", ".pptx", ".rtf", ".xls", ".xlsx"}

_STATE_BY_WEBSPACE: dict[str, dict[str, Any]] = {}
_FALLBACK_MEMORY: dict[str, Any] = {}
_GLOBAL_SOURCES_KEY = f"{_STATE_MEMORY_PREFIX}.sources"


def _now() -> float:
    return time.time()


def _now_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts or _now())))


def _payload(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    raw = getattr(evt, "payload", evt)
    data = dict(raw) if isinstance(raw, Mapping) else {}
    data.update({k: v for k, v in kwargs.items() if v is not None})
    return data


def _webspace_id(value: Any = None) -> str:
    return coerce_webspace_id(value, fallback=_DEFAULT_WEBSPACE_ID)


def _state_key(webspace_id: str) -> str:
    return f"{_STATE_MEMORY_PREFIX}.{_webspace_id(webspace_id)}"


def _state_revision(state: Mapping[str, Any] | None) -> int:
    if not isinstance(state, Mapping):
        return 0
    for key in ("_stream_rev", "sequence", "rev", "revision"):
        try:
            value = int(state.get(key) or 0)
        except Exception:
            value = 0
        if value:
            return value
    return 0


def _mem_get(key: str, default: Any = None) -> Any:
    try:
        return skill_memory_get(key, default)
    except Exception:
        return deepcopy(_FALLBACK_MEMORY.get(key, default))


def _mem_set(key: str, value: Any) -> None:
    try:
        skill_memory_set(key, value)
    except Exception:
        _FALLBACK_MEMORY[key] = deepcopy(value)


def _safe_label(value: Any, fallback: str) -> str:
    label = str(value or "").strip()
    return label[:80] if label else fallback


def _source_id(label: str, root: Path) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", label.strip().lower()).strip("-") or "local"
    digest = hashlib.sha1(str(root.resolve()).encode("utf-8", "replace")).hexdigest()[:10]
    return f"{slug}-{digest}"[:80]


def _default_root() -> Path:
    candidates: list[Any] = [
        os.getenv("ADAOS_DRIVE_DEFAULT_ROOT"),
        os.getenv("ADAOS_WORKSPACE_DIR"),
    ]
    try:
        candidates.append(get_ctx().paths.workspace_dir())
    except Exception:
        pass
    candidates.extend([Path.cwd(), Path.home()])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            path = Path(candidate).expanduser().resolve()
            if path.exists() and path.is_dir():
                return path
        except Exception:
            continue
    return Path.cwd().resolve()


def _source_payload(label: str, root: Path) -> dict[str, Any]:
    resolved = root.expanduser().resolve()
    return {
        "id": _source_id(label, resolved),
        "kind": "local",
        "label": _safe_label(label, resolved.name or str(resolved)),
        "path": str(resolved),
        "root_path": str(resolved),
        "description": str(resolved),
        "connected": resolved.exists() and resolved.is_dir(),
    }


def _normalize_sources(value: Any) -> list[dict[str, Any]]:
    sources = [dict(item) for item in value or [] if isinstance(item, Mapping)]
    return [item for item in sources if str(item.get("id") or "").strip() and str(item.get("path") or "").strip()]


def _merge_sources(*source_lists: Any) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for source_list in source_lists:
        for item in _normalize_sources(source_list):
            source_id = str(item.get("id") or "").strip()
            if not source_id:
                continue
            if source_id not in merged:
                order.append(source_id)
            merged[source_id] = item
    return [merged[source_id] for source_id in order if source_id in merged]


def _ensure_panel_sources(state: dict[str, Any]) -> None:
    sources = _normalize_sources(state.get("sources"))
    if not sources:
        sources = _default_state()["sources"]
    state["sources"] = sources
    valid_source_ids = {str(item.get("id") or "") for item in sources}
    panels = state.setdefault("panels", {})
    for panel in ("left", "right"):
        panel_state = dict(panels.get(panel) or {}) if isinstance(panels.get(panel), Mapping) else {}
        source_id = str(panel_state.get("source_id") or "").strip()
        if source_id not in valid_source_ids:
            panel_state["source_id"] = sources[0]["id"]
            panel_state["path"] = ""
            panel_state["selected_path"] = ""
            panel_state["selected_id"] = ""
        panels[panel] = panel_state


def _load_global_sources(webspace_id: str | None = None) -> list[dict[str, Any]]:
    candidates: list[Any] = [_mem_get(_GLOBAL_SOURCES_KEY, None)]
    ws = _webspace_id(webspace_id)
    for candidate_ws in (ws, _DEFAULT_WEBSPACE_ID):
        if candidate_ws:
            persisted = _mem_get(_state_key(candidate_ws), None)
            if isinstance(persisted, Mapping):
                candidates.append(persisted.get("sources"))
    return _merge_sources(*candidates)


def _persist_global_sources(state: Mapping[str, Any]) -> None:
    sources = _normalize_sources(state.get("sources"))
    if sources:
        _mem_set(_GLOBAL_SOURCES_KEY, sources)


def _default_state() -> dict[str, Any]:
    root = _default_root()
    source = _source_payload(root.name or "Local folder", root)
    return {
        "schema": "adaos_drive.state.v1",
        "sources": [source],
        "active_panel": "left",
        "panels": {
            "left": {
                "id": "left",
                "source_id": source["id"],
                "path": "",
                "selected_path": "",
                "selected_id": "",
                "sort": "name_asc",
                "tree": {},
            },
            "right": {
                "id": "right",
                "source_id": source["id"],
                "path": "",
                "selected_path": "",
                "selected_id": "",
                "sort": "name_asc",
                "tree": {},
            },
        },
        "preview": _empty_preview(),
        "last_link": _empty_link(),
        "messages": [],
        "sequence": 0,
        "updated_at": _now(),
    }


def _empty_preview() -> dict[str, Any]:
    return {
        "mode": "empty",
        "panel": "right",
        "title": "No preview",
        "content": "",
        "language": "text",
        "item": None,
        "url": "",
        "download_url": "",
        "summary": "",
        "updated_at": None,
    }


def _empty_link() -> dict[str, Any]:
    return {
        "id": "",
        "public_token": "",
        "item": None,
        "url": "",
        "view_url": "",
        "download_url": "",
        "root_download_url": "",
        "list_url": "",
        "zone": "",
        "expires_at": None,
        "resource_kind": "",
        "readonly": True,
        "capabilities": [],
        "label": "",
        "summary": "",
        "created_at": None,
    }


def _coerce_state(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    sources = _normalize_sources(value.get("sources"))
    if not sources:
        return None
    state = _default_state()
    state["sources"] = sources
    panels = value.get("panels") if isinstance(value.get("panels"), Mapping) else {}
    for panel in ("left", "right"):
        current = dict(panels.get(panel) or {}) if isinstance(panels.get(panel), Mapping) else {}
        source_id = str(current.get("source_id") or sources[0]["id"]).strip()
        if not any(str(item.get("id")) == source_id for item in sources):
            source_id = sources[0]["id"]
        state["panels"][panel] = {
            "id": panel,
            "source_id": source_id,
            "path": _clean_rel(current.get("path") or ""),
            "selected_path": _clean_rel(current.get("selected_path") or ""),
            "selected_id": str(current.get("selected_id") or "").strip(),
            "sort": str(current.get("sort") or "name_asc"),
            "tree": current.get("tree") if isinstance(current.get("tree"), Mapping) else {},
        }
    active_panel = str(value.get("active_panel") or "left").strip().lower()
    state["active_panel"] = active_panel if active_panel in {"left", "right"} else "left"
    state["preview"] = dict(value.get("preview") or _empty_preview()) if isinstance(value.get("preview"), Mapping) else _empty_preview()
    state["last_link"] = dict(value.get("last_link") or _empty_link()) if isinstance(value.get("last_link"), Mapping) else _empty_link()
    state["messages"] = list(value.get("messages") or [])[-10:] if isinstance(value.get("messages"), list) else []
    state["sequence"] = _state_revision(value)
    state["updated_at"] = float(value.get("updated_at") or _now())
    return state


def _load_persisted_state(webspace_id: str) -> dict[str, Any]:
    state = _coerce_state(_mem_get(_state_key(webspace_id), None)) or _default_state()
    state["sources"] = _merge_sources(state.get("sources"), _load_global_sources(webspace_id))
    _ensure_panel_sources(state)
    return state


def _load_state(webspace_id: str) -> dict[str, Any]:
    ws = _webspace_id(webspace_id)
    state = _STATE_BY_WEBSPACE.get(ws)
    if state is not None:
        return state
    loaded = _load_persisted_state(ws)
    _STATE_BY_WEBSPACE[ws] = loaded
    return loaded


def _persist_state(webspace_id: str, state: dict[str, Any]) -> None:
    state["sequence"] = int(state.get("sequence") or 0) + 1
    state["updated_at"] = _now()
    _ensure_panel_sources(state)
    _persist_global_sources(state)
    _mem_set(_state_key(webspace_id), state)


def _panel_name(value: Any, state: Mapping[str, Any] | None = None) -> str:
    token = str(value or "").strip().lower()
    if token in {"left", "right"}:
        return token
    active = str((state or {}).get("active_panel") or "left").strip().lower()
    return active if active in {"left", "right"} else "left"


def _other_panel(panel: str) -> str:
    return "right" if panel == "left" else "left"


def _source_map(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id")): dict(item)
        for item in state.get("sources") or []
        if isinstance(item, Mapping) and str(item.get("id") or "").strip()
    }


def _source_for_panel(state: Mapping[str, Any], panel: str) -> dict[str, Any]:
    sources = _source_map(state)
    panel_state = dict((state.get("panels") or {}).get(panel) or {})
    source_id = str(panel_state.get("source_id") or "").strip()
    source = sources.get(source_id)
    if source is None and sources:
        source = next(iter(sources.values()))
    if source is None:
        root = _default_root()
        source = _source_payload(root.name or "Local folder", root)
    return source


def _clean_rel(value: Any) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    if not raw or raw == ".":
        return ""
    if raw.startswith("/") or ":" in raw:
        raise ValueError("path must be relative to the selected source")
    parts: list[str] = []
    for part in raw.split("/"):
        token = part.strip()
        if not token or token == ".":
            continue
        if token == "..":
            raise ValueError("path cannot contain parent traversal")
        if "\x00" in token:
            raise ValueError("path contains a null byte")
        parts.append(token)
    return "/".join(parts)


def _resolve_source_root(source: Mapping[str, Any]) -> Path:
    root = Path(str(source.get("path") or source.get("root_path") or "")).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"source is not available: {source.get('label') or root}")
    return root


def _resolve_entry(source: Mapping[str, Any], rel_path: Any = "") -> Path:
    root = _resolve_source_root(source)
    rel = _clean_rel(rel_path)
    target = (root / Path(*rel.split("/"))).resolve() if rel else root
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes the selected source") from exc
    return target


def _rel_from_path(source: Mapping[str, Any], path: Path) -> str:
    root = _resolve_source_root(source)
    target = path.resolve()
    try:
        rel = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes the selected source") from exc
    return "" if str(rel) == "." else rel.as_posix()


def _safe_name(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("name is required")
    if raw in {".", ".."} or "/" in raw or "\\" in raw or "\x00" in raw or ":" in raw:
        raise ValueError("name must be a single filesystem segment")
    return raw[:240]


def _human_size(value: int | None) -> str:
    if value is None:
        return ""
    size = float(max(0, int(value)))
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            text = f"{size:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
        size /= 1024
    return f"{int(value)} B"


def _modified_label(ts: float) -> str:
    return _now_iso(ts)


def _icon_for(path: Path, *, is_dir: bool, is_parent: bool = False) -> str:
    if is_parent:
        return "arrow-up-outline"
    if is_dir:
        return "folder-outline"
    suffix = path.suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return "image-outline"
    if suffix in _VIDEO_EXTENSIONS:
        return "film-outline"
    if suffix in _AUDIO_EXTENSIONS:
        return "musical-notes-outline"
    if suffix in _ARCHIVE_EXTENSIONS:
        return "archive-outline"
    if suffix in _TEXT_EXTENSIONS:
        return "document-text-outline"
    if suffix in _DOC_EXTENSIONS:
        return "document-attach-outline"
    return "document-outline"


def _language_for(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return {
        "cfg": "ini",
        "conf": "ini",
        "htm": "html",
        "md": "markdown",
        "ps1": "powershell",
        "sh": "shell",
        "yml": "yaml",
    }.get(suffix, suffix or "text")


def _can_preview(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in _TEXT_EXTENSIONS or suffix in _IMAGE_EXTENSIONS:
        return True
    mime = mimetypes.guess_type(path.name)[0] or ""
    return mime.startswith("text/") or mime in {"application/json", "application/xml"}


def _item_for(source: Mapping[str, Any], path: Path, *, is_parent: bool = False) -> dict[str, Any]:
    if is_parent:
        return {
            "id": "__parent__",
            "name": "..",
            "extension": "",
            "path": _rel_from_path(source, path),
            "kind": "parent",
            "is_dir": True,
            "is_file": False,
            "is_parent": True,
            "size": "",
            "size_bytes": None,
            "modified_at": None,
            "modified_label": "",
            "icon": _icon_for(path, is_dir=True, is_parent=True),
            "summary": "Parent folder",
            "can_expand": True,
            "can_preview": False,
            "can_download": False,
        }
    try:
        stat = path.stat()
    except OSError:
        stat = None
    is_dir = path.is_dir()
    size_bytes = None if is_dir or stat is None else int(stat.st_size)
    modified_at = float(stat.st_mtime) if stat is not None else 0.0
    suffix = "" if is_dir else path.suffix.lower().lstrip(".")
    kind = "folder" if is_dir else "file"
    summary_parts = [kind.capitalize()]
    if suffix:
        summary_parts.append(suffix.upper())
    if size_bytes is not None:
        summary_parts.append(_human_size(size_bytes))
    if modified_at:
        summary_parts.append(_modified_label(modified_at))
    rel = _rel_from_path(source, path)
    return {
        "id": rel or "__root__",
        "name": path.name or str(source.get("label") or "Root"),
        "extension": suffix,
        "path": rel,
        "kind": kind,
        "is_dir": is_dir,
        "is_file": path.is_file(),
        "is_parent": False,
        "size": _human_size(size_bytes),
        "size_bytes": size_bytes,
        "modified_at": modified_at or None,
        "modified_label": _modified_label(modified_at) if modified_at else "",
        "icon": _icon_for(path, is_dir=is_dir),
        "summary": " | ".join(summary_parts),
        "can_expand": is_dir,
        "can_preview": path.is_file() and _can_preview(path),
        "can_download": path.is_file(),
        "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }


def _list_dir(source: Mapping[str, Any], rel_path: Any, *, limit: int = _MAX_ITEMS_PER_FOLDER) -> list[dict[str, Any]]:
    target = _resolve_entry(source, rel_path)
    if not target.is_dir():
        raise NotADirectoryError(str(rel_path or ""))
    items: list[dict[str, Any]] = []
    current_rel = _clean_rel(rel_path)
    if current_rel:
        items.append(_item_for(source, target.parent, is_parent=True))
    children: list[Path] = []
    try:
        children = list(target.iterdir())
    except PermissionError:
        return items
    children.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
    for child in children[: max(0, int(limit))]:
        try:
            items.append(_item_for(source, child))
        except OSError:
            continue
    if len(children) > limit:
        items.append(
            {
                "id": "__truncated__",
                "name": f"{len(children) - limit} more items hidden",
                "extension": "",
                "path": current_rel,
                "kind": "status",
                "is_dir": False,
                "is_file": False,
                "size": "",
                "size_bytes": None,
                "modified_at": None,
                "modified_label": "",
                "icon": "ellipsis-horizontal-outline",
                "summary": f"Folder contains more than {limit} entries.",
                "can_expand": False,
                "can_preview": False,
                "can_download": False,
            }
        )
    return items


def _tree_children(source: Mapping[str, Any], rel_path: Any) -> list[dict[str, Any]]:
    target = _resolve_entry(source, rel_path)
    if not target.is_dir():
        return []
    out: list[dict[str, Any]] = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return out
    for child in children[:_MAX_TREE_CHILDREN]:
        try:
            item = _item_for(source, child)
        except OSError:
            continue
        out.append(
            {
                "id": item["id"],
                "name": item["name"],
                "path": item["path"],
                "kind": item["kind"],
                "is_dir": item["is_dir"],
                "icon": item["icon"],
                "summary": item["summary"],
                "has_children": item["is_dir"],
            }
        )
    return out


def _tree_view_node(source: Mapping[str, Any], tree: Mapping[str, Any], rel_path: str = "") -> dict[str, Any]:
    key = rel_path or "__root__"
    try:
        path = _resolve_entry(source, rel_path)
        title = str(source.get("label") or "Root") if not rel_path else path.name
        icon = "folder-outline" if path.is_dir() else _icon_for(path, is_dir=False)
    except Exception:
        title = str(source.get("label") or "Root")
        icon = "folder-outline"
    raw_children = tree.get(key) if isinstance(tree, Mapping) else []
    children: list[dict[str, Any]] = []
    if isinstance(raw_children, list):
        for child in raw_children:
            if not isinstance(child, Mapping):
                continue
            child_rel = _clean_rel(child.get("path") or "")
            if bool(child.get("is_dir")):
                children.append(_tree_view_node(source, tree, child_rel))
            else:
                children.append(
                    {
                        "id": str(child.get("id") or child_rel),
                        "name": str(child.get("name") or child_rel or "File"),
                        "title": str(child.get("name") or child_rel or "File"),
                        "path": child_rel,
                        "kind": "file",
                        "icon": str(child.get("icon") or "document-outline"),
                        "subtitle": str(child.get("summary") or ""),
                        "children": [],
                    }
                )
    return {
        "id": rel_path or "__root__",
        "name": title,
        "title": title,
        "path": rel_path,
        "kind": "folder",
        "is_dir": True,
        "icon": icon,
        "subtitle": "Local source" if not rel_path else "Folder",
        "children": children,
    }


def _selected_item(state: Mapping[str, Any], panel: str) -> dict[str, Any] | None:
    panel_state = dict((state.get("panels") or {}).get(panel) or {})
    selected = str(panel_state.get("selected_path") or "").strip()
    if not selected:
        return None
    source = _source_for_panel(state, panel)
    try:
        path = _resolve_entry(source, selected)
        if path.exists():
            return _item_for(source, path)
    except Exception:
        return None
    return None


def _panel_snapshot(state: Mapping[str, Any], panel: str) -> dict[str, Any]:
    source = _source_for_panel(state, panel)
    panel_state = dict((state.get("panels") or {}).get(panel) or {})
    rel = ""
    try:
        rel = _clean_rel(panel_state.get("path") or "")
        items = _list_dir(source, rel)
        error = ""
    except Exception as exc:
        rel = ""
        items = []
        error = str(exc)
    selected = _selected_item(state, panel)
    root_label = str(source.get("label") or "Local folder")
    path_label = f"{root_label}:/{rel}" if rel else f"{root_label}:/"
    tree = dict(panel_state.get("tree") or {})
    return {
        "id": panel,
        "active": str(state.get("active_panel") or "left") == panel,
        "source_id": str(source.get("id") or ""),
        "source": {
            "id": str(source.get("id") or ""),
            "kind": str(source.get("kind") or "local"),
            "label": root_label,
            "path": str(source.get("path") or ""),
            "description": str(source.get("description") or source.get("path") or ""),
            "connected": bool(source.get("connected", True)),
        },
        "path": rel,
        "path_label": path_label,
        "selected_path": str(panel_state.get("selected_path") or ""),
        "selected_item": selected,
        "items": items,
        "tree": tree,
        "tree_view": {"root": _tree_view_node(source, tree, "")},
        "error": error,
    }


def _source_options(state: Mapping[str, Any], webspace_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ws = _webspace_id(webspace_id)
    for item in state.get("sources") or []:
        if not isinstance(item, Mapping):
            continue
        out.append(
            {
                "id": str(item.get("id") or ""),
                "value": str(item.get("id") or ""),
                "webspace_id": ws,
                "label": str(item.get("label") or "Local folder"),
                "description": str(item.get("description") or item.get("path") or ""),
                "kind": str(item.get("kind") or "local"),
                "path": str(item.get("path") or ""),
                "connected": bool(item.get("connected", True)),
            }
        )
    return out


def _recent_link_items(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    link = state.get("last_link") if isinstance(state.get("last_link"), Mapping) else {}
    if not link or not str(link.get("url") or "").strip():
        return []
    return [dict(link)]


def _public_link_items() -> list[dict[str, Any]]:
    try:
        out: list[dict[str, Any]] = []
        for item in list_hub_public_links(ctx=get_ctx()):
            if not isinstance(item, Mapping):
                continue
            enriched = dict(item)
            public_token = str(enriched.get("public_token") or "").strip()
            zone_id = canonical_zone_id(enriched.get("zone")) or _current_zone_id()
            if public_token:
                app_view_url = _build_drive_app_view_url(public_token, zone_id)
                if not str(enriched.get("url") or "").strip():
                    enriched["url"] = app_view_url
                if not str(enriched.get("view_url") or "").strip():
                    enriched["view_url"] = str(enriched.get("url") or app_view_url)
                root_base = _root_public_base_url(zone_id)
                if not str(enriched.get("root_download_url") or "").strip():
                    enriched["root_download_url"] = build_root_public_content_url(root_base, public_token, download=True)
                if not str(enriched.get("list_url") or "").strip():
                    enriched["list_url"] = build_root_public_list_url(root_base, public_token)
                if not str(enriched.get("download_url") or "").strip():
                    enriched["download_url"] = str(enriched.get("view_url") or enriched.get("url") or "")
            out.append(enriched)
        return out
    except Exception:
        return []


def _snapshot(state: dict[str, Any], webspace_id: str) -> dict[str, Any]:
    ws = _webspace_id(webspace_id)
    sources = _source_options(state, ws)
    left_source = _panel_snapshot(state, "left")
    right_source = _panel_snapshot(state, "right")
    revision = _state_revision(state)
    return {
        "ok": True,
        "schema": "adaos_drive.snapshot.v1",
        "status": "ready",
        "receiver": _RECEIVER,
        "webspace_id": ws,
        "_stream_rev": revision,
        "_stream_require_revision": True,
        "sources": sources,
        "source_options": sources,
        "selectors": {
            "left_source": {"options": sources, "current": left_source["source_id"]},
            "right_source": {"options": sources, "current": right_source["source_id"]},
        },
        "active_panel": str(state.get("active_panel") or "left"),
        "panels": {
            "left": left_source,
            "right": right_source,
        },
        "preview": dict(state.get("preview") or _empty_preview()),
        "last_link": dict(state.get("last_link") or _empty_link()),
        "recent_links": {"items": _recent_link_items(state)},
        "public_links": {"items": _public_link_items()},
        "messages": list(state.get("messages") or [])[-10:],
        "sequence": revision,
        "updated_at": _now_iso(state.get("updated_at") or _now()),
    }


def _publish(state: dict[str, Any], webspace_id: str) -> dict[str, Any]:
    payload = _snapshot(state, webspace_id)
    try:
        stream_publish(_RECEIVER, payload, _meta={"webspace_id": _webspace_id(webspace_id)})
    except Exception:
        _LOG.exception("failed to publish adaos_drive snapshot")
    return payload


def _message(state: dict[str, Any], text: str, *, level: str = "info") -> None:
    state.setdefault("messages", [])
    state["messages"].append({"level": level, "text": str(text), "at": _now_iso()})
    state["messages"] = state["messages"][-10:]


def _save_and_publish(state: dict[str, Any], webspace_id: str, message: str | None = None) -> dict[str, Any]:
    if message:
        _message(state, message)
    _persist_state(webspace_id, state)
    return _publish(state, webspace_id)


def _panel_state(state: dict[str, Any], panel: str) -> dict[str, Any]:
    panels = state.setdefault("panels", {})
    if panel not in panels or not isinstance(panels.get(panel), Mapping):
        panels[panel] = {
            "id": panel,
            "source_id": state["sources"][0]["id"],
            "path": "",
            "selected_path": "",
            "selected_id": "",
            "sort": "name_asc",
            "tree": {},
        }
    return panels[panel]


def _selected_path_or_payload(state: Mapping[str, Any], panel: str, data: Mapping[str, Any]) -> str:
    explicit = data.get("path") or data.get("item_path") or data.get("selected_path")
    if explicit is not None and str(explicit).strip():
        return _clean_rel(explicit)
    panel_state = dict((state.get("panels") or {}).get(panel) or {})
    return _clean_rel(panel_state.get("selected_path") or "")


def _destination_name(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem or path.name
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem} ({uuid.uuid4().hex[:8]}){suffix}")


def _copy_any(src: Path, dst: Path) -> None:
    target = _destination_name(dst)
    if src.is_dir():
        shutil.copytree(src, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def _skills_root() -> Path:
    try:
        return Path(get_ctx().paths.skills_workspace_dir()).resolve()
    except Exception:
        return Path(__file__).resolve().parents[2]


def _config_text_attr(name: str) -> str:
    try:
        ctx = get_ctx()
        for source in (getattr(ctx, "settings", None), getattr(ctx, "config", None)):
            value = getattr(source, name, None) if source is not None else None
            if value:
                return str(value).strip()
    except Exception:
        pass
    try:
        conf = getattr(get_ctx(), "config", None)
        value = getattr(conf, name, None) if conf is not None else None
        if value:
            return str(value).strip()
    except Exception:
        pass
    try:
        value = getattr(load_config(), name, None)
        if value:
            return str(value).strip()
    except Exception:
        pass
    return ""


def _normalize_base_url(value: Any, *, default_scheme: str) -> str:
    token = str(value or "").strip().rstrip("/")
    if not token:
        return ""
    if "://" not in token:
        token = f"{default_scheme}://{token}"
    return token.replace("://0.0.0.0", "://127.0.0.1").replace("://[::]", "://127.0.0.1")


def _zone_id_from_url(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        host = str(parsed.hostname or "").strip().lower()
    except Exception:
        return None
    if host == "ru.api.inimatic.com":
        return "ru"
    if host == "api.inimatic.com":
        return "us"
    return None


def _current_zone_id() -> str:
    cfg = None
    try:
        cfg = load_config(ctx=get_ctx())
    except Exception:
        try:
            cfg = load_config()
        except Exception:
            cfg = None
    candidates = [
        os.getenv("ADAOS_ZONE_ID"),
        os.getenv("ZONE_ID"),
        _config_text_attr("zone_id"),
        getattr(cfg, "zone_id", None) if cfg is not None else None,
        _zone_id_from_url(getattr(getattr(cfg, "root_settings", None), "base_url", None) if cfg is not None else None),
        _zone_id_from_url(_config_text_attr("api_base")),
    ]
    for candidate in candidates:
        zone_id = canonical_zone_id(candidate)
        if zone_id:
            return zone_id
    return "lo"


def _current_subnet_id() -> str:
    cfg = None
    try:
        cfg = load_config(ctx=get_ctx())
    except Exception:
        try:
            cfg = load_config()
        except Exception:
            cfg = None
    for candidate in (
        os.getenv("ADAOS_SUBNET_ID"),
        os.getenv("ADAOS_HUB_ID"),
        getattr(cfg, "subnet_id", None) if cfg is not None else None,
        _config_text_attr("subnet_id"),
        _config_text_attr("default_hub"),
    ):
        token = str(candidate or "").strip()
        if token:
            return token
    return ""


def _current_node_id() -> str:
    cfg = None
    try:
        cfg = load_config(ctx=get_ctx())
    except Exception:
        try:
            cfg = load_config()
        except Exception:
            cfg = None
    for candidate in (
        os.getenv("ADAOS_NODE_ID"),
        getattr(cfg, "node_id", None) if cfg is not None else None,
        _config_text_attr("node_id"),
    ):
        token = str(candidate or "").strip()
        if token:
            return token
    return ""


def _public_app_base_url() -> str:
    candidates = [
        os.getenv("ADAOS_PUBLIC_APP_URL") or "",
        os.getenv("ADAOS_PUBLIC_APP_BASE") or "",
        os.getenv("ADAOS_APP_BASE_URL") or "",
        os.getenv("PUBLIC_ADAOS_BASE") or "",
        _config_text_attr("app_base"),
        _config_text_attr("public_app_base_url"),
        _DEFAULT_PUBLIC_DOWNLOAD_BASE_URL,
    ]
    for item in candidates:
        token = _normalize_base_url(item, default_scheme="https")
        if not token:
            continue
        try:
            host = str(urlsplit(token).hostname or "").strip().lower()
        except Exception:
            host = ""
        if host in {"app.inimatic.com", "inimatic.web.app", "inimatic.firebaseapp.com"}:
            return DEFAULT_PUBLIC_APP_BASE_URL
        return token
    return _DEFAULT_PUBLIC_DOWNLOAD_BASE_URL


def _root_public_base_url(zone_id: str) -> str:
    explicit = _normalize_base_url(os.getenv("ADAOS_DRIVE_ROOT_BASE_URL") or "", default_scheme="https")
    if explicit:
        return explicit
    raw_zone = str(zone_id or "").strip().lower()
    zone = canonical_zone_id(zone_id) or ("lo" if raw_zone == "lo" else "")
    if zone and zone != "lo":
        return zone_public_base_url(zone)
    for candidate in (
        _config_text_attr("api_base"),
        _config_text_attr("root_base_url"),
    ):
        token = _normalize_base_url(candidate, default_scheme="http" if "127.0.0.1" in str(candidate) else "https")
        if token:
            return token
    return "http://127.0.0.1:8777" if zone == "lo" else zone_public_base_url(zone)


def _root_token_value() -> str:
    for candidate in (
        _config_text_attr("root_token"),
        os.getenv("HUB_ROOT_TOKEN"),
        os.getenv("ADAOS_ROOT_TOKEN"),
        os.getenv("ROOT_TOKEN"),
        os.getenv("ADAOS_ROOT_OWNER_TOKEN"),
    ):
        token = str(candidate or "").strip()
        if token:
            return token
    return ""


def _owner_token_value() -> str:
    for candidate in (
        _config_text_attr("owner_token"),
        os.getenv("ADAOS_ROOT_OWNER_TOKEN"),
    ):
        token = str(candidate or "").strip()
        if token:
            return token
    return ""


def _bearer_token_value() -> str:
    for candidate in (
        _config_text_attr("bearer_token"),
        os.getenv("ADAOS_ROOT_BEARER_TOKEN"),
    ):
        token = str(candidate or "").strip()
        if token:
            return token
    return ""


def _root_registration_auth_headers() -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    owner_token = _owner_token_value()
    bearer_token = _bearer_token_value()
    root_token = _root_token_value()
    if owner_token:
        candidates.append({"X-Owner-Token": owner_token})
    if bearer_token:
        candidates.append({"Authorization": f"Bearer {bearer_token}"})
    if root_token:
        candidates.append({"X-Root-Token": root_token})
    if owner_token:
        candidates.append({"X-Root-Token": owner_token})
    seen: set[tuple[tuple[str, str], ...]] = set()
    out: list[dict[str, str]] = []
    for item in candidates:
        key = tuple(sorted(item.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _is_loopback_base_url(value: str) -> bool:
    try:
        host = str(urlsplit(value).hostname or "").strip().lower()
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _register_root_drive_link(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    auth_headers = _root_registration_auth_headers()
    if not auth_headers:
        return _root_registration_failure("root_token_missing", "Root registration token is not available in skill runtime.", retryable=True)
    api_base = str(payload.get("root_base_url") or "").strip().rstrip("/")
    if not api_base:
        return _root_registration_failure("root_base_url_missing", "Root public base URL is not available.", retryable=False)
    body = dict(payload)
    body.pop("root_base_url", None)
    last_failure: dict[str, Any] | None = None
    for auth_header in auth_headers:
        try:
            req = UrlRequest(
                f"{api_base}/v1/drive/public-links/register",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    **auth_header,
                },
                method="POST",
            )
            with urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
            return dict(data) if isinstance(data, Mapping) else None
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                detail = ""
            _LOG.warning("Root drive public link registration failed: HTTP %s %s", exc.code, detail or exc.reason)
            last_failure = _root_registration_failure(
                "root_registration_http_error",
                detail or str(exc),
                status=int(exc.code or 0),
                retryable=int(exc.code or 0) in {408, 409, 425, 429, 500, 502, 503, 504},
            )
            if int(exc.code or 0) in {401, 403}:
                continue
            return last_failure
        except Exception as exc:
            _LOG.warning("Root drive public link registration failed: %s", exc)
            return _root_registration_failure("root_registration_request_failed", str(exc), retryable=True)
    return last_failure or _root_registration_failure("root_registration_auth_failed", "Root registration auth was rejected.", status=401, retryable=True)


def _root_registration_failure(
    error: str,
    detail: str = "",
    *,
    status: int | None = None,
    retryable: bool = True,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "status": "pending_root_registration",
        "error": str(error or "root_registration_failed"),
        "detail": str(detail or "").strip(),
        "retryable": bool(retryable),
    }
    if status:
        out["http_status"] = int(status)
    return out


def _root_registration_ok(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("ok") is True


def _root_registration_error_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "root_registration_failed"
    return str(value.get("detail") or value.get("error") or "root_registration_failed").strip()


def _build_drive_app_download_url(public_token: str, zone_id: str) -> str:
    base_url = _public_app_base_url()
    helper = getattr(sdk_navigation, "drive_download_destination", None)
    if callable(helper):
        try:
            return sdk_navigation.build_url(helper(public_token, zone=zone_id), base_url=base_url)
        except Exception as exc:
            _LOG.warning("SDK drive download URL builder failed, using local fallback: %s", exc)
    query = urlencode(
        {
            "intent": "drive.download",
            "zone": str(zone_id or "").strip().lower() or "lo",
            "public_token": str(public_token or "").strip(),
        }
    )
    return f"{base_url.rstrip('/') or _DEFAULT_PUBLIC_DOWNLOAD_BASE_URL}/?{query}"


def _build_drive_app_view_url(public_token: str, zone_id: str) -> str:
    base_url = _public_app_base_url()
    helper = getattr(sdk_navigation, "drive_view_destination", None)
    if callable(helper):
        try:
            return sdk_navigation.build_url(helper(public_token, zone=zone_id), base_url=base_url)
        except Exception as exc:
            _LOG.warning("SDK drive view URL builder failed, using local fallback: %s", exc)
    query = urlencode(
        {
            "intent": "drive.view",
            "zone": str(zone_id or "").strip().lower() or "lo",
            "public_token": str(public_token or "").strip(),
        }
    )
    return f"{base_url.rstrip('/') or _DEFAULT_PUBLIC_DOWNLOAD_BASE_URL}/?{query}"


def _create_public_link(source: Mapping[str, Any], source_path: Path, *, download: bool = False) -> dict[str, Any]:
    if not source_path.exists() or not (source_path.is_file() or source_path.is_dir()):
        raise FileNotFoundError("selected item is not available")
    zone_id = _current_zone_id()
    subnet_id = _current_subnet_id()
    if not subnet_id:
        raise RuntimeError("subnet_id is not available for Root public link registration")
    root = _resolve_source_root(source)
    rel_path = _rel_from_path(source, source_path)
    resource_kind = "folder" if source_path.is_dir() else "file"
    capabilities = ["read", "list", "preview", "download"] if resource_kind == "folder" else ["read", "preview", "download"]
    public_token = issue_public_token()
    hub_token = issue_hub_token()
    ttl_seconds = int(os.getenv("ADAOS_DRIVE_PUBLIC_LINK_TTL_SECONDS") or 7 * 24 * 3600)
    hub_record = register_hub_public_link(
        public_token=public_token,
        hub_token=hub_token,
        source_root=root,
        rel_path=rel_path,
        source_id=str(source.get("id") or ""),
        source_label=str(source.get("label") or ""),
        subnet_id=subnet_id,
        node_id=_current_node_id(),
        zone=zone_id,
        ttl_seconds=ttl_seconds,
        capabilities=capabilities,
        ctx=get_ctx(),
    )
    root_base = _root_public_base_url(zone_id)
    root_content_url = build_root_public_content_url(root_base, public_token, download=False)
    root_download_url = build_root_public_content_url(root_base, public_token, download=True)
    root_list_url = build_root_public_list_url(root_base, public_token)
    app_view_url = _build_drive_app_view_url(public_token, zone_id)
    payload = {
        "public_token": public_token,
        "hub_token": hub_token,
        "subnet_id": subnet_id,
        "hub_id": subnet_id,
        "node_id": _current_node_id(),
        "skill": _SKILL_NAME,
        "zone": zone_id,
        "zone_id": zone_id,
        "grant_kind": "drive.files",
        "face_id": "adaos_drive.files.public",
        "resource_kind": resource_kind,
        "readonly": True,
        "capabilities": capabilities,
        "filename": source_path.name,
        "size_bytes": int(hub_record.get("size_bytes") or (source_path.stat().st_size if source_path.is_file() else 0)),
        "mime_type": str(
            hub_record.get("mime_type")
            or ("inode/directory" if source_path.is_dir() else mimetypes.guess_type(source_path.name)[0])
            or "application/octet-stream"
        ),
        "modified_at": hub_record.get("modified_at"),
        "expires_at": hub_record.get("expires_at"),
        "url": app_view_url,
        "view_url": app_view_url,
        "content_url": root_content_url,
        "download_url": app_view_url,
        "root_download_url": root_download_url,
        "list_url": root_list_url,
        "root_base_url": root_base,
        "metadata": {
            "source_id": str(source.get("id") or ""),
            "source_label": str(source.get("label") or ""),
            "rel_path": rel_path,
        },
    }
    root_result = _register_root_drive_link(payload)
    if not _root_registration_ok(root_result) and (zone_id == "lo" or _is_loopback_base_url(root_base)):
        local_payload = dict(payload)
        local_payload.pop("root_base_url", None)
        try:
            root_result = {"ok": True, "link": register_root_public_link(local_payload, ctx=get_ctx())}
        except Exception as exc:
            root_result = _root_registration_failure("local_root_registration_failed", str(exc), retryable=True)
    registration_ok = _root_registration_ok(root_result)
    if not registration_ok:
        _LOG.warning("Root public link registration pending: %s", _root_registration_error_text(root_result))
    return {
        "id": public_token,
        "public_token": public_token,
        "url": root_download_url if download and source_path.is_file() else app_view_url,
        "view_url": app_view_url,
        "content_url": root_content_url,
        "download_url": app_view_url,
        "root_download_url": root_download_url,
        "list_url": root_list_url,
        "open_url": root_download_url if download and source_path.is_file() else (app_view_url if download else ""),
        "root_registration": root_result,
        "registration_status": "registered" if registration_ok else "pending_root_registration",
        "registration_error": "" if registration_ok else _root_registration_error_text(root_result),
        "name": source_path.name,
        "resource_kind": resource_kind,
        "readonly": True,
        "capabilities": capabilities,
        "size_bytes": int(source_path.stat().st_size) if source_path.is_file() else 0,
        "mime": "inode/directory" if source_path.is_dir() else mimetypes.guess_type(source_path.name)[0] or "application/octet-stream",
        "zone": zone_id,
        "expires_at": hub_record.get("expires_at"),
    }


def _artifact_path(value: Mapping[str, Any]) -> Path:
    for key in ("path", "local_path", "stored_path"):
        raw = str(value.get(key) or "").strip()
        if raw:
            path = Path(raw).expanduser().resolve()
            if path.exists() and path.is_file():
                return path
    relative = str(value.get("relative_path") or "").strip()
    skill = str(value.get("skill") or _SKILL_NAME).strip() or _SKILL_NAME
    if relative:
        path = resolve_skill_file_path(skills_root=_skills_root(), skill_name=skill, relative_path=relative)
        if path.exists() and path.is_file():
            return path
    raise FileNotFoundError("uploaded artifact is not available")


def _preview_text(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    truncated = len(data) > _MAX_PREVIEW_BYTES
    raw = data[:_MAX_PREVIEW_BYTES]
    text = raw.decode("utf-8-sig", errors="replace")
    language = _language_for(path)
    if path.suffix.lower() == ".json":
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            language = "json"
        except Exception:
            pass
    if truncated:
        text += "\n\n[Preview truncated]"
    return text, language


def _set_preview_from_path(state: dict[str, Any], source: Mapping[str, Any], path: Path, *, panel: str) -> dict[str, Any]:
    item = _item_for(source, path)
    if not path.is_file():
        state["preview"] = {
            "mode": "metadata",
            "panel": panel,
            "title": item["name"],
            "content": json.dumps(item, indent=2, ensure_ascii=False),
            "language": "json",
            "item": item,
            "url": "",
            "download_url": "",
            "summary": item["summary"],
            "updated_at": _now_iso(),
        }
        return state["preview"]
    suffix = path.suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        link = _create_public_link(source, path)
        state["preview"] = {
            "mode": "image",
            "panel": panel,
            "title": item["name"],
            "content": "",
            "language": "text",
            "item": item,
            "url": link["view_url"],
            "download_url": link["download_url"],
            "summary": item["summary"],
            "updated_at": _now_iso(),
        }
        return state["preview"]
    if _can_preview(path):
        text, language = _preview_text(path)
        state["preview"] = {
            "mode": "text",
            "panel": panel,
            "title": item["name"],
            "content": text,
            "language": language,
            "item": item,
            "url": "",
            "download_url": "",
            "summary": item["summary"],
            "updated_at": _now_iso(),
        }
        return state["preview"]
    link = _create_public_link(source, path)
    state["preview"] = {
        "mode": "download",
        "panel": panel,
        "title": item["name"],
        "content": json.dumps(item, indent=2, ensure_ascii=False),
        "language": "json",
        "item": item,
        "url": link["view_url"],
        "download_url": link["download_url"],
        "summary": "Preview is not supported for this format. Use Open or Download.",
        "updated_at": _now_iso(),
    }
    return state["preview"]


@tool("get_snapshot")
def get_snapshot(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    return {"ok": True, "snapshot": _publish(state, ws)}


@tool("add_source")
def add_source(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    root = Path(str(data.get("path") or "").strip()).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError("local source path must exist and be a folder")
    label = _safe_label(data.get("label"), root.name or str(root))
    source = _source_payload(label, root)
    existing = [item for item in state.get("sources") or [] if str(item.get("id")) != source["id"]]
    state["sources"] = [*existing, source]
    panel = _panel_name(data.get("panel"), state)
    panel_state = _panel_state(state, panel)
    panel_state["source_id"] = source["id"]
    panel_state["path"] = ""
    panel_state["selected_path"] = ""
    state["active_panel"] = panel
    snapshot = _save_and_publish(state, ws, f"Added source {source['label']}.")
    return {"ok": True, "source": source, "snapshot": snapshot}


@tool("select_source")
def select_source(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    source_id = str(data.get("source_id") or data.get("value") or "").strip()
    if source_id not in _source_map(state):
        source_path = str(data.get("source_path") or data.get("path") or "").strip()
        if source_path:
            root = Path(source_path).expanduser().resolve()
            if root.exists() and root.is_dir():
                source = _source_payload(_safe_label(data.get("source_label"), root.name or str(root)), root)
                existing = [item for item in state.get("sources") or [] if str(item.get("id")) != source["id"]]
                state["sources"] = [*existing, source]
                source_id = source["id"]
        if source_id not in _source_map(state):
            raise ValueError("unknown source")
    panel_state = _panel_state(state, panel)
    panel_state["source_id"] = source_id
    panel_state["path"] = ""
    panel_state["selected_path"] = ""
    panel_state["selected_id"] = ""
    state["active_panel"] = panel
    snapshot = _save_and_publish(state, ws)
    return {"ok": True, "panel": panel, "source_id": source_id, "snapshot": snapshot}


@tool("set_active_panel")
def set_active_panel(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    state["active_panel"] = panel
    snapshot = _save_and_publish(state, ws)
    return {"ok": True, "active_panel": panel, "snapshot": snapshot}


@tool("select_item")
def select_item(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    panel_state = _panel_state(state, panel)
    rel = _clean_rel(data.get("path") or data.get("id") or "")
    if rel == "__parent__":
        rel = _clean_rel(panel_state.get("path") or "")
    source = _source_for_panel(state, panel)
    if rel:
        target = _resolve_entry(source, rel)
        if not target.exists():
            raise FileNotFoundError("selected item does not exist")
    panel_state["selected_path"] = rel
    panel_state["selected_id"] = rel
    state["active_panel"] = panel
    snapshot = _save_and_publish(state, ws)
    return {"ok": True, "panel": panel, "selected_path": rel, "snapshot": snapshot}


@tool("activate_item")
def activate_item(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    panel_state = _panel_state(state, panel)
    raw = data.get("path") or data.get("id") or ""
    if str(raw or "").strip() in {"__parent__", ".."}:
        current = _clean_rel(panel_state.get("path") or "")
        return open_folder({"panel": panel, "path": "/".join(current.split("/")[:-1]), "webspace_id": ws})
    rel = _clean_rel(raw)
    source = _source_for_panel(state, panel)
    target = _resolve_entry(source, rel)
    if target.is_dir():
        return open_folder({"panel": panel, "path": rel, "webspace_id": ws})
    return select_item({"panel": panel, "path": rel, "webspace_id": ws})


@tool("open_folder")
def open_folder(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    panel_state = _panel_state(state, panel)
    has_path = "path" in data
    rel = data.get("path")
    if str(rel or "").strip() in {"__parent__", ".."}:
        current = _clean_rel(panel_state.get("path") or "")
        rel = "/".join(current.split("/")[:-1])
    rel = _clean_rel((rel if has_path else panel_state.get("selected_path")) or "")
    source = _source_for_panel(state, panel)
    target = _resolve_entry(source, rel)
    if not target.is_dir():
        raise NotADirectoryError("selected item is not a folder")
    panel_state["path"] = rel
    panel_state["selected_path"] = ""
    panel_state["selected_id"] = ""
    state["active_panel"] = panel
    snapshot = _save_and_publish(state, ws)
    return {"ok": True, "panel": panel, "path": rel, "snapshot": snapshot}


@tool("navigate_up")
def navigate_up(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    panel_state = _panel_state(state, panel)
    current = _clean_rel(panel_state.get("path") or "")
    panel_state["path"] = "/".join(current.split("/")[:-1]) if current else ""
    panel_state["selected_path"] = ""
    panel_state["selected_id"] = ""
    state["active_panel"] = panel
    snapshot = _save_and_publish(state, ws)
    return {"ok": True, "panel": panel, "path": panel_state["path"], "snapshot": snapshot}


@tool("expand_tree")
def expand_tree(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    rel = _clean_rel(data.get("path") or "")
    source = _source_for_panel(state, panel)
    children = _tree_children(source, rel)
    panel_state = _panel_state(state, panel)
    tree = dict(panel_state.get("tree") or {})
    tree[rel or "__root__"] = children
    panel_state["tree"] = tree
    state["active_panel"] = panel
    snapshot = _save_and_publish(state, ws)
    return {"ok": True, "panel": panel, "path": rel, "children": children, "snapshot": snapshot}


@tool("open_selected")
def open_selected(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    selected = _selected_path_or_payload(state, panel, data)
    if not selected:
        raise ValueError("select an item first")
    source = _source_for_panel(state, panel)
    path = _resolve_entry(source, selected)
    if path.is_dir():
        return open_folder({"panel": panel, "path": selected, "webspace_id": ws})
    return preview_in_other_panel({"panel": panel, "path": selected, "webspace_id": ws})


@tool("rename_selected")
def rename_selected(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    selected = _selected_path_or_payload(state, panel, data)
    if not selected:
        raise ValueError("select an item first")
    new_name = _safe_name(data.get("new_name") or data.get("name"))
    source = _source_for_panel(state, panel)
    path = _resolve_entry(source, selected)
    if not path.exists():
        raise FileNotFoundError("selected item does not exist")
    target = path.with_name(new_name).resolve()
    root = _resolve_source_root(source)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("rename target escapes the selected source") from exc
    if target.exists():
        raise FileExistsError("target name already exists")
    path.rename(target)
    panel_state = _panel_state(state, panel)
    panel_state["selected_path"] = _rel_from_path(source, target)
    panel_state["selected_id"] = panel_state["selected_path"]
    state["active_panel"] = panel
    snapshot = _save_and_publish(state, ws, f"Renamed to {target.name}.")
    return {"ok": True, "panel": panel, "path": panel_state["selected_path"], "snapshot": snapshot}


@tool("copy_to_other_panel")
def copy_to_other_panel(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    from_panel = _panel_name(data.get("panel") or data.get("from_panel"), state)
    to_panel = _panel_name(data.get("to_panel") or _other_panel(from_panel), state)
    selected = _selected_path_or_payload(state, from_panel, data)
    if not selected:
        raise ValueError("select an item first")
    from_source = _source_for_panel(state, from_panel)
    to_source = _source_for_panel(state, to_panel)
    src = _resolve_entry(from_source, selected)
    if not src.exists():
        raise FileNotFoundError("selected item does not exist")
    dst_dir = _resolve_entry(to_source, _panel_state(state, to_panel).get("path") or "")
    if not dst_dir.is_dir():
        raise NotADirectoryError("target panel is not a folder")
    _copy_any(src, dst_dir / src.name)
    state["active_panel"] = from_panel
    snapshot = _save_and_publish(state, ws, f"Copied {src.name} to {to_panel} panel.")
    return {"ok": True, "from_panel": from_panel, "to_panel": to_panel, "snapshot": snapshot}


@tool("create_guest_link")
def create_guest_link(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    selected = _selected_path_or_payload(state, panel, data)
    if not selected:
        raise ValueError("select an item first")
    source = _source_for_panel(state, panel)
    path = _resolve_entry(source, selected)
    link = _create_public_link(source, path, download=bool(data.get("download")))
    item = _item_for(source, path)
    state["last_link"] = {
        "id": link["id"],
        "public_token": link.get("public_token") or link["id"],
        "item": item,
        "url": link["url"],
        "view_url": link["view_url"],
        "download_url": link["download_url"],
        "root_download_url": link.get("root_download_url", ""),
        "list_url": link.get("list_url", ""),
        "open_url": link.get("open_url", ""),
        "registration_status": link.get("registration_status", ""),
        "registration_error": link.get("registration_error", ""),
        "zone": link.get("zone", ""),
        "expires_at": link.get("expires_at"),
        "resource_kind": link.get("resource_kind", item.get("kind", "")),
        "readonly": True,
        "capabilities": list(link.get("capabilities") or []),
        "label": item["name"],
        "summary": (
            f"{item['name']} | {item.get('kind', 'item')} | {_human_size(link['size_bytes']) if item.get('is_file') else 'readonly public access'}"
            if link.get("registration_status") == "registered"
            else f"{item['name']} | Root registration pending"
        ),
        "created_at": _now_iso(),
    }
    state["active_panel"] = panel
    message = (
        f"Created public readonly link for {path.name}."
        if link.get("registration_status") == "registered"
        else f"Created local link token for {path.name}; Root registration is pending."
    )
    snapshot = _save_and_publish(state, ws, message)
    return {"ok": True, "link": state["last_link"], "snapshot": snapshot}


@tool("download_selected")
def download_selected(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    data["download"] = True
    return create_guest_link(data)


@tool("list_public_links")
def list_public_links(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "links": _public_link_items()}


@tool("revoke_public_link")
def revoke_public_link(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    token = str(data.get("public_token") or data.get("id") or "").strip()
    if not token:
        raise ValueError("public_token is required")
    revoked = revoke_hub_public_link(token, ctx=get_ctx())
    root_base = _root_public_base_url(_current_zone_id())
    root_result = _root_registration_failure("root_revoke_not_attempted", retryable=True)
    auth_headers = _root_registration_auth_headers()
    for auth_header in auth_headers:
        try:
            req = UrlRequest(
                f"{root_base.rstrip('/')}/v1/drive/public-links/{token}/revoke",
                data=b"{}",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    **auth_header,
                },
                method="POST",
            )
            with urlopen(req, timeout=3.0) as resp:
                raw = json.loads(resp.read().decode("utf-8") or "{}")
            root_result = dict(raw) if isinstance(raw, Mapping) else {"ok": False, "error": "root_revoke_invalid_response"}
            break
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                detail = ""
            root_result = _root_registration_failure("root_revoke_http_error", detail or str(exc), status=int(exc.code or 0))
            if int(exc.code or 0) not in {401, 403}:
                break
        except Exception as exc:
            root_result = _root_registration_failure("root_revoke_request_failed", str(exc))
            break
    return {
        "ok": True,
        "link": revoked,
        "root_revoke": root_result,
    }


@tool("preview_in_other_panel")
def preview_in_other_panel(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    selected = _selected_path_or_payload(state, panel, data)
    if not selected:
        raise ValueError("select an item first")
    source = _source_for_panel(state, panel)
    path = _resolve_entry(source, selected)
    target_panel = _panel_name(data.get("preview_panel") or _other_panel(panel), state)
    preview = _set_preview_from_path(state, source, path, panel=target_panel)
    state["active_panel"] = panel
    snapshot = _save_and_publish(state, ws, f"Previewed {path.name}.")
    return {"ok": True, "preview": preview, "snapshot": snapshot}


@tool("upload_to_panel")
def upload_to_panel(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    artifact = data.get("artifact_ref") if isinstance(data.get("artifact_ref"), Mapping) else {}
    if not artifact and isinstance(data.get("file"), Mapping):
        artifact = data.get("file")
    if not isinstance(artifact, Mapping):
        raise ValueError("artifact_ref is required")
    src = _artifact_path(artifact)
    source = _source_for_panel(state, panel)
    dst_dir = _resolve_entry(source, _panel_state(state, panel).get("path") or "")
    if not dst_dir.is_dir():
        raise NotADirectoryError("current panel path is not a folder")
    requested_name = data.get("filename") or data.get("name") or artifact.get("name") or src.name
    target_name = _safe_name(requested_name)
    target = _destination_name(dst_dir / target_name)
    shutil.copy2(src, target)
    panel_state = _panel_state(state, panel)
    panel_state["selected_path"] = _rel_from_path(source, target)
    panel_state["selected_id"] = panel_state["selected_path"]
    state["active_panel"] = panel
    snapshot = _save_and_publish(state, ws, f"Uploaded {target.name}.")
    return {"ok": True, "panel": panel, "path": panel_state["selected_path"], "snapshot": snapshot}


@tool("make_folder")
def make_folder(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = _panel_name(data.get("panel"), state)
    name = _safe_name(data.get("name") or data.get("folder_name") or "New folder")
    source = _source_for_panel(state, panel)
    dst_dir = _resolve_entry(source, _panel_state(state, panel).get("path") or "")
    target = _destination_name(dst_dir / name)
    target.mkdir(parents=False, exist_ok=False)
    panel_state = _panel_state(state, panel)
    panel_state["selected_path"] = _rel_from_path(source, target)
    panel_state["selected_id"] = panel_state["selected_path"]
    state["active_panel"] = panel
    snapshot = _save_and_publish(state, ws, f"Created folder {target.name}.")
    return {"ok": True, "panel": panel, "path": panel_state["selected_path"], "snapshot": snapshot}


@tool("refresh")
def refresh(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    panel = data.get("panel")
    if panel:
        state["active_panel"] = _panel_name(panel, state)
    snapshot = _save_and_publish(state, ws)
    return {"ok": True, "snapshot": snapshot}


@tool("persist_state")
def persist_state(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    state = _load_state(ws)
    _persist_state(ws, state)
    return {"ok": True}


@tool("rehydrate")
def rehydrate(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    current = _STATE_BY_WEBSPACE.get(ws)
    loaded = _load_persisted_state(ws)
    if current is not None and _state_revision(current) > _state_revision(loaded):
        state = current
        _mem_set(_state_key(ws), state)
    else:
        state = loaded
        _STATE_BY_WEBSPACE[ws] = state
    return {"ok": True, "snapshot": _publish(state, ws)}


@tool("reset_drive")
def reset_drive(evt: Any = None, **kwargs: Any) -> dict[str, Any]:
    data = _payload(evt, **kwargs)
    ws = _webspace_id(data.get("webspace_id"))
    root = data.get("root")
    if root:
        source = _source_payload("Test source", Path(str(root)).expanduser().resolve())
        state = _default_state()
        state["sources"] = [source]
        for panel in ("left", "right"):
            state["panels"][panel]["source_id"] = source["id"]
    else:
        state = _default_state()
    _STATE_BY_WEBSPACE[ws] = state
    snapshot = _save_and_publish(state, ws)
    return {"ok": True, "snapshot": snapshot}


@subscribe("webio.stream.snapshot.requested")
def on_stream_snapshot_requested(evt: Any = None) -> dict[str, Any] | None:
    data = _payload(evt)
    receiver = str(data.get("receiver") or data.get("id") or "").strip()
    if receiver and receiver != _RECEIVER:
        return None
    ws = _webspace_id(data.get("webspace_id") or data.get("room") or data.get("webspace"))
    state = _load_state(ws)
    return {"ok": True, "snapshot": _publish(state, ws)}


@subscribe("webio.stream.subscription.changed")
def on_stream_subscription_changed(evt: Any = None) -> dict[str, Any] | None:
    data = _payload(evt)
    receiver = str(data.get("receiver") or data.get("id") or "").strip()
    subscribed = data.get("subscribed")
    if receiver and receiver != _RECEIVER:
        return None
    if subscribed is False:
        return None
    ws = _webspace_id(data.get("webspace_id") or data.get("room") or data.get("webspace"))
    state = _load_state(ws)
    return {"ok": True, "snapshot": _publish(state, ws)}
