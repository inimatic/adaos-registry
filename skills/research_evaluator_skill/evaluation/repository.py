from __future__ import annotations

import json
from typing import Any, Mapping

from adaos.sdk.data.relational import RelationalMigration, RelationalStorageRequirements, database

from evaluation.contracts import canonical


MIGRATIONS = (
    RelationalMigration(
        version=1,
        name="research calibration evaluator ledger",
        idempotent=True,
        statements=(
            "CREATE TABLE calibration_tasks (task_id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE calibration_packets (packet_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, arm_id TEXT NOT NULL, attempt_index INTEGER NOT NULL, budget_view TEXT NOT NULL, digest TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE UNIQUE INDEX calibration_packet_attempt ON calibration_packets(task_id, arm_id, attempt_index, budget_view)",
            "CREATE TABLE calibration_results (result_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, arm_id TEXT NOT NULL, attempt_index INTEGER NOT NULL, budget_view TEXT NOT NULL, digest TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE UNIQUE INDEX calibration_result_attempt ON calibration_results(task_id, arm_id, attempt_index, budget_view)",
        ),
    ),
)


def db():
    value = database(
        "research_evaluator",
        requirements=RelationalStorageRequirements(
            durability="durable",
            transactions_required=True,
            json_required=False,
            backup_required=True,
            restore_required=True,
            rollback_policy="restore",
            locality="any",
            migration_owner="skill:research_evaluator_skill",
        ),
    )
    value.migrate(MIGRATIONS, staged=True)
    return value


class EvaluationRepository:
    def __init__(self) -> None:
        self._db = db()

    def put_task(self, task: Mapping[str, Any]) -> dict[str, Any]:
        existing = self._db.fetch_one("SELECT payload_json FROM calibration_tasks WHERE task_id=:task_id", {"task_id": task["task_id"]})
        if existing:
            value = json.loads(str(existing["payload_json"]))
            if value["digest"] != task["digest"]:
                raise ValueError("calibration task id is already frozen with another digest")
            return value
        self._db.execute(
            "INSERT INTO calibration_tasks(task_id, digest, payload_json, created_at) VALUES (:task_id, :digest, :payload_json, :created_at)",
            {"task_id": task["task_id"], "digest": task["digest"], "payload_json": canonical(task).decode("utf-8"), "created_at": task["frozen_at"]},
        )
        return dict(task)

    def find_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._db.fetch_one("SELECT payload_json FROM calibration_tasks WHERE task_id=:task_id", {"task_id": task_id})
        return json.loads(str(row["payload_json"])) if row else None

    def get_task(self, task_id: str) -> dict[str, Any]:
        row = self._db.fetch_one("SELECT payload_json FROM calibration_tasks WHERE task_id=:task_id", {"task_id": task_id})
        if not row:
            raise ValueError(f"calibration task {task_id!r} was not found")
        return json.loads(str(row["payload_json"]))

    def put_packet(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        existing = self._db.fetch_one(
            "SELECT payload_json FROM calibration_packets WHERE task_id=:task_id AND arm_id=:arm_id AND attempt_index=:attempt_index AND budget_view=:budget_view",
            {"task_id": packet["task_id"], "arm_id": packet["arm_id"], "attempt_index": packet["attempt_index"], "budget_view": packet["budget_view"]},
        )
        if existing:
            value = json.loads(str(existing["payload_json"]))
            if value["digest"] != packet["digest"]:
                raise ValueError("calibration packet attempt already exists with another digest")
            return value
        self._db.execute(
            "INSERT INTO calibration_packets(packet_id, task_id, arm_id, attempt_index, budget_view, digest, payload_json, created_at) VALUES (:packet_id, :task_id, :arm_id, :attempt_index, :budget_view, :digest, :payload_json, :created_at)",
            {**dict(packet), "payload_json": canonical(packet).decode("utf-8"), "created_at": packet["created_at"]},
        )
        return dict(packet)

    def get_packet(
        self,
        task_id: str,
        arm_id: str,
        attempt_index: int,
        budget_view: str,
    ) -> dict[str, Any]:
        row = self._db.fetch_one(
            "SELECT payload_json FROM calibration_packets WHERE task_id=:task_id AND arm_id=:arm_id AND attempt_index=:attempt_index AND budget_view=:budget_view",
            {
                "task_id": task_id,
                "arm_id": arm_id,
                "attempt_index": int(attempt_index),
                "budget_view": budget_view,
            },
        )
        if not row:
            raise ValueError("calibration packet was not prepared")
        return json.loads(str(row["payload_json"]))

    def packets(self, task_id: str, *, budget_view: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT payload_json FROM calibration_packets WHERE task_id=:task_id"
        params: dict[str, Any] = {"task_id": task_id}
        if budget_view:
            sql += " AND budget_view=:budget_view"
            params["budget_view"] = budget_view
        rows = self._db.fetch_all(sql + " ORDER BY budget_view, arm_id, attempt_index", params)
        return [json.loads(str(row["payload_json"])) for row in rows]

    def put_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        existing = self._db.fetch_one(
            "SELECT payload_json FROM calibration_results WHERE task_id=:task_id AND arm_id=:arm_id AND attempt_index=:attempt_index AND budget_view=:budget_view",
            {"task_id": result["task_id"], "arm_id": result["arm_id"], "attempt_index": result["attempt_index"], "budget_view": result["budget_view"]},
        )
        if existing:
            value = json.loads(str(existing["payload_json"]))
            if value["digest"] != result["digest"]:
                raise ValueError("calibration result attempt already exists with another digest")
            return value
        self._db.execute(
            "INSERT INTO calibration_results(result_id, task_id, arm_id, attempt_index, budget_view, digest, payload_json, created_at) VALUES (:result_id, :task_id, :arm_id, :attempt_index, :budget_view, :digest, :payload_json, :created_at)",
            {**dict(result), "payload_json": canonical(result).decode("utf-8"), "created_at": result["evaluated_at"]},
        )
        return dict(result)

    def find_result(
        self,
        task_id: str,
        arm_id: str,
        attempt_index: int,
        budget_view: str,
    ) -> dict[str, Any] | None:
        row = self._db.fetch_one(
            "SELECT payload_json FROM calibration_results WHERE task_id=:task_id AND arm_id=:arm_id AND attempt_index=:attempt_index AND budget_view=:budget_view",
            {
                "task_id": str(task_id),
                "arm_id": str(arm_id),
                "attempt_index": int(attempt_index),
                "budget_view": str(budget_view),
            },
        )
        return json.loads(str(row["payload_json"])) if row else None

    def results(self, task_id: str, *, budget_view: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT payload_json FROM calibration_results WHERE task_id=:task_id"
        params: dict[str, Any] = {"task_id": task_id}
        if budget_view:
            sql += " AND budget_view=:budget_view"
            params["budget_view"] = budget_view
        rows = self._db.fetch_all(sql + " ORDER BY budget_view, arm_id, attempt_index", params)
        return [json.loads(str(row["payload_json"])) for row in rows]


__all__ = ["EvaluationRepository", "MIGRATIONS", "db"]
