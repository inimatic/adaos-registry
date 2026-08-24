from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import ARTWORK_PLAN_SCHEMA, RENDITION_PLAN_SCHEMA, text


CancelCallback = Callable[[], bool]
ARTWORK_PROFILE = "artwork-card-v1"
ARTWORK_NAMES = ("cover", "folder", "front", "poster", "album", "artwork")
ARTWORK_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def _ffmpeg_executable() -> tuple[str | None, str]:
    explicit = text(os.environ.get("MEDIA_LIBRARY_AGENT_FFMPEG_PATH"))
    if explicit and Path(explicit).is_file():
        return explicit, "configured"
    system = shutil.which("ffmpeg")
    if system:
        return system, "system"
    try:
        from imageio_ffmpeg import get_ffmpeg_exe  # type: ignore[import-not-found]

        bundled = text(get_ffmpeg_exe())
        if bundled and Path(bundled).is_file():
            return bundled, "imageio_ffmpeg"
    except Exception:
        pass
    return None, "unavailable"


def artwork_capabilities() -> dict[str, Any]:
    executable, source = _ffmpeg_executable()
    try:
        from PIL import Image  # noqa: F401

        image_backend = True
    except Exception:
        image_backend = False
    try:
        from mutagen import File as MutagenFile  # noqa: F401

        embedded_audio = True
    except Exception:
        embedded_audio = False
    witness = hashlib.sha256(
        f"image:{int(image_backend)}|embedded:{int(embedded_audio)}|ffmpeg:{source}".encode(
            "utf-8"
        )
    ).hexdigest()[:20]
    return {
        "schema": "adaos.media_library.artwork_capabilities.v1",
        "local_images": image_backend,
        "embedded_audio": embedded_audio,
        "video_frames": bool(executable),
        "ffmpeg_source": source,
        "witness": witness,
    }


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


