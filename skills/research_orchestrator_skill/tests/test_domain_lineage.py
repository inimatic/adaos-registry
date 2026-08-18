from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from research.repository import OrchestratorRepository


SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def _validate(name: str, value: dict) -> None:
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(value)


def test_direction_task_track_and_alias_lineage_is_durable_and_typed() -> None:
    repository = OrchestratorRepository()
    direction = repository.initialize(
        "tlp_direction",
        "TLP direction",
        description="A bounded calibration direction",
        tags=["tlp", "calibration"],
        artifact_owner_skill_id="tlp_assets",
    )
    task = repository.get_task(direction["active_task_id"])
    assert task is not None
    track = repository.create_track(
        direction["direction_id"],
        task["task_id"],
        track_id=f"{task['task_id']}.c0.a1",
        title="C0_raw · attempt 1",
        condition_id="C0_raw",
        project_ref="project:tlp_cal_c0",
        primary_target_ref="skill:tlp_cal_c0",
        metadata={"packet_digest": "sha256:" + "1" * 64},
    )
    evaluated = repository.record_track_evaluation(
        track["track_id"],
        status="evaluated_failed",
        metadata={
            "packet_digest": "sha256:" + "1" * 64,
            "result_digest": "sha256:" + "2" * 64,
            "metrics": {"evidence_valid_completion": False},
        },
    )
    alias = repository.put_alias(
        "project:tlp_cal_c0",
        evaluated["ref"],
        {"kind": "legacy_calibration_candidate"},
    )

    _validate("research.direction.v1.schema.json", direction)
    _validate("research.task.v1.schema.json", task)
    _validate("research.implementation_track.v1.schema.json", evaluated)
    assert alias["canonical_ref"] == evaluated["ref"]
    assert repository.list_aliases(evaluated["ref"]) == [alias]
    assert repository.record_track_evaluation(
        evaluated["track_id"],
        status=evaluated["status"],
        metadata=evaluated["metadata"],
    )["revision"] == evaluated["revision"]
