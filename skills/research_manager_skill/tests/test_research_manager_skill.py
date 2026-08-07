from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import jsonschema
import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from research.contracts import ResearchRecord, identity
from research.manager import ResearchManager
from research.workflow import TRANSITIONS
from migrations.data_migration import migrate as migrate_runtime_data


def _splits() -> dict[str, dict[str, str]]:
    return {
        "validation": {"digest": "sha256:" + "1" * 64, "dataset_digest": "sha256:" + "a" * 64, "locator": "dataset:validation"},
        "robustness": {"digest": "sha256:" + "2" * 64, "dataset_digest": "sha256:" + "a" * 64, "locator": "dataset:robustness"},
        "test": {"digest": "sha256:" + "3" * 64, "dataset_digest": "sha256:" + "a" * 64, "locator": "secret-ref:test"},
    }


def _create(manager: ResearchManager, suffix: str) -> dict:
    study_id = identity("study", {"fixture": suffix})
    return manager.create_study(
        title=f"Research fixture {suffix}",
        hypothesis="The paired max-plus fixture differs from its baseline.",
        protocol={"dataset": "fixture", "paired": True},
        analysis_plan={"primary_metric": "accuracy", "paired": True},
        splits=_splits(),
        mode="confirmatory",
        study_id=study_id,
        idempotency_key=f"create:{suffix}",
    )


def _advance(manager: ResearchManager, study_id: str, command: str, generation: int) -> dict:
    return manager.advance(
        study_id=study_id,
        command=command,
        expected_generation=generation,
        idempotency_key=f"{study_id}:{command}:{generation}",
        actor="user:test",
        evidence_refs=("evidence:test",) if command in {"unblind_test", "decide_claim"} else (),
    )


def test_end_to_end_research_kernel_survives_repository_reopen() -> None:
    manager = ResearchManager()
    suffix = f"e2e-{uuid.uuid4().hex}"
    created = _create(manager, suffix)
    study_id = created["study"]["record_id"]
    assert _create(manager, suffix) == created

    _advance(manager, study_id, "submit_protocol_review", 0)
    lock = _advance(manager, study_id, "lock_protocol", 1)
    assert lock["state"] == "locked"
    matrix = [
        {
            "pair_key": "pair-0",
            "operators": [
                {"name": "baseline", "variant": "flat-max"},
                {"name": "max_plus", "variant": "channelwise-centered"},
            ],
        }
    ]
    materialized = manager.materialize_trials(
        study_id=study_id,
        matrix=matrix,
        idempotency_key=f"{study_id}:trials",
    )
    _advance(manager, study_id, "approve_smoke", 2)
    first_trial = materialized["trials"][0]["record_id"]
    validation = manager.run_fixture(
        study_id=study_id,
        trial_id=first_trial,
        split_role="validation",
        seed=17,
        idempotency_key=f"{study_id}:attempt:validation",
    )
    assert validation["attempt"]["payload"]["status"] == "succeeded"
    assert validation["run"]["record_id"] != validation["attempt"]["record_id"]
    assert set(validation["run"]["payload"]["rng_streams"]) == {
        "initialization", "data_ordering", "augmentation", "operator_initialization", "analysis"
    }

    _advance(manager, study_id, "start_execution", 3)
    _advance(manager, study_id, "complete_execution", 4)
    with pytest.raises(PermissionError, match="sealed test"):
        manager.run_fixture(
            study_id=study_id,
            trial_id=first_trial,
            split_role="test",
            seed=17,
            idempotency_key=f"{study_id}:attempt:test:early",
        )
    unblinded = manager.unblind_test(
        study_id=study_id,
        expected_generation=5,
        idempotency_key=f"{study_id}:unblind",
        actor="user:reviewer",
        reason="QC passed",
        evidence_refs=(validation["attempt"]["digest"],),
    )
    assert unblinded["test_binding"]["sealed"] is False
    test_run = manager.run_fixture(
        study_id=study_id,
        trial_id=first_trial,
        split_role="test",
        seed=17,
        idempotency_key=f"{study_id}:attempt:test",
    )
    assert test_run["observations"][0]["payload"]["split_role"] == "test"
    _advance(manager, study_id, "run_analysis", 6)
    bundle = manager.export_evidence(study_id)
    verification = manager.verify_evidence(bundle["record_id"])
    assert verification["ok"] is True
    _advance(manager, study_id, "submit_claim_review", 7)
    completed = manager.decide_claim(
        study_id=study_id,
        verdict="inconclusive",
        rationale="The orchestration fixture is not scientific evidence of superiority.",
        bundle_id=bundle["record_id"],
        expected_generation=8,
        idempotency_key=f"{study_id}:claim",
        actor="user:reviewer",
    )
    assert completed["workflow"]["state"] == "complete"

    reopened = ResearchManager()
    status = reopened.status(study_id)
    assert status["workflow"] == {"study_id": study_id, "state": "complete", "generation": 9}
    assert status["counts"]["execution_attempt"] == 2
    with pytest.raises(ValueError, match="finalized"):
        reopened.repository.put(
            ResearchRecord("observation", identity("observation", {"late": True}), study_id, 1, {"late": True})
        )


