from __future__ import annotations

import pytest

from research.contracts import digest
from research import orchestrator as orchestrator_module
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
                "network_mode": "offline",
                "max_wall_time_minutes": 30,
                "workload": {
                    "mode": "bounded",
                    "limits": [
                        {"name": "examples", "maximum": 8, "unit": "items"}
                    ],
                },
                "input_policy": {
                    "source": "deterministic_contract_fixture",
                    "readiness": "required_before_execution",
                    "sampling": "deterministic_prefix",
                },
                "inference_allowed": False,
            },
            "paired-series": {
                "evidence_class": "confirmatory",
                "epochs": 30,
                "seeds": [17, 23],
                "device": "cpu",
                "network_mode": "offline",
                "max_wall_time_minutes": 360,
                "workload": {"mode": "full", "limits": []},
                "input_policy": {
                    "source": "accepted_dataset",
                    "readiness": "required_before_execution",
                    "sampling": "full",
                },
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
    assert conditions["execution"]["preflight"]["network_mode"] == "offline"
    assert conditions["execution"]["preflight"]["workload"] == {
        "mode": "bounded",
        "limits": [{"name": "examples", "maximum": 8, "unit": "items"}],
    }
    assert conditions["execution"]["preflight"]["input_policy"] == {
        "source": "deterministic_contract_fixture",
        "readiness": "required_before_execution",
        "sampling": "deterministic_prefix",
    }
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


def test_observed_builder_trial_is_adopted_only_with_complete_immutable_identity() -> None:
    orchestrator = ResearchOrchestrator(repository=object())
    candidate_id = "direction-0-1-0-" + "a" * 12
    release_digest = "sha256:" + "b" * 64
    package_digest = "sha256:" + "c" * 64
    orchestrator._invoke_skill = lambda *args, **kwargs: {
        "delivery": {
            "status": "trial",
            "candidate_id": candidate_id,
            "release_digest": release_digest,
            "package_digest": package_digest,
        },
        "governed": {"state": "trial_review"},
    }

    assert orchestrator._observed_builder_trial_identity("skill", "direction") == {
        "candidate_id": candidate_id,
        "release_digest": release_digest,
        "package_digest": package_digest,
        "version": None,
    }

    orchestrator._invoke_skill = lambda *args, **kwargs: {
        "delivery": {"status": "trial", "candidate_id": candidate_id},
        "governed": {"state": "trial_review"},
    }
    assert orchestrator._observed_builder_trial_identity("skill", "direction") is None


def test_consumer_contract_refresh_supersedes_only_the_development_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_contract = {
        "schema": "adaos.contract.operation_set.v1",
        "contract": "adaos.research.runner.v1",
        "version": "1.9.0",
        "operations": {},
    }
    old_contract["digest"] = digest(old_contract)
    current_contract = {
        "schema": "adaos.contract.operation_set.v1",
        "contract": "adaos.research.runner.v1",
        "version": "1.10.0",
        "operations": {},
    }
    current_contract["digest"] = digest(current_contract)
    brief = {"schema": "brief", "digest": "sha256:" + "b" * 64}
    compilation = {"schema": "compilation", "digest": "sha256:" + "c" * 64}
    previous_session = {
        "session_id": "dev_direction_old",
        "project_ref": "project:direction_implementation",
        "base_release": {"package_digest": "sha256:" + "d" * 64},
        "focus": {"ref": "skill:direction"},
        "targets": {
            "primary": [{"ref": "skill:direction"}],
            "secondary": [],
        },
        "context_members": [
            {"ref": "skill:research_manager_skill", "access": "read-only"}
        ],
        "artifact_inputs": [
            {
                "ref": "artifact://skill/direction/part0",
                "audience": "research.implementation",
                "manifest_digest": "sha256:" + "e" * 64,
            }
        ],
        "subject_refs": [{"kind": "research_task", "ref": "research-task:task-1"}],
        "contract_inputs": [
            {"kind": "research_compilation", "digest": compilation["digest"]},
            {"kind": "automation_brief", "digest": brief["digest"]},
            {"kind": "consumer_contract", "digest": old_contract["digest"]},
        ],
        "acceptance_profiles": ["research.consumer-contracts", "research.traceability"],
        "acceptance_requirements": [
            {
                "id": "research.consumer-contracts",
                "profile": "research.consumer-contracts",
                "provider_ref": "skill:research_manager_skill",
                "operation": "validate_development_candidate",
                "required": True,
            }
        ],
        "handoff": {
            "automation_brief_digest": brief["digest"],
            "research_prototype_digest": "sha256:" + "f" * 64,
            "prohibited_actions": ["Do not run scientific inference."],
        },
    }
    track = {
        "track_id": "task-1.track-001",
        "ref": "implementation-track:task-1.track-001",
        "primary_target_ref": "skill:direction",
    }
    state = {
        "direction": {"direction_id": "direction"},
        "selected_task": {"ref": "research-task:task-1"},
        "active_implementation_track": track,
        "development_session": previous_session,
    }

    class Repository:
        def __init__(self) -> None:
            self.bound: dict | None = None
            self.activity_record: tuple | None = None

        def bind_track_development(self, track_id: str, **kwargs: object) -> dict:
            self.bound = {"track_id": track_id, **kwargs}
            return {**track, **kwargs, "status": "development_ready"}

        def activity(self, *args: object, **kwargs: object) -> dict:
            self.activity_record = (args, kwargs)
            return {"event_id": "event-1"}

    repository = Repository()
    orchestrator = ResearchOrchestrator(repository=repository)
    monkeypatch.setattr(orchestrator, "get", lambda *args, **kwargs: state)
    monkeypatch.setattr(
        orchestrator,
        "_invoke_skill",
        lambda *args, **kwargs: current_contract,
    )
    created: dict = {}
    attached: list[tuple[str, str, dict]] = []

    def get_instruction(session_id: str, kind: str) -> dict:
        assert session_id == previous_session["session_id"]
        value = {
            "consumer_contract": old_contract,
            "automation_brief": brief,
            "research_compilation": compilation,
        }[kind]
        if kind == "automation_brief":
            value = {"ok": True, "instruction": {"kind": kind}, "value": value}
        return {
            "ok": True,
            "instruction": {"kind": kind},
            "value": value,
        }

    def create(project_id: str, **kwargs: object) -> dict:
        created.update({"project_id": project_id, **kwargs})
        return {
            "session": {
                **previous_session,
                "session_id": str(kwargs["session_id"]),
                "contract_inputs": kwargs["contract_inputs"],
            }
        }

    def attach_instruction(
        session_id: str,
        kind: str,
        value: dict,
        **_: object,
    ) -> dict:
        attached.append((session_id, kind, value))
        return {
            "session": {
                **previous_session,
                "session_id": session_id,
                "contract_inputs": created["contract_inputs"],
            }
        }

    monkeypatch.setattr(orchestrator_module.development_sessions, "get_instruction", get_instruction)
    monkeypatch.setattr(orchestrator_module.development_sessions, "create", create)
    monkeypatch.setattr(
        orchestrator_module.development_sessions,
        "attach_instruction",
        attach_instruction,
    )

    result = orchestrator.refresh_development_contract("direction", actor="system:test")

    assert result["reused"] is False
    assert result["previous_consumer_contract_digest"] == old_contract["digest"]
    assert result["consumer_contract_digest"] == current_contract["digest"]
    assert result["instruction_envelope_normalized"] is True
    assert created["project_id"] == "direction_implementation"
    assert created["artifact_sources"] == [
        {
            "skill_id": "direction",
            "group_id": "part0",
            "audience": "research.implementation",
        }
    ]
    assert next(
        item for item in created["contract_inputs"] if item["kind"] == "consumer_contract"
    )["digest"] == current_contract["digest"]
    assert created["acceptance_requirements"][0]["parameters"] == {
        "execute_workflow_smoke": True
    }
    assert [item[1] for item in attached] == [
        "automation_brief",
        "research_compilation",
        "consumer_contract",
    ]
    assert attached[0][2] == brief
    assert attached[1][2] == compilation
    assert attached[2][2] == current_contract
    assert repository.bound is not None
    assert repository.activity_record is not None
