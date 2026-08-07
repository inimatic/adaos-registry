"""Minimal typed tracker provider; MLflow is not required."""

from __future__ import annotations

import json
from typing import Any, Mapping

from research.contracts import canonical_json, identity, now
from research.repository import ResearchRepository


class LocalTracker:
    provider_id = "local-tracker"
    protocol_version = "1.0"

    def __init__(self, repository: ResearchRepository) -> None:
        self.repository = repository
        self.db = repository._db

    def register_run(
        self,
        *,
        run_id: str,
        study_id: str,
        trial_id: str,
        parameters: Mapping[str, Any],
        tags: Mapping[str, str],
    ) -> dict[str, Any]:
        existing = self.db.fetch_one("SELECT * FROM tracker_runs WHERE run_id=:run_id", {"run_id": run_id})
        if existing:
            return dict(existing)
        started_at = now()
        self.db.execute(
            "INSERT INTO tracker_runs(run_id, study_id, trial_id, status, parameters_json, tags_json, started_at, finalized_at) VALUES (:run_id, :study_id, :trial_id, 'running', :parameters, :tags, :started_at, NULL)",
            {"run_id": run_id, "study_id": study_id, "trial_id": trial_id, "parameters": canonical_json(parameters), "tags": canonical_json(tags), "started_at": started_at},
        )
        return {"run_id": run_id, "study_id": study_id, "trial_id": trial_id, "status": "running", "parameters": dict(parameters), "tags": dict(tags), "started_at": started_at}

    def observe(
        self,
        *,
        run_id: str,
        name: str,
        value: Any,
        split_role: str,
        step: int | None = None,
    ) -> dict[str, Any]:
        if split_role not in {"train", "validation", "robustness", "test"}:
            raise ValueError("unsupported observation split role")
        observation_id = identity(
            "observation",
            {"run_id": run_id, "name": name, "value": value, "split_role": split_role, "step": step},
        )
        recorded_at = now()
        self.db.execute(
            "INSERT INTO tracker_observations(observation_id, run_id, name, value_json, step, split_role, recorded_at) VALUES (:observation_id, :run_id, :name, :value, :step, :split_role, :recorded_at)",
            {"observation_id": observation_id, "run_id": run_id, "name": name, "value": canonical_json(value), "step": step, "split_role": split_role, "recorded_at": recorded_at},
        )
        return {"observation_id": observation_id, "run_id": run_id, "name": name, "value": value, "step": step, "split_role": split_role, "recorded_at": recorded_at}

    def finalize(self, run_id: str, status: str) -> dict[str, Any]:
        if status not in {"succeeded", "failed", "cancelled", "lost"}:
            raise ValueError("tracker final status is invalid")
        finalized_at = now()
        self.db.execute(
            "UPDATE tracker_runs SET status=:status, finalized_at=:finalized_at WHERE run_id=:run_id",
            {"run_id": run_id, "status": status, "finalized_at": finalized_at},
        )
        return self.export(run_id)

    def export(self, run_id: str) -> dict[str, Any]:
        run = self.db.fetch_one("SELECT * FROM tracker_runs WHERE run_id=:run_id", {"run_id": run_id})
        if not run:
            raise KeyError(run_id)
        observations = self.db.fetch_all(
            "SELECT observation_id, name, value_json, step, split_role, recorded_at FROM tracker_observations WHERE run_id=:run_id ORDER BY recorded_at, observation_id",
            {"run_id": run_id},
        )
        return {
            **dict(run),
            "parameters": json.loads(str(run["parameters_json"])),
            "tags": json.loads(str(run["tags_json"])),
            "observations": [
                {**dict(item), "value": json.loads(str(item["value_json"]))}
                for item in observations
            ],
            "provider": {"provider_id": self.provider_id, "protocol_version": self.protocol_version},
        }


__all__ = ["LocalTracker"]
