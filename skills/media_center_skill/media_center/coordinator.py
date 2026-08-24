from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from .catalog import (
    MediaCenterRepository,
    _json_dumps,
    _json_loads,
    _media_kind,
    _public_artwork,
    _public_content_path,
    _public_direct_url,
    _public_item,
    _public_metadata,
    _public_resource_descriptor,
    _text,
    _title_from_name,
    now_iso,
    schema_revision_is_current,
)
from .discovery import discovery_score, fold_text


COORDINATOR_SCHEMA = "adaos.media_center.coordinator.v2"
COORDINATOR_SCHEMA_REVISION = "2026-08-25.1"
SEARCH_ROWID_REVISION = "1"
AUDIO_CONTEXT_IDENTITY_REVISION = "1"
VIDEO_SERIES_IDENTITY_REVISION = "2"
CATALOG_ITEM_SCHEMA = "adaos.media_center.media_source.v1"
WORK_SCHEMA = "adaos.media_center.media_work.v1"
COLLECTION_SCHEMA = "adaos.media_center.media_collection.v1"
PERSONAL_SCHEMA = "adaos.media_center.personal_state.v1"
FOLDER_NODE_SCHEMA = "adaos.media_center.folder_node.v1"
PLAYLIST_SCHEMA = "adaos.media_center.playlist.v1"
CORRECTION_SCHEMA = "adaos.media_center.catalog_correction.v1"
PLAYBACK_PLAN_SCHEMA = "adaos.media_center.playback_plan.v2"
PROFILE_SCHEMA = "adaos.media_center.profile.v1"
MAX_PAGE_SIZE = 30
HOME_SHELF_ORDER = (
    "continue",
    "favorites",
    "recent",
    "recommended",
    "movies",
    "series",
    "music",
    "albums",
    "audiobooks",
    "playlists",
    "folders",
)


def _default_profile_policy(kind: str = "personal") -> dict[str, Any]:
    shared = _text(kind).lower() in {"household", "kids"}
    return {
        "allowed_media_kinds": ["audio", "video"],
        "maximum_maturity_rating": 12 if _text(kind).lower() == "kids" else 18,
        "allow_explicit": _text(kind).lower() != "kids",
        "show_history_on_shared_surface": not shared,
        "recommendations_enabled": True,
        "home_row_order": list(HOME_SHELF_ORDER),
        "default_view": "rail" if shared else "grid",
        "density": "comfortable" if shared else "compact",
        "default_target_id": "",
    }

_SEASON_EPISODE = re.compile(r"(?i)(?:^|[ ._\-])s(?P<season>\d{1,3})e(?P<episode>\d{1,4})(?:[ ._\-]|$)")
_SEASON_FOLDER = re.compile(r"(?i)^(?:season|сезон)[ ._\-]*(?P<season>\d{1,3})$")
_DISC_FOLDER = re.compile(r"(?i)^(?:disc|disk|cd|диск)[ ._\-]*(?P<disc>\d{1,3})$")
_PART_FOLDER = re.compile(r"(?i)^(?:part|book|том|часть)[ ._\-]*(?P<part>\d{1,3})$")
_NUMERIC_FOLDER = re.compile(r"^(?P<number>\d{1,3})$")
_AUDIOBOOK_HINT = re.compile(r"(?i)(?:audio[ ._\-]*books?|аудиокниг)")
_LEADING_NUMBER = re.compile(r"^(?P<number>\d{1,4})(?:[ ._\-]+|$)")


