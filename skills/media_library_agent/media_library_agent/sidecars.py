from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

from .contracts import text


NFO_SCHEMA = "adaos.media.local_nfo.v1"
MAX_NFO_BYTES = 512 * 1024
MAX_LIST_VALUES = 100


def _candidate_paths(media_path: Path) -> list[Path]:
    names = [
        media_path.with_suffix(".nfo"),
        media_path.parent / "movie.nfo",
        media_path.parent / "episode.nfo",
        media_path.parent / "album.nfo",
        media_path.parent / "artist.nfo",
        media_path.parent / "tvshow.nfo",
    ]
    result: list[Path] = []
    for candidate in names:
        if candidate not in result and candidate.is_file():
            result.append(candidate)
    return result[:4]


def nfo_witness(media_path: Path) -> list[dict[str, Any]]:
    result = []
    for candidate in _candidate_paths(media_path):
        try:
            stat = candidate.stat()
        except OSError:
            continue
        result.append(
            {
                "name": candidate.name,
                "size_bytes": int(stat.st_size),
                "modified_ns": int(stat.st_mtime_ns),
            }
        )
    return result


def _first(root: ElementTree.Element, *names: str) -> str:
    for name in names:
        node = root.find(name)
        value = text(node.text if node is not None else "")
        if value:
            return value
    return ""


def _values(root: ElementTree.Element, *names: str) -> list[str]:
    result: list[str] = []
    for name in names:
        for node in root.findall(name)[:MAX_LIST_VALUES]:
            value = text(node.text)
            if value and value not in result:
                result.append(value)
    return result[:MAX_LIST_VALUES]


def _number(value: str, *, floating: bool = False) -> int | float | None:
    try:
        return float(value) if floating else int(float(value))
    except (TypeError, ValueError):
        return None


def _parse(candidate: Path) -> dict[str, Any]:
    payload = candidate.read_bytes()
    if not payload or len(payload) > MAX_NFO_BYTES:
        raise ValueError("nfo_size_invalid")
    root = ElementTree.fromstring(payload)
    result: dict[str, Any] = {
        "media_type": root.tag.lower(),
        "title": _first(root, "title"),
        "original_title": _first(root, "originaltitle", "original_title"),
        "sort_title": _first(root, "sorttitle", "sort_title"),
        "plot": _first(root, "plot", "outline"),
        "tagline": _first(root, "tagline"),
        "release_date": _first(root, "premiered", "releasedate", "aired"),
        "content_rating": _first(root, "mpaa", "certification"),
        "studio": _first(root, "studio", "label"),
        "edition": _first(root, "edition"),
        "album": _first(root, "album"),
        "series": _first(root, "showtitle", "series"),
        "genres": _values(root, "genre"),
        "tags": _values(root, "tag"),
        "countries": _values(root, "country"),
        "artists": _values(root, "artist", "albumartist"),
        "directors": _values(root, "director"),
    }
    numeric = {
        "year": _number(_first(root, "year")),
        "rating": _number(_first(root, "rating", "userrating"), floating=True),
        "votes": _number(_first(root, "votes")),
        "runtime_minutes": _number(_first(root, "runtime")),
        "season": _number(_first(root, "season")),
        "episode": _number(_first(root, "episode")),
        "track": _number(_first(root, "track")),
        "disc": _number(_first(root, "disc")),
    }
    result.update({key: value for key, value in numeric.items() if value is not None})
    unique_ids: dict[str, str] = {}
    for node in root.findall("uniqueid")[:20]:
        value = text(node.text)
        provider = text(node.attrib.get("type") or "default").lower()
        if value and provider:
            unique_ids[provider] = value
    for provider in ("imdb", "tmdb", "tvdb", "musicbrainz"):
        value = _first(root, f"{provider}id", f"{provider}_id")
        if value:
            unique_ids[provider] = value
    if unique_ids:
        result["external_ids"] = unique_ids
    actors = []
    for node in root.findall("actor")[:MAX_LIST_VALUES]:
        name = _first(node, "name")
        if name:
            actors.append(
                {
                    "name": name,
                    "role": _first(node, "role"),
                    "order": _number(_first(node, "order")) or len(actors),
                }
            )
    if actors:
        result["actors"] = actors
    artwork = []
    for node in root.findall("thumb")[:20]:
        url = text(node.text)
        if url.startswith(("http://", "https://")):
            artwork.append(
                {
                    "kind": text(node.attrib.get("aspect") or "poster"),
                    "url": url,
                }
            )
    fanart = root.find("fanart")
    if fanart is not None:
        for node in fanart.findall("thumb")[:20]:
            url = text(node.text)
            if url.startswith(("http://", "https://")):
                artwork.append({"kind": "backdrop", "url": url})
    if artwork:
        result["artwork_candidates"] = artwork
    return {key: value for key, value in result.items() if value not in ("", [], {})}


def read_local_nfo(media_path: Path) -> dict[str, Any] | None:
    documents = []
    errors = []
    for candidate in _candidate_paths(media_path):
        try:
            values = _parse(candidate)
        except (OSError, ElementTree.ParseError, ValueError) as exc:
            errors.append({"name": candidate.name, "error": type(exc).__name__})
            continue
        documents.append({"name": candidate.name, "values": values})
    if not documents and not errors:
        return None
    merged: dict[str, Any] = {}
    for document in reversed(documents):
        merged.update(document["values"])
    witness = nfo_witness(media_path)
    digest = hashlib.sha256(
        repr((witness, documents)).encode("utf-8", errors="replace")
    ).hexdigest()[:24]
    return {
        "schema": NFO_SCHEMA,
        "state": "ready" if documents else "failed",
        "values": merged,
        "documents": documents,
        "errors": errors,
        "witness": witness,
        "revision_witness": digest,
    }


__all__ = ["MAX_NFO_BYTES", "NFO_SCHEMA", "nfo_witness", "read_local_nfo"]
