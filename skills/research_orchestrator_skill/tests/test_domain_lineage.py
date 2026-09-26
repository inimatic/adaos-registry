from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from research.contracts import digest
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


def test_task_relations_active_projection_and_compilation_record_are_exact() -> None:
    repository = OrchestratorRepository()
    direction = repository.initialize("agenda", "Agenda")
    first = repository.get_task(direction["active_task_id"])
    assert first is not None
    follow_up = repository.create_task(
        "agenda",
        task_id="agenda.task-002",
        title="Independent confirmation",
        research_question="Does the effect replicate?",
        parent_task_id=first["task_id"],
        dependency_refs=[first["ref"]],
    )
    assert follow_up["dependency_refs"] == [first["ref"]]
    selected = repository.set_active_task("agenda", follow_up["task_id"])
    assert selected["active_task_id"] == follow_up["task_id"]
    assert selected["revision"] == selected["generation"] + 1

    payload = {
        "schema": "adaos.research.compilation_package.v1",
        "compilation_id": "agenda-task-002-r1",
        "source_bundle_digest": "sha256:" + "1" * 64,
        "readiness": {"decision": "ready_for_acceptance"},
    }
    payload["digest"] = digest(payload)
    record = repository.put_compilation(
        "agenda",
        follow_up["task_id"],
        payload,
        prototype_digest="sha256:" + "2" * 64,
        actor="user:test",
    )
    _validate("research.compilation.v1.schema.json", record)
    assert record["task_ref"] == follow_up["ref"]
    assert record["compilation_id"] == "agenda-task-002-r1"
    assert record["ref"] == "research-compilation:agenda-task-002-r1"
    assert record["payload"] == payload
    assert repository.latest_compilation_for_task(follow_up["task_id"]) == record


def test_legacy_prefixed_compilation_id_is_normalized_in_projection() -> None:
    repository = OrchestratorRepository()
    direction = repository.initialize("legacy", "Legacy")
    task = repository.get_task(direction["active_task_id"])
    assert task is not None
    payload = {
        "schema": "adaos.research.compilation_package.v1",
        "compilation_id": "research-compilation:legacy-r1",
        "source_bundle_digest": "sha256:" + "1" * 64,
    }
    payload["digest"] = digest(payload)

    record = repository.put_compilation(
        "legacy",
        task["task_id"],
        payload,
        prototype_digest="sha256:" + "2" * 64,
        actor="user:test",
    )

    assert record["compilation_id"] == "legacy-r1"
    assert record["ref"] == "research-compilation:legacy-r1"


def test_task_relations_cannot_cross_direction_boundary() -> None:
    repository = OrchestratorRepository()
    left = repository.initialize("left", "Left")
    repository.initialize("right", "Right")
    with pytest.raises(ValueError, match="inside research-direction:right"):
        repository.create_task(
            "right",
            task_id="right.task-002",
            title="Invalid dependency",
            dependency_refs=[f"research-task:{left['active_task_id']}"],
        )
