from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
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
_SESSION_RECEIVER = "slideshow_skill.session"
_COMMAND_RECEIVER = "slideshow_skill.command"
_SUPPORTED = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
_ENDPOINT_SIZE = (1280, 800)
_WIDGET_SIZE = (420, 260)
_MAX_SCAN = 2000
_MAX_CONTROL_SCAN = 240
_MAX_ENDPOINT_CURRENT = 10
_MAX_ENDPOINT_FAVORITES = 20


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _source_dir(source_dir: str | None = None) -> Path:
    token = _text(source_dir)
    return Path(token or os.environ.get("SLIDESHOW_SOURCE_DIR") or r"C:\Users\Zver\Pictures")


def _state_dir() -> Path:
    base = Path(os.environ.get("ADAOS_STATE_DIR") or Path.home() / ".adaos" / "state") / "slideshow_skill"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _cache_dir() -> Path:
    base = Path(os.environ.get("SLIDESHOW_CACHE_DIR") or _state_dir() / "thumbs")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _state_path() -> Path:
    return _state_dir() / "state.json"


def _default_state() -> dict[str, Any]:
    return {
        "selected_codes": [],
        "sync": True,
        "mode": "sequential",
        "scope": "all",
        "fullscreen": True,
        "current_index": 0,
        "favorites": [],
        "source_dir": str(_source_dir()),
        "last_event_by_code": {},
    }


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_state()
    state = _default_state()
    if isinstance(data, Mapping):
        state.update(dict(data))
    state["selected_codes"] = _unique_texts(state.get("selected_codes"))
    state["favorites"] = _unique_texts(state.get("favorites"))
    state["sync"] = bool(state.get("sync", True))
    state["fullscreen"] = bool(state.get("fullscreen", True))
    if _text(state.get("mode")) not in {"sequential", "random"}:
        state["mode"] = "sequential"
    if _text(state.get("scope")) not in {"all", "favorites"}:
        state["scope"] = "all"
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
    _state_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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


def _photo_files(source_dir: str | None = None, limit: int | None = None) -> list[Path]:
    root = _source_dir(source_dir)
    if not root.exists():
        return []
    max_items = min(max(1, int(limit or _MAX_SCAN)), _MAX_SCAN)
    files: list[Path] = []
    for current, _dirs, names in os.walk(str(root)):
        for name in names:
            path = Path(current) / name
            if path.suffix.lower() not in _SUPPORTED:
                continue
            files.append(path)
            if len(files) >= max_items:
                break
        if len(files) >= max_items:
            break
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:24]


def _thumbnail(path: Path, size: tuple[int, int], label: str) -> tuple[Path, bool]:
    cache_path = _cache_dir() / f"{_fingerprint(path)}-{label}.jpg"
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
    ref = f"content:sha256:{_fingerprint(path)}"
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
    ref = f"content:sha256:{_fingerprint(path)}"
    return {
        "id": ref,
        "content_ref": ref,
        "title": path.name,
        "source_name": path.name,
        "source_path": str(path),
        "favorite": ref in set(_load_state().get("favorites") or []),
        "size": size,
        "size_label": f"{size // 1024} KB" if size >= 1024 else f"{size} B",
        "modified_at": modified,
        "modified": modified[:19].replace("T", " "),
    }


def _selected_photos(files: list[Path], state: Mapping[str, Any]) -> list[Path]:
    favorites = set(_unique_texts(state.get("favorites")))
    if _text(state.get("scope")) != "favorites":
        return files
    selected = [path for path in files if f"content:sha256:{_fingerprint(path)}" in favorites]
    return selected or files


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
    favorites = set(_unique_texts(state.get("favorites")))
    fav_paths = [
        path
        for path in files
        if f"content:sha256:{_fingerprint(path)}" in favorites
    ][: _MAX_ENDPOINT_FAVORITES]
    by_ref: dict[str, Path] = {}
    for path in [*current, *fav_paths]:
        by_ref[f"content:sha256:{_fingerprint(path)}"] = path
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
    ref = f"content:sha256:{_fingerprint(current)}"
    favorites = set(_unique_texts(state.get("favorites")))
    if ref in favorites:
        favorites.remove(ref)
    else:
        favorites.add(ref)
    state["favorites"] = sorted(favorites)
    return state


