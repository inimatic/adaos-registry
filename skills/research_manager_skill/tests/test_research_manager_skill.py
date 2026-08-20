from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

import research.manager as manager_module
from research.contracts import ResearchRecord, identity
from research.manager import ResearchManager
from research.runner_contract import descriptor as runner_contract_descriptor
from research.tracker import MlflowTracker, TrackerConflict
from research.workflow import TRANSITIONS
from migrations.data_migration import migrate as migrate_runtime_data


def test_fixture_timeout_cancels_attempt_and_reports_terminal_diagnostics(monkeypatch) -> None:
    pending = SimpleNamespace(attempt_id="attempt-timeout", status="running", terminal=False)
    cancelled = SimpleNamespace(
        attempt_id="attempt-timeout",
        status="cancelled",
        terminal=True,
        to_dict=lambda: {
            "attempt_id": "attempt-timeout",
            "status": "cancelled",
            "failure": {"reason": "operator_cancelled"},
            "last_heartbeat_at": "2026-08-18T00:00:00+00:00",
        },
    )
    cancelled_ids: list[str] = []
    monkeypatch.setattr(
        manager_module,
        "cancel_execution",
        lambda attempt_id: cancelled_ids.append(attempt_id) or cancelled,
    )

    with pytest.raises(TimeoutError, match='"status": "cancelled"'):
        ResearchManager._await_terminal_attempt(pending, timeout_s=0)
    assert cancelled_ids == ["attempt-timeout"]


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


def _realization() -> dict[str, str]:
    return {
        "direction_ref": "research-direction:tlp",
        "task_ref": "research-task:tlp.task-001",
        "compilation_ref": "research-compilation:tlp.task-001:1",
        "compilation_digest": "sha256:" + "4" * 64,
        "implementation_track_ref": "implementation-track:tlp.task-001.track-001",
        "development_session_id": "development-session.test",
        "project_release_ref": "project-release:tlp:0.1.0",
        "project_release_digest": "sha256:" + "5" * 64,
        "runner_ref": "skill:tlp_generated_runner",
        "runner_contract": "adaos.research.runner.v1",
    }


