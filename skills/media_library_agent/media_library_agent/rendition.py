from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import platform
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import ARTWORK_PLAN_SCHEMA, RENDITION_PLAN_SCHEMA, text


CancelCallback = Callable[[], bool]
ARTWORK_PROFILE = "artwork-card-v1"
ARTWORK_SELECTION_ALGORITHM = "informative-frame-v2"
ARTWORK_NAMES = ("cover", "folder", "front", "poster", "album", "artwork")
ARTWORK_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
MP4_COPY_VIDEO_CODECS = frozenset({"h264", "avc", "avc1"})
MP4_COPY_AUDIO_CODECS = frozenset({"aac", "mp3", "mp4a"})
MANAGED_MEDIA_DIRECTORY = ".adaos-media"
MANAGED_STORE_SCHEMA = "adaos.media_library.managed_store.v1"


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
        f"image:{int(image_backend)}|embedded:{int(embedded_audio)}|ffmpeg:{source}|selector:{ARTWORK_SELECTION_ALGORITHM}".encode(
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


def _mime_tokens(value: Any) -> set[str]:
    return {item.split(";", 1)[0].strip() for item in _tokens(value)}


def _technical_streams(technical: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in technical.get("streams") or []
        if isinstance(item, Mapping) and text(item.get("kind")).lower() == kind
    ]


def _preferred_audio(
    streams: list[dict[str, Any]], preferred_language: str
) -> dict[str, Any]:
    language = text(preferred_language).lower()
    if language:
        match = next(
            (
                item
                for item in streams
                if text(item.get("language")).lower() == language
            ),
            None,
        )
        if match:
            return match
    default = next(
        (
            item
            for item in streams
            if bool((item.get("disposition") or {}).get("default"))
        ),
        None,
    )
    return default or (streams[0] if streams else {})


def _abr_ladder(
    maximum_height: int, maximum_bitrate: int, source_height: int
) -> list[dict[str, int]]:
    presets = (
        (360, 800_000),
        (480, 1_400_000),
        (720, 3_000_000),
        (1080, 6_000_000),
        (2160, 16_000_000),
    )
    ceiling_height = min(
        value for value in (maximum_height, source_height, 1080) if value > 0
    )
    ceiling_bitrate = maximum_bitrate or 8_000_000
    ladder = [
        {"height": height, "video_bitrate": min(bitrate, ceiling_bitrate)}
        for height, bitrate in presets
        if height <= ceiling_height and bitrate <= ceiling_bitrate
    ]
    if not ladder:
        ladder.append(
            {
                "height": max(240, min(ceiling_height, 1080)),
                "video_bitrate": max(300_000, ceiling_bitrate),
            }
        )
    return ladder[-3:]


def rendition_plan(
    source: Mapping[str, Any],
    *,
    endpoint_capabilities: Mapping[str, Any] | None = None,
    profile: str = "browser-mp4-v1",
    preferred_audio_language: str = "",
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
    video_streams = _technical_streams(technical, "video")
    audio_streams = _technical_streams(technical, "audio")
    video = video_streams[0] if video_streams else {}
    audio = _preferred_audio(audio_streams, preferred_audio_language)
    codec = text(
        video.get("codec") or audio.get("codec") or technical.get("codec")
    ).lower()
    audio_codec = text(audio.get("codec")).lower()
    codecs = _tokens(capabilities.get("codecs"))
    mime_types = _mime_tokens(capabilities.get("mime_types"))
    containers = _tokens(capabilities.get("containers"))
    container = text(
        technical.get("container") or Path(text(source.get("name"))).suffix.lstrip(".")
    ).lower()
    reasons: list[str] = []
    codec_incompatible = bool(codecs and codec and codec not in codecs)
    audio_incompatible = bool(codecs and audio_codec and audio_codec not in codecs)
    if codec_incompatible:
        reasons.append(
            "video_codec_not_supported"
            if kind == "video"
            else "audio_codec_not_supported"
        )
    if kind == "video" and audio_incompatible:
        reasons.append("audio_codec_not_supported")
    if mime_types and mime_type and mime_type not in mime_types:
        reasons.append("mime_type_not_supported")
    if containers and container and container not in containers:
        reasons.append("container_not_supported")
    maximum_height = max(0, int(capabilities.get("max_video_height") or 0))
    height = max(0, int(video.get("height") or technical.get("height") or 0))
    if kind == "video" and maximum_height and height > maximum_height:
        reasons.append("height_above_endpoint_limit")
    maximum_bitrate = max(0, int(capabilities.get("max_bitrate") or 0))
    bitrate = max(0, int(technical.get("bitrate") or 0))
    if maximum_bitrate and bitrate > maximum_bitrate:
        reasons.append("bitrate_above_endpoint_limit")
    source_hdr = {
        text(item)
        for item in technical.get("hdr_modes") or []
        if text(item) and text(item) != "sdr"
    }
    endpoint_hdr = _tokens(capabilities.get("hdr_modes"))
    hdr_unsupported = bool(
        source_hdr and endpoint_hdr and source_hdr.isdisjoint(endpoint_hdr)
    )
    if hdr_unsupported:
        reasons.append("hdr_tone_mapping_deferred")
    required = bool(reasons)
    if not (codecs or mime_types or containers or maximum_height or maximum_bitrate):
        required = False
        reasons = ["endpoint_capabilities_not_restrictive"]
    elif not required and not reasons:
        reasons = ["direct_compatible"]
    container_only = bool(reasons) and set(reasons).issubset(
        {"container_not_supported", "mime_type_not_supported"}
    )
    remux_safe = bool(
        container_only
        and (
            (
                kind == "video"
                and video
                and codec in MP4_COPY_VIDEO_CODECS
                and (not audio or audio_codec in MP4_COPY_AUDIO_CODECS)
            )
            or (kind == "audio" and audio and audio_codec in MP4_COPY_AUDIO_CODECS)
        )
    )
    if container_only and not remux_safe:
        reasons.append("stream_compatibility_unverified")
    if hdr_unsupported:
        decision = "unsupported"
    elif not required:
        decision = "direct"
    elif remux_safe:
        decision = "remux"
    elif bool(capabilities.get("hls")) and kind == "video" and bool(video):
        decision = "prepared_hls"
    else:
        decision = "transcode"
    hls = decision == "prepared_hls"
    requested_profile = text(profile) or "browser-mp4-v1"
    default_profile = {
        "remux": "browser-remux-mp4-v1",
        "prepared_hls": "browser-hls-cmaf-v1",
        "transcode": "browser-mp4-v1",
    }.get(decision, requested_profile)
    target_profile = (
        default_profile if requested_profile == "browser-mp4-v1" else requested_profile
    )
    profile_parts = [target_profile]
    if decision != "remux" and maximum_height:
        profile_parts.append(f"{maximum_height}p")
    if text(audio.get("language")):
        profile_parts.append(text(audio.get("language")).lower())
    target = {
        "profile": "-".join(profile_parts),
        "decision": decision,
        "packaging": "hls_cmaf_vod" if hls else "single_file",
        "container": "cmaf" if hls else ("mp4" if kind == "video" else "m4a"),
        "mime_type": (
            "application/vnd.apple.mpegurl"
            if hls
            else ("video/mp4" if kind == "video" else "audio/mp4")
        ),
        "video_codec": "h264" if kind == "video" else "",
        "audio_codec": "aac",
        "max_width": max(0, int(capabilities.get("max_video_width") or 0)),
        "max_height": maximum_height,
        "max_bitrate": maximum_bitrate,
        "abr_ladder": (
            _abr_ladder(maximum_height, maximum_bitrate, height) if hls else []
        ),
        "selected_tracks": {
            "video_index": int(video.get("index") or 0) if video else -1,
            "audio_index": int(audio.get("index") or 0) if audio else -1,
            "audio_language": text(audio.get("language")),
            "has_audio": bool(audio),
        },
    }
    return {
        "schema": RENDITION_PLAN_SCHEMA,
        "source_id": text(source.get("id")),
        "source_revision": int(source.get("revision") or 0),
        "source_fingerprint": text(source.get("fingerprint")),
        "required": required,
        "decision": decision,
        "reasons": reasons,
        "selected_tracks": {
            "video_index": int(video.get("index") or 0) if video else -1,
            "audio_index": int(audio.get("index") or 0) if audio else -1,
            "audio_language": text(audio.get("language")),
        },
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
    generated_frame_current = bool(
        text(artwork.get("source_kind")) != "generated_frame"
        or text(artwork.get("selection_algorithm")) == ARTWORK_SELECTION_ALGORITHM
    )
    current = bool(
        artwork.get("state") == "ready"
        and text(artwork.get("exact_source_fingerprint"))
        == text(source.get("fingerprint"))
        and generated_frame_current
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
            ["forced"]
            if force
            else (
                ["artwork_ready"]
                if current
                else (["terminal_artwork_state"] if terminal else ["artwork_missing"])
            )
        ),
        "target": {
            "profile": ARTWORK_PROFILE,
            "kind": "artwork",
            "mime_type": "image/jpeg",
            "max_width": 720,
            "max_height": 1080,
            "quality": 84,
            "max_output_bytes": 4 * 1024**2,
            "sample_duration_seconds": max(
                0.0,
                float(
                    ((metadata.get("technical") or {}).get("duration_seconds") or 0)
                    if isinstance(metadata.get("technical"), Mapping)
                    else 0
                ),
            ),
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
                candidates.extend(
                    value if isinstance(value, (list, tuple)) else [value]
                )
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


def _frame_information(path: Path) -> dict[str, Any]:
    from PIL import Image, ImageStat  # type: ignore[import-not-found]

    with Image.open(path) as image:
        sample = image.convert("L")
        sample.thumbnail((96, 96))
        entropy = float(sample.entropy())
        stats = ImageStat.Stat(sample)
        mean = float(stats.mean[0])
        deviation = float(stats.stddev[0])
        histogram = sample.histogram()
        pixels = max(1, sum(histogram))
        clipped = float(sum(histogram[:5]) + sum(histogram[251:])) / pixels
    acceptable = bool(
        8.0 <= mean <= 247.0
        and clipped < 0.985
        and (entropy >= 2.0 or deviation >= 14.0)
    )
    return {
        "acceptable": acceptable,
        "score": round(entropy + deviation / 32.0 - clipped, 6),
        "entropy": round(entropy, 6),
        "luminance_mean": round(mean, 3),
        "luminance_deviation": round(deviation, 3),
        "clipped_ratio": round(clipped, 6),
    }


def _artwork_sample_positions(duration_seconds: float) -> tuple[float, ...]:
    duration = max(0.0, float(duration_seconds or 0))
    if duration >= 20.0:
        maximum = max(0.0, duration - 2.0)
        values = [
            max(2.0, min(maximum, duration * fraction))
            for fraction in (0.18, 0.48, 0.72)
        ]
    else:
        values = [5.0, 30.0, 90.0]
    return tuple(dict.fromkeys(round(value, 3) for value in values))[:3]


def _video_frame_artwork(
    source_path: Path,
    target_path: Path,
    *,
    cancelled: CancelCallback,
    timeout_seconds: int,
    maximum_output_bytes: int,
    duration_seconds: float = 0,
) -> dict[str, Any]:
    executable, _source = _ffmpeg_executable()
    if not executable:
        raise RuntimeError("artwork_video_backend_unavailable")
    started = time.monotonic()
    errors: list[str] = []
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    sample_positions = [*_artwork_sample_positions(duration_seconds), 0.0]
    for index, seek_value in enumerate(sample_positions):
        if seek_value == 0.0 and candidates:
            break
        seek_seconds = f"{seek_value:g}"
        partial = target_path.with_suffix(
            target_path.suffix + f".candidate-{index}.partial"
        )
        partial.unlink(missing_ok=True)
        command = [
            executable,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-ss",
            seek_seconds,
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            "scale=w='min(720,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease",
            "-pix_fmt",
            "yuvj420p",
            "-threads",
            "1",
            "-q:v",
            "3",
            "-f",
            "image2",
            str(partial),
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
                from PIL import Image  # type: ignore[import-not-found]

                with Image.open(partial) as image:
                    width, height = image.size
                information = _frame_information(partial)
                if information["acceptable"]:
                    candidates.append(
                        (
                            float(information["score"]),
                            partial,
                            {
                                "size_bytes": int(partial.stat().st_size),
                                "mime_type": "image/jpeg",
                                "width": int(width),
                                "height": int(height),
                                "sample_seek_seconds": seek_value,
                                "information_score": float(information["score"]),
                                "selection_algorithm": ARTWORK_SELECTION_ALGORITHM,
                            },
                        )
                    )
                    continue
                errors.append(f"uninformative frame at {seek_seconds}s")
            detail = error.decode("utf-8", errors="replace")[-1000:].strip()
            if not partial.exists():
                errors.append(detail or f"no frame at {seek_seconds}s")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            if not any(path == partial for _score, path, _result in candidates):
                partial.unlink(missing_ok=True)
    if candidates:
        _score, selected_path, result = max(candidates, key=lambda item: item[0])
        selected_path.replace(target_path)
        for _candidate_score, candidate_path, _candidate_result in candidates:
            if candidate_path != selected_path:
                candidate_path.unlink(missing_ok=True)
        return result
    raise RuntimeError(f"artwork_video_frame_failed:{errors[-1]}")


def materialize_artwork(
    source_path: Path,
    target_path: Path,
    job: Mapping[str, Any],
    *,
    cancelled: CancelCallback,
) -> dict[str, Any]:
    target = dict(job.get("target") or {})
    maximum_input = max(
        1024, min(32 * 1024**2, int(target.get("max_input_bytes") or 32 * 1024**2))
    )
    maximum_output = max(
        64 * 1024, min(4 * 1024**2, int(target.get("max_output_bytes") or 4 * 1024**2))
    )
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
                duration_seconds=float(target.get("sample_duration_seconds") or 0),
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
    target = dict(job.get("target") or {})
    suffix = (
        ".jpg"
        if text(job.get("profile")) == ARTWORK_PROFILE
        else (".mp4" if text(job.get("media_kind")) == "video" else ".m4a")
    )
    digest = hashlib.sha256(
        f"{text(job.get('id'))}:{text(job.get('source_fingerprint'))}".encode("utf-8")
    ).hexdigest()[:24]
    if text(target.get("packaging")) == "hls_cmaf_vod":
        package = derived_workspace(db_path) / f"rendition-{digest}"
        package.mkdir(parents=True, exist_ok=True)
        return package / "master.m3u8"
    return derived_workspace(db_path) / f"rendition-{digest}{suffix}"


def _managed_content_digest(job: Mapping[str, Any]) -> str:
    target = dict(job.get("target") or {})
    payload = "\0".join(
        (
            text(job.get("source_fingerprint")),
            text(job.get("profile")),
            json.dumps(
                target,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            ),
        )
    )
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _managed_store(root: Path) -> Path:
    root_path = root.resolve(strict=True)
    if not root_path.is_dir():
        raise RuntimeError("rendition_storage_root_unavailable")
    store = root_path / MANAGED_MEDIA_DIRECTORY
    if store.exists() and store.is_symlink():
        raise RuntimeError("rendition_storage_symlink_rejected")
    store.mkdir(parents=True, exist_ok=True)
    resolved = store.resolve(strict=True)
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise RuntimeError("rendition_storage_outside_root") from exc
    marker = resolved / "store.json"
    if not marker.exists():
        partial = marker.with_suffix(".json.partial")
        partial.write_text(
            json.dumps(
                {
                    "schema": MANAGED_STORE_SCHEMA,
                    "owner": "media_library_agent",
                    "layout": "content_addressed_v1",
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        partial.replace(marker)
    return resolved


def publish_managed_rendition(
    path: Path,
    job: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Move one derived file to the source volume and register it in place."""

    target = dict(job.get("target") or {})
    if text(target.get("packaging")) == "hls_cmaf_vod":
        raise RuntimeError("managed_rendition_package_not_supported")
    source = path.resolve(strict=True)
    if not source.is_file():
        raise RuntimeError("rendition_output_empty")
    root_path = Path(text(root.get("path"))).resolve(strict=True)
    store = _managed_store(root_path)
    digest = _managed_content_digest(job)
    suffix = source.suffix.lower() or (
        ".mp4" if text(job.get("media_kind")) == "video" else ".m4a"
    )
    profile = (
        "".join(
            character
            for character in text(job.get("profile")).lower()
            if character.isalnum() or character in {"-", "_"}
        )
        or "rendition"
    )
    directory = store / "renditions" / profile / digest[:2]
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest}{suffix}"
    source_size = int(source.stat().st_size)
    source_checksum = _sha256_file(source)
    if destination.is_file():
        if (
            int(destination.stat().st_size) != source_size
            or _sha256_file(destination) != source_checksum
        ):
            destination.unlink()
    if not destination.exists():
        temporary = destination.with_name(
            f"{destination.name}.{text(job.get('id'))[:24]}.partial"
        )
        temporary.unlink(missing_ok=True)
        shutil.copyfile(source, temporary)
        if (
            int(temporary.stat().st_size) != source_size
            or _sha256_file(temporary) != source_checksum
        ):
            temporary.unlink(missing_ok=True)
            raise RuntimeError("rendition_storage_verification_failed")
        temporary.replace(destination)

    from adaos.sdk.io.media import register_media_file

    descriptor = register_media_file(
        destination,
        root=root_path,
        content_ref=(
            f"managed-rendition:{text(job.get('source_id'))}:"
            f"{text(job.get('source_fingerprint'))}:{text(job.get('profile'))}:"
            f"{digest}"
        ),
        namespace="media-library-rendition",
        mime=text(target.get("mime_type"))
        or mimetypes.guess_type(destination.name)[0]
        or "application/octet-stream",
        metadata={
            "managed_store_schema": MANAGED_STORE_SCHEMA,
            "managed_storage_mode": "source_root",
            "checksum_sha256": source_checksum,
            "content_digest": digest,
            "rendition_profile": text(job.get("profile")),
        },
    )
    metadata = dict(descriptor.get("metadata") or {})
    metadata.update(
        {
            "storage_mode": "source_root",
            "managed_storage_mode": "source_root",
            "checksum_sha256": source_checksum,
            "content_digest": digest,
        }
    )
    descriptor["metadata"] = metadata
    delivery = dict(descriptor.get("delivery") or {})
    delivery["storage_mode"] = "source_root"
    descriptor["delivery"] = delivery
    return descriptor


def current_disk_usage(db_path: Path) -> int:
    root = derived_workspace(db_path)
    total = 0
    for candidate in root.rglob("*"):
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


@lru_cache(maxsize=8)
def _probe_ffmpeg_capabilities(
    executable: str,
    source: str,
    policy: str,
) -> dict[str, Any]:
    try:
        encoder_probe = subprocess.run(
            [executable, "-hide_banner", "-encoders"],
            capture_output=True,
            check=False,
            timeout=8,
            text=True,
        )
        acceleration_probe = subprocess.run(
            [executable, "-hide_banner", "-hwaccels"],
            capture_output=True,
            check=False,
            timeout=8,
            text=True,
        )
        encoder_output = encoder_probe.stdout + encoder_probe.stderr
        acceleration_output = acceleration_probe.stdout + acceleration_probe.stderr
    except (OSError, subprocess.SubprocessError):
        encoder_output = ""
        acceleration_output = ""
    encoder_names = {
        "nvenc": "h264_nvenc",
        "qsv": "h264_qsv",
        "amf": "h264_amf",
        "vaapi": "h264_vaapi",
        "videotoolbox": "h264_videotoolbox",
    }
    available_encoders = sorted(
        name for name in {"libx264", *encoder_names.values()} if name in encoder_output
    )
    available_hwaccels = sorted(
        name
        for name in ("cuda", "qsv", "d3d11va", "dxva2", "vaapi", "videotoolbox")
        if name in acceleration_output.lower()
    )
    preferred = {
        "Windows": ("nvenc", "qsv", "amf"),
        "Linux": ("vaapi", "qsv", "nvenc"),
        "Darwin": ("videotoolbox",),
    }.get(platform.system(), ("nvenc", "qsv", "vaapi"))
    selected = "libx264"
    selected_backend = "software"
    requested = (
        preferred if policy == "auto" else (() if policy == "software" else (policy,))
    )
    for backend in requested:
        candidate = encoder_names[backend]
        if candidate in available_encoders:
            selected = candidate
            selected_backend = backend
            break
    result = {
        "schema": "adaos.media_library.ffmpeg_capabilities.v1",
        "available": True,
        "source": source,
        "policy": policy,
        "hardware_acceleration": selected_backend,
        "selected_video_encoder": selected,
        "software_fallback": "libx264" in available_encoders,
        "encoders": available_encoders,
        "hwaccels": available_hwaccels,
    }
    return dict(result)


def ffmpeg_capabilities() -> dict[str, Any]:
    executable, source = _ffmpeg_executable()
    if not executable:
        return {
            "schema": "adaos.media_library.ffmpeg_capabilities.v1",
            "available": False,
            "source": source,
            "hardware_acceleration": "unavailable",
            "selected_video_encoder": "",
            "encoders": [],
            "hwaccels": [],
        }
    policy = text(
        os.environ.get("MEDIA_LIBRARY_AGENT_HARDWARE_ACCELERATION") or "auto"
    ).lower()
    if policy not in {
        "auto",
        "software",
        "nvenc",
        "qsv",
        "amf",
        "vaapi",
        "videotoolbox",
    }:
        policy = "auto"
    return dict(_probe_ffmpeg_capabilities(executable, source, policy))


def _encoder_options(encoder: str) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-preset", "p4"]
    if encoder == "h264_qsv":
        return ["-preset", "veryfast"]
    if encoder == "h264_amf":
        return ["-quality", "speed"]
    if encoder == "h264_videotoolbox":
        return ["-realtime", "true"]
    if encoder == "h264_vaapi":
        return ["-quality", "6"]
    return ["-preset", "veryfast", "-crf", "22"]


def _output_bytes(path: Path) -> int:
    if path.is_file():
        return int(path.stat().st_size)
    if path.is_dir():
        return sum(
            int(candidate.stat().st_size)
            for candidate in path.rglob("*")
            if candidate.is_file()
        )
    return 0


def _clear_materialized_output(path: Path) -> None:
    target = path.parent if path.name == "master.m3u8" else path
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink(missing_ok=True)
        target.with_suffix(target.suffix + ".partial").unlink(missing_ok=True)


def _run_ffmpeg(
    command: list[str],
    *,
    output_root: Path,
    limits: Mapping[str, Any],
    cancelled: CancelCallback,
    cwd: Path | None = None,
) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
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
            if _output_bytes(output_root) > int(limits["max_output_bytes"]):
                process.terminate()
                raise RuntimeError("rendition_output_limit_exceeded")
            time.sleep(0.1)
        _stdout, error = process.communicate(timeout=2)
        if process.returncode != 0:
            detail = error.decode("utf-8", errors="replace")[-1000:].strip()
            raise RuntimeError(f"rendition_backend_failed:{detail}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def _single_file_command(
    executable: str,
    source_path: Path,
    partial: Path,
    job: Mapping[str, Any],
    *,
    encoder: str,
    limits: Mapping[str, Any],
) -> list[str]:
    target = dict(job.get("target") or {})
    tracks = dict(target.get("selected_tracks") or {})
    kind = text(job.get("media_kind"))
    decision = text(target.get("decision")) or "transcode"
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
    if kind == "video":
        video_index = int(
            tracks.get("video_index") if tracks.get("video_index") is not None else -1
        )
        command.extend(["-map", f"0:{video_index}" if video_index >= 0 else "0:v:0"])
        audio_index = int(
            tracks.get("audio_index") if tracks.get("audio_index") is not None else -1
        )
        command.extend(["-map", f"0:{audio_index}" if audio_index >= 0 else "0:a:0?"])
        if decision == "remux":
            command.extend(["-c:v", "copy", "-c:a", "copy"])
        else:
            command.extend(["-c:v", encoder, *_encoder_options(encoder)])
            max_width = max(0, int(target.get("max_width") or 0))
            max_height = max(0, int(target.get("max_height") or 0))
            if max_width or max_height:
                width = max_width or 16384
                height = max_height or 16384
                command.extend(
                    [
                        "-vf",
                        f"scale=w='min(iw,{width})':h='min(ih,{height})':"
                        "force_original_aspect_ratio=decrease:force_divisible_by=2",
                    ]
                )
            command.extend(["-c:a", "aac", "-b:a", "192k"])
    else:
        audio_index = int(tracks.get("audio_index") or 0)
        command.extend(["-map", f"0:{audio_index}", "-vn"])
        command.extend(["-c:a", "copy" if decision == "remux" else "aac"])
        if decision != "remux":
            command.extend(["-b:a", "192k"])
    command.extend(
        [
            "-sn",
            "-map_metadata",
            "0",
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
    return command


def _hls_command(
    executable: str,
    source_path: Path,
    target_path: Path,
    job: Mapping[str, Any],
    *,
    encoder: str,
    limits: Mapping[str, Any],
) -> list[str]:
    target = dict(job.get("target") or {})
    tracks = dict(target.get("selected_tracks") or {})
    ladder = [
        dict(item)
        for item in target.get("abr_ladder") or []
        if isinstance(item, Mapping)
    ][:3]
    if not ladder:
        raise RuntimeError("rendition_hls_ladder_empty")
    output_directory = target_path.parent
    output_directory.mkdir(parents=True, exist_ok=True)
    video_index = int(
        tracks.get("video_index") if tracks.get("video_index") is not None else -1
    )
    has_audio = bool(tracks.get("has_audio"))
    audio_index = int(
        tracks.get("audio_index") if tracks.get("audio_index") is not None else -1
    )
    split_outputs = "".join(f"[vin{index}]" for index in range(len(ladder)))
    video_input = f"0:{video_index}" if video_index >= 0 else "0:v:0"
    filters = [f"[{video_input}]split={len(ladder)}{split_outputs}"]
    for index, rung in enumerate(ladder):
        height = max(240, int(rung.get("height") or 720))
        filters.append(
            f"[vin{index}]scale=w=-2:h={height}:"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2[v{index}]"
        )
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
        "-filter_complex",
        ";".join(filters),
    ]
    stream_map: list[str] = []
    for index, rung in enumerate(ladder):
        command.extend(["-map", f"[v{index}]"])
        if has_audio and audio_index >= 0:
            command.extend(["-map", f"0:{audio_index}"])
        command.extend(
            [
                f"-c:v:{index}",
                encoder,
                *_encoder_options(encoder),
                f"-b:v:{index}",
                str(max(300_000, int(rung.get("video_bitrate") or 3_000_000))),
            ]
        )
        if has_audio and audio_index >= 0:
            command.extend([f"-c:a:{index}", "aac", f"-b:a:{index}", "160k"])
            stream_map.append(f"v:{index},a:{index},name:{int(rung['height'])}p")
        else:
            stream_map.append(f"v:{index},name:{int(rung['height'])}p")
    command.extend(
        [
            "-sn",
            "-threads",
            str(limits["threads"]),
            "-f",
            "hls",
            "-hls_time",
            "6",
            "-hls_playlist_type",
            "vod",
            "-hls_segment_type",
            "fmp4",
            "-hls_flags",
            "independent_segments+temp_file",
            "-hls_fmp4_init_filename",
            "init-%v.mp4",
            "-hls_segment_filename",
            "v%v-segment-%06d.m4s",
            "-master_pl_name",
            target_path.name,
            "-var_stream_map",
            " ".join(stream_map),
            "v%v.m3u8",
        ]
    )
    return command


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
    target = dict(job.get("target") or {})
    packaging = text(target.get("packaging")) or "single_file"
    decision = text(target.get("decision")) or "transcode"
    capabilities = ffmpeg_capabilities()
    selected_encoder = text(capabilities.get("selected_video_encoder")) or "libx264"
    candidates = [selected_encoder]
    if selected_encoder != "libx264" and bool(capabilities.get("software_fallback")):
        candidates.append("libx264")
    if text(job.get("media_kind")) != "video" or decision == "remux":
        candidates = ["copy"]
    failures: list[str] = []
    for encoder in candidates:
        _clear_materialized_output(target_path)
        if packaging == "hls_cmaf_vod":
            target_path.parent.mkdir(parents=True, exist_ok=True)
            command = _hls_command(
                executable,
                source_path,
                target_path,
                job,
                encoder=encoder,
                limits=limits,
            )
            output_root = target_path.parent
        else:
            partial = target_path.with_suffix(target_path.suffix + ".partial")
            command = _single_file_command(
                executable,
                source_path,
                partial,
                job,
                encoder=encoder,
                limits=limits,
            )
            output_root = partial
        try:
            _run_ffmpeg(
                command,
                output_root=output_root,
                limits=limits,
                cancelled=cancelled,
                cwd=(target_path.parent if packaging == "hls_cmaf_vod" else None),
            )
            if packaging == "hls_cmaf_vod":
                if not target_path.is_file() or not list(
                    target_path.parent.glob("*.m4s")
                ):
                    raise RuntimeError("rendition_output_empty")
            else:
                partial = target_path.with_suffix(target_path.suffix + ".partial")
                if not partial.is_file() or partial.stat().st_size <= 0:
                    raise RuntimeError("rendition_output_empty")
                partial.replace(target_path)
            return {
                "size_bytes": _output_bytes(
                    target_path.parent if packaging == "hls_cmaf_vod" else target_path
                ),
                "mime_type": text(target.get("mime_type"))
                or mimetypes.guess_type(target_path.name)[0]
                or "application/octet-stream",
                "decision": decision,
                "packaging": packaging,
                "video_encoder": encoder,
                "hardware_accelerated": encoder not in {"copy", "libx264"},
                "hardware_backend": text(capabilities.get("hardware_acceleration")),
                "software_fallback_used": bool(failures and encoder == "libx264"),
                "attempt_failures": failures[-2:],
            }
        except RuntimeError as exc:
            failures.append(text(exc)[-1000:])
            if encoder == candidates[-1] or text(exc).startswith(
                ("rendition_canceled", "rendition_timeout", "rendition_output_limit")
            ):
                raise
    raise RuntimeError("rendition_backend_failed")


def publish_derived_resource(path: Path, job: Mapping[str, Any]) -> Mapping[str, Any]:
    from adaos.sdk.io.media import publish_media_file, publish_media_package

    target = dict(job.get("target") or {})
    if text(target.get("packaging")) == "hls_cmaf_vod":
        return publish_media_package(
            path.parent,
            manifest=path.name,
            content_ref=(
                f"rendition:{text(job.get('source_id'))}:"
                f"{int(job.get('source_revision') or 0)}:{text(job.get('profile'))}"
            ),
            namespace="media-library-rendition",
            variant=text(job.get("profile")) or "browser-hls-cmaf-v1",
            mime="application/vnd.apple.mpegurl",
            max_bytes=int(rendition_limits()["max_output_bytes"]),
        )

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
