from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import (
    DELTA_SCHEMA,
    JOB_SCHEMA,
    ROOT_SCHEMA,
    RENDITION_JOB_SCHEMA,
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
_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


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
                CREATE VIRTUAL TABLE IF NOT EXISTS source_search USING fts5(
                    source_id UNINDEXED,
                    text,
                    tokenize='unicode61 remove_diacritics 2'
                );
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
                    watch_enabled INTEGER NOT NULL DEFAULT 0,
                    watch_poll_seconds INTEGER NOT NULL DEFAULT 30,
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
                CREATE TABLE IF NOT EXISTS rendition_jobs (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    root_id TEXT NOT NULL,
                    source_revision INTEGER NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    media_kind TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 50,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    output_bytes INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_detail TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    cleaned_at TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_media_agent_rendition_queue
                    ON rendition_jobs(status, priority, requested_at);
                CREATE INDEX IF NOT EXISTS idx_media_agent_rendition_source
                    ON rendition_jobs(source_id, source_fingerprint, profile);
                """
            )
            schedule_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(schedules)"
                ).fetchall()
            }
            for name, definition in {
                "watch_enabled": "INTEGER NOT NULL DEFAULT 0",
                "watch_poll_seconds": "INTEGER NOT NULL DEFAULT 30",
            }.items():
                if name not in schedule_columns:
                    connection.execute(
                        f"ALTER TABLE schedules ADD COLUMN {name} {definition}"
                    )
            connection.execute("INSERT OR REPLACE INTO agent_meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            search_count = int(
                connection.execute("SELECT COUNT(*) FROM source_search").fetchone()[0]
            )
            source_count = int(
                connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            )
            if search_count != source_count:
                connection.execute("DELETE FROM source_search")
                for source_row in connection.execute(
                    "SELECT id,name,relative_path,folder_path,metadata_json,descriptor_json FROM sources"
                ).fetchall():
                    connection.execute(
                        "INSERT INTO source_search(source_id,text) VALUES (?,?)",
                        (
                            str(source_row["id"]),
                            " ".join(
                                str(source_row[key] or "")
                                for key in (
                                    "name",
                                    "relative_path",
                                    "folder_path",
                                    "metadata_json",
                                    "descriptor_json",
                                )
                            ),
                        ),
                    )
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
        for existing in self.list_roots(include_disabled=False)["items"]:
            existing_path = Path(str(existing["path"]))
            if existing_path == resolved:
                continue
            try:
                overlaps = resolved.is_relative_to(existing_path) or existing_path.is_relative_to(resolved)
            except (OSError, ValueError):
                overlaps = False
            if overlaps:
                return {
                    "ok": False,
                    "error": "root_path_overlap",
                    "schema": SCHEMA_VERSION,
                    "path": str(resolved),
                    "overlap": {
                        "root_id": str(existing["id"]),
                        "path": str(existing_path),
                    },
                }
        patterns = [text(item) for item in exclusions if text(item)][:64]
        if any(len(item) > 300 for item in patterns):
            return {"ok": False, "error": "root_exclusion_invalid", "schema": SCHEMA_VERSION}
        window = dict(scan_window or {})
        if window:
            unknown = set(window).difference({"start", "end", "days"})
            days = window.get("days", [])
            valid_days = (
                isinstance(days, list)
                and len(days) <= 7
                and all(isinstance(item, int) and 0 <= item <= 6 for item in days)
            )
            if (
                unknown
                or not _CLOCK.fullmatch(text(window.get("start")))
                or not _CLOCK.fullmatch(text(window.get("end")))
                or not valid_days
            ):
                return {
                    "ok": False,
                    "error": "root_scan_window_invalid",
                    "schema": SCHEMA_VERSION,
                }
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
                    json_dumps(window),
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
            count += connection.execute(
                """
                UPDATE rendition_jobs SET status='queued', started_at='',
                    finished_at='', error_code='', error_detail='',
                    revision=revision+1
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
        updated = self.update_job(
            token,
            status="canceled" if job["status"] == "queued" else "canceling",
            cancel_requested=1,
            finished_at=now_iso() if job["status"] == "queued" else "",
        )
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

    def create_rendition_job(
        self,
        source_id: str,
        *,
        profile: str,
        target: Mapping[str, Any],
        priority: int = 50,
        force: bool = False,
    ) -> dict[str, Any]:
        source = self.get_source(source_id)
        if source is None or not source.get("present"):
            return {
                "ok": False,
                "error": "source_not_found",
                "source_id": text(source_id),
                "schema": SCHEMA_VERSION,
            }
        profile_token = text(profile) or "browser-mp4-v1"
        target_json = json_dumps(dict(target))
        with self.connect() as connection:
            if not force:
                existing = connection.execute(
                    """
                    SELECT * FROM rendition_jobs
                    WHERE source_id=? AND source_fingerprint=? AND profile=?
                        AND target_json=?
                        AND status IN ('queued','running','waiting_resources','canceling','completed')
                    ORDER BY requested_at DESC LIMIT 1
                    """,
                    (
                        text(source_id),
                        text(source.get("fingerprint")),
                        profile_token,
                        target_json,
                    ),
                ).fetchone()
                if existing:
                    return {
                        "ok": True,
                        "schema": SCHEMA_VERSION,
                        "created": False,
                        "job": self._public_rendition_job(existing),
                    }
            requested_at = now_iso()
            job_id = stable_id(
                "renditionjob",
                source_id,
                source.get("revision"),
                source.get("fingerprint"),
                profile_token,
                target_json,
                requested_at if force else "",
                size=28,
            )
            connection.execute(
                """
                INSERT INTO rendition_jobs(
                    id,source_id,root_id,source_revision,source_fingerprint,
                    media_kind,profile,target_json,priority,status,requested_at
                ) VALUES (?,?,?,?,?,?,?,?,?,'queued',?)
                """,
                (
                    job_id,
                    text(source_id),
                    text(source.get("root_id")),
                    int(source.get("revision") or 0),
                    text(source.get("fingerprint")),
                    text(source.get("media_kind")),
                    profile_token,
                    target_json,
                    max(0, min(1000, int(priority or 50))),
                    requested_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM rendition_jobs WHERE id=?", (job_id,)
            ).fetchone()
            connection.commit()
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "created": True,
            "job": self._public_rendition_job(row),
        }

    def get_rendition_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM rendition_jobs WHERE id=?", (text(job_id),)
            ).fetchone()
        return self._public_rendition_job(row) if row else None

    def next_queued_rendition_job(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM rendition_jobs WHERE status='queued'
                ORDER BY priority,requested_at LIMIT 1
                """
            ).fetchone()
        return self._public_rendition_job(row) if row else None

    def claim_rendition_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            changed = connection.execute(
                """
                UPDATE rendition_jobs SET status='running',started_at=?,
                    finished_at='',error_code='',error_detail='',revision=revision+1
                WHERE id=? AND status='queued'
                """,
                (now_iso(), text(job_id)),
            ).rowcount
            row = connection.execute(
                "SELECT * FROM rendition_jobs WHERE id=?", (text(job_id),)
            ).fetchone()
            connection.commit()
        return self._public_rendition_job(row) if changed and row else None

    def update_rendition_job(
        self, job_id: str, *, status: str | None = None, **fields: Any
    ) -> dict[str, Any] | None:
        allowed = {
            "started_at",
            "finished_at",
            "output_json",
            "output_bytes",
            "error_code",
            "error_detail",
            "cancel_requested",
            "cleaned_at",
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
            return self.get_rendition_job(job_id)
        updates.append("revision=revision+1")
        values.append(text(job_id))
        with self.connect() as connection:
            connection.execute(
                f"UPDATE rendition_jobs SET {', '.join(updates)} WHERE id=?",
                tuple(values),
            )
            connection.commit()
        return self.get_rendition_job(job_id)

    def request_rendition_cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get_rendition_job(job_id)
        if job is None:
            return {
                "ok": False,
                "error": "rendition_job_not_found",
                "job_id": text(job_id),
                "schema": SCHEMA_VERSION,
            }
        if job["status"] in {"completed", "failed", "canceled", "invalidated"}:
            return {"ok": True, "schema": SCHEMA_VERSION, "job": job, "changed": False}
        updated = self.update_rendition_job(
            job_id,
            status="canceled" if job["status"] == "queued" else "canceling",
            cancel_requested=1,
            finished_at=now_iso() if job["status"] == "queued" else "",
            error_code="rendition_canceled" if job["status"] == "queued" else "",
        )
        return {"ok": True, "schema": SCHEMA_VERSION, "job": updated, "changed": True}

    def list_rendition_jobs(
        self, *, limit: int = 20, source_id: str = ""
    ) -> dict[str, Any]:
        bounded = max(1, min(100, int(limit or 20)))
        params: list[Any] = []
        where = ""
        if text(source_id):
            where = "WHERE source_id=?"
            params.append(text(source_id))
        params.append(bounded)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM rendition_jobs {where} ORDER BY requested_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "items": [self._public_rendition_job(row) for row in rows],
            "count": len(rows),
        }

    def complete_rendition_job(
        self,
        job_id: str,
        *,
        descriptor: Mapping[str, Any],
        output_bytes: int,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM rendition_jobs WHERE id=?", (text(job_id),)
            ).fetchone()
            if job is None:
                connection.rollback()
                return {"ok": False, "error": "rendition_job_not_found"}
            source = connection.execute(
                "SELECT * FROM sources WHERE id=?", (str(job["source_id"]),)
            ).fetchone()
            source_matches = bool(
                source
                and bool(source["present"])
                and int(source["revision"]) == int(job["source_revision"])
                and str(source["fingerprint"]) == str(job["source_fingerprint"])
            )
            if not source_matches:
                connection.execute(
                    """
                    UPDATE rendition_jobs SET status='invalidated',finished_at=?,
                        error_code='source_changed',error_detail='',revision=revision+1
                    WHERE id=?
                    """,
                    (now_iso(), str(job["id"])),
                )
                connection.commit()
                return {
                    "ok": False,
                    "error": "source_changed",
                    "advertised": False,
                    "cleanup_required": True,
                }
            metadata = json_loads(source["metadata_json"], {})
            if not isinstance(metadata, dict):
                metadata = {}
            renditions = [
                dict(item)
                for item in metadata.get("derived_renditions") or []
                if isinstance(item, Mapping)
                and text(item.get("profile")) != str(job["profile"])
            ]
            rendition_id = stable_id(
                "rendition",
                source["id"],
                source["fingerprint"],
                job["profile"],
                size=28,
            )
            target = json_loads(job["target_json"], {})
            rendition = {
                "id": rendition_id,
                "profile": str(job["profile"]),
                "exact_source_id": str(source["id"]),
                "exact_source_revision": int(source["revision"]),
                "exact_source_fingerprint": str(source["fingerprint"]),
                "mime_type": text(descriptor.get("mime_type") or descriptor.get("mime")),
                "descriptor": dict(descriptor),
                "quality": {
                    "codec": text(target.get("video_codec") or target.get("audio_codec")),
                    "container": text(target.get("container")),
                    "width": int(target.get("max_width") or 0),
                    "height": int(target.get("max_height") or 0),
                    "derived": True,
                },
                "size_bytes": max(0, int(output_bytes)),
                "created_at": now_iso(),
            }
            renditions.append(rendition)
            metadata["derived_renditions"] = renditions[-8:]
            next_revision = int(source["revision"]) + 1
            connection.execute(
                """
                UPDATE sources SET metadata_json=?,revision=?,last_seen_at=? WHERE id=?
                """,
                (json_dumps(metadata), next_revision, now_iso(), str(source["id"])),
            )
            connection.execute(
                """
                UPDATE rendition_jobs SET status='completed',finished_at=?,
                    output_json=?,output_bytes=?,error_code='',error_detail='',
                    revision=revision+1 WHERE id=?
                """,
                (
                    now_iso(),
                    json_dumps(dict(descriptor)),
                    max(0, int(output_bytes)),
                    str(job["id"]),
                ),
            )
            changed = connection.execute(
                "SELECT * FROM sources WHERE id=?", (str(source["id"]),)
            ).fetchone()
            public_source = self._public_source(changed)
            self._insert_delta(
                connection, "updated", public_source, job_id=str(job["id"])
            )
            connection.commit()
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "advertised": True,
            "rendition": rendition,
            "source": public_source,
            "job": self.get_rendition_job(job_id),
        }

    def invalidated_rendition_outputs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM rendition_jobs
                WHERE status='invalidated' AND cleaned_at='' AND output_json!='{}'
                ORDER BY finished_at LIMIT ?
                """,
                (max(1, min(100, int(limit or 20))),),
            ).fetchall()
        return [self._public_rendition_job(row) for row in rows]

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
                elif str(previous["fingerprint"]) != text(source.get("fingerprint")):
                    connection.execute(
                        """
                        UPDATE rendition_jobs SET status='invalidated',finished_at=?,
                            error_code='source_changed',revision=revision+1
                        WHERE source_id=? AND status='completed'
                        """,
                        (now, str(previous["id"])),
                    )
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
                connection.execute(
                    "DELETE FROM source_search WHERE source_id=?", (source_id,)
                )
                connection.execute(
                    "INSERT INTO source_search(source_id,text) VALUES (?,?)",
                    (
                        source_id,
                        " ".join(
                            (
                                text(source.get("name")),
                                relative_path,
                                text(source.get("folder_path")),
                                json_dumps(metadata),
                                json_dumps(descriptor),
                            )
                        ),
                    ),
                )
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

    def search_sources(
        self, *, query: str, limit: int = 30, cursor: str = ""
    ) -> dict[str, Any]:
        token = text(query)
        bounded = max(1, min(100, int(limit or 30)))
        offset = decode_cursor(cursor)
        if not token:
            return {
                "ok": True,
                "schema": SCHEMA_VERSION,
                "items": [],
                "count": 0,
                "next_cursor": None,
                "has_more": False,
                "agent": {"id": self.agent_id, "node_id": self.node_id},
            }
        terms = [term for term in re.findall(r"[\w]+", token, flags=re.UNICODE) if term][:12]
        if not terms:
            terms = [token]
        expression = " AND ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms
        )
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.*,bm25(source_search) AS rank
                FROM source_search JOIN sources s ON s.id=source_search.source_id
                WHERE source_search.text MATCH ? AND s.present=1
                ORDER BY rank,lower(s.name),s.id LIMIT ? OFFSET ?
                """,
                (expression, bounded + 1, offset),
            ).fetchall()
        visible = rows[:bounded]
        has_more = len(rows) > bounded
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "items": [
                self._public_source(row)
                | {"match": {"stage": "agent_technical_fts", "rank": float(row["rank"])}}
                for row in visible
            ],
            "count": len(visible),
            "next_cursor": encode_cursor(offset + len(visible)) if has_more else None,
            "has_more": has_more,
            "agent": {"id": self.agent_id, "node_id": self.node_id},
        }

    def browse_folders(
        self,
        *,
        root_id: str = "",
        parent: str = "",
        limit: int = 100,
        cursor: str = "",
    ) -> dict[str, Any]:
        bounded = max(1, min(500, int(limit or 100)))
        offset = decode_cursor(cursor)
        root_token = text(root_id)
        parent_token = text(parent).replace("\\", "/").strip("/")
        params: list[Any] = []
        clauses = ["present=1"]
        if root_token:
            clauses.append("root_id=?")
            params.append(root_token)
        if parent_token:
            clauses.append("folder_path LIKE ? ESCAPE '\\'")
            params.append(
                parent_token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "/%"
            )
            relative_start = len(parent_token) + 2
        else:
            clauses.append("folder_path<>''")
            relative_start = 1
        where = " AND ".join(clauses)
        sql = f"""
            WITH scoped AS (
                SELECT root_id, revision, substr(folder_path, ?) AS relative_path
                FROM sources WHERE {where}
            ), projected AS (
                SELECT root_id, revision,
                    CASE WHEN instr(relative_path, '/')>0
                        THEN substr(relative_path, 1, instr(relative_path, '/')-1)
                        ELSE relative_path END AS child_name
                FROM scoped WHERE relative_path<>''
            )
            SELECT root_id, child_name, COUNT(*) AS source_count,
                MAX(revision) AS revision
            FROM projected WHERE child_name<>''
            GROUP BY root_id, child_name
            ORDER BY lower(child_name), root_id
        """
        query_params = [relative_start, *params]
        with self.connect() as connection:
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
            child = str(row["child_name"])
            child_path = "/".join(
                item for item in (parent_token, child) if item
            )
            items.append(
                {
                    "schema": "adaos.media_library.folder_node.v1",
                    "id": stable_id(
                        "folder", self.agent_id, str(row["root_id"]), child_path, size=20
                    ),
                    "agent_id": self.agent_id,
                    "node_id": self.node_id,
                    "root_id": str(row["root_id"]),
                    "path": child_path,
                    "parent": parent_token,
                    "name": child,
                    "source_count": int(row["source_count"]),
                    "revision": int(row["revision"]),
                }
            )
        next_offset = offset + len(items)
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "items": items,
            "count": len(items),
            "total_count": total,
            "parent": parent_token,
            "breadcrumbs": [
                {
                    "name": segment,
                    "path": "/".join(parent_token.split("/")[: index + 1]),
                }
                for index, segment in enumerate(parent_token.split("/"))
                if segment
            ],
            "pagination": {
                "limit": bounded,
                "cursor": encode_cursor(offset),
                "next_cursor": encode_cursor(next_offset)
                if next_offset < total
                else None,
                "has_more": next_offset < total,
            },
        }

    def configure_schedule(
        self,
        root_id: str,
        *,
        enabled: bool,
        interval_seconds: int = 21600,
        debounce_seconds: int = 30,
        watch_enabled: bool = False,
        watch_poll_seconds: int = 30,
    ) -> dict[str, Any]:
        if self.get_root(root_id) is None:
            return {"ok": False, "error": "root_not_found", "root_id": text(root_id), "schema": SCHEMA_VERSION}
        interval = max(300, min(604800, int(interval_seconds or 21600)))
        debounce = max(1, min(3600, int(debounce_seconds or 30)))
        watch_poll = max(5, min(3600, int(watch_poll_seconds or 30)))
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO schedules(
                    root_id, enabled, interval_seconds, debounce_seconds,
                    watch_enabled, watch_poll_seconds, next_run_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '', ?)
                ON CONFLICT(root_id) DO UPDATE SET enabled=excluded.enabled,
                    interval_seconds=excluded.interval_seconds, debounce_seconds=excluded.debounce_seconds,
                    watch_enabled=excluded.watch_enabled,
                    watch_poll_seconds=excluded.watch_poll_seconds,
                    updated_at=excluded.updated_at
                """,
                (
                    text(root_id), int(enabled), interval, debounce,
                    int(watch_enabled), watch_poll, now_iso(),
                ),
            )
            connection.commit()
        return {"ok": True, "schema": SCHEMA_VERSION, "schedule": self.get_schedule(root_id)}

    def get_schedule(self, root_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM schedules WHERE root_id=?", (text(root_id),)).fetchone()
        return (
            dict(row)
            | {
                "enabled": bool(row["enabled"]),
                "watch_enabled": bool(row["watch_enabled"]),
            }
            if row
            else None
        )

    def due_schedules(self, *, now: str | None = None) -> list[dict[str, Any]]:
        moment = text(now) or now_iso()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM schedules WHERE enabled=1 AND (next_run_at='' OR next_run_at<=?) ORDER BY next_run_at",
                (moment,),
            ).fetchall()
        return [dict(row) | {"enabled": True} for row in rows]

    def watch_schedules(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.*, r.path, r.follow_symlinks, r.exclusions_json
                FROM schedules s JOIN roots r ON r.id=s.root_id
                WHERE s.enabled=1 AND s.watch_enabled=1 AND r.enabled=1
                ORDER BY s.root_id
                """
            ).fetchall()
        return [
            dict(row)
            | {
                "enabled": True,
                "watch_enabled": True,
                "follow_symlinks": bool(row["follow_symlinks"]),
                "exclusions": json_loads(row["exclusions_json"], []),
            }
            for row in rows
        ]

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
            rendition = connection.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN status IN ('queued','running','waiting_resources','canceling') THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
                FROM rendition_jobs
                """
            ).fetchone()
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
            "renditions": {
                "job_count": int(rendition["total"] or 0),
                "active_count": int(rendition["active"] or 0),
                "completed_count": int(rendition["completed"] or 0),
                "failed_count": int(rendition["failed"] or 0),
            },
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

    def _public_rendition_job(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": RENDITION_JOB_SCHEMA,
            "id": str(row["id"]),
            "source_id": str(row["source_id"]),
            "root_id": str(row["root_id"]),
            "source_revision": int(row["source_revision"]),
            "source_fingerprint": str(row["source_fingerprint"]),
            "media_kind": str(row["media_kind"]),
            "profile": str(row["profile"]),
            "target": json_loads(row["target_json"], {}),
            "priority": int(row["priority"]),
            "status": str(row["status"]),
            "requested_at": str(row["requested_at"]),
            "started_at": str(row["started_at"]),
            "finished_at": str(row["finished_at"]),
            "output": json_loads(row["output_json"], {}),
            "output_bytes": int(row["output_bytes"]),
            "error": (
                {
                    "code": str(row["error_code"]),
                    "detail": str(row["error_detail"]),
                }
                if row["error_code"]
                else None
            ),
            "cancel_requested": bool(row["cancel_requested"]),
            "cleaned_at": str(row["cleaned_at"]),
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