def _stable_id(prefix: str, *parts: Any, size: int = 24) -> str:
    raw = "\0".join(_text(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()[:size]}"


def _folder_nodes(folder_path: Any) -> list[tuple[str, str, str]]:
    segments = [
        part
        for part in _text(folder_path).replace("\\", "/").split("/")
        if part
    ]
    nodes: list[tuple[str, str, str]] = []
    parent = ""
    for name in segments:
        path = "/".join(part for part in (parent, name) if part)
        nodes.append((path, parent, name))
        parent = path
    return nodes


def _observed_count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return -1


def _stored_observed_count(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return -1
    return parsed if parsed >= 0 else -1


def _normalize_title(value: Any) -> str:
    token = Path(_text(value)).stem
    token = _SEASON_EPISODE.sub(" ", token)
    token = _LEADING_NUMBER.sub("", token)
    token = re.sub(r"[._\-]+", " ", token)
    return " ".join(token.split()).strip() or _title_from_name(_text(value))


@lru_cache(maxsize=2048)
def _episode_filename_evidence(name: str) -> dict[str, Any]:
    match = _SEASON_EPISODE.search(name)
    if not match:
        return {}
    stem = Path(name).stem
    stem_match = _SEASON_EPISODE.search(stem)
    if stem_match is None:
        return {}
    title = re.sub(r"[._\-]+", " ", stem[: stem_match.start()]).strip()
    title = " ".join(title.split())
    try:
        season = int(stem_match.group("season") or 0)
        episode = int(stem_match.group("episode") or 0)
    except (TypeError, ValueError):
        return {}
    if not title or season <= 0 or episode <= 0:
        return {}
    return {
        "title": title[:300],
        "season": season,
        "episode": episode,
        "parser": "sxe-basename-v1",
    }


def clear_filename_evidence_cache() -> None:
    _episode_filename_evidence.cache_clear()


def _cursor_signature(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _encode_cursor(
    offset: int,
    signature: str,
    anchor: list[Any] | tuple[Any, ...] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "v": 2 if anchor is not None else 1,
        "offset": max(0, int(offset)),
        "sig": signature,
    }
    if anchor is not None:
        payload["anchor"] = list(anchor)[:4]
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_keyset_cursor(value: Any, signature: str) -> tuple[int, list[Any] | None]:
    token = _text(value)
    if not token:
        return 0
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("v") not in {1, 2} or payload.get("sig") != signature:
            raise ValueError("cursor does not match query")
        anchor = payload.get("anchor") if payload.get("v") == 2 else None
        if anchor is not None and (
            not isinstance(anchor, list)
            or not 1 <= len(anchor) <= 4
            or any(not isinstance(item, (str, int, float)) for item in anchor)
        ):
            raise ValueError("cursor anchor is invalid")
        return max(0, int(payload["offset"])), anchor
    except Exception as exc:
        raise ValueError("invalid_media_catalog_cursor") from exc


def _decode_cursor(value: Any, signature: str) -> int:
    offset, _anchor = _decode_keyset_cursor(value, signature)
    return offset


def _keyset_predicate(
    keys: tuple[tuple[str, str], ...],
    anchor: list[Any],
) -> tuple[str, list[Any]]:
    if len(keys) != len(anchor):
        raise ValueError("invalid_media_catalog_cursor")
    branches: list[str] = []
    params: list[Any] = []
    for index, (expression, direction) in enumerate(keys):
        equality = [f"{keys[prior][0]}=?" for prior in range(index)]
        comparator = "<" if direction == "desc" else ">"
        branches.append(
            "(" + " AND ".join([*equality, f"{expression}{comparator}?"]) + ")"
        )
        params.extend(anchor[:index])
        params.append(anchor[index])
    return "(" + " OR ".join(branches) + ")", params


class MediaCatalogCoordinator:
    """Global catalog read model fed by idempotent node-agent deltas."""

    def __init__(self, repository: MediaCenterRepository):
        self.repository = repository
        self.ensure_schema()

    def _schema_is_current(self) -> bool:
        return schema_revision_is_current(
            self.repository.db_path,
            table="coordinator_meta",
            key="coordinator_schema_revision",
            expected=COORDINATOR_SCHEMA_REVISION,
            unavailable_error="media_center_coordinator_schema_state_unavailable",
        )

    def ensure_schema(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self._schema_is_current():
            return {
                "ok": True,
                "schema": COORDINATOR_SCHEMA,
                "db_path": str(self.repository.db_path),
                "retired_legacy_count": 0,
                "migration": "current",
            }
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
                "maturity_rating": "INTEGER NOT NULL DEFAULT 0",
                "explicit": "INTEGER NOT NULL DEFAULT 0",
            }
            policy_columns_added = any(
                name not in columns for name in ("maturity_rating", "explicit")
            )
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE catalog_items ADD COLUMN {name} {definition}")
            if policy_columns_added:
                connection.execute(
                    """
                    UPDATE catalog_items SET
                        maturity_rating=MAX(0,MIN(21,COALESCE(CAST(
                            json_extract(metadata_json,'$.maturity_rating')
                            AS INTEGER),0))),
                        explicit=CASE WHEN lower(CAST(COALESCE(
                            json_extract(metadata_json,'$.explicit'),0) AS TEXT))
                            IN ('1','true','yes','on') THEN 1 ELSE 0 END
                    """
                )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_media_center_agent_source ON catalog_items(agent_id, source_id);
                CREATE INDEX IF NOT EXISTS idx_media_center_work ON catalog_items(work_id, missing);
                CREATE INDEX IF NOT EXISTS idx_media_center_collection ON catalog_items(collection_id, missing);
                CREATE INDEX IF NOT EXISTS idx_media_center_folder_browse
                    ON catalog_items(agent_id, root_id, folder_path, missing);
                CREATE TABLE IF NOT EXISTS catalog_folder_nodes (
                    agent_id TEXT NOT NULL,
                    node_id TEXT NOT NULL DEFAULT '',
                    root_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    parent TEXT NOT NULL,
                    name TEXT NOT NULL,
                    source_count INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(agent_id, root_id, path)
                );
                CREATE INDEX IF NOT EXISTS idx_media_center_folder_parent
                    ON catalog_folder_nodes(parent, name COLLATE NOCASE,
                        agent_id, root_id, path);
                CREATE INDEX IF NOT EXISTS idx_media_center_folder_files
                    ON catalog_items(folder_path, missing, media_kind,
                        maturity_rating, explicit, title COLLATE NOCASE, id);
                CREATE INDEX IF NOT EXISTS idx_media_center_browse_title
                    ON catalog_items(missing,media_kind,title COLLATE NOCASE,id);
                CREATE INDEX IF NOT EXISTS idx_media_center_browse_size
                    ON catalog_items(missing,media_kind,size_bytes DESC,id);
                CREATE INDEX IF NOT EXISTS idx_media_center_source_path
                    ON catalog_items(source_path,missing,agent_id);
                CREATE INDEX IF NOT EXISTS idx_media_center_catalog_variant
                    ON catalog_items(variant_id);
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
                CREATE INDEX IF NOT EXISTS idx_media_center_collection_parent
                    ON media_collections(parent_id);
                CREATE INDEX IF NOT EXISTS idx_media_center_collection_kind_title
                    ON media_collections(kind, title COLLATE NOCASE, id);
                CREATE INDEX IF NOT EXISTS idx_media_center_membership_work
                    ON collection_memberships(work_id);
                CREATE INDEX IF NOT EXISTS idx_media_center_membership_preview
                    ON collection_memberships(
                        collection_id, season_number, episode_number, ordinal,
                        work_id, variant_id
                    );
                CREATE INDEX IF NOT EXISTS idx_media_center_catalog_work_variant
                    ON catalog_items(work_id, variant_id, missing, id);
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
                CREATE INDEX IF NOT EXISTS idx_media_center_claim_lookup
                    ON metadata_claims(subject_ref,field_name,confidence DESC,created_at DESC);
                CREATE TABLE IF NOT EXISTS catalog_metadata_projection (
                    item_id TEXT PRIMARY KEY REFERENCES catalog_items(id) ON DELETE CASCADE,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    title TEXT NOT NULL DEFAULT '',
                    year INTEGER,
                    release_date TEXT NOT NULL DEFAULT '',
                    rating REAL,
                    critic_rating REAL,
                    audience_rating REAL,
                    content_rating TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    genres_json TEXT NOT NULL DEFAULT '[]',
                    artists_json TEXT NOT NULL DEFAULT '[]',
                    album TEXT NOT NULL DEFAULT '',
                    series TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_media_center_metadata_year
                    ON catalog_metadata_projection(year,item_id);
                CREATE INDEX IF NOT EXISTS idx_media_center_metadata_rating
                    ON catalog_metadata_projection(rating,item_id);
                CREATE TABLE IF NOT EXISTS catalog_metadata_facets (
                    item_id TEXT NOT NULL REFERENCES catalog_items(id) ON DELETE CASCADE,
                    field_name TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    display_value TEXT NOT NULL,
                    numeric_value REAL,
                    PRIMARY KEY(item_id,field_name,normalized_value)
                );
                CREATE INDEX IF NOT EXISTS idx_media_center_metadata_facet_lookup
                    ON catalog_metadata_facets(field_name,normalized_value,item_id);
                CREATE TABLE IF NOT EXISTS catalog_aliases (
                    alias_id TEXT PRIMARY KEY,
                    canonical_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    reversible INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_media_center_alias_canonical
                    ON catalog_aliases(canonical_id,active);
                CREATE TABLE IF NOT EXISTS agent_catalog_state (
                    agent_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL DEFAULT '',
                    node_id TEXT NOT NULL,
                    cursor TEXT NOT NULL DEFAULT '',
                    last_sequence INTEGER NOT NULL DEFAULT 0,
                    availability TEXT NOT NULL DEFAULT 'unknown',
                    freshness TEXT NOT NULL DEFAULT 'unknown',
                    last_error TEXT NOT NULL DEFAULT '',
                    root_count INTEGER NOT NULL DEFAULT -1,
                    source_count INTEGER NOT NULL DEFAULT -1,
                    available_count INTEGER NOT NULL DEFAULT -1,
                    active_job_count INTEGER NOT NULL DEFAULT -1,
                    failed_job_count INTEGER NOT NULL DEFAULT -1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS personal_media_state (
                    profile_id TEXT NOT NULL,
                    item_id TEXT NOT NULL REFERENCES catalog_items(id) ON DELETE CASCADE,
                    favorite INTEGER NOT NULL DEFAULT 0,
                    resume_ms INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    rating INTEGER NOT NULL DEFAULT 0,
                    hidden INTEGER NOT NULL DEFAULT 0,
                    play_count INTEGER NOT NULL DEFAULT 0,
                    last_played_at TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id, item_id)
                );
                CREATE INDEX IF NOT EXISTS idx_personal_media_recent ON personal_media_state(profile_id, last_played_at DESC);
                CREATE TABLE IF NOT EXISTS media_profiles (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    policy_json TEXT NOT NULL DEFAULT '{}',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
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
            variant_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(media_variants)"
                ).fetchall()
            }
            for name, definition in {
                "descriptor_json": "TEXT NOT NULL DEFAULT '{}'",
                "resource_id": "TEXT NOT NULL DEFAULT ''",
                "source_revision": "INTEGER NOT NULL DEFAULT 0",
                "exact_source_id": "TEXT NOT NULL DEFAULT ''",
                "exact_source_revision": "INTEGER NOT NULL DEFAULT 0",
                "derived": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in variant_columns:
                    connection.execute(
                        f"ALTER TABLE media_variants ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_center_variant_work "
                "ON media_variants(work_id,derived,id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_center_variant_exact_source "
                "ON media_variants(exact_source_id,node_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_center_background_subject "
                "ON media_background_jobs(subject_ref,kind,status,updated_at DESC,id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_center_background_claim "
                "ON media_background_jobs(status,attempts,priority,created_at,id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_center_background_recent "
                "ON media_background_jobs(updated_at DESC,id DESC)"
            )
            alias_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(catalog_aliases)"
                ).fetchall()
            }
            if "active" not in alias_columns:
                connection.execute(
                    "ALTER TABLE catalog_aliases ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
            personal_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(personal_media_state)"
                ).fetchall()
            }
            for name, definition in {
                "rating": "INTEGER NOT NULL DEFAULT 0",
                "hidden": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in personal_columns:
                    connection.execute(
                        f"ALTER TABLE personal_media_state ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_personal_media_recent_item "
                "ON personal_media_state(profile_id,last_played_at DESC,item_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_personal_media_continue "
                "ON personal_media_state(profile_id,completed,last_played_at DESC,item_id) "
                "WHERE resume_ms>0 AND last_played_at<>''"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_personal_media_favorite "
                "ON personal_media_state(profile_id,item_id) WHERE favorite=1"
            )
            agent_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(agent_catalog_state)"
                ).fetchall()
            }
            for name, definition in {
                "instance_id": "TEXT NOT NULL DEFAULT ''",
                "root_count": "INTEGER NOT NULL DEFAULT -1",
                "source_count": "INTEGER NOT NULL DEFAULT -1",
                "available_count": "INTEGER NOT NULL DEFAULT -1",
                "active_job_count": "INTEGER NOT NULL DEFAULT -1",
                "failed_job_count": "INTEGER NOT NULL DEFAULT -1",
            }.items():
                if name not in agent_columns:
                    connection.execute(
                        f"ALTER TABLE agent_catalog_state ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS catalog_search USING fts5(item_id UNINDEXED, text, tokenize='unicode61 remove_diacritics 2')"
            )
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fuzzy_search USING fts5(item_id UNINDEXED, tokens, tokenize='unicode61')"
            )
            now = now_iso()
            default_profiles = (
                ("default", "Personal", "personal", _default_profile_policy()),
                (
                    "household",
                    "Household",
                    "household",
                    _default_profile_policy("household"),
                ),
                ("kids", "Kids", "kids", _default_profile_policy("kids")),
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO media_profiles(
                    id,label,kind,policy_json,created_at,updated_at
                ) VALUES (?,?,?,?,?,?)
                """,
                [
                    (profile_id, label, kind, _json_dumps(policy), now, now)
                    for profile_id, label, kind, policy in default_profiles
                ],
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO personal_media_state(
                    profile_id,item_id,favorite,updated_at
                )
                SELECT 'default',id,1,? FROM catalog_items WHERE favorite=1
                """,
                (now,),
            )
            for profile_row in connection.execute(
                "SELECT id,kind,policy_json FROM media_profiles"
            ).fetchall():
                current_policy = _json_loads(profile_row["policy_json"]) or {}
                migrated_policy = {
                    **_default_profile_policy(str(profile_row["kind"])),
                    **current_policy,
                }
                if migrated_policy != current_policy:
                    connection.execute(
                        """
                        UPDATE media_profiles SET policy_json=?,revision=revision+1,
                            updated_at=? WHERE id=?
                        """,
                        (
                            _json_dumps(migrated_policy),
                            now,
                            str(profile_row["id"]),
                        ),
                    )
                profile_id = str(profile_row["id"])
                personal_revision = connection.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM personal_media_state "
                    "WHERE profile_id=?",
                    (profile_id,),
                ).fetchone()[0]
                profile_revision = connection.execute(
                    "SELECT revision FROM media_profiles WHERE id=?",
                    (profile_id,),
                ).fetchone()[0]
                connection.execute(
                    "INSERT OR IGNORE INTO coordinator_meta(key,value) VALUES (?,?)",
                    (
                        self._profile_revision_key(profile_id),
                        str(max(int(personal_revision or 0), int(profile_revision or 0))),
                    ),
                )
            identity_repair = {
                "audio": self._repair_contextual_audio_identity(connection),
                "video_series": self._repair_video_series_identity(connection),
            }
            connection.execute("INSERT OR REPLACE INTO coordinator_meta(key, value) VALUES ('schema_version', ?)", (COORDINATOR_SCHEMA,))
            retired_legacy_count = connection.execute(
                """
                UPDATE catalog_items AS legacy
                SET missing=1,last_seen_at=?
                WHERE legacy.source='media_server'
                    AND legacy.agent_id=''
                    AND legacy.missing=0
                    AND legacy.source_path<>''
                    AND EXISTS (
                        SELECT 1 FROM catalog_items AS agent
                        WHERE agent.agent_id<>''
                            AND agent.missing=0
                            AND agent.source_path=legacy.source_path
                    )
                """,
                (now,),
            ).rowcount
            self._rebuild_folder_nodes(connection)
            self._backfill_search(connection)
            self._ensure_search_rowids(connection)
            self._backfill_metadata_projections(connection)
            connection.execute(
                "INSERT OR REPLACE INTO coordinator_meta(key, value) "
                "VALUES ('coordinator_schema_revision', ?)",
                (COORDINATOR_SCHEMA_REVISION,),
            )
            connection.commit()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "db_path": str(self.repository.db_path),
            "retired_legacy_count": max(0, int(retired_legacy_count or 0)),
            "identity_repair": identity_repair,
        }

    @staticmethod
    def _rebuild_folder_nodes(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM catalog_folder_nodes")
        connection.execute(
            """
            WITH RECURSIVE expanded(
                agent_id,node_id,root_id,path,parent,name,rest,revision
            ) AS (
                SELECT agent_id,node_id,root_id,
                    CASE WHEN instr(folder_path,'/')>0
                        THEN substr(folder_path,1,instr(folder_path,'/')-1)
                        ELSE folder_path END,
                    '',
                    CASE WHEN instr(folder_path,'/')>0
                        THEN substr(folder_path,1,instr(folder_path,'/')-1)
                        ELSE folder_path END,
                    CASE WHEN instr(folder_path,'/')>0
                        THEN substr(folder_path,instr(folder_path,'/')+1)
                        ELSE '' END,
                    catalog_revision
                FROM catalog_items
                WHERE missing=0 AND folder_path<>''
                UNION ALL
                SELECT agent_id,node_id,root_id,
                    path || '/' || CASE WHEN instr(rest,'/')>0
                        THEN substr(rest,1,instr(rest,'/')-1)
                        ELSE rest END,
                    path,
                    CASE WHEN instr(rest,'/')>0
                        THEN substr(rest,1,instr(rest,'/')-1)
                        ELSE rest END,
                    CASE WHEN instr(rest,'/')>0
                        THEN substr(rest,instr(rest,'/')+1)
                        ELSE '' END,
                    revision
                FROM expanded WHERE rest<>''
            )
            INSERT INTO catalog_folder_nodes(
                agent_id,node_id,root_id,path,parent,name,source_count,revision
            )
            SELECT agent_id,MAX(node_id),root_id,path,MAX(parent),MAX(name),
                COUNT(*),MAX(revision)
            FROM expanded WHERE path<>'' AND name<>''
            GROUP BY agent_id,root_id,path
            """
        )

    @staticmethod
    def _adjust_folder_nodes(
        connection: sqlite3.Connection,
        *,
        agent_id: str,
        node_id: str,
        root_id: str,
        folder_path: str,
        delta: int,
        revision: int,
    ) -> None:
        nodes = _folder_nodes(folder_path)
        if not nodes:
            return
        if delta > 0:
            connection.executemany(
                """
                INSERT INTO catalog_folder_nodes(
                    agent_id,node_id,root_id,path,parent,name,source_count,revision
                ) VALUES (?,?,?,?,?,?,1,?)
                ON CONFLICT(agent_id,root_id,path) DO UPDATE SET
                    node_id=excluded.node_id,
                    parent=excluded.parent,
                    name=excluded.name,
                    source_count=catalog_folder_nodes.source_count+1,
                    revision=MAX(catalog_folder_nodes.revision,excluded.revision)
                """,
                [
                    (
                        agent_id,
                        node_id,
                        root_id,
                        path,
                        parent,
                        name,
                        revision,
                    )
                    for path, parent, name in nodes
                ],
            )
            return
        if delta < 0:
            connection.executemany(
                """
                UPDATE catalog_folder_nodes
                SET source_count=source_count-1,revision=MAX(revision,?)
                WHERE agent_id=? AND root_id=? AND path=?
                """,
                [(revision, agent_id, root_id, path) for path, _parent, _name in nodes],
            )
            connection.execute(
                "DELETE FROM catalog_folder_nodes WHERE source_count<=0"
            )
            return
        connection.executemany(
            """
            UPDATE catalog_folder_nodes
            SET node_id=?,revision=MAX(revision,?)
            WHERE agent_id=? AND root_id=? AND path=?
            """,
            [
                (node_id, revision, agent_id, root_id, path)
                for path, _parent, _name in nodes
            ],
        )

    def _repair_contextual_audio_identity(
        self, connection: sqlite3.Connection
    ) -> dict[str, int]:
        marker = connection.execute(
            "SELECT value FROM coordinator_meta "
            "WHERE key='audio_context_identity_revision'"
        ).fetchone()
        if marker and str(marker["value"]) == AUDIO_CONTEXT_IDENTITY_REVISION:
            return {
                "migration_applied": 0,
                "audio_items": 0,
                "repaired_items": 0,
                "rebuilt_memberships": 0,
                "removed_collections": 0,
                "removed_works": 0,
            }
        rows = connection.execute(
            """
            SELECT id,name,folder_path,metadata_json,node_id,source_id,
                work_id,variant_id,collection_id
            FROM catalog_items NOT INDEXED
            WHERE agent_id<>'' AND media_kind='audio'
            """
        ).fetchall()
        now = now_iso()
        work_records: dict[str, tuple[Any, ...]] = {}
        collection_records: dict[str, tuple[Any, ...]] = {}
        variant_updates: list[tuple[Any, ...]] = []
        catalog_updates: list[tuple[Any, ...]] = []
        membership_deletes: list[tuple[str]] = []
        membership_records: list[tuple[Any, ...]] = []
        for row in rows:
            metadata = _json_loads(row["metadata_json"])
            work, collections, membership = self._classify_source(
                str(row["name"]),
                "audio",
                str(row["folder_path"]),
                metadata if isinstance(metadata, Mapping) else {},
            )
            title = _text(work.get("canonical_title"))
            kind = _text(work.get("media_kind")) or "other"
            work_identity = _text(work.get("identity_key")) or title
            work_id = _stable_id(
                "work", kind, work_identity.casefold(), size=24
            )
            work_records[work_id] = (
                work_id,
                WORK_SCHEMA,
                kind,
                title,
                title.casefold(),
                _json_dumps(work.get("metadata") or {}),
                now,
                now,
            )
            collection_ids: list[str] = []
            for collection in collections:
                value = dict(collection)
                parent_index = value.pop("parent_index", None)
                if parent_index is not None:
                    value["parent_id"] = collection_ids[int(parent_index)]
                collection_kind = _text(value.get("kind"))
                collection_title = _text(value.get("title"))
                parent_id = _text(value.get("parent_id"))
                collection_identity = (
                    _text(value.get("identity_key")) or collection_title
                )
                collection_id = _stable_id(
                    "collection",
                    collection_kind,
                    collection_identity.casefold(),
                    parent_id,
                    size=24,
                )
                collection_ids.append(collection_id)
                collection_records[collection_id] = (
                    collection_id,
                    COLLECTION_SCHEMA,
                    collection_kind,
                    collection_title,
                    parent_id,
                    _text(value.get("ownership")) or "derived",
                    _json_dumps(value.get("metadata") or {}),
                    now,
                    now,
                )
            collection_id = collection_ids[-1] if collection_ids else ""
            variant_id = str(row["variant_id"])
            changed = (
                str(row["work_id"]) != work_id
                or str(row["collection_id"]) != collection_id
            )
            if not changed:
                continue
            source_id = str(row["source_id"])
            variant_updates.append(
                (work_id, str(row["node_id"]), source_id, work_id)
            )
            catalog_updates.append((work_id, collection_id, str(row["id"])))
            if not variant_id:
                continue
            membership_deletes.append((variant_id,))
            for membership_collection_id in collection_ids:
                membership_records.append(
                    (
                        membership_collection_id,
                        work_id,
                        variant_id,
                        int(membership.get("ordinal") or 0),
                        membership.get("season_number"),
                        membership.get("episode_number"),
                        membership.get("disc_number"),
                        membership.get("track_number"),
                        membership.get("chapter_number"),
                    )
                )

        connection.executemany(
            """
            INSERT INTO media_works(
                id,schema_name,media_kind,canonical_title,sort_title,
                metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at,
                revision=media_works.revision+1
            """,
            work_records.values(),
        )
        connection.executemany(
            """
            INSERT INTO media_collections(
                id,schema_name,kind,title,parent_id,ownership,metadata_json,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at,
                revision=media_collections.revision+1
            """,
            collection_records.values(),
        )
        connection.executemany(
            """
            UPDATE media_variants SET work_id=?,revision=revision+1
            WHERE node_id=? AND source_id=? AND work_id<>?
            """,
            variant_updates,
        )
        connection.executemany(
            """
            UPDATE media_variants SET work_id=?,revision=revision+1
            WHERE node_id=? AND exact_source_id=? AND source_id<>?
                AND work_id<>?
            """,
            [
                (work_id, node_id, source_id, source_id, current_work_id)
                for work_id, node_id, source_id, current_work_id in variant_updates
            ],
        )
        connection.executemany(
            "UPDATE catalog_items SET work_id=?,collection_id=? WHERE id=?",
            catalog_updates,
        )
        connection.executemany(
            "DELETE FROM collection_memberships WHERE variant_id=?",
            membership_deletes,
        )
        connection.executemany(
            """
            INSERT INTO collection_memberships(
                collection_id,work_id,variant_id,ordinal,season_number,
                episode_number,disc_number,track_number,chapter_number,revision
            ) VALUES (?,?,?,?,?,?,?,?,?,1)
            """,
            membership_records,
        )

        removed_collections = 0
        while True:
            removed = connection.execute(
                """
                DELETE FROM media_collections
                WHERE NOT EXISTS (
                        SELECT 1 FROM catalog_items c
                        WHERE c.collection_id=media_collections.id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM collection_memberships m
                        WHERE m.collection_id=media_collections.id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM media_collections child
                        WHERE child.parent_id=media_collections.id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM metadata_claims claim
                        WHERE claim.subject_ref='collection:' || media_collections.id
                    )
                """
            ).rowcount
            removed_collections += max(0, int(removed or 0))
            if not removed:
                break
        removed_works = connection.execute(
            """
            DELETE FROM media_works
            WHERE NOT EXISTS (
                    SELECT 1 FROM catalog_items c WHERE c.work_id=media_works.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM media_variants v WHERE v.work_id=media_works.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM collection_memberships m
                    WHERE m.work_id=media_works.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM metadata_claims claim
                    WHERE claim.subject_ref='work:' || media_works.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM catalog_aliases alias
                    WHERE alias.alias_id=media_works.id
                        OR alias.canonical_id=media_works.id
                )
            """
        ).rowcount
        connection.execute(
            "INSERT OR REPLACE INTO coordinator_meta(key,value) VALUES (?,?)",
            ("audio_context_identity_revision", AUDIO_CONTEXT_IDENTITY_REVISION),
        )
        return {
            "migration_applied": 1,
            "audio_items": len(rows),
            "repaired_items": len(catalog_updates),
            "rebuilt_memberships": len(membership_records),
            "removed_collections": removed_collections,
            "removed_works": max(0, int(removed_works or 0)),
        }

    def _repair_video_series_identity(
        self, connection: sqlite3.Connection
    ) -> dict[str, int]:
        marker = connection.execute(
            "SELECT value FROM coordinator_meta "
            "WHERE key='video_series_identity_revision'"
        ).fetchone()
        if marker and str(marker["value"]) == VIDEO_SERIES_IDENTITY_REVISION:
            return {
                "migration_applied": 0,
                "episode_items": 0,
                "repaired_items": 0,
                "removed_collections": 0,
                "removed_works": 0,
            }
        rows = connection.execute(
            """
            SELECT id,name,folder_path,metadata_json,node_id,source_id,
                work_id,variant_id,collection_id
            FROM catalog_items NOT INDEXED
            WHERE agent_id<>'' AND media_kind='video'
            """
        ).fetchall()
        repaired = 0
        episodes = 0
        for row in rows:
            if not _SEASON_EPISODE.search(str(row["name"])):
                continue
            episodes += 1
            metadata = _json_loads(row["metadata_json"])
            work, collections, membership = self._classify_source(
                str(row["name"]),
                "video",
                str(row["folder_path"]),
                metadata if isinstance(metadata, Mapping) else {},
            )
            if not collections or _text(collections[0].get("kind")) != "series":
                continue
            work_id, collection_ids = self._upsert_classification(
                connection, work, collections
            )
            collection_id = collection_ids[-1] if collection_ids else ""
            variant_id = str(row["variant_id"])
            classification_changed = not (
                str(row["work_id"]) == work_id
                and str(row["collection_id"]) == collection_id
            )
            if classification_changed:
                connection.execute(
                    """
                    UPDATE media_variants SET work_id=?,revision=revision+1
                    WHERE node_id=? AND source_id=?
                    """,
                    (
                        work_id,
                        str(row["node_id"]),
                        str(row["source_id"]),
                    ),
                )
                connection.execute(
                    """
                    UPDATE media_variants SET work_id=?,revision=revision+1
                    WHERE node_id=? AND exact_source_id=? AND source_id<>?
                    """,
                    (
                        work_id,
                        str(row["node_id"]),
                        str(row["source_id"]),
                        str(row["source_id"]),
                    ),
                )
            connection.execute(
                "DELETE FROM collection_memberships WHERE variant_id=?",
                (variant_id,),
            )
            for membership_collection_id in collection_ids:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO collection_memberships(
                        collection_id,work_id,variant_id,ordinal,season_number,
                        episode_number,disc_number,track_number,chapter_number,
                        revision
                    ) VALUES (?,?,?,?,?,?,?,?,?,1)
                    """,
                    (
                        membership_collection_id,
                        work_id,
                        variant_id,
                        int(membership.get("ordinal") or 0),
                        membership.get("season_number"),
                        membership.get("episode_number"),
                        membership.get("disc_number"),
                        membership.get("track_number"),
                        membership.get("chapter_number"),
                    ),
                )
            if classification_changed:
                connection.execute(
                    "UPDATE catalog_items SET work_id=?,collection_id=? WHERE id=?",
                    (work_id, collection_id, str(row["id"])),
                )
                repaired += 1
        removed_collections = 0
        for _depth in range(8):
            removed = connection.execute(
                """
                DELETE FROM media_collections
                WHERE NOT EXISTS (
                        SELECT 1 FROM catalog_items c
                        WHERE c.collection_id=media_collections.id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM collection_memberships m
                        WHERE m.collection_id=media_collections.id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM media_collections child
                        WHERE child.parent_id=media_collections.id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM metadata_claims claim
                        WHERE claim.subject_ref='collection:' || media_collections.id
                    )
                """
            ).rowcount
            removed_collections += max(0, int(removed or 0))
            if not removed:
                break
        removed_works = connection.execute(
            """
            DELETE FROM media_works
            WHERE NOT EXISTS (
                    SELECT 1 FROM catalog_items c WHERE c.work_id=media_works.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM media_variants v WHERE v.work_id=media_works.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM collection_memberships m
                    WHERE m.work_id=media_works.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM metadata_claims claim
                    WHERE claim.subject_ref='work:' || media_works.id
                )
            """
        ).rowcount
        connection.execute(
            "INSERT OR REPLACE INTO coordinator_meta(key,value) VALUES (?,?)",
            ("video_series_identity_revision", VIDEO_SERIES_IDENTITY_REVISION),
        )
        return {
            "migration_applied": 1,
            "episode_items": episodes,
            "repaired_items": repaired,
            "removed_collections": removed_collections,
            "removed_works": max(0, int(removed_works or 0)),
        }

    def _backfill_search(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT rowid AS search_rowid,id,title,name,source_path,folder_path,
                metadata_json
            FROM catalog_items
            WHERE search_text=''
            """
        ).fetchall()
        updates: list[tuple[str, str]] = []
        search_rows: list[tuple[int, str, str]] = []
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
            search_rows.append((int(row["search_rowid"]), item_id, search_text))
        if updates:
            connection.executemany("UPDATE catalog_items SET search_text=? WHERE id=?", updates)
            for start in range(0, len(search_rows), 400):
                batch = search_rows[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                connection.execute(
                    f"DELETE FROM catalog_search WHERE rowid IN ({placeholders})",
                    tuple(rowid for rowid, _item_id, _value in batch),
                )
                connection.execute(
                    f"DELETE FROM catalog_fuzzy_search WHERE rowid IN ({placeholders})",
                    tuple(rowid for rowid, _item_id, _value in batch),
                )
            connection.executemany(
                "INSERT INTO catalog_search(rowid,item_id,text) VALUES (?,?,?)",
                search_rows,
            )
            connection.executemany(
                "INSERT INTO catalog_fuzzy_search(rowid,item_id,tokens) VALUES (?,?,?)",
                [
                    (rowid, item_id, self._fuzzy_tokens(search_text))
                    for rowid, item_id, search_text in search_rows
                ],
            )
        catalog_count = int(
            connection.execute("SELECT COUNT(*) FROM catalog_items").fetchone()[0]
        )
        fuzzy_count = int(
            connection.execute("SELECT COUNT(*) FROM catalog_fuzzy_search").fetchone()[0]
        )
        if catalog_count != fuzzy_count:
            connection.execute("DELETE FROM catalog_fuzzy_search")
            rows = connection.execute(
                "SELECT rowid AS search_rowid,id,search_text "
                "FROM catalog_items ORDER BY rowid"
            ).fetchall()
            connection.executemany(
                "INSERT INTO catalog_fuzzy_search(rowid,item_id,tokens) VALUES (?,?,?)",
                [
                    (
                        int(row["search_rowid"]),
                        str(row["id"]),
                        self._fuzzy_tokens(row["search_text"]),
                    )
                    for row in rows
                ],
            )

    def _ensure_search_rowids(self, connection: sqlite3.Connection) -> None:
        marker = connection.execute(
            "SELECT value FROM coordinator_meta WHERE key='search_rowid_revision'"
        ).fetchone()
        if marker and str(marker["value"]) == SEARCH_ROWID_REVISION:
            return
        rows = connection.execute(
            "SELECT rowid AS search_rowid,id,search_text "
            "FROM catalog_items ORDER BY rowid"
        ).fetchall()
        connection.execute("DELETE FROM catalog_search")
        connection.execute("DELETE FROM catalog_fuzzy_search")
        connection.executemany(
            "INSERT INTO catalog_search(rowid,item_id,text) VALUES (?,?,?)",
            [
                (int(row["search_rowid"]), str(row["id"]), str(row["search_text"]))
                for row in rows
            ],
        )
        connection.executemany(
            "INSERT INTO catalog_fuzzy_search(rowid,item_id,tokens) VALUES (?,?,?)",
            [
                (
                    int(row["search_rowid"]),
                    str(row["id"]),
                    self._fuzzy_tokens(row["search_text"]),
                )
                for row in rows
            ],
        )
        connection.execute(
            "INSERT OR REPLACE INTO coordinator_meta(key,value) VALUES (?,?)",
            ("search_rowid_revision", SEARCH_ROWID_REVISION),
        )

    @staticmethod
    def _claim_priority(row: Mapping[str, Any]) -> tuple[int, float, str]:
        provenance = _text(row["provenance"]).lower()
        if bool(row["preferred"]):
            rank = 1000
        elif provenance.startswith(("profile:", "user:", "actor:")):
            rank = 900
        elif "local_nfo" in provenance:
            rank = 800
        elif any(token in provenance for token in ("tmdb", "musicbrainz", "audiodb")):
            rank = 600
        elif "deterministic_local" in provenance:
            rank = 200
        else:
            rank = 400
        return rank, float(row["confidence"] or 0.0), _text(row["created_at"])

    @staticmethod
    def _numeric(value: Any, default: float | None = None) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _resolved_metadata(
        self,
        connection: sqlite3.Connection,
        item_id: str,
        base_metadata: Mapping[str, Any],
        *,
        work_id: str = "",
    ) -> tuple[dict[str, Any], dict[str, str]]:
        metadata = dict(base_metadata)
        provenance = {
            str(key): "media_library_agent.source_metadata.v1"
            for key, value in metadata.items()
            if value not in (None, "", [], {})
        }
        subjects = [f"item:{item_id}"]
        if _text(work_id):
            subjects.append(f"work:{_text(work_id)}")
        placeholders = ",".join("?" for _ in subjects)
        rows = connection.execute(
            f"SELECT * FROM metadata_claims WHERE subject_ref IN ({placeholders})",
            tuple(subjects),
        ).fetchall()
        winners: dict[str, sqlite3.Row] = {}
        aliases = {"canonical_title": "title", "vote_average": "rating"}
        for row in rows:
            field = aliases.get(str(row["field_name"]), str(row["field_name"]))
            previous = winners.get(field)
            if previous is None or self._claim_priority(row) > self._claim_priority(previous):
                winners[field] = row
        for field, row in winners.items():
            value = _json_loads(row["value_json"])
            if value not in (None, "", [], {}):
                metadata[field] = value
                provenance[field] = str(row["provenance"])
        return metadata, provenance

    def _refresh_metadata_projection(
        self, connection: sqlite3.Connection, item_id: str
    ) -> None:
        row = connection.execute(
            "SELECT id,name,source_path,folder_path,metadata_json,work_id,title "
            "FROM catalog_items WHERE id=?",
            (_text(item_id),),
        ).fetchone()
        if row is None:
            return
        base = _json_loads(row["metadata_json"])
        metadata, provenance = self._resolved_metadata(
            connection,
            str(row["id"]),
            base if isinstance(base, Mapping) else {},
            work_id=str(row["work_id"]),
        )
        title = _text(metadata.get("title")) or str(row["title"])
        year_number = self._numeric(metadata.get("year") or metadata.get("release_year"))
        year = int(year_number) if year_number is not None else None
        rating = self._numeric(metadata.get("rating") or metadata.get("vote_average"))
        critic_rating = self._numeric(metadata.get("critic_rating"))
        audience_rating = self._numeric(metadata.get("audience_rating"))
        duration_ms_number = self._numeric(metadata.get("duration_ms"), 0.0) or 0.0
        if duration_ms_number <= 0:
            duration_seconds = self._numeric(metadata.get("duration_seconds"), 0.0) or 0.0
            duration_ms_number = duration_seconds * 1000.0
        genres = metadata.get("genres") or metadata.get("categories") or []
        artists = metadata.get("artists") or metadata.get("artist") or []
        if isinstance(genres, str):
            genres = [genres]
        if isinstance(artists, str):
            artists = [artists]
        genres = [_text(value) for value in list(genres)[:100] if _text(value)]
        artists = [_text(value) for value in list(artists)[:100] if _text(value)]
        previous = connection.execute(
            "SELECT revision FROM catalog_metadata_projection WHERE item_id=?",
            (str(row["id"]),),
        ).fetchone()
        revision = int(previous["revision"] or 0) + 1 if previous else 1
        connection.execute(
            """
            INSERT INTO catalog_metadata_projection(
                item_id,metadata_json,provenance_json,title,year,release_date,
                rating,critic_rating,audience_rating,content_rating,duration_ms,
                genres_json,artists_json,album,series,revision,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET
                metadata_json=excluded.metadata_json,
                provenance_json=excluded.provenance_json,title=excluded.title,
                year=excluded.year,release_date=excluded.release_date,
                rating=excluded.rating,critic_rating=excluded.critic_rating,
                audience_rating=excluded.audience_rating,
                content_rating=excluded.content_rating,
                duration_ms=excluded.duration_ms,genres_json=excluded.genres_json,
                artists_json=excluded.artists_json,album=excluded.album,
                series=excluded.series,revision=excluded.revision,
                updated_at=excluded.updated_at
            """,
            (
                str(row["id"]), _json_dumps(metadata), _json_dumps(provenance),
                title, year, _text(metadata.get("release_date")), rating,
                critic_rating, audience_rating, _text(metadata.get("content_rating")),
                max(0, int(duration_ms_number)), _json_dumps(genres),
                _json_dumps(artists), _text(metadata.get("album")),
                _text(metadata.get("series")), revision, now_iso(),
            ),
        )
        connection.execute(
            "DELETE FROM catalog_metadata_facets WHERE item_id=?", (str(row["id"]),)
        )
        facet_rows: list[tuple[Any, ...]] = []
        for field, values in (
            ("genre", genres),
            ("artist", artists),
            ("tag", metadata.get("tags") or []),
            ("country", metadata.get("countries") or []),
            ("director", metadata.get("directors") or []),
        ):
            if isinstance(values, str):
                values = [values]
            for value in list(values)[:100]:
                display = _text(value)
                normalized = fold_text(display)
                if display and normalized:
                    facet_rows.append((str(row["id"]), field, normalized, display, None))
        for field, value in (
            ("year", year),
            ("content_rating", _text(metadata.get("content_rating"))),
            ("album", _text(metadata.get("album"))),
            ("series", _text(metadata.get("series"))),
        ):
            if value not in (None, ""):
                facet_rows.append(
                    (str(row["id"]), field, fold_text(value), _text(value), self._numeric(value))
                )
        if facet_rows:
            connection.executemany(
                "INSERT OR REPLACE INTO catalog_metadata_facets("
                "item_id,field_name,normalized_value,display_value,numeric_value) "
                "VALUES (?,?,?,?,?)",
                facet_rows,
            )
        search_text = self._search_text(
            title=title,
            name=row["name"],
            relative_path=row["source_path"],
            folder_path=row["folder_path"],
            metadata=metadata,
        )
        connection.execute(
            "UPDATE catalog_items SET title=?,search_text=? WHERE id=?",
            (title, search_text, str(row["id"])),
        )
        self._replace_search(connection, str(row["id"]), search_text)

    def _backfill_metadata_projections(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT id FROM catalog_items WHERE id NOT IN "
            "(SELECT item_id FROM catalog_metadata_projection) ORDER BY id"
        ).fetchall()
        for row in rows:
            self._refresh_metadata_projection(connection, str(row["id"]))

    @staticmethod
    def _metadata_projection_map(
        connection: sqlite3.Connection, item_ids: Iterable[str]
    ) -> dict[str, sqlite3.Row]:
        tokens = [str(value) for value in item_ids if _text(value)]
        if not tokens:
            return {}
        placeholders = ",".join("?" for _ in tokens)
        return {
            str(row["item_id"]): row
            for row in connection.execute(
                f"SELECT * FROM catalog_metadata_projection WHERE item_id IN ({placeholders})",
                tuple(tokens),
            ).fetchall()
        }

    def refresh_search_index(self, *, force_legacy: bool = False) -> dict[str, Any]:
        with self.repository.connect() as connection:
            if force_legacy:
                connection.execute(
                    "UPDATE catalog_items SET search_text='' WHERE agent_id=''"
                )
                connection.execute(
                    """
                    UPDATE catalog_items SET
                        maturity_rating=MAX(0,MIN(21,COALESCE(CAST(
                            json_extract(metadata_json,'$.maturity_rating')
                            AS INTEGER),0))),
                        explicit=CASE WHEN lower(CAST(COALESCE(
                            json_extract(metadata_json,'$.explicit'),0) AS TEXT))
                            IN ('1','true','yes','on') THEN 1 ELSE 0 END
                    WHERE agent_id=''
                    """
                )
            self._backfill_search(connection)
            self._rebuild_folder_nodes(connection)
            connection.commit()
            indexed = int(
                connection.execute("SELECT COUNT(*) FROM catalog_items").fetchone()[0]
            )
            folder_nodes = int(
                connection.execute(
                    "SELECT COUNT(*) FROM catalog_folder_nodes"
                ).fetchone()[0]
            )
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "indexed_count": indexed,
            "folder_node_count": folder_nodes,
            "force_legacy": bool(force_legacy),
        }

    @staticmethod
    def _replace_search(connection: sqlite3.Connection, item_id: str, search_text: str) -> None:
        row = connection.execute(
            "SELECT rowid AS search_rowid FROM catalog_items WHERE id=?", (item_id,)
        ).fetchone()
        if row is None:
            return
        search_rowid = int(row["search_rowid"])
        connection.execute("DELETE FROM catalog_search WHERE rowid=?", (search_rowid,))
        connection.execute(
            "INSERT INTO catalog_search(rowid,item_id,text) VALUES (?,?,?)",
            (search_rowid, item_id, search_text),
        )
        connection.execute(
            "DELETE FROM catalog_fuzzy_search WHERE rowid=?", (search_rowid,)
        )
        connection.execute(
            "INSERT INTO catalog_fuzzy_search(rowid,item_id,tokens) VALUES (?,?,?)",
            (
                search_rowid,
                item_id,
                MediaCatalogCoordinator._fuzzy_tokens(search_text),
            ),
        )

    @staticmethod
    def _fuzzy_tokens(value: Any) -> str:
        compact = fold_text(value).replace(" ", "_")[:1024]
        trigrams = {
            compact[index : index + 3]
            for index in range(max(0, len(compact) - 2))
        }
        return " ".join(
            token.encode("ascii", errors="ignore").hex()
            for token in sorted(trigrams)
            if token
        )

    @staticmethod
    def _search_text(*, title: Any, name: Any, relative_path: Any, folder_path: Any, metadata: Mapping[str, Any]) -> str:
        values: list[str] = [_text(title), _text(name), _text(relative_path), _text(folder_path)]
        for key in (
            "folder_segments", "tags", "genres", "categories", "artists",
            "people", "actors", "directors", "countries", "aliases",
        ):
            value = metadata.get(key)
            if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
                for item in list(value)[:100]:
                    if isinstance(item, Mapping):
                        values.extend(
                            _text(item.get(field)) for field in ("name", "role")
                        )
                    else:
                        values.append(_text(item))
            elif value:
                values.append(_text(value))
        values.extend(
            _text(metadata.get(key))
            for key in (
                "album", "series", "root_label", "plot", "overview", "tagline",
                "original_title", "sort_title",
            )
        )
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
        library_state = (
            page.get("library_state")
            if isinstance(page.get("library_state"), Mapping)
            else {}
        )
        observed_counts = {
            key: _observed_count(library_state.get(key))
            for key in (
                "root_count",
                "source_count",
                "available_count",
                "active_job_count",
                "failed_job_count",
            )
        }
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
                INSERT INTO agent_catalog_state(
                    agent_id, instance_id, node_id, cursor, last_sequence,
                    availability, freshness, last_error, root_count,
                    source_count, available_count, active_job_count,
                    failed_job_count, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'available', 'fresh', '', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    instance_id=CASE WHEN excluded.instance_id<>''
                        THEN excluded.instance_id
                        ELSE agent_catalog_state.instance_id END,
                    node_id=excluded.node_id, cursor=excluded.cursor,
                    last_sequence=MAX(agent_catalog_state.last_sequence, excluded.last_sequence),
                    root_count=CASE WHEN excluded.root_count>=0
                        THEN excluded.root_count ELSE agent_catalog_state.root_count END,
                    source_count=CASE WHEN excluded.source_count>=0
                        THEN excluded.source_count ELSE agent_catalog_state.source_count END,
                    available_count=CASE WHEN excluded.available_count>=0
                        THEN excluded.available_count ELSE agent_catalog_state.available_count END,
                    active_job_count=CASE WHEN excluded.active_job_count>=0
                        THEN excluded.active_job_count ELSE agent_catalog_state.active_job_count END,
                    failed_job_count=CASE WHEN excluded.failed_job_count>=0
                        THEN excluded.failed_job_count ELSE agent_catalog_state.failed_job_count END,
                    availability='available', freshness='fresh', last_error='', updated_at=excluded.updated_at
                """,
                (
                    agent_id,
                    _text(instance_id),
                    node_id,
                    _text(page.get("next_cursor")),
                    last_sequence,
                    observed_counts["root_count"],
                    observed_counts["source_count"],
                    observed_counts["available_count"],
                    observed_counts["active_job_count"],
                    observed_counts["failed_job_count"],
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
            "SELECT id,source_revision,variant_id,folder_path,missing,"
            "agent_id,node_id,root_id FROM catalog_items "
            "WHERE agent_id=? AND source_id=?",
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
                if not bool(previous["missing"]):
                    self._adjust_folder_nodes(
                        connection,
                        agent_id=str(previous["agent_id"]),
                        node_id=str(previous["node_id"]),
                        root_id=str(previous["root_id"]),
                        folder_path=str(previous["folder_path"]),
                        delta=-1,
                        revision=revision,
                    )
                connection.execute(
                    """
                    UPDATE media_variants SET available=0,revision=revision+1
                    WHERE node_id=? AND source_id=?
                    """,
                    (node_id, source_id),
                )
                connection.execute(
                    """
                    UPDATE media_variants SET available=0,revision=revision+1
                    WHERE node_id=? AND exact_source_id=? AND source_id<>?
                    """,
                    (node_id, source_id, source_id),
                )
            return "removed"

        descriptor = dict(source.get("descriptor") or {})
        metadata = dict(source.get("metadata") or descriptor.get("metadata") or {})
        name = _text(source.get("name") or descriptor.get("name"))
        if not name:
            return "ignored"
        relative_path = _text(source.get("relative_path") or metadata.get("relative_path") or name).replace("\\", "/")
        folder_path = "/".join(
            part
            for part in _text(
                source.get("folder_path") or metadata.get("folder_path")
            )
            .replace("\\", "/")
            .split("/")
            if part
        )
        mime_type = _text(source.get("mime_type") or descriptor.get("mime_type") or descriptor.get("mime")) or "application/octet-stream"
        kind = _text(source.get("media_kind")) or _media_kind(mime_type, name)
        title = (
            _text(metadata.get("title"))
            or _text(descriptor.get("title"))
            or _title_from_name(name)
        )
        work, collections, membership = self._classify_source(
            name, kind, folder_path, metadata
        )
        work_id, collection_ids = self._upsert_classification(
            connection, work, collections
        )
        collection_id = collection_ids[-1] if collection_ids else ""
        variant_id = (
            str(previous["variant_id"])
            if previous and _text(previous["variant_id"])
            else _stable_id("variant", node_id, source_id, size=24)
        )
        item_id = str(previous["id"]) if previous else _stable_id("mc", agent_id, source_id, size=24)
        content_path = _text(descriptor.get("content_path"))
        routed_path = _text(descriptor.get("routed_content_path") or descriptor.get("browser_path"))
        source_path = _text(descriptor.get("source_path") or descriptor.get("path"))
        retired_legacy_rows: list[sqlite3.Row] = []
        if source_path:
            retired_legacy_rows = connection.execute(
                """
                SELECT agent_id,node_id,root_id,folder_path
                FROM catalog_items
                WHERE source='media_server' AND agent_id='' AND missing=0
                    AND source_path=?
                """,
                (source_path,),
            ).fetchall()
            connection.execute(
                """
                UPDATE catalog_items
                SET missing=1,last_seen_at=?
                WHERE source='media_server' AND agent_id='' AND missing=0
                    AND source_path=?
                """,
                (now_iso(), source_path),
            )
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
        try:
            maturity_rating = max(
                0, min(21, int(metadata.get("maturity_rating") or 0))
            )
        except (TypeError, ValueError):
            maturity_rating = 0
        explicit_value = metadata.get("explicit", False)
        if isinstance(explicit_value, str):
            explicit_value = explicit_value.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        catalog_revision = self._next_catalog_revision(connection)
        connection.execute(
            """
            INSERT INTO catalog_items(
                id, source, resource_id, name, title, media_kind, mime_type, size_bytes,
                modified_at, content_path, routed_content_path, playback_id, source_path,
                descriptor_json, metadata_json, fingerprint, indexed_at, last_seen_at,
                missing, favorite, play_count, tags_json, agent_id, node_id, root_id,
                source_id, source_revision, folder_path, search_text, catalog_revision,
                work_id, variant_id, collection_id, quality_json,
                maturity_rating, explicit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, '[]', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                collection_id=excluded.collection_id, quality_json=excluded.quality_json,
                maturity_rating=excluded.maturity_rating,
                explicit=excluded.explicit
            """,
            (
                item_id, f"agent:{agent_id}", _text(source.get("resource_id") or descriptor.get("resource_id") or descriptor.get("id")),
                name, title, kind, mime_type, int(source.get("size_bytes") or descriptor.get("size_bytes") or 0),
                modified_at, content_path, routed_path, _text(descriptor.get("playback_id")), source_path,
                _json_dumps(descriptor), _json_dumps(metadata), _text(source.get("fingerprint")), now_iso(), now_iso(),
                agent_id, node_id, _text(source.get("root_id") or delta.get("root_id")), source_id, source_revision,
                folder_path, search_text, catalog_revision, work_id, variant_id, collection_id,
                _json_dumps(self._quality(descriptor, metadata)),
                maturity_rating, int(bool(explicit_value)),
            ),
        )
        previous_scope = (
            str(previous["agent_id"]),
            str(previous["root_id"]),
            str(previous["folder_path"]),
        ) if previous and not bool(previous["missing"]) else None
        current_scope = (agent_id, _text(source.get("root_id") or delta.get("root_id")), folder_path)
        if previous_scope and previous_scope != current_scope:
            self._adjust_folder_nodes(
                connection,
                agent_id=previous_scope[0],
                node_id=str(previous["node_id"]),
                root_id=previous_scope[1],
                folder_path=previous_scope[2],
                delta=-1,
                revision=catalog_revision,
            )
        self._adjust_folder_nodes(
            connection,
            agent_id=current_scope[0],
            node_id=node_id,
            root_id=current_scope[1],
            folder_path=current_scope[2],
            delta=0 if previous_scope == current_scope else 1,
            revision=catalog_revision,
        )
        for legacy_row in retired_legacy_rows:
            self._adjust_folder_nodes(
                connection,
                agent_id=str(legacy_row["agent_id"]),
                node_id=str(legacy_row["node_id"]),
                root_id=str(legacy_row["root_id"]),
                folder_path=str(legacy_row["folder_path"]),
                delta=-1,
                revision=catalog_revision,
            )
        self._replace_search(connection, item_id, search_text)
        connection.execute(
            """
            INSERT INTO media_variants(
                id,work_id,source_id,node_id,media_kind,mime_type,quality_json,
                available,revision,descriptor_json,resource_id,source_revision,
                exact_source_id,exact_source_revision,derived
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET work_id=excluded.work_id, media_kind=excluded.media_kind,
                mime_type=excluded.mime_type,quality_json=excluded.quality_json,
                available=1,descriptor_json=excluded.descriptor_json,
                resource_id=excluded.resource_id,source_revision=excluded.source_revision,
                exact_source_id=excluded.exact_source_id,
                exact_source_revision=excluded.exact_source_revision,derived=0,
                revision=media_variants.revision+1
            """,
            (
                variant_id,
                work_id,
                source_id,
                node_id,
                kind,
                mime_type,
                _json_dumps(self._quality(descriptor, metadata)),
                _json_dumps(descriptor),
                _text(
                    source.get("resource_id")
                    or descriptor.get("resource_id")
                    or descriptor.get("id")
                ),
                source_revision,
                source_id,
                source_revision,
            ),
        )
        connection.execute(
            "DELETE FROM media_variants WHERE node_id=? AND exact_source_id=? AND derived=1",
            (node_id, source_id),
        )
        for derived in metadata.get("derived_renditions") or []:
            if not isinstance(derived, Mapping):
                continue
            if (
                _text(derived.get("exact_source_id")) != source_id
                or _text(derived.get("exact_source_fingerprint"))
                != _text(source.get("fingerprint"))
            ):
                continue
            derived_descriptor = dict(derived.get("descriptor") or {})
            derived_id = _text(derived.get("id"))
            if not derived_id or not derived_descriptor:
                continue
            derived_mime = _text(
                derived.get("mime_type")
                or derived_descriptor.get("mime_type")
                or derived_descriptor.get("mime")
            )
            derived_quality = dict(derived.get("quality") or {})
            derived_variant_id = _stable_id(
                "variant", node_id, derived_id, size=24
            )
            connection.execute(
                """
                INSERT INTO media_variants(
                    id,work_id,source_id,node_id,media_kind,mime_type,
                    quality_json,available,revision,descriptor_json,resource_id,
                    source_revision,exact_source_id,exact_source_revision,derived
                ) VALUES (?,?,?,?,?,?,?,1,1,?,?,?,?,?,1)
                ON CONFLICT(id) DO UPDATE SET
                    work_id=excluded.work_id,mime_type=excluded.mime_type,
                    quality_json=excluded.quality_json,available=1,
                    descriptor_json=excluded.descriptor_json,
                    resource_id=excluded.resource_id,
                    source_revision=excluded.source_revision,
                    exact_source_id=excluded.exact_source_id,
                    exact_source_revision=excluded.exact_source_revision,
                    derived=1,revision=media_variants.revision+1
                """,
                (
                    derived_variant_id,
                    work_id,
                    derived_id,
                    node_id,
                    kind,
                    derived_mime,
                    _json_dumps(derived_quality),
                    _json_dumps(derived_descriptor),
                    _text(
                        derived_descriptor.get("resource_id")
                        or derived_descriptor.get("id")
                    ),
                    source_revision,
                    source_id,
                    int(derived.get("exact_source_revision") or 0),
                ),
            )
        connection.execute(
            "DELETE FROM collection_memberships WHERE variant_id=?",
            (variant_id,),
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
        subject_ref = f"item:{item_id}"
        connection.execute(
            "DELETE FROM metadata_claims WHERE subject_ref=? AND provenance=?",
            (subject_ref, "media_library_agent.local_nfo.v1"),
        )
        local_nfo = metadata.get("local_nfo")
        local_nfo_values = (
            local_nfo.get("values") if isinstance(local_nfo, Mapping) else None
        )
        if isinstance(local_nfo_values, Mapping):
            for field_name, value in list(local_nfo_values.items())[:100]:
                if value in (None, "", [], {}):
                    continue
                self._record_metadata_claim_connection(
                    connection,
                    subject_ref=subject_ref,
                    field_name=str(field_name),
                    value=value,
                    provenance="media_library_agent.local_nfo.v1",
                    confidence=0.98,
                    preferred=False,
                )
        self._refresh_metadata_projection(connection, item_id)
        for job_kind, priority in (
            ("metadata_enrichment", 200),
            ("embedding", 600),
            ("fingerprint", 500),
        ):
            job_id = _stable_id(
                "mediajob", job_kind, item_id, source_revision, size=24
            )
            queued_at = now_iso()
            connection.execute(
                """
                UPDATE media_background_jobs
                SET status='canceled', error_code='superseded_source_revision',
                    finished_at=?, updated_at=?
                WHERE subject_ref=? AND kind=? AND status='queued' AND id<>?
                """,
                (queued_at, queued_at, subject_ref, job_kind, job_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO media_background_jobs(
                    id,kind,subject_ref,status,priority,created_at,updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    job_id,
                    job_kind,
                    subject_ref,
                    priority,
                    queued_at,
                    queued_at,
                ),
            )
            connection.execute(
                """
                DELETE FROM media_background_jobs WHERE id IN (
                    SELECT id FROM media_background_jobs
                    WHERE subject_ref=? AND kind=?
                        AND status IN ('completed','failed','canceled')
                    ORDER BY updated_at DESC,id DESC LIMIT -1 OFFSET 8
                )
                """,
                (subject_ref, job_kind),
            )
        return operation or "updated"

    @staticmethod
    def _quality(descriptor: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any]:
        technical = metadata.get("technical") if isinstance(metadata.get("technical"), Mapping) else {}
        streams = [
            dict(item)
            for item in technical.get("streams") or []
            if isinstance(item, Mapping)
        ][:64]
        video = next(
            (item for item in streams if _text(item.get("kind")) == "video"), {}
        )
        audio_tracks = [
            {
                "index": int(item.get("index") or 0),
                "codec": _text(item.get("codec")),
                "language": _text(item.get("language")),
                "title": _text(item.get("title")),
                "channels": int(item.get("channels") or 0),
                "channel_layout": _text(item.get("channel_layout")),
                "disposition": dict(item.get("disposition") or {}),
            }
            for item in streams
            if _text(item.get("kind")) == "audio"
        ]
        subtitle_tracks = [
            {
                "index": int(item.get("index") or 0),
                "codec": _text(item.get("codec")),
                "language": _text(item.get("language")),
                "title": _text(item.get("title")),
                "disposition": dict(item.get("disposition") or {}),
            }
            for item in streams
            if _text(item.get("kind")) == "subtitle"
        ]
        return {
            "technical_schema": _text(technical.get("schema")),
            "file_container": _text(technical.get("file_container")),
            "container": _text(technical.get("container")),
            "containers": list(technical.get("containers") or []),
            "width": int(video.get("width") or technical.get("width") or descriptor.get("width") or 0),
            "height": int(video.get("height") or technical.get("height") or descriptor.get("height") or 0),
            "bitrate": int(technical.get("bitrate") or descriptor.get("bitrate") or 0),
            "codec": _text(video.get("codec") or technical.get("codec") or descriptor.get("codec")),
            "profile": _text(video.get("profile")),
            "bit_depth": int(video.get("bit_depth") or 0),
            "frame_rate": float(video.get("frame_rate") or 0),
            "hdr_modes": list(technical.get("hdr_modes") or []),
            "language": _text(metadata.get("language") or (audio_tracks[0].get("language") if audio_tracks else "")),
            "audio_tracks": audio_tracks,
            "subtitle_tracks": subtitle_tracks,
        }

    @staticmethod
    def _endpoint_compatibility(
        quality: Mapping[str, Any],
        *,
        media_kind: str,
        mime_type: str,
        capabilities: Mapping[str, Any],
        preferred_language: str,
    ) -> dict[str, Any]:
        def tokens(value: Any) -> set[str]:
            if not isinstance(value, (list, tuple, set, frozenset)):
                return set()
            return {_text(item).lower() for item in value if _text(item)}

        supported_codecs = tokens(capabilities.get("codecs"))
        supported_containers = tokens(capabilities.get("containers"))
        supported_mime_types = {
            item.split(";", 1)[0].strip()
            for item in tokens(capabilities.get("mime_types"))
        }
        audio_tracks = [
            dict(item)
            for item in quality.get("audio_tracks") or []
            if isinstance(item, Mapping)
        ]
        language = _text(preferred_language).lower()
        selected_audio = next(
            (
                item
                for item in audio_tracks
                if language
                and _text(item.get("language")).lower() in {language, language[:2]}
            ),
            None,
        )
        selected_audio = selected_audio or next(
            (
                item
                for item in audio_tracks
                if bool((item.get("disposition") or {}).get("default"))
            ),
            None,
        )
        selected_audio = selected_audio or (audio_tracks[0] if audio_tracks else {})
        source_codec = _text(quality.get("codec")).lower()
        audio_codec = _text(selected_audio.get("codec")).lower()
        source_container = _text(
            quality.get("file_container") or quality.get("container")
        ).lower()
        source_mime = _text(mime_type).lower().split(";", 1)[0].strip()
        reasons: list[str] = []
        if supported_codecs and source_codec and source_codec not in supported_codecs:
            reasons.append(
                "video_codec_not_supported"
                if media_kind == "video"
                else "audio_codec_not_supported"
            )
        if (
            media_kind == "video"
            and supported_codecs
            and audio_codec
            and audio_codec not in supported_codecs
        ):
            reasons.append("audio_codec_not_supported")
        if (
            supported_containers
            and source_container
            and source_container not in supported_containers
        ):
            reasons.append("container_not_supported")
        if supported_mime_types and source_mime not in supported_mime_types:
            reasons.append("mime_type_not_supported")
        maximum_height = max(0, int(capabilities.get("max_video_height") or 0))
        if maximum_height and int(quality.get("height") or 0) > maximum_height:
            reasons.append("height_above_endpoint_limit")
        maximum_bitrate = max(0, int(capabilities.get("max_bitrate") or 0))
        if maximum_bitrate and int(quality.get("bitrate") or 0) > maximum_bitrate:
            reasons.append("bitrate_above_endpoint_limit")
        source_hdr = {
            _text(item).lower()
            for item in quality.get("hdr_modes") or []
            if _text(item) and _text(item).lower() != "sdr"
        }
        endpoint_hdr = tokens(capabilities.get("hdr_modes"))
        hdr_unsupported = bool(
            source_hdr and endpoint_hdr and source_hdr.isdisjoint(endpoint_hdr)
        )
        if hdr_unsupported:
            reasons.append("hdr_tone_mapping_deferred")
        restrictive = bool(
            supported_codecs
            or supported_containers
            or supported_mime_types
            or maximum_height
            or maximum_bitrate
            or endpoint_hdr
        )
        if not restrictive:
            reasons = ["endpoint_capabilities_not_restrictive"]
            mode = "direct"
        elif not reasons:
            reasons = ["direct_compatible"]
            mode = "direct"
        elif hdr_unsupported:
            mode = "unsupported"
        elif set(reasons).issubset(
            {"container_not_supported", "mime_type_not_supported"}
        ):
            mode = "remux"
        elif bool(capabilities.get("hls")) and media_kind == "video":
            mode = "prepared_hls"
        else:
            mode = "transcode"
        return {
            "schema": "adaos.playback.compatibility_decision.v1",
            "mode": mode,
            "ready": mode == "direct",
            "requires_preparation": mode in {"remux", "prepared_hls", "transcode"},
            "reasons": reasons,
            "selected_tracks": {
                "video_index": 0 if media_kind == "video" else -1,
                "audio_index": (
                    int(selected_audio.get("index") or 0) if selected_audio else -1
                ),
                "audio_language": _text(selected_audio.get("language")),
            },
            "endpoint_profile": {
                "schema": _text(capabilities.get("schema")),
                "revision": max(0, int(capabilities.get("revision") or 0)),
                "evidence": dict(capabilities.get("evidence") or {}),
            },
        }

    def _classify_source(
        self, name: str, media_kind: str, folder_path: str, metadata: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        parts = [part for part in folder_path.split("/") if part]
        match = _SEASON_EPISODE.search(name)
        episode_evidence = (
            _episode_filename_evidence(name) if media_kind == "video" else {}
        )
        membership: dict[str, Any] = {"ordinal": 0}
        collections: list[dict[str, Any]] = []
        canonical_title = _normalize_title(name)
        if media_kind == "video" and (match or episode_evidence):
            season = int(
                episode_evidence.get("season")
                or (match.group("season") if match else 0)
            )
            episode = int(
                episode_evidence.get("episode")
                or (match.group("episode") if match else 0)
            )
            fallback_title = (
                parts[-2]
                if len(parts) >= 2 and _SEASON_FOLDER.match(parts[-1])
                else (parts[-1] if parts else canonical_title)
            )
            series_title = _text(episode_evidence.get("title")) or fallback_title
            series_parts = (
                parts[:-1] if parts and _SEASON_FOLDER.match(parts[-1]) else parts
            )
            series_identity = (
                f"filename:{fold_text(series_title)}"
                if episode_evidence
                else ("/".join(series_parts) or series_title)
            )
            canonical_title = f"{series_title} S{season:02d}E{episode:02d}"
            collections = [
                {
                    "kind": "series",
                    "title": series_title,
                    "parent_id": "",
                    "ownership": "derived",
                    "identity_key": series_identity,
                    "metadata": {
                        "identity_basis": _text(episode_evidence.get("parser"))
                        or "folder",
                    },
                },
                {
                    "kind": "season",
                    "title": f"Season {season}",
                    "parent_index": 0,
                    "ownership": "derived",
                    "identity_key": f"{series_identity}/season:{season}",
                    "metadata": {"season_number": season},
                },
            ]
            membership.update({"ordinal": season * 10000 + episode, "season_number": season, "episode_number": episode})
        elif media_kind == "audio" and parts:
            disc_match = _DISC_FOLDER.match(parts[-1]) if parts else None
            part_match = _PART_FOLDER.match(parts[-1]) if parts else None
            numeric_part = _NUMERIC_FOLDER.match(parts[-1]) if parts else None
            audiobook_hint = any(_AUDIOBOOK_HINT.search(part) for part in parts)
            nested_part = bool(disc_match or part_match or (numeric_part and audiobook_hint))
            container_title = parts[-2] if len(parts) >= 2 and nested_part else parts[-1]
            album_title = _text(metadata.get("album")) or container_title
            number_match = _LEADING_NUMBER.match(Path(name).stem)
            ordinal = int(number_match.group("number")) if number_match else 0
            kind = (
                "audiobook"
                if not metadata.get("album")
                and (audiobook_hint or bool(part_match))
                else "album"
            )
            container_parts = parts[:-1] if nested_part else parts
            container_identity = "/".join(container_parts) or album_title
            collections = [
                {
                    "kind": kind,
                    "title": album_title,
                    "parent_id": "",
                    "ownership": "derived",
                    "identity_key": container_identity,
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
                        "identity_key": f"{container_identity}/disc:{disc}",
                        "metadata": {"disc_number": disc},
                    }
                )
                membership["disc_number"] = disc
            elif part_match or (numeric_part and audiobook_hint):
                part = int((part_match or numeric_part).group("part" if part_match else "number"))
                collections.append(
                    {
                        "kind": "book_part",
                        "title": f"Part {part}",
                        "parent_index": 0,
                        "ownership": "derived",
                        "identity_key": f"{container_identity}/part:{part}",
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
                    "identity_key": "/".join(parts),
                }
            ]
        identity_key = canonical_title
        if media_kind == "audio" and parts:
            identity_key = f"{'/'.join(parts)}\0{canonical_title}"
        work = {
            "media_kind": media_kind,
            "canonical_title": canonical_title,
            "identity_key": identity_key,
            "metadata": {"source_title": _title_from_name(name)},
        }
        return work, collections, membership

    def _upsert_classification(
        self,
        connection: sqlite3.Connection,
        work: Mapping[str, Any],
        collections: Iterable[Mapping[str, Any]],
    ) -> tuple[str, list[str]]:
        work_id = self._upsert_work(connection, work)
        collection_ids: list[str] = []
        for collection in collections:
            value = dict(collection)
            parent_index = value.pop("parent_index", None)
            if parent_index is not None:
                value["parent_id"] = collection_ids[int(parent_index)]
            collection_ids.append(self._upsert_collection(connection, value))
        return work_id, collection_ids

    def _upsert_work(self, connection: sqlite3.Connection, work: Mapping[str, Any]) -> str:
        title = _text(work.get("canonical_title"))
        kind = _text(work.get("media_kind")) or "other"
        identity_key = _text(work.get("identity_key")) or title
        work_id = _stable_id("work", kind, identity_key.casefold(), size=24)
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
        identity_key = _text(collection.get("identity_key")) or title
        collection_id = _stable_id(
            "collection", kind, identity_key.casefold(), parent_id, size=24
        )
        now = now_iso()
        connection.execute(
            """
            INSERT INTO media_collections(id, schema_name, kind, title, parent_id, ownership, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                metadata_json=excluded.metadata_json,updated_at=excluded.updated_at,
                revision=media_collections.revision+1
            """,
            (
                collection_id,
                COLLECTION_SCHEMA,
                kind,
                title,
                parent_id,
                _text(collection.get("ownership")) or "derived",
                _json_dumps(collection.get("metadata") or {}),
                now,
                now,
            ),
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

    def source_binding(
        self, *, agent_id: str = "", source_id: str = "", item_id: str = ""
    ) -> dict[str, Any] | None:
        filters: list[str] = []
        params: list[Any] = []
        if _text(item_id):
            filters.append("c.id=?")
            params.append(_text(item_id))
        if _text(agent_id):
            filters.append("c.agent_id=?")
            params.append(_text(agent_id))
        if _text(source_id):
            filters.append("c.source_id=?")
            params.append(_text(source_id))
        if not filters:
            return None
        with self.repository.connect() as connection:
            row = connection.execute(
                f"""
                SELECT c.id,c.agent_id,c.node_id,c.source_id,c.source_revision,
                    a.instance_id,a.availability,a.freshness
                FROM catalog_items c
                LEFT JOIN agent_catalog_state a ON a.agent_id=c.agent_id
                WHERE {' AND '.join(filters)} LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        return dict(row) if row else None

    def resolve_agent_hits(
        self,
        hits: Iterable[Mapping[str, Any]],
        *,
        agent_id: str,
        profile_id: str = "default",
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        profile = self.get_profile(profile_id)["profile"]
        policy = dict(profile.get("policy") or {})
        allowed = {
            _text(item).lower()
            for item in policy.get("allowed_media_kinds") or []
            if _text(item)
        }
        maximum = max(
            0, min(21, int(policy.get("maximum_maturity_rating") or 0))
        )
        result: list[dict[str, Any]] = []
        for hit in hits:
            if len(result) >= max(1, min(100, int(limit or 30))):
                break
            source_id = _text(hit.get("id") or hit.get("source_id"))
            if not source_id:
                continue
            with self.repository.connect() as connection:
                row = connection.execute(
                    """
                    SELECT c.*,
                        COALESCE(ps.favorite,c.favorite) AS profile_favorite,
                        COALESCE(ps.resume_ms,0) AS profile_resume_ms,
                        COALESCE(ps.duration_ms,0) AS profile_duration_ms,
                        COALESCE(ps.completed,0) AS profile_completed,
                        COALESCE(ps.rating,0) AS profile_rating,
                        COALESCE(ps.hidden,0) AS profile_hidden,
                        COALESCE(ps.last_played_at,'') AS profile_last_played_at,
                        COALESCE(ps.revision,0) AS profile_revision
                    FROM catalog_items c
                    LEFT JOIN personal_media_state ps
                        ON ps.item_id=c.id AND ps.profile_id=?
                    WHERE c.agent_id=? AND c.source_id=? AND c.missing=0
                    LIMIT 1
                    """,
                    (_text(profile_id) or "default", _text(agent_id), source_id),
                ).fetchone()
            if row is not None:
                if bool(row["profile_hidden"]) or self._policy_denial(row, policy):
                    continue
                item = self._public_coordinator_item(
                    row, _text(profile_id) or "default"
                )
                item["deep_match"] = dict(hit.get("match") or {})
                item["materialized"] = True
                result.append(item)
                continue
            metadata = dict(hit.get("metadata") or {})
            kind = _text(hit.get("media_kind")).lower()
            if kind not in allowed:
                continue
            try:
                maturity = max(0, int(metadata.get("maturity_rating") or 0))
            except (TypeError, ValueError):
                maturity = 0
            explicit = metadata.get("explicit", False)
            if isinstance(explicit, str):
                explicit = explicit.strip().lower() in {"1", "true", "yes", "on"}
            if maturity > maximum or (
                bool(explicit) and not bool(policy.get("allow_explicit", False))
            ):
                continue
            result.append(
                {
                    "schema": CATALOG_ITEM_SCHEMA,
                    "id": f"agent:{_text(agent_id)}:{source_id}",
                    "agent_id": _text(agent_id),
                    "node_id": _text(hit.get("node_id")),
                    "source_id": source_id,
                    "source_revision": int(hit.get("revision") or 0),
                    "title": _title_from_name(_text(hit.get("name"))),
                    "name": _text(hit.get("name")),
                    "media_kind": kind,
                    "mime_type": _text(hit.get("mime_type")),
                    "folder_path": _text(hit.get("folder_path")),
                    "available": bool(hit.get("present", True)),
                    "materialized": False,
                    "playable": False,
                    "deep_match": dict(hit.get("match") or {}),
                }
            )
        return result

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

    def retire_unbound_agent_states(
        self, active_agent_ids: Iterable[str]
    ) -> dict[str, Any]:
        """Retire local-compatibility rows after a complete distributed sync."""
        active = {_text(item) for item in active_agent_ids if _text(item)}
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT agent_id FROM agent_catalog_state WHERE instance_id='' ORDER BY agent_id"
            ).fetchall()
            retired = [
                str(row["agent_id"])
                for row in rows
                if str(row["agent_id"]) not in active
            ]
            retired_source_count = 0
            for agent_id in retired:
                retired_source_count += max(
                    0,
                    int(connection.execute(
                        "UPDATE catalog_items SET missing=1 WHERE agent_id=? AND missing=0",
                        (agent_id,),
                    ).rowcount or 0),
                )
                connection.execute(
                    "DELETE FROM catalog_folder_nodes WHERE agent_id=?", (agent_id,)
                )
                connection.execute(
                    "DELETE FROM agent_catalog_state WHERE agent_id=?", (agent_id,)
                )
            connection.commit()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "retired_agent_ids": retired,
            "retired_agent_count": len(retired),
            "retired_source_count": retired_source_count,
            "participation": self.participation(),
        }

    def profile_revision(self, profile_id: str) -> int:
        profile = _text(profile_id) or "default"
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT value FROM coordinator_meta WHERE key=?",
                (self._profile_revision_key(profile),),
            ).fetchone()
            if row is not None:
                return int(row["value"] or 0)
            personal = connection.execute(
                "SELECT COALESCE(MAX(revision),0) AS revision "
                "FROM personal_media_state WHERE profile_id=?",
                (profile,),
            ).fetchone()
            profile_row = connection.execute(
                "SELECT revision FROM media_profiles WHERE id=?", (profile,)
            ).fetchone()
        return max(
            int(personal["revision"] or 0),
            int(profile_row["revision"] or 0) if profile_row else 0,
        )

    @staticmethod
    def _profile_revision_key(profile_id: str) -> str:
        return f"profile_revision:{_text(profile_id) or 'default'}"

    def _next_profile_revision(
        self, connection: sqlite3.Connection, profile_id: str
    ) -> int:
        key = self._profile_revision_key(profile_id)
        row = connection.execute(
            "SELECT value FROM coordinator_meta WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            personal = connection.execute(
                "SELECT COALESCE(MAX(revision),0) FROM personal_media_state "
                "WHERE profile_id=?",
                (_text(profile_id) or "default",),
            ).fetchone()[0]
            profile = connection.execute(
                "SELECT revision FROM media_profiles WHERE id=?",
                (_text(profile_id) or "default",),
            ).fetchone()
            current = max(
                int(personal or 0),
                int(profile["revision"] or 0) if profile else 0,
            )
        else:
            current = int(row["value"] or 0)
        revision = current + 1
        connection.execute(
            "INSERT OR REPLACE INTO coordinator_meta(key,value) VALUES (?,?)",
            (key, str(revision)),
        )
        return revision

    def list_profiles(self, *, limit: int = 20) -> dict[str, Any]:
        bounded = max(1, min(50, int(limit or 20)))
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM media_profiles ORDER BY lower(label),id LIMIT ?",
                (bounded,),
            ).fetchall()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "items": [self._public_profile(row) for row in rows],
            "count": len(rows),
            "bounded": True,
        }

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        token = _text(profile_id) or "default"
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM media_profiles WHERE id=?", (token,)
            ).fetchone()
            if row is None:
                now = now_iso()
                label = " ".join(
                    part for part in re.split(r"[._\-]+", token) if part
                ).strip().title() or "Personal"
                connection.execute(
                    """
                    INSERT OR IGNORE INTO media_profiles(
                        id,label,kind,policy_json,created_at,updated_at
                    ) VALUES (?,?,'personal',?,?,?)
                    """,
                    (token, label[:100], _json_dumps(_default_profile_policy()), now, now),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO coordinator_meta(key,value) VALUES (?, '0')",
                    (self._profile_revision_key(token),),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM media_profiles WHERE id=?", (token,)
                ).fetchone()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "profile": self._public_profile(row),
        }

    @staticmethod
    def _policy_denial(
        row: sqlite3.Row, policy: Mapping[str, Any]
    ) -> str:
        media_kind = _text(row["media_kind"]).lower()
        allowed = {
            _text(item).lower()
            for item in policy.get("allowed_media_kinds") or []
            if _text(item)
        }
        if media_kind not in allowed:
            return "media_kind_not_allowed"
        metadata = _json_loads(row["metadata_json"]) or {}
        try:
            maturity = max(0, int(metadata.get("maturity_rating") or 0))
        except (TypeError, ValueError):
            maturity = 0
        maximum = max(
            0, min(21, int(policy.get("maximum_maturity_rating") or 0))
        )
        if maturity > maximum:
            return "maturity_rating_exceeded"
        explicit = metadata.get("explicit", False)
        if isinstance(explicit, str):
            explicit = explicit.strip().lower() in {"1", "true", "yes", "on"}
        if bool(explicit) and not bool(policy.get("allow_explicit", False)):
            return "explicit_content_not_allowed"
        return ""

    def set_profile_policy(
        self,
        profile_id: str,
        *,
        expected_revision: int,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        token = _text(profile_id) or "default"
        accepted = {
            "allowed_media_kinds",
            "maximum_maturity_rating",
            "allow_explicit",
            "show_history_on_shared_surface",
            "recommendations_enabled",
            "home_row_order",
            "default_view",
            "density",
            "default_target_id",
        }
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM media_profiles WHERE id=?", (token,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return {"ok": False, "error": "media_profile_not_found"}
            if int(row["revision"]) != int(expected_revision):
                connection.rollback()
                return {
                    "ok": False,
                    "error": "media_profile_revision_conflict",
                    "current_revision": int(row["revision"]),
                }
            current = _json_loads(row["policy_json"]) or {}
            policy = {
                **current,
                **{key: value for key, value in values.items() if key in accepted},
            }
            kinds = list(
                dict.fromkeys(
                    _text(item).lower()
                    for item in policy.get("allowed_media_kinds") or []
                    if _text(item).lower() in {"audio", "video"}
                )
            )
            if not kinds:
                connection.rollback()
                return {"ok": False, "error": "media_profile_policy_invalid"}
            policy["allowed_media_kinds"] = kinds
            policy["maximum_maturity_rating"] = max(
                0, min(21, int(policy.get("maximum_maturity_rating") or 0))
            )
            policy["allow_explicit"] = bool(policy.get("allow_explicit", False))
            policy["show_history_on_shared_surface"] = bool(
                policy.get("show_history_on_shared_surface", False)
            )
            policy["recommendations_enabled"] = bool(
                policy.get("recommendations_enabled", True)
            )
            requested_order = [
                _text(item)
                for item in policy.get("home_row_order") or []
                if _text(item) in HOME_SHELF_ORDER
            ]
            policy["home_row_order"] = list(
                dict.fromkeys(
                    requested_order
                    + [
                        item
                        for item in HOME_SHELF_ORDER
                        if item not in requested_order
                    ]
                )
            )
            default_view = _text(policy.get("default_view")).lower()
            policy["default_view"] = (
                default_view
                if default_view in {"list", "grid", "rail"}
                else "grid"
            )
            density = _text(policy.get("density")).lower()
            policy["density"] = (
                density
                if density in {"compact", "comfortable", "ten_foot"}
                else "compact"
            )
            policy["default_target_id"] = _text(
                policy.get("default_target_id")
            )[:200]
            connection.execute(
                """
                UPDATE media_profiles SET policy_json=?,revision=revision+1,
                    updated_at=? WHERE id=?
                """,
                (_json_dumps(policy), now_iso(), token),
            )
            self._next_profile_revision(connection, token)
            connection.commit()
        return self.get_profile(token)

    @staticmethod
    def _public_profile(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": PROFILE_SCHEMA,
            "id": str(row["id"]),
            "label": str(row["label"]),
            "kind": str(row["kind"]),
            "policy": _json_loads(row["policy_json"]) or {},
            "revision": int(row["revision"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def set_personal_state(
        self,
        item_id: str,
        *,
        profile_id: str,
        rating: int | None = None,
        hidden: bool | None = None,
    ) -> dict[str, Any]:
        token = _text(item_id)
        profile = _text(profile_id) or "default"
        self.get_profile(profile)
        with self.repository.connect() as connection:
            if not connection.execute(
                "SELECT id FROM catalog_items WHERE id=?", (token,)
            ).fetchone():
                return {"ok": False, "error": "item_not_found", "item_id": token}
            connection.execute(
                """
                INSERT OR IGNORE INTO personal_media_state(
                    profile_id,item_id,updated_at
                ) VALUES (?,?,?)
                """,
                (profile, token, now_iso()),
            )
            updates: dict[str, Any] = {}
            if rating is not None:
                updates["rating"] = max(0, min(5, int(rating)))
            if hidden is not None:
                updates["hidden"] = int(bool(hidden))
            if updates:
                updates["revision"] = connection.execute(
                    "SELECT revision+1 FROM personal_media_state WHERE profile_id=? AND item_id=?",
                    (profile, token),
                ).fetchone()[0]
                updates["updated_at"] = now_iso()
                assignments = ",".join(f"{name}=?" for name in updates)
                connection.execute(
                    f"UPDATE personal_media_state SET {assignments} WHERE profile_id=? AND item_id=?",
                    (*updates.values(), profile, token),
                )
                self._next_profile_revision(connection, profile)
            row = connection.execute(
                "SELECT * FROM personal_media_state WHERE profile_id=? AND item_id=?",
                (profile, token),
            ).fetchone()
            connection.commit()
        return {
            "ok": True,
            "schema": PERSONAL_SCHEMA,
            "state": dict(row)
            | {
                "favorite": bool(row["favorite"]),
                "completed": bool(row["completed"]),
                "hidden": bool(row["hidden"]),
            },
        }

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
        history_only: bool = False,
        continue_only: bool = False,
        sort: str = "recent",
        sort_direction: str = "",
        profile_id: str = "default",
        collection_id: str = "",
        genre: str = "",
        year: int | None = None,
        rating_min: float | None = None,
        content_rating: str = "",
    ) -> dict[str, Any]:
        page_size = max(1, min(MAX_PAGE_SIZE, int(limit or MAX_PAGE_SIZE)))
        profile = _text(profile_id) or "default"
        profile_result = self.get_profile(profile)
        if not profile_result.get("ok"):
            return profile_result
        profile_record = profile_result["profile"]
        policy = dict(profile_record.get("policy") or {})
        allowed_kinds = {
            _text(item).lower()
            for item in policy.get("allowed_media_kinds") or []
            if _text(item)
        }
        query_token = _text(query)
        sort_token = _text(sort).lower() or "recent"
        direction_token = _text(sort_direction).lower()
        direction = (
            "asc"
            if direction_token == "asc"
            or (not direction_token and sort_token in {"title", "year", "release_date", "content_rating"})
            else "desc"
        )
        signature = _cursor_signature(
            {
                "q": query_token.casefold(), "kind": media_kind, "source": source, "missing": bool(include_missing),
                "favorites": bool(favorites_only), "sort": sort_token, "profile": profile,
                "history": bool(history_only), "continue": bool(continue_only),
                "profile_revision": int(profile_record["revision"]), "collection": collection_id,
                "direction": direction, "genre": fold_text(genre), "year": year,
                "rating_min": rating_min, "content_rating": fold_text(content_rating),
            }
        )
        if _text(cursor):
            resolved_offset, cursor_anchor = _decode_keyset_cursor(cursor, signature)
        else:
            resolved_offset = max(0, int(offset or 0))
            cursor_anchor = None
        personal_driven = bool(
            not query_token and (favorites_only or history_only or continue_only)
        )
        filters: list[str] = []
        params: list[Any] = [profile]
        if personal_driven:
            filters.append("ps.profile_id=?")
        if not include_missing:
            filters.append("c.missing=0")
        filters.append("COALESCE(ps.hidden,0)=0")
        if favorites_only:
            filters.append(
                "ps.favorite=1"
                if personal_driven
                else "COALESCE(ps.favorite,c.favorite)=1"
            )
        if history_only or continue_only:
            filters.append("ps.last_played_at<>''")
        if continue_only:
            filters.extend(["ps.resume_ms>0", "ps.completed=0"])
        kind = _text(media_kind).lower()
        if kind == "playable":
            admitted = sorted(allowed_kinds & {"audio", "video"})
            if admitted:
                placeholders = ",".join("?" for _ in admitted)
                filters.append(f"c.media_kind IN ({placeholders})")
                params.extend(admitted)
            else:
                filters.append("1=0")
        elif kind in {"audio", "video", "image", "other"}:
            if kind not in allowed_kinds:
                filters.append("1=0")
            else:
                filters.append("c.media_kind=?")
                params.append(kind)
        maximum_rating = max(
            0, min(21, int(policy.get("maximum_maturity_rating") or 0))
        )
        filters.append("c.maturity_rating<=?")
        params.append(maximum_rating)
        if not bool(policy.get("allow_explicit", False)):
            filters.append("c.explicit=0")
        if _text(source) and _text(source) != "all":
            filters.append("c.source=?")
            params.append(_text(source))
        if _text(collection_id):
            filters.append(
                "EXISTS (SELECT 1 FROM collection_memberships cm "
                "WHERE cm.collection_id=? AND cm.work_id=c.work_id "
                "AND cm.variant_id=c.variant_id)"
            )
            params.append(_text(collection_id))
        if _text(genre):
            filters.append(
                "EXISTS (SELECT 1 FROM catalog_metadata_facets mf "
                "WHERE mf.item_id=c.id AND mf.field_name='genre' "
                "AND mf.normalized_value=?)"
            )
            params.append(fold_text(genre))
        if year not in (None, ""):
            try:
                resolved_year = int(year)
            except (TypeError, ValueError):
                resolved_year = 0
            if 1000 <= resolved_year <= 3000:
                filters.append(
                    "EXISTS (SELECT 1 FROM catalog_metadata_projection mp "
                    "WHERE mp.item_id=c.id AND mp.year=?)"
                )
                params.append(resolved_year)
        if rating_min not in (None, ""):
            try:
                minimum_rating = max(0.0, min(10.0, float(rating_min)))
            except (TypeError, ValueError):
                minimum_rating = 0.0
            filters.append(
                "EXISTS (SELECT 1 FROM catalog_metadata_projection mp "
                "WHERE mp.item_id=c.id AND mp.rating>=?)"
            )
            params.append(minimum_rating)
        if _text(content_rating):
            filters.append(
                "EXISTS (SELECT 1 FROM catalog_metadata_facets mf "
                "WHERE mf.item_id=c.id AND mf.field_name='content_rating' "
                "AND mf.normalized_value=?)"
            )
            params.append(fold_text(content_rating))
        fts_query = ""
        if query_token:
            terms = [term for term in re.findall(r"[\w]+", query_token, flags=re.UNICODE) if term][:12]
            if not terms:
                terms = [query_token]
            fts_query = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        from_sql = (
            "personal_media_state ps JOIN catalog_items c ON c.id=ps.item_id"
            if personal_driven
            else "catalog_items c LEFT JOIN personal_media_state ps ON ps.item_id=c.id AND ps.profile_id=?"
        )
        if query_token:
            order = "catalog_rank,c.rowid"
            order_params: list[Any] = []
        else:
            sort_contracts = {
                "title": (
                    "c.title COLLATE NOCASE, c.id",
                    (("c.title COLLATE NOCASE", "asc"), ("c.id", "asc")),
                ),
                "size": (
                    "c.size_bytes DESC, c.id",
                    (("c.size_bytes", "desc"), ("c.id", "asc")),
                ),
                "source": (
                    "c.source, lower(c.title), c.id",
                    (("c.source", "asc"), ("lower(c.title)", "asc"), ("c.id", "asc")),
                ),
                "favorite": (
                    "COALESCE(ps.favorite,c.favorite) DESC, c.title COLLATE NOCASE, c.id",
                    (
                        ("COALESCE(ps.favorite,c.favorite)", "desc"),
                        ("c.title COLLATE NOCASE", "asc"),
                        ("c.id", "asc"),
                    ),
                ),
                "recent": (
                    "ps.last_played_at DESC, c.id"
                    if personal_driven
                    else "COALESCE(ps.last_played_at,c.modified_at) DESC, c.id",
                    (
                        (("ps.last_played_at", "desc"), ("c.id", "asc"))
                        if personal_driven
                        else (("COALESCE(ps.last_played_at,c.modified_at)", "desc"), ("c.id", "asc"))
                    ),
                ),
            }
            order, sort_keys = sort_contracts.get(sort_token, sort_contracts["recent"])
            order_params = []
            metadata_sort_expressions = {
                "year": "COALESCE((SELECT mp.year FROM catalog_metadata_projection mp WHERE mp.item_id=c.id),0)",
                "release_date": "COALESCE((SELECT mp.release_date FROM catalog_metadata_projection mp WHERE mp.item_id=c.id),'')",
                "critic_rating": "COALESCE((SELECT mp.critic_rating FROM catalog_metadata_projection mp WHERE mp.item_id=c.id),-1)",
                "audience_rating": "COALESCE((SELECT mp.audience_rating FROM catalog_metadata_projection mp WHERE mp.item_id=c.id),-1)",
                "rating": "COALESCE((SELECT mp.rating FROM catalog_metadata_projection mp WHERE mp.item_id=c.id),-1)",
                "content_rating": "COALESCE((SELECT mp.content_rating FROM catalog_metadata_projection mp WHERE mp.item_id=c.id),'')",
                "duration": "COALESCE((SELECT mp.duration_ms FROM catalog_metadata_projection mp WHERE mp.item_id=c.id),0)",
                "progress": "CASE WHEN COALESCE(ps.duration_ms,0)>0 THEN CAST(ps.resume_ms AS REAL)/ps.duration_ms ELSE 0 END",
                "plays": "COALESCE(ps.play_count,c.play_count,0)",
                "date_added": "c.indexed_at",
                "date_viewed": "COALESCE(ps.last_played_at,'')",
                "resolution": "COALESCE(CAST(json_extract(c.quality_json,'$.height') AS INTEGER),0)",
                "bitrate": "COALESCE(CAST(json_extract(c.quality_json,'$.bitrate') AS INTEGER),0)",
            }
            if sort_token == "title":
                order = f"c.title COLLATE NOCASE {direction.upper()},c.id {direction.upper()}"
                sort_keys = (
                    ("c.title COLLATE NOCASE", direction), ("c.id", direction)
                )
            elif sort_token in metadata_sort_expressions:
                order = (
                    f"{metadata_sort_expressions[sort_token]} {direction.upper()},"
                    f"c.title COLLATE NOCASE ASC,c.id ASC"
                )
                sort_keys = ()
            elif sort_token == "random":
                order = "((c.rowid * 1103515245 + 12345) & 2147483647),c.id"
                sort_keys = ()
            if sort_token == "collection" and _text(collection_id):
                order_params.append(_text(collection_id))
                order = (
                    "COALESCE((SELECT printf('%010d:%010d:%010d',"
                    "COALESCE(cm_order.season_number,0),"
                    "COALESCE(cm_order.episode_number,0),cm_order.ordinal) "
                    "FROM collection_memberships cm_order "
                    "WHERE cm_order.collection_id=? "
                    "AND cm_order.work_id=c.work_id "
                    "AND cm_order.variant_id=c.variant_id LIMIT 1),"
                    "'9999999999:9999999999:9999999999'),"
                    "c.title COLLATE NOCASE,c.id"
                )
        if cursor_anchor is not None and not query_token and sort_keys:
            keyset_filter, keyset_params = _keyset_predicate(sort_keys, cursor_anchor)
            filters.append(keyset_filter)
            params.extend(keyset_params)
            where = f"WHERE {' AND '.join(filters)}"
        use_offset = not query_token and cursor_anchor is None and resolved_offset > 0
        query_suffix = "LIMIT ? OFFSET ?" if use_offset else "LIMIT ?"
        query_params = (
            (*params, *order_params, page_size + 1, resolved_offset)
            if use_offset
            else (*params, *order_params, page_size + 1)
        )
        try:
            search_candidate_limit = int(
                os.environ.get("MEDIA_CENTER_SEARCH_CANDIDATE_LIMIT") or 192
            )
        except ValueError:
            search_candidate_limit = 192
        search_candidate_limit = max(64, min(10_000, search_candidate_limit))
        search_candidate_count = 0
        with self.repository.connect() as connection:
            if query_token:
                rows = connection.execute(
                    f"""
                    WITH search_input(query_label) AS (VALUES (?)),
                    search_candidates AS MATERIALIZED (
                        SELECT catalog_search.rowid,catalog_search.item_id
                        FROM catalog_search
                        CROSS JOIN catalog_items c
                            ON c.rowid=catalog_search.rowid
                            AND c.id=catalog_search.item_id
                        LEFT JOIN personal_media_state ps
                            ON ps.item_id=c.id AND ps.profile_id=?
                        WHERE catalog_search.text MATCH ?
                            AND {' AND '.join(filters)}
                        LIMIT ?
                    ),
                    search_page AS MATERIALIZED (
                        SELECT c.id,
                            CASE
                                WHEN lower(c.title)=lower(search_input.query_label) THEN 0
                                WHEN instr(lower(c.title),lower(search_input.query_label))>0 THEN 1
                                WHEN instr(lower(c.name),lower(search_input.query_label))>0 THEN 2
                                ELSE 3
                            END AS catalog_rank,
                            c.rowid AS catalog_rowid,
                            (SELECT COUNT(*) FROM search_candidates)
                                AS search_candidate_count
                        FROM search_candidates candidate
                        CROSS JOIN catalog_items c
                            ON c.rowid=candidate.rowid AND c.id=candidate.item_id
                        CROSS JOIN search_input
                        ORDER BY {order} LIMIT ? OFFSET ?
                    )
                    SELECT c.*, COALESCE(ps.favorite,c.favorite) AS profile_favorite,
                        COALESCE(ps.resume_ms,0) AS profile_resume_ms,
                        COALESCE(ps.duration_ms,0) AS profile_duration_ms,
                        COALESCE(ps.completed,0) AS profile_completed,
                        COALESCE(ps.rating,0) AS profile_rating,
                        COALESCE(ps.hidden,0) AS profile_hidden,
                        COALESCE(ps.last_played_at,'') AS profile_last_played_at,
                        COALESCE(ps.revision,0) AS profile_revision,
                        search_page.catalog_rank,search_page.catalog_rowid,
                        search_page.search_candidate_count
                    FROM search_page JOIN catalog_items c ON c.id=search_page.id
                    LEFT JOIN personal_media_state ps
                        ON ps.item_id=c.id AND ps.profile_id=?
                    ORDER BY search_page.catalog_rank,search_page.catalog_rowid
                    """,
                    (
                        query_token,
                        profile,
                        fts_query,
                        *params[1:],
                        search_candidate_limit,
                        page_size + 1,
                        resolved_offset,
                        profile,
                    ),
                ).fetchall()
                if rows:
                    search_candidate_count = int(
                        rows[0]["search_candidate_count"] or 0
                    )
                else:
                    search_candidate_count = int(
                        connection.execute(
                            f"""
                            SELECT COUNT(*) FROM (
                                SELECT 1
                                FROM catalog_search
                                CROSS JOIN catalog_items c
                                    ON c.rowid=catalog_search.rowid
                                    AND c.id=catalog_search.item_id
                                LEFT JOIN personal_media_state ps
                                    ON ps.item_id=c.id AND ps.profile_id=?
                                WHERE catalog_search.text MATCH ?
                                    AND {' AND '.join(filters)}
                                LIMIT ?
                            )
                            """,
                            (
                                profile,
                                fts_query,
                                *params[1:],
                                search_candidate_limit,
                            ),
                        ).fetchone()[0]
                    )
            else:
                rows = connection.execute(
                    f"""
                    SELECT c.*, COALESCE(ps.favorite,c.favorite) AS profile_favorite,
                        COALESCE(ps.resume_ms,0) AS profile_resume_ms,
                        COALESCE(ps.duration_ms,0) AS profile_duration_ms,
                        COALESCE(ps.completed,0) AS profile_completed,
                        COALESCE(ps.rating,0) AS profile_rating,
                        COALESCE(ps.hidden,0) AS profile_hidden,
                        COALESCE(ps.last_played_at,'') AS profile_last_played_at,
                        COALESCE(ps.revision,0) AS profile_revision
                    FROM {from_sql} {where} ORDER BY {order} {query_suffix}
                    """,
                    query_params,
                ).fetchall()
            projections = self._metadata_projection_map(
                connection, (str(row["id"]) for row in rows)
            )
        has_more = len(rows) > page_size
        visible_rows = rows[:page_size]
        items = [
            self._public_coordinator_item(
                row, profile, projections.get(str(row["id"]))
            )
            for row in visible_rows
        ]
        next_offset = resolved_offset + len(items)
        total_count = next_offset + (1 if has_more else 0)
        next_anchor: list[Any] | None = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            if query_token:
                next_anchor = [last["catalog_rank"], last["catalog_rowid"]]
            elif sort_token == "title":
                next_anchor = [last["title"], last["id"]]
            elif sort_token == "size":
                next_anchor = [last["size_bytes"], last["id"]]
            elif sort_token == "source":
                next_anchor = [last["source"], str(last["title"]).lower(), last["id"]]
            elif sort_token == "favorite":
                next_anchor = [last["profile_favorite"], last["title"], last["id"]]
            elif sort_token == "collection" or not sort_keys:
                next_anchor = None
            else:
                next_anchor = [last["profile_last_played_at"] or last["modified_at"], last["id"]]
        next_cursor = None
        if has_more:
            next_cursor = (
                _encode_cursor(next_offset, signature)
                if not query_token and (sort_token == "collection" or not sort_keys)
                else _encode_cursor(next_offset, signature, next_anchor)
            )
        participation = self.participation()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "items": items,
            "count": len(items),
            "total_count": total_count,
            "total_count_exact": not has_more and not query_token,
            "total_count_lower_bound": total_count,
            "catalog_revision": self.catalog_revision(),
            "ranking": {
                "version": "deterministic-fts-v2",
                "query_mode": "explicit_submit",
                "candidate_window_bounded": bool(query_token),
                "candidate_limit": search_candidate_limit if query_token else None,
                "candidate_count": search_candidate_count if query_token else None,
                "candidate_window_full": bool(
                    query_token and search_candidate_count >= search_candidate_limit
                ),
            },
            "participation": participation,
            "partial": participation["partial"],
            "pagination": {
                "limit": page_size,
                "offset": resolved_offset,
                "cursor": cursor or _encode_cursor(resolved_offset, signature),
                "next_offset": next_offset if has_more else None,
                "next_cursor": next_cursor,
                "has_more": has_more,
            },
            "profile_policy": policy,
        }

    def discovery_search(
        self,
        query: str,
        *,
        profile_id: str = "default",
        media_kind: str = "playable",
        limit: int = 30,
    ) -> dict[str, Any]:
        token = _text(query)
        bounded = max(1, min(MAX_PAGE_SIZE, int(limit or MAX_PAGE_SIZE)))
        if not token:
            return {
                "ok": True,
                "schema": COORDINATOR_SCHEMA,
                "items": [],
                "count": 0,
                "bounded": True,
                "ranking": {"version": "local-discovery-v1"},
            }
        profile = _text(profile_id) or "default"
        profile_record = self.get_profile(profile)["profile"]
        policy = dict(profile_record.get("policy") or {})
        allowed = {
            _text(item).lower()
            for item in policy.get("allowed_media_kinds") or []
            if _text(item)
        }
        kind = _text(media_kind).lower()
        if kind == "playable":
            admitted = allowed & {"audio", "video"}
        elif kind in {"audio", "video", "image", "other"}:
            admitted = {kind} & allowed
        else:
            admitted = allowed
        try:
            candidate_limit = int(
                os.environ.get("MEDIA_CENTER_DISCOVERY_MAX_CANDIDATES") or 5000
            )
        except ValueError:
            candidate_limit = 5000
        candidate_limit = max(100, min(20_000, candidate_limit))
        try:
            score_limit = int(
                os.environ.get("MEDIA_CENTER_DISCOVERY_SCORE_CANDIDATES") or 600
            )
        except ValueError:
            score_limit = 600
        score_limit = max(100, min(candidate_limit, score_limit, 5000))
        query_trigrams = self._fuzzy_tokens(token).split()
        if not query_trigrams:
            return {
                "ok": True,
                "schema": COORDINATOR_SCHEMA,
                "query": token,
                "items": [],
                "count": 0,
                "bounded": True,
                "candidate_count": 0,
                "candidate_limit": candidate_limit,
                "truncated_candidates": False,
                "partial": self.participation()["partial"],
                "ranking": {"version": "local-discovery-v1"},
            }
        expression = " OR ".join(query_trigrams[:96])
        rows: list[sqlite3.Row] = []
        with self.repository.connect() as connection:
            candidate_rows = connection.execute(
                """
                SELECT item_id,rank FROM catalog_fuzzy_search
                WHERE catalog_fuzzy_search MATCH ?
                ORDER BY rank,item_id LIMIT ?
                """,
                (expression, score_limit + 1),
            ).fetchall()
            candidate_ids = [str(row["item_id"]) for row in candidate_rows]
            admitted_values = sorted(admitted)
            admitted_placeholders = (
                ",".join("?" for _ in admitted_values) or "''"
            )
            for start in range(0, min(score_limit, len(candidate_ids)), 400):
                batch = candidate_ids[start : start + 400]
                id_placeholders = ",".join("?" for _ in batch)
                rows.extend(
                    connection.execute(
                        f"""
                        SELECT c.*,
                            COALESCE(ps.favorite,c.favorite) AS profile_favorite,
                            COALESCE(ps.resume_ms,0) AS profile_resume_ms,
                            COALESCE(ps.duration_ms,0) AS profile_duration_ms,
                            COALESCE(ps.completed,0) AS profile_completed,
                            COALESCE(ps.rating,0) AS profile_rating,
                            COALESCE(ps.hidden,0) AS profile_hidden,
                            COALESCE(ps.last_played_at,'') AS profile_last_played_at,
                            COALESCE(ps.revision,0) AS profile_revision,
                            (
                                SELECT value_json FROM metadata_claims mc
                                WHERE mc.subject_ref='item:' || c.id
                                    AND mc.field_name IN ('semantic_embedding_v1','text_embedding_v1')
                                ORDER BY mc.confidence DESC,mc.created_at DESC LIMIT 1
                            ) AS discovery_embedding
                        FROM catalog_items c
                        LEFT JOIN personal_media_state ps
                            ON ps.item_id=c.id AND ps.profile_id=?
                        WHERE c.id IN ({id_placeholders}) AND c.missing=0
                            AND COALESCE(ps.hidden,0)=0
                            AND c.media_kind IN ({admitted_placeholders})
                        """,
                        (profile, *batch, *admitted_values),
                    ).fetchall()
                )
        scored: list[tuple[float, str, sqlite3.Row, list[str]]] = []
        for row in rows[:candidate_limit]:
            denial = self._policy_denial(row, policy)
            if denial:
                continue
            embedding = _json_loads(row["discovery_embedding"]) or []
            score, reasons = discovery_score(
                token,
                row["search_text"],
                candidate_embedding=(
                    embedding if isinstance(embedding, list) else []
                ),
            )
            if score < 0.18:
                continue
            scored.append((score, str(row["title"]).casefold(), row, reasons))
        scored.sort(key=lambda value: (-value[0], value[1], str(value[2]["id"])))
        items = []
        for score, _title, row, reasons in scored[:bounded]:
            item = self._public_coordinator_item(row, profile)
            item["deep_match"] = {
                "stage": "coordinator_local_discovery",
                "score": round(score, 6),
                "reasons": reasons,
            }
            items.append(item)
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "query": token,
            "items": items,
            "count": len(items),
            "bounded": True,
            "candidate_count": min(len(candidate_rows), score_limit),
            "candidate_limit": candidate_limit,
            "score_limit": score_limit,
            "truncated_candidates": len(candidate_rows) > score_limit,
            "partial": self.participation()["partial"],
            "ranking": {
                "version": "local-discovery-v1",
                "signals": [
                    "normalized_text",
                    "phonetic_code",
                    "trigram_similarity",
                    "local_text_embedding",
                ],
                "external_provider": False,
            },
        }

    @staticmethod
    def _public_coordinator_item(
        row: sqlite3.Row,
        profile_id: str,
        projection: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
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
                    "rating": int(row["profile_rating"]), "hidden": bool(row["profile_hidden"]),
                    "last_played_at": str(row["profile_last_played_at"]), "revision": int(row["profile_revision"]),
                },
            }
        )
        if projection is not None:
            metadata = _json_loads(projection["metadata_json"])
            provenance = _json_loads(projection["provenance_json"])
            public_metadata = _public_metadata(metadata)
            item["metadata"] = (
                public_metadata if isinstance(public_metadata, Mapping) else {}
            )
            item["metadata_provenance"] = (
                provenance if isinstance(provenance, Mapping) else {}
            )
            item["title"] = _text(projection["title"]) or item["title"]
            item["year"] = projection["year"]
            item["release_date"] = _text(projection["release_date"])
            item["rating"] = projection["rating"]
            item["critic_rating"] = projection["critic_rating"]
            item["audience_rating"] = projection["audience_rating"]
            item["content_rating"] = _text(projection["content_rating"])
            item["duration_ms"] = int(projection["duration_ms"] or 0)
            item["genres"] = _json_loads(projection["genres_json"]) or []
            item["artists"] = _json_loads(projection["artists_json"]) or []
            item["album"] = _text(projection["album"])
            item["series"] = _text(projection["series"])
            item["metadata_revision"] = int(projection["revision"] or 0)
            if isinstance(metadata, Mapping):
                projected_artwork = _public_artwork(metadata)
                if projected_artwork.get("state") == "ready":
                    item["artwork"] = projected_artwork
        return item

    def set_favorite(self, item_id: str, *, profile_id: str, favorite: bool) -> dict[str, Any]:
        token = _text(item_id)
        profile = _text(profile_id) or "default"
        self.get_profile(profile)
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
            self._next_profile_revision(connection, profile)
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
        self.get_profile(profile)
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
            self._next_profile_revision(connection, profile)
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
            where = "WHERE kind=?"
            params.append(kind_token)
        with self.repository.connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM media_collections {where}",
                    tuple(params),
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                WITH selected AS (
                    SELECT * FROM media_collections
                    {where}
                    ORDER BY title COLLATE NOCASE,id
                    LIMIT ? OFFSET ?
                )
                SELECT c.*,
                    (SELECT COUNT(DISTINCT m.work_id)
                     FROM collection_memberships m
                     WHERE m.collection_id=c.id
                         AND EXISTS (
                             SELECT 1 FROM catalog_items available
                                 INDEXED BY idx_media_center_catalog_work_variant
                             WHERE available.work_id=m.work_id
                                 AND available.variant_id=m.variant_id
                                 AND available.missing=0
                         )) AS item_count,
                    (
                        SELECT ci.metadata_json
                        FROM collection_memberships preview_membership
                            INDEXED BY idx_media_center_membership_preview
                        CROSS JOIN catalog_items ci
                            INDEXED BY idx_media_center_catalog_work_variant
                            ON ci.work_id=preview_membership.work_id
                            AND ci.variant_id=preview_membership.variant_id
                        WHERE preview_membership.collection_id=c.id
                            AND ci.missing=0
                        ORDER BY CASE
                                WHEN json_extract(ci.metadata_json, '$.artwork.state')='ready'
                                THEN 0 ELSE 1 END,
                            preview_membership.season_number,
                            preview_membership.episode_number,
                            preview_membership.ordinal,ci.id LIMIT 1
                    ) AS representative_metadata_json
                FROM selected c ORDER BY c.title COLLATE NOCASE,c.id
                """,
                (*params, bounded, offset),
            ).fetchall()
        items = [self._public_collection(row) for row in rows]
        next_offset = offset + len(items)
        return {
            "ok": True, "schema": COORDINATOR_SCHEMA, "items": items, "total_count": total,
            "pagination": {"limit": bounded, "cursor": _encode_cursor(offset, signature), "next_cursor": _encode_cursor(next_offset, signature) if next_offset < total else None, "has_more": next_offset < total},
        }

    @staticmethod
    def _public_collection(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        metadata = _json_loads(value.pop("representative_metadata_json", ""))
        return value | {
            "schema": COLLECTION_SCHEMA,
            "item_count": int(row["item_count"]),
            "artwork": _public_artwork(
                metadata if isinstance(metadata, Mapping) else {}
            ),
        }

    def collection_contents(
        self,
        collection_id: str,
        *,
        profile_id: str = "default",
        limit: int = 30,
        cursor: str = "",
    ) -> dict[str, Any]:
        token = _text(collection_id)
        if not token:
            return {"ok": False, "error": "collection_id_required"}
        with self.repository.connect() as connection:
            collection = connection.execute(
                """
                SELECT c.*,
                    (SELECT COUNT(DISTINCT m.work_id)
                     FROM collection_memberships m
                     WHERE m.collection_id=c.id
                         AND EXISTS (
                             SELECT 1 FROM catalog_items available
                                 INDEXED BY idx_media_center_catalog_work_variant
                             WHERE available.work_id=m.work_id
                                 AND available.variant_id=m.variant_id
                                 AND available.missing=0
                         )) AS item_count
                FROM media_collections c WHERE c.id=?
                """,
                (token,),
            ).fetchone()
            if collection is None:
                return {
                    "ok": False,
                    "error": "collection_not_found",
                    "collection_id": token,
                }
            child_rows = connection.execute(
                """
                SELECT c.*,
                    (SELECT COUNT(DISTINCT child_membership.work_id)
                     FROM collection_memberships child_membership
                     WHERE child_membership.collection_id=c.id
                         AND EXISTS (
                             SELECT 1 FROM catalog_items available
                                 INDEXED BY idx_media_center_catalog_work_variant
                             WHERE available.work_id=child_membership.work_id
                                 AND available.variant_id=child_membership.variant_id
                                 AND available.missing=0
                         )) AS item_count,
                    (
                        SELECT ci.metadata_json
                        FROM collection_memberships preview_membership
                            INDEXED BY idx_media_center_membership_preview
                        CROSS JOIN catalog_items ci
                            INDEXED BY idx_media_center_catalog_work_variant
                            ON ci.work_id=preview_membership.work_id
                            AND ci.variant_id=preview_membership.variant_id
                        WHERE preview_membership.collection_id=c.id
                            AND ci.missing=0
                        ORDER BY CASE
                                WHEN json_extract(ci.metadata_json, '$.artwork.state')='ready'
                                THEN 0 ELSE 1 END,
                            preview_membership.season_number,
                            preview_membership.episode_number,
                            preview_membership.ordinal,ci.id LIMIT 1
                    ) AS representative_metadata_json
                FROM media_collections c
                WHERE c.parent_id=?
                ORDER BY lower(c.title),c.id LIMIT 30
                """,
                (token,),
            ).fetchall()
            breadcrumbs: list[dict[str, Any]] = []
            current = collection
            for _depth in range(8):
                breadcrumbs.append(
                    {
                        "id": str(current["id"]),
                        "title": str(current["title"]),
                        "kind": str(current["kind"]),
                    }
                )
                parent_id = str(current["parent_id"] or "")
                if not parent_id:
                    break
                parent = connection.execute(
                    "SELECT * FROM media_collections WHERE id=?", (parent_id,)
                ).fetchone()
                if parent is None:
                    break
                current = parent
        page = self.list_items(
            media_kind="playable",
            profile_id=profile_id,
            collection_id=token,
            limit=limit,
            cursor=cursor,
            sort="collection",
        )
        children = [self._public_collection(row) for row in child_rows]
        collection_value = dict(collection) | {
            "schema": COLLECTION_SCHEMA,
            "item_count": int(collection["item_count"]),
            "artwork": (
                children[0]["artwork"]
                if children
                else (page["items"][0]["artwork"] if page["items"] else _public_artwork({}))
            ),
        }
        return {
            **page,
            "schema": COORDINATOR_SCHEMA,
            "collection": collection_value,
            "breadcrumbs": list(reversed(breadcrumbs)),
            "children": children,
            "child_count": len(children),
        }

    def folders(
        self,
        *,
        agent_id: str = "",
        root_id: str = "",
        parent: str = "",
        profile_id: str = "default",
        limit: int = 30,
        cursor: str = "",
    ) -> dict[str, Any]:
        bounded = max(1, min(MAX_PAGE_SIZE, int(limit or MAX_PAGE_SIZE)))
        agent = _text(agent_id)
        root = _text(root_id)
        parent_path = _text(parent).replace("\\", "/").strip("/")
        profile = _text(profile_id) or "default"
        profile_record = self.get_profile(profile)["profile"]
        policy = dict(profile_record.get("policy") or {})
        signature = _cursor_signature(
            {
                "agent": agent,
                "root": root,
                "parent": parent_path,
                "profile": profile,
                "profile_revision": int(profile_record["revision"]),
            }
        )
        offset = _decode_cursor(cursor, signature) if _text(cursor) else 0
        filters = ["c.missing=0", "COALESCE(ps.hidden,0)=0"]
        visibility_params: list[Any] = [profile]
        allowed_kinds = sorted(
            {
                _text(item).lower()
                for item in policy.get("allowed_media_kinds") or []
                if _text(item).lower() in {"audio", "video"}
            }
        )
        if allowed_kinds:
            placeholders = ",".join("?" for _ in allowed_kinds)
            filters.append(f"c.media_kind IN ({placeholders})")
            visibility_params.extend(allowed_kinds)
        else:
            filters.append("1=0")
        filters.append("c.maturity_rating<=?")
        visibility_params.append(
            max(0, min(21, int(policy.get("maximum_maturity_rating") or 0)))
        )
        if not bool(policy.get("allow_explicit", False)):
            filters.append("c.explicit=0")
        where = " AND ".join(filters)
        if not root and not parent_path:
            root_filters = ["c.agent_id<>''", "c.root_id<>''", where]
            root_params = list(visibility_params)
            if agent:
                root_filters.append("c.agent_id=?")
                root_params.append(agent)
            root_where = " AND ".join(root_filters)
            with self.repository.connect() as connection:
                root_total = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(*) FROM (
                            SELECT c.agent_id,c.root_id
                            FROM catalog_items c
                            LEFT JOIN personal_media_state ps
                                ON ps.item_id=c.id AND ps.profile_id=?
                            WHERE {root_where}
                            GROUP BY c.agent_id,c.root_id
                        )
                        """,
                        tuple(root_params),
                    ).fetchone()[0]
                )
                root_rows = connection.execute(
                    f"""
                    SELECT c.agent_id,MAX(c.node_id) AS node_id,c.root_id,
                        COUNT(*) AS source_count,
                        MAX(c.catalog_revision) AS revision,
                        MAX(COALESCE(json_extract(
                            c.metadata_json,'$.media_library_root_path'
                        ),'')) AS root_path,
                        MAX(COALESCE(json_extract(
                            c.metadata_json,'$.media_library_root_label'
                        ),'')) AS root_label
                    FROM catalog_items c
                    LEFT JOIN personal_media_state ps
                        ON ps.item_id=c.id AND ps.profile_id=?
                    WHERE {root_where}
                    GROUP BY c.agent_id,c.root_id
                    ORDER BY root_label COLLATE NOCASE,root_path COLLATE NOCASE,
                        c.agent_id,c.root_id
                    LIMIT ? OFFSET ?
                    """,
                    (*root_params, bounded, offset),
                ).fetchall()
            items = []
            for row in root_rows:
                root_path = _text(row["root_path"]).replace("\\", "/").rstrip("/")
                name = (
                    _text(row["root_label"])
                    or (root_path.rsplit("/", 1)[-1] if root_path else "")
                    or str(row["root_id"])
                )
                items.append(
                    {
                        "schema": FOLDER_NODE_SCHEMA,
                        "id": _stable_id(
                            "folder-root",
                            str(row["agent_id"]),
                            str(row["root_id"]),
                            size=24,
                        ),
                        "agent_id": str(row["agent_id"]),
                        "node_id": str(row["node_id"]),
                        "root_id": str(row["root_id"]),
                        "path": "/",
                        "queue_ref": (
                            f"{row['agent_id']}:{row['root_id']}:"
                        ),
                        "parent": "",
                        "name": name,
                        "entry_type": "folder",
                        "navigable": True,
                        "icon": "folder-outline",
                        "source_count": int(row["source_count"]),
                        "revision": int(row["revision"]),
                    }
                )
            next_offset = offset + len(items)
            participation = self.participation()
            return {
                "ok": True,
                "schema": COORDINATOR_SCHEMA,
                "items": items,
                "folders": items,
                "files": [],
                "count": len(items),
                "folder_count": len(items),
                "file_count": 0,
                "total_count": root_total,
                "parent": "",
                "breadcrumbs": [
                    {
                        "name": "Folders",
                        "name_i18n": {
                            "key": "runtime.media_center.ui.folders"
                        },
                        "agent_id": "",
                        "root_id": "",
                        "path": "",
                        "root": True,
                    }
                ],
                "can_go_up": False,
                "partial": participation["partial"],
                "participation": participation,
                "pagination": {
                    "limit": bounded,
                    "cursor": _encode_cursor(offset, signature),
                    "next_cursor": (
                        _encode_cursor(next_offset, signature)
                        if next_offset < root_total
                        else None
                    ),
                    "has_more": next_offset < root_total,
                },
            }
        if not agent or not root:
            return {
                "ok": False,
                "schema": COORDINATOR_SCHEMA,
                "error": "folder_root_scope_required",
                "items": [],
                "folders": [],
                "files": [],
                "count": 0,
                "total_count": 0,
            }
        folder_filters = ["f.parent=?"]
        folder_params: list[Any] = [parent_path]
        if agent:
            folder_filters.append("f.agent_id=?")
            folder_params.append(agent)
        if root:
            folder_filters.append("f.root_id=?")
            folder_params.append(root)
        folder_sql = f"""
            SELECT f.* FROM catalog_folder_nodes f
            WHERE {' AND '.join(folder_filters)}
                AND EXISTS (
                    SELECT 1 FROM catalog_items c
                    LEFT JOIN personal_media_state ps
                        ON ps.item_id=c.id AND ps.profile_id=?
                    WHERE c.agent_id=f.agent_id AND c.root_id=f.root_id
                        AND (c.folder_path=f.path OR
                            substr(c.folder_path,1,length(f.path)+1)=f.path || '/')
                        AND {where}
                )
        """
        folder_query_params = [*folder_params, *visibility_params]
        direct_filters = [*filters, "c.folder_path=?"]
        direct_params = [*visibility_params, parent_path]
        if agent:
            direct_filters.append("c.agent_id=?")
            direct_params.append(agent)
        if root:
            direct_filters.append("c.root_id=?")
            direct_params.append(root)
        direct_where = " AND ".join(direct_filters)
        with self.repository.connect() as connection:
            folder_total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM ({folder_sql})",
                    tuple(folder_query_params),
                ).fetchone()[0]
            )
            file_total = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM catalog_items c
                    LEFT JOIN personal_media_state ps
                        ON ps.item_id=c.id AND ps.profile_id=?
                    WHERE {direct_where}
                    """,
                    tuple(direct_params),
                ).fetchone()[0]
            )
            folder_rows: list[sqlite3.Row] = []
            if offset < folder_total:
                folder_rows = connection.execute(
                    f"{folder_sql} "
                    "ORDER BY f.name COLLATE NOCASE,f.agent_id,f.root_id,f.path "
                    "LIMIT ? OFFSET ?",
                    (*folder_query_params, bounded, offset),
                ).fetchall()
            remaining = bounded - len(folder_rows)
            file_rows: list[sqlite3.Row] = []
            if remaining > 0:
                file_offset = max(0, offset - folder_total)
                file_rows = connection.execute(
                    f"""
                    SELECT c.*, COALESCE(ps.favorite,c.favorite) AS profile_favorite,
                        COALESCE(ps.resume_ms,0) AS profile_resume_ms,
                        COALESCE(ps.duration_ms,0) AS profile_duration_ms,
                        COALESCE(ps.completed,0) AS profile_completed,
                        COALESCE(ps.rating,0) AS profile_rating,
                        COALESCE(ps.hidden,0) AS profile_hidden,
                        COALESCE(ps.last_played_at,'') AS profile_last_played_at,
                        COALESCE(ps.revision,0) AS profile_revision
                    FROM catalog_items c
                    LEFT JOIN personal_media_state ps
                        ON ps.item_id=c.id AND ps.profile_id=?
                    WHERE {direct_where}
                    ORDER BY c.title COLLATE NOCASE,c.id LIMIT ? OFFSET ?
                    """,
                    (*direct_params, remaining, file_offset),
                ).fetchall()
        total = folder_total + file_total
        items = []
        for row in folder_rows:
            name = str(row["name"])
            path = str(row["path"])
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
                    "queue_ref": (
                        f"{row['agent_id']}:{row['root_id']}:{path}"
                    ),
                    "parent": parent_path,
                    "name": name,
                    "entry_type": "folder",
                    "navigable": True,
                    "icon": "folder-outline",
                    "source_count": int(row["source_count"]),
                    "revision": int(row["revision"]),
                }
            )
        for row in file_rows:
            item = self._public_coordinator_item(row, profile)
            item.update(
                {
                    "entry_type": "media",
                    "path": "/".join(
                        part for part in (parent_path, str(row["name"])) if part
                    ),
                    "parent": parent_path,
                    "icon": (
                        "musical-notes-outline"
                        if str(row["media_kind"]) == "audio"
                        else "videocam-outline"
                    ),
                }
            )
            items.append(item)
        next_offset = offset + len(items)
        with self.repository.connect() as connection:
            root_row = connection.execute(
                """
                SELECT MAX(COALESCE(json_extract(
                    metadata_json,'$.media_library_root_path'
                ),'')) AS root_path,
                    MAX(COALESCE(json_extract(
                        metadata_json,'$.media_library_root_label'
                    ),'')) AS root_label
                FROM catalog_items
                WHERE agent_id=? AND root_id=? AND missing=0
                """,
                (agent, root),
            ).fetchone()
        root_path = _text(root_row["root_path"] if root_row else "").replace(
            "\\", "/"
        ).rstrip("/")
        root_name = (
            _text(root_row["root_label"] if root_row else "")
            or (root_path.rsplit("/", 1)[-1] if root_path else "")
            or root
        )
        breadcrumbs = [
            {
                "name": "Folders",
                "name_i18n": {"key": "runtime.media_center.ui.folders"},
                "agent_id": "",
                "root_id": "",
                "path": "",
                "root": True,
            },
            {
                "name": root_name,
                "agent_id": agent,
                "root_id": root,
                "path": "/",
                "root": False,
            },
        ] + [
            {
                "name": segment,
                "agent_id": agent,
                "root_id": root,
                "path": "/".join(parent_path.split("/")[: index + 1]),
                "root": False,
            }
            for index, segment in enumerate(parent_path.split("/"))
            if segment
        ]
        participation = self.participation()
        folder_items = [item for item in items if item.get("entry_type") == "folder"]
        file_items = [item for item in items if item.get("entry_type") == "media"]
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "items": items,
            "folders": folder_items,
            "files": file_items,
            "count": len(items),
            "folder_count": len(folder_items),
            "file_count": len(file_items),
            "total_count": total,
            "parent": parent_path,
            "breadcrumbs": breadcrumbs,
            "can_go_up": bool(parent_path),
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
                    COALESCE(ps.rating,0) AS profile_rating,
                    COALESCE(ps.hidden,0) AS profile_hidden,
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

    def playback_plan(
        self,
        item_id: str,
        *,
        endpoint_id: str = "",
        endpoint_node_id: str = "",
        endpoint_capabilities: Mapping[str, Any] | None = None,
        preferred_quality: str = "auto",
        preferred_language: str = "",
        variant_id: str = "",
        profile_id: str = "default",
    ) -> dict[str, Any]:
        token = _text(item_id)
        profile = _text(profile_id) or "default"
        profile_result = self.get_profile(profile)
        policy = dict(profile_result["profile"].get("policy") or {})
        capabilities = dict(endpoint_capabilities or {})
        with self.repository.connect() as connection:
            selected_item = connection.execute(
                "SELECT * FROM catalog_items WHERE id=?", (token,)
            ).fetchone()
            if selected_item is None:
                return {"ok": False, "error": "item_not_found", "item_id": token}
            denial = self._policy_denial(selected_item, policy)
            if denial:
                return {
                    "ok": False,
                    "error": "playback_policy_denied",
                    "reason": denial,
                    "item_id": token,
                    "profile_id": profile,
                    "profile_revision": int(profile_result["profile"]["revision"]),
                }
            rows = connection.execute(
                """
                SELECT v.id AS selected_variant_id,
                    v.source_id AS selected_source_id,
                    v.node_id AS selected_node_id,
                    v.media_kind AS selected_media_kind,
                    v.mime_type AS selected_mime_type,
                    v.quality_json AS variant_quality_json,
                    v.available AS variant_available,
                    v.descriptor_json AS variant_descriptor_json,
                    v.resource_id AS variant_resource_id,
                    v.source_revision AS variant_source_revision,
                    v.exact_source_id,v.exact_source_revision,v.derived,
                    c.id AS catalog_item_id,c.missing,c.content_path,
                    c.routed_content_path,c.descriptor_json AS catalog_descriptor_json
                FROM media_variants v
                LEFT JOIN catalog_items c ON c.variant_id=v.id
                WHERE v.work_id=?
                ORDER BY v.derived,v.id
                """,
                (str(selected_item["work_id"]),),
            ).fetchall()
        codec_support = {
            _text(item).lower()
            for item in capabilities.get("codecs") or []
            if _text(item)
        }
        maximum_height = max(0, int(capabilities.get("max_video_height") or 0))
        maximum_bitrate = max(0, int(capabilities.get("max_bitrate") or 0))
        quality_preference = _text(preferred_quality).lower() or "auto"
        language_preference = _text(preferred_language).lower()
        override = _text(variant_id)
        selected_item_variant = str(selected_item["variant_id"])
        ranked: list[
            tuple[
                float,
                sqlite3.Row,
                dict[str, Any],
                list[str],
                dict[str, Any],
            ]
        ] = []
        for row in rows:
            quality = _json_loads(row["variant_quality_json"]) or {}
            reasons: list[str] = []
            available = bool(row["variant_available"]) and not bool(
                row["missing"] if row["missing"] is not None else False
            )
            score = 1000.0 if available else -10000.0
            if str(row["selected_variant_id"]) == selected_item_variant:
                score += 1.0
                reasons.append("selected_item_source")
            if override:
                score += (
                    100000.0
                    if str(row["selected_variant_id"]) == override
                    else -100000.0
                )
                reasons.append("user_variant_override")
            codec = _text(quality.get("codec")).lower()
            if codec_support and codec:
                if codec in codec_support:
                    score += 100.0
                    reasons.append("codec_supported")
                else:
                    score -= 1000.0
                    reasons.append("codec_not_advertised")
            height = max(0, int(quality.get("height") or 0))
            bitrate = max(0, int(quality.get("bitrate") or 0))
            if maximum_height and height > maximum_height:
                score -= 500.0 + (height - maximum_height) / 10
                reasons.append("height_above_endpoint_limit")
            else:
                score += min(height, maximum_height or height) / 100
            if maximum_bitrate and bitrate > maximum_bitrate:
                score -= 500.0 + (bitrate - maximum_bitrate) / 100000
                reasons.append("bitrate_above_network_limit")
            target_height = {
                "4k": 2160,
                "2160p": 2160,
                "fhd": 1080,
                "1080p": 1080,
                "hd": 720,
                "720p": 720,
                "sd": 480,
            }.get(quality_preference)
            if target_height and height:
                score += max(0.0, 80.0 - abs(target_height - height) / 10)
                reasons.append("quality_preference")
            language = _text(quality.get("language")).lower()
            if language_preference and language == language_preference:
                score += 50.0
                reasons.append("language_preference")
            if endpoint_node_id and str(row["selected_node_id"]) == _text(endpoint_node_id):
                score += 25.0
                reasons.append("source_colocated_with_endpoint")
            compatibility = self._endpoint_compatibility(
                quality,
                media_kind=str(row["selected_media_kind"]),
                mime_type=str(row["selected_mime_type"]),
                capabilities=capabilities,
                preferred_language=language_preference,
            )
            compatibility_score = {
                "direct": 300.0,
                "remux": 120.0,
                "prepared_hls": 80.0,
                "transcode": -300.0,
                "unsupported": -5000.0,
            }.get(_text(compatibility.get("mode")), -5000.0)
            score += compatibility_score
            reasons.append(f"compatibility_{compatibility['mode']}")
            ranked.append((score, row, quality, reasons, compatibility))
        ranked.sort(
            key=lambda item: (-item[0], str(item[1]["selected_variant_id"]))
        )
        if not ranked or ranked[0][0] < -9000:
            return {
                "ok": False,
                "error": "playback_source_unavailable",
                "item_id": token,
            }
        score, selected, quality, reasons, compatibility = ranked[0]
        descriptor = _json_loads(selected["variant_descriptor_json"]) or {}
        if not descriptor:
            descriptor = _json_loads(selected["catalog_descriptor_json"]) or {}
        direct_candidates = [
            _public_direct_url(item)
            for item in (
                descriptor.get("direct_urls")
                or descriptor.get("content_url_candidates")
                or []
            )
            if _public_direct_url(item)
        ][:8]
        routed_path = _public_content_path(
            descriptor.get("routed_content_path")
            or descriptor.get("browser_path")
            or selected["routed_content_path"]
        )
        node_path = _public_content_path(
            descriptor.get("content_path") or selected["content_path"]
        )
        route = {
            "schema": "adaos.media_center.playback_route.v1",
            "mode": (
                "direct_agent_to_endpoint"
                if direct_candidates
                else "root_routed_http_relay"
            ),
            "source_node_id": str(selected["selected_node_id"]),
            "endpoint_id": _text(endpoint_id),
            "endpoint_node_id": _text(endpoint_node_id),
            "direct_candidates": direct_candidates,
            "routed_path": routed_path,
            "node_path": node_path,
            "resource_id": str(selected["variant_resource_id"]),
            "fallback": {
                "mode": "root_routed_http_relay",
                "path": routed_path or node_path,
                "target_node_id": str(selected["selected_node_id"]),
                "reason": (
                    "direct_candidate_failed"
                    if direct_candidates
                    else "no_direct_candidate"
                ),
            },
        }
        return {
            "ok": True,
            "schema": PLAYBACK_PLAN_SCHEMA,
            "item_id": token,
            "work_id": str(selected_item["work_id"]),
            "variant_id": str(selected["selected_variant_id"]),
            "source_id": str(selected["selected_source_id"]),
            "media_kind": str(selected["selected_media_kind"]),
            "mime_type": str(selected["selected_mime_type"]),
            "title": str(selected_item["title"]),
            "profile_id": profile,
            "quality": quality,
            "descriptor": _public_resource_descriptor(
                descriptor,
                resource_id=str(selected["variant_resource_id"]),
                mime_type=str(selected["selected_mime_type"]),
                content_path=node_path,
                routed_content_path=routed_path,
            ),
            "route": route,
            "compatibility": compatibility,
            "decision": {
                "policy": "deterministic_variant_route_v2",
                "score": round(score, 3),
                "reasons": reasons,
                "candidate_count": len(ranked),
                "requested_variant_id": override,
                "preferred_quality": quality_preference,
                "preferred_language": language_preference,
                "derived": bool(selected["derived"]),
                "exact_source_id": str(selected["exact_source_id"]),
                "exact_source_revision": int(
                    selected["exact_source_revision"] or 0
                ),
            },
        }

    def build_queue(
        self,
        *,
        source_type: str,
        source_id: str,
        profile_id: str = "default",
        limit: int = 500,
        endpoint_id: str = "",
        endpoint_node_id: str = "",
        endpoint_capabilities: Mapping[str, Any] | None = None,
        preferred_quality: str = "auto",
        preferred_language: str = "",
    ) -> dict[str, Any]:
        kind = _text(source_type).lower()
        token = _text(source_id)
        bounded = max(1, min(500, int(limit or 500)))
        profile = _text(profile_id) or "default"
        if kind not in {"item", "work", "collection", "folder", "playlist"}:
            return {"ok": False, "error": "playback_queue_source_invalid"}
        if kind == "playlist":
            access = self.get_playlist(token, profile_id=profile)
            if not access.get("ok"):
                return access
            with self.repository.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT c.id
                    FROM user_playlist_items pi
                    JOIN catalog_items c ON c.id=pi.item_id
                    WHERE pi.playlist_id=? AND c.missing=0
                    ORDER BY pi.ordinal LIMIT ?
                    """,
                    (token, bounded),
                ).fetchall()
            item_ids = [str(row["id"]) for row in rows]
            ownership = "user_playlist"
        else:
            filters = ["missing=0", "media_kind IN ('audio','video')"]
            params: list[Any] = []
            order = "lower(title), id"
            if kind == "item":
                filters.append("id=?")
                params.append(token)
            elif kind == "work":
                filters.append("work_id=?")
                params.append(token)
            elif kind == "collection":
                filters.append("m.collection_id=?")
                params.append(token)
                order = "MIN(m.ordinal), catalog_items.work_id"
            else:
                parts = token.split(":", 2)
                if len(parts) == 3:
                    agent_id, root_id, path = parts
                elif len(parts) == 2:
                    agent_id, path = parts
                    root_id = ""
                else:
                    return {"ok": False, "error": "playback_folder_ref_invalid"}
                prefix = f"{path.rstrip('/')}/" if path else ""
                filters.extend(
                    [
                        "agent_id=?",
                        "(folder_path=? OR substr(folder_path,1,?)=?)",
                    ]
                )
                params.extend([agent_id, path, len(prefix), prefix])
                if root_id:
                    filters.append("root_id=?")
                    params.append(root_id)
                order = "folder_path, lower(title), id"
            with self.repository.connect() as connection:
                if kind == "collection":
                    rows = connection.execute(
                        f"""
                        SELECT MIN(catalog_items.id) AS id
                        FROM catalog_items
                        JOIN collection_memberships m
                            ON m.work_id=catalog_items.work_id
                        WHERE {' AND '.join(filters)}
                        GROUP BY catalog_items.work_id
                        ORDER BY {order} LIMIT ?
                        """,
                        (*params, bounded),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        f"""
                        SELECT id FROM catalog_items
                        WHERE {' AND '.join(filters)}
                        ORDER BY {order} LIMIT ?
                        """,
                        (*params, bounded),
                    ).fetchall()
            item_ids = [str(row["id"]) for row in rows]
            ownership = "derived_snapshot"
        queue = []
        for item_id_value in item_ids[:bounded]:
            plan = self.playback_plan(
                item_id_value,
                endpoint_id=endpoint_id,
                endpoint_node_id=endpoint_node_id,
                endpoint_capabilities=endpoint_capabilities,
                preferred_quality=preferred_quality,
                preferred_language=preferred_language,
                profile_id=profile,
            )
            if (
                not plan.get("ok")
                and plan.get("error") == "playback_source_unavailable"
            ):
                legacy = self.repository.playback_plan(item_id_value)
                legacy_item = dict(legacy.get("item") or {})
                if (
                    legacy.get("ok")
                    and not _text(legacy_item.get("work_id"))
                    and not _text(legacy_item.get("variant_id"))
                ):
                    plan = self._legacy_playback_plan(
                        legacy,
                        endpoint_id=endpoint_id,
                        endpoint_node_id=endpoint_node_id,
                    )
            if not plan.get("ok"):
                continue
            queue.append(
                {
                    "schema": "adaos.media_control.playback_queue_item.v1",
                    "id": plan["item_id"],
                    "item_id": plan["item_id"],
                    "work_id": plan["work_id"],
                    "variant_id": plan["variant_id"],
                    "source_id": plan["source_id"],
                    "title": plan["title"],
                    "name": plan["title"],
                    "media_kind": plan["media_kind"],
                    "mime_type": plan["mime_type"],
                    "size_bytes": int(
                        plan["descriptor"].get("size_bytes") or 0
                    ),
                    "modified_at": _text(
                        plan["descriptor"].get("modified_at")
                    ),
                    "resource_id": _text(plan["descriptor"].get("resource_id")),
                    "content_path": _text(plan["route"].get("node_path")),
                    "routed_content_path": _text(plan["route"].get("routed_path")),
                    "node_id": _text(plan["route"].get("source_node_id")),
                    "available": True,
                    "descriptor": plan["descriptor"],
                    "route": plan["route"],
                    "compatibility": dict(plan.get("compatibility") or {}),
                    "compatibility_mode": _text(
                        plan.get("compatibility_mode")
                        or (plan.get("compatibility") or {}).get("mode")
                    ),
                }
            )
        queue_source = {"type": kind, "id": token, "ownership": ownership}
        return {
            "ok": True,
            "schema": "adaos.media_center.queue_source.v1",
            "source": queue_source,
            "playback_control": {
                "schema": "adaos.playback.endpoint_control.v1",
                "adapter": {
                    "skill": "media_control_skill",
                    "open_session_method": "open_endpoint_session",
                    "pull_commands_method": "pull_commands",
                    "reconcile_method": "reconcile_endpoint",
                },
                "profile_id": profile,
                "queue_source": queue_source,
                "authority": "endpoint_preferred",
                "checkpoint_interval_seconds": 15,
                "command_poll_interval_ms": 2000,
            },
            "items": queue,
            "count": len(queue),
            "limit": bounded,
            "bounded": True,
            "partial": self.participation()["partial"],
        }

    @staticmethod
    def _legacy_playback_plan(
        legacy: Mapping[str, Any],
        *,
        endpoint_id: str = "",
        endpoint_node_id: str = "",
    ) -> dict[str, Any]:
        item = dict(legacy.get("item") or {})
        resource = dict(legacy.get("resource") or item.get("resource") or {})
        playback = dict(legacy.get("playback") or {})
        item_id = _text(item.get("id"))
        resource_id = _text(
            resource.get("resource_id")
            or resource.get("id")
            or item.get("resource_id")
        )
        media_kind = _text(item.get("media_kind")) or "other"
        mime_type = _text(item.get("mime_type")) or "application/octet-stream"
        node_path = _public_content_path(
            item.get("content_path")
            or resource.get("content_path")
            or playback.get("preferred_path")
        )
        routed_path = _public_content_path(
            item.get("routed_content_path")
            or resource.get("routed_content_path")
            or playback.get("preferred_path")
        )
        route = {
            "schema": "adaos.media_center.playback_route.v1",
            "mode": "root_routed_http_relay",
            "source_node_id": "",
            "endpoint_id": _text(endpoint_id),
            "endpoint_node_id": _text(endpoint_node_id),
            "direct_candidates": [],
            "routed_path": routed_path,
            "node_path": node_path,
            "resource_id": resource_id,
            "fallback": {
                "mode": "root_routed_http_relay",
                "path": routed_path or node_path,
                "target_node_id": "",
                "reason": "legacy_reference_source",
            },
        }
        return {
            "ok": True,
            "schema": PLAYBACK_PLAN_SCHEMA,
            "item_id": item_id,
            "work_id": "",
            "variant_id": "",
            "source_id": _text(item.get("source")) or resource_id,
            "media_kind": media_kind,
            "mime_type": mime_type,
            "title": _text(item.get("title") or item.get("name")),
            "profile_id": "default",
            "quality": {},
            "descriptor": _public_resource_descriptor(
                resource,
                resource_id=resource_id,
                mime_type=mime_type,
                content_path=node_path,
                routed_content_path=routed_path,
            ),
            "route": route,
            "compatibility": {
                "schema": "adaos.playback.compatibility_decision.v1",
                "mode": "direct",
                "ready": True,
                "requires_preparation": False,
                "reasons": ["legacy_reference_source"],
                "selected_tracks": {
                    "video_index": 0 if media_kind == "video" else -1,
                    "audio_index": 0 if media_kind == "audio" else -1,
                    "audio_language": "",
                },
                "endpoint_profile": {"schema": "", "revision": 0, "evidence": {}},
            },
            "compatibility_mode": "legacy_catalog_row",
        }

    def home(
        self,
        *,
        profile_id: str = "default",
        limit: int = 12,
        shared_surface: bool = False,
    ) -> dict[str, Any]:
        bounded = max(1, min(20, int(limit or 12)))
        profile = self.get_profile(profile_id)["profile"]
        show_shared_history = bool(
            profile["policy"].get("show_history_on_shared_surface", False)
        )
        shelves = []
        flattened: list[dict[str, Any]] = []
        recommendation_signals: list[dict[str, Any]] = []
        for shelf_id, title, options in (
            (
                "continue",
                "Continue",
                {"sort": "recent", "continue_only": True},
            ),
            ("favorites", "Favorites", {"favorites_only": True, "sort": "favorite"}),
            ("recent", "Recent", {"sort": "recent", "history_only": True}),
            ("movies", "Movies", {"media_kind": "video", "sort": "title"}),
            ("music", "Music", {"media_kind": "audio", "sort": "title"}),
        ):
            if shared_surface and not show_shared_history and shelf_id in {
                "continue",
                "recent",
            }:
                continue
            page = self.list_items(profile_id=profile_id, limit=bounded, **options)
            if shelf_id in {"favorites", "recent"}:
                recommendation_signals.extend(page["items"])
            shelves.append({"id": shelf_id, "title": title, "layout": "rail", "items": page["items"], "partial": page["partial"]})
            flattened.extend(
                dict(item)
                | {
                    "shelf_id": shelf_id,
                    "shelf_title": title,
                    "queue_source_type": "item",
                    "queue_source_id": _text(item.get("id")),
                }
                for item in page["items"]
            )
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
            flattened.extend(
                dict(item)
                | {
                    "shelf_id": shelf_id,
                    "shelf_title": title,
                    "queue_source_type": "collection",
                    "queue_source_id": _text(item.get("id")),
                }
                for item in page["items"]
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
        flattened.extend(
            dict(item)
            | {
                "shelf_id": "playlists",
                "shelf_title": "Playlists",
                "queue_source_type": "playlist",
                "queue_source_id": _text(item.get("id")),
            }
            for item in playlist_page["items"]
        )
        folder_page = self.folders(profile_id=profile_id, limit=bounded)
        shelves.append(
            {
                "id": "folders",
                "title": "Folders",
                "layout": "rail",
                "items": folder_page["folders"],
                "partial": folder_page["partial"],
            }
        )
        flattened.extend(
            dict(item)
            | {
                "shelf_id": "folders",
                "shelf_title": "Folders",
                "queue_source_type": "folder",
                "queue_source_id": _text(item.get("queue_ref")),
            }
            for item in folder_page["folders"]
        )
        recommendation_page = self.recommendations(
            profile_id=profile_id,
            limit=bounded,
            _signals=recommendation_signals,
        )
        if recommendation_page["enabled"]:
            shelves.append(
                {
                    "id": "recommended",
                    "title": "Recommended",
                    "layout": "rail",
                    "items": recommendation_page["items"],
                    "partial": recommendation_page["partial"],
                }
            )
            flattened.extend(
                dict(item)
                | {
                    "shelf_id": "recommended",
                    "shelf_title": "Recommended",
                    "queue_source_type": "item",
                    "queue_source_id": _text(item.get("id")),
                }
                for item in recommendation_page["items"]
            )
        order = {
            shelf_id: index
            for index, shelf_id in enumerate(
                profile["policy"].get("home_row_order") or HOME_SHELF_ORDER
            )
        }
        shelves.sort(
            key=lambda shelf: (order.get(_text(shelf.get("id")), 999), shelf["id"])
        )
        flattened.sort(
            key=lambda item: order.get(_text(item.get("shelf_id")), 999)
        )
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "profile_id": _text(profile_id) or "default",
            "profile": profile,
            "shared_surface": bool(shared_surface),
            "shelves": shelves,
            "items": flattened,
        }

    def recommendations(
        self,
        *,
        profile_id: str = "default",
        limit: int = 12,
        _signals: Iterable[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        bounded = max(1, min(20, int(limit or 12)))
        profile = self.get_profile(profile_id)["profile"]
        if not bool(profile["policy"].get("recommendations_enabled", True)):
            return {
                "ok": True,
                "schema": COORDINATOR_SCHEMA,
                "profile_id": profile["id"],
                "enabled": False,
                "items": [],
                "count": 0,
                "partial": self.participation()["partial"],
                "algorithm": "disabled_by_profile",
            }
        if _signals is None:
            favorites = self.list_items(
                profile_id=profile["id"],
                favorites_only=True,
                sort="favorite",
                limit=MAX_PAGE_SIZE,
            )["items"]
            recent = self.list_items(
                profile_id=profile["id"],
                sort="recent",
                history_only=True,
                limit=MAX_PAGE_SIZE,
            )["items"]
            signals = favorites + [item for item in recent if item not in favorites]
        else:
            signals = [dict(item) for item in _signals]
        preferred_kinds: dict[str, int] = {}
        preferred_folders: dict[str, int] = {}
        preferred_genres: dict[str, int] = {}
        preferred_artists: dict[str, int] = {}
        for item in signals[:60]:
            kind = _text(item.get("media_kind"))
            if kind:
                preferred_kinds[kind] = preferred_kinds.get(kind, 0) + 1
            folder = _text(item.get("folder_path")).strip("/").split("/", 1)[0]
            if folder:
                preferred_folders[folder.casefold()] = (
                    preferred_folders.get(folder.casefold(), 0) + 1
                )
            for value in list(item.get("genres") or [])[:20]:
                token = fold_text(value)
                if token:
                    preferred_genres[token] = preferred_genres.get(token, 0) + 1
            for value in list(item.get("artists") or [])[:20]:
                token = fold_text(value)
                if token:
                    preferred_artists[token] = preferred_artists.get(token, 0) + 1
        candidates: list[dict[str, Any]] = []
        cursor = ""
        for _page in range(3 if signals else 1):
            page = self.list_items(
                profile_id=profile["id"],
                sort="title",
                limit=MAX_PAGE_SIZE,
                cursor=cursor,
            )
            candidates.extend(page["items"])
            cursor = _text(page["pagination"].get("next_cursor"))
            if not cursor:
                break
        scored: list[tuple[int, str, dict[str, Any], list[str]]] = []
        for item in candidates:
            if item.get("favorite") or item.get("personal", {}).get("last_played_at"):
                continue
            reasons: list[str] = []
            score = 1
            kind = _text(item.get("media_kind"))
            if preferred_kinds.get(kind):
                score += min(20, preferred_kinds[kind] * 2)
                reasons.append(f"preferred_media_kind:{kind}")
            folder = _text(item.get("folder_path")).strip("/").split("/", 1)[0]
            if folder and preferred_folders.get(folder.casefold()):
                score += min(30, preferred_folders[folder.casefold()] * 3)
                reasons.append(f"related_library_section:{folder}")
            matched_genres = [
                _text(value)
                for value in list(item.get("genres") or [])[:20]
                if preferred_genres.get(fold_text(value))
            ]
            if matched_genres:
                score += min(
                    40,
                    sum(preferred_genres[fold_text(value)] for value in matched_genres) * 4,
                )
                reasons.append(f"preferred_genre:{matched_genres[0]}")
            matched_artists = [
                _text(value)
                for value in list(item.get("artists") or [])[:20]
                if preferred_artists.get(fold_text(value))
            ]
            if matched_artists:
                score += min(
                    40,
                    sum(preferred_artists[fold_text(value)] for value in matched_artists) * 5,
                )
                reasons.append(f"preferred_artist:{matched_artists[0]}")
            if not reasons:
                reasons.append("unplayed_library_item")
            scored.append((score, _text(item.get("title")).casefold(), item, reasons))
        scored.sort(key=lambda value: (-value[0], value[1], _text(value[2].get("id"))))
        items: list[dict[str, Any]] = []
        diversity: dict[str, int] = {}
        for score, _title, item, reasons in scored:
            diversity_key = fold_text(
                item.get("series")
                or item.get("album")
                or next(iter(item.get("genres") or []), "")
                or item.get("folder_path")
            )
            if diversity_key and diversity.get(diversity_key, 0) >= 2:
                continue
            if diversity_key:
                diversity[diversity_key] = diversity.get(diversity_key, 0) + 1
            items.append(
                dict(item) | {
                "recommendation": {
                    "algorithm": "bounded_profile_metadata_v2",
                    "score": score,
                    "reasons": reasons,
                    "uses_external_profile": False,
                }
                }
            )
            if len(items) >= bounded:
                break
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "profile_id": profile["id"],
            "enabled": True,
            "items": items,
            "count": len(items),
            "partial": self.participation()["partial"],
            "algorithm": "bounded_profile_metadata_v2",
            "privacy": {
                "profile_scoped": True,
                "external_provider": False,
                "history_opt_out": True,
            },
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
            perceptual_rows = connection.execute(
                """
                SELECT value_json,COUNT(*) AS candidate_count,
                    GROUP_CONCAT(substr(subject_ref,6)) AS item_ids
                FROM metadata_claims
                WHERE field_name='perceptual_hash_v1'
                GROUP BY value_json HAVING COUNT(*)>1
                ORDER BY candidate_count DESC,value_json LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        items = [
            {
                "work_id": str(row["work_id"]),
                "size_bytes": int(row["size_bytes"]),
                "candidate_count": int(row["candidate_count"]),
                "item_ids": str(row["item_ids"]).split(","),
                "evidence": "same_work_and_size",
                "confidence": 0.7,
                "disposition": "review_only",
            }
            for row in rows
        ]
        seen = {tuple(sorted(item["item_ids"])) for item in items}
        for row in perceptual_rows:
            item_ids = str(row["item_ids"]).split(",")
            signature = tuple(sorted(item_ids))
            if signature in seen:
                continue
            seen.add(signature)
            items.append(
                {
                    "perceptual_hash": _json_loads(row["value_json"]),
                    "candidate_count": int(row["candidate_count"]),
                    "item_ids": item_ids,
                    "evidence": "perceptual_sample_hash_v1",
                    "confidence": 0.9,
                    "disposition": "review_only",
                }
            )
            if len(items) >= bounded:
                break
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "items": items[:bounded],
            "count": min(len(items), bounded),
            "source_deletion": False,
            "automatic_merge": False,
        }

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
        if action not in {"metadata", "merge", "split", "regroup"}:
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
            elif action in {"merge", "split"}:
                duplicate_id = subject.removeprefix("work:")
                if action == "merge":
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
                            alias_id,canonical_id,reason,actor_ref,reversible,
                            created_at,active
                        ) VALUES (?, ?, 'user_merge', ?, 1, ?, 1)
                        """,
                        (alias_id, canonical_id, actor, now_iso()),
                    )
                else:
                    row = connection.execute(
                        "SELECT id,alias_of FROM media_works WHERE id=?",
                        (duplicate_id,),
                    ).fetchone()
                    canonical_id = str(row["alias_of"] or "") if row else ""
                    if not canonical_id:
                        connection.rollback()
                        return {"ok": False, "error": "catalog_correction_subject_invalid"}
                    before = {"alias_of": canonical_id}
                    after = {"alias_of": ""}
                    connection.execute(
                        "UPDATE media_works SET alias_of='',revision=revision+1,updated_at=? WHERE id=?",
                        (now_iso(), duplicate_id),
                    )
                    connection.execute(
                        "UPDATE catalog_aliases SET active=0 WHERE alias_id=?",
                        (_stable_id("alias", duplicate_id, canonical_id, size=24),),
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
                connection.execute(
                    "UPDATE catalog_aliases SET active=0 WHERE alias_id=?",
                    (
                        _stable_id(
                            "alias",
                            duplicate_id,
                            _text((_json_loads(row["after_json"]) or {}).get("alias_of")),
                            size=24,
                        ),
                    ),
                )
            elif action == "split":
                duplicate_id = subject.removeprefix("work:")
                canonical_id = _text(before.get("alias_of"))
                connection.execute(
                    "UPDATE media_works SET alias_of=?,revision=revision+1,updated_at=? WHERE id=?",
                    (canonical_id, now_iso(), duplicate_id),
                )
                connection.execute(
                    "UPDATE catalog_aliases SET active=1 WHERE alias_id=?",
                    (_stable_id("alias", duplicate_id, canonical_id, size=24),),
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

    def metadata_facets(
        self,
        *,
        dimension: str = "genre",
        media_kind: str = "playable",
        profile_id: str = "default",
        limit: int = 50,
    ) -> dict[str, Any]:
        field = _text(dimension).lower() or "genre"
        if field == "category":
            field = "genre"
        allowed_fields = {
            "genre", "year", "content_rating", "artist", "album", "series",
            "tag", "country", "director",
        }
        if field not in allowed_fields:
            return {"ok": False, "error": "metadata_facet_dimension_invalid"}
        profile = self.get_profile(_text(profile_id) or "default")["profile"]
        policy = dict(profile.get("policy") or {})
        allowed_kinds = {
            _text(value).lower()
            for value in policy.get("allowed_media_kinds") or []
            if _text(value)
        }
        requested_kind = _text(media_kind).lower()
        admitted = (
            sorted(allowed_kinds & {"audio", "video"})
            if requested_kind == "playable"
            else sorted({requested_kind} & allowed_kinds)
        )
        if not admitted:
            return {
                "ok": True, "schema": COORDINATOR_SCHEMA, "dimension": field,
                "items": [], "count": 0, "bounded": True,
            }
        placeholders = ",".join("?" for _ in admitted)
        bounded = max(1, min(100, int(limit or 50)))
        maximum_rating = max(
            0, min(21, int(policy.get("maximum_maturity_rating") or 0))
        )
        explicit_filter = "" if bool(policy.get("allow_explicit", False)) else "AND c.explicit=0"
        with self.repository.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT mf.normalized_value,mf.display_value,mf.numeric_value,
                    COUNT(DISTINCT mf.item_id) AS item_count
                FROM catalog_metadata_facets mf
                JOIN catalog_items c ON c.id=mf.item_id
                LEFT JOIN personal_media_state ps
                    ON ps.item_id=c.id AND ps.profile_id=?
                WHERE mf.field_name=? AND c.missing=0
                    AND c.media_kind IN ({placeholders})
                    AND c.maturity_rating<=? AND COALESCE(ps.hidden,0)=0
                    {explicit_filter}
                GROUP BY mf.normalized_value,mf.display_value,mf.numeric_value
                ORDER BY CASE WHEN ?='year' THEN mf.numeric_value END DESC,
                    item_count DESC,mf.display_value COLLATE NOCASE
                LIMIT ?
                """,
                (
                    _text(profile_id) or "default", field, *admitted,
                    maximum_rating, field, bounded,
                ),
            ).fetchall()
        return {
            "ok": True,
            "schema": COORDINATOR_SCHEMA,
            "dimension": field,
            "media_kind": requested_kind,
            "items": [
                {
                    "value": str(row["display_value"]),
                    "normalized_value": str(row["normalized_value"]),
                    "numeric_value": row["numeric_value"],
                    "count": int(row["item_count"]),
                }
                for row in rows
            ],
            "count": len(rows),
            "bounded": True,
            "limit": bounded,
        }

    def participation(self) -> dict[str, Any]:
        with self.repository.connect() as connection:
            rows = connection.execute("SELECT * FROM agent_catalog_state ORDER BY agent_id").fetchall()
        agents = [dict(row) for row in rows]
        unavailable = [item["agent_id"] for item in agents if item["availability"] != "available"]
        stale = [item["agent_id"] for item in agents if item["freshness"] != "fresh"]
        return {"agents": agents, "expected_count": len(agents), "available_count": len(agents) - len(unavailable), "unavailable_agent_ids": unavailable, "stale_agent_ids": stale, "partial": bool(unavailable or stale), "fresh": not bool(unavailable or stale)}

    def collection_state(
        self, *, agent_sync: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        participation = self.participation()
        agents = [
            dict(item)
            for item in participation.get("agents") or []
            if isinstance(item, Mapping)
        ]
        observed_roots = [
            _stored_observed_count(item.get("root_count"))
            for item in agents
            if _stored_observed_count(item.get("root_count")) >= 0
        ]
        local_root_count = int(self.repository.list_roots().get("count") or 0)
        root_count = max(sum(observed_roots), local_root_count)
        configured: bool | None
        if root_count > 0:
            configured = True
        elif agents and len(observed_roots) == len(agents):
            configured = False
        else:
            configured = None

        summary = self.repository.compact_summary()
        available_count = int(summary.get("available_count") or 0)
        background_counts = self.background_job_counts()
        coordinator_active = sum(
            background_counts.get(status, 0)
            for status in ("queued", "running", "waiting_resources", "canceling")
        )
        agent_active = sum(
            _stored_observed_count(item.get("active_job_count"))
            for item in agents
            if _stored_observed_count(item.get("active_job_count")) >= 0
        )
        active_operation_count = coordinator_active + agent_active
        sync = dict(agent_sync or {})
        sync_state = str(sync.get("state") or "stopped").strip().lower()
        synchronizing = sync_state in {"running", "catching_up"}

        if available_count > 0:
            state = "updating" if active_operation_count or synchronizing else "ready"
        elif active_operation_count or synchronizing:
            state = "indexing"
        elif configured is True:
            state = "empty"
        elif configured is False:
            state = "unconfigured"
        elif sync_state in {"stopped", "idle", "unknown"}:
            state = "loading"
        else:
            state = "unavailable"
        return {
            "schema": "adaos.media_center.collection_state.v1",
            "state": state,
            "configured": configured,
            "root_count": root_count,
            "available_count": available_count,
            "active_operation_count": active_operation_count,
            "partial": bool(participation.get("partial")),
            "sync_state": sync_state,
            "updated_at": now_iso(),
        }

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
        if kind_token not in {"technical_probe", "metadata_enrichment", "fingerprint", "embedding"}:
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

    def recover_stale_background_jobs(
        self, *, stale_seconds: float = 900.0
    ) -> dict[str, Any]:
        bounded = max(60.0, min(float(stale_seconds), 86400.0))
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=bounded)
        ).isoformat()
        now = now_iso()
        with self.repository.connect() as connection:
            retried = connection.execute(
                """
                UPDATE media_background_jobs
                SET status='queued', error_code='background_worker_interrupted',
                    progress_json=?, started_at='', finished_at='', updated_at=?
                WHERE status='running' AND updated_at<? AND attempts<3
                """,
                (
                    _json_dumps(
                        {"phase": "retry", "completed": 0, "total": 1}
                    ),
                    now,
                    cutoff,
                ),
            ).rowcount
            failed = connection.execute(
                """
                UPDATE media_background_jobs
                SET status='failed', error_code='background_worker_interrupted',
                    progress_json=?, finished_at=?, updated_at=?
                WHERE status='running' AND updated_at<? AND attempts>=3
                """,
                (
                    _json_dumps(
                        {"phase": "failed", "completed": 0, "total": 1}
                    ),
                    now,
                    now,
                    cutoff,
                ),
            ).rowcount
            connection.commit()
        return {
            "ok": True,
            "retried": int(retried),
            "failed": int(failed),
            "stale_seconds": bounded,
        }

    def prune_terminal_background_jobs(
        self, *, retain: int = 10000, batch_size: int = 250
    ) -> dict[str, Any]:
        retained = max(1000, min(int(retain), 100000))
        bounded_batch = max(1, min(int(batch_size), 5000))
        with self.repository.connect() as connection:
            removed = connection.execute(
                """
                DELETE FROM media_background_jobs WHERE id IN (
                    SELECT id FROM media_background_jobs
                    WHERE status IN ('completed','failed','canceled')
                    ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?
                )
                """,
                (bounded_batch, retained),
            ).rowcount
            connection.commit()
        return {
            "ok": True,
            "removed": int(removed),
            "retained": retained,
            "batch_size": bounded_batch,
        }

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

    def _record_metadata_claim_connection(
        self,
        connection: sqlite3.Connection,
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
        return {"ok": True, "claim_id": claim_id, "revision": revision}

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
        with self.repository.connect() as connection:
            result = self._record_metadata_claim_connection(
                connection,
                subject_ref=subject_ref,
                field_name=field_name,
                value=value,
                provenance=provenance,
                confidence=confidence,
                preferred=preferred,
            )
            if result.get("ok") and _text(subject_ref).startswith("item:"):
                self._refresh_metadata_projection(
                    connection, _text(subject_ref).removeprefix("item:")
                )
            elif result.get("ok") and _text(subject_ref).startswith("work:"):
                work_id = _text(subject_ref).removeprefix("work:")
                item_rows = connection.execute(
                    "SELECT id FROM catalog_items WHERE work_id=?", (work_id,)
                ).fetchall()
                for row in item_rows:
                    self._refresh_metadata_projection(connection, str(row["id"]))
            connection.commit()
        return result

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
                    "fingerprint": str(row["fingerprint"]),
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

    def background_job_counts(self) -> dict[str, int]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                """
                SELECT status,COUNT(*) AS count FROM media_background_jobs
                GROUP BY status
                """
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def operation_state(self, *, limit: int = 30) -> dict[str, Any]:
        operations = self.operations(limit=limit)
        counts = self.background_job_counts()
        active_count = sum(
            counts.get(status, 0)
            for status in ("queued", "running", "waiting_resources", "canceling")
        )
        return {
            **operations,
            "schema": "adaos.media_center.operation_state.v1",
            "counts": counts,
            "active_count": active_count,
            "updated_at": now_iso(),
        }

    def diagnostics(
        self, *, summary: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        with self.repository.connect() as connection:
            works = int(connection.execute("SELECT COUNT(*) FROM media_works").fetchone()[0])
            variants = int(connection.execute("SELECT COUNT(*) FROM media_variants").fetchone()[0])
            collections = int(connection.execute("SELECT COUNT(*) FROM media_collections").fetchone()[0])
            jobs = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status,COUNT(*) AS count FROM media_background_jobs
                    GROUP BY status
                    """
                ).fetchall()
            }
            agents = [
                {
                    "agent_id": str(row["agent_id"]),
                    "node_id": str(row["node_id"]),
                    "availability": str(row["availability"]),
                    "freshness": str(row["freshness"]),
                    "last_error": str(row["last_error"])[:200],
                    "updated_at": str(row["updated_at"]),
                }
                for row in connection.execute(
                    """
                    SELECT agent_id,node_id,availability,freshness,last_error,updated_at
                    FROM agent_catalog_state ORDER BY agent_id LIMIT 100
                    """
                ).fetchall()
            ]
        compact_summary = dict(summary or self.repository.compact_summary())
        # FTS5 COUNT(*) scans every indexed token payload and can take minutes
        # for large libraries. Search rows have a strict one-to-one catalog
        # rowid invariant, so the ordinary catalog count is the bounded witness.
        search_rows = int(compact_summary["total_count"])
        recommendations: list[dict[str, Any]] = []
        if not agents:
            recommendations.append(
                {
                    "id": "deploy_library_agent",
                    "severity": "warning",
                    "reason": "no_library_agents_observed",
                    "review_required": True,
                    "proposed_action": {
                        "tool": "media_center_skill.configure_deployment",
                        "mode": "dry_run",
                    },
                }
            )
        for agent in agents:
            if agent["availability"] != "available" or agent["freshness"] != "fresh":
                recommendations.append(
                    {
                        "id": f"reconcile_agent:{agent['agent_id']}",
                        "severity": "warning",
                        "reason": "agent_unavailable_or_stale",
                        "agent_id": agent["agent_id"],
                        "node_id": agent["node_id"],
                        "review_required": True,
                        "proposed_action": {
                            "tool": "media_center_skill.sync_agent",
                            "arguments": {"max_pages": 4, "limit": 500},
                        },
                    }
                )
        if jobs.get("failed", 0):
            recommendations.append(
                {
                    "id": "review_failed_metadata_jobs",
                    "severity": "warning",
                    "reason": "background_jobs_failed",
                    "count": jobs["failed"],
                    "review_required": True,
                    "proposed_action": {
                        "tool": "media_center_skill.operations",
                        "arguments": {"limit": 30},
                    },
                }
            )
        return {
            "ok": True, "schema": COORDINATOR_SCHEMA, "catalog_revision": self.catalog_revision(),
            "counts": {"sources": compact_summary["total_count"], "works": works, "variants": variants, "collections": collections},
            "participation": self.participation(),
            "budgets": {"catalog_page": MAX_PAGE_SIZE, "player_queue": 10, "agent_delta_page": 1000},
            "storage": {"media_bytes": "external", "catalog": "skill_local_relational"},
            "search": {
                "indexed_rows": search_rows,
                "ranking_version": "federated-discovery-v2",
                "local_discovery_candidate_default": 5000,
                "local_discovery_candidate_hard_maximum": 20000,
            },
            "background_jobs": jobs,
            "agents": agents,
            "repair_recommendations": recommendations[:30],
        }
