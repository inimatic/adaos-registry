from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from .catalog import (
    MediaCenterRepository,
    _json_dumps,
    _json_loads,
    _media_kind,
    _public_item,
    _text,
    _title_from_name,
    now_iso,
)


COORDINATOR_SCHEMA = "adaos.media_center.coordinator.v2"
CATALOG_ITEM_SCHEMA = "adaos.media_center.media_source.v1"
WORK_SCHEMA = "adaos.media_center.media_work.v1"
COLLECTION_SCHEMA = "adaos.media_center.media_collection.v1"
PERSONAL_SCHEMA = "adaos.media_center.personal_state.v1"
FOLDER_NODE_SCHEMA = "adaos.media_center.folder_node.v1"
PLAYLIST_SCHEMA = "adaos.media_center.playlist.v1"
CORRECTION_SCHEMA = "adaos.media_center.catalog_correction.v1"
MAX_PAGE_SIZE = 30

_SEASON_EPISODE = re.compile(r"(?i)(?:^|[ ._\-])s(?P<season>\d{1,3})e(?P<episode>\d{1,4})(?:[ ._\-]|$)")
_SEASON_FOLDER = re.compile(r"(?i)^(?:season|сезон)[ ._\-]*(?P<season>\d{1,3})$")
_DISC_FOLDER = re.compile(r"(?i)^(?:disc|disk|cd|диск)[ ._\-]*(?P<disc>\d{1,3})$")
_PART_FOLDER = re.compile(r"(?i)^(?:part|book|том|часть)[ ._\-]*(?P<part>\d{1,3})$")
_LEADING_NUMBER = re.compile(r"^(?P<number>\d{1,4})(?:[ ._\-]+|$)")


