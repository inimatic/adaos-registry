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
from research.tracker import MlflowTracker, TrackerConflict
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


def _experiment_conditions() -> dict:
    return {
        "schema": "adaos.research.tlp_experiment_conditions.v1",
        "dataset": {"name": "STL10", "version": "binary-2011", "split_seed": 7, "validation_per_class": 1, "download": False},
        "operators": {
            "location": "pool2",
            "arms": [
                {"id": "maxpool", "kind": "torch.nn.MaxPool2d"},
                {"id": "tlp", "kind": "centered-channelwise-max-plus", "initialization": "zero"},
            ],
        },
        "execution": {
            "preflight": {"epochs": 1, "seeds": [17], "batch_size": 10, "max_train_samples": 10, "max_validation_samples": 10},
            "confirmatory": {"epochs": 120, "seeds": [17, 29], "batch_size": 32},
        },
        "randomization": {
            "paired": True,
            "named_streams": ["initialization", "data_ordering", "augmentation", "operator_initialization", "analysis"],
        },
        "analysis": {
            "paired": True,
            "primary_metric": "validation.top1_accuracy",
            "primary_contrast": {"minuend": "tlp", "subtrahend": "maxpool"},
        },
        "tracker": {"provider": "local-tracker"},
        "runner": {
            "provider": "fixture_runner_skill",
            "contract": "adaos.research.runner.v1",
            "data_owner": "fixture_data_skill",
        },
    }


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


def test_experiment_revisions_lock_and_attempt_aware_tracker_contract() -> None:
    manager = ResearchManager()
    created = _create(manager, f"experiment-{uuid.uuid4().hex}")
    study_id = created["study"]["record_id"]
    experiment_id = identity("experiment", {"study_id": study_id, "slug": "E001"})
    first = manager.create_experiment(
        study_id=study_id,
        slug="E001",
        title="STL-10 pool2 control",
        purpose="Contract vertical",
        conditions=_experiment_conditions(),
        experiment_id=experiment_id,
        idempotency_key=f"{experiment_id}:create",
    )
    revised_conditions = _experiment_conditions()
    revised_conditions["execution"]["preflight"]["epochs"] = 3
    revised = manager.revise_experiment(
        experiment_id=experiment_id,
        expected_revision=1,
        conditions=revised_conditions,
        rationale="Use the three-epoch workflow profile",
        actor="user:test",
        idempotency_key=f"{experiment_id}:revise:2",
    )
    assert revised["revision"]["payload"]["parent_revision_id"] == first["revision"]["record_id"]
    assert revised["revision"]["payload"]["revision"] == 2
    manager.submit_experiment_review(
        experiment_id=experiment_id,
        expected_generation=0,
        idempotency_key=f"{experiment_id}:review",
        actor="user:test",
    )
    _advance(manager, study_id, "submit_protocol_review", 0)
    locked = manager.lock_experiment(
        experiment_id=experiment_id,
        expected_generation=1,
        idempotency_key=f"{experiment_id}:lock",
        actor="user:test",
    )
    assert locked["state"] == "locked"
    with pytest.raises(ValueError, match="cannot be rewritten"):
        manager.revise_experiment(
            experiment_id=experiment_id,
            expected_revision=2,
            conditions=revised_conditions,
            rationale="illegal rewrite",
            actor="user:test",
            idempotency_key=f"{experiment_id}:revise:illegal",
        )

    common = {
        "study_id": study_id,
        "experiment_id": experiment_id,
        "experiment_revision_id": revised["revision"]["record_id"],
        "trial_id": "trial.contract",
        "run_id": "run.contract",
        "parameters": {"epochs": 3},
        "tags": {"adaos.run_id": "run.contract"},
    }
    first_session = manager.tracker.open_session(
        session_id="session.contract.1", attempt_id="attempt.contract.1", **common
    )
    second_session = manager.tracker.open_session(
        session_id="session.contract.2", attempt_id="attempt.contract.2", **common
    )
    assert first_session["run_id"] == second_session["run_id"]
    assert first_session["attempt_id"] != second_session["attempt_id"]
    observation = {
        "metric": {"namespace": "tlp", "name": "top1_accuracy"},
        "value": 0.5,
        "value_type": "float",
        "split_role": "validation",
        "step": {"axis": "epoch", "value": 1},
        "producer": {"attempt_id": "attempt.contract.1", "sequence": 1},
    }
    receipt = manager.tracker.append_observations("session.contract.1", [observation])
    duplicate = manager.tracker.append_observations("session.contract.1", [observation])
    assert receipt["accepted"] == duplicate["duplicates"]
    conflicting = {**observation, "value": 0.75}
    with pytest.raises(TrackerConflict, match="conflict"):
        manager.tracker.append_observations("session.contract.1", [conflicting])
    exported = manager.tracker.close_session(
        "session.contract.1", "succeeded", {"observations_complete": True}
    )
    assert exported["session"]["attempt_id"] == "attempt.contract.1"
    assert exported["events"][0]["payload"]["metric"]["name"] == "top1_accuracy"


