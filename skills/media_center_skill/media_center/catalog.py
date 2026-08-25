from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = "adaos.media_center.catalog.v1"
CATALOG_SCHEMA_REVISION = "2026-08-25.1"
SKILL_NAME = "media_center_skill"
MAX_LIST_LIMIT = 500
DEFAULT_LIST_LIMIT = 100
PLAYABLE_KINDS = {"video", "audio"}
_SCHEMA_READ_ATTEMPTS = 4
_SCHEMA_READ_TIMEOUT_SECONDS = 1.0
_SCHEMA_READ_RETRY_SECONDS = (0.05, 0.15, 0.35)


class _ClosingConnection(sqlite3.Connection):
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def default_db_path() -> Path:
    override = _text(os.environ.get("MEDIA_CENTER_DB_PATH"))
    if override:
        path = Path(override).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    try:
        from adaos.sdk.data.skill_env import skill_env_path

        env_path = Path(skill_env_path())
        data_root = env_path.parents[1] if env_path.parent.name == "db" else env_path.parent
        db_dir = data_root / "db"
    except Exception:
        db_dir = Path(__file__).resolve().parents[1] / ".skill_state" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "media_center.sqlite3"


def schema_revision_is_current(
    db_path: str | Path,
    *,
    table: str,
    key: str,
    expected: str,
    unavailable_error: str,
) -> bool:
    path = Path(db_path)
    if not path.exists():
        return False
    for attempt in range(_SCHEMA_READ_ATTEMPTS):
        try:
            with closing(
                sqlite3.connect(str(path), timeout=_SCHEMA_READ_TIMEOUT_SECONDS)
            ) as connection:
                row = connection.execute(
                    f"SELECT value FROM {table} WHERE key=?",
                    (key,),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "no such table" in message:
                return False
            transient = any(token in message for token in ("locked", "busy"))
            if transient and attempt + 1 < _SCHEMA_READ_ATTEMPTS:
                time.sleep(_SCHEMA_READ_RETRY_SECONDS[attempt])
                continue
            raise RuntimeError(unavailable_error) from exc
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(unavailable_error) from exc
        return bool(row and str(row[0]) == expected)
    raise RuntimeError(unavailable_error)


class MediaCenterRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=30,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            connection.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError as exc:
            if not any(token in str(exc).lower() for token in ("locked", "busy")):
                connection.close()
                raise
        return connection

    def _schema_is_current(self) -> bool:
        return schema_revision_is_current(
            self.db_path,
            table="meta",
            key="catalog_schema_revision",
            expected=CATALOG_SCHEMA_REVISION,
            unavailable_error="media_center_schema_state_unavailable",
        )

    def ensure_schema(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self._schema_is_current():
            return {
                "ok": True,
                "schema": SCHEMA_VERSION,
                "db_path": str(self.db_path),
                "retired_legacy_count": 0,
                "migration": "current",
            }
        retired_legacy_count = 0
        with closing(sqlite3.connect(str(self.db_path), timeout=30)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            # New catalogs reclaim deleted pages incrementally. Existing catalogs
            # adopt this setting after their explicit one-time VACUUM maintenance.
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_items (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    media_kind TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    modified_at TEXT NOT NULL DEFAULT '',
                    content_path TEXT NOT NULL DEFAULT '',
                    routed_content_path TEXT NOT NULL DEFAULT '',
                    playback_id TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL DEFAULT '',
                    descriptor_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    indexed_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    missing INTEGER NOT NULL DEFAULT 0,
                    favorite INTEGER NOT NULL DEFAULT 0,
                    play_count INTEGER NOT NULL DEFAULT 0,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(source, resource_id)
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_media_center_kind ON catalog_items(media_kind, missing)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_media_center_source ON catalog_items(source, missing)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_media_center_recent ON catalog_items(missing, modified_at DESC)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    discovered_count INTEGER NOT NULL,
                    updated_count INTEGER NOT NULL,
                    missing_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS library_roots (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    include_images INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_scan_at TEXT NOT NULL DEFAULT '',
                    last_status TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            retired_legacy_count = connection.execute(
                """
                UPDATE catalog_items
                SET missing = 1, last_seen_at = ?
                WHERE source = 'media_server'
                  AND missing = 0
                  AND (
                    (metadata_json LIKE '%"namespace":"media-center"%'
                     AND metadata_json LIKE '%"variant":"import"%')
                    OR name GLOB 'media-center-????????????????????????-import.*'
                  )
                """,
                (now_iso(),),
            ).rowcount
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('catalog_schema_revision', ?)",
                (CATALOG_SCHEMA_REVISION,),
            )
            connection.commit()
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "db_path": str(self.db_path),
            "retired_legacy_count": max(0, int(retired_legacy_count or 0)),
        }

    def scan_resources(
        self,
        resources: Iterable[Mapping[str, Any]],
        *,
        source: str = "all",
        mark_missing: bool = True,
    ) -> dict[str, Any]:
        started_at = now_iso()
        started_monotonic = time.monotonic()
        normalized = [_normalize_resource(item) for item in resources]
        items = [item for item in normalized if item is not None]
        seen_keys = {(item["source"], item["resource_id"]) for item in items}
        scan_sources = {item["source"] for item in items}
        if source and source != "all":
            scan_sources.add(_normalize_source(source))

        updated = 0
        with self.connect() as connection:
            for item in items:
                previous = connection.execute(
                    "SELECT fingerprint, favorite, tags_json, play_count FROM catalog_items WHERE source = ? AND resource_id = ?",
                    (item["source"], item["resource_id"]),
                ).fetchone()
                favorite = int(previous["favorite"]) if previous else 0
                tags_json = str(previous["tags_json"]) if previous else "[]"
                play_count = int(previous["play_count"]) if previous else 0
                if previous is None or str(previous["fingerprint"]) != item["fingerprint"]:
                    updated += 1
                connection.execute(
                    """
                    INSERT INTO catalog_items (
                        id, source, resource_id, name, title, media_kind, mime_type,
                        size_bytes, modified_at, content_path, routed_content_path,
                        playback_id, source_path, descriptor_json, metadata_json,
                        fingerprint, indexed_at, last_seen_at, missing, favorite,
                        play_count, tags_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    ON CONFLICT(source, resource_id) DO UPDATE SET
                        name = excluded.name,
                        title = excluded.title,
                        media_kind = excluded.media_kind,
                        mime_type = excluded.mime_type,
                        size_bytes = excluded.size_bytes,
                        modified_at = excluded.modified_at,
                        content_path = excluded.content_path,
                        routed_content_path = excluded.routed_content_path,
                        playback_id = excluded.playback_id,
                        source_path = excluded.source_path,
                        descriptor_json = excluded.descriptor_json,
                        metadata_json = excluded.metadata_json,
                        fingerprint = excluded.fingerprint,
                        indexed_at = excluded.indexed_at,
                        last_seen_at = excluded.last_seen_at,
                        missing = 0
                    """,
                    (
                        item["id"],
                        item["source"],
                        item["resource_id"],
                        item["name"],
                        item["title"],
                        item["media_kind"],
                        item["mime_type"],
                        item["size_bytes"],
                        item["modified_at"],
                        item["content_path"],
                        item["routed_content_path"],
                        item["playback_id"],
                        item["source_path"],
                        _json_dumps(item["descriptor"]),
                        _json_dumps(item["metadata"]),
                        item["fingerprint"],
                        started_at,
                        started_at,
                        favorite,
                        play_count,
                        tags_json,
                    ),
                )

            missing_count = 0
            if mark_missing and scan_sources:
                rows = connection.execute(
                    f"SELECT source, resource_id FROM catalog_items WHERE missing = 0 AND source IN ({','.join('?' for _ in scan_sources)})",
                    tuple(sorted(scan_sources)),
                ).fetchall()
                for row in rows:
                    key = (str(row["source"]), str(row["resource_id"]))
                    if key in seen_keys:
                        continue
                    connection.execute(
                        "UPDATE catalog_items SET missing = 1, last_seen_at = ? WHERE source = ? AND resource_id = ?",
                        (started_at, key[0], key[1]),
                    )
                    missing_count += 1

            run_id = hashlib.sha256(
                f"{started_at}:{source}:{len(items)}:{time.monotonic_ns()}".encode(
                    "utf-8"
                )
            ).hexdigest()[:20]
            connection.execute(
                """
                INSERT INTO scan_runs(id, started_at, finished_at, source, discovered_count, updated_count, missing_count, status, message)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ok', '')
                """,
                (run_id, started_at, now_iso(), source, len(items), updated, missing_count),
            )
            connection.commit()

        summary = self.summary()
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "run_id": run_id,
            "source": source,
            "discovered_count": len(items),
            "updated_count": updated,
            "missing_count": missing_count,
            "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
            "summary": summary,
        }

    def list_items(
        self,
        *,
        query: str = "",
        media_kind: str = "",
        source: str = "",
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        include_missing: bool = False,
        favorites_only: bool = False,
        sort: str = "recent",
    ) -> dict[str, Any]:
        limit = max(1, min(MAX_LIST_LIMIT, int(limit or DEFAULT_LIST_LIMIT)))
        offset = max(0, int(offset or 0))
        filters = []
        params: list[Any] = []
        if not include_missing:
            filters.append("missing = 0")
        if favorites_only:
            filters.append("favorite = 1")
        query_token = _text(query)
        if query_token:
            like = f"%{query_token.lower()}%"
            filters.append("(lower(title) LIKE ? OR lower(name) LIKE ? OR lower(source_path) LIKE ?)")
            params.extend([like, like, like])
        kind_token = _normalize_kind(media_kind)
        if kind_token == "playable":
            filters.append("media_kind IN (?, ?)")
            params.extend(sorted(PLAYABLE_KINDS))
        elif kind_token:
            filters.append("media_kind = ?")
            params.append(kind_token)
        source_token = _normalize_source(source)
        if source_token and source_token != "all":
            filters.append("source = ?")
            params.append(source_token)

        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        order = {
            "title": "lower(title) ASC, modified_at DESC",
            "size": "size_bytes DESC, modified_at DESC",
            "source": "source ASC, lower(title) ASC",
            "favorite": "favorite DESC, modified_at DESC",
        }.get(_text(sort).lower(), "modified_at DESC, indexed_at DESC")

        with self.connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM catalog_items {where}", tuple(params)).fetchone()[0])
            rows = connection.execute(
                f"SELECT * FROM catalog_items {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        items = [_public_item(row) for row in rows]
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "items": items,
            "count": len(items),
            "total_count": total,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "next_offset": offset + len(items) if offset + len(items) < total else None,
                "has_more": offset + len(items) < total,
            },
            "summary": self.summary(),
            "facets": self.facets(),
        }

    def get_item(self, item_id: str) -> dict[str, Any]:
        token = _text(item_id)
        if not token:
            return {"ok": False, "error": "item_id_required"}
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM catalog_items WHERE id = ?", (token,)).fetchone()
        if not row:
            return {"ok": False, "error": "item_not_found", "item_id": token}
        item = _public_item(row)
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "item": item,
            "resource": item.get("resource") or {},
            "content": _item_markdown(item),
        }

    def playback_plan(self, item_id: str) -> dict[str, Any]:
        item_result = self.get_item(item_id)
        if not item_result.get("ok"):
            return item_result
        item = dict(item_result["item"])
        resource = dict(item.get("resource") or {})
        path = _text(item.get("routed_content_path")) or _text(item.get("content_path"))
        if not path:
            return {"ok": False, "error": "content_path_missing", "item": item}
        with self.connect() as connection:
            connection.execute("UPDATE catalog_items SET play_count = play_count + 1 WHERE id = ?", (_text(item_id),))
            connection.commit()
        return {
            "ok": True,
            "schema": "adaos.media_center.playback_plan.v1",
            "item": item,
            "resource": resource,
            "playback": {
                "mode": "core_media_resource",
                "preferred_path": path,
                "media_kind": item.get("media_kind") or "other",
                "mime_type": item.get("mime_type") or "application/octet-stream",
            },
        }

    def set_favorite(self, item_id: str, favorite: bool = True) -> dict[str, Any]:
        token = _text(item_id)
        with self.connect() as connection:
            row = connection.execute("SELECT id FROM catalog_items WHERE id = ?", (token,)).fetchone()
            if not row:
                return {"ok": False, "error": "item_not_found", "item_id": token}
            connection.execute(
                "UPDATE catalog_items SET favorite = ? WHERE id = ?",
                (1 if favorite else 0, token),
            )
            connection.commit()
        return self.get_item(token)

    def add_root(self, path: str, *, label: str = "", include_images: bool = False) -> dict[str, Any]:
        path_token = _text(path)
        if not path_token:
            return {"ok": False, "error": "root_path_required", "roots": self.list_roots()["items"]}
        root_path = Path(path_token).expanduser()
        try:
            resolved = root_path.resolve(strict=True)
        except Exception:
            return {"ok": False, "error": "root_path_not_found", "path": path_token, "roots": self.list_roots()["items"]}
        if not resolved.is_dir():
            return {"ok": False, "error": "root_path_not_directory", "path": str(resolved), "roots": self.list_roots()["items"]}

        now = now_iso()
        root_id = _root_id(str(resolved))
        root_label = _text(label) or resolved.name or str(resolved)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM library_roots WHERE id = ? OR path = ?",
                (root_id, str(resolved)),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now
            connection.execute(
                """
                INSERT INTO library_roots (
                    id, path, label, enabled, include_images, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    label = excluded.label,
                    enabled = 1,
                    include_images = excluded.include_images,
                    updated_at = excluded.updated_at
                """,
                (root_id, str(resolved), root_label, 1 if include_images else 0, created_at, now),
            )
            connection.commit()
        roots = self.list_roots()["items"]
        root = next((item for item in roots if item["id"] == root_id), None)
        return {"ok": True, "schema": SCHEMA_VERSION, "root": root, "roots": roots, "summary": self.summary()}

    def remove_root(self, *, root_id: str = "", path: str = "") -> dict[str, Any]:
        id_token = _text(root_id)
        path_token = _text(path)
        if not id_token and path_token:
            try:
                id_token = _root_id(str(Path(path_token).expanduser().resolve(strict=False)))
            except Exception:
                id_token = ""
        if not id_token:
            return {"ok": False, "error": "root_id_required", "roots": self.list_roots()["items"]}
        with self.connect() as connection:
            row = connection.execute("SELECT id FROM library_roots WHERE id = ?", (id_token,)).fetchone()
            if not row:
                return {"ok": False, "error": "root_not_found", "root_id": id_token, "roots": self.list_roots()["items"]}
            connection.execute(
                "UPDATE library_roots SET enabled = 0, updated_at = ? WHERE id = ?",
                (now_iso(), id_token),
            )
            connection.commit()
        return {"ok": True, "schema": SCHEMA_VERSION, "roots": self.list_roots()["items"], "summary": self.summary()}

    def root_delete_plan(self, *, root_id: str = "", path: str = "") -> dict[str, Any]:
        id_token = _text(root_id)
        path_token = _text(path)
        with self.connect() as connection:
            if id_token:
                root = connection.execute("SELECT * FROM library_roots WHERE id = ?", (id_token,)).fetchone()
            elif path_token:
                try:
                    resolved_path = str(Path(path_token).expanduser().resolve(strict=False))
                except Exception:
                    resolved_path = path_token
                root = connection.execute("SELECT * FROM library_roots WHERE path = ?", (resolved_path,)).fetchone()
            else:
                root = None
            if root is None:
                error = "root_id_required" if not id_token and not path_token else "root_not_found"
                return {"ok": False, "error": error, "root_id": id_token, "roots": self.list_roots()["items"]}
            item_rows = connection.execute(
                "SELECT id, resource_id, metadata_json FROM catalog_items"
            ).fetchall()

        public_root = _public_root(root)
        item_ids: list[str] = []
        resource_ids: list[str] = []
        for item in item_rows:
            metadata = _json_loads(item["metadata_json"])
            if not _metadata_matches_root(metadata, public_root):
                continue
            item_ids.append(str(item["id"]))
            resource_id = _text(item["resource_id"])
            if resource_id.startswith("ref_") and resource_id not in resource_ids:
                resource_ids.append(resource_id)
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "root": public_root,
            "item_ids": item_ids,
            "resource_ids": resource_ids,
            "item_count": len(item_ids),
            "resource_link_count": len(resource_ids),
        }

    def delete_root(self, *, root_id: str = "", path: str = "") -> dict[str, Any]:
        plan = self.root_delete_plan(root_id=root_id, path=path)
        if not plan.get("ok"):
            return plan
        root = dict(plan["root"])
        item_ids = [str(item_id) for item_id in plan.get("item_ids") or []]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if item_ids:
                placeholders = ",".join("?" for _ in item_ids)
                connection.execute(f"DELETE FROM catalog_items WHERE id IN ({placeholders})", tuple(item_ids))
            deleted_root = connection.execute("DELETE FROM library_roots WHERE id = ?", (root["id"],)).rowcount
            connection.commit()
        if not deleted_root:
            return {**plan, "ok": False, "error": "root_delete_conflict"}
        return {
            **plan,
            "deleted": True,
            "deleted_item_count": len(item_ids),
            "roots": self.list_roots()["items"],
            "summary": self.summary(),
        }

    def list_roots(self, *, include_disabled: bool = False) -> dict[str, Any]:
        where = "" if include_disabled else "WHERE enabled = 1"
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM library_roots {where} ORDER BY lower(label) ASC, path ASC"
            ).fetchall()
        items = [_public_root(row) for row in rows]
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "items": items,
            "roots": items,
            "count": len(items),
            "summary": {
                "title": "Media folders",
                "value": len(items),
                "subtitle": f"{len(items)} active folders",
            },
        }

    def mark_root_scanned(self, root_id: str, *, status: str) -> None:
        token = _text(root_id)
        if not token:
            return
        with self.connect() as connection:
            connection.execute(
                "UPDATE library_roots SET last_scan_at = ?, last_status = ?, updated_at = ? WHERE id = ?",
                (now_iso(), _text(status), now_iso(), token),
            )
            connection.commit()

    def summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN missing = 0 THEN 1 ELSE 0 END) AS available,
                  SUM(CASE WHEN missing = 0 THEN size_bytes ELSE 0 END) AS total_bytes,
                  MAX(indexed_at) AS indexed_at
                FROM catalog_items
                """
            ).fetchone()
            last_scan = connection.execute(
                "SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        available = int(row["available"] or 0) if row else 0
        total = int(row["total"] or 0) if row else 0
        total_bytes = int(row["total_bytes"] or 0) if row else 0
        return {
            "title": "Media Center",
            "value": available,
            "subtitle": f"{available} available / {total} cataloged",
            "details": f"{_format_bytes(total_bytes)} indexed",
            "schema": SCHEMA_VERSION,
            "total_count": total,
            "available_count": available,
            "missing_count": max(0, total - available),
            "total_bytes": total_bytes,
            "indexed_at": _text(row["indexed_at"] if row else ""),
            "last_scan": dict(last_scan) if last_scan else None,
        }

    def compact_summary(self) -> dict[str, Any]:
        """Return first-paint counters without reading wide catalog rows."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM catalog_items
                        INDEXED BY idx_media_center_recent) AS total,
                    (SELECT COUNT(*) FROM catalog_items
                        INDEXED BY idx_media_center_recent
                        WHERE missing=0) AS available
                """
            ).fetchone()
            last_scan = connection.execute(
                "SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        available = int(row["available"] or 0) if row else 0
        total = int(row["total"] or 0) if row else 0
        return {
            "title": "Media Center",
            "value": available,
            "subtitle": f"{available} available / {total} cataloged",
            "details": "Media bytes remain at their source",
            "schema": SCHEMA_VERSION,
            "total_count": total,
            "available_count": available,
            "missing_count": max(0, total - available),
            "total_bytes": 0,
            "total_bytes_exact": False,
            "indexed_at": _text(last_scan["finished_at"] if last_scan else ""),
            "last_scan": dict(last_scan) if last_scan else None,
        }

    def facets(self) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as connection:
            kind_rows = connection.execute(
                "SELECT media_kind AS id, COUNT(*) AS count FROM catalog_items WHERE missing = 0 GROUP BY media_kind ORDER BY count DESC, id ASC"
            ).fetchall()
            source_rows = connection.execute(
                "SELECT source AS id, COUNT(*) AS count FROM catalog_items WHERE missing = 0 GROUP BY source ORDER BY count DESC, id ASC"
            ).fetchall()
        media_kind = [{"id": str(row["id"]), "label": _label(str(row["id"])), "count": int(row["count"])} for row in kind_rows]
        playable_count = sum(item["count"] for item in media_kind if item["id"] in PLAYABLE_KINDS)
        if playable_count:
            media_kind.insert(0, {"id": "playable", "label": "Playable", "count": playable_count})
        return {
            "media_kind": media_kind,
            "source": [{"id": str(row["id"]), "label": _label(str(row["id"])), "count": int(row["count"])} for row in source_rows],
        }


def _normalize_resource(resource: Mapping[str, Any]) -> dict[str, Any] | None:
    descriptor = dict(resource)
    source = _normalize_source(descriptor.get("source")) or "unknown"
    resource_id = _text(descriptor.get("resource_id") or descriptor.get("id") or descriptor.get("name"))
    name = _text(descriptor.get("name") or resource_id)
    if not resource_id or not name:
        return None
    mime_type = _text(descriptor.get("mime_type") or descriptor.get("mime")) or "application/octet-stream"
    content_path = _text(descriptor.get("content_path"))
    routed_content_path = _text(descriptor.get("routed_content_path") or descriptor.get("browser_path"))
    source_path = _text(descriptor.get("source_path") or descriptor.get("path"))
    playback_id = _text(descriptor.get("playback_id"))
    size_bytes = _int(descriptor.get("size_bytes"))
    modified_at = _text(descriptor.get("modified_at"))
    metadata = descriptor.get("metadata") if isinstance(descriptor.get("metadata"), Mapping) else {}
    media_kind = _media_kind(mime_type, name)
    title = _title_from_name(_text(descriptor.get("title")) or name)
    fingerprint_payload = {
        "source": source,
        "resource_id": resource_id,
        "name": name,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "modified_at": modified_at,
        "content_path": content_path,
        "routed_content_path": routed_content_path,
        "playback_id": playback_id,
        "source_path": source_path,
    }
    return {
        "id": _item_id(source, resource_id),
        "source": source,
        "resource_id": resource_id,
        "name": name,
        "title": title,
        "media_kind": media_kind,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "modified_at": modified_at,
        "content_path": content_path,
        "routed_content_path": routed_content_path,
        "playback_id": playback_id,
        "source_path": source_path,
        "descriptor": descriptor,
        "metadata": dict(metadata),
        "fingerprint": hashlib.sha256(_json_dumps(fingerprint_payload).encode("utf-8")).hexdigest(),
    }


def _public_artwork(metadata: Mapping[str, Any]) -> dict[str, Any]:
    candidates = [
        dict(candidate)
        for candidate in metadata.get("artwork_candidates") or []
        if isinstance(candidate, Mapping)
    ][:20]
    candidates.sort(
        key=lambda candidate: (
            {"poster": 0, "cover": 0, "backdrop": 1, "logo": 2}.get(
                _text(candidate.get("kind")).lower(), 3
            ),
            _text(candidate.get("url")),
        )
    )
    fallback_urls = list(
        dict.fromkeys(
            url
            for candidate in candidates
            if (url := _public_direct_url(candidate.get("url")))
        )
    )[:8]
    artwork = metadata.get("artwork")
    if isinstance(artwork, Mapping):
        artwork_descriptor = artwork.get("descriptor")
        artwork_descriptor = (
            artwork_descriptor if isinstance(artwork_descriptor, Mapping) else {}
        )
        raw_url = _text(
            artwork_descriptor.get("routed_content_path")
            or artwork_descriptor.get("browser_path")
            or artwork_descriptor.get("browser_url")
            or artwork_descriptor.get("content_path")
            or artwork.get("url")
        )
        url = _public_content_path(raw_url) or _public_direct_url(raw_url)
        state = _text(artwork.get("state")) or "unavailable"
        if state == "ready" and not url:
            state = "failed"
        descriptor = _public_resource_descriptor(
            artwork_descriptor,
            resource_id=_text(
                artwork_descriptor.get("resource_id")
                or artwork_descriptor.get("id")
                or artwork_descriptor.get("filename")
            ),
            name=_text(
                artwork_descriptor.get("name")
                or artwork_descriptor.get("filename")
            ),
            mime_type=_text(
                artwork_descriptor.get("mime_type")
                or artwork_descriptor.get("mime")
            ),
            size_bytes=_int(artwork_descriptor.get("size_bytes")),
            modified_at=_text(artwork_descriptor.get("modified_at")),
            content_path=artwork_descriptor.get("content_path"),
            routed_content_path=(
                artwork_descriptor.get("routed_content_path")
                or artwork_descriptor.get("browser_path")
            ),
        )
        return {
            "schema": "adaos.media.artwork.v1",
            "state": state,
            "url": url,
            "descriptor": descriptor,
            "provider_id": _text(artwork.get("provider_id")),
            "source_kind": _text(artwork.get("source_kind")),
            "source_revision": _int(artwork.get("exact_source_revision")),
            "source_fingerprint": _text(
                artwork.get("exact_source_fingerprint")
            )[:128],
            "width": _int(artwork.get("width")),
            "height": _int(artwork.get("height")),
            "error_code": (
                "artwork_route_unavailable"
                if state == "failed" and not url
                else _text(artwork.get("error_code"))
            ),
            "fallback_urls": [candidate for candidate in fallback_urls if candidate != url],
        }
    for candidate in candidates:
        url = _public_direct_url(candidate.get("url"))
        if not url:
            continue
        return {
            "schema": "adaos.media.artwork.v1",
            "state": "ready",
            "url": url,
            "descriptor": {},
            "provider_id": _text(candidate.get("provider")),
            "source_kind": "external_candidate",
            "source_revision": 0,
            "source_fingerprint": "",
            "width": _int(candidate.get("width")),
            "height": _int(candidate.get("height")),
            "error_code": "",
            "fallback_urls": [candidate_url for candidate_url in fallback_urls if candidate_url != url],
        }
    return {
        "schema": "adaos.media.artwork.v1",
        "state": "missing",
        "url": "",
        "descriptor": {},
        "provider_id": "",
        "source_kind": "fallback",
        "source_revision": 0,
        "source_fingerprint": "",
        "width": 0,
        "height": 0,
        "error_code": "",
        "fallback_urls": [],
    }


def _public_item(row: sqlite3.Row) -> dict[str, Any]:
    descriptor = _json_loads(row["descriptor_json"])
    metadata = _json_loads(row["metadata_json"])
    metadata = metadata if isinstance(metadata, Mapping) else {}
    tags = _json_loads(row["tags_json"])
    tags_list = tags if isinstance(tags, list) else []
    item = {
        "id": str(row["id"]),
        "schema": SCHEMA_VERSION,
        "source": str(row["source"]),
        "resource_id": str(row["resource_id"]),
        "name": str(row["name"]),
        "title": str(row["title"]),
        "subtitle": f"{_label(str(row['media_kind']))} - {_label(str(row['source']))} - {_format_bytes(int(row['size_bytes'] or 0))}",
        "media_kind": str(row["media_kind"]),
        "kind": str(row["media_kind"]),
        "icon": _kind_icon(str(row["media_kind"])),
        "mime_type": str(row["mime_type"]),
        "size_bytes": int(row["size_bytes"] or 0),
        "modified_at": str(row["modified_at"] or ""),
        "content_path": _public_content_path(row["content_path"]),
        "routed_content_path": _public_content_path(row["routed_content_path"]),
        "playback_id": str(row["playback_id"] or ""),
        "missing": bool(row["missing"]),
        "favorite": bool(row["favorite"]),
        "play_count": int(row["play_count"] or 0),
        "tags": tags_list,
        "resource": _public_resource_descriptor(
            descriptor if isinstance(descriptor, Mapping) else {},
            resource_id=str(row["resource_id"]),
            name=str(row["name"]),
            mime_type=str(row["mime_type"]),
            size_bytes=int(row["size_bytes"] or 0),
            modified_at=str(row["modified_at"] or ""),
            content_path=row["content_path"],
            routed_content_path=row["routed_content_path"],
            playback_id=str(row["playback_id"] or ""),
        ),
        "playable": bool(str(row["content_path"] or "") or str(row["routed_content_path"] or "")),
    }
    item["preview"] = item["subtitle"]
    item["artwork"] = _public_artwork(metadata)
    return item


def _public_resource_descriptor(
    descriptor: Mapping[str, Any],
    *,
    resource_id: str = "",
    name: str = "",
    mime_type: str = "",
    size_bytes: int = 0,
    modified_at: str = "",
    content_path: Any = "",
    routed_content_path: Any = "",
    playback_id: str = "",
) -> dict[str, Any]:
    """Project an internal source descriptor into the browser-safe contract."""
    resolved_resource_id = _text(resource_id or descriptor.get("resource_id") or descriptor.get("id"))
    result: dict[str, Any] = {
        "schema": _text(descriptor.get("schema")) or "adaos.media.resource.v1",
        "id": resolved_resource_id,
        "resource_id": resolved_resource_id,
        "name": _text(name or descriptor.get("name")),
        "mime_type": _text(mime_type or descriptor.get("mime_type") or descriptor.get("mime")),
        "size_bytes": _int(size_bytes or descriptor.get("size_bytes")),
        "modified_at": _text(modified_at or descriptor.get("modified_at")),
        "content_path": _public_content_path(content_path or descriptor.get("content_path")),
        "routed_content_path": _public_content_path(
            routed_content_path
            or descriptor.get("routed_content_path")
            or descriptor.get("browser_path")
        ),
        "playback_id": _text(playback_id or descriptor.get("playback_id")),
        "metadata": _public_metadata(descriptor.get("metadata")),
    }
    delivery = descriptor.get("delivery")
    if isinstance(delivery, Mapping):
        public_delivery = {
            key: delivery.get(key)
            for key in ("storage_mode", "preferred_route", "fallback_route", "range_supported")
            if key in delivery
        }
        if public_delivery:
            result["delivery"] = _public_metadata(public_delivery)
    return result


def _public_content_path(value: Any) -> str:
    token = _text(value)
    if not token.startswith(("/api/node/media/", "/media/")):
        return ""
    return token.split("?", 1)[0].split("#", 1)[0]


def _public_direct_url(value: Any) -> str:
    token = _text(value)
    if not token.startswith(("http://", "https://")):
        return ""
    try:
        parsed = urlsplit(token)
    except ValueError:
        return ""
    if not parsed.hostname or parsed.username or parsed.password:
        return ""
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return ""
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _public_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:100]:
            key = str(raw_key)
            lowered = key.lower()
            if any(
                sensitive in lowered
                for sensitive in (
                    "password",
                    "secret",
                    "token",
                    "credential",
                    "source_path",
                    "root_path",
                    "content_ref",
                    "direct_url",
                    "content_url",
                )
            ):
                continue
            projected = _public_metadata(item, depth=depth + 1)
            if projected is not None:
                result[key] = projected
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in list(value)[:100]:
            projected = _public_metadata(item, depth=depth + 1)
            if projected is not None:
                result.append(projected)
        return result
    if isinstance(value, str):
        token = value.strip()
        if token.startswith(("http://", "https://")):
            return _public_direct_url(token) or None
        if token.startswith(("/", "\\", "file://")) or (len(token) > 2 and token[1:3] in {":\\", ":/"}):
            return None
        return token[:500]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:500]


def _metadata_matches_root(metadata: Any, root: Mapping[str, Any]) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    root_id = _text(root.get("id"))
    metadata_root_id = _text(metadata.get("media_center_root_id"))
    if root_id and metadata_root_id == root_id:
        return True
    root_path = _text(root.get("path"))
    metadata_root_path = _text(metadata.get("media_center_root_path"))
    if not root_path or not metadata_root_path:
        return False
    return os.path.normcase(os.path.normpath(metadata_root_path)) == os.path.normcase(os.path.normpath(root_path))


def _item_id(source: str, resource_id: str) -> str:
    digest = hashlib.sha256(f"{source}\0{resource_id}".encode("utf-8", errors="replace")).hexdigest()[:24]
    return f"mc_{digest}"


def _media_kind(mime_type: str, name: str) -> str:
    mime = _text(mime_type).lower()
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    suffix = Path(name).suffix.lower()
    if suffix in {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".ogv"}:
        return "video"
    if suffix in {".mp3", ".wav", ".flac", ".m4a", ".aac", ".opus", ".ogg"}:
        return "audio"
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
        return "image"
    return "other"


def _normalize_kind(value: Any) -> str:
    token = _text(value).lower()
    return token if token in {"playable", "video", "audio", "image", "other"} else ""


def _root_id(path: str) -> str:
    digest = hashlib.sha256(_text(path).encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"root_{digest}"


def _normalize_source(value: Any) -> str:
    token = _text(value).lower()
    if token in {"", "all"}:
        return token
    if token in {"media", "media_store", "mediaserver"}:
        return "media_server"
    if token in {"indexer", "media_indexer_skill"}:
        return "media_indexer"
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in token).strip("_")


def _title_from_name(name: str) -> str:
    stem = Path(name).stem if Path(name).suffix else name
    text = stem.replace("_", " ").replace(".", " ").replace("-", " ")
    return " ".join(part for part in text.split() if part).strip() or name


def _kind_icon(kind: str) -> str:
    return {
        "video": "videocam-outline",
        "audio": "musical-notes-outline",
        "image": "image-outline",
    }.get(kind, "document-outline")


def _public_root(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "schema": SCHEMA_VERSION,
        "path": str(row["path"]),
        "label": str(row["label"] or row["path"]),
        "enabled": bool(row["enabled"]),
        "include_images": bool(row["include_images"]),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "last_scan_at": str(row["last_scan_at"] or ""),
        "last_status": str(row["last_status"] or ""),
    }


def _item_markdown(item: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            f"# {_text(item.get('title')) or _text(item.get('name'))}",
            "",
            f"- Kind: `{_text(item.get('media_kind'))}`",
            f"- Source: `{_text(item.get('source'))}`",
            f"- MIME: `{_text(item.get('mime_type'))}`",
            f"- Size: `{_format_bytes(_int(item.get('size_bytes')))}`",
            f"- Modified: `{_text(item.get('modified_at')) or 'unknown'}`",
            f"- Playback path: `{_text(item.get('routed_content_path')) or _text(item.get('content_path')) or 'missing'}`",
        ]
    )


def _format_bytes(value: int) -> str:
    size = float(max(0, int(value or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{int(value)} B"


def _label(value: str) -> str:
    token = _text(value).replace("_", " ").replace("-", " ")
    return token[:1].upper() + token[1:] if token else ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_loads(value: Any) -> Any:
    try:
        return json.loads(str(value or "null"))
    except Exception:
        return None
