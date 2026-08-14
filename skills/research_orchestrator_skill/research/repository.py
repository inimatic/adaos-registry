from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from adaos.sdk.data.relational import RelationalMigration, RelationalStorageRequirements, database

from research.contracts import canonical_json, now


MIGRATIONS = (
    RelationalMigration(
        version=1,
        name="research direction formulation ledger",
        idempotent=True,
        statements=(
            "CREATE TABLE research_directions (direction_id TEXT PRIMARY KEY, title TEXT NOT NULL, project_kind TEXT NOT NULL, status TEXT NOT NULL, generation INTEGER NOT NULL, current_bundle_digest TEXT, current_prototype_digest TEXT, accepted_prototype_digest TEXT, automation_brief_digest TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE research_prototypes (digest TEXT PRIMARY KEY, direction_id TEXT NOT NULL, revision INTEGER NOT NULL, parent_digest TEXT, source_bundle_digest TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE UNIQUE INDEX research_prototypes_revision ON research_prototypes(direction_id, revision)",
            "CREATE TABLE research_automation_briefs (digest TEXT PRIMARY KEY, direction_id TEXT NOT NULL, prototype_digest TEXT NOT NULL, source_bundle_digest TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE research_activity (event_id TEXT PRIMARY KEY, direction_id TEXT NOT NULL, seq INTEGER NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE UNIQUE INDEX research_activity_seq ON research_activity(direction_id, seq)",
            "CREATE TABLE research_commands (idempotency_key TEXT PRIMARY KEY, command_name TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)",
        ),
    ),
    RelationalMigration(
        version=2,
        name="durable staged formulation artifacts",
        idempotent=True,
        statements=(
            "CREATE TABLE research_formulation_stages (run_id TEXT NOT NULL, direction_id TEXT NOT NULL, stage_index INTEGER NOT NULL, stage_name TEXT NOT NULL, status TEXT NOT NULL, input_digest TEXT NOT NULL, output_digest TEXT, payload_json TEXT NOT NULL, telemetry_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id, stage_name))",
            "CREATE INDEX research_formulation_direction ON research_formulation_stages(direction_id, created_at)",
        ),
    ),
)


def db():
    value = database(
        "research_orchestrator",
        requirements=RelationalStorageRequirements(
            durability="durable",
            transactions_required=True,
            json_required=False,
            backup_required=True,
            restore_required=True,
            rollback_policy="restore",
            locality="any",
            migration_owner="skill:research_orchestrator_skill",
        ),
    )
    value.migrate(MIGRATIONS, staged=True)
    return value