def _acceptance_plan() -> dict:
    return {
        "digest": "sha256:" + "d" * 64,
        "dataset": {
            "id": "stl10_torchvision",
            "logical_name": "STL-10",
            "policy_digest": "sha256:" + "e" * 64,
            "split_strategy": "fixed",
            "evaluation_seal": "sealed",
        },
        "operators": {
            "arms": [
                {"id": "maxpool", "role": "baseline"},
                {"id": "tlp", "role": "intervention"},
            ]
        },
        "execution": {
            "stage_smoke_cpu": {
                "device": "cpu",
                "epochs": 3,
                "evidence_class": "workflow_smoke",
                "inference_allowed": False,
                "max_wall_time_minutes": 60,
                "seeds": [17],
                "network_mode": "offline",
                "workload": {
                    "mode": "bounded",
                    "limits": [
                        {"name": "train_samples", "maximum": 128, "unit": "samples"}
                    ],
                },
                "input_policy": {
                    "source": "deterministic_contract_fixture",
                    "readiness": "required_before_execution",
                    "sampling": "deterministic_seeded",
                },
            }
        },
        "randomization": {
            "named_streams": ["initialization", "sampling", "augmentation", "analysis"],
            "unit": "seed",
            "invariant_fields": ["initialization"],
            "varied_fields": ["pool2"],
        },
        "analysis": {
            "primary_metric": "accuracy",
            "primary_estimand": "paired delta",
            "primary_contrast": {"minuend": "tlp", "subtrahend": "maxpool"},
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


def _acceptance_envelope(profile: str) -> dict:
    compilation_digest = "sha256:" + "a" * 64
    brief_digest = "sha256:" + "b" * 64
    consumer_contract = runner_contract_descriptor()
    return {
        "schema": "adaos.builder.acceptance_candidate.v1",
        "profile": profile,
        "development_session_id": "dev_acceptance",
        "project_ref": "project:tlp",
        "candidate_ref": "skill:tlp_runner",
        "candidate": {"id": "tlp_runner", "version": "0.1.0"},
        "contract_inputs": [
            {"kind": "research_compilation", "digest": compilation_digest},
            {"kind": "automation_brief", "digest": brief_digest},
            {"kind": "consumer_contract", "digest": consumer_contract["digest"]},
        ],
        "instructions": {
            "research_compilation": {
                "digest": compilation_digest,
                "facets": {"experiment_plan": {"payload": _acceptance_plan()}},
            },
            "automation_brief": {
                "digest": brief_digest,
                "compilation_digest": compilation_digest,
                "prototype_digest": "sha256:" + "c" * 64,
            },
            "consumer_contract": consumer_contract,
        },
    }


def _smoke_expected_outputs() -> list[str]:
    return list(
        runner_contract_descriptor()["workflow_smoke_evidence"][
            "required_expected_outputs"
        ]
    )


def test_development_traceability_acceptance_is_digest_bound() -> None:
    manager = ResearchManager()
    accepted = manager.validate_development_candidate(
        _acceptance_envelope("research.traceability")
    )
    assert accepted["ok"] is True

    drifted = _acceptance_envelope("research.traceability")
    drifted["contract_inputs"][0]["digest"] = "sha256:" + "f" * 64
    rejected = manager.validate_development_candidate(drifted)
    assert rejected["ok"] is False
    assert "research_compilation digest" in rejected["errors"][0]


def test_runner_consumer_contract_is_content_addressed_and_exact() -> None:
    contract = runner_contract_descriptor()
    identity = {key: item for key, item in contract.items() if key != "digest"}
    assert contract["digest"] == manager_module.digest(identity)
    assert contract["version"] == "1.5.0"
    assert set(contract["operations"]) == {
        "prepare_attempt",
        "collect_attempt",
        "verify_artifact",
        "dataset_status",
    }
    assert contract["operations"]["prepare_attempt"]["input_schema"]["required"] == [
        "request"
    ]
    prepare_contract = contract["operations"]["prepare_attempt"]
    profile_schema = prepare_contract["input_schema"]["properties"]["request"][
        "properties"
    ]["profile"]
    assert profile_schema["enum"] == ["preflight", "confirmatory"]
    assert prepare_contract["profile_mapping"]["preflight"] == {
        "required_evidence_class": "workflow_smoke",
        "scientific_stage_field": "profile_conditions.source_stage_id",
        "inference_allowed": False,
    }
    assert any(
        "workflow_smoke is not a valid request.profile" in invariant
        for invariant in prepare_contract["invariants"]
    )
    for operation in contract["operations"].values():
        jsonschema.Draft202012Validator.check_schema(operation["input_schema"])
        jsonschema.Draft202012Validator.check_schema(operation["output_schema"])
    smoke_contract = contract["workflow_smoke_evidence"]
    assert smoke_contract["required_expected_outputs"] == [
        "run_log.json",
        "evaluation_audit.json",
        "artifacts_index.json",
    ]
    for schema in smoke_contract["documents"].values():
        jsonschema.Draft202012Validator.check_schema(schema)
    assert "MUST NOT index itself" in smoke_contract["collection"]["index_boundary"]

    dataset_schema = contract["operations"]["dataset_status"]["output_schema"]
    split_values = {
        role: {**item, "sealed": role == "test"}
        for role, item in _splits().items()
    }
    jsonschema.validate(
        {
            "dataset_id": "stl10_torchvision",
            "ready": True,
            "execution_ready_without_network": True,
            "split_bindings": split_values,
        },
        dataset_schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "dataset_id": "stl10_torchvision",
                "ready": True,
                "execution_ready_without_network": True,
                "split_bindings": [
                    {"role": role, **item, "sealed": role == "test"}
                    for role, item in split_values.items()
                ],
            },
            dataset_schema,
        )


def test_development_consumer_rejects_symbolic_rng_seed_units() -> None:
    plan = _acceptance_plan()
    plan["execution"]["stage_smoke_cpu"]["seeds"] = ["S1"]

    with pytest.raises(ValueError, match="integer RNG seeds"):
        ResearchManager._acceptance_conditions(
            plan,
            runner_id="tlp_runner",
            dataset_digest="sha256:" + "a" * 64,
        )


def test_development_consumer_acceptance_invokes_exact_manager_abi(monkeypatch) -> None:
    from adaos.sdk.developer import validation as developer_validation

    invocations: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        developer_validation,
        "validate_skill",
        lambda *_args, **_kwargs: {"ok": True, "digest": "sha256:" + "1" * 64},
    )
    monkeypatch.setattr(
        developer_validation,
        "activate_skill",
        lambda project_id: {"ok": True, "project_id": project_id, "version": "0.1.0"},
    )

    split_values = {
        role: {
            **item,
            "sealed": role == "test",
        }
        for role, item in _splits().items()
    }

    def invoke(project_id: str, operation_id: str, arguments: dict, **_kwargs):
        invocations.append((operation_id, arguments))
        if operation_id == "dataset_status":
            return {
                "dataset_id": "stl10_torchvision",
                "ready": True,
                "execution_ready_without_network": True,
                "split_bindings": split_values,
            }
        assert operation_id == "prepare_attempt"
        request = arguments["request"]
        assert request["profile"] == "preflight"
        assert request["seed"] == 17
        assert request["arm"]["id"] == "maxpool"
        return {
            "contract": "adaos.research.runner.v1",
            "provider_id": project_id,
            "package_ref": {
                "uri": "skill-data:files/acceptance/package.json",
                "digest": "sha256:" + "2" * 64,
                "size_bytes": 42,
                "media_type": "application/json",
                "owner_ref": f"skill:{project_id}",
            },
            "command": [sys.executable, "runner.py"],
            "working_directory": "skill-data:files/acceptance",
            "code_digest": "sha256:" + "3" * 64,
            "environment_digest": "sha256:" + "4" * 64,
            "output_ref": "skill-data:files/acceptance/output",
            "spec_id": "acceptance-spec",
            "expected_outputs": _smoke_expected_outputs(),
        }

    monkeypatch.setattr(developer_validation, "invoke_skill", invoke)
    receipt = ResearchManager().validate_development_candidate(
        _acceptance_envelope("research.consumer-contracts")
    )
    assert not receipt["errors"], receipt["errors"]
    assert receipt["ok"] is True
    assert [item[0] for item in invocations] == ["dataset_status", "prepare_attempt"]
    assert receipt["evidence"]["scientific_execution_started"] is False


