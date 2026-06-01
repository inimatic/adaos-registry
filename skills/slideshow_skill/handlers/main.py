from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import tool
from adaos.sdk.io import stream_publish
from PIL import Image, ImageOps

try:
    from adaos.services.yjs.webspace import default_webspace_id
except Exception:  # pragma: no cover
    def default_webspace_id() -> str:
        return "default"


_RECEIVER = "slideshow_skill.state"
_SUPPORTED = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
_MAX_SIZE = (1280, 800)


def _root_base() -> str:
    raw = (
        os.environ.get("ADAOS_ROOT_API_BASE")
        or os.environ.get("PUBLIC_ROOT_BASE")
        or os.environ.get("ROOT_API_BASE")
        or "https://ru.api.inimatic.com"
    )
    return str(raw).strip().rstrip("/")


def _source_dir(source_dir: str | None = None) -> Path:
    token = str(source_dir or "").strip()
    return Path(token or os.environ.get("SLIDESHOW_SOURCE_DIR") or r"C:\Users\Zver\Pictures")


def _cache_dir() -> Path:
    raw = os.environ.get("SLIDESHOW_CACHE_DIR")
    base = Path(raw) if raw else Path(os.environ.get("ADAOS_STATE_DIR") or Path.home() / ".adaos" / "state") / "slideshow_skill" / "thumbs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _node_id() -> str:
    return os.environ.get("ADAOS_NODE_ID") or os.environ.get("NODE_ID") or f"node:{socket.gethostname().lower()}"


def _owner() -> dict[str, str]:
    node_id = _node_id()
    return {"node_id": node_id, "skill_id": "slideshow_skill", "target": f"{node_id}:slideshow_skill"}


def _request_json(method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    url = f"{_root_base()}{path}"
    body = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method.upper())
    req.add_header("accept", "application/json")
    if body is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"http_{exc.code}", "detail": detail}


def _photo_files(limit: int = 20, source_dir: str | None = None) -> list[Path]:
    root = _source_dir(source_dir)
    if not root.exists():
        return []
    files = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in _SUPPORTED]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[: max(1, int(limit or 1))]


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:24]


