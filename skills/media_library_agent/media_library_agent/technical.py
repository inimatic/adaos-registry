from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from .contracts import text


TECHNICAL_DESCRIPTOR_SCHEMA = "adaos.media.technical_descriptor.v2"
EMBEDDED_METADATA_REVISION = "1"
MAX_STREAMS = 64
MAX_CHAPTERS = 256


def _tag_values(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [
        text(item)[:500]
        for item in list(values)[:20]
        if text(item)
    ]


def normalize_embedded_metadata(tags: Mapping[str, Any]) -> dict[str, Any]:
    folded = {
        text(key).lower().replace(" ", "_"): value
        for key, value in list(tags.items())[:500]
    }

    def first(*keys: str) -> str:
        for key in keys:
            values = _tag_values(folded.get(key))
            if values:
                return values[0]
        return ""

    def many(*keys: str) -> list[str]:
        for key in keys:
            values = _tag_values(folded.get(key))
            if values:
                return values
        return []

    result: dict[str, Any] = {}
    scalar_fields = {
        "title": ("title",),
        "album": ("album",),
        "album_artist": ("albumartist", "album_artist"),
        "composer": ("composer",),
        "release_date": ("date", "originaldate", "original_date"),
        "language": ("language",),
    }
    for field, keys in scalar_fields.items():
        value = first(*keys)
        if value:
            result[field] = value
    artists = many("artist", "artists")
    if artists:
        result["artists"] = artists
    genres = many("genre", "genres")
    if genres:
        result["genres"] = genres
    for field, keys in (
        ("track_number", ("tracknumber", "track_number")),
        ("disc_number", ("discnumber", "disc_number")),
    ):
        value = first(*keys).split("/", 1)[0].strip()
        if value.isdigit():
            result[field] = int(value)
    date = text(result.get("release_date"))
    year_match = re.match(r"^(?:19|20)\d{2}", date)
    if year_match:
        result["year"] = int(year_match.group(0))
    external_ids = {
        "musicbrainz_recording": first(
            "musicbrainz_trackid", "musicbrainz_recordingid"
        ),
        "musicbrainz_release": first("musicbrainz_albumid"),
        "musicbrainz_artist": first("musicbrainz_artistid"),
    }
    external_ids = {key: value for key, value in external_ids.items() if value}
    if external_ids:
        result["external_ids"] = external_ids
    return result


def read_embedded_metadata(path: Path) -> dict[str, Any]:
    try:
        from mutagen import File as MutagenFile  # type: ignore[import-not-found]

        media = MutagenFile(path, easy=True)
        tags = getattr(media, "tags", None)
    except Exception:
        return {}
    return normalize_embedded_metadata(tags) if isinstance(tags, Mapping) else {}


def _number(value: Any, *, integer: bool = False) -> int | float:
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError):
        parsed = 0.0
    return int(parsed) if integer else round(parsed, 6)


def _frame_rate(value: Any) -> float:
    token = text(value)
    if not token or token == "0/0":
        return 0.0
    try:
        return round(float(Fraction(token)), 6)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _language(tags: Mapping[str, Any]) -> str:
    return text(tags.get("language") or tags.get("LANGUAGE")).lower()


def _disposition(value: Any) -> dict[str, bool]:
    raw = dict(value or {}) if isinstance(value, Mapping) else {}
    names = (
        "default",
        "dub",
        "original",
        "comment",
        "lyrics",
        "karaoke",
        "forced",
        "hearing_impaired",
        "visual_impaired",
        "clean_effects",
        "attached_pic",
        "timed_thumbnails",
    )
    return {name: bool(raw.get(name)) for name in names if bool(raw.get(name))}


def _hdr_mode(stream: Mapping[str, Any]) -> str:
    for item in stream.get("side_data_list") or []:
        if not isinstance(item, Mapping):
            continue
        side_type = text(item.get("side_data_type")).lower()
        if "dovi" in side_type or "dolby vision" in side_type:
            return "dolby_vision"
    transfer = text(stream.get("color_transfer")).lower()
    primaries = text(stream.get("color_primaries")).lower()
    if transfer in {"smpte2084", "pq"}:
        return "hdr10" if primaries == "bt2020" else "pq"
    if transfer in {"arib-std-b67", "hlg"}:
        return "hlg"
    return "sdr"


def _stream_descriptor(stream: Mapping[str, Any]) -> dict[str, Any]:
    tags = dict(stream.get("tags") or {}) if isinstance(stream.get("tags"), Mapping) else {}
    kind = text(stream.get("codec_type")).lower() or "unknown"
    result: dict[str, Any] = {
        "index": max(0, int(stream.get("index") or 0)),
        "kind": kind,
        "codec": text(stream.get("codec_name")).lower(),
        "codec_long_name": text(stream.get("codec_long_name")),
        "codec_tag": text(stream.get("codec_tag_string")).lower(),
        "profile": text(stream.get("profile")),
        "level": max(0, int(stream.get("level") or 0)),
        "bitrate": max(0, int(_number(stream.get("bit_rate"), integer=True))),
        "language": _language(tags),
        "title": text(tags.get("title") or tags.get("handler_name")),
        "disposition": _disposition(stream.get("disposition")),
    }
    if kind == "video":
        result.update(
            {
                "width": max(0, int(stream.get("width") or 0)),
                "height": max(0, int(stream.get("height") or 0)),
                "pixel_format": text(stream.get("pix_fmt")).lower(),
                "bit_depth": max(
                    0,
                    int(
                        stream.get("bits_per_raw_sample")
                        or stream.get("bits_per_sample")
                        or 0
                    ),
                ),
                "frame_rate": _frame_rate(
                    stream.get("avg_frame_rate") or stream.get("r_frame_rate")
                ),
                "field_order": text(stream.get("field_order")).lower(),
                "color_range": text(stream.get("color_range")).lower(),
                "color_space": text(stream.get("color_space")).lower(),
                "color_transfer": text(stream.get("color_transfer")).lower(),
                "color_primaries": text(stream.get("color_primaries")).lower(),
                "chroma_location": text(stream.get("chroma_location")).lower(),
                "hdr_mode": _hdr_mode(stream),
            }
        )
    elif kind == "audio":
        result.update(
            {
                "sample_rate": max(0, int(stream.get("sample_rate") or 0)),
                "channels": max(0, int(stream.get("channels") or 0)),
                "channel_layout": text(stream.get("channel_layout")).lower(),
                "sample_format": text(stream.get("sample_fmt")).lower(),
                "bit_depth": max(
                    0,
                    int(
                        stream.get("bits_per_raw_sample")
                        or stream.get("bits_per_sample")
                        or 0
                    ),
                ),
            }
        )
    return result