def test_development_consumer_acceptance_reads_public_compilation_projection(
    monkeypatch,
) -> None:
    from adaos.sdk.developer import validation as developer_validation

    monkeypatch.setattr(
        developer_validation,
        "validate_skill",
        lambda *_args, **_kwargs: {"ok": True, "digest": "sha256:" + "1" * 64},
    )
    monkeypatch.setattr(
        developer_validation,
        "activate_skill",
        lambda project_id: {"ok": True, "project_id": project_id, "version": "0.1.0"},
    )

    def invoke(project_id: str, operation_id: str, arguments: dict, **_kwargs):
        if operation_id == "dataset_status":
            return {
                "dataset_id": "stl10_torchvision",
                "ready": True,
                "execution_ready_without_network": True,
                "split_bindings": {
                    role: {**item, "sealed": role == "test"}
                    for role, item in _splits().items()
                },
            }
        return {
            "contract": "adaos.research.runner.v1",
            "provider_id": project_id,
            "package_ref": {
                "uri": "skill-data:files/acceptance/package.json",
                "digest": "sha256:" + "2" * 64,
                "size_bytes": 42,
                "media_type": "application/json",
                "owner_ref": f"skill:{project_id}",
            },
            "command": [sys.executable, "runner.py"],
            "working_directory": "skill-data:files/acceptance",
            "code_digest": "sha256:" + "3" * 64,
            "environment_digest": "sha256:" + "4" * 64,
            "output_ref": "skill-data:files/acceptance/output",
            "spec_id": "acceptance-spec",
            "expected_outputs": _smoke_expected_outputs(),
        }

    monkeypatch.setattr(developer_validation, "invoke_skill", invoke)
    envelope = _acceptance_envelope("research.consumer-contracts")
    compilation = envelope["instructions"]["research_compilation"]
    compilation["schema"] = "adaos.research.compilation_projection.v1"
    compilation["experiment_plan"] = compilation.pop("facets")["experiment_plan"]["payload"]

    receipt = ResearchManager().validate_development_candidate(envelope)

    assert receipt["ok"] is True, receipt["errors"]


