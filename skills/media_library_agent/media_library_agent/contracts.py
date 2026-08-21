from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "adaos.media_library_agent.v1"
ROOT_SCHEMA = "adaos.media_library.root.v1"
JOB_SCHEMA = "adaos.media_library.scan_job.v1"
DELTA_SCHEMA = "adaos.media_library.source_delta.v1"
PROGRESS_SCHEMA = "adaos.media_library.scan_progress.v1"
RENDITION_JOB_SCHEMA = "adaos.media_library.rendition_job.v1"
RENDITION_PLAN_SCHEMA = "adaos.media_library.rendition_plan.v1"
ARTWORK_PLAN_SCHEMA = "adaos.media_library.artwork_plan.v1"

VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".ogv", ".ts", ".m2ts"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".flac", ".m4a", ".aac", ".opus", ".ogg", ".wma", ".aiff", ".ape", ".mka"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif"})


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def text(value: Any) -> str:
    return str(value or "").strip()


def stable_id(prefix: str, *parts: Any, size: int = 24) -> str:
    payload = "\0".join(text(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8', errors='replace')).hexdigest()[:size]}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def json_loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def media_kind(path: Path, mime_type: str = "") -> str:
    mime = text(mime_type).lower()
    if mime.startswith("video/") or path.suffix.lower() in VIDEO_EXTENSIONS:
        return "video"
    if mime.startswith("audio/") or path.suffix.lower() in AUDIO_EXTENSIONS:
        return "audio"
    if mime.startswith("image/") or path.suffix.lower() in IMAGE_EXTENSIONS:
        return "image"
    return "other"


def encode_cursor(sequence: int) -> str:
    raw = json_dumps({"v": 1, "seq": max(0, int(sequence))}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: Any) -> int:
    token = text(value)
    if not token:
        return 0
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("v") != 1:
            raise ValueError("unsupported cursor")
        return max(0, int(payload["seq"]))
    except Exception as exc:
        raise ValueError("invalid_media_library_cursor") from exc


def fingerprint(path: Path, *, relative_path: str, stat: Any) -> str:
    return hashlib.sha256(
        json_dumps(
            {
                "relative_path": relative_path.replace("\\", "/"),
                "size_bytes": int(stat.st_size),
                "modified_ns": int(stat.st_mtime_ns),
                "inode": int(getattr(stat, "st_ino", 0) or 0),
            }
        ).encode("utf-8")
    ).hexdigest()


def folder_segments(relative_path: str) -> list[str]:
    parts = Path(relative_path).parent.parts
    result: list[str] = []
    for part in parts:
        normalized = " ".join(part.replace("_", " ").replace(".", " ").replace("-", " ").split())
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def compact_error(code: str, *, detail: str = "", retryable: bool = False, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "schema": SCHEMA_VERSION,
        "error": text(code) or "media_library_agent_error",
        "retryable": bool(retryable),
    }
    if detail:
        payload["detail"] = detail[:2000]
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def require_mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return dict(value)