class OrchestratorRepository:
    def __init__(self) -> None:
        self._db = db()

    @staticmethod
    def _direction(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "direction_id": str(row["direction_id"]),
            "title": str(row["title"]),
            "project_kind": str(row["project_kind"]),
            "status": str(row["status"]),
            "generation": int(row["generation"]),
            "current_bundle_digest": row.get("current_bundle_digest"),
            "current_prototype_digest": row.get("current_prototype_digest"),
            "accepted_prototype_digest": row.get("accepted_prototype_digest"),
            "automation_brief_digest": row.get("automation_brief_digest"),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def initialize(self, direction_id: str, title: str) -> dict[str, Any]:
        existing = self.get_direction(direction_id)
        if existing:
            return existing
        timestamp = now()
        self._db.execute(
            "INSERT INTO research_directions(direction_id, title, project_kind, status, generation, current_bundle_digest, current_prototype_digest, accepted_prototype_digest, automation_brief_digest, created_at, updated_at) VALUES (:direction_id, :title, 'skill', 'intake', 0, NULL, NULL, NULL, NULL, :created_at, :updated_at)",
            {"direction_id": direction_id, "title": title, "created_at": timestamp, "updated_at": timestamp},
        )
        return dict(self.get_direction(direction_id) or {})

    def get_direction(self, direction_id: str) -> dict[str, Any] | None:
        row = self._db.fetch_one("SELECT * FROM research_directions WHERE direction_id=:direction_id", {"direction_id": direction_id})
        return self._direction(row)

    def list_directions(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._db.fetch_all(
            "SELECT * FROM research_directions ORDER BY updated_at DESC, direction_id",
            {},
        )[: max(1, min(int(limit), 5000))]
        return [value for row in rows if (value := self._direction(row)) is not None]

    def set_bundle(self, direction_id: str, bundle_digest: str) -> dict[str, Any]:
        self._db.execute(
            "UPDATE research_directions SET current_bundle_digest=:bundle, status='formulation', generation=generation+1, updated_at=:updated_at WHERE direction_id=:direction_id",
            {"direction_id": direction_id, "bundle": bundle_digest, "updated_at": now()},
        )
        return dict(self.get_direction(direction_id) or {})

    def put_prototype(self, direction_id: str, prototype: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(prototype)
        with self._db.transaction() as tx:
            existing = tx.fetch_one("SELECT payload_json FROM research_prototypes WHERE digest=:digest", {"digest": value["digest"]})
            if existing:
                return json.loads(str(existing["payload_json"]))
            tx.execute(
                "INSERT INTO research_prototypes(digest, direction_id, revision, parent_digest, source_bundle_digest, payload_json, created_at) VALUES (:digest, :direction_id, :revision, :parent_digest, :source_bundle_digest, :payload_json, :created_at)",
                {
                    "digest": value["digest"], "direction_id": direction_id, "revision": value["revision"],
                    "parent_digest": value.get("parent_digest"), "source_bundle_digest": value["source_bundle_digest"],
                    "payload_json": canonical_json(value), "created_at": value["created_at"],
                },
            )
            tx.execute(
                "UPDATE research_directions SET current_prototype_digest=:digest, current_bundle_digest=:bundle, status='formulation', generation=generation+1, updated_at=:updated_at WHERE direction_id=:direction_id",
                {"digest": value["digest"], "bundle": value["source_bundle_digest"], "updated_at": now(), "direction_id": direction_id},
            )
        return value

    def get_prototype(self, digest: str | None) -> dict[str, Any] | None:
        if not digest:
            return None
        row = self._db.fetch_one("SELECT payload_json FROM research_prototypes WHERE digest=:digest", {"digest": digest})
        return json.loads(str(row["payload_json"])) if row else None

    def list_prototypes(self, direction_id: str) -> list[dict[str, Any]]:
        rows = self._db.fetch_all("SELECT payload_json FROM research_prototypes WHERE direction_id=:direction_id ORDER BY revision", {"direction_id": direction_id})
        return [json.loads(str(row["payload_json"])) for row in rows]

    def accept(self, direction_id: str, *, expected_generation: int, prototype: Mapping[str, Any], brief: Mapping[str, Any]) -> dict[str, Any]:
        with self._db.transaction() as tx:
            row = tx.fetch_one("SELECT generation, accepted_prototype_digest FROM research_directions WHERE direction_id=:direction_id", {"direction_id": direction_id})
            if not row:
                raise ValueError("research direction is not initialized")
            if row.get("accepted_prototype_digest"):
                if str(row["accepted_prototype_digest"]) != str(prototype["digest"]):
                    raise ValueError("another ResearchPrototype is already accepted")
                existing = tx.fetch_one("SELECT payload_json FROM research_automation_briefs WHERE direction_id=:direction_id", {"direction_id": direction_id})
                return json.loads(str(existing["payload_json"])) if existing else dict(brief)
            if int(row["generation"]) != int(expected_generation):
                raise ValueError(f"stale generation: expected {expected_generation}, current {row['generation']}")
            tx.execute(
                "INSERT INTO research_automation_briefs(digest, direction_id, prototype_digest, source_bundle_digest, payload_json, created_at) VALUES (:digest, :direction_id, :prototype_digest, :source_bundle_digest, :payload_json, :created_at)",
                {
                    "digest": brief["digest"], "direction_id": direction_id, "prototype_digest": prototype["digest"],
                    "source_bundle_digest": prototype["source_bundle_digest"], "payload_json": canonical_json(brief), "created_at": brief["created_at"],
                },
            )
            tx.execute(
                "UPDATE research_directions SET accepted_prototype_digest=:prototype, automation_brief_digest=:brief, status='handoff_ready', generation=generation+1, updated_at=:updated_at WHERE direction_id=:direction_id",
                {"prototype": prototype["digest"], "brief": brief["digest"], "updated_at": now(), "direction_id": direction_id},
            )
        return dict(brief)

    def get_brief(self, digest: str | None) -> dict[str, Any] | None:
        if not digest:
            return None
        row = self._db.fetch_one("SELECT payload_json FROM research_automation_briefs WHERE digest=:digest", {"digest": digest})
        return json.loads(str(row["payload_json"])) if row else None

    def activity(self, direction_id: str, stage: str, status: str, message: str, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._db.transaction() as tx:
            row = tx.fetch_one("SELECT COALESCE(MAX(seq), 0) AS seq FROM research_activity WHERE direction_id=:direction_id", {"direction_id": direction_id})
            seq = int(row["seq"] if row else 0) + 1
            event_id = f"activity-{direction_id}-{seq:06d}"
            event = {"event_id": event_id, "direction_id": direction_id, "seq": seq, "stage": stage, "status": status, "message": message, "detail": dict(detail or {}), "created_at": now()}
            tx.execute(
                "INSERT INTO research_activity(event_id, direction_id, seq, stage, status, message, detail_json, created_at) VALUES (:event_id, :direction_id, :seq, :stage, :status, :message, :detail_json, :created_at)",
                {**event, "detail_json": canonical_json(event["detail"])},
            )
        return event

    def activities(self, direction_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._db.fetch_all(
            "SELECT event_id, direction_id, seq, stage, status, message, detail_json, created_at FROM research_activity WHERE direction_id=:direction_id ORDER BY seq DESC",
            {"direction_id": direction_id},
        )[: max(1, min(int(limit), 500))]
        return [{**dict(row), "detail": json.loads(str(row["detail_json"]))} for row in reversed(rows)]

    def put_formulation_stage(
        self,
        *,
        run_id: str,
        direction_id: str,
        stage_index: int,
        stage_name: str,
        status: str,
        input_digest: str,
        output_digest: str | None,
        payload: Mapping[str, Any] | None,
        telemetry: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        value = {
            "run_id": str(run_id),
            "direction_id": str(direction_id),
            "stage_index": int(stage_index),
            "stage_name": str(stage_name),
            "status": str(status),
            "input_digest": str(input_digest),
            "output_digest": str(output_digest) if output_digest else None,
            "payload": dict(payload or {}),
            "telemetry": dict(telemetry or {}),
            "created_at": now(),
        }
        parameters = {
            **value,
            "payload_json": canonical_json(value["payload"]),
            "telemetry_json": canonical_json(value["telemetry"]),
        }
        existing = self._db.fetch_one(
            "SELECT run_id FROM research_formulation_stages WHERE run_id=:run_id AND stage_name=:stage_name",
            {"run_id": value["run_id"], "stage_name": value["stage_name"]},
        )
        if existing:
            self._db.execute(
                "UPDATE research_formulation_stages SET status=:status, input_digest=:input_digest, output_digest=:output_digest, payload_json=:payload_json, telemetry_json=:telemetry_json, created_at=:created_at WHERE run_id=:run_id AND stage_name=:stage_name",
                parameters,
            )
        else:
            self._db.execute(
                "INSERT INTO research_formulation_stages(run_id, direction_id, stage_index, stage_name, status, input_digest, output_digest, payload_json, telemetry_json, created_at) VALUES (:run_id, :direction_id, :stage_index, :stage_name, :status, :input_digest, :output_digest, :payload_json, :telemetry_json, :created_at)",
                parameters,
            )
        return value

    def formulation_stages(self, direction_id: str, *, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if run_id:
            rows = self._db.fetch_all(
                "SELECT * FROM research_formulation_stages WHERE direction_id=:direction_id AND run_id=:run_id ORDER BY stage_index",
                {"direction_id": direction_id, "run_id": run_id},
            )
        else:
            rows = self._db.fetch_all(
                "SELECT * FROM research_formulation_stages WHERE direction_id=:direction_id ORDER BY created_at DESC, stage_index DESC",
                {"direction_id": direction_id},
            )[: max(1, min(int(limit), 500))]
        return [
            {
                **{key: value for key, value in dict(row).items() if key not in {"payload_json", "telemetry_json"}},
                "stage_index": int(row["stage_index"]),
                "payload": json.loads(str(row["payload_json"])),
                "telemetry": json.loads(str(row["telemetry_json"])),
            }
            for row in rows
        ]

    def once(self, key: str, command: str, operation: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
        previous = self._db.fetch_one("SELECT command_name, result_json FROM research_commands WHERE idempotency_key=:key", {"key": key})
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
            winner = self._db.fetch_one("SELECT command_name, result_json FROM research_commands WHERE idempotency_key=:key", {"key": key})
            if not winner or str(winner["command_name"]) != command:
                raise
            return json.loads(str(winner["result_json"]))
        return result


__all__ = ["MIGRATIONS", "OrchestratorRepository", "db"]