def test_development_consumer_evaluation_runs_exact_collection_and_verifier_abi(
    monkeypatch,
) -> None:
    from adaos.sdk.developer import validation as developer_validation

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        developer_validation,
        "validate_skill",
        lambda *_args, **_kwargs: {"ok": True, "digest": "sha256:" + "1" * 64},
    )
    monkeypatch.setattr(
        developer_validation,
        "activate_skill",
        lambda project_id: {"ok": True, "project_id": project_id, "version": "0.1.0"},
    )
    split_values = {
        role: {**item, "sealed": role == "test"} for role, item in _splits().items()
    }
    smoke_outputs = _smoke_expected_outputs()
    indexed_output_names = smoke_outputs[:2]
    artifact_rows = [
        {
            "uri": f"skill-data:files/acceptance/{name}",
            "digest": "sha256:" + str(index) * 64,
            "size_bytes": 42,
            "media_type": "application/json",
            "owner_ref": "skill:tlp_runner",
            "kind": "workflow-smoke-evidence",
            "metadata": {"evidence_class": "workflow_smoke"},
        }
        for index, name in enumerate(indexed_output_names, start=5)
    ]
    smoke_documents = {
        "run_log.json": {
            "stage": "workflow_smoke",
            "device": "cpu",
            "epochs_completed": 3,
            "seeds": ["seed-17"],
            "inference_allowed": False,
            "evidence_class": "workflow_smoke",
            "workload": {
                "mode": "bounded",
                "limits": [
                    {"name": "train_samples", "maximum": 128, "unit": "samples"}
                ],
                "observed": {"train_samples": 128},
            },
            "input_policy": {
                "source": "deterministic_contract_fixture",
                "readiness": "required_before_execution",
                "sampling": "deterministic_seeded",
            },
            "network": {"mode": "offline", "accessed": False},
        },
        "evaluation_audit.json": {
            "per_stage": {"workflow_smoke": {"test_evaluations_count": 0}},
            "test_access": [],
        },
        "artifacts_index.json": {
            "files": [
                {
                    "path": name,
                    "digest": artifact["digest"],
                    "content_ref": artifact,
                }
                for name, artifact in zip(indexed_output_names, artifact_rows, strict=True)
            ]
        },
    }

    def invoke(project_id: str, operation_id: str, arguments: dict, **_kwargs):
        calls.append((operation_id, dict(arguments)))
        if operation_id == "dataset_status":
            return {
                "dataset_id": "stl10_torchvision",
                "ready": True,
                "execution_ready_without_network": True,
                "split_bindings": split_values,
            }
        if operation_id == "prepare_attempt":
            return {
                "contract": "adaos.research.runner.v1",
                "provider_id": project_id,
                "package_ref": {
                    "uri": "skill-data:files/acceptance/package.json",
                    "digest": "sha256:" + "2" * 64,
                    "size_bytes": 42,
                    "media_type": "application/json",
                    "owner_ref": f"skill:{project_id}",
                },
                "command": [sys.executable, str(Path(__file__).resolve())],
                "working_directory": str(Path(__file__).resolve().parent),
                "code_digest": "sha256:" + "3" * 64,
                "environment_digest": "sha256:" + "4" * 64,
                "output_ref": "skill-data:files/acceptance/output",
                "spec_id": "acceptance-spec",
                "expected_outputs": _smoke_expected_outputs(),
            }
        if operation_id == "collect_attempt":
            assert arguments == {"output_ref": "skill-data:files/acceptance/output"}
            return {
                "provider_id": project_id,
                "complete": True,
                "tracker_session_calls": 0,
                "observations": [],
                "artifacts": artifact_rows,
            }
        assert operation_id == "verify_artifact"
        assert arguments in [
            {"uri": item["uri"], "digest": item["digest"]}
            for item in artifact_rows
        ]
        return {"ok": True}

    monkeypatch.setattr(developer_validation, "invoke_skill", invoke)
    monkeypatch.setattr(
        developer_validation,
        "execute_spec",
        lambda *_args, **_kwargs: {
            "ok": True,
            "digest": "sha256:" + "8" * 64,
            "documents": smoke_documents,
            "outputs": [
                {"path": name, "digest": artifact["digest"]}
                for name, artifact in zip(indexed_output_names, artifact_rows, strict=True)
            ]
            + [{"path": "artifacts_index.json", "digest": "sha256:" + "7" * 64}],
        },
    )
    envelope = _acceptance_envelope("research.consumer-contracts")
    envelope["execute_workflow_smoke"] = True

    receipt = ResearchManager().validate_development_candidate(envelope)

    assert receipt["ok"] is True, receipt["errors"]
    assert [item[0] for item in calls] == [
        "dataset_status",
        "prepare_attempt",
        "collect_attempt",
        "verify_artifact",
        "verify_artifact",
    ]
    assert receipt["evidence"]["workflow_smoke_executed"] is True
    assert receipt["evidence"]["verified_artifacts"] == [
        {"ok": True},
        {"ok": True},
    ]