def _chapter_descriptor(chapter: Mapping[str, Any]) -> dict[str, Any]:
    tags = dict(chapter.get("tags") or {}) if isinstance(chapter.get("tags"), Mapping) else {}
    return {
        "id": max(0, int(chapter.get("id") or 0)),
        "start_seconds": max(0.0, float(_number(chapter.get("start_time")))),
        "end_seconds": max(0.0, float(_number(chapter.get("end_time")))),
        "title": text(tags.get("title")),
    }


def basic_descriptor(path: Path, *, stat: os.stat_result) -> dict[str, Any]:
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    container = path.suffix.lower().lstrip(".")
    return {
        "schema": TECHNICAL_DESCRIPTOR_SCHEMA,
        "probe": "basic",
        "probe_status": "partial",
        "file_container": container,
        "container": container,
        "containers": [container] if container else [],
        "mime_type": mime_type,
        "size_bytes": int(stat.st_size),
        "duration_seconds": 0.0,
        "bitrate": 0,
        "streams": [],
        "stream_counts": {"video": 0, "audio": 0, "subtitle": 0, "attachment": 0},
        "chapters": [],
        "chapter_count": 0,
        "hdr_modes": [],
    }


def probe_media(
    path: Path,
    *,
    stat: os.stat_result,
    executable: str | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    result = basic_descriptor(path, stat=stat)
    binary = executable or shutil.which("ffprobe")
    if not binary:
        return result
    timeout = timeout_seconds
    if timeout is None:
        try:
            timeout = float(os.environ.get("MEDIA_LIBRARY_AGENT_PROBE_TIMEOUT_SECONDS") or 10)
        except ValueError:
            timeout = 10
    timeout = max(1.0, min(float(timeout), 60.0))
    try:
        completed = subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_format",
                "-show_streams",
                "-show_chapters",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
            return result | {"probe": "ffprobe", "probe_status": "failed"}
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except Exception:
        return result | {"probe": "ffprobe", "probe_status": "failed"}

    raw_streams = payload.get("streams") or []
    raw_chapters = payload.get("chapters") or []
    streams = [
        _stream_descriptor(item)
        for item in raw_streams[:MAX_STREAMS]
        if isinstance(item, Mapping)
    ]
    chapters = [
        _chapter_descriptor(item)
        for item in raw_chapters[:MAX_CHAPTERS]
        if isinstance(item, Mapping)
    ]
    format_value = (
        dict(payload.get("format") or {})
        if isinstance(payload.get("format"), Mapping)
        else {}
    )
    containers = [
        token.strip().lower()
        for token in text(format_value.get("format_name")).split(",")
        if token.strip()
    ]
    counts = {
        kind: sum(1 for item in streams if item["kind"] == kind)
        for kind in ("video", "audio", "subtitle", "attachment")
    }
    video = next((item for item in streams if item["kind"] == "video"), {})
    audio = next((item for item in streams if item["kind"] == "audio"), {})
    hdr_modes = sorted(
        {
            text(item.get("hdr_mode"))
            for item in streams
            if item.get("kind") == "video" and text(item.get("hdr_mode")) != "sdr"
        }
    )
    return result | {
        "probe": "ffprobe",
        "probe_status": "complete",
        "container": containers[0] if containers else result["container"],
        "containers": containers or result["containers"],
        "format": text(format_value.get("format_long_name")),
        "duration_seconds": max(0.0, float(_number(format_value.get("duration")))),
        "bitrate": max(0, int(_number(format_value.get("bit_rate"), integer=True))),
        "streams": streams,
        "streams_truncated": len(raw_streams) > MAX_STREAMS,
        "stream_counts": counts,
        "chapters": chapters,
        "chapter_count": len(raw_chapters),
        "chapters_truncated": len(raw_chapters) > MAX_CHAPTERS,
        "hdr_modes": hdr_modes,
        # Compatibility mirrors for rolling-upgrade consumers.
        "codec": text(video.get("codec") or audio.get("codec")),
        "width": max(0, int(video.get("width") or 0)),
        "height": max(0, int(video.get("height") or 0)),
        "sample_rate": max(0, int(audio.get("sample_rate") or 0)),
        "channels": max(0, int(audio.get("channels") or 0)),
    }


__all__ = [
    "EMBEDDED_METADATA_REVISION",
    "TECHNICAL_DESCRIPTOR_SCHEMA",
    "basic_descriptor",
    "normalize_embedded_metadata",
    "probe_media",
    "read_embedded_metadata",
]
