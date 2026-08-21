"""Portable relational repository for immutable research records and events."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

from adaos.sdk.data.relational import RelationalMigration, RelationalStorageRequirements, database

from research.contracts import ResearchRecord, canonical_json, digest, identity, now


MIGRATIONS = (
    RelationalMigration(
        version=1,
        name="research immutable record store",
        idempotent=True,
        statements=(
            "CREATE TABLE research_records (kind TEXT NOT NULL, record_id TEXT NOT NULL, study_id TEXT NOT NULL, generation INTEGER NOT NULL, payload_json TEXT NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(kind, record_id))",
            "CREATE INDEX research_records_study_kind ON research_records(study_id, kind)",
            "CREATE TABLE research_events (event_id TEXT PRIMARY KEY, study_id TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE INDEX research_events_study ON research_events(study_id, created_at)",
            "CREATE TABLE research_commands (idempotency_key TEXT PRIMARY KEY, command_name TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)",
        ),
    ),
    RelationalMigration(
        version=2,
        name="typed local tracker",
        idempotent=True,
        statements=(
            "CREATE TABLE tracker_runs (run_id TEXT PRIMARY KEY, study_id TEXT NOT NULL, trial_id TEXT NOT NULL, status TEXT NOT NULL, parameters_json TEXT NOT NULL, tags_json TEXT NOT NULL, started_at TEXT NOT NULL, finalized_at TEXT)",
            "CREATE TABLE tracker_observations (observation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, name TEXT NOT NULL, value_json TEXT NOT NULL, step INTEGER, split_role TEXT NOT NULL, recorded_at TEXT NOT NULL)",
            "CREATE INDEX tracker_observations_run ON tracker_observations(run_id, name)",
        ),
    ),
    RelationalMigration(
        version=3,
        name="attempt-aware tracker contract journal",
        idempotent=True,
        statements=(
            "CREATE TABLE tracker_sessions (session_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, study_id TEXT NOT NULL, experiment_id TEXT NOT NULL, experiment_revision_id TEXT NOT NULL, trial_id TEXT NOT NULL, run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, status TEXT NOT NULL, parameters_json TEXT NOT NULL, tags_json TEXT NOT NULL, inputs_json TEXT NOT NULL, provider_binding_json TEXT NOT NULL, completeness_json TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT)",
            "CREATE UNIQUE INDEX tracker_sessions_attempt ON tracker_sessions(attempt_id)",
            "CREATE INDEX tracker_sessions_experiment ON tracker_sessions(experiment_id, opened_at)",
            "CREATE TABLE tracker_events (event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, event_kind TEXT NOT NULL, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, observed_at TEXT NOT NULL, ingested_at TEXT NOT NULL, delivery_state TEXT NOT NULL, provider_receipt_json TEXT NOT NULL)",
            "CREATE INDEX tracker_events_session ON tracker_events(session_id, observed_at, event_id)",
            "CREATE INDEX tracker_events_delivery ON tracker_events(delivery_state, ingested_at)",
        ),
    ),
    RelationalMigration(
        version=4,
        name="tracker provider links",
        idempotent=True,
        statements=(
            "CREATE TABLE tracker_provider_links (session_id TEXT NOT NULL, provider_id TEXT NOT NULL, link_json TEXT NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(session_id, provider_id))",
            "CREATE INDEX tracker_provider_links_state ON tracker_provider_links(provider_id, state, updated_at)",
        ),
    ),
)


def db():
    value = database(
        "research",
        requirements=RelationalStorageRequirements(
            durability="durable",
            transactions_required=True,
            json_required=False,
            backup_required=True,
            restore_required=True,
            rollback_policy="restore",
            locality="any",
            migration_owner="skill:research_manager_skill",
        ),
    )
    value.migrate(MIGRATIONS, staged=True)
    return value


class ResearchRepository:
    def __init__(self) -> None:
        self._db = db()

    def put(self, record: ResearchRecord) -> ResearchRecord:
        existing = self.get(record.kind, record.record_id)
        if existing is not None:
            if existing.digest != record.digest:
                raise ValueError(f"immutable {record.kind} record already exists with different content")
            return existing
        if record.kind not in {"evidence_bundle", "claim_decision"}:
            finalized_claim = any(
                str(item.payload.get("scope") or "study_claim") == "study_claim"
                for item in self.list(record.study_id, "evidence_bundle")
            )
            if finalized_claim:
                raise ValueError("claim evidence is finalized; research inputs are immutable")
        self._db.execute(
            "INSERT INTO research_records(kind, record_id, study_id, generation, payload_json, digest, created_at) VALUES (:kind, :record_id, :study_id, :generation, :payload_json, :digest, :created_at)",
            {
                "kind": record.kind,
                "record_id": record.record_id,
                "study_id": record.study_id,
                "generation": record.generation,
                "payload_json": canonical_json(record.payload),
                "digest": record.digest,
                "created_at": record.created_at,
            },
        )
        return record

    @staticmethod
    def _record(row: Mapping[str, Any]) -> ResearchRecord:
        record = ResearchRecord(
            kind=str(row["kind"]),
            record_id=str(row["record_id"]),
            study_id=str(row["study_id"]),
            generation=int(row["generation"]),
            payload=json.loads(str(row["payload_json"])),
            created_at=str(row["created_at"]),
        )
        if record.digest != str(row["digest"]):
            raise ValueError(f"research record digest mismatch: {record.record_id}")
        return record

    def get(self, kind: str, record_id: str) -> ResearchRecord | None:
        row = self._db.fetch_one(
            "SELECT kind, record_id, study_id, generation, payload_json, digest, created_at FROM research_records WHERE kind=:kind AND record_id=:record_id",
            {"kind": kind, "record_id": record_id},
        )
        return self._record(row) if row else None

    def list(self, study_id: str, kind: str | None = None) -> list[ResearchRecord]:
        statement = "SELECT kind, record_id, study_id, generation, payload_json, digest, created_at FROM research_records WHERE study_id=:study_id"
        parameters: dict[str, Any] = {"study_id": study_id}
        if kind:
            statement += " AND kind=:kind"
            parameters["kind"] = kind
        statement += " ORDER BY created_at, record_id"
        return [self._record(row) for row in self._db.fetch_all(statement, parameters)]

    def event(self, study_id: str, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        created_at = now()
        event_id = identity(
            "event",
            {"study_id": study_id, "event_type": event_type, "payload": dict(payload), "created_at": created_at},
        )
        self._db.execute(
            "INSERT INTO research_events(event_id, study_id, event_type, payload_json, created_at) VALUES (:event_id, :study_id, :event_type, :payload_json, :created_at)",
            {"event_id": event_id, "study_id": study_id, "event_type": event_type, "payload_json": canonical_json(payload), "created_at": created_at},
        )
        return {"event_id": event_id, "study_id": study_id, "event_type": event_type, "payload": dict(payload), "created_at": created_at}

    def events(self, study_id: str) -> list[dict[str, Any]]:
        return [
            {**dict(row), "payload": json.loads(str(row["payload_json"]))}
            for row in self._db.fetch_all(
                "SELECT event_id, study_id, event_type, payload_json, created_at FROM research_events WHERE study_id=:study_id ORDER BY created_at, event_id",
                {"study_id": study_id},
            )
        ]

    def once(self, key: str, command: str, operation: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
        previous = self._db.fetch_one(
            "SELECT command_name, result_json FROM research_commands WHERE idempotency_key=:key",
            {"key": key},
        )
        if previous:
            if str(previous["command_name"]) != command:
                raise ValueError("idempotency key is bound to another command")
            return json.loads(str(previous["result_json"]))
        result = dict(operation())
        try:
            self._db.execute(
                "INSERT INTO research_commands(idempotency_key, command_name, result_json, created_at) VALUES (:key, :command, :result, :created_at)",
                {"key": key, "command": command, "result": canonical_json(result), "created_at": now()},
            )
        except Exception:
            winner = self._db.fetch_one(
                "SELECT command_name, result_json FROM research_commands WHERE idempotency_key=:key",
                {"key": key},
            )
            if not winner or str(winner["command_name"]) != command:
                raise
            return json.loads(str(winner["result_json"]))
        return result


__all__ = ["MIGRATIONS", "ResearchRepository", "db"]
