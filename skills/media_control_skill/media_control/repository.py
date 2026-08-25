from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "adaos.media_control.v2"
SESSION_SCHEMA = "adaos.media_control.playback_session.v1"
TARGET_SCHEMA = "adaos.media_control.playback_target.v1"
COMMAND_SCHEMA = "adaos.media_control.playback_command.v1"
QUEUE_SCHEMA = "adaos.media_control.playback_queue.v1"
CHECKPOINT_SCHEMA = "adaos.media_control.playback_checkpoint.v1"
RECONCILIATION_SCHEMA = "adaos.media_control.endpoint_reconciliation.v1"
QOE_SUMMARY_SCHEMA = "adaos.media_control.qoe_summary.v1"
MAX_QUEUE_ITEMS = 500
MAX_QUEUE_PAGE = 30
MAX_COMMAND_PAGE = 100


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def text(value: Any) -> str:
    return str(value or "").strip()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def stable_id(prefix: str, *parts: Any, size: int = 24) -> str:
    payload = "\0".join(text(item) for item in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8', errors='replace')).hexdigest()[:size]}"


def encode_cursor(value: int) -> str:
    raw = json.dumps({"v": 1, "n": max(0, int(value))}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: Any) -> int:
    token = text(value)
    if not token:
        return 0
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("v") != 1:
            raise ValueError("unsupported cursor")
        return max(0, int(payload["n"]))
    except Exception as exc:
        raise ValueError("invalid_media_control_cursor") from exc


def default_db_path() -> Path:
    override = text(os.environ.get("MEDIA_CONTROL_DB_PATH"))
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
    return db_dir / "media_control.sqlite3"


class MediaControlRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def ensure_schema(self) -> dict[str, Any]:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS playback_targets (
                    id TEXT PRIMARY KEY,
                    endpoint_id TEXT NOT NULL UNIQUE,
                    webspace_id TEXT NOT NULL,
                    node_id TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'available',
                    last_seen_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS playback_sessions (
                    id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    target_id TEXT NOT NULL REFERENCES playback_targets(id),
                    state TEXT NOT NULL,
                    active_queue_index INTEGER NOT NULL DEFAULT 0,
                    active_item_id TEXT NOT NULL DEFAULT '',
                    work_id TEXT NOT NULL DEFAULT '',
                    variant_id TEXT NOT NULL DEFAULT '',
                    source_id TEXT NOT NULL DEFAULT '',
                    route_json TEXT NOT NULL DEFAULT '{}',
                    position_ms INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    rate REAL NOT NULL DEFAULT 1.0,
                    volume REAL NOT NULL DEFAULT 1.0,
                    muted INTEGER NOT NULL DEFAULT 0,
                    tracks_json TEXT NOT NULL DEFAULT '{}',
                    autoplay INTEGER NOT NULL DEFAULT 1,
                    auto_fullscreen INTEGER NOT NULL DEFAULT 1,
                    queue_source_json TEXT NOT NULL DEFAULT '{}',
                    queue_revision INTEGER NOT NULL DEFAULT 1,
                    command_revision INTEGER NOT NULL DEFAULT 0,
                    observed_command_revision INTEGER NOT NULL DEFAULT 0,
                    endpoint_observation_revision INTEGER NOT NULL DEFAULT 0,
                    endpoint_last_seen_at TEXT NOT NULL DEFAULT '',
                    endpoint_state_json TEXT NOT NULL DEFAULT '{}',
                    sleep_timer_at REAL NOT NULL DEFAULT 0,
                    control_actor_ref TEXT NOT NULL,
                    control_lease_expires_at REAL NOT NULL,
                    control_lease_revision INTEGER NOT NULL DEFAULT 1,
                    interruption_json TEXT NOT NULL DEFAULT '{}',
                    checkpoint_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_media_control_target ON playback_sessions(target_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_media_control_profile ON playback_sessions(profile_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS playback_queue_items (
                    session_id TEXT NOT NULL REFERENCES playback_sessions(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    work_id TEXT NOT NULL DEFAULT '',
                    variant_id TEXT NOT NULL DEFAULT '',
                    source_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    available INTEGER NOT NULL DEFAULT 1,
                    descriptor_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(session_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS playback_commands (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL REFERENCES playback_sessions(id) ON DELETE CASCADE,
                    target_id TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    command TEXT NOT NULL,
                    arguments_json TEXT NOT NULL DEFAULT '{}',
                    expected_revision INTEGER NOT NULL,
                    resulting_revision INTEGER NOT NULL,
                    command_revision INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_media_control_commands_target ON playback_commands(target_id, sequence);
                CREATE TABLE IF NOT EXISTS playback_checkpoints (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    position_ms INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    session_revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_media_control_checkpoint_item ON playback_checkpoints(profile_id, item_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS playback_settings (
                    profile_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    autoplay INTEGER NOT NULL DEFAULT 1,
                    auto_fullscreen INTEGER NOT NULL DEFAULT 1,
                    background_audio INTEGER NOT NULL DEFAULT 1,
                    video_close_policy TEXT NOT NULL DEFAULT 'pip_or_pause',
                    preferred_quality TEXT NOT NULL DEFAULT 'auto',
                    audio_language TEXT NOT NULL DEFAULT '',
                    subtitle_language TEXT NOT NULL DEFAULT '',
                    preferred_rate REAL NOT NULL DEFAULT 1.0,
                    resume_after_reconnect INTEGER NOT NULL DEFAULT 1,
                    checkpoint_interval_seconds INTEGER NOT NULL DEFAULT 15,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id, target_id)
                );
                CREATE TABLE IF NOT EXISTS playback_qoe_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    value REAL NOT NULL,
                    dimensions_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_media_control_qoe_session
                    ON playback_qoe_events(session_id, sequence DESC);
                CREATE TABLE IF NOT EXISTS playback_endpoint_observations (
                    session_id TEXT NOT NULL REFERENCES playback_sessions(id) ON DELETE CASCADE,
                    target_id TEXT NOT NULL,
                    endpoint_revision INTEGER NOT NULL,
                    acknowledged_command_revision INTEGER NOT NULL,
                    observed_json TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, target_id, endpoint_revision)
                );
                """
            )
            session_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(playback_sessions)"
                ).fetchall()
            }
            for name, definition in {
                "queue_source_json": "TEXT NOT NULL DEFAULT '{}'",
                "observed_command_revision": "INTEGER NOT NULL DEFAULT 0",
                "endpoint_observation_revision": "INTEGER NOT NULL DEFAULT 0",
                "endpoint_last_seen_at": "TEXT NOT NULL DEFAULT ''",
                "endpoint_state_json": "TEXT NOT NULL DEFAULT '{}'",
                "sleep_timer_at": "REAL NOT NULL DEFAULT 0",
            }.items():
                if name not in session_columns:
                    connection.execute(
                        f"ALTER TABLE playback_sessions ADD COLUMN {name} {definition}"
                    )
            command_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(playback_commands)"
                ).fetchall()
            }
            if "command_revision" not in command_columns:
                connection.execute(
                    "ALTER TABLE playback_commands ADD COLUMN command_revision INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                UPDATE playback_commands AS command
                SET command_revision=(
                    SELECT COUNT(*) FROM playback_commands AS earlier
                    WHERE earlier.session_id=command.session_id
                        AND earlier.sequence<=command.sequence
                )
                WHERE command_revision=0
                """
            )
            settings_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(playback_settings)"
                ).fetchall()
            }
            for name, definition in {
                "preferred_rate": "REAL NOT NULL DEFAULT 1.0",
                "resume_after_reconnect": "INTEGER NOT NULL DEFAULT 1",
                "checkpoint_interval_seconds": "INTEGER NOT NULL DEFAULT 15",
            }.items():
                if name not in settings_columns:
                    connection.execute(
                        f"ALTER TABLE playback_settings ADD COLUMN {name} {definition}"
                    )
            connection.commit()
        return {"ok": True, "schema": SCHEMA_VERSION, "db_path": str(self.db_path)}

    def register_target(
        self,
        endpoint_id: str,
        *,
        webspace_id: str,
        label: str,
        kind: str,
        node_id: str = "",
        capabilities: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        endpoint = text(endpoint_id)
        if not endpoint:
            return {"ok": False, "error": "endpoint_id_required"}
        target_id = stable_id("target", endpoint, size=20)
        target_kind = text(kind).lower() or "browser"
        if target_kind not in {"browser", "tv", "mobile", "speaker", "native"}:
            return {"ok": False, "error": "invalid_target_kind"}
        now = now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO playback_targets(id,endpoint_id,webspace_id,node_id,label,kind,capabilities_json,status,last_seen_at)
                VALUES (?,?,?,?,?,?,?,'available',?)
                ON CONFLICT(endpoint_id) DO UPDATE SET webspace_id=excluded.webspace_id,node_id=excluded.node_id,
                    label=excluded.label,kind=excluded.kind,capabilities_json=excluded.capabilities_json,
                    status='available',last_seen_at=excluded.last_seen_at,revision=playback_targets.revision+1
                """,
                (target_id, endpoint, text(webspace_id), text(node_id), text(label) or endpoint, target_kind, dumps(dict(capabilities or {})), now),
            )
            row = connection.execute("SELECT * FROM playback_targets WHERE endpoint_id=?", (endpoint,)).fetchone()
            connection.commit()
        return {"ok": True, "schema": SCHEMA_VERSION, "target": self._public_target(row)}

    def list_targets(self, *, include_unavailable: bool = False, limit: int = 50) -> dict[str, Any]:
        bounded = max(1, min(100, int(limit or 50)))
        where = "" if include_unavailable else "WHERE status='available'"
        with self.connect() as connection:
            rows = connection.execute(f"SELECT * FROM playback_targets {where} ORDER BY lower(label),id LIMIT ?", (bounded,)).fetchall()
        return {"ok": True, "schema": SCHEMA_VERSION, "items": [self._public_target(row) for row in rows], "count": len(rows)}

    def create_session(
        self,
        *,
        profile_id: str,
        target_id: str,
        actor_ref: str,
        queue: Iterable[Mapping[str, Any]],
        active_index: int = 0,
        route: Mapping[str, Any] | None = None,
        queue_source: Mapping[str, Any] | None = None,
        lease_seconds: int = 120,
        retire_existing: bool = False,
    ) -> dict[str, Any]:
        target = self.get_target(target_id)
        if target is None or target["status"] != "available":
            return {"ok": False, "error": "playback_target_unavailable", "target_id": text(target_id)}
        items = [dict(item) for item in queue][:MAX_QUEUE_ITEMS]
        if not items:
            return {"ok": False, "error": "playback_queue_empty"}
        index = max(0, min(len(items) - 1, int(active_index or 0)))
        profile = text(profile_id) or "default"
        actor = text(actor_ref) or f"profile:{profile}"
        settings = self.get_settings(profile_id=profile, target_id=target["id"])["settings"]
        created_at = now_iso()
        session_id = stable_id("session", target["id"], profile, created_at, size=24)
        active = items[index]
        lease_expires = time.time() + max(30, min(900, int(lease_seconds or 120)))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            retired_session_count = 0
            if retire_existing:
                retired_session_count = connection.execute(
                    """
                    UPDATE playback_sessions
                    SET state='stopped',revision=revision+1,updated_at=?
                    WHERE target_id=? AND state NOT IN ('stopped','ended')
                    """,
                    (created_at, target["id"]),
                ).rowcount
            connection.execute(
                """
                INSERT INTO playback_sessions(
                    id,profile_id,target_id,state,active_queue_index,active_item_id,work_id,
                    variant_id,source_id,route_json,rate,autoplay,auto_fullscreen,queue_source_json,control_actor_ref,
                    control_lease_expires_at,created_at,updated_at
                ) VALUES (?,?,?,'requested',?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session_id, profile, target["id"], index, text(active.get("item_id") or active.get("id")),
                    text(active.get("work_id")), text(active.get("variant_id")), text(active.get("source_id")),
                    dumps(dict(route or active.get("route") or {})), float(settings["preferred_rate"]),
                    int(settings["autoplay"]), int(settings["auto_fullscreen"]),
                    dumps(dict(queue_source or {})), actor, lease_expires, created_at, created_at,
                ),
            )
            self._replace_queue(connection, session_id, items)
            connection.commit()
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "session": self.get_session(session_id)["session"],
            "retired_session_count": retired_session_count,
        }

    def get_target(self, target_id: str) -> dict[str, Any] | None:
        token = text(target_id)
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM playback_targets WHERE id=? OR endpoint_id=?", (token, token)).fetchone()
        return self._public_target(row) if row else None

    def get_session(self, session_id: str, *, queue_limit: int = 10, queue_cursor: str = "") -> dict[str, Any]:
        token = text(session_id)
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM playback_sessions WHERE id=?", (token,)).fetchone()
            if row is None:
                return {"ok": False, "error": "playback_session_not_found", "session_id": token}
            session = self._public_session(row)
            queue_page = self._queue_page(connection, token, limit=queue_limit, cursor=queue_cursor)
        session["queue"] = queue_page
        session["target"] = self.get_target(session["target_id"])
        return {"ok": True, "schema": SCHEMA_VERSION, "session": session}

    def command(
        self,
        session_id: str,
        *,
        command: str,
        arguments: Mapping[str, Any] | None,
        actor_ref: str,
        expected_revision: int,
        idempotency_key: str,
        lease_seconds: int = 120,
    ) -> dict[str, Any]:
        token = text(session_id)
        command_token = text(command).lower()
        supported = {
            "play",
            "pause",
            "seek",
            "volume",
            "mute",
            "next",
            "previous",
            "stop",
            "handoff",
            "tracks",
            "rate",
            "sleep_timer",
        }
        if command_token not in supported:
            return {"ok": False, "error": "unsupported_playback_command"}
        key = text(idempotency_key)
        if not key:
            return {"ok": False, "error": "idempotency_key_required"}
        actor = text(actor_ref)
        args = dict(arguments or {})
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT * FROM playback_commands WHERE idempotency_key=?", (key,)).fetchone()
            if previous:
                connection.commit()
                return {"ok": True, "schema": SCHEMA_VERSION, "idempotent_replay": True, "command": self._public_command(previous), "session": self.get_session(token)["session"]}
            row = connection.execute("SELECT * FROM playback_sessions WHERE id=?", (token,)).fetchone()
            if row is None:
                connection.rollback()
                return {"ok": False, "error": "playback_session_not_found", "session_id": token}
            current_revision = int(row["revision"])
            if int(expected_revision) != current_revision:
                connection.rollback()
                return {"ok": False, "error": "playback_revision_conflict", "expected_revision": int(expected_revision), "current_revision": current_revision}
            if not self._lease_allows(row, actor):
                connection.rollback()
                return {"ok": False, "error": "playback_control_lease_conflict", "holder": str(row["control_actor_ref"]), "lease_revision": int(row["control_lease_revision"])}
            updates = self._command_updates(connection, row, command_token, args)
            next_revision = current_revision + 1
            command_revision = int(row["command_revision"]) + 1
            updates.update(
                {
                    "revision": next_revision,
                    "command_revision": command_revision,
                    "control_actor_ref": actor or str(row["control_actor_ref"]),
                    "control_lease_expires_at": time.time() + max(30, min(900, int(lease_seconds or 120))),
                    "control_lease_revision": int(row["control_lease_revision"]) + 1,
                    "updated_at": now_iso(),
                }
            )
            assignments = ",".join(f"{name}=?" for name in updates)
            connection.execute(f"UPDATE playback_sessions SET {assignments} WHERE id=?", (*updates.values(), token))
            command_id = stable_id("command", token, key, size=24)
            connection.execute(
                """
                INSERT INTO playback_commands(
                    id,idempotency_key,session_id,target_id,actor_ref,command,arguments_json,
                    expected_revision,resulting_revision,command_revision,status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)
                """,
                (
                    command_id,
                    key,
                    token,
                    str(row["target_id"]),
                    actor,
                    command_token,
                    dumps(args),
                    current_revision,
                    next_revision,
                    command_revision,
                    now_iso(),
                ),
            )
            changed = connection.execute("SELECT * FROM playback_commands WHERE id=?", (command_id,)).fetchone()
            connection.commit()
        return {"ok": True, "schema": SCHEMA_VERSION, "command": self._public_command(changed), "session": self.get_session(token)["session"], "idempotent_replay": False}

    def _command_updates(self, connection: sqlite3.Connection, row: sqlite3.Row, command: str, args: Mapping[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if command == "play":
            updates["state"] = "playing"
        elif command == "pause":
            updates["state"] = "paused"
        elif command == "stop":
            updates.update({"state": "stopped", "position_ms": 0})
        elif command == "seek":
            updates["position_ms"] = max(0, min(int(row["duration_ms"] or 2**63 - 1), int(args.get("position_ms") or 0)))
        elif command == "volume":
            updates["volume"] = max(0.0, min(1.0, float(args.get("volume") if args.get("volume") is not None else row["volume"])))
        elif command == "mute":
            updates["muted"] = int(bool(args.get("muted", True)))
        elif command == "rate":
            updates["rate"] = max(0.25, min(4.0, float(args.get("rate") or 1.0)))
        elif command == "tracks":
            updates["tracks_json"] = dumps(dict(args.get("tracks") or {}))
        elif command == "sleep_timer":
            seconds = max(0, min(24 * 60 * 60, int(args.get("seconds") or 0)))
            updates["sleep_timer_at"] = time.time() + seconds if seconds else 0
        elif command in {"next", "previous"}:
            direction = 1 if command == "next" else -1
            next_item = self._next_available(connection, str(row["id"]), int(row["active_queue_index"]), direction)
            if next_item is None:
                updates["state"] = "ended" if direction > 0 else str(row["state"])
            else:
                updates.update(
                    {
                        "active_queue_index": int(next_item["ordinal"]),
                        "active_item_id": str(next_item["item_id"]),
                        "work_id": str(next_item["work_id"]),
                        "variant_id": str(next_item["variant_id"]),
                        "source_id": str(next_item["source_id"]),
                        "position_ms": 0,
                        "state": "playing" if bool(row["autoplay"]) else "ready",
                        "route_json": dumps((loads(next_item["descriptor_json"], {}) or {}).get("route") or {}),
                    }
                )
        elif command == "handoff":
            target = self.get_target(text(args.get("target_id")))
            if target is None or target["status"] != "available":
                raise ValueError("playback_target_unavailable")
            updates["target_id"] = target["id"]
            updates["state"] = "recovering"
            updates["interruption_json"] = dumps({"reason": "handoff", "from_target_id": str(row["target_id"]), "at": now_iso()})
        return updates

    def update_queue(
        self,
        session_id: str,
        *,
        queue: Iterable[Mapping[str, Any]],
        expected_queue_revision: int,
        active_index: int | None = None,
        actor_ref: str,
    ) -> dict[str, Any]:
        items = [dict(item) for item in queue][:MAX_QUEUE_ITEMS]
        if not items:
            return {"ok": False, "error": "playback_queue_empty"}
        token = text(session_id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM playback_sessions WHERE id=?", (token,)).fetchone()
            if row is None:
                connection.rollback()
                return {"ok": False, "error": "playback_session_not_found"}
            if int(row["queue_revision"]) != int(expected_queue_revision):
                connection.rollback()
                return {"ok": False, "error": "playback_queue_revision_conflict", "current_queue_revision": int(row["queue_revision"])}
            if not self._lease_allows(row, text(actor_ref)):
                connection.rollback()
                return {"ok": False, "error": "playback_control_lease_conflict"}
            self._replace_queue(connection, token, items)
            requested_index = (
                int(row["active_queue_index"])
                if active_index is None
                else int(active_index)
            )
            new_index = max(0, min(requested_index, len(items) - 1))
            active = items[new_index]
            active_item_id = text(active.get("item_id") or active.get("id"))
            selection_changed = (
                active_index is not None
                or active_item_id != str(row["active_item_id"])
            )
            descriptor = (
                active.get("descriptor")
                if isinstance(active.get("descriptor"), Mapping)
                else active
            )
            route = descriptor.get("route") if isinstance(descriptor, Mapping) else {}
            connection.execute(
                """
                UPDATE playback_sessions SET queue_revision=queue_revision+1,revision=revision+1,
                    active_queue_index=?,active_item_id=?,work_id=?,variant_id=?,source_id=?,route_json=?,
                    position_ms=?,duration_ms=?,state=?,updated_at=? WHERE id=?
                """,
                (
                    new_index,
                    active_item_id,
                    text(active.get("work_id")),
                    text(active.get("variant_id")),
                    text(active.get("source_id")),
                    dumps(route or {}),
                    0 if selection_changed else int(row["position_ms"]),
                    0 if selection_changed else int(row["duration_ms"]),
                    "ready" if selection_changed else str(row["state"]),
                    now_iso(),
                    token,
                ),
            )
            connection.commit()
        return self.get_session(token)

    def checkpoint(
        self,
        session_id: str,
        *,
        position_ms: int,
        duration_ms: int,
        state: str,
        source: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        token = text(session_id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM playback_sessions WHERE id=?", (token,)).fetchone()
            if row is None:
                connection.rollback()
                return {"ok": False, "error": "playback_session_not_found"}
            if int(row["revision"]) != int(expected_revision):
                connection.rollback()
                return {"ok": False, "error": "playback_revision_conflict", "current_revision": int(row["revision"])}
            position = max(0, int(position_ms or 0))
            duration = max(position, int(duration_ms or 0))
            completed = state == "ended" or (duration > 0 and position >= duration * 0.95)
            next_revision = int(row["revision"]) + 1
            connection.execute(
                "UPDATE playback_sessions SET position_ms=?,duration_ms=?,state=?,checkpoint_at=?,updated_at=?,revision=? WHERE id=?",
                (0 if completed else position, duration, text(state) or str(row["state"]), now_iso(), now_iso(), next_revision, token),
            )
            checkpoint_id = stable_id("checkpoint", token, next_revision, size=24)
            connection.execute(
                "INSERT INTO playback_checkpoints(id,session_id,profile_id,item_id,position_ms,duration_ms,completed,source,session_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (checkpoint_id, token, str(row["profile_id"]), str(row["active_item_id"]), position, duration, int(completed), text(source) or "endpoint", next_revision, now_iso()),
            )
            connection.commit()
        return {"ok": True, "schema": CHECKPOINT_SCHEMA, "checkpoint_id": checkpoint_id, "session": self.get_session(token)["session"]}

    def pull_commands(
        self,
        target_id: str,
        *,
        session_id: str = "",
        cursor: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        self.apply_due_sleep_timers()
        after = decode_cursor(cursor)
        bounded = max(1, min(MAX_COMMAND_PAGE, int(limit or 50)))
        target = self.get_target(target_id)
        if target is None:
            return {"ok": False, "error": "playback_target_not_found"}
        filters = ["target_id=?", "sequence>?"]
        params: list[Any] = [target["id"], after]
        if text(session_id):
            filters.append("session_id=?")
            params.append(text(session_id))
        params.append(bounded + 1)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM playback_commands WHERE {' AND '.join(filters)} ORDER BY sequence LIMIT ?",
                tuple(params),
            ).fetchall()
        has_more = len(rows) > bounded
        visible = rows[:bounded]
        next_sequence = int(visible[-1]["sequence"]) if visible else after
        return {"ok": True, "schema": SCHEMA_VERSION, "items": [self._public_command(row) for row in visible], "count": len(visible), "next_cursor": encode_cursor(next_sequence), "has_more": has_more}

    def apply_due_sleep_timers(self) -> dict[str, Any]:
        applied: list[str] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM playback_sessions
                WHERE sleep_timer_at>0 AND sleep_timer_at<=?
                    AND state NOT IN ('paused','stopped','ended')
                ORDER BY sleep_timer_at LIMIT 100
                """,
                (time.time(),),
            ).fetchall()
            for row in rows:
                current_revision = int(row["revision"])
                next_revision = current_revision + 1
                command_revision = int(row["command_revision"]) + 1
                deadline = float(row["sleep_timer_at"])
                key = f"sleep-expire:{row['id']}:{int(deadline)}"
                command_id = stable_id("command", row["id"], key, size=24)
                connection.execute(
                    """
                    UPDATE playback_sessions SET state='paused',sleep_timer_at=0,
                        revision=?,command_revision=?,updated_at=? WHERE id=?
                    """,
                    (next_revision, command_revision, now_iso(), row["id"]),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO playback_commands(
                        id,idempotency_key,session_id,target_id,actor_ref,command,
                        arguments_json,expected_revision,resulting_revision,
                        command_revision,status,created_at
                    ) VALUES (?,?,?,?,?,'pause',?,?,?,?,'pending',?)
                    """,
                    (
                        command_id,
                        key,
                        row["id"],
                        row["target_id"],
                        "system:sleep_timer",
                        dumps({"reason": "sleep_timer", "deadline": deadline}),
                        current_revision,
                        next_revision,
                        command_revision,
                        now_iso(),
                    ),
                )
                applied.append(str(row["id"]))
            connection.commit()
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "applied_session_ids": applied,
            "count": len(applied),
            "bounded": True,
        }

    def acknowledge_command(self, command_id: str, *, status: str, result: Mapping[str, Any] | None = None) -> dict[str, Any]:
        status_token = text(status).lower()
        if status_token not in {"applied", "rejected", "failed"}:
            return {"ok": False, "error": "invalid_command_status"}
        with self.connect() as connection:
            changed = connection.execute(
                "UPDATE playback_commands SET status=?,acknowledged_at=?,result_json=? WHERE id=? AND status='pending'",
                (status_token, now_iso(), dumps(dict(result or {})), text(command_id)),
            ).rowcount
            row = connection.execute("SELECT * FROM playback_commands WHERE id=?", (text(command_id),)).fetchone()
            connection.commit()
        if row is None:
            return {"ok": False, "error": "playback_command_not_found"}
        return {"ok": True, "schema": SCHEMA_VERSION, "changed": bool(changed), "command": self._public_command(row)}

    def reconcile_endpoint(
        self,
        session_id: str,
        *,
        target_id: str,
        endpoint_revision: int,
        acknowledged_command_revision: int,
        observed: Mapping[str, Any] | None = None,
        authority: str = "endpoint_preferred",
    ) -> dict[str, Any]:
        token = text(session_id)
        target = self.get_target(target_id)
        if target is None:
            return {"ok": False, "error": "playback_target_not_found"}
        observation_revision = int(endpoint_revision or 0)
        if observation_revision < 1:
            return {"ok": False, "error": "endpoint_revision_required"}
        authority_token = text(authority).lower() or "endpoint_preferred"
        if authority_token not in {"endpoint_preferred", "coordinator_preferred"}:
            return {"ok": False, "error": "invalid_reconciliation_authority"}
        payload = dict(observed or {})
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM playback_sessions WHERE id=?", (token,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return {
                    "ok": False,
                    "error": "playback_session_not_found",
                    "session_id": token,
                }
            if str(row["target_id"]) != target["id"]:
                connection.rollback()
                return {"ok": False, "error": "playback_target_session_mismatch"}
            replay = connection.execute(
                """
                SELECT action_json FROM playback_endpoint_observations
                WHERE session_id=? AND target_id=? AND endpoint_revision=?
                """,
                (token, target["id"], observation_revision),
            ).fetchone()
            if replay is not None:
                connection.commit()
                return {
                    "ok": True,
                    "schema": RECONCILIATION_SCHEMA,
                    "idempotent_replay": True,
                    "action": loads(replay["action_json"], {}),
                    "session": self.get_session(token)["session"],
                }
            if observation_revision < int(row["endpoint_observation_revision"]):
                connection.rollback()
                return {
                    "ok": False,
                    "error": "stale_endpoint_observation",
                    "current_endpoint_revision": int(
                        row["endpoint_observation_revision"]
                    ),
                }
            acknowledged = max(0, int(acknowledged_command_revision or 0))
            if acknowledged > int(row["command_revision"]):
                connection.rollback()
                return {
                    "ok": False,
                    "error": "invalid_acknowledged_command_revision",
                    "current_command_revision": int(row["command_revision"]),
                }
            connection.execute(
                """
                UPDATE playback_commands
                SET status='applied', acknowledged_at=?
                WHERE session_id=? AND target_id=? AND status='pending'
                    AND command_revision>0 AND command_revision<=?
                """,
                (now_iso(), token, target["id"], acknowledged),
            )
            pending_rows = connection.execute(
                """
                SELECT * FROM playback_commands
                WHERE session_id=? AND target_id=? AND status='pending'
                    AND command_revision>?
                ORDER BY command_revision LIMIT 30
                """,
                (token, target["id"], acknowledged),
            ).fetchall()
            observed_item_id = text(payload.get("active_item_id"))
            desired_item_id = str(row["active_item_id"])
            action: dict[str, Any]
            if observed_item_id and observed_item_id != desired_item_id:
                observed_item = connection.execute(
                    """
                    SELECT * FROM playback_queue_items
                    WHERE session_id=? AND item_id=?
                    ORDER BY ordinal LIMIT 1
                    """,
                    (token, observed_item_id),
                ).fetchone()
                if (
                    authority_token == "endpoint_preferred"
                    and not pending_rows
                    and observed_item is not None
                ):
                    descriptor = loads(observed_item["descriptor_json"], {})
                    connection.execute(
                        """
                        UPDATE playback_sessions
                        SET active_queue_index=?,active_item_id=?,work_id=?,variant_id=?,
                            source_id=?,route_json=?,state=?,position_ms=?,duration_ms=?,
                            rate=?,volume=?,muted=?,revision=revision+1,updated_at=?
                        WHERE id=?
                        """,
                        (
                            int(observed_item["ordinal"]),
                            str(observed_item["item_id"]),
                            str(observed_item["work_id"]),
                            str(observed_item["variant_id"]),
                            str(observed_item["source_id"]),
                            dumps(descriptor.get("route") or {}),
                            text(payload.get("state")) or str(row["state"]),
                            max(0, int(payload.get("position_ms") or 0)),
                            max(0, int(payload.get("duration_ms") or 0)),
                            max(0.25, min(4.0, float(payload.get("rate") or row["rate"]))),
                            max(
                                0.0,
                                min(
                                    1.0,
                                    float(
                                        payload.get("volume")
                                        if payload.get("volume") is not None
                                        else row["volume"]
                                    ),
                                ),
                            ),
                            int(bool(payload.get("muted", row["muted"]))),
                            now_iso(),
                            token,
                        ),
                    )
                    action = {
                        "type": "noop",
                        "reason": "endpoint_queue_advance_accepted",
                    }
                else:
                    active = connection.execute(
                    """
                    SELECT descriptor_json FROM playback_queue_items
                    WHERE session_id=? AND ordinal=?
                    """,
                    (token, int(row["active_queue_index"])),
                ).fetchone()
                    action = {
                        "type": "load",
                        "item_id": desired_item_id,
                        "position_ms": int(row["position_ms"]),
                        "state": str(row["state"]),
                        "descriptor": loads(active["descriptor_json"], {}) if active else {},
                        "reason": "endpoint_item_differs",
                    }
            elif pending_rows:
                action = {
                    "type": "replay_commands",
                    "from_command_revision": acknowledged + 1,
                    "commands": [self._public_command(item) for item in pending_rows],
                }
            elif authority_token == "coordinator_preferred":
                observed_position = max(0, int(payload.get("position_ms") or 0))
                desired_position = int(row["position_ms"])
                if abs(observed_position - desired_position) > 1500:
                    action = {
                        "type": "seek",
                        "position_ms": desired_position,
                        "reason": "coordinator_checkpoint_newer",
                    }
                elif text(payload.get("state")) != str(row["state"]):
                    action = {
                        "type": "transport",
                        "state": str(row["state"]),
                        "reason": "coordinator_state_newer",
                    }
                else:
                    action = {"type": "noop", "reason": "already_converged"}
            else:
                updates = {
                    "state": text(payload.get("state")) or str(row["state"]),
                    "position_ms": max(0, int(payload.get("position_ms") or 0)),
                    "duration_ms": max(0, int(payload.get("duration_ms") or 0)),
                    "rate": max(
                        0.25,
                        min(4.0, float(payload.get("rate") or row["rate"])),
                    ),
                    "volume": max(
                        0.0,
                        min(
                            1.0,
                            float(
                                payload.get("volume")
                                if payload.get("volume") is not None
                                else row["volume"]
                            ),
                        ),
                    ),
                    "muted": int(bool(payload.get("muted", row["muted"]))),
                }
                connection.execute(
                    """
                    UPDATE playback_sessions SET state=?,position_ms=?,duration_ms=?,
                        rate=?,volume=?,muted=?,revision=revision+1,updated_at=?
                    WHERE id=?
                    """,
                    (*updates.values(), now_iso(), token),
                )
                action = {"type": "noop", "reason": "endpoint_state_accepted"}
            seen_at = now_iso()
            connection.execute(
                """
                UPDATE playback_sessions
                SET observed_command_revision=?,endpoint_observation_revision=?,
                    endpoint_last_seen_at=?,endpoint_state_json=?,updated_at=?
                WHERE id=?
                """,
                (
                    acknowledged,
                    observation_revision,
                    seen_at,
                    dumps(payload),
                    seen_at,
                    token,
                ),
            )
            connection.execute(
                """
                INSERT INTO playback_endpoint_observations(
                    session_id,target_id,endpoint_revision,
                    acknowledged_command_revision,observed_json,action_json,created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    token,
                    target["id"],
                    observation_revision,
                    acknowledged,
                    dumps(payload),
                    dumps(action),
                    seen_at,
                ),
            )
            connection.commit()
        return {
            "ok": True,
            "schema": RECONCILIATION_SCHEMA,
            "idempotent_replay": False,
            "action": action,
            "session": self.get_session(token)["session"],
        }

    def get_settings(self, *, profile_id: str, target_id: str = "") -> dict[str, Any]:
        profile = text(profile_id) or "default"
        target = text(target_id) or "*"
        with self.connect() as connection:
            global_row = connection.execute(
                "SELECT * FROM playback_settings WHERE profile_id=? AND target_id='*'",
                (profile,),
            ).fetchone()
            target_row = (
                connection.execute(
                    "SELECT * FROM playback_settings WHERE profile_id=? AND target_id=?",
                    (profile, target),
                ).fetchone()
                if target != "*"
                else global_row
            )
        defaults = {
            "profile_id": profile,
            "target_id": target,
            "autoplay": True,
            "auto_fullscreen": True,
            "background_audio": True,
            "video_close_policy": "pip_or_pause",
            "preferred_quality": "auto",
            "audio_language": "",
            "subtitle_language": "",
            "preferred_rate": 1.0,
            "resume_after_reconnect": True,
            "checkpoint_interval_seconds": 15,
            "revision": 0,
            "updated_at": "",
        }
        inherited = (
            {**defaults, **self._public_settings(global_row)}
            if global_row is not None
            else defaults
        )
        settings = (
            {**inherited, **self._public_settings(target_row)}
            if target_row is not None
            else inherited
        )
        settings["profile_id"] = profile
        settings["target_id"] = target
        settings["inherited_from_profile"] = bool(target != "*" and target_row is None)
        return {"ok": True, "schema": SCHEMA_VERSION, "settings": settings}

    def set_settings(self, *, profile_id: str, target_id: str = "", values: Mapping[str, Any]) -> dict[str, Any]:
        profile = text(profile_id) or "default"
        target = text(target_id) or "*"
        current = self.get_settings(profile_id=profile, target_id=target)["settings"]
        accepted = {
            "autoplay",
            "auto_fullscreen",
            "background_audio",
            "video_close_policy",
            "preferred_quality",
            "audio_language",
            "subtitle_language",
            "preferred_rate",
            "resume_after_reconnect",
            "checkpoint_interval_seconds",
        }
        merged = {
            **current,
            **{key: value for key, value in values.items() if key in accepted},
        }
        if text(merged["video_close_policy"]) not in {"pip_or_pause", "audio_only", "pause"}:
            return {"ok": False, "error": "invalid_video_close_policy"}
        preferred_rate = max(0.25, min(4.0, float(merged["preferred_rate"])))
        checkpoint_interval = max(
            5, min(120, int(merged["checkpoint_interval_seconds"] or 15))
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO playback_settings(profile_id,target_id,autoplay,auto_fullscreen,background_audio,video_close_policy,preferred_quality,audio_language,subtitle_language,preferred_rate,resume_after_reconnect,checkpoint_interval_seconds,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(profile_id,target_id) DO UPDATE SET autoplay=excluded.autoplay,auto_fullscreen=excluded.auto_fullscreen,
                    background_audio=excluded.background_audio,video_close_policy=excluded.video_close_policy,
                    preferred_quality=excluded.preferred_quality,audio_language=excluded.audio_language,
                    subtitle_language=excluded.subtitle_language,preferred_rate=excluded.preferred_rate,
                    resume_after_reconnect=excluded.resume_after_reconnect,
                    checkpoint_interval_seconds=excluded.checkpoint_interval_seconds,
                    revision=playback_settings.revision+1,updated_at=excluded.updated_at
                """,
                (
                    profile,
                    target,
                    int(bool(merged["autoplay"])),
                    int(bool(merged["auto_fullscreen"])),
                    int(bool(merged["background_audio"])),
                    text(merged["video_close_policy"]),
                    text(merged["preferred_quality"]),
                    text(merged["audio_language"]),
                    text(merged["subtitle_language"]),
                    preferred_rate,
                    int(bool(merged["resume_after_reconnect"])),
                    checkpoint_interval,
                    now_iso(),
                ),
            )
            connection.commit()
        return self.get_settings(profile_id=profile, target_id=target)

    def record_qoe(self, session_id: str, *, metric: str, value: float, dimensions: Mapping[str, Any] | None = None) -> dict[str, Any]:
        allowed = {"plan_latency_ms", "first_frame_ms", "seek_latency_ms", "rebuffer_ms", "route_change", "interruption", "completion"}
        metric_token = text(metric)
        if metric_token not in allowed:
            return {"ok": False, "error": "unsupported_qoe_metric"}
        session = self.get_session(session_id)
        if not session.get("ok"):
            return session
        target_id = session["session"]["target_id"]
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO playback_qoe_events(session_id,target_id,metric,value,dimensions_json,created_at) VALUES (?,?,?,?,?,?)",
                (text(session_id), target_id, metric_token, float(value), dumps(dict(dimensions or {})), now_iso()),
            )
            connection.commit()
        return {"ok": True, "schema": SCHEMA_VERSION, "metric": metric_token}

    def qoe_summary(
        self,
        *,
        session_id: str = "",
        target_id: str = "",
        limit: int = 30,
    ) -> dict[str, Any]:
        filters = ["1=1"]
        params: list[Any] = []
        if text(session_id):
            filters.append("session_id=?")
            params.append(text(session_id))
        if text(target_id):
            target = self.get_target(target_id)
            if target is None:
                return {"ok": False, "error": "playback_target_not_found"}
            filters.append("target_id=?")
            params.append(target["id"])
        where = " AND ".join(filters)
        bounded = max(1, min(100, int(limit or 30)))
        with self.connect() as connection:
            aggregates = connection.execute(
                f"""
                SELECT metric,COUNT(*) AS sample_count,AVG(value) AS average,
                    MAX(value) AS maximum,SUM(value) AS total
                FROM playback_qoe_events WHERE {where}
                GROUP BY metric ORDER BY metric
                """,
                tuple(params),
            ).fetchall()
            recent = connection.execute(
                f"""
                SELECT sequence,session_id,target_id,metric,value,dimensions_json,
                    created_at FROM playback_qoe_events WHERE {where}
                ORDER BY sequence DESC LIMIT ?
                """,
                (*params, bounded),
            ).fetchall()
        return {
            "ok": True,
            "schema": QOE_SUMMARY_SCHEMA,
            "metrics": [
                {
                    "metric": str(row["metric"]),
                    "sample_count": int(row["sample_count"]),
                    "average": float(row["average"]),
                    "maximum": float(row["maximum"]),
                    "total": float(row["total"]),
                }
                for row in aggregates
            ],
            "recent": [
                {
                    "sequence": int(row["sequence"]),
                    "session_id": str(row["session_id"]),
                    "target_id": str(row["target_id"]),
                    "metric": str(row["metric"]),
                    "value": float(row["value"]),
                    "dimensions": loads(row["dimensions_json"], {}),
                    "created_at": str(row["created_at"]),
                }
                for row in recent
            ],
            "count": len(recent),
            "bounded": True,
        }

    def now_playing(self, *, profile_id: str = "", target_id: str = "", limit: int = 20) -> dict[str, Any]:
        self.apply_due_sleep_timers()
        try:
            configured_freshness = int(
                os.environ.get("MEDIA_CONTROL_NOW_PLAYING_FRESHNESS_SECONDS") or 300
            )
        except (TypeError, ValueError):
            configured_freshness = 300
        freshness_seconds = max(
            30,
            min(3600, configured_freshness),
        )
        freshness_cutoff = (
            datetime.now(tz=timezone.utc) - timedelta(seconds=freshness_seconds)
        ).isoformat()
        filters = [
            "s.state NOT IN ('stopped','ended','error','failed')",
            "COALESCE(NULLIF(s.endpoint_last_seen_at,''),s.created_at)>=?",
        ]
        params: list[Any] = [freshness_cutoff]
        if text(profile_id):
            filters.append("s.profile_id=?")
            params.append(text(profile_id))
        if text(target_id):
            target = self.get_target(target_id)
            if target is None:
                return {"ok": False, "error": "playback_target_not_found"}
            filters.append("s.target_id=?")
            params.append(target["id"])
        params.append(max(1, min(50, int(limit or 20))))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT s.*,
                       t.label AS target_label,
                       t.kind AS target_kind,
                       t.capabilities_json AS target_capabilities_json,
                       q.title AS active_title,
                       q.descriptor_json AS active_descriptor_json
                FROM playback_sessions AS s
                LEFT JOIN playback_targets AS t ON t.id=s.target_id
                LEFT JOIN playback_queue_items AS q
                  ON q.session_id=s.id AND q.ordinal=s.active_queue_index
                WHERE {' AND '.join(filters)}
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        items = []
        for row in rows:
            item = self._public_session(row)
            descriptor = loads(row["active_descriptor_json"], {})
            target_capabilities = loads(row["target_capabilities_json"], {})
            target_device_label = text(
                target_capabilities.get("device_display_name")
            ) or text(row["target_label"])
            item.update(
                {
                    "title": text(row["active_title"]) or item["active_item_id"],
                    "media_kind": text(
                        descriptor.get("media_kind") or descriptor.get("kind")
                    ),
                    "artwork": dict(descriptor.get("artwork") or {}),
                    "target_label": target_device_label or item["target_id"],
                    "target_endpoint_label": text(
                        target_capabilities.get("endpoint_display_name")
                    )
                    or text(row["target_label"]),
                    "target_authorization_state": text(
                        target_capabilities.get("authorization_state")
                    )
                    or (
                        "authorized"
                        if bool(target_capabilities.get("authorized"))
                        else "guest"
                    ),
                    "target_kind": text(row["target_kind"]),
                }
            )
            items.append(item)
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "items": items,
            "count": len(items),
            "freshness_seconds": freshness_seconds,
            "updated_at": now_iso(),
        }

    def diagnostics(self) -> dict[str, Any]:
        with self.connect() as connection:
            sessions = connection.execute("SELECT state,COUNT(*) AS count FROM playback_sessions GROUP BY state").fetchall()
            pending = int(connection.execute("SELECT COUNT(*) FROM playback_commands WHERE status='pending'").fetchone()[0])
            targets = connection.execute("SELECT status,COUNT(*) AS count FROM playback_targets GROUP BY status").fetchall()
        return {
            "ok": True, "schema": SCHEMA_VERSION,
            "sessions": {str(row["state"]): int(row["count"]) for row in sessions},
            "targets": {str(row["status"]): int(row["count"]) for row in targets},
            "pending_command_count": pending,
            "limits": {"max_queue_items": MAX_QUEUE_ITEMS, "queue_page": MAX_QUEUE_PAGE, "command_page": MAX_COMMAND_PAGE},
            "media_bytes_routed_through_control": False,
        }

    @staticmethod
    def _lease_allows(row: sqlite3.Row, actor_ref: str) -> bool:
        holder = str(row["control_actor_ref"])
        return not holder or holder == actor_ref or float(row["control_lease_expires_at"] or 0) <= time.time()

    @staticmethod
    def _next_available(connection: sqlite3.Connection, session_id: str, current: int, direction: int) -> sqlite3.Row | None:
        comparator = ">" if direction > 0 else "<"
        order = "ASC" if direction > 0 else "DESC"
        return connection.execute(
            f"SELECT * FROM playback_queue_items WHERE session_id=? AND ordinal {comparator} ? AND available=1 ORDER BY ordinal {order} LIMIT 1",
            (session_id, current),
        ).fetchone()

    @staticmethod
    def _replace_queue(connection: sqlite3.Connection, session_id: str, items: list[Mapping[str, Any]]) -> None:
        connection.execute("DELETE FROM playback_queue_items WHERE session_id=?", (session_id,))
        connection.executemany(
            """
            INSERT INTO playback_queue_items(session_id,ordinal,item_id,work_id,variant_id,source_id,title,available,descriptor_json)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    session_id, ordinal, text(item.get("item_id") or item.get("id")), text(item.get("work_id")),
                    text(item.get("variant_id")), text(item.get("source_id")), text(item.get("title") or item.get("name")),
                    int(bool(item.get("available", not item.get("missing", False)))), dumps(dict(item)),
                )
                for ordinal, item in enumerate(items)
            ],
        )

    def _queue_page(self, connection: sqlite3.Connection, session_id: str, *, limit: int, cursor: str) -> dict[str, Any]:
        offset = decode_cursor(cursor)
        bounded = max(1, min(MAX_QUEUE_PAGE, int(limit or 10)))
        total = int(connection.execute("SELECT COUNT(*) FROM playback_queue_items WHERE session_id=?", (session_id,)).fetchone()[0])
        rows = connection.execute(
            "SELECT * FROM playback_queue_items WHERE session_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
            (session_id, bounded, offset),
        ).fetchall()
        items = [
            {
                "ordinal": int(row["ordinal"]), "item_id": str(row["item_id"]), "work_id": str(row["work_id"]),
                "variant_id": str(row["variant_id"]), "source_id": str(row["source_id"]), "title": str(row["title"]),
                "available": bool(row["available"]), "descriptor": loads(row["descriptor_json"], {}),
            }
            for row in rows
        ]
        next_offset = offset + len(items)
        return {"schema": QUEUE_SCHEMA, "items": items, "count": len(items), "total_count": total, "revision": int(connection.execute("SELECT queue_revision FROM playback_sessions WHERE id=?", (session_id,)).fetchone()[0]), "pagination": {"limit": bounded, "cursor": encode_cursor(offset), "next_cursor": encode_cursor(next_offset) if next_offset < total else None, "has_more": next_offset < total}}

    @staticmethod
    def _public_target(row: sqlite3.Row) -> dict[str, Any]:
        capabilities = loads(row["capabilities_json"], {})
        label = str(row["label"])
        device_label = text(capabilities.get("device_display_name")) or label
        endpoint_label = text(capabilities.get("endpoint_display_name")) or label
        authorization_state = text(capabilities.get("authorization_state")) or (
            "authorized" if bool(capabilities.get("authorized")) else "guest"
        )
        return {
            "schema": TARGET_SCHEMA,
            "id": str(row["id"]),
            "endpoint_id": str(row["endpoint_id"]),
            "webspace_id": str(row["webspace_id"]),
            "node_id": str(row["node_id"]),
            "label": label,
            "display_label": device_label,
            "device_label": device_label,
            "endpoint_label": endpoint_label,
            "authorization_state": authorization_state,
            "kind": str(row["kind"]),
            "capabilities": capabilities,
            "status": str(row["status"]),
            "last_seen_at": str(row["last_seen_at"]),
            "revision": int(row["revision"]),
        }

    @staticmethod
    def _public_session(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": SESSION_SCHEMA, "id": str(row["id"]), "profile_id": str(row["profile_id"]), "target_id": str(row["target_id"]),
            "state": str(row["state"]), "active_queue_index": int(row["active_queue_index"]), "active_item_id": str(row["active_item_id"]),
            "work_id": str(row["work_id"]), "variant_id": str(row["variant_id"]), "source_id": str(row["source_id"]),
            "route": loads(row["route_json"], {}), "position_ms": int(row["position_ms"]), "duration_ms": int(row["duration_ms"]),
            "rate": float(row["rate"]), "volume": float(row["volume"]), "muted": bool(row["muted"]), "tracks": loads(row["tracks_json"], {}),
            "autoplay": bool(row["autoplay"]), "auto_fullscreen": bool(row["auto_fullscreen"]),
            "queue_source": loads(row["queue_source_json"], {}), "queue_revision": int(row["queue_revision"]),
            "command_revision": int(row["command_revision"]), "observed_command_revision": int(row["observed_command_revision"]),
            "endpoint_observation_revision": int(row["endpoint_observation_revision"]),
            "endpoint_last_seen_at": str(row["endpoint_last_seen_at"]), "endpoint_state": loads(row["endpoint_state_json"], {}),
            "sleep_timer_at": float(row["sleep_timer_at"]),
            "control_lease": {"actor_ref": str(row["control_actor_ref"]), "expires_at": float(row["control_lease_expires_at"]), "revision": int(row["control_lease_revision"])},
            "interruption": loads(row["interruption_json"], {}), "checkpoint_at": str(row["checkpoint_at"]), "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]), "revision": int(row["revision"]),
        }

    @staticmethod
    def _public_command(row: sqlite3.Row) -> dict[str, Any]:
        return {"schema": COMMAND_SCHEMA, "sequence": int(row["sequence"]), "id": str(row["id"]), "idempotency_key": str(row["idempotency_key"]), "session_id": str(row["session_id"]), "target_id": str(row["target_id"]), "actor_ref": str(row["actor_ref"]), "command": str(row["command"]), "arguments": loads(row["arguments_json"], {}), "expected_revision": int(row["expected_revision"]), "resulting_revision": int(row["resulting_revision"]), "command_revision": int(row["command_revision"]), "status": str(row["status"]), "created_at": str(row["created_at"]), "acknowledged_at": str(row["acknowledged_at"]), "result": loads(row["result_json"], {})}

    @staticmethod
    def _public_settings(row: sqlite3.Row) -> dict[str, Any]:
        return {"profile_id": str(row["profile_id"]), "target_id": str(row["target_id"]), "autoplay": bool(row["autoplay"]), "auto_fullscreen": bool(row["auto_fullscreen"]), "background_audio": bool(row["background_audio"]), "video_close_policy": str(row["video_close_policy"]), "preferred_quality": str(row["preferred_quality"]), "audio_language": str(row["audio_language"]), "subtitle_language": str(row["subtitle_language"]), "preferred_rate": float(row["preferred_rate"]), "resume_after_reconnect": bool(row["resume_after_reconnect"]), "checkpoint_interval_seconds": int(row["checkpoint_interval_seconds"]), "revision": int(row["revision"]), "updated_at": str(row["updated_at"])}
