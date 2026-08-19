from __future__ import annotations

import pytest

from research.orchestrator import ResearchOrchestrator
from research.repository import OrchestratorRepository


def _plan() -> dict:
    return {
        "dataset": {
            "logical_name": "immutable fixture dataset",
            "policy_digest": "sha256:" + "d" * 64,
            "split_strategy": "One predeclared train and validation partition.",
            "evaluation_seal": "The test split remains sealed until explicit unblinding.",
        },
        "operators": {
            "arms": [
                {"id": "baseline", "label": "Baseline", "role": "baseline", "specification": "Fixed baseline operator for paired execution."},
                {"id": "candidate", "label": "Candidate", "role": "intervention", "specification": "Candidate operator at the same intervention boundary."},
            ]
        },
        "execution": {
            "cpu-smoke": {
                "evidence_class": "workflow_smoke",
                "epochs": 3,
                "seeds": [17],
                "device": "cpu",
                "max_wall_time_minutes": 30,
                "inference_allowed": False,
            },
            "paired-series": {
                "evidence_class": "confirmatory",
                "epochs": 30,
                "seeds": [17, 23],
                "device": "cpu",
                "max_wall_time_minutes": 360,
                "inference_allowed": True,
            },
        },
        "randomization": {
            "unit": "seed",
            "named_streams": ["initialization", "sampling", "augmentation", "analysis"],
            "invariant_fields": ["initial state"],
            "varied_fields": ["operator"],
        },
        "analysis": {
            "primary_metric": "validation accuracy",
            "primary_estimand": "paired_accuracy_delta",
            "primary_contrast": {"minuend": "candidate", "subtrahend": "baseline"},
            "uncertainty": {"method": "paired bootstrap"},
            "stopping_rule": {"kind": "fixed_budget"},
        },
        "runner_contract": {
            "result_record": {
                "primary_metric_path": "primary_metric",
                "step_path": "step",
                "pairing_identity_path": "pairing_identity_digest",
            }
        },
    }


def _splits() -> dict:
    dataset = "sha256:" + "a" * 64
    return {
        "ready": True,
        "split_bindings": {
            "validation": {"digest": "sha256:" + "1" * 64, "dataset_digest": dataset, "locator": "dataset:validation", "sealed": False},
            "robustness": {"digest": "sha256:" + "2" * 64, "dataset_digest": dataset, "locator": "dataset:robustness", "sealed": False},
            "test": {"digest": "sha256:" + "3" * 64, "dataset_digest": dataset, "locator": "secret-ref:test", "sealed": True},
        },
    }


def test_experiment_plan_projects_without_scientific_inference_or_provider_heuristics() -> None:
    splits = ResearchOrchestrator._validated_split_bindings(_splits())
    conditions = ResearchOrchestrator._manager_conditions(
        _plan(),
        runner_id="direction_skill",
        dataset_digest=splits["validation"]["dataset_digest"],
    )

    assert set(conditions["execution"]) == {"preflight", "confirmatory"}
    assert conditions["execution"]["preflight"]["source_stage_id"] == "cpu-smoke"
    assert conditions["execution"]["preflight"]["epochs"] == 3
    assert conditions["analysis"]["primary_contrast"] == {"minuend": "candidate", "subtrahend": "baseline"}
    assert conditions["analysis"]["primary_metric"] == "validation accuracy"
    assert conditions["analysis"]["result_metric_path"] == "primary_metric"
    assert conditions["analysis"]["result_step_path"] == "step"
    assert conditions["analysis"]["initialization_digest_path"] == "pairing_identity_digest"
    assert conditions["runner"] == {
        "provider": "direction_skill",
        "contract": "adaos.research.runner.v1",
        "data_owner": "direction_skill",
    }


def test_legacy_plan_without_canonical_result_record_fails_closed() -> None:
    plan = _plan()
    plan["runner_contract"].pop("result_record")
    with pytest.raises(ValueError, match="canonical result_record"):
        ResearchOrchestrator._manager_conditions(
            plan,
            runner_id="direction_skill",
            dataset_digest="sha256:" + "a" * 64,
        )


def test_study_split_admission_fails_closed_on_alias_or_unsealed_test() -> None:
    value = _splits()
    value["split_bindings"]["test"]["sealed"] = False
    with pytest.raises(ValueError, match="must be sealed"):
        ResearchOrchestrator._validated_split_bindings(value)

    value = _splits()
    value["split_bindings"]["robustness"]["digest"] = value["split_bindings"]["validation"]["digest"]
    with pytest.raises(ValueError, match="must be distinct"):
        ResearchOrchestrator._validated_split_bindings(value)


def test_release_and_external_activity_bindings_are_idempotent_and_exact() -> None:
    repository = OrchestratorRepository()
    repository.initialize("direction", "Direction")
    track = repository.create_track(
        "direction",
        "direction.task-001",
        track_id="direction.task-001.track-001",
        title="Primary",
        project_ref="project:direction_implementation",
        primary_target_ref="skill:direction",
    )
    repository.bind_track_development(
        track["track_id"],
        project_ref="project:direction_implementation",
        primary_target_ref="skill:direction",
        development_session_id="dev-session-1",
    )
    candidate_digest = "sha256:" + "4" * 64
    trial = repository.bind_track_release(
        track["track_id"], candidate_release_digest=candidate_digest
    )
    assert trial["status"] == "trial_ready"
    released = repository.bind_track_release(
        track["track_id"],
        candidate_release_digest=candidate_digest,
        project_release_ref=f"project-release:direction_implementation:{candidate_digest}",
        project_release_digest=candidate_digest,
    )
    assert released["status"] == "release_ready"
    with pytest.raises(ValueError, match="differs"):
        repository.bind_track_release(
            track["track_id"],
            candidate_release_digest=candidate_digest,
            project_release_ref="project-release:direction_implementation:other",
            project_release_digest="sha256:" + "5" * 64,
        )

    first = repository.activity(
        "direction",
        "implementation",
        "working",
        "Builder is working.",
        origin="skill:builder_sdk_control_skill",
        source_event_id="builder-event-1",
    )
    replay = repository.activity(
        "direction",
        "implementation",
        "working",
        "This replay must not append.",
        origin="skill:builder_sdk_control_skill",
        source_event_id="builder-event-1",
    )
    assert replay["event_id"] == first["event_id"]
    assert len(repository.activities("direction")) == 1