def test_workflow_smoke_index_rejects_self_referential_digest() -> None:
    self_ref = {
        "uri": "skill-data:files/acceptance/artifacts_index.json",
        "digest": "sha256:" + "7" * 64,
        "size_bytes": 42,
        "media_type": "application/json",
        "owner_ref": "skill:tlp_runner",
        "metadata": {"evidence_class": "workflow_smoke"},
    }
    with pytest.raises(ValueError, match="must not index itself"):
        ResearchManager._validate_workflow_smoke_evidence(
            trial={
                "documents": {
                    "run_log.json": {
                        "stage": "workflow_smoke",
                        "device": "cpu",
                        "epochs_completed": 3,
                        "seeds": ["seed-17"],
                        "inference_allowed": False,
                        "evidence_class": "workflow_smoke",
                        "workload": {
                            "mode": "bounded",
                            "limits": [
                                {"name": "train_samples", "maximum": 128, "unit": "samples"}
                            ],
                            "observed": {"train_samples": 128},
                        },
                        "input_policy": {
                            "source": "deterministic_contract_fixture",
                            "readiness": "required_before_execution",
                            "sampling": "deterministic_seeded",
                        },
                        "network": {"mode": "offline", "accessed": False},
                    },
                    "evaluation_audit.json": {
                        "per_stage": {
                            "workflow_smoke": {"test_evaluations_count": 0}
                        },
                        "test_access": [],
                    },
                    "artifacts_index.json": {
                        "files": [
                            {
                                "path": "artifacts_index.json",
                                "digest": self_ref["digest"],
                                "content_ref": self_ref,
                            }
                        ]
                    },
                },
                "outputs": [
                    {"path": "artifacts_index.json", "digest": self_ref["digest"]}
                ],
            },
            collected={"complete": True, "artifacts": [self_ref]},
            verified_artifacts=[{"ok": True}],
            expected_seed_labels=["seed-17"],
            expected_profile=_acceptance_plan()["execution"]["stage_smoke_cpu"],
        )