def test_protocol_amendment_lineage_and_trial_disposition_are_explicit() -> None:
    manager = ResearchManager()
    created = _create(manager, f"amend-{uuid.uuid4().hex}")
    study_id = created["study"]["record_id"]
    _advance(manager, study_id, "submit_protocol_review", 0)
    _advance(manager, study_id, "lock_protocol", 1)
    trials = manager.materialize_trials(
        study_id=study_id,
        matrix=[{"pair_key": "p", "operators": [{"name": "baseline"}, {"name": "max_plus"}]}],
        idempotency_key=f"{study_id}:trials",
    )
    amended = manager.amend_protocol(
        study_id=study_id,
        content={"dataset": "fixture", "paired": True, "revision": 2},
        reason="Pre-registered correction",
        prior_trials="invalidate",
        expected_generation=2,
        idempotency_key=f"{study_id}:amend:v2",
        actor="user:reviewer",
    )
    assert amended["protocol"]["payload"]["parent_digest"] == created["protocol"]["digest"]
    assert amended["protocol"]["payload"]["version"] == 2
    assert amended["workflow"]["state"] == "protocol_review"
    assert len(amended["trial_dispositions"]) == len(trials["trials"])
    assert {item["payload"]["disposition"] for item in amended["trial_dispositions"]} == {"invalidate"}
    assert manager.amend_protocol(
        study_id=study_id,
        content={"dataset": "fixture", "paired": True, "revision": 2},
        reason="Pre-registered correction",
        prior_trials="invalidate",
        expected_generation=2,
        idempotency_key=f"{study_id}:amend:v2",
        actor="user:reviewer",
    ) == amended


def test_model_properties_reject_illegal_transitions_stale_generations_and_aliases() -> None:
    manager = ResearchManager()
    created = _create(manager, f"model-{uuid.uuid4().hex}")
    study_id = created["study"]["record_id"]
    for command in {value[1] for value in TRANSITIONS} - {"submit_protocol_review"}:
        with pytest.raises(ValueError, match="illegal research transition"):
            manager.advance(
                study_id=study_id,
                command=command,
                expected_generation=0,
                idempotency_key=f"illegal:{command}",
                actor="user:test",
                evidence_refs=("evidence:x",),
            )
    first = _advance(manager, study_id, "submit_protocol_review", 0)
    duplicate = _advance(manager, study_id, "submit_protocol_review", 0)
    assert duplicate == first
    transition_events = [
        item for item in manager.repository.events(study_id) if item["event_type"] == "research.workflow.transition"
    ]
    assert len(transition_events) == 1
    with pytest.raises(ValueError, match="stale workflow generation"):
        manager.advance(
            study_id=study_id,
            command="lock_protocol",
            expected_generation=0,
            idempotency_key=f"{study_id}:stale:lock",
            actor="user:test",
        )

    bad_splits = _splits()
    bad_splits["validation"]["digest"] = bad_splits["test"]["digest"]
    with pytest.raises(ValueError, match="must not alias"):
        manager.create_study(
            title="Aliased split fixture",
            hypothesis="invalid",
            protocol={},
            analysis_plan={},
            splits=bad_splits,
            mode="confirmatory",
            study_id=identity("study", {"fixture": f"alias-{uuid.uuid4().hex}"}),
            idempotency_key=f"create:alias:{uuid.uuid4().hex}",
        )


def test_versioned_entity_and_evidence_schemas_validate_representative_records() -> None:
    root = Path(__file__).resolve().parents[1]
    entity_schema = json.loads((root / "schemas" / "research.contracts.v1.schema.json").read_text(encoding="utf-8"))
    evidence_schema = json.loads((root / "schemas" / "evidence.manifest.v1.schema.json").read_text(encoding="utf-8"))
    manager = ResearchManager()
    created = _create(manager, f"schemas-{uuid.uuid4().hex}")
    jsonschema.Draft202012Validator(entity_schema).validate(created["study"])
    jsonschema.Draft202012Validator(entity_schema).validate(created["hypothesis"])
    jsonschema.Draft202012Validator(entity_schema).validate(created["protocol"])
    jsonschema.Draft202012Validator(entity_schema).validate(created["analysis_plan"])
    assert evidence_schema["$id"] == "adaos.research.evidence_manifest.v1"


def test_migration_fixture_declares_backward_forward_policy() -> None:
    root = Path(__file__).resolve().parents[1]
    fixture = json.loads((root / "schemas" / "migration-fixtures.json").read_text(encoding="utf-8"))
    assert fixture["fixtures"][-1]["expected"] == "idempotent-noop"
    assert set(fixture["compatibility"]) == {"patch", "minor", "major", "forward_reader", "backward_reader"}


def test_reserved_runtime_migration_imports_repository_from_installed_skill(tmp_path: Path) -> None:
    result = migrate_runtime_data(
        {
            "source_data_root": str(tmp_path / "missing-source"),
            "target_data_root": str(tmp_path / "target-data"),
        }
    )
    assert result["ok"] is True
    assert result["staged"] is True
    assert result["provider_id"] == "sqlite"
    assert result["health"]["ok"] is True
