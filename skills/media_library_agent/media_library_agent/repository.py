from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import (
    DELTA_SCHEMA,
    JOB_SCHEMA,
    ROOT_SCHEMA,
    SCHEMA_VERSION,
    decode_cursor,
    encode_cursor,
    json_dumps,
    json_loads,
    now_iso,
    stable_id,
    text,
)


ACTIVE_JOB_STATUSES = ("queued", "running", "waiting_resources", "canceling")
TERMINAL_JOB_STATUSES = ("completed", "failed", "canceled")


def default_db_path() -> Path:
    override = text(os.environ.get("MEDIA_LIBRARY_AGENT_DB_PATH"))
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
    return db_dir / "media_library_agent.sqlite3"


class MediaLibraryAgentRepository:
    def __init__(self, db_path: str | Path | None = None, *, node_id: str = ""):
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.node_id = text(node_id or os.environ.get("ADAOS_NODE_ID") or os.environ.get("ADAOS_HUB_ID")) or "local"
        self.ensure_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def ensure_schema(self) -> dict[str, Any]:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS roots (
                    id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    include_images INTEGER NOT NULL DEFAULT 0,
                    follow_symlinks INTEGER NOT NULL DEFAULT 0,
                    exclusions_json TEXT NOT NULL DEFAULT '[]',
                    scan_window_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_scan_at TEXT NOT NULL DEFAULT '',
                    last_status TEXT NOT NULL DEFAULT 'never_scanned',
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS scan_jobs (
                    id TEXT PRIMARY KEY,
                    root_id TEXT NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    discovered_count INTEGER NOT NULL DEFAULT 0,
                    processed_count INTEGER NOT NULL DEFAULT 0,
                    added_count INTEGER NOT NULL DEFAULT 0,
                    updated_count INTEGER NOT NULL DEFAULT 0,
                    removed_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    processed_bytes INTEGER NOT NULL DEFAULT 0,
                    current_path TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_detail TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    webspace_id TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_media_agent_jobs_status ON scan_jobs(status, requested_at);
                CREATE INDEX IF NOT EXISTS idx_media_agent_jobs_root ON scan_jobs(root_id, requested_at DESC);
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    root_id TEXT NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
                    node_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    folder_path TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    media_kind TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    modified_ns INTEGER NOT NULL DEFAULT 0,
                    inode INTEGER NOT NULL DEFAULT 0,
                    fingerprint TEXT NOT NULL,
                    resource_id TEXT NOT NULL DEFAULT '',
                    descriptor_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    present INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(root_id, relative_path)
                );
                CREATE INDEX IF NOT EXISTS idx_media_agent_sources_root ON sources(root_id, present, relative_path);
                CREATE INDEX IF NOT EXISTS idx_media_agent_sources_kind ON sources(media_kind, present);
                CREATE TABLE IF NOT EXISTS source_deltas (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    schema_name TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    root_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    source_revision INTEGER NOT NULL,
                    job_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_media_agent_deltas_root ON source_deltas(root_id, sequence);
                CREATE TABLE IF NOT EXISTS schedules (
                    root_id TEXT PRIMARY KEY REFERENCES roots(id) ON DELETE CASCADE,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    interval_seconds INTEGER NOT NULL DEFAULT 21600,
                    debounce_seconds INTEGER NOT NULL DEFAULT 30,
                    next_run_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS topology_phase_receipts (
                    idempotency_key TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_media_agent_topology_operation
                    ON topology_phase_receipts(operation_id, phase);
                """
            )
            connection.execute("INSERT OR REPLACE INTO agent_meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            connection.commit()
        return {"ok": True, "schema": SCHEMA_VERSION, "db_path": str(self.db_path), "node_id": self.node_id}

    @property
    def agent_id(self) -> str:
        return stable_id("agent", self.node_id, size=20)

    def add_root(
        self,
        path: str,
        *,
        label: str = "",
        include_images: bool = False,
        follow_symlinks: bool = False,
        exclusions: Iterable[str] = (),
        scan_window: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = text(path)
        if not token:
            return {"ok": False, "error": "root_path_required", "schema": SCHEMA_VERSION}
        try:
            resolved = Path(token).expanduser().resolve(strict=True)
        except Exception:
            return {"ok": False, "error": "root_path_not_found", "path": token, "schema": SCHEMA_VERSION}
        if not resolved.is_dir():
            return {"ok": False, "error": "root_path_not_directory", "path": str(resolved), "schema": SCHEMA_VERSION}
        patterns = [text(item) for item in exclusions if text(item)][:64]
        if any(len(item) > 300 for item in patterns):
            return {"ok": False, "error": "root_exclusion_invalid", "schema": SCHEMA_VERSION}
        now = now_iso()
        root_id = stable_id("root", self.node_id, str(resolved), size=20)
        root_label = text(label) or resolved.name or str(resolved)
        with self.connect() as connection:
            existing = connection.execute("SELECT * FROM roots WHERE path = ?", (str(resolved),)).fetchone()
            revision = int(existing["revision"] or 0) + 1 if existing else 1
            created_at = str(existing["created_at"]) if existing else now
            connection.execute(
                """
                INSERT INTO roots (
                    id, node_id, path, label, enabled, include_images, follow_symlinks,
                    exclusions_json, scan_window_json, created_at, updated_at, revision
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    label=excluded.label, enabled=1, include_images=excluded.include_images,
                    follow_symlinks=excluded.follow_symlinks, exclusions_json=excluded.exclusions_json,
                    scan_window_json=excluded.scan_window_json, updated_at=excluded.updated_at,
                    revision=excluded.revision
                """,
                (
                    root_id,
                    self.node_id,
                    str(resolved),
                    root_label,
                    int(include_images),
                    int(follow_symlinks),
                    json_dumps(patterns),
                    json_dumps(dict(scan_window or {})),
                    created_at,
                    now,
                    revision,
                ),
            )
            connection.commit()
        return {"ok": True, "schema": SCHEMA_VERSION, "root": self.get_root(root_id), "roots": self.list_roots()["items"]}

    def get_root(self, root_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM roots WHERE id = ?", (text(root_id),)).fetchone()
        return self._public_root(row) if row else None

    def list_roots(self, *, include_disabled: bool = False) -> dict[str, Any]:
        where = "" if include_disabled else "WHERE enabled = 1"
        with self.connect() as connection:
            rows = connection.execute(f"SELECT * FROM roots {where} ORDER BY lower(label), path").fetchall()
        items = [self._public_root(row) for row in rows]
        return {"ok": True, "schema": SCHEMA_VERSION, "items": items, "count": len(items), "node_id": self.node_id}

    def disable_root(self, root_id: str) -> dict[str, Any]:
        token = text(root_id)
        with self.connect() as connection:
            changed = connection.execute(
                "UPDATE roots SET enabled=0, updated_at=?, revision=revision+1 WHERE id=?",
                (now_iso(), token),
            ).rowcount
            connection.commit()
        if not changed:
            return {"ok": False, "error": "root_not_found", "root_id": token, "schema": SCHEMA_VERSION}
        return {"ok": True, "schema": SCHEMA_VERSION, "root": self.get_root(token), "roots": self.list_roots()["items"]}

    def active_job_for_root(self, root_id: str) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT * FROM scan_jobs WHERE root_id=? AND status IN ({placeholders}) ORDER BY requested_at LIMIT 1",
                (text(root_id), *ACTIVE_JOB_STATUSES),
            ).fetchone()
        return self._public_job(row) if row else None

    def create_job(self, root_id: str, *, mode: str = "incremental", webspace_id: str = "") -> dict[str, Any]:
        root = self.get_root(root_id)
        if root is None:
            return {"ok": False, "error": "root_not_found", "root_id": text(root_id), "schema": SCHEMA_VERSION}
        if not root["enabled"]:
            return {"ok": False, "error": "root_disabled", "root_id": root["id"], "schema": SCHEMA_VERSION}
        active = self.active_job_for_root(root["id"])
        if active:
            return {"ok": True, "schema": SCHEMA_VERSION, "job": active, "accepted": False, "deduplicated": True}
        requested_at = now_iso()
        job_id = stable_id("scan", root["id"], requested_at, size=24)
        mode_token = text(mode).lower()
        if mode_token not in {"incremental", "reconcile", "full"}:
            mode_token = "incremental"
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO scan_jobs(id, root_id, mode, status, requested_at, webspace_id) VALUES (?, ?, ?, 'queued', ?, ?)",
                (job_id, root["id"], mode_token, requested_at, text(webspace_id)),
            )
            connection.commit()
        return {"ok": True, "schema": SCHEMA_VERSION, "job": self.get_job(job_id), "accepted": True, "deduplicated": False}

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM scan_jobs WHERE id=?", (text(job_id),)).fetchone()
        return self._public_job(row) if row else None

    def next_queued_job(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM scan_jobs WHERE status='queued' ORDER BY requested_at LIMIT 1").fetchone()
        return self._public_job(row) if row else None

    def claim_job(self, job_id: str) -> dict[str, Any] | None:
        token = text(job_id)
        started_at = now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE scan_jobs SET status='running', started_at=?, finished_at='',
                    error_code='', error_detail='', revision=revision+1
                WHERE id=? AND status='queued'
                """,
                (started_at, token),
            ).rowcount
            connection.commit()
        return self.get_job(token) if changed else None

    def requeue_interrupted_jobs(self) -> int:
        with self.connect() as connection:
            count = connection.execute(
                """
                UPDATE scan_jobs SET status='queued', started_at='', finished_at='',
                    error_code='', error_detail='', revision=revision+1
                WHERE status IN ('running', 'waiting_resources', 'canceling')
                """
            ).rowcount
            connection.commit()
        return max(0, int(count or 0))

    def update_job(self, job_id: str, *, status: str | None = None, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "started_at", "finished_at", "discovered_count", "processed_count", "added_count",
            "updated_count", "removed_count", "skipped_count", "error_count", "processed_bytes",
            "current_path", "error_code", "error_detail", "cancel_requested",
        }
        updates: list[str] = []
        values: list[Any] = []
        if status is not None:
            updates.append("status=?")
            values.append(text(status))
        for key, value in fields.items():
            if key not in allowed:
                continue
            updates.append(f"{key}=?")
            values.append(value)
        if not updates:
            return self.get_job(job_id)
        updates.append("revision=revision+1")
        values.append(text(job_id))
        with self.connect() as connection:
            connection.execute(f"UPDATE scan_jobs SET {', '.join(updates)} WHERE id=?", tuple(values))
            connection.commit()
        return self.get_job(job_id)

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        token = text(job_id)
        job = self.get_job(token)
        if job is None:
            return {"ok": False, "error": "scan_job_not_found", "job_id": token, "schema": SCHEMA_VERSION}
        if job["status"] in TERMINAL_JOB_STATUSES:
            return {"ok": True, "schema": SCHEMA_VERSION, "job": job, "changed": False}
        updated = self.update_job(token, status="canceling", cancel_requested=1)
        return {"ok": True, "schema": SCHEMA_VERSION, "job": updated, "changed": True}

    def list_jobs(self, *, limit: int = 20, root_id: str = "") -> dict[str, Any]:
        bounded = max(1, min(100, int(limit or 20)))
        params: list[Any] = []
        where = ""
        if text(root_id):
            where = "WHERE root_id=?"
            params.append(text(root_id))
        params.append(bounded)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM scan_jobs {where} ORDER BY requested_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return {"ok": True, "schema": SCHEMA_VERSION, "items": [self._public_job(row) for row in rows], "count": len(rows)}

    def source_by_path(self, root_id: str, relative_path: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE root_id=? AND relative_path=?",
                (text(root_id), text(relative_path)),
            ).fetchone()
        return self._public_source(row) if row else None

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM sources WHERE id=?", (text(source_id),)).fetchone()
        return self._public_source(row) if row else None

    def upsert_source(self, source: Mapping[str, Any], *, job_id: str) -> tuple[str, dict[str, Any]]:
        root_id = text(source.get("root_id"))
        relative_path = text(source.get("relative_path")).replace("\\", "/")
        now = now_iso()
        with self.connect() as connection:
            previous = connection.execute(
                "SELECT * FROM sources WHERE root_id=? AND relative_path=?",
                (root_id, relative_path),
            ).fetchone()
            operation = "added"
            if previous is not None:
                operation = "restored" if not bool(previous["present"]) else "updated"
                if str(previous["fingerprint"]) == text(source.get("fingerprint")) and bool(previous["present"]):
                    operation = "unchanged"
            revision = int(previous["revision"] or 0) + (0 if operation == "unchanged" else 1) if previous else 1
            source_id = str(previous["id"]) if previous else stable_id("source", self.node_id, root_id, relative_path, size=28)
            first_seen_at = str(previous["first_seen_at"]) if previous else now
            descriptor = dict(source.get("descriptor") or {})
            metadata = dict(source.get("metadata") or {})
            connection.execute(
                """
                INSERT INTO sources (
                    id, root_id, node_id, relative_path, folder_path, name, media_kind,
                    mime_type, size_bytes, modified_ns, inode, fingerprint, resource_id,
                    descriptor_json, metadata_json, present, first_seen_at, last_seen_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(root_id, relative_path) DO UPDATE SET
                    folder_path=excluded.folder_path, name=excluded.name, media_kind=excluded.media_kind,
                    mime_type=excluded.mime_type, size_bytes=excluded.size_bytes,
                    modified_ns=excluded.modified_ns, inode=excluded.inode,
                    fingerprint=excluded.fingerprint, resource_id=excluded.resource_id,
                    descriptor_json=excluded.descriptor_json, metadata_json=excluded.metadata_json,
                    present=1, last_seen_at=excluded.last_seen_at, revision=excluded.revision
                """,
                (
                    source_id, root_id, self.node_id, relative_path, text(source.get("folder_path")),
                    text(source.get("name")), text(source.get("media_kind")), text(source.get("mime_type")),
                    int(source.get("size_bytes") or 0), int(source.get("modified_ns") or 0),
                    int(source.get("inode") or 0), text(source.get("fingerprint")), text(source.get("resource_id")),
                    json_dumps(descriptor), json_dumps(metadata), first_seen_at, now, revision,
                ),
            )
            row = connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            public = self._public_source(row)
            if operation != "unchanged":
                self._insert_delta(connection, operation, public, job_id=job_id)
            connection.commit()
        return operation, public

    def mark_missing(self, root_id: str, *, seen_relative_paths: set[str], job_id: str) -> list[dict[str, Any]]:
        removed: list[dict[str, Any]] = []
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM sources WHERE root_id=? AND present=1", (text(root_id),)).fetchall()
            for row in rows:
                if str(row["relative_path"]) in seen_relative_paths:
                    continue
                revision = int(row["revision"] or 0) + 1
                connection.execute(
                    "UPDATE sources SET present=0, last_seen_at=?, revision=? WHERE id=?",
                    (now_iso(), revision, str(row["id"])),
                )
                changed = connection.execute("SELECT * FROM sources WHERE id=?", (str(row["id"]),)).fetchone()
                public = self._public_source(changed)
                self._insert_delta(connection, "removed", public, job_id=job_id)
                removed.append(public)
            connection.commit()
        return removed

    def _insert_delta(self, connection: sqlite3.Connection, operation: str, source: Mapping[str, Any], *, job_id: str) -> None:
        created_at = now_iso()
        delta_id = stable_id("delta", source.get("id"), source.get("revision"), operation, job_id, size=28)
        connection.execute(
            """
            INSERT OR IGNORE INTO source_deltas(
                id, schema_name, agent_id, node_id, root_id, source_id, operation,
                source_revision, job_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delta_id, DELTA_SCHEMA, self.agent_id, self.node_id, text(source.get("root_id")),
                text(source.get("id")), operation, int(source.get("revision") or 0), text(job_id),
                json_dumps(dict(source)), created_at,
            ),
        )

    def pull_deltas(self, *, cursor: str = "", limit: int = 250, root_id: str = "") -> dict[str, Any]:
        after = decode_cursor(cursor)
        bounded = max(1, min(1000, int(limit or 250)))
        params: list[Any] = [after]
        root_clause = ""
        if text(root_id):
            root_clause = "AND root_id=?"
            params.append(text(root_id))
        params.append(bounded + 1)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM source_deltas WHERE sequence>? {root_clause} ORDER BY sequence LIMIT ?",
                tuple(params),
            ).fetchall()
        has_more = len(rows) > bounded
        visible = rows[:bounded]
        items = [self._public_delta(row) for row in visible]
        next_sequence = int(visible[-1]["sequence"]) if visible else after
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "items": items,
            "count": len(items),
            "cursor": encode_cursor(after),
            "next_cursor": encode_cursor(next_sequence),
            "has_more": has_more,
            "agent": {"id": self.agent_id, "node_id": self.node_id},
        }

    def browse_folders(self, *, root_id: str = "", parent: str = "", limit: int = 100) -> dict[str, Any]:
        bounded = max(1, min(500, int(limit or 100)))
        root_token = text(root_id)
        parent_token = text(parent).replace("\\", "/").strip("/")
        params: list[Any] = []
        clauses = ["present=1"]
        if root_token:
            clauses.append("root_id=?")
            params.append(root_token)
        params.append(bounded * 20)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT root_id, folder_path FROM sources WHERE {' AND '.join(clauses)} ORDER BY folder_path LIMIT ?",
                tuple(params),
            ).fetchall()
        folders: dict[tuple[str, str], dict[str, Any]] = {}
        prefix = parent_token + "/" if parent_token else ""
        for row in rows:
            folder = str(row["folder_path"] or "").strip("/")
            if parent_token and folder != parent_token and not folder.startswith(prefix):
                continue
            remainder = folder[len(prefix):] if folder.startswith(prefix) else folder
            child = remainder.split("/", 1)[0] if remainder else ""
            child_path = "/".join(item for item in (parent_token, child) if item)
            key = (str(row["root_id"]), child_path)
            item = folders.setdefault(key, {"root_id": key[0], "path": child_path, "name": child or Path(self.get_root(key[0])["path"]).name, "source_count": 0})
            item["source_count"] += 1
        items = list(folders.values())[:bounded]
        return {"ok": True, "schema": SCHEMA_VERSION, "items": items, "count": len(items), "parent": parent_token}

    def configure_schedule(self, root_id: str, *, enabled: bool, interval_seconds: int = 21600, debounce_seconds: int = 30) -> dict[str, Any]:
        if self.get_root(root_id) is None:
            return {"ok": False, "error": "root_not_found", "root_id": text(root_id), "schema": SCHEMA_VERSION}
        interval = max(300, min(604800, int(interval_seconds or 21600)))
        debounce = max(1, min(3600, int(debounce_seconds or 30)))
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO schedules(root_id, enabled, interval_seconds, debounce_seconds, next_run_at, updated_at)
                VALUES (?, ?, ?, ?, '', ?)
                ON CONFLICT(root_id) DO UPDATE SET enabled=excluded.enabled,
                    interval_seconds=excluded.interval_seconds, debounce_seconds=excluded.debounce_seconds,
                    updated_at=excluded.updated_at
                """,
                (text(root_id), int(enabled), interval, debounce, now_iso()),
            )
            connection.commit()
        return {"ok": True, "schema": SCHEMA_VERSION, "schedule": self.get_schedule(root_id)}

    def get_schedule(self, root_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM schedules WHERE root_id=?", (text(root_id),)).fetchone()
        return dict(row) | {"enabled": bool(row["enabled"])} if row else None

    def due_schedules(self, *, now: str | None = None) -> list[dict[str, Any]]:
        moment = text(now) or now_iso()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM schedules WHERE enabled=1 AND (next_run_at='' OR next_run_at<=?) ORDER BY next_run_at",
                (moment,),
            ).fetchall()
        return [dict(row) | {"enabled": True} for row in rows]

    def advance_schedule(self, root_id: str, next_run_at: str) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE schedules SET next_run_at=?, updated_at=? WHERE root_id=?", (text(next_run_at), now_iso(), text(root_id)))
            connection.commit()

    def mark_root_scan(self, root_id: str, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE roots SET last_scan_at=?, last_status=?, updated_at=?, revision=revision+1 WHERE id=?",
                (now_iso(), text(status), now_iso(), text(root_id)),
            )
            connection.commit()

    def summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            roots = connection.execute("SELECT COUNT(*) AS count FROM roots WHERE enabled=1").fetchone()
            sources = connection.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN present=1 THEN 1 ELSE 0 END) AS available, SUM(CASE WHEN present=1 THEN size_bytes ELSE 0 END) AS bytes FROM sources"
            ).fetchone()
            active = connection.execute("SELECT COUNT(*) AS count FROM scan_jobs WHERE status IN ('queued','running','waiting_resources','canceling')").fetchone()
            errors = connection.execute("SELECT COUNT(*) AS count FROM scan_jobs WHERE status='failed'").fetchone()
            delta = connection.execute("SELECT MAX(sequence) AS sequence FROM source_deltas").fetchone()
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "agent": {"id": self.agent_id, "node_id": self.node_id},
            "root_count": int(roots["count"] or 0),
            "source_count": int(sources["total"] or 0),
            "available_count": int(sources["available"] or 0),
            "available_bytes": int(sources["bytes"] or 0),
            "active_job_count": int(active["count"] or 0),
            "failed_job_count": int(errors["count"] or 0),
            "delta_cursor": encode_cursor(int(delta["sequence"] or 0)),
            "storage": {"mode": "external_reference", "media_bytes_copied": False},
        }

    def topology_root_witness(self, root_id: str) -> dict[str, Any] | None:
        root = self.get_root(root_id)
        if root is None:
            return None
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN present=1 THEN 1 ELSE 0 END) AS available,
                    SUM(CASE WHEN present=1 THEN size_bytes ELSE 0 END) AS bytes,
                    MAX(revision) AS source_revision,
                    MAX(last_seen_at) AS observed_at
                FROM sources WHERE root_id=?
                """,
                (text(root_id),),
            ).fetchone()
            delta = connection.execute(
                "SELECT MAX(sequence) AS sequence FROM source_deltas WHERE root_id=?",
                (text(root_id),),
            ).fetchone()
        source_revision = int(row["source_revision"] or 0)
        sequence = int(delta["sequence"] or 0)
        manifest = {
            "root_id": root["id"],
            "root_revision": int(root["revision"]),
            "source_revision": source_revision,
            "delta_sequence": sequence,
            "total": int(row["total"] or 0),
            "available": int(row["available"] or 0),
            "bytes": int(row["bytes"] or 0),
            "observed_at": text(row["observed_at"]),
        }
        import hashlib

        return {
            **manifest,
            "checkpoint": f"root:{root['revision']}:source:{source_revision}:delta:{sequence}",
            "content_witness": "sha256:"
            + hashlib.sha256(json_dumps(manifest).encode("utf-8")).hexdigest(),
        }

    def topology_phase_receipt(self, idempotency_key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM topology_phase_receipts WHERE idempotency_key=?",
                (text(idempotency_key),),
            ).fetchone()
        if row is None:
            return None
        return {
            "request_digest": str(row["request_digest"]),
            "operation_id": str(row["operation_id"]),
            "phase": str(row["phase"]),
            "result": json_loads(row["result_json"], {}),
            "created_at": str(row["created_at"]),
        }

    def save_topology_phase_receipt(
        self,
        *,
        idempotency_key: str,
        request_digest: str,
        operation_id: str,
        phase: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO topology_phase_receipts(
                    idempotency_key, request_digest, operation_id, phase,
                    result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    text(idempotency_key),
                    text(request_digest),
                    text(operation_id),
                    text(phase),
                    json_dumps(dict(result)),
                    now_iso(),
                ),
            )
            connection.commit()
        saved = self.topology_phase_receipt(idempotency_key)
        if saved is None:
            raise RuntimeError("topology_phase_receipt_not_saved")
        if saved["request_digest"] != request_digest:
            raise RuntimeError("topology_phase_idempotency_conflict")
        return dict(saved["result"])

    def _public_root(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": ROOT_SCHEMA,
            "id": str(row["id"]),
            "node_id": str(row["node_id"]),
            "path": str(row["path"]),
            "label": str(row["label"]),
            "enabled": bool(row["enabled"]),
            "include_images": bool(row["include_images"]),
            "follow_symlinks": bool(row["follow_symlinks"]),
            "exclusions": json_loads(row["exclusions_json"], []),
            "scan_window": json_loads(row["scan_window_json"], {}),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "last_scan_at": str(row["last_scan_at"]),
            "last_status": str(row["last_status"]),
            "revision": int(row["revision"]),
        }

    def _public_job(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": JOB_SCHEMA,
            "id": str(row["id"]),
            "root_id": str(row["root_id"]),
            "mode": str(row["mode"]),
            "status": str(row["status"]),
            "requested_at": str(row["requested_at"]),
            "started_at": str(row["started_at"]),
            "finished_at": str(row["finished_at"]),
            "progress": {
                "discovered_count": int(row["discovered_count"]),
                "processed_count": int(row["processed_count"]),
                "added_count": int(row["added_count"]),
                "updated_count": int(row["updated_count"]),
                "removed_count": int(row["removed_count"]),
                "skipped_count": int(row["skipped_count"]),
                "error_count": int(row["error_count"]),
                "processed_bytes": int(row["processed_bytes"]),
                "current_path": str(row["current_path"]),
            },
            "error": {"code": str(row["error_code"]), "detail": str(row["error_detail"])} if row["error_code"] else None,
            "cancel_requested": bool(row["cancel_requested"]),
            "webspace_id": str(row["webspace_id"]),
            "revision": int(row["revision"]),
        }

    def _public_source(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": "adaos.media_library.source.v1",
            "id": str(row["id"]),
            "root_id": str(row["root_id"]),
            "node_id": str(row["node_id"]),
            "relative_path": str(row["relative_path"]),
            "folder_path": str(row["folder_path"]),
            "name": str(row["name"]),
            "media_kind": str(row["media_kind"]),
            "mime_type": str(row["mime_type"]),
            "size_bytes": int(row["size_bytes"]),
            "modified_ns": int(row["modified_ns"]),
            "inode": int(row["inode"]),
            "fingerprint": str(row["fingerprint"]),
            "resource_id": str(row["resource_id"]),
            "descriptor": json_loads(row["descriptor_json"], {}),
            "metadata": json_loads(row["metadata_json"], {}),
            "present": bool(row["present"]),
            "first_seen_at": str(row["first_seen_at"]),
            "last_seen_at": str(row["last_seen_at"]),
            "revision": int(row["revision"]),
        }

    def _public_delta(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": DELTA_SCHEMA,
            "id": str(row["id"]),
            "sequence": int(row["sequence"]),
            "agent_id": str(row["agent_id"]),
            "node_id": str(row["node_id"]),
            "root_id": str(row["root_id"]),
            "source_id": str(row["source_id"]),
            "operation": str(row["operation"]),
            "source_revision": int(row["source_revision"]),
            "job_id": str(row["job_id"]),
            "source": json_loads(row["payload_json"], {}),
            "created_at": str(row["created_at"]),
        }