def _toggle_favorite_ref(state: dict[str, Any], ref: str) -> dict[str, Any]:
    token = _text(ref)
    if not token:
        return state
    favorites = set(_unique_texts(state.get("favorites")))
    if token in favorites:
        favorites.remove(token)
    else:
        favorites.add(token)
    state["favorites"] = sorted(favorites)
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
        content_ref = f"content:sha256:{_fingerprint(current)}"
    selected_codes = _unique_texts(state.get("selected_codes"))
    header = ", ".join(selected_codes) if selected_codes else "No endpoint"
    return {
        "ok": bool(current),
        "title": title,
        "subtitle": f"{header} | {state.get('mode')} | {state.get('scope')}",
        "value": title,
        "label": header,
        "description": f"{len(files)} photos, {len(_unique_texts(state.get('favorites')))} favorites",
        "image": image,
        "frame": {"label": f"{(int(state.get('current_index') or 0) + 1) if files else 0}/{len(_selected_photos(files, state))}"},
        "status": {"label": "sync" if state.get("sync") else "independent", "color": "success" if state.get("sync") else "warning"},
        "content_ref": content_ref,
        "selected_codes": selected_codes,
        "sync": bool(state.get("sync")),
        "mode": state.get("mode"),
        "scope": state.get("scope"),
        "fullscreen": bool(state.get("fullscreen")),
        "buttons": [
            {"id": "prev", "label": "Prev"},
            {"id": "next", "label": "Next"},
            {"id": "fav", "label": "Fav"},
        ],
        "last_command": dict(last_command or {}),
        "updated_at": _now(),
    }


def _publish(receiver: str, payload: Mapping[str, Any], webspace_id: str | None = None) -> None:
    stream_publish(receiver, dict(payload), _meta={"webspace_id": _text(webspace_id) or default_webspace_id()})


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
        "sync": bool(state.get("sync")),
        "mode": state.get("mode"),
        "scope": state.get("scope"),
        "fullscreen": bool(state.get("fullscreen")),
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


def _build_command(pair_code: str, items: list[dict[str, Any]], state: Mapping[str, Any]) -> dict[str, Any]:
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
    if not selected_codes:
        device = _select_device(devices)
        selected_codes = [_text(device.get("code"))] if device else []
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
        command = _build_command(pair_code, items, state)
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


def _apply_root_events(state: dict[str, Any], devices: list[Mapping[str, Any]], files: list[Path]) -> dict[str, Any]:
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
    return state


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
    files = _photo_files(state.get("source_dir"), limit)
    payload = {
        "ok": True,
        "source_dir": str(_source_dir(state.get("source_dir"))),
        "count": len(files),
        "items": [_preview_item(p) for p in files],
        "updated_at": _now(),
    }
    _publish(_PREVIEW_RECEIVER, payload, webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, _photo_files(state.get("source_dir"), _MAX_CONTROL_SCAN)), webspace_id)
    return payload


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
    _publish(_SESSION_RECEIVER, _session_payload(state, _photo_files(state.get("source_dir"), _MAX_CONTROL_SCAN)), webspace_id)
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
    _publish(_SESSION_RECEIVER, _session_payload(state, _photo_files(state.get("source_dir"), _MAX_CONTROL_SCAN)), webspace_id)
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
    state = _save_state(state)
    files = _photo_files(state.get("source_dir"), min(_MAX_CONTROL_SCAN, max(limit, _MAX_ENDPOINT_CURRENT + _MAX_ENDPOINT_FAVORITES)))
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
    files = _photo_files(state.get("source_dir"), _MAX_CONTROL_SCAN)
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
    elif token == "start":
        pass
    else:
        return {"ok": False, "error": "unknown_action", "action": action}
    state = _save_state(state)
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
    files = _photo_files(state.get("source_dir"), _MAX_CONTROL_SCAN)
    state = _apply_root_events(state, devices, files)
    state = _save_state(state)
    endpoint_payload = _endpoint_payload(devices, state)
    _publish(_ENDPOINTS_RECEIVER, endpoint_payload, webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, files), webspace_id)
    return endpoint_payload


def _event_payload(evt: Any) -> Mapping[str, Any]:
    payload = getattr(evt, "payload", evt)
    return payload if isinstance(payload, Mapping) else {}


def _matches_receiver(payload: Mapping[str, Any]) -> bool:
    receiver = _text(payload.get("receiver"))
    return receiver in {_ENDPOINTS_RECEIVER, _PREVIEW_RECEIVER, _SESSION_RECEIVER, _COMMAND_RECEIVER, "slideshow_skill.*"}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = _event_payload(evt)
    if not _matches_receiver(payload):
        return
    webspace_id = _text(payload.get("webspace_id") or payload.get("workspace_id")) or default_webspace_id()
    state = _load_state()
    files = _photo_files(state.get("source_dir"), _MAX_CONTROL_SCAN)
    devices = _load_devices()
    _publish(_ENDPOINTS_RECEIVER, _endpoint_payload(devices, state), webspace_id)
    _publish(_SESSION_RECEIVER, _session_payload(state, files), webspace_id)


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = _event_payload(evt)
    if _matches_receiver(payload):
        on_webio_stream_snapshot_requested(evt)
