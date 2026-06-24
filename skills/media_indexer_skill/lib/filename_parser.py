"""Lightweight media filename entity extraction.

This parser is intentionally deterministic and dependency-free. It gives the
index useful title/artist/year/quality fields even when the optional ML NER
worker is disabled or fails.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

QUALITY_RE = re.compile(
    r"(?ix)\b("
    r"2160p|1440p|1080p|720p|480p|4k|8k|uhd|hdr10|hdr|dv|dolby[ ._-]?vision|"
    r"web[ ._-]?dl|web[ ._-]?rip|b[rd][ ._-]?rip|blu[ ._-]?ray|hdtv|hdrip|dvdrip|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|aac|flac|mp3|opus"
    r")\b"
)
YEAR_RE = re.compile(r"(?<!\d)(19[0-9]{2}|20[0-9]{2})(?!\d)")
TRACK_PREFIX_RE = re.compile(r"^\s*(?:\d{1,3}[\s._-]+|cd\s*\d+[\s._-]+|track\s*\d+[\s._-]+)", re.IGNORECASE)
BRACKET_RE = re.compile(r"[\[\]\(\)\{\}]")
SEPARATOR_RE = re.compile(r"[._]+")
WHITESPACE_RE = re.compile(r"\s+")

DROP_TOKENS = {
    "proper",
    "repack",
    "remastered",
    "extended",
    "director",
    "directors",
    "cut",
    "final",
    "limited",
    "internal",
    "multi",
    "subs",
    "subbed",
    "dubbed",
    "webrip",
    "webdl",
    "web",
    "dl",
    "bluray",
    "brrip",
    "bdrip",
    "hdrip",
    "hdtv",
    "dvdrip",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "aac",
    "flac",
    "mp3",
    "opus",
}


def parse_filename(name: str, media_type: str = "media") -> Dict[str, str]:
    stem = Path(str(name or "")).stem.strip()
    if not stem:
        return {}

    raw = _normalize_separators(stem)
    quality = _first_quality(raw)
    year = _first_year(raw)
    artist = ""
    title = ""

    if str(media_type or "").lower() == "audio":
        artist, title = _parse_audio_title(raw)
    else:
        title = _parse_visual_title(raw, year=year, quality=quality)

    if not title and str(media_type or "").lower() != "audio":
        title = _clean_title(raw)
    if str(media_type or "").lower() == "audio" and not title:
        title = _clean_title(raw)

    result: Dict[str, str] = {
        "title": title,
        "year": year,
        "quality": quality,
        "artist": artist,
        "source": "rules",
    }
    return {key: value for key, value in result.items() if value}


def merge_entities(base: Dict[str, Any], override: Dict[str, Any] | None) -> Dict[str, str]:
    merged = {key: str(value).strip() for key, value in (base or {}).items() if str(value or "").strip()}
    applied = False
    for key in ("title", "year", "quality", "artist"):
        value = str((override or {}).get(key) or "").strip()
        if value and value != "---":
            merged[key] = value
            applied = True
    if applied:
        merged["source"] = "ml+rules"
    elif merged:
        merged.setdefault("source", "rules")
    return merged


def _normalize_separators(value: str) -> str:
    text = BRACKET_RE.sub(" ", value)
    text = SEPARATOR_RE.sub(" ", text)
    text = re.sub(r"\s+-\s+", " - ", text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text


def _first_quality(value: str) -> str:
    match = QUALITY_RE.search(value)
    if not match:
        return ""
    return _canonical_quality(match.group(1))


def _canonical_quality(value: str) -> str:
    raw = re.sub(r"[\s._]+", "-", str(value or "").strip())
    replacements = {
        "web-dl": "WEB-DL",
        "web-rip": "WEB-Rip",
        "bluray": "BluRay",
        "blu-ray": "BluRay",
        "brrip": "BRRip",
        "bdrip": "BDRip",
        "hdrip": "HDRip",
        "hdtv": "HDTV",
        "dvdrip": "DVDRip",
        "uhd": "UHD",
        "hdr": "HDR",
        "hdr10": "HDR10",
        "dolby-vision": "Dolby Vision",
        "dv": "DV",
        "hevc": "HEVC",
        "avc": "AVC",
        "aac": "AAC",
        "flac": "FLAC",
        "mp3": "MP3",
        "opus": "Opus",
    }
    lowered = raw.lower()
    return replacements.get(lowered, raw.upper() if lowered in {"4k", "8k"} else raw)


def _first_year(value: str) -> str:
    match = YEAR_RE.search(value)
    return match.group(1) if match else ""


def _parse_audio_title(value: str) -> tuple[str, str]:
    text = TRACK_PREFIX_RE.sub("", value).strip()
    parts = re.split(r"\s+-\s+| - ", text, maxsplit=1)
    if len(parts) == 2:
        artist = _clean_title(parts[0])
        title = _clean_title(parts[1])
        return artist, title
    return "", _clean_title(text)


def _parse_visual_title(value: str, *, year: str, quality: str) -> str:
    text = value
    if year:
        match = YEAR_RE.search(text)
        if match:
            text = text[: match.start()]
    elif quality:
        match = QUALITY_RE.search(text)
        if match:
            text = text[: match.start()]
    return _clean_title(text)


def _clean_title(value: str) -> str:
    text = TRACK_PREFIX_RE.sub("", str(value or ""))
    text = QUALITY_RE.sub(" ", text)
    text = YEAR_RE.sub(" ", text)
    text = text.replace("-", " ")
    words = []
    for raw in WHITESPACE_RE.sub(" ", text).strip().split(" "):
        token = raw.strip(" ._-")
        if not token:
            continue
        folded = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "", token).lower()
        if folded in DROP_TOKENS:
            continue
        words.append(token)
    return WHITESPACE_RE.sub(" ", " ".join(words)).strip()