def artwork_plan(
    source: Mapping[str, Any],
    *,
    force: bool = False,
    capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    capability_state = dict(capabilities or artwork_capabilities())
    metadata = dict(source.get("metadata") or {})
    artwork = (
        dict(metadata.get("artwork") or {})
        if isinstance(metadata.get("artwork"), Mapping)
        else {}
    )
    current = bool(
        artwork.get("state") == "ready"
        and text(artwork.get("exact_source_fingerprint"))
        == text(source.get("fingerprint"))
    )
    terminal = bool(
        artwork.get("state") in {"unavailable", "failed"}
        and text(artwork.get("exact_source_fingerprint"))
        == text(source.get("fingerprint"))
        and (
            text(artwork.get("capability_witness"))
            in {"", text(capability_state.get("witness"))}
        )
    )
    return {
        "schema": ARTWORK_PLAN_SCHEMA,
        "source_id": text(source.get("id")),
        "source_revision": int(source.get("revision") or 0),
        "source_fingerprint": text(source.get("fingerprint")),
        "required": bool(force or not (current or terminal)),
        "state": "ready" if current else (text(artwork.get("state")) or "missing"),
        "reasons": (
            ["forced"] if force else
            (["artwork_ready"] if current else
             (["terminal_artwork_state"] if terminal else ["artwork_missing"]))
        ),
        "target": {
            "profile": ARTWORK_PROFILE,
            "kind": "artwork",
            "mime_type": "image/jpeg",
            "max_width": 720,
            "max_height": 1080,
            "quality": 84,
            "max_output_bytes": 4 * 1024**2,
        },
        "artwork": artwork or None,
        "capabilities": capability_state,
        "resource_policy": {
            "max_concurrent": 1,
            "timeout_seconds": 45,
            "max_output_bytes": 4 * 1024**2,
            "max_input_bytes": 32 * 1024**2,
        },
    }


def folder_artwork_witness(source_path: Path) -> dict[str, Any]:
    candidate = folder_artwork_candidate(source_path)
    if candidate is not None:
        stat = candidate.stat()
        return {
            "name": candidate.name,
            "size_bytes": int(stat.st_size),
            "modified_ns": int(stat.st_mtime_ns),
        }
    return {}


def folder_artwork_candidate(source_path: Path) -> Path | None:
    names = set(ARTWORK_NAMES)
    suffixes = set(ARTWORK_EXTENSIONS)
    try:
        entries = list(source_path.parent.iterdir())[:512]
    except OSError:
        return None
    candidates = [
        entry
        for entry in entries
        if entry.is_file()
        and entry.stem.casefold() in names
        and entry.suffix.casefold() in suffixes
    ]
    candidates.sort(
        key=lambda entry: (
            ARTWORK_NAMES.index(entry.stem.casefold()),
            ARTWORK_EXTENSIONS.index(entry.suffix.casefold()),
            entry.name.casefold(),
        )
    )
    return candidates[0] if candidates else None


def _embedded_artwork_bytes(source_path: Path, *, maximum: int) -> bytes | None:
    try:
        from mutagen import File as MutagenFile  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        media = MutagenFile(source_path)
    except Exception:
        return None
    candidates: list[Any] = []
    candidates.extend(list(getattr(media, "pictures", None) or []))
    tags = getattr(media, "tags", None)
    if isinstance(tags, Mapping):
        for key, value in list(tags.items())[:500]:
            if str(key).casefold() in {"covr", "cover", "metadata_block_picture"}:
                candidates.extend(value if isinstance(value, (list, tuple)) else [value])
                continue
            if hasattr(value, "data") and str(key).upper().startswith("APIC"):
                candidates.append(value)
    for candidate in candidates:
        payload = getattr(candidate, "data", candidate)
        try:
            data = bytes(payload)
        except Exception:
            continue
        if 0 < len(data) <= maximum:
            return data
    return None


def _render_artwork(
    source: Path | bytes,
    target_path: Path,
    *,
    maximum_size: tuple[int, int],
    quality: int,
    maximum_output_bytes: int,
) -> dict[str, Any]:
    from PIL import Image, ImageOps  # type: ignore[import-not-found]

    partial = target_path.with_suffix(target_path.suffix + ".partial")
    partial.unlink(missing_ok=True)
    opened = io.BytesIO(source) if isinstance(source, bytes) else source
    with Image.open(opened) as image:
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > 40_000_000:
            raise RuntimeError("artwork_input_dimensions_exceeded")
        image = ImageOps.exif_transpose(image)
        if getattr(image, "is_animated", False):
            image.seek(0)
        image.thumbnail(maximum_size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", image.size, "black")
        if image.mode in {"RGBA", "LA"}:
            canvas.paste(image, mask=image.getchannel("A"))
        else:
            canvas.paste(image.convert("RGB"))
        canvas.save(
            partial,
            "JPEG",
            quality=max(50, min(92, int(quality))),
            optimize=True,
        )
        output_width, output_height = canvas.size
    size_bytes = int(partial.stat().st_size)
    if size_bytes <= 0 or size_bytes > maximum_output_bytes:
        partial.unlink(missing_ok=True)
        raise RuntimeError("artwork_output_limit_exceeded")
    partial.replace(target_path)
    return {
        "size_bytes": size_bytes,
        "mime_type": "image/jpeg",
        "width": output_width,
        "height": output_height,
    }


def _video_frame_artwork(
    source_path: Path,
    target_path: Path,
    *,
    cancelled: CancelCallback,
    timeout_seconds: int,
    maximum_output_bytes: int,
) -> dict[str, Any]:
    executable, _source = _ffmpeg_executable()
    if not executable:
        raise RuntimeError("artwork_video_backend_unavailable")
    partial = target_path.with_suffix(target_path.suffix + ".partial")
    started = time.monotonic()
    errors: list[str] = []
    for seek_seconds in ("5", "0"):
        partial.unlink(missing_ok=True)
        command = [
            executable, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file,pipe", "-ss", seek_seconds,
            "-i", str(source_path), "-map", "0:v:0", "-frames:v", "1",
            "-vf", "scale=w='min(720,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease",
            "-pix_fmt", "yuvj420p", "-threads", "1", "-q:v", "3",
            "-f", "image2", str(partial),
        ]
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        error = b""
        try:
            while process.poll() is None:
                if cancelled():
                    process.terminate()
                    raise RuntimeError("rendition_canceled")
                if time.monotonic() - started > timeout_seconds:
                    process.terminate()
                    raise RuntimeError("artwork_timeout")
                if partial.exists() and partial.stat().st_size > maximum_output_bytes:
                    process.terminate()
                    raise RuntimeError("artwork_output_limit_exceeded")
                time.sleep(0.1)
            _stdout, error = process.communicate(timeout=2)
            if process.returncode == 0 and partial.exists():
                if partial.stat().st_size > maximum_output_bytes:
                    raise RuntimeError("artwork_output_limit_exceeded")
                partial.replace(target_path)
                from PIL import Image  # type: ignore[import-not-found]
                with Image.open(target_path) as image:
                    width, height = image.size
                return {
                    "size_bytes": int(target_path.stat().st_size),
                    "mime_type": "image/jpeg",
                    "width": int(width),
                    "height": int(height),
                }
            detail = error.decode("utf-8", errors="replace")[-1000:].strip()
            errors.append(detail or f"no frame at {seek_seconds}s")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            partial.unlink(missing_ok=True)
    raise RuntimeError(f"artwork_video_frame_failed:{errors[-1]}")


def materialize_artwork(
    source_path: Path,
    target_path: Path,
    job: Mapping[str, Any],
    *,
    cancelled: CancelCallback,
) -> dict[str, Any]:
    target = dict(job.get("target") or {})
    maximum_input = max(1024, min(32 * 1024**2, int(target.get("max_input_bytes") or 32 * 1024**2)))
    maximum_output = max(64 * 1024, min(4 * 1024**2, int(target.get("max_output_bytes") or 4 * 1024**2)))
    maximum_size = (
        max(64, min(1920, int(target.get("max_width") or 720))),
        max(64, min(1920, int(target.get("max_height") or 1080))),
    )
    quality = max(50, min(92, int(target.get("quality") or 84)))
    if cancelled():
        raise RuntimeError("rendition_canceled")
    embedded = _embedded_artwork_bytes(source_path, maximum=maximum_input)
    if embedded:
        return {
            **_render_artwork(
                embedded,
                target_path,
                maximum_size=maximum_size,
                quality=quality,
                maximum_output_bytes=maximum_output,
            ),
            "provider_id": "media_library_agent.embedded_cover.v1",
            "source_kind": "embedded",
        }
    folder = folder_artwork_candidate(source_path)
    if folder is not None:
        if folder.stat().st_size > maximum_input:
            raise RuntimeError("artwork_input_limit_exceeded")
        return {
            **_render_artwork(
                folder,
                target_path,
                maximum_size=maximum_size,
                quality=quality,
                maximum_output_bytes=maximum_output,
            ),
            "provider_id": "media_library_agent.folder_artwork.v1",
            "source_kind": "folder",
            "source_name": folder.name,
        }
    if text(job.get("media_kind")) == "video":
        return {
            **_video_frame_artwork(
                source_path,
                target_path,
                cancelled=cancelled,
                timeout_seconds=45,
                maximum_output_bytes=maximum_output,
            ),
            "provider_id": "media_library_agent.video_frame.v1",
            "source_kind": "generated_frame",
        }
    raise RuntimeError("artwork_not_found")


def derived_workspace(db_path: Path) -> Path:
    root = db_path.parent / "renditions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def output_path(db_path: Path, job: Mapping[str, Any]) -> Path:
    suffix = (
        ".jpg"
        if text(job.get("profile")) == ARTWORK_PROFILE
        else (".mp4" if text(job.get("media_kind")) == "video" else ".m4a")
    )
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
    executable, _source = _ffmpeg_executable()
    if not executable:
        raise RuntimeError("rendition_backend_unavailable")
    limits = rendition_limits()
    partial = target_path.with_suffix(target_path.suffix + ".partial")
    partial.unlink(missing_ok=True)
    target = dict(job.get("target") or {})
    command = [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-y",
        "-i",
        str(source_path),
    ]
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
        namespace=(
            "media-library-artwork"
            if text(job.get("profile")) == ARTWORK_PROFILE
            else "media-library-rendition"
        ),
        variant=text(job.get("profile")) or "browser-mp4-v1",
        mime=text((job.get("target") or {}).get("mime_type"))
        or "application/octet-stream",
    )