def _stable_id(prefix: str, *parts: Any, size: int = 24) -> str:
    raw = "\0".join(_text(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()[:size]}"


def _normalize_title(value: Any) -> str:
    token = Path(_text(value)).stem
    token = _SEASON_EPISODE.sub(" ", token)
    token = _LEADING_NUMBER.sub("", token)
    token = re.sub(r"[._\-]+", " ", token)
    return " ".join(token.split()).strip() or _title_from_name(_text(value))


def _cursor_signature(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _encode_cursor(offset: int, signature: str) -> str:
    raw = json.dumps({"v": 1, "offset": max(0, int(offset)), "sig": signature}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: Any, signature: str) -> int:
    token = _text(value)
    if not token:
        return 0
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("v") != 1 or payload.get("sig") != signature:
            raise ValueError("cursor does not match query")
        return max(0, int(payload["offset"]))
    except Exception as exc:
        raise ValueError("invalid_media_catalog_cursor") from exc


class MediaCatalogCoordinator:
    """Global catalog read model fed by idempotent node-agent deltas."""

    def __init__(self, repository: MediaCenterRepository):
        self.repository = repository
        self.ensure_schema()

    def ensure_schema(self) -> dict[str, Any]:
        with self.repository.connect() as connection:
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(catalog_items)").fetchall()}
            additions = {
                "agent_id": "TEXT NOT NULL DEFAULT ''",
                "node_id": "TEXT NOT NULL DEFAULT ''",
                "root_id": "TEXT NOT NULL DEFAULT ''",
                "source_id": "TEXT NOT NULL DEFAULT ''",
                "source_revision": "INTEGER NOT NULL DEFAULT 0",
                "folder_path": "TEXT NOT NULL DEFAULT ''",
                "search_text": "TEXT NOT NULL DEFAULT ''",
                "catalog_revision": "INTEGER NOT NULL DEFAULT 0",
                "work_id": "TEXT NOT NULL DEFAULT ''",
                "variant_id": "TEXT NOT NULL DEFAULT ''",
                "collection_id": "TEXT NOT NULL DEFAULT ''",
                "quality_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE catalog_items ADD COLUMN {name} {definition}")
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_media_center_agent_source ON catalog_items(agent_id, source_id);
                CREATE INDEX IF NOT EXISTS idx_media_center_work ON catalog_items(work_id, missing);
                CREATE INDEX IF NOT EXISTS idx_media_center_collection ON catalog_items(collection_id, missing);
                CREATE INDEX IF NOT EXISTS idx_media_center_folder_browse
                    ON catalog_items(agent_id, root_id, folder_path, missing);
                CREATE TABLE IF NOT EXISTS coordinator_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS media_works (
                    id TEXT PRIMARY KEY,
                    schema_name TEXT NOT NULL,
                    media_kind TEXT NOT NULL,
                    canonical_title TEXT NOT NULL,
                    sort_title TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    alias_of TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS media_variants (
                    id TEXT PRIMARY KEY,
                    work_id TEXT NOT NULL REFERENCES media_works(id),
                    source_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    media_kind TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    quality_json TEXT NOT NULL DEFAULT '{}',
                    available INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(source_id, node_id)
                );
                CREATE TABLE IF NOT EXISTS media_collections (
                    id TEXT PRIMARY KEY,
                    schema_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    parent_id TEXT NOT NULL DEFAULT '',
                    ownership TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_memberships (
                    collection_id TEXT NOT NULL REFERENCES media_collections(id),
                    work_id TEXT NOT NULL REFERENCES media_works(id),
                    variant_id TEXT NOT NULL DEFAULT '',
                    ordinal INTEGER NOT NULL DEFAULT 0,
                    season_number INTEGER,
                    episode_number INTEGER,
                    disc_number INTEGER,
                    track_number INTEGER,
                    chapter_number INTEGER,
                    revision INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(collection_id, work_id, variant_id)
                );
                CREATE TABLE IF NOT EXISTS metadata_claims (
                    id TEXT PRIMARY KEY,
                    subject_ref TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    preferred INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_aliases (
                    alias_id TEXT PRIMARY KEY,
                    canonical_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    reversible INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_catalog_state (
                    agent_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL DEFAULT '',
                    node_id TEXT NOT NULL,
                    cursor TEXT NOT NULL DEFAULT '',
                    last_sequence INTEGER NOT NULL DEFAULT 0,
                    availability TEXT NOT NULL DEFAULT 'unknown',
                    freshness TEXT NOT NULL DEFAULT 'unknown',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS personal_media_state (
                    profile_id TEXT NOT NULL,
                    item_id TEXT NOT NULL REFERENCES catalog_items(id) ON DELETE CASCADE,
                    favorite INTEGER NOT NULL DEFAULT 0,
                    resume_ms INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    play_count INTEGER NOT NULL DEFAULT 0,
                    last_played_at TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id, item_id)
                );
                CREATE INDEX IF NOT EXISTS idx_personal_media_recent ON personal_media_state(profile_id, last_played_at DESC);
                CREATE TABLE IF NOT EXISTS user_playlists (
                    id TEXT PRIMARY KEY,
                    owner_profile_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    visibility TEXT NOT NULL DEFAULT 'private',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_playlist_items (
                    playlist_id TEXT NOT NULL REFERENCES user_playlists(id) ON DELETE CASCADE,
                    item_id TEXT NOT NULL REFERENCES catalog_items(id),
                    ordinal INTEGER NOT NULL,
                    added_by_profile_id TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY(playlist_id, item_id)
                );
                CREATE TABLE IF NOT EXISTS catalog_corrections (
                    id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    subject_ref TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    reversed_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS media_background_jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    subject_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            job_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(media_background_jobs)"
                ).fetchall()
            }
            for name, definition in {
                "provider_id": "TEXT NOT NULL DEFAULT ''",
                "started_at": "TEXT NOT NULL DEFAULT ''",
                "finished_at": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in job_columns:
                    connection.execute(
                        f"ALTER TABLE media_background_jobs ADD COLUMN {name} {definition}"
                    )
            agent_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(agent_catalog_state)"
                ).fetchall()
            }
            if "instance_id" not in agent_columns:
                connection.execute(
                    "ALTER TABLE agent_catalog_state ADD COLUMN instance_id TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS catalog_search USING fts5(item_id UNINDEXED, text, tokenize='unicode61 remove_diacritics 2')"
            )
            connection.execute("INSERT OR REPLACE INTO coordinator_meta(key, value) VALUES ('schema_version', ?)", (COORDINATOR_SCHEMA,))
            self._backfill_search(connection)
            connection.commit()
        return {"ok": True, "schema": COORDINATOR_SCHEMA, "db_path": str(self.repository.db_path)}

    def _backfill_search(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT id, title, name, source_path, folder_path, metadata_json
            FROM catalog_items
            WHERE search_text=''
            """
        ).fetchall()
        updates: list[tuple[str, str]] = []
        search_rows: list[tuple[str, str]] = []
        for row in rows:
            metadata = _json_loads(row["metadata_json"])
            search_text = self._search_text(
                title=row["title"],
                name=row["name"],
                relative_path=row["source_path"],
                folder_path=row["folder_path"],
                metadata=metadata if isinstance(metadata, Mapping) else {},
            )
            item_id = str(row["id"])
            updates.append((search_text, item_id))
            search_rows.append((item_id, search_text))
        if not updates:
            return
        connection.executemany("UPDATE catalog_items SET search_text=? WHERE id=?", updates)
        for start in range(0, len(search_rows), 400):
            batch = search_rows[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            connection.execute(
                f"DELETE FROM catalog_search WHERE item_id IN ({placeholders})",
                tuple(item_id for item_id, _value in batch),
            )
        connection.executemany("INSERT INTO catalog_search(item_id, text) VALUES (?, ?)", search_rows)

    @staticmethod
    def _replace_search(connection: sqlite3.Connection, item_id: str, search_text: str) -> None:
        connection.execute("DELETE FROM catalog_search WHERE item_id=?", (item_id,))
        connection.execute("INSERT INTO catalog_search(item_id, text) VALUES (?, ?)", (item_id, search_text))

    @staticmethod
    def _search_text(*, title: Any, name: Any, relative_path: Any, folder_path: Any, metadata: Mapping[str, Any]) -> str:
        values: list[str] = [_text(title), _text(name), _text(relative_path), _text(folder_path)]
        for key in ("folder_segments", "tags", "artists", "people", "aliases"):
            value = metadata.get(key)
            if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
                values.extend(_text(item) for item in value)
            elif value:
                values.append(_text(value))
        values.extend([_text(metadata.get("album")), _text(metadata.get("series")), _text(metadata.get("root_label"))])
        return " ".join(part for part in values if part)

    def apply_agent_page(
        self, page: Mapping[str, Any], *, instance_id: str = ""
    ) -> dict[str, Any]:
        agent = page.get("agent") if isinstance(page.get("agent"), Mapping) else {}
        agent_id = _text(agent.get("id"))
        node_id = _text(agent.get("node_id"))
        if not agent_id:
            return {"ok": False, "error": "agent_identity_required", "schema": COORDINATOR_SCHEMA}
        items = [dict(item) for item in page.get("items") or [] if isinstance(item, Mapping)]
        applied = ignored = removed = 0
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for delta in items:
                outcome = self._apply_delta(connection, delta, agent_id=agent_id, node_id=node_id)
                if outcome == "ignored":
                    ignored += 1
                else:
                    applied += 1
                    removed += int(outcome == "removed")
            last_sequence = max([int(item.get("sequence") or 0) for item in items] or [0])
            connection.execute(
                """
                INSERT INTO agent_catalog_state(agent_id, instance_id, node_id, cursor, last_sequence, availability, freshness, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, 'available', 'fresh', '', ?)
                ON CONFLICT(agent_id) DO UPDATE SET instance_id=excluded.instance_id,
                    node_id=excluded.node_id, cursor=excluded.cursor,
                    last_sequence=MAX(agent_catalog_state.last_sequence, excluded.last_sequence),
                    availability='available', freshness='fresh', last_error='', updated_at=excluded.updated_at
                """,
                (
                    agent_id,
                    _text(instance_id),
                    node_id,
                    _text(page.get("next_cursor")),
                    last_sequence,
                    now_iso(),
                ),
            )
            connection.commit()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "agent_id": agent_id,
            "applied_count": applied,
            "ignored_count": ignored,
            "removed_count": removed,
            "next_cursor": _text(page.get("next_cursor")),
            "has_more": bool(page.get("has_more")),
            "catalog_revision": self.catalog_revision(),
        }

    def _apply_delta(self, connection: sqlite3.Connection, delta: Mapping[str, Any], *, agent_id: str, node_id: str) -> str:
        source = delta.get("source") if isinstance(delta.get("source"), Mapping) else {}
        source_id = _text(delta.get("source_id") or source.get("id"))
        source_revision = int(delta.get("source_revision") or source.get("revision") or 0)
        if not source_id or source_revision < 1:
            return "ignored"
        previous = connection.execute(
            "SELECT id, source_revision FROM catalog_items WHERE agent_id=? AND source_id=?",
            (agent_id, source_id),
        ).fetchone()
        if previous and int(previous["source_revision"] or 0) >= source_revision:
            return "ignored"
        operation = _text(delta.get("operation")).lower()
        if operation == "removed":
            if previous:
                revision = self._next_catalog_revision(connection)
                connection.execute(
                    "UPDATE catalog_items SET missing=1, source_revision=?, catalog_revision=?, last_seen_at=? WHERE id=?",
                    (source_revision, revision, now_iso(), str(previous["id"])),
                )
                connection.execute("UPDATE media_variants SET available=0, revision=revision+1 WHERE source_id=? AND node_id=?", (source_id, node_id))
            return "removed"

        descriptor = dict(source.get("descriptor") or {})
        metadata = dict(source.get("metadata") or descriptor.get("metadata") or {})
        name = _text(source.get("name") or descriptor.get("name"))
        if not name:
            return "ignored"
        relative_path = _text(source.get("relative_path") or metadata.get("relative_path") or name).replace("\\", "/")
        folder_path = _text(source.get("folder_path") or metadata.get("folder_path")).replace("\\", "/").strip("/")
        mime_type = _text(source.get("mime_type") or descriptor.get("mime_type") or descriptor.get("mime")) or "application/octet-stream"
        kind = _text(source.get("media_kind")) or _media_kind(mime_type, name)
        title = _text(descriptor.get("title")) or _title_from_name(name)
        work, collections, membership = self._classify_source(
            name, kind, folder_path, metadata
        )
        work_id = self._upsert_work(connection, work)
        collection_ids: list[str] = []
        for collection in collections:
            value = dict(collection)
            parent_index = value.pop("parent_index", None)
            if parent_index is not None:
                value["parent_id"] = collection_ids[int(parent_index)]
            collection_ids.append(self._upsert_collection(connection, value))
        collection_id = collection_ids[-1] if collection_ids else ""
        variant_id = _stable_id("variant", work_id, node_id, source_id, size=24)
        item_id = str(previous["id"]) if previous else _stable_id("mc", agent_id, source_id, size=24)
        content_path = _text(descriptor.get("content_path"))
        routed_path = _text(descriptor.get("routed_content_path") or descriptor.get("browser_path"))
        source_path = _text(descriptor.get("source_path") or descriptor.get("path"))
        modified_ns = int(source.get("modified_ns") or 0)
        modified_at = _text(descriptor.get("modified_at")) or (str(modified_ns) if modified_ns else "")
        search_text = self._search_text(
            title=title,
            name=name,
            relative_path=relative_path,
            folder_path=folder_path,
            metadata={
                **metadata,
                "collection": " ".join(
                    _text(item.get("title")) for item in collections
                ),
            },
        )
        catalog_revision = self._next_catalog_revision(connection)
        connection.execute(
            """
            INSERT INTO catalog_items(
                id, source, resource_id, name, title, media_kind, mime_type, size_bytes,
                modified_at, content_path, routed_content_path, playback_id, source_path,
                descriptor_json, metadata_json, fingerprint, indexed_at, last_seen_at,
                missing, favorite, play_count, tags_json, agent_id, node_id, root_id,
                source_id, source_revision, folder_path, search_text, catalog_revision,
                work_id, variant_id, collection_id, quality_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, '[]', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET source=excluded.source, resource_id=excluded.resource_id,
                name=excluded.name, title=excluded.title, media_kind=excluded.media_kind,
                mime_type=excluded.mime_type, size_bytes=excluded.size_bytes,
                modified_at=excluded.modified_at, content_path=excluded.content_path,
                routed_content_path=excluded.routed_content_path, playback_id=excluded.playback_id,
                source_path=excluded.source_path, descriptor_json=excluded.descriptor_json,
                metadata_json=excluded.metadata_json, fingerprint=excluded.fingerprint,
                indexed_at=excluded.indexed_at, last_seen_at=excluded.last_seen_at,
                missing=0, agent_id=excluded.agent_id, node_id=excluded.node_id,
                root_id=excluded.root_id, source_id=excluded.source_id,
                source_revision=excluded.source_revision, folder_path=excluded.folder_path,
                search_text=excluded.search_text, catalog_revision=excluded.catalog_revision,
                work_id=excluded.work_id, variant_id=excluded.variant_id,
                collection_id=excluded.collection_id, quality_json=excluded.quality_json
            """,
            (
                item_id, f"agent:{agent_id}", _text(source.get("resource_id") or descriptor.get("resource_id") or descriptor.get("id")),
                name, title, kind, mime_type, int(source.get("size_bytes") or descriptor.get("size_bytes") or 0),
                modified_at, content_path, routed_path, _text(descriptor.get("playback_id")), source_path,
                _json_dumps(descriptor), _json_dumps(metadata), _text(source.get("fingerprint")), now_iso(), now_iso(),
                agent_id, node_id, _text(source.get("root_id") or delta.get("root_id")), source_id, source_revision,
                folder_path, search_text, catalog_revision, work_id, variant_id, collection_id,
                _json_dumps(self._quality(descriptor, metadata)),
            ),
        )
        self._replace_search(connection, item_id, search_text)
        connection.execute(
            """
            INSERT INTO media_variants(id, work_id, source_id, node_id, media_kind, mime_type, quality_json, available, revision)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1)
            ON CONFLICT(id) DO UPDATE SET work_id=excluded.work_id, media_kind=excluded.media_kind,
                mime_type=excluded.mime_type, quality_json=excluded.quality_json, available=1, revision=media_variants.revision+1
            """,
            (variant_id, work_id, source_id, node_id, kind, mime_type, _json_dumps(self._quality(descriptor, metadata))),
        )
        for membership_collection_id in collection_ids:
            connection.execute(
                """
                INSERT INTO collection_memberships(
                    collection_id, work_id, variant_id, ordinal, season_number, episode_number,
                    disc_number, track_number, chapter_number, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(collection_id, work_id, variant_id) DO UPDATE SET ordinal=excluded.ordinal,
                    season_number=excluded.season_number, episode_number=excluded.episode_number,
                    disc_number=excluded.disc_number, track_number=excluded.track_number,
                    chapter_number=excluded.chapter_number, revision=collection_memberships.revision+1
                """,
                (
                    membership_collection_id, work_id, variant_id, int(membership.get("ordinal") or 0),
                    membership.get("season_number"), membership.get("episode_number"), membership.get("disc_number"),
                    membership.get("track_number"), membership.get("chapter_number"),
                ),
            )
        enrichment_job_id = _stable_id(
            "mediajob", "metadata_enrichment", item_id, source_revision, size=24
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO media_background_jobs(
                id,kind,subject_ref,status,priority,created_at,updated_at
            ) VALUES (?, 'metadata_enrichment', ?, 'queued', 200, ?, ?)
            """,
            (enrichment_job_id, f"item:{item_id}", now_iso(), now_iso()),
        )
        return operation or "updated"

    @staticmethod
    def _quality(descriptor: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any]:
        technical = metadata.get("technical") if isinstance(metadata.get("technical"), Mapping) else {}
        return {
            "width": int(technical.get("width") or descriptor.get("width") or 0),
            "height": int(technical.get("height") or descriptor.get("height") or 0),
            "bitrate": int(technical.get("bitrate") or descriptor.get("bitrate") or 0),
            "codec": _text(technical.get("codec") or descriptor.get("codec")),
            "language": _text(metadata.get("language")),
        }

    def _classify_source(
        self, name: str, media_kind: str, folder_path: str, metadata: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        parts = [part for part in folder_path.split("/") if part]
        match = _SEASON_EPISODE.search(name)
        membership: dict[str, Any] = {"ordinal": 0}
        collections: list[dict[str, Any]] = []
        canonical_title = _normalize_title(name)
        if media_kind == "video" and match:
            season = int(match.group("season"))
            episode = int(match.group("episode"))
            series_title = parts[-2] if len(parts) >= 2 and _SEASON_FOLDER.match(parts[-1]) else (parts[-1] if parts else canonical_title)
            canonical_title = f"{series_title} S{season:02d}E{episode:02d}"
            collections = [
                {
                    "kind": "series",
                    "title": series_title,
                    "parent_id": "",
                    "ownership": "derived",
                },
                {
                    "kind": "season",
                    "title": f"Season {season}",
                    "parent_index": 0,
                    "ownership": "derived",
                    "metadata": {"season_number": season},
                },
            ]
            membership.update({"ordinal": season * 10000 + episode, "season_number": season, "episode_number": episode})
        elif media_kind == "audio" and parts:
            disc_match = _DISC_FOLDER.match(parts[-1]) if parts else None
            part_match = _PART_FOLDER.match(parts[-1]) if parts else None
            container_title = parts[-2] if len(parts) >= 2 and (disc_match or part_match) else parts[-1]
            album_title = _text(metadata.get("album")) or container_title
            number_match = _LEADING_NUMBER.match(Path(name).stem)
            ordinal = int(number_match.group("number")) if number_match else 0
            kind = (
                "audiobook"
                if len(parts) >= 2
                and number_match
                and not metadata.get("album")
                and not disc_match
                else "album"
            )
            collections = [
                {
                    "kind": kind,
                    "title": album_title,
                    "parent_id": "",
                    "ownership": "derived",
                }
            ]
            if disc_match:
                disc = int(disc_match.group("disc"))
                collections.append(
                    {
                        "kind": "disc",
                        "title": f"Disc {disc}",
                        "parent_index": 0,
                        "ownership": "derived",
                        "metadata": {"disc_number": disc},
                    }
                )
                membership["disc_number"] = disc
            elif part_match:
                part = int(part_match.group("part"))
                collections.append(
                    {
                        "kind": "book_part",
                        "title": f"Part {part}",
                        "parent_index": 0,
                        "ownership": "derived",
                        "metadata": {"part_number": part},
                    }
                )
            key = "chapter_number" if kind == "audiobook" else "track_number"
            membership.update({"ordinal": ordinal, key: ordinal or None})
        elif parts:
            collections = [
                {
                    "kind": "folder",
                    "title": parts[-1],
                    "parent_id": "",
                    "ownership": "source",
                }
            ]
        work = {"media_kind": media_kind, "canonical_title": canonical_title, "metadata": {"source_title": _title_from_name(name)}}
        return work, collections, membership

    def _upsert_work(self, connection: sqlite3.Connection, work: Mapping[str, Any]) -> str:
        title = _text(work.get("canonical_title"))
        kind = _text(work.get("media_kind")) or "other"
        work_id = _stable_id("work", kind, title.casefold(), size=24)
        now = now_iso()
        connection.execute(
            """
            INSERT INTO media_works(id, schema_name, media_kind, canonical_title, sort_title, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET metadata_json=excluded.metadata_json, updated_at=excluded.updated_at,
                revision=media_works.revision+1
            """,
            (work_id, WORK_SCHEMA, kind, title, title.casefold(), _json_dumps(work.get("metadata") or {}), now, now),
        )
        return work_id

    def _upsert_collection(self, connection: sqlite3.Connection, collection: Mapping[str, Any]) -> str:
        kind = _text(collection.get("kind"))
        title = _text(collection.get("title"))
        parent_id = _text(collection.get("parent_id"))
        collection_id = _stable_id("collection", kind, title.casefold(), parent_id, size=24)
        now = now_iso()
        connection.execute(
            """
            INSERT INTO media_collections(id, schema_name, kind, title, parent_id, ownership, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at,
                revision=media_collections.revision+1
            """,
            (collection_id, COLLECTION_SCHEMA, kind, title, parent_id, _text(collection.get("ownership")) or "derived", now, now),
        )
        return collection_id

    @staticmethod
    def _next_catalog_revision(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT value FROM coordinator_meta WHERE key='catalog_revision'").fetchone()
        revision = int(row["value"] or 0) + 1 if row else 1
        connection.execute("INSERT OR REPLACE INTO coordinator_meta(key, value) VALUES ('catalog_revision', ?)", (str(revision),))
        return revision

    def catalog_revision(self) -> int:
        with self.repository.connect() as connection:
            row = connection.execute("SELECT value FROM coordinator_meta WHERE key='catalog_revision'").fetchone()
        return int(row["value"] or 0) if row else 0

    def agent_cursor(self, agent_id: str) -> str:
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT cursor FROM agent_catalog_state WHERE agent_id=?",
                (_text(agent_id),),
            ).fetchone()
        return str(row["cursor"] or "") if row else ""

    def agent_binding(self, instance_id: str) -> dict[str, Any] | None:
        token = _text(instance_id)
        if not token:
            return None
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_catalog_state WHERE instance_id=?",
                (token,),
            ).fetchone()
        return dict(row) if row else None

    def reconcile_agent_instances(
        self, active_instance_ids: Iterable[str]
    ) -> dict[str, Any]:
        active = {_text(item) for item in active_instance_ids if _text(item)}
        now = now_iso()
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT agent_id, instance_id FROM agent_catalog_state WHERE instance_id<>''"
            ).fetchall()
            missing = [
                str(row["agent_id"])
                for row in rows
                if str(row["instance_id"]) not in active
            ]
            for agent_id in missing:
                connection.execute(
                    """
                    UPDATE agent_catalog_state
                    SET availability='unavailable', freshness='stale',
                        last_error='service_instance_not_ready', updated_at=?
                    WHERE agent_id=?
                    """,
                    (now, agent_id),
                )
            connection.commit()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "missing_agent_ids": missing,
            "participation": self.participation(),
        }

    def profile_revision(self, profile_id: str) -> int:
        profile = _text(profile_id) or "default"
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(revision),0) AS revision FROM personal_media_state WHERE profile_id=?",
                (profile,),
            ).fetchone()
        return int(row["revision"] or 0)

    def list_items(
        self,
        *,
        query: str = "",
        media_kind: str = "playable",
        source: str = "",
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
        cursor: str = "",
        include_missing: bool = False,
        favorites_only: bool = False,
        sort: str = "recent",
        profile_id: str = "default",
        collection_id: str = "",
    ) -> dict[str, Any]:
        page_size = max(1, min(MAX_PAGE_SIZE, int(limit or MAX_PAGE_SIZE)))
        profile = _text(profile_id) or "default"
        query_token = _text(query)
        sort_token = _text(sort).lower() or "recent"
        signature = _cursor_signature(
            {
                "q": query_token.casefold(), "kind": media_kind, "source": source, "missing": bool(include_missing),
                "favorites": bool(favorites_only), "sort": sort_token, "profile": profile, "collection": collection_id,
            }
        )
        resolved_offset = _decode_cursor(cursor, signature) if _text(cursor) else max(0, int(offset or 0))
        filters: list[str] = []
        params: list[Any] = [profile]
        if not include_missing:
            filters.append("c.missing=0")
        if favorites_only:
            filters.append("COALESCE(ps.favorite, c.favorite)=1")
        kind = _text(media_kind).lower()
        if kind == "playable":
            filters.append("c.media_kind IN ('audio','video')")
        elif kind in {"audio", "video", "image", "other"}:
            filters.append("c.media_kind=?")
            params.append(kind)
        if _text(source) and _text(source) != "all":
            filters.append("c.source=?")
            params.append(_text(source))
        if _text(collection_id):
            filters.append("c.collection_id=?")
            params.append(_text(collection_id))
        join_search = ""
        if query_token:
            terms = [term for term in re.findall(r"[\w]+", query_token, flags=re.UNICODE) if term][:12]
            if not terms:
                terms = [query_token]
            fts_query = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
            join_search = "JOIN catalog_search ON catalog_search.item_id=c.id"
            filters.append("catalog_search.text MATCH ?")
            params.append(fts_query)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        from_sql = f"catalog_items c {join_search} LEFT JOIN personal_media_state ps ON ps.item_id=c.id AND ps.profile_id=?"
        if query_token:
            order = "bm25(catalog_search), lower(c.title), c.id"
        else:
            order = {
                "title": "lower(c.title), c.id",
                "size": "c.size_bytes DESC, c.id",
                "source": "c.source, lower(c.title), c.id",
                "favorite": "COALESCE(ps.favorite,c.favorite) DESC, lower(c.title), c.id",
                "recent": "COALESCE(ps.last_played_at,c.modified_at) DESC, c.id",
            }.get(sort_token, "COALESCE(ps.last_played_at,c.modified_at) DESC, c.id")
        with self.repository.connect() as connection:
            self._backfill_search(connection)
            total = int(connection.execute(f"SELECT COUNT(*) FROM {from_sql} {where}", tuple(params)).fetchone()[0])
            rows = connection.execute(
                f"""
                SELECT c.*, COALESCE(ps.favorite,c.favorite) AS profile_favorite,
                    COALESCE(ps.resume_ms,0) AS profile_resume_ms,
                    COALESCE(ps.duration_ms,0) AS profile_duration_ms,
                    COALESCE(ps.completed,0) AS profile_completed,
                    COALESCE(ps.last_played_at,'') AS profile_last_played_at,
                    COALESCE(ps.revision,0) AS profile_revision
                FROM {from_sql} {where} ORDER BY {order} LIMIT ? OFFSET ?
                """,
                (*params, page_size, resolved_offset),
            ).fetchall()
        items = [self._public_coordinator_item(row, profile) for row in rows]
        next_offset = resolved_offset + len(items)
        has_more = next_offset < total
        participation = self.participation()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "items": items,
            "count": len(items),
            "total_count": total,
            "catalog_revision": self.catalog_revision(),
            "ranking": {"version": "deterministic-fts-v1", "query_mode": "explicit_submit"},
            "participation": participation,
            "partial": participation["partial"],
            "pagination": {
                "limit": page_size,
                "offset": resolved_offset,
                "cursor": _encode_cursor(resolved_offset, signature),
                "next_offset": next_offset if has_more else None,
                "next_cursor": _encode_cursor(next_offset, signature) if has_more else None,
                "has_more": has_more,
            },
            "summary": self.repository.summary(),
            "facets": self.repository.facets(),
        }

    @staticmethod
    def _public_coordinator_item(row: sqlite3.Row, profile_id: str) -> dict[str, Any]:
        item = _public_item(row)
        item.update(
            {
                "schema": CATALOG_ITEM_SCHEMA,
                "agent_id": str(row["agent_id"]), "node_id": str(row["node_id"]), "root_id": str(row["root_id"]),
                "source_id": str(row["source_id"]), "source_revision": int(row["source_revision"]),
                "folder_path": str(row["folder_path"]), "catalog_revision": int(row["catalog_revision"]),
                "work_id": str(row["work_id"]), "variant_id": str(row["variant_id"]), "collection_id": str(row["collection_id"]),
                "quality": _json_loads(row["quality_json"]) or {},
                "favorite": bool(row["profile_favorite"]),
                "personal": {
                    "schema": PERSONAL_SCHEMA, "profile_id": profile_id, "resume_ms": int(row["profile_resume_ms"]),
                    "duration_ms": int(row["profile_duration_ms"]), "completed": bool(row["profile_completed"]),
                    "last_played_at": str(row["profile_last_played_at"]), "revision": int(row["profile_revision"]),
                },
            }
        )
        return item

    def set_favorite(self, item_id: str, *, profile_id: str, favorite: bool) -> dict[str, Any]:
        token = _text(item_id)
        profile = _text(profile_id) or "default"
        now = now_iso()
        with self.repository.connect() as connection:
            exists = connection.execute("SELECT id FROM catalog_items WHERE id=?", (token,)).fetchone()
            if not exists:
                return {"ok": False, "error": "item_not_found", "item_id": token}
            connection.execute(
                """
                INSERT INTO personal_media_state(profile_id,item_id,favorite,updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(profile_id,item_id) DO UPDATE SET favorite=excluded.favorite,
                    revision=personal_media_state.revision+1, updated_at=excluded.updated_at
                """,
                (profile, token, int(favorite), now),
            )
            if profile == "default":
                connection.execute("UPDATE catalog_items SET favorite=? WHERE id=?", (int(favorite), token))
            row = connection.execute("SELECT revision FROM personal_media_state WHERE profile_id=? AND item_id=?", (profile, token)).fetchone()
            connection.commit()
        item_result = self.repository.get_item(token)
        item = dict(item_result.get("item") or {})
        item["favorite"] = bool(favorite)
        return {
            "ok": True,
            "schema": PERSONAL_SCHEMA,
            "profile_id": profile,
            "item_id": token,
            "favorite": bool(favorite),
            "revision": int(row["revision"]),
            "item": item,
            "invalidation_tags": [f"media_center.personal.{profile}", "media_center.catalog"],
        }

    def checkpoint(
        self, item_id: str, *, profile_id: str, position_ms: int, duration_ms: int, completed: bool = False
    ) -> dict[str, Any]:
        token = _text(item_id)
        profile = _text(profile_id) or "default"
        position = max(0, int(position_ms or 0))
        duration = max(position, int(duration_ms or 0))
        done = bool(completed or (duration > 0 and position >= duration * 0.95))
        with self.repository.connect() as connection:
            if not connection.execute("SELECT id FROM catalog_items WHERE id=?", (token,)).fetchone():
                return {"ok": False, "error": "item_not_found", "item_id": token}
            connection.execute(
                """
                INSERT INTO personal_media_state(profile_id,item_id,resume_ms,duration_ms,completed,play_count,last_played_at,updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(profile_id,item_id) DO UPDATE SET resume_ms=excluded.resume_ms,
                    duration_ms=excluded.duration_ms, completed=excluded.completed,
                    play_count=CASE WHEN personal_media_state.last_played_at='' THEN 1 ELSE personal_media_state.play_count END,
                    last_played_at=excluded.last_played_at, revision=personal_media_state.revision+1,
                    updated_at=excluded.updated_at
                """,
                (profile, token, 0 if done else position, duration, int(done), now_iso(), now_iso()),
            )
            row = connection.execute("SELECT * FROM personal_media_state WHERE profile_id=? AND item_id=?", (profile, token)).fetchone()
            connection.commit()
        return {"ok": True, "schema": PERSONAL_SCHEMA, "state": dict(row) | {"completed": bool(row["completed"]), "favorite": bool(row["favorite"])}}

    def collections(self, *, kind: str = "", limit: int = 30, cursor: str = "") -> dict[str, Any]:
        bounded = max(1, min(30, int(limit or 30)))
        kind_token = _text(kind).lower()
        signature = _cursor_signature({"kind": kind_token})
        offset = _decode_cursor(cursor, signature) if _text(cursor) else 0
        params: list[Any] = []
        where = ""
        if kind_token:
            where = "WHERE c.kind=?"
            params.append(kind_token)
        with self.repository.connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM media_collections c {where}", tuple(params)).fetchone()[0])
            rows = connection.execute(
                f"""
                SELECT c.*, COUNT(m.work_id) AS item_count
                FROM media_collections c LEFT JOIN collection_memberships m ON m.collection_id=c.id
                {where} GROUP BY c.id ORDER BY lower(c.title), c.id LIMIT ? OFFSET ?
                """,
                (*params, bounded, offset),
            ).fetchall()
        items = [dict(row) | {"schema": COLLECTION_SCHEMA, "item_count": int(row["item_count"])} for row in rows]
        next_offset = offset + len(items)
        return {
            "ok": True, "schema": COORDINATOR_SCHEMA, "items": items, "total_count": total,
            "pagination": {"limit": bounded, "cursor": _encode_cursor(offset, signature), "next_cursor": _encode_cursor(next_offset, signature) if next_offset < total else None, "has_more": next_offset < total},
        }

    def folders(
        self,
        *,
        agent_id: str = "",
        root_id: str = "",
        parent: str = "",
        limit: int = 30,
        cursor: str = "",
    ) -> dict[str, Any]:
        bounded = max(1, min(MAX_PAGE_SIZE, int(limit or MAX_PAGE_SIZE)))
        agent = _text(agent_id)
        root = _text(root_id)
        parent_path = _text(parent).replace("\\", "/").strip("/")
        signature = _cursor_signature(
            {"agent": agent, "root": root, "parent": parent_path}
        )
        offset = _decode_cursor(cursor, signature) if _text(cursor) else 0
        filters = ["missing=0"]
        params: list[Any] = []
        if agent:
            filters.append("agent_id=?")
            params.append(agent)
        if root:
            filters.append("root_id=?")
            params.append(root)
        if parent_path:
            filters.append("folder_path LIKE ? ESCAPE '\\'")
            params.append(
                parent_path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "/%"
            )
            relative_start = len(parent_path) + 2
        else:
            filters.append("folder_path<>''")
            relative_start = 1
        where = " AND ".join(filters)
        sql = f"""
            WITH scoped AS (
                SELECT agent_id, node_id, root_id, catalog_revision,
                    substr(folder_path, ?) AS relative_path
                FROM catalog_items WHERE {where}
            ), projected AS (
                SELECT agent_id, node_id, root_id, catalog_revision,
                    CASE WHEN instr(relative_path, '/')>0
                        THEN substr(relative_path, 1, instr(relative_path, '/')-1)
                        ELSE relative_path END AS child_name
                FROM scoped WHERE relative_path<>''
            )
            SELECT agent_id, node_id, root_id, child_name,
                COUNT(*) AS source_count, MAX(catalog_revision) AS revision
            FROM projected WHERE child_name<>''
            GROUP BY agent_id, node_id, root_id, child_name
            ORDER BY lower(child_name), agent_id, root_id
        """
        query_params = [relative_start, *params]
        with self.repository.connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM ({sql})", tuple(query_params)
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"{sql} LIMIT ? OFFSET ?",
                (*query_params, bounded, offset),
            ).fetchall()
        items = []
        for row in rows:
            name = str(row["child_name"])
            path = "/".join(item for item in (parent_path, name) if item)
            items.append(
                {
                    "schema": FOLDER_NODE_SCHEMA,
                    "id": _stable_id(
                        "folder",
                        str(row["agent_id"]),
                        str(row["root_id"]),
                        path.casefold(),
                        size=24,
                    ),
                    "agent_id": str(row["agent_id"]),
                    "node_id": str(row["node_id"]),
                    "root_id": str(row["root_id"]),
                    "path": path,
                    "parent": parent_path,
                    "name": name,
                    "source_count": int(row["source_count"]),
                    "revision": int(row["revision"]),
                }
            )
        next_offset = offset + len(items)
        breadcrumbs = [
            {
                "name": segment,
                "path": "/".join(parent_path.split("/")[: index + 1]),
            }
            for index, segment in enumerate(parent_path.split("/"))
            if segment
        ]
        participation = self.participation()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "items": items,
            "count": len(items),
            "total_count": total,
            "parent": parent_path,
            "breadcrumbs": breadcrumbs,
            "partial": participation["partial"],
            "participation": participation,
            "pagination": {
                "limit": bounded,
                "cursor": _encode_cursor(offset, signature),
                "next_cursor": (
                    _encode_cursor(next_offset, signature)
                    if next_offset < total
                    else None
                ),
                "has_more": next_offset < total,
            },
        }

    def create_playlist(
        self,
        *,
        profile_id: str,
        title: str,
        visibility: str = "private",
        item_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        profile = _text(profile_id) or "default"
        playlist_title = _text(title)
        if not playlist_title:
            return {"ok": False, "error": "playlist_title_required"}
        visibility_token = _text(visibility).lower() or "private"
        if visibility_token not in {"private", "household", "shared"}:
            return {"ok": False, "error": "playlist_visibility_invalid"}
        playlist_id = _stable_id(
            "playlist", profile, playlist_title, now_iso(), size=24
        )
        now = now_iso()
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO user_playlists(
                    id, owner_profile_id, title, visibility, revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (playlist_id, profile, playlist_title, visibility_token, now, now),
            )
            error = self._replace_playlist_items(
                connection,
                playlist_id=playlist_id,
                profile_id=profile,
                item_ids=item_ids,
                now=now,
            )
            if error:
                connection.rollback()
                return {"ok": False, "error": error}
            connection.commit()
        return self.get_playlist(playlist_id, profile_id=profile)

    @staticmethod
    def _replace_playlist_items(
        connection: sqlite3.Connection,
        *,
        playlist_id: str,
        profile_id: str,
        item_ids: Iterable[str],
        now: str,
    ) -> str:
        ordered = list(dict.fromkeys(_text(item) for item in item_ids if _text(item)))
        if len(ordered) > 500:
            return "playlist_item_limit_exceeded"
        if ordered:
            placeholders = ",".join("?" for _ in ordered)
            existing = {
                str(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM catalog_items WHERE id IN ({placeholders}) AND missing=0",
                    tuple(ordered),
                ).fetchall()
            }
            if existing != set(ordered):
                return "playlist_item_unavailable"
        connection.execute(
            "DELETE FROM user_playlist_items WHERE playlist_id=?", (playlist_id,)
        )
        connection.executemany(
            """
            INSERT INTO user_playlist_items(
                playlist_id,item_id,ordinal,added_by_profile_id,added_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (playlist_id, item_id, index, profile_id, now)
                for index, item_id in enumerate(ordered)
            ],
        )
        return ""

    def get_playlist(self, playlist_id: str, *, profile_id: str) -> dict[str, Any]:
        token = _text(playlist_id)
        profile = _text(profile_id) or "default"
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_playlists WHERE id=?", (token,)
            ).fetchone()
            if row is None or (
                row["visibility"] == "private"
                and row["owner_profile_id"] != profile
            ):
                return {"ok": False, "error": "playlist_not_found"}
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM user_playlist_items WHERE playlist_id=?",
                    (token,),
                ).fetchone()[0]
            )
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "playlist": {"schema": PLAYLIST_SCHEMA, **dict(row), "item_count": count},
        }

    def playlists(
        self, *, profile_id: str, limit: int = 30, cursor: str = ""
    ) -> dict[str, Any]:
        profile = _text(profile_id) or "default"
        bounded = max(1, min(MAX_PAGE_SIZE, int(limit or MAX_PAGE_SIZE)))
        signature = _cursor_signature({"profile": profile})
        offset = _decode_cursor(cursor, signature) if _text(cursor) else 0
        where = "owner_profile_id=? OR visibility IN ('household','shared')"
        with self.repository.connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM user_playlists WHERE {where}", (profile,)
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT p.*, COUNT(i.item_id) AS item_count
                FROM user_playlists p
                LEFT JOIN user_playlist_items i ON i.playlist_id=p.id
                WHERE {where}
                GROUP BY p.id ORDER BY lower(p.title), p.id LIMIT ? OFFSET ?
                """,
                (profile, bounded, offset),
            ).fetchall()
        items = [
            {"schema": PLAYLIST_SCHEMA, **dict(row), "item_count": int(row["item_count"])}
            for row in rows
        ]
        next_offset = offset + len(items)
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "items": items,
            "count": len(items),
            "total_count": total,
            "pagination": {
                "limit": bounded,
                "cursor": _encode_cursor(offset, signature),
                "next_cursor": _encode_cursor(next_offset, signature)
                if next_offset < total
                else None,
                "has_more": next_offset < total,
            },
        }

    def update_playlist(
        self,
        playlist_id: str,
        *,
        profile_id: str,
        expected_revision: int,
        title: str | None = None,
        visibility: str | None = None,
        item_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        token = _text(playlist_id)
        profile = _text(profile_id) or "default"
        now = now_iso()
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM user_playlists WHERE id=?", (token,)
            ).fetchone()
            if row is None or row["owner_profile_id"] != profile:
                connection.rollback()
                return {"ok": False, "error": "playlist_not_found"}
            if int(row["revision"]) != int(expected_revision):
                connection.rollback()
                return {
                    "ok": False,
                    "error": "playlist_revision_conflict",
                    "current_revision": int(row["revision"]),
                }
            next_title = _text(title) if title is not None else str(row["title"])
            next_visibility = (
                _text(visibility).lower()
                if visibility is not None
                else str(row["visibility"])
            )
            if not next_title:
                connection.rollback()
                return {"ok": False, "error": "playlist_title_required"}
            if next_visibility not in {"private", "household", "shared"}:
                connection.rollback()
                return {"ok": False, "error": "playlist_visibility_invalid"}
            if item_ids is not None:
                error = self._replace_playlist_items(
                    connection,
                    playlist_id=token,
                    profile_id=profile,
                    item_ids=item_ids,
                    now=now,
                )
                if error:
                    connection.rollback()
                    return {"ok": False, "error": error}
            connection.execute(
                """
                UPDATE user_playlists SET title=?, visibility=?, revision=revision+1,
                    updated_at=? WHERE id=?
                """,
                (next_title, next_visibility, now, token),
            )
            connection.commit()
        return self.get_playlist(token, profile_id=profile)

    def delete_playlist(
        self, playlist_id: str, *, profile_id: str, expected_revision: int
    ) -> dict[str, Any]:
        token = _text(playlist_id)
        profile = _text(profile_id) or "default"
        with self.repository.connect() as connection:
            changed = connection.execute(
                """
                DELETE FROM user_playlists
                WHERE id=? AND owner_profile_id=? AND revision=?
                """,
                (token, profile, max(1, int(expected_revision))),
            ).rowcount
            connection.commit()
        return {
            "ok": bool(changed),
            "schema": COORDINATOR_SCHEMA,
            "playlist_id": token,
            **({} if changed else {"error": "playlist_revision_conflict"}),
        }

    def playlist_items(
        self,
        playlist_id: str,
        *,
        profile_id: str,
        limit: int = 30,
        cursor: str = "",
    ) -> dict[str, Any]:
        access = self.get_playlist(playlist_id, profile_id=profile_id)
        if not access.get("ok"):
            return access
        bounded = max(1, min(MAX_PAGE_SIZE, int(limit or MAX_PAGE_SIZE)))
        signature = _cursor_signature(
            {"playlist": _text(playlist_id), "profile": _text(profile_id)}
        )
        offset = _decode_cursor(cursor, signature) if _text(cursor) else 0
        with self.repository.connect() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM user_playlist_items WHERE playlist_id=?",
                    (_text(playlist_id),),
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT c.*, COALESCE(ps.favorite,c.favorite) AS profile_favorite,
                    COALESCE(ps.resume_ms,0) AS profile_resume_ms,
                    COALESCE(ps.duration_ms,0) AS profile_duration_ms,
                    COALESCE(ps.completed,0) AS profile_completed,
                    COALESCE(ps.last_played_at,'') AS profile_last_played_at,
                    COALESCE(ps.revision,0) AS profile_revision,
                    pi.ordinal AS playlist_ordinal
                FROM user_playlist_items pi
                JOIN catalog_items c ON c.id=pi.item_id
                LEFT JOIN personal_media_state ps
                    ON ps.item_id=c.id AND ps.profile_id=?
                WHERE pi.playlist_id=? AND c.missing=0
                ORDER BY pi.ordinal LIMIT ? OFFSET ?
                """,
                (_text(profile_id) or "default", _text(playlist_id), bounded, offset),
            ).fetchall()
        items = [
            self._public_coordinator_item(row, _text(profile_id) or "default")
            | {"playlist_ordinal": int(row["playlist_ordinal"])}
            for row in rows
        ]
        next_offset = offset + len(items)
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "playlist": access["playlist"],
            "items": items,
            "count": len(items),
            "total_count": total,
            "pagination": {
                "limit": bounded,
                "cursor": _encode_cursor(offset, signature),
                "next_cursor": _encode_cursor(next_offset, signature)
                if next_offset < total
                else None,
                "has_more": next_offset < total,
            },
        }

    def home(self, *, profile_id: str = "default", limit: int = 12) -> dict[str, Any]:
        bounded = max(1, min(20, int(limit or 12)))
        shelves = []
        for shelf_id, title, options in (
            ("continue", "Continue", {"sort": "recent"}),
            ("favorites", "Favorites", {"favorites_only": True, "sort": "favorite"}),
            ("recent", "Recent", {"sort": "recent"}),
            ("movies", "Movies", {"media_kind": "video", "sort": "title"}),
            ("music", "Music", {"media_kind": "audio", "sort": "title"}),
        ):
            page = self.list_items(profile_id=profile_id, limit=bounded, **options)
            shelves.append({"id": shelf_id, "title": title, "layout": "rail", "items": page["items"], "partial": page["partial"]})
        for shelf_id, title, kind in (
            ("series", "Series", "series"),
            ("albums", "Albums", "album"),
            ("audiobooks", "Audiobooks", "audiobook"),
        ):
            page = self.collections(kind=kind, limit=bounded)
            shelves.append(
                {
                    "id": shelf_id,
                    "title": title,
                    "layout": "rail",
                    "items": page["items"],
                    "partial": self.participation()["partial"],
                }
            )
        playlist_page = self.playlists(profile_id=profile_id, limit=bounded)
        shelves.append(
            {
                "id": "playlists",
                "title": "Playlists",
                "layout": "rail",
                "items": playlist_page["items"],
                "partial": False,
            }
        )
        folder_page = self.folders(limit=bounded)
        shelves.append(
            {
                "id": "folders",
                "title": "Folders",
                "layout": "rail",
                "items": folder_page["items"],
                "partial": folder_page["partial"],
            }
        )
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "profile_id": _text(profile_id) or "default",
            "shelves": shelves,
        }

    def duplicate_candidates(self, *, limit: int = 30) -> dict[str, Any]:
        bounded = max(1, min(100, int(limit or 30)))
        with self.repository.connect() as connection:
            rows = connection.execute(
                """
                SELECT work_id, size_bytes, COUNT(*) AS candidate_count, GROUP_CONCAT(id) AS item_ids
                FROM catalog_items WHERE missing=0 AND work_id<>'' AND size_bytes>0
                GROUP BY work_id, size_bytes HAVING COUNT(*)>1
                ORDER BY candidate_count DESC, work_id LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        items = [
            {"work_id": str(row["work_id"]), "size_bytes": int(row["size_bytes"]), "candidate_count": int(row["candidate_count"]), "item_ids": str(row["item_ids"]).split(","), "disposition": "review_only"}
            for row in rows
        ]
        return {"ok": True, "schema": COORDINATOR_SCHEMA, "items": items, "count": len(items), "source_deletion": False}

    def apply_correction(
        self,
        *,
        operation: str,
        subject_ref: str,
        values: Mapping[str, Any],
        actor_ref: str,
    ) -> dict[str, Any]:
        action = _text(operation).lower()
        subject = _text(subject_ref)
        actor = _text(actor_ref) or "profile:default"
        if action not in {"metadata", "merge", "regroup"}:
            return {"ok": False, "error": "catalog_correction_unsupported"}
        before: dict[str, Any]
        after: dict[str, Any]
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if action == "metadata":
                work_id = subject.removeprefix("work:")
                row = connection.execute(
                    "SELECT * FROM media_works WHERE id=?", (work_id,)
                ).fetchone()
                title = _text(values.get("canonical_title"))
                if row is None or not title:
                    connection.rollback()
                    return {"ok": False, "error": "catalog_correction_subject_invalid"}
                before = {"canonical_title": str(row["canonical_title"])}
                after = {"canonical_title": title}
                connection.execute(
                    """
                    UPDATE media_works SET canonical_title=?, sort_title=?,
                        revision=revision+1, updated_at=? WHERE id=?
                    """,
                    (title, title.casefold(), now_iso(), work_id),
                )
                claim_id = _stable_id(
                    "claim", work_id, "canonical_title", title, actor, size=24
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO metadata_claims(
                        id,subject_ref,field_name,value_json,provenance,
                        confidence,preferred,revision,created_at
                    ) VALUES (?, ?, 'canonical_title', ?, ?, 1, 1, 1, ?)
                    """,
                    (claim_id, f"work:{work_id}", _json_dumps(title), actor, now_iso()),
                )
            elif action == "merge":
                duplicate_id = subject.removeprefix("work:")
                canonical_id = _text(values.get("canonical_work_id"))
                rows = connection.execute(
                    "SELECT id, alias_of FROM media_works WHERE id IN (?, ?)",
                    (duplicate_id, canonical_id),
                ).fetchall()
                if len(rows) != 2 or duplicate_id == canonical_id:
                    connection.rollback()
                    return {"ok": False, "error": "catalog_correction_subject_invalid"}
                before = {"alias_of": next(str(row["alias_of"]) for row in rows if row["id"] == duplicate_id)}
                after = {"alias_of": canonical_id}
                alias_id = _stable_id("alias", duplicate_id, canonical_id, size=24)
                connection.execute(
                    "UPDATE media_works SET alias_of=?, revision=revision+1, updated_at=? WHERE id=?",
                    (canonical_id, now_iso(), duplicate_id),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO catalog_aliases(
                        alias_id,canonical_id,reason,actor_ref,reversible,created_at
                    ) VALUES (?, ?, 'user_merge', ?, 1, ?)
                    """,
                    (alias_id, canonical_id, actor, now_iso()),
                )
            else:
                item_id = subject.removeprefix("item:")
                collection_id = _text(values.get("collection_id"))
                row = connection.execute(
                    "SELECT work_id,variant_id,collection_id FROM catalog_items WHERE id=?",
                    (item_id,),
                ).fetchone()
                collection = connection.execute(
                    "SELECT id FROM media_collections WHERE id=?", (collection_id,)
                ).fetchone()
                if row is None or collection is None:
                    connection.rollback()
                    return {"ok": False, "error": "catalog_correction_subject_invalid"}
                before = {"collection_id": str(row["collection_id"])}
                after = {"collection_id": collection_id}
                connection.execute(
                    "UPDATE catalog_items SET collection_id=? WHERE id=?",
                    (collection_id, item_id),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO collection_memberships(
                        collection_id,work_id,variant_id,ordinal,revision
                    ) VALUES (?, ?, ?, 0, 1)
                    """,
                    (collection_id, str(row["work_id"]), str(row["variant_id"])),
                )
            correction_id = _stable_id(
                "correction", action, subject, actor, now_iso(), size=24
            )
            connection.execute(
                """
                INSERT INTO catalog_corrections(
                    id,operation,subject_ref,before_json,after_json,actor_ref,created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correction_id,
                    action,
                    subject,
                    _json_dumps(before),
                    _json_dumps(after),
                    actor,
                    now_iso(),
                ),
            )
            connection.commit()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "correction": {
                "schema": CORRECTION_SCHEMA,
                "id": correction_id,
                "operation": action,
                "subject_ref": subject,
                "before": before,
                "after": after,
                "actor_ref": actor,
                "reversible": True,
            },
            "source_deletion": False,
        }

    def reverse_correction(
        self, correction_id: str, *, actor_ref: str
    ) -> dict[str, Any]:
        token = _text(correction_id)
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM catalog_corrections WHERE id=?", (token,)
            ).fetchone()
            if row is None or row["reversed_by"]:
                connection.rollback()
                return {"ok": False, "error": "catalog_correction_not_reversible"}
            action = str(row["operation"])
            subject = str(row["subject_ref"])
            before = _json_loads(row["before_json"]) or {}
            if action == "metadata":
                work_id = subject.removeprefix("work:")
                title = _text(before.get("canonical_title"))
                connection.execute(
                    """
                    UPDATE media_works SET canonical_title=?, sort_title=?,
                        revision=revision+1, updated_at=? WHERE id=?
                    """,
                    (title, title.casefold(), now_iso(), work_id),
                )
            elif action == "merge":
                duplicate_id = subject.removeprefix("work:")
                connection.execute(
                    "UPDATE media_works SET alias_of=?, revision=revision+1, updated_at=? WHERE id=?",
                    (_text(before.get("alias_of")), now_iso(), duplicate_id),
                )
            elif action == "regroup":
                item_id = subject.removeprefix("item:")
                connection.execute(
                    "UPDATE catalog_items SET collection_id=? WHERE id=?",
                    (_text(before.get("collection_id")), item_id),
                )
            reversal_id = _stable_id(
                "correction-reversal", token, _text(actor_ref), now_iso(), size=24
            )
            connection.execute(
                "UPDATE catalog_corrections SET reversed_by=? WHERE id=?",
                (reversal_id, token),
            )
            connection.commit()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "correction_id": token,
            "reversed_by": reversal_id,
            "source_deletion": False,
        }

    def metadata_claims(
        self, subject_ref: str, *, limit: int = 30
    ) -> dict[str, Any]:
        bounded = max(1, min(100, int(limit or 30)))
        with self.repository.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM metadata_claims WHERE subject_ref=?
                ORDER BY preferred DESC, confidence DESC, created_at DESC LIMIT ?
                """,
                (_text(subject_ref), bounded),
            ).fetchall()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "items": [
                dict(row) | {"value": _json_loads(row["value_json"])}
                for row in rows
            ],
            "count": len(rows),
        }

    def participation(self) -> dict[str, Any]:
        with self.repository.connect() as connection:
            rows = connection.execute("SELECT * FROM agent_catalog_state ORDER BY agent_id").fetchall()
        agents = [dict(row) for row in rows]
        unavailable = [item["agent_id"] for item in agents if item["availability"] != "available"]
        stale = [item["agent_id"] for item in agents if item["freshness"] != "fresh"]
        return {"agents": agents, "expected_count": len(agents), "available_count": len(agents) - len(unavailable), "unavailable_agent_ids": unavailable, "stale_agent_ids": stale, "partial": bool(unavailable or stale), "fresh": not bool(unavailable or stale)}

    def mark_agent_unavailable(self, agent_id: str, *, node_id: str = "", reason: str = "agent_unavailable") -> dict[str, Any]:
        token = _text(agent_id)
        with self.repository.connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_catalog_state(agent_id,node_id,availability,freshness,last_error,updated_at)
                VALUES (?, ?, 'unavailable', 'stale', ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET availability='unavailable', freshness='stale',
                    last_error=excluded.last_error, updated_at=excluded.updated_at
                """,
                (token, _text(node_id), _text(reason), now_iso()),
            )
            connection.commit()
        return {"ok": True, "schema": COORDINATOR_SCHEMA, "participation": self.participation()}

    def queue_background_job(self, kind: str, subject_ref: str, *, priority: int = 100) -> dict[str, Any]:
        kind_token = _text(kind)
        if kind_token not in {"technical_probe", "metadata_enrichment", "thumbnail", "fingerprint", "embedding"}:
            return {"ok": False, "error": "unsupported_background_job"}
        job_id = _stable_id("mediajob", kind_token, subject_ref, now_iso(), size=24)
        now = now_iso()
        with self.repository.connect() as connection:
            connection.execute(
                "INSERT INTO media_background_jobs(id,kind,subject_ref,status,priority,created_at,updated_at) VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                (job_id, kind_token, _text(subject_ref), max(1, min(1000, int(priority))), now, now),
            )
            connection.commit()
        return {"ok": True, "schema": COORDINATOR_SCHEMA, "job": {"id": job_id, "kind": kind_token, "subject_ref": _text(subject_ref), "status": "queued", "priority": priority}}

    def claim_background_job(self) -> dict[str, Any] | None:
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM media_background_jobs
                WHERE status='queued' AND attempts<3
                ORDER BY priority, created_at LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            now = now_iso()
            changed = connection.execute(
                """
                UPDATE media_background_jobs
                SET status='running', attempts=attempts+1, started_at=?,
                    progress_json=?, error_code='', updated_at=?
                WHERE id=? AND status='queued'
                """,
                (
                    now,
                    _json_dumps({"phase": "provider", "completed": 0, "total": 1}),
                    now,
                    str(row["id"]),
                ),
            ).rowcount
            connection.commit()
        if not changed:
            return None
        with self.repository.connect() as connection:
            claimed = connection.execute(
                "SELECT * FROM media_background_jobs WHERE id=?",
                (str(row["id"]),),
            ).fetchone()
        return dict(claimed) if claimed else None

    def finish_background_job(
        self,
        job_id: str,
        *,
        provider_id: str,
        claim_count: int,
    ) -> dict[str, Any]:
        now = now_iso()
        with self.repository.connect() as connection:
            changed = connection.execute(
                """
                UPDATE media_background_jobs
                SET status='completed', provider_id=?, finished_at=?,
                    progress_json=?, updated_at=? WHERE id=? AND status='running'
                """,
                (
                    _text(provider_id),
                    now,
                    _json_dumps(
                        {
                            "phase": "completed",
                            "completed": 1,
                            "total": 1,
                            "claim_count": max(0, int(claim_count)),
                        }
                    ),
                    now,
                    _text(job_id),
                ),
            ).rowcount
            connection.commit()
        return {"ok": bool(changed), "job_id": _text(job_id), "status": "completed"}

    def fail_background_job(
        self, job_id: str, *, error_code: str, retryable: bool
    ) -> dict[str, Any]:
        token = _text(job_id)
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM media_background_jobs WHERE id=?", (token,)
            ).fetchone()
            attempts = int(row["attempts"] or 0) if row else 3
            status = "queued" if retryable and attempts < 3 else "failed"
            connection.execute(
                """
                UPDATE media_background_jobs SET status=?, error_code=?,
                    progress_json=?, finished_at=?, updated_at=? WHERE id=?
                """,
                (
                    status,
                    _text(error_code),
                    _json_dumps(
                        {
                            "phase": "retry" if status == "queued" else "failed",
                            "completed": 0,
                            "total": 1,
                        }
                    ),
                    "" if status == "queued" else now_iso(),
                    now_iso(),
                    token,
                ),
            )
            connection.commit()
        return {"ok": True, "job_id": token, "status": status, "attempts": attempts}

    def record_metadata_claim(
        self,
        *,
        subject_ref: str,
        field_name: str,
        value: Any,
        provenance: str,
        confidence: float,
        preferred: bool = False,
    ) -> dict[str, Any]:
        subject = _text(subject_ref)
        field = _text(field_name)
        provider = _text(provenance)
        if not subject or not field or not provider:
            return {"ok": False, "error": "metadata_claim_invalid"}
        claim_id = _stable_id(
            "claim", subject, field, _json_dumps(value), provider, size=24
        )
        with self.repository.connect() as connection:
            previous = connection.execute(
                "SELECT revision FROM metadata_claims WHERE id=?", (claim_id,)
            ).fetchone()
            revision = int(previous["revision"] or 0) + 1 if previous else 1
            connection.execute(
                """
                INSERT INTO metadata_claims(
                    id,subject_ref,field_name,value_json,provenance,confidence,
                    preferred,revision,created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET confidence=excluded.confidence,
                    preferred=excluded.preferred, revision=excluded.revision,
                    created_at=excluded.created_at
                """,
                (
                    claim_id,
                    subject,
                    field,
                    _json_dumps(value),
                    provider,
                    max(0.0, min(1.0, float(confidence))),
                    int(preferred),
                    revision,
                    now_iso(),
                ),
            )
            connection.commit()
        return {"ok": True, "claim_id": claim_id, "revision": revision}

    def enrichment_subject(self, subject_ref: str) -> dict[str, Any] | None:
        subject = _text(subject_ref)
        if subject.startswith("item:"):
            item_id = subject.removeprefix("item:")
            with self.repository.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM catalog_items WHERE id=?", (item_id,)
                ).fetchone()
            if row:
                return {
                    "subject_ref": subject,
                    "kind": "item",
                    "id": item_id,
                    "name": str(row["name"]),
                    "title": str(row["title"]),
                    "folder_path": str(row["folder_path"]),
                    "media_kind": str(row["media_kind"]),
                    "metadata": _json_loads(row["metadata_json"]) or {},
                    "descriptor": _json_loads(row["descriptor_json"]) or {},
                }
        if subject.startswith("work:"):
            work_id = subject.removeprefix("work:")
            with self.repository.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM media_works WHERE id=?", (work_id,)
                ).fetchone()
            if row:
                return {
                    "subject_ref": subject,
                    "kind": "work",
                    "id": work_id,
                    "title": str(row["canonical_title"]),
                    "media_kind": str(row["media_kind"]),
                    "metadata": _json_loads(row["metadata_json"]) or {},
                }
        return None

    def operations(self, *, limit: int = 30) -> dict[str, Any]:
        bounded = max(1, min(100, int(limit or 30)))
        with self.repository.connect() as connection:
            rows = connection.execute("SELECT * FROM media_background_jobs ORDER BY updated_at DESC LIMIT ?", (bounded,)).fetchall()
        return {"ok": True, "schema": COORDINATOR_SCHEMA, "items": [dict(row) | {"progress": _json_loads(row["progress_json"]) or {}} for row in rows], "count": len(rows)}

    def diagnostics(self) -> dict[str, Any]:
        with self.repository.connect() as connection:
            works = int(connection.execute("SELECT COUNT(*) FROM media_works").fetchone()[0])
            variants = int(connection.execute("SELECT COUNT(*) FROM media_variants").fetchone()[0])
            collections = int(connection.execute("SELECT COUNT(*) FROM media_collections").fetchone()[0])
        summary = self.repository.summary()
        return {
            "ok": True, "schema": COORDINATOR_SCHEMA, "catalog_revision": self.catalog_revision(),
            "counts": {"sources": summary["total_count"], "works": works, "variants": variants, "collections": collections},
            "participation": self.participation(),
            "budgets": {"catalog_page": MAX_PAGE_SIZE, "player_queue": 10, "agent_delta_page": 1000},
            "storage": {"media_bytes": "external", "catalog": "skill_local_relational"},
        }