def test_compiled_study_binds_exact_realization_and_is_idempotent() -> None:
    manager = ResearchManager()
    suffix = f"compiled-{uuid.uuid4().hex}"
    request = {
        "title": f"Compiled research fixture {suffix}",
        "hypothesis": "The exact generated runner preserves the accepted protocol.",
        "protocol": {"dataset": "fixture", "paired": True},
        "analysis_plan": {"primary_metric": "accuracy", "paired": True},
        "splits": _splits(),
        "realization": _realization(),
        "mode": "confirmatory",
        "study_id": None,
        "idempotency_key": f"create-compiled:{suffix}",
    }
    created = manager.create_compiled_study(**request)
    assert manager.create_compiled_study(**request) == created
    realization = created["realization"]
    assert realization["kind"] == "study_realization"
    assert realization["study_id"] == created["study"]["record_id"]
    assert realization["payload"] == {
        "schema": "adaos.research.study_realization.v1",
        **_realization(),
    }
    status = manager.status(created["study"]["record_id"])
    assert status["counts"]["study_realization"] == 1
    assert status["realizations"] == [realization]
    assert any(
        item["event_type"] == "research.study.realization_bound"
        for item in status["events"]
    )


def test_compiled_study_rejects_incomplete_release_lineage() -> None:
    manager = ResearchManager()
    realization = _realization()
    realization["project_release_digest"] = "mutable"
    with pytest.raises(ValueError, match="project_release_digest"):
        manager.create_compiled_study(
            title="Rejected compiled study",
            hypothesis="An immutable release is required.",
            protocol={"dataset": "fixture"},
            analysis_plan={"primary_metric": "accuracy"},
            splits=_splits(),
            realization=realization,
            mode="confirmatory",
            study_id=None,
            idempotency_key=f"reject-compiled:{uuid.uuid4().hex}",
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
    compiled = manager.create_compiled_study(
        title="Schema-bound compiled study",
        hypothesis="The installed runner preserves the compiled contract.",
        protocol={"dataset": "fixture"},
        analysis_plan={"primary_metric": "accuracy"},
        splits=_splits(),
        realization=_realization(),
        mode="confirmatory",
        study_id=None,
        idempotency_key=f"schema-compiled:{uuid.uuid4().hex}",
    )
    jsonschema.Draft202012Validator(entity_schema).validate(compiled["realization"])
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
    assert first["experiment"]["payload"]["data_owner_skill_id"] == "fixture_data_skill"
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


def test_experiment_status_degrades_dependency_health_without_losing_core_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ResearchManager()
    created = _create(manager, f"status-degraded-{uuid.uuid4().hex}")
    study_id = created["study"]["record_id"]
    experiment_id = identity("experiment", {"study_id": study_id, "slug": "DEGRADED"})
    manager.create_experiment(
        study_id=study_id,
        slug="DEGRADED",
        title="Dependency degradation",
        purpose="Keep the control-plane read model available",
        conditions=_experiment_conditions(),
        experiment_id=experiment_id,
        idempotency_key=f"{experiment_id}:create",
    )

    def unavailable_runner(*_args, **_kwargs):
        raise TimeoutError("runner did not answer")

    class UnavailableTracker:
        def health(self):
            raise ConnectionError("tracker did not answer")

    monkeypatch.setattr("research.manager.invoke_skill", unavailable_runner)
    manager._trackers["local-tracker"] = UnavailableTracker()

    status = manager.experiment_status(experiment_id)

    assert status["schema"] == "adaos.research.experiment_workbench.v1"
    assert status["experiment"]["record_id"] == experiment_id
    assert status["lifecycle"]["state"] == "draft"
    assert status["dataset"]["state"] == "unavailable"
    assert status["dataset"]["error"]["type"] == "TimeoutError"
    assert status["tracker"]["state"] == "unavailable"
    assert status["tracker"]["error"]["type"] == "ConnectionError"


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
    draft = manager.describe_experiment(experiment_id, locale="ru", channel="voice")

    assert draft["schema"] == "adaos.scenario.guidance_projection.v1"
    assert draft["workflow"]["state"] == "draft"
    assert [item["id"] for item in draft["next_actions"]] == [
        "edit_conditions",
        "submit_review",
    ]
    assert draft["message"] == draft["speech_text"]

    filtered = manager.describe_experiment(
        experiment_id,
        locale="en",
        channel="web",
        available_actions=("submit_review",),
    )
    assert [item["id"] for item in filtered["next_actions"]] == ["submit_review"]
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