def _thumbnail(path: Path) -> tuple[Path, bool]:
    cache_path = _cache_dir() / f"{_fingerprint(path)}.jpg"
    if cache_path.exists():
        return cache_path, True
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        if getattr(image, "is_animated", False):
            image.seek(0)
        image.thumbnail(_MAX_SIZE, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", image.size, "black")
        if image.mode in {"RGBA", "LA"}:
            canvas.paste(image, mask=image.getchannel("A"))
        else:
            canvas.paste(image.convert("RGB"))
        canvas.save(cache_path, "JPEG", quality=78, optimize=True)
    return cache_path, False


def _content_item(path: Path) -> dict[str, Any]:
    thumb, cached = _thumbnail(path)
    data = base64.b64encode(thumb.read_bytes()).decode("ascii")
    return {
        "content_ref": f"content:sha256:{_fingerprint(path)}",
        "source_path": str(path),
        "source_name": path.name,
        "title": path.stem,
        "mime": "image/jpeg",
        "cached": cached,
        "thumbnail_path": str(thumb),
        "data_uri": f"data:image/jpeg;base64,{data}",
    }


def _load_devices() -> list[dict[str, Any]]:
    res = _request_json("GET", "/v1/redevice/devices")
    devices = res.get("devices") if isinstance(res, Mapping) else None
    return [dict(item) for item in devices if isinstance(item, Mapping)] if isinstance(devices, list) else []


def _age_seconds(value: Any) -> int | None:
    try:
        ts = float(value or 0)
    except Exception:
        return None
    if ts <= 0:
        return None
    return max(0, int(datetime.now(tz=timezone.utc).timestamp() - ts))


def _online_state(item: Mapping[str, Any]) -> str:
    sec = _age_seconds(item.get("last_seen_at"))
    if sec is None:
        return "unknown"
    if sec < 60:
        return "online"
    if sec < 5 * 60:
        return "stale"
    return "offline"


def _compact_device(item: Mapping[str, Any]) -> dict[str, Any]:
    policy = item.get("endpoint_policy") if isinstance(item.get("endpoint_policy"), Mapping) else {}
    state = str(item.get("state") or "-")
    code = str(item.get("code") or "")
    endpoint_id = str(item.get("endpoint_id") or "")
    label = str(item.get("device_label") or endpoint_id or code)
    seen = _age_seconds(item.get("last_seen_at"))
    return {
        "id": code or endpoint_id,
        "code": code,
        "title": label,
        "state": state,
        "online_state": _online_state(item),
        "last_seen": "-" if seen is None else f"{seen}s" if seen < 60 else f"{seen // 60}m {seen % 60}s",
        "zone_id": str(item.get("zone_id") or "-"),
        "trust_level": str(policy.get("trust_level") or "limited"),
        "endpoint_id": endpoint_id,
        "selectable": bool(code and state in {"approved", "consumed"}),
    }


def _select_device(code: str | None = None) -> dict[str, Any] | None:
    devices = _load_devices()
    if code:
        for item in devices:
            if str(item.get("code") or "") == str(code):
                return item
        return None
    ranked = sorted(
        devices,
        key=lambda item: (
            0 if _online_state(item) == "online" else 1 if _online_state(item) == "stale" else 2 if _online_state(item) == "unknown" else 3,
            0 if str(item.get("state") or "") in {"approved", "consumed"} else 1,
        ),
    )
    for item in ranked:
        if str(item.get("state") or "") in {"approved", "consumed"}:
            return item
    for item in devices:
        if str(item.get("state") or "") in {"approved", "consumed"}:
            return item
    return devices[0] if devices else None


def _publish(payload: Mapping[str, Any], webspace_id: str | None = None) -> None:
    stream_publish(_RECEIVER, dict(payload), _meta={"webspace_id": str(webspace_id or default_webspace_id())})


@tool
def list_slideshow_photos(
    limit: int = 10,
    source_dir: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    root = _source_dir(source_dir)
    files = _photo_files(limit, source_dir)
    payload = {
        "ok": True,
        "source_dir": str(root),
        "count": len(files),
        "items": [{"path": str(p), "name": p.name, "size": p.stat().st_size} for p in files],
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    _publish(payload, webspace_id)
    return payload


@tool
def start_redevice_slideshow(
    code: str | None = None,
    limit: int = 5,
    webspace_id: str | None = None,
    source_dir: str | None = None,
) -> dict[str, Any]:
    device = _select_device(code)
    if not device:
        return {"ok": False, "error": "no_redevice_endpoint"}
    pair_code = str(device.get("code") or "")
    if not pair_code:
        return {"ok": False, "error": "device_code_missing"}
    root = _source_dir(source_dir)
    files = _photo_files(limit, source_dir)
    if not files:
        return {"ok": False, "error": "no_supported_photos", "source_dir": str(root)}
    items = [_content_item(path) for path in files]
    command_id = "cmd:slideshow:" + hashlib.sha256(f"{pair_code}:{datetime.now(tz=timezone.utc).isoformat()}".encode("utf-8")).hexdigest()[:16]
    owner = _owner()
    command = {
        "command_id": command_id,
        "type": "display.render_surface",
        "owner": owner,
        "binding": {
            "binding_id": f"bind:slideshow:{pair_code}",
            "role": "slideshow_frame",
            "owner": owner,
            "events": {
                "next": {"target": owner["target"], "handler": "slideshow.next"},
                "favorite_toggle": {"target": owner["target"], "handler": "slideshow.favorite_toggle"},
                "hide_item": {"target": owner["target"], "handler": "slideshow.hide_item"},
            },
        },
        "payload": {
            "surface_ref": "slideshow.viewer",
            "surface_id": f"surface:slideshow:{command_id.split(':')[-1]}",
            "items": items,
            "controls": {
                "hardware": {"volume_up": "next", "volume_down": "favorite_toggle"},
                "touch": {"tap": "favorite_toggle"},
            },
        },
    }
    res = _request_json("POST", f"/v1/redevice/devices/{urllib.parse.quote(pair_code, safe='')}/commands", {"command": command})
    payload = {
        "ok": bool(res.get("ok")),
        "device": {"code": pair_code, "endpoint_id": device.get("endpoint_id"), "state": device.get("state")},
        "command_id": command_id,
        "source_dir": str(root),
        "items": [{"source_name": item["source_name"], "thumbnail_path": item["thumbnail_path"], "cached": item["cached"]} for item in items],
        "result": res,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    _publish(payload, webspace_id)
    return payload


@tool
def refresh_redevice_slideshow_state(
    code: str | None = None,
    webspace_id: str | None = None,
    source_dir: str | None = None,
) -> dict[str, Any]:
    devices = _load_devices()
    device = _select_device(code)
    payload = {
        "ok": device is not None,
        "device": device,
        "selected_device_code": str(device.get("code") or "") if device else "",
        "items": [_compact_device(item) for item in devices],
        "owner": _owner(),
        "source_dir": str(_source_dir(source_dir)),
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    _publish(payload, webspace_id)
    return payload
