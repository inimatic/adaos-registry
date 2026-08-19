from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import RENDITION_PLAN_SCHEMA, text


CancelCallback = Callable[[], bool]


def _tokens(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {text(item).lower() for item in value if text(item)}


def rendition_plan(
    source: Mapping[str, Any],
    *,
    endpoint_capabilities: Mapping[str, Any] | None = None,
    profile: str = "browser-mp4-v1",
) -> dict[str, Any]:
    capabilities = dict(endpoint_capabilities or {})
    metadata = dict(source.get("metadata") or {})
    technical = (
        dict(metadata.get("technical") or {})
        if isinstance(metadata.get("technical"), Mapping)
        else {}
    )
    kind = text(source.get("media_kind")).lower()
    mime_type = text(source.get("mime_type")).lower()
    codec = text(technical.get("codec")).lower()
    codecs = _tokens(capabilities.get("codecs"))
    mime_types = _tokens(capabilities.get("mime_types"))
    containers = _tokens(capabilities.get("containers"))
    container = text(technical.get("container") or Path(text(source.get("name"))).suffix.lstrip(".")).lower()
    reasons: list[str] = []
    if codecs and codec and codec not in codecs:
        reasons.append("codec_not_supported")
    if mime_types and mime_type and mime_type not in mime_types:
        reasons.append("mime_type_not_supported")
    if containers and container and container not in containers:
        reasons.append("container_not_supported")
    maximum_height = max(0, int(capabilities.get("max_video_height") or 0))
    height = max(0, int(technical.get("height") or 0))
    if kind == "video" and maximum_height and height > maximum_height:
        reasons.append("height_above_endpoint_limit")
    maximum_bitrate = max(0, int(capabilities.get("max_bitrate") or 0))
    bitrate = max(0, int(technical.get("bitrate") or 0))
    if maximum_bitrate and bitrate > maximum_bitrate:
        reasons.append("bitrate_above_endpoint_limit")
    required = bool(reasons)
    if not (codecs or mime_types or containers or maximum_height or maximum_bitrate):
        required = False
        reasons = ["endpoint_capabilities_not_restrictive"]
    target = {
        "profile": text(profile) or "browser-mp4-v1",
        "container": "mp4" if kind == "video" else "m4a",
        "mime_type": "video/mp4" if kind == "video" else "audio/mp4",
        "video_codec": "h264" if kind == "video" else "",
        "audio_codec": "aac",
        "max_width": max(0, int(capabilities.get("max_video_width") or 0)),
        "max_height": maximum_height,
        "max_bitrate": maximum_bitrate,
    }
    return {
        "schema": RENDITION_PLAN_SCHEMA,
        "source_id": text(source.get("id")),
        "source_revision": int(source.get("revision") or 0),
        "source_fingerprint": text(source.get("fingerprint")),
        "required": required,
        "reasons": reasons,
        "target": target,
        "resource_policy": rendition_limits(),
    }


def rendition_limits() -> dict[str, Any]:
    def bounded(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.environ.get(name) or default)
        except ValueError:
            value = default
        return max(minimum, min(maximum, value))

    return {
        "max_concurrent": 1,
        "threads": bounded("MEDIA_LIBRARY_AGENT_RENDITION_THREADS", 1, 1, 8),
        "timeout_seconds": bounded(
            "MEDIA_LIBRARY_AGENT_RENDITION_TIMEOUT_SECONDS", 7200, 30, 21600
        ),
        "max_output_bytes": bounded(
            "MEDIA_LIBRARY_AGENT_RENDITION_MAX_OUTPUT_BYTES",
            8 * 1024**3,
            1024**2,
            128 * 1024**3,
        ),
        "disk_quota_bytes": bounded(
            "MEDIA_LIBRARY_AGENT_RENDITION_DISK_QUOTA_BYTES",
            20 * 1024**3,
            1024**2,
            1024**4,
        ),
        "max_rss_mb": bounded(
            "MEDIA_LIBRARY_AGENT_RENDITION_MAX_RSS_MB", 1024, 64, 32768
        ),
    }


def derived_workspace(db_path: Path) -> Path:
    root = db_path.parent / "renditions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def output_path(db_path: Path, job: Mapping[str, Any]) -> Path:
    suffix = ".mp4" if text(job.get("media_kind")) == "video" else ".m4a"
    digest = hashlib.sha256(
        f"{text(job.get('id'))}:{text(job.get('source_fingerprint'))}".encode("utf-8")
    ).hexdigest()[:24]
    return derived_workspace(db_path) / f"rendition-{digest}{suffix}"


def current_disk_usage(db_path: Path) -> int:
    root = derived_workspace(db_path)
    total = 0
    for candidate in root.glob("rendition-*"):
        try:
            if candidate.is_file():
                total += int(candidate.stat().st_size)
        except OSError:
            continue
    return total


def process_rss_mb(process_id: int) -> float | None:
    try:
        import psutil  # type: ignore[import-not-found]

        return float(psutil.Process(process_id).memory_info().rss) / (1024 * 1024)
    except Exception:
        pass
    status_path = Path(f"/proc/{process_id}/status")
    try:
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def transcode_with_ffmpeg(
    source_path: Path,
    target_path: Path,
    job: Mapping[str, Any],
    *,
    cancelled: CancelCallback,
) -> dict[str, Any]:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("rendition_backend_unavailable")
    limits = rendition_limits()
    partial = target_path.with_suffix(target_path.suffix + ".partial")
    partial.unlink(missing_ok=True)
    target = dict(job.get("target") or {})
    command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_path)]
    if text(job.get("media_kind")) == "video":
        command.extend(["-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22"])
        max_width = max(0, int(target.get("max_width") or 0))
        max_height = max(0, int(target.get("max_height") or 0))
        if max_width or max_height:
            width = max_width or 16384
            height = max_height or 16384
            command.extend(["-vf", f"scale='min(iw,{width})':'min(ih,{height})':force_original_aspect_ratio=decrease"])
    else:
        command.extend(["-map", "0:a:0", "-vn"])
    command.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-threads",
            str(limits["threads"]),
            "-movflags",
            "+faststart",
            "-fs",
            str(limits["max_output_bytes"]),
            "-f",
            "mp4",
            str(partial),
        ]
    )
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    started = time.monotonic()
    error = b""
    try:
        while process.poll() is None:
            if cancelled():
                process.terminate()
                raise RuntimeError("rendition_canceled")
            if time.monotonic() - started > int(limits["timeout_seconds"]):
                process.terminate()
                raise RuntimeError("rendition_timeout")
            rss = process_rss_mb(process.pid)
            if rss is not None and rss > int(limits["max_rss_mb"]):
                process.terminate()
                raise RuntimeError("rendition_rss_limit_exceeded")
            if partial.exists() and partial.stat().st_size > int(limits["max_output_bytes"]):
                process.terminate()
                raise RuntimeError("rendition_output_limit_exceeded")
            time.sleep(0.1)
        _stdout, error = process.communicate(timeout=2)
        if process.returncode != 0:
            detail = error.decode("utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"rendition_backend_failed:{detail}")
        if not partial.exists() or partial.stat().st_size <= 0:
            raise RuntimeError("rendition_output_empty")
        partial.replace(target_path)
        return {
            "size_bytes": int(target_path.stat().st_size),
            "mime_type": text(target.get("mime_type"))
            or mimetypes.guess_type(target_path.name)[0]
            or "application/octet-stream",
        }
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        partial.unlink(missing_ok=True)


def publish_derived_resource(
    path: Path, job: Mapping[str, Any]
) -> Mapping[str, Any]:
    from adaos.sdk.io.media import publish_media_file

    return publish_media_file(
        path,
        content_ref=(
            f"rendition:{text(job.get('source_id'))}:"
            f"{int(job.get('source_revision') or 0)}:{text(job.get('profile'))}"
        ),
        namespace="media-library-rendition",
        variant=text(job.get("profile")) or "browser-mp4-v1",
        mime=text((job.get("target") or {}).get("mime_type"))
        or "application/octet-stream",
    )