def test_experiment_guidance_is_localized_channel_neutral_and_workflow_aware() -> None:
    manager = ResearchManager()
    created = _create(manager, f"guidance-{uuid.uuid4().hex}")
    study_id = created["study"]["record_id"]
    experiment_id = identity("experiment", {"study_id": study_id, "slug": "GUIDE"})
    manager.create_experiment(
        study_id=study_id,
        slug="GUIDE",
        title="Guidance contract",
        purpose="Verify channel-neutral workflow guidance",
        conditions=_experiment_conditions(),
        experiment_id=experiment_id,
        idempotency_key=f"{experiment_id}:create",
    )
    assert first["experiment"]["payload"]["data_owner_skill_id"] == "fixture_data_skill"

    draft = manager.describe_experiment(experiment_id, locale="ru", channel="voice")

    assert draft["schema"] == "adaos.scenario.guidance_projection.v1"
    assert draft["workflow"]["state"] == "draft"
    assert [item["id"] for item in draft["next_actions"]] == [
        "edit_conditions",
        "submit_review",
    ]
    assert draft["message"] == draft["speech_text"]
    assert "Следующие шаги" in draft["speech_text"]

    manager.submit_experiment_review(
        experiment_id=experiment_id,
        expected_generation=0,
        idempotency_key=f"{experiment_id}:review",
        actor="user:test",
    )
    review = manager.describe_experiment(
        experiment_id,
        locale="en",
        channel="text",
        section="next_steps",
    )

    assert review["workflow"]["state"] == "review"
    assert [item["id"] for item in review["next_actions"]] == ["edit_conditions", "lock"]
    assert review["message"] == review["text"]


def test_mlflow_provider_projects_only_new_journal_events_per_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ResearchManager()
    provider = MlflowTracker(manager.repository, "http://127.0.0.1:18121")
    calls: list[tuple[str, dict | None]] = []
    remote_run_exists = False

    def request(path: str, payload: dict | None = None, *, method: str | None = None) -> dict:
        nonlocal remote_run_exists
        calls.append((path, payload))
        if path.startswith("/api/2.0/mlflow/experiments/get-by-name"):
            return {"experiment": {"experiment_id": "42"}}
        if path == "/api/2.0/mlflow/runs/search":
            return {"runs": [{"info": {"run_id": "mlflow-run-1"}}]} if remote_run_exists else {"runs": []}
        if path == "/api/2.0/mlflow/runs/create":
            remote_run_exists = True
            return {"run": {"info": {"run_id": "mlflow-run-1"}}}
        if path.startswith("/api/2.0/mlflow/runs/get"):
            return {"run": {"data": {"params": []}}}
        if path in {"/api/2.0/mlflow/runs/log-batch", "/api/2.0/mlflow/runs/update"}:
            return {}
        raise AssertionError(path)

    monkeypatch.setattr(provider, "_request", request)
    provider.open_session(
        session_id="session.mlflow.1",
        study_id="study.mlflow",
        experiment_id="experiment.mlflow",
        experiment_revision_id="revision.mlflow",
        trial_id="trial.mlflow",
        run_id="run.mlflow",
        attempt_id="attempt.mlflow.1",
        parameters={"epochs": 3},
        tags={"adaos.profile": "preflight"},
        inputs=({"kind": "dataset", "name": "STL10"},),
    )
    observation = {
        "metric": {"namespace": "tlp", "name": "top1_accuracy"},
        "value": 0.5,
        "value_type": "float",
        "split_role": "validation",
        "step": {"axis": "epoch", "value": 1},
        "producer": {"attempt_id": "attempt.mlflow.1", "sequence": 1},
    }
    first = provider.append_observations("session.mlflow.1", [observation])
    second = provider.append_observations("session.mlflow.1", [observation])
    assert first["projection"]["state"] == "delivered"
    assert second["duplicates"] == first["accepted"]
    metric_batches = [
        payload
        for path, payload in calls
        if path == "/api/2.0/mlflow/runs/log-batch" and payload and payload.get("metrics")
    ]
    assert len(metric_batches) == 1
    assert metric_batches[0]["metrics"][0]["key"] == "validation.tlp.top1_accuracy"
    exported = provider.export_session("session.mlflow.1")
    assert exported["session"]["provider_id"] == "mlflow"
    assert exported["events"][0]["delivery_state"] == "delivered"


def test_live_mlflow_provider_contract_when_endpoint_is_declared() -> None:
    import os

    endpoint = str(os.getenv("ADAOS_TEST_MLFLOW_URI") or "").strip()
    if not endpoint:
        pytest.skip("ADAOS_TEST_MLFLOW_URI is not configured")
    manager = ResearchManager()
    provider = MlflowTracker(manager.repository, endpoint)
    suffix = uuid.uuid4().hex
    session_id = f"session.live.{suffix}"
    attempt_id = f"attempt.live.{suffix}"
    provider.open_session(
        session_id=session_id,
        study_id=f"study.live.{suffix}",
        experiment_id=f"experiment.live.{suffix}",
        experiment_revision_id=f"revision.live.{suffix}",
        trial_id=f"trial.live.{suffix}",
        run_id=f"run.live.{suffix}",
        attempt_id=attempt_id,
        parameters={"epochs": 3, "operator": "tlp"},
        tags={"adaos.profile": "contract"},
    )
    receipt = provider.append_observations(
        session_id,
        [
            {
                "metric": {"namespace": "tlp", "name": "top1_accuracy"},
                "value": 0.5,
                "value_type": "float",
                "split_role": "validation",
                "step": {"axis": "epoch", "value": 1},
                "producer": {"attempt_id": attempt_id, "sequence": 1},
            }
        ],
    )
    assert receipt["projection"]["state"] == "delivered"
    closed = provider.close_session(
        session_id,
        "succeeded",
        {"observations_complete": True, "artifacts_complete": True},
    )
    assert closed["session"]["status"] == "succeeded"
    assert closed["events"][0]["provider_receipt"]["mlflow_run_id"]
