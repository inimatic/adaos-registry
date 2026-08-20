from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from evaluation.contracts import ARM_IDS, CALIBRATION_EXCLUSION_RULES, freeze_task
from evaluation.harness import build_recomputable_package, evaluate_candidate, prepare_arm, summarize
from evaluation.independent import build_independent_candidate
from handlers.main import _snapshot_hidden_inputs


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _contract_file(root: Path, name: str, token: str) -> tuple[Path, str]:
    declared = "sha256:" + token * 64
    path = root / name
    path.write_text(json.dumps({"digest": declared}), encoding="utf-8")
    return path, declared


@pytest.fixture
def task_value(tmp_path: Path) -> dict:
    review_path, review_digest = _contract_file(tmp_path, "review.json", "1")
    compilation_path, compilation_digest = _contract_file(tmp_path, "compilation.json", "2")
    brief_path, brief_digest = _contract_file(tmp_path, "brief.json", "3")
    fixture_path, fixture_digest = _contract_file(tmp_path, "conformance.json", "4")
    scaffold_path, scaffold_digest = _contract_file(tmp_path, "scaffold.json", "5")
    oracle = tmp_path / "oracle.md"
    oracle.write_text("Hidden expert oracle", encoding="utf-8")
    legacy = tmp_path / "legacy.py"
    legacy.write_text("ANSWER = True\n", encoding="utf-8")
    inputs = [
        {"input_id": "review", "kind": "reviewed_prose", "ref": "artifact://review", "digest": review_digest, "path": str(review_path), "visible_arms": ["C1_reviewed_prose"]},
        {"input_id": "compilation", "kind": "research_compilation", "ref": "instruction://compilation", "digest": compilation_digest, "path": str(compilation_path), "visible_arms": ["C2_staged", "C3_typed_execution", "C4_over_specified"]},
        {"input_id": "brief", "kind": "automation_brief", "ref": "instruction://brief", "digest": brief_digest, "path": str(brief_path), "visible_arms": ["C3_typed_execution", "C4_over_specified"]},
        {"input_id": "fixture", "kind": "conformance_fixture", "ref": "instruction://fixture", "digest": fixture_digest, "path": str(fixture_path), "visible_arms": ["C3_typed_execution", "C4_over_specified"]},
        {"input_id": "scaffold", "kind": "prescribed_scaffold", "ref": "instruction://scaffold", "digest": scaffold_digest, "path": str(scaffold_path), "visible_arms": ["C4_over_specified"]},
    ]
    by_arm = {
        arm_id: [item["input_id"] for item in inputs if arm_id in item["visible_arms"]]
        for arm_id in ARM_IDS
    }
    return {
        "schema_version": "1.2.0",
        "task_id": "tlp-calibration-v1",
        "title": "TLP clean research compilation calibration",
        "direction_skill_id": "tlp_research_03",
        "base_request": "Build a clean executable TLP experiment from the supplied source material without using hidden answers.",
        "artifact_groups": ["part0"],
        "expected_protocol_digest": "sha256:" + "a" * 64,
        "expected_smoke_profile": {
            "profile_id": "workflow_smoke",
            "stage_id": "workflow_smoke",
            "device": "cpu",
            "epochs": 3,
            "seeds": [17],
            "evidence_class": "workflow_smoke",
            "inference_allowed": False,
            "gpu_count": 0,
            "network_mode": "offline",
        },
        "agent_profile": {
            "provider": "openai-codex-cli",
            "model": "gpt-5.4",
            "reasoning_effort": "high",
            "tool_profile": "adaos-local-bounded-v1",
        },
        "environment_spec": {
            "core_commit": "a" * 40,
            "python_version": "3.12.10",
            "platform": "windows-amd64",
            "executor_provider": "adaos.local_skill_factory",
            "hostile_isolation": False,
            "network_enforcement": False,
            "skill_workspace_commit": "b" * 40,
            "component_versions": {
                "research_orchestrator_skill": "0.19.0",
                "research_evaluator_skill": "0.1.7",
                "research_calibration_runner_skill": "0.1.3",
            },
            "standard_prompt_version": "adaos-skill-realization/0.1.0",
            "core_source_tree_clean": True,
            "core_source_tree_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "measurement_policy": {
            "model_token_charge": "input_plus_output_including_cached",
            "wall_clock": "builder_automation_elapsed_seconds",
            "attempt_count": "initial_plus_automatic_repairs",
            "human_intervention_count": "post_start_operator_directives",
        },
        "inputs": inputs,
        "hidden_inputs": [
            {"input_id": "expert-oracle", "kind": "expert_oracle", "ref": "hidden://oracle", "digest": _sha(oracle.read_bytes()), "path": str(oracle)},
            {"input_id": "legacy", "kind": "legacy_implementation", "ref": "hidden://legacy", "digest": _sha(legacy.read_bytes()), "path": str(legacy)},
        ],
        "arms": [
            {"arm_id": arm_id, "artifact_audience": f"research.calibration.{arm_id.lower()}", "instruction_input_ids": by_arm[arm_id], "description": f"Frozen delivery policy for {arm_id}."}
            for arm_id in ARM_IDS
        ],
        "rubric": {
            "primary_endpoint": "evidence_valid_completion",
            "checks": [
                {"check_id": "protocol_fidelity", "stage": "implementation", "evaluation_mode": "deterministic", "mandatory": True, "description": "The frozen protocol digest is preserved."},
                {"check_id": "runner_conformance", "stage": "implementation", "evaluation_mode": "deterministic", "mandatory": True, "description": "The direction passes the consumer runner contract."},
                {"check_id": "evidence_manifest", "stage": "scientific_evaluation", "evaluation_mode": "deterministic", "mandatory": True, "description": "The evidence package verifies independently."},
            ],
            "failure_stages": ["source_understanding", "formulation", "operationalization", "engineering_compilation", "implementation", "runtime_infrastructure", "scientific_evaluation"],
        },
        "budget_views": {
            "fixed_downstream": {"max_model_tokens": 10000, "max_wall_seconds": 3600, "max_attempts": 1, "max_human_interventions": 0},
            "fixed_total_system": {"max_model_tokens": 14000, "max_wall_seconds": 5400, "max_attempts": 1, "max_human_interventions": 0},
        },
        "repetitions": {"attempts_per_arm": 2, "paired_seeds": [17, 23], "model_random_seed_control": "unsupported_not_claimed"},
        "exclusion_rules": ["Exclude only a preregistered platform outage before agent execution."],
    }


def test_freeze_task_requires_exact_c0_c4_delivery_contract(task_value) -> None:
    frozen = freeze_task(task_value)

    assert frozen["digest"].startswith("sha256:")
    assert {item["arm_id"] for item in frozen["arms"]} == set(ARM_IDS)

    invalid = dict(task_value)
    invalid["arms"] = [dict(item) for item in task_value["arms"]]
    invalid["arms"][0]["instruction_input_ids"] = ["review"]
    with pytest.raises(ValueError, match="visibility declarations"):
        freeze_task(invalid)


def test_v17_freeze_enforces_separate_correctness_and_resource_endpoints(task_value) -> None:
    valid = copy.deepcopy(task_value)
    valid["schema_version"] = "1.7.0"
    valid["exclusion_rules"] = list(CALIBRATION_EXCLUSION_RULES)
    valid["comparison_plan"] = _paired_task(task_value)["comparison_plan"]
    valid["repetitions"] = {
        "attempts_per_arm": 5,
        "paired_seeds": [17, 23, 47, 71, 101],
        "model_random_seed_control": "unsupported_not_claimed",
    }
    valid["consumer_evaluation"] = {
        "max_wall_seconds": 1200,
        "timeout_result_policy": "persist_terminal_failure",
        "repeat_policy": "return_existing_result",
    }
    valid["expected_smoke_profile"].update(
        {
            "network_enforcement_required": False,
            "max_wall_seconds": 600,
            "workload": {"mode": "bounded", "limits": []},
            "input_policy": {
                "source": "deterministic_contract_fixture",
                "readiness": "required_before_execution",
                "sampling": "deterministic_seeded",
            },
        }
    )
    valid["environment_spec"]["runner_contract_digest"] = "sha256:" + "6" * 64
    valid["environment_spec"]["component_versions"]["research_manager_skill"] = "0.1.0"

    frozen = freeze_task(valid)
    assert frozen["exclusion_rules"] == list(CALIBRATION_EXCLUSION_RULES)

    invalid = copy.deepcopy(valid)
    invalid["exclusion_rules"][-1] = "Budget exhaustion is a correctness failure."
    with pytest.raises(ValueError, match="correctness and resource endpoints separate"):
        freeze_task(invalid)


def test_v18_freeze_requires_preregistered_runtime_disk_headroom(task_value) -> None:
    valid = copy.deepcopy(task_value)
    valid["schema_version"] = "1.8.0"
    valid["exclusion_rules"] = list(CALIBRATION_EXCLUSION_RULES)
    valid["comparison_plan"] = _paired_task(task_value)["comparison_plan"]
    valid["repetitions"] = {
        "attempts_per_arm": 5,
        "paired_seeds": [17, 23, 47, 71, 101],
        "model_random_seed_control": "unsupported_not_claimed",
    }
    valid["consumer_evaluation"] = {
        "max_wall_seconds": 1200,
        "timeout_result_policy": "persist_terminal_failure",
        "repeat_policy": "return_existing_result",
    }
    valid["expected_smoke_profile"].update(
        {
            "network_enforcement_required": False,
            "max_wall_seconds": 600,
            "workload": {"mode": "bounded", "limits": []},
            "input_policy": {
                "source": "deterministic_contract_fixture",
                "readiness": "required_before_execution",
                "sampling": "deterministic_seeded",
            },
        }
    )
    valid["environment_spec"].update(
        {
            "runner_contract_digest": "sha256:" + "6" * 64,
            "minimum_free_disk_bytes": 17_179_869_184,
        }
    )
    valid["environment_spec"]["component_versions"]["research_manager_skill"] = "0.1.0"

    frozen = freeze_task(valid)
    assert frozen["environment_spec"]["minimum_free_disk_bytes"] == 17_179_869_184

    invalid = copy.deepcopy(valid)
    del invalid["environment_spec"]["minimum_free_disk_bytes"]
    with pytest.raises(ValueError, match="minimum_free_disk_bytes"):
        freeze_task(invalid)


def test_v19_freeze_requires_the_scientific_implementation_gate(task_value) -> None:
    valid = copy.deepcopy(task_value)
    valid["schema_version"] = "1.9.0"
    valid["exclusion_rules"] = list(CALIBRATION_EXCLUSION_RULES)
    valid["comparison_plan"] = _paired_task(task_value)["comparison_plan"]
    valid["repetitions"] = {
        "attempts_per_arm": 5,
        "paired_seeds": [17, 23, 47, 71, 101],
        "model_random_seed_control": "unsupported_not_claimed",
    }
    valid["consumer_evaluation"] = {
        "max_wall_seconds": 1200,
        "timeout_result_policy": "persist_terminal_failure",
        "repeat_policy": "return_existing_result",
    }
    valid["expected_smoke_profile"].update(
        {
            "network_enforcement_required": False,
            "max_wall_seconds": 600,
            "workload": {"mode": "bounded", "limits": []},
            "input_policy": {
                "source": "deterministic_contract_fixture",
                "readiness": "required_before_execution",
                "sampling": "deterministic_seeded",
            },
        }
    )
    valid["environment_spec"].update(
        {
            "runner_contract_digest": "sha256:" + "6" * 64,
            "minimum_free_disk_bytes": 17_179_869_184,
        }
    )
    valid["environment_spec"]["component_versions"][
        "research_manager_skill"
    ] = "0.1.0"
    stages = {
        "context_isolation": "source_understanding",
        "protocol_fidelity": "operationalization",
        "native_skill_validation": "implementation",
        "runner_conformance": "implementation",
        "scientific_implementation": "implementation",
        "cpu_workflow_smoke": "runtime_infrastructure",
        "evidence_manifest": "scientific_evaluation",
    }
    valid["rubric"]["checks"] = [
        {
            "check_id": check_id,
            "stage": stage,
            "evaluation_mode": "deterministic",
            "mandatory": True,
            "description": f"Independently verify the mandatory {check_id} condition.",
        }
        for check_id, stage in stages.items()
    ]

    frozen = freeze_task(valid)
    assert {item["check_id"] for item in frozen["rubric"]["checks"]} == set(stages)

    invalid = copy.deepcopy(valid)
    invalid["rubric"]["checks"] = [
        item
        for item in invalid["rubric"]["checks"]
        if item["check_id"] != "scientific_implementation"
    ]
    with pytest.raises(ValueError, match="scientific implementation rubric"):
        freeze_task(invalid)


def test_hidden_judge_inputs_are_snapshotted_before_freeze(tmp_path: Path) -> None:
    class MemoryBlobStore:
        def __init__(self, root: Path) -> None:
            self.root = root
            self.root.mkdir(parents=True, exist_ok=True)

        def put_bytes(self, name: str, data: bytes, *, media_type: str) -> dict:
            path = self.root / name
            path.write_bytes(data)
            return {"ref": f"blob://{name}", "path": str(path), "media_type": media_type}

        def materialize_path(self, blob: dict) -> Path:
            return Path(blob["path"])

    rubric_path = tmp_path / "hidden-rubric.json"
    rubric_path.write_text(
        json.dumps({"exclusions": list(CALIBRATION_EXCLUSION_RULES)}),
        encoding="utf-8",
    )
    oracle_path = tmp_path / "oracle.json"
    oracle_path.write_text('{"legacy":true}', encoding="utf-8")
    oracle_digest = _sha(oracle_path.read_bytes())
    values = [
        {
            "input_id": "hidden-rubric",
            "kind": "hidden_rubric",
            "ref": "source://rubric",
            "digest": "sha256:" + "0" * 64,
            "path": "obsolete-rubric.json",
        },
        {
            "input_id": "legacy-oracle",
            "kind": "legacy_implementation",
            "ref": "source://oracle",
            "digest": oracle_digest,
            "path": str(oracle_path),
        },
    ]

    snapshots, refs = _snapshot_hidden_inputs(
        "task-v1",
        values,
        MemoryBlobStore(tmp_path / "blobs"),
        rubric_path=rubric_path,
    )
    snapshot_paths = [Path(item["path"]) for item in snapshots]
    before = [path.read_bytes() for path in snapshot_paths]
    rubric_path.write_text("{}", encoding="utf-8")
    oracle_path.write_text("{}", encoding="utf-8")

    assert [path.read_bytes() for path in snapshot_paths] == before
    assert set(refs) == {"hidden-rubric", "legacy-oracle"}
    assert all(item["ref"].startswith("calibration-hidden://task-v1/") for item in snapshots)


def test_prepare_arm_never_projects_hidden_evaluator_material(task_value, monkeypatch) -> None:
    frozen = freeze_task(task_value)
    monkeypatch.setattr(
        "evaluation.harness.artifact_context.materialize_context",
        lambda skill, group, audience: {
            "source_ref": f"artifact://skill/{skill}/{group}",
            "audience": audience,
            "digest": "sha256:" + "b" * 64,
            "source_manifest_digest": "sha256:" + "c" * 64,
            "root_path": "/isolated/view/files",
            "items": [{"artifact_id": "notebook", "path": "experiment.ipynb", "digest": "sha256:" + "d" * 64, "size_bytes": 100}],
        },
    )

    packet = prepare_arm(frozen, "C3_typed_execution", 1)
    serialized = json.dumps(packet)

    assert [item["kind"] for item in packet["instruction_inputs"]] == [
        "research_compilation",
        "automation_brief",
        "conformance_fixture",
    ]
    assert "expert-oracle" not in serialized
    assert "hidden://legacy" not in serialized
    assert packet["paired_seed"] == 17
    assert packet["agent_profile"]["model"] == "gpt-5.4"


def _candidate(
    *,
    arm_id: str,
    attempt_index: int,
    seed: int,
    failed: str | None = None,
    tokens: int = 9000,
    execution_started_at: str | None = None,
) -> dict:
    value = {
        "arm_id": arm_id,
        "attempt_index": attempt_index,
        "paired_seed": seed,
        "budget_view": "fixed_downstream",
        "model": "codex-test",
        "environment_digest": "sha256:" + "e" * 64,
        "protocol_digest": "sha256:" + "a" * 64,
        "budget_usage": {"model_tokens": tokens, "wall_seconds": 100, "attempts": 1, "human_interventions": 0},
        "checks": [
            {"check_id": check_id, "status": "fail" if check_id == failed else "pass", "evidence_refs": [f"evidence://{check_id}"], "detail": "verified" if check_id != failed else "failed verification"}
            for check_id in ("protocol_fidelity", "runner_conformance", "evidence_manifest")
        ],
    }
    if execution_started_at:
        value["execution_started_at"] = execution_started_at
    return value


def _paired_task(task_value: dict) -> dict:
    value = copy.deepcopy(task_value)
    seeds = [17, 23, 47, 71, 101]
    value["schema_version"] = "1.3.0"
    value["repetitions"] = {
        "attempts_per_arm": len(seeds),
        "paired_seeds": seeds,
        "model_random_seed_control": "unsupported_not_claimed",
    }
    value["comparison_plan"] = {
        "control_arm": "C0_raw",
        "treatment_arm": "C3_typed_execution",
        "primary_endpoint": "evidence_valid_completion",
        "pairing_key": "paired_seed",
        "planned_pairs": len(seeds),
        "execution_order": [
            {
                "attempt_index": index,
                "paired_seed": seed,
                "first_arm": "C0_raw" if index % 2 else "C3_typed_execution",
                "second_arm": "C3_typed_execution" if index % 2 else "C0_raw",
            }
            for index, seed in enumerate(seeds, start=1)
        ],
        "test": "exact_paired_sign_test",
        "alternative": "treatment_greater",
        "alpha": 0.05,
        "missing_policy": "incomplete_no_claim",
        "claim_scope": "local_tlp_fixed_stack",
    }
    return freeze_task(value)


def _paired_results(task: dict, *, control_passes: set[int] | None = None) -> list[dict]:
    passing = set(control_passes or set())
    rows = []
    sequence = 0
    for scheduled in task["comparison_plan"]["execution_order"]:
        index = int(scheduled["attempt_index"])
        seed = int(scheduled["paired_seed"])
        for arm_id in (scheduled["first_arm"], scheduled["second_arm"]):
            rows.append(
                evaluate_candidate(
                    task,
                    _candidate(
                        arm_id=arm_id,
                        attempt_index=index,
                        seed=seed,
                        failed=(
                            None
                            if arm_id == "C3_typed_execution" or index in passing
                            else "runner_conformance"
                        ),
                        execution_started_at=f"2026-08-18T00:{sequence:02d}:00Z",
                    ),
                )
            )
            sequence += 1
    return rows


def test_evaluator_computes_primary_endpoint_budget_and_first_failure(task_value) -> None:
    task = freeze_task(task_value)
    passed = evaluate_candidate(task, _candidate(arm_id="C0_raw", attempt_index=1, seed=17))
    failed = evaluate_candidate(task, _candidate(arm_id="C3_typed_execution", attempt_index=1, seed=17, failed="runner_conformance"))
    over_budget = evaluate_candidate(task, _candidate(arm_id="C4_over_specified", attempt_index=1, seed=17, tokens=10001))

    assert passed["metrics"]["evidence_valid_completion"] is True
    assert failed["failure"]["stage"] == "implementation"
    assert over_budget["metrics"]["evidence_valid_completion"] is True
    assert over_budget["metrics"]["budget_compliant"] is False
    assert over_budget["metrics"]["budgeted_evidence_valid_completion"] is False
    assert over_budget["failure"] is None

    summary = summarize(task, [passed, failed, over_budget])
    assert summary["complete"] is False
    assert summary["arms"][0]["rate"] == 1.0
    assert summary["digest"].startswith("sha256:")

    package = build_recomputable_package(
        task,
        [
            {
                "packet_id": "packet-1",
                "budget_view": "fixed_downstream",
                "arm_id": "C0_raw",
                "attempt_index": 1,
                "digest": "sha256:" + "9" * 64,
            }
        ],
        [passed, failed, over_budget],
        budget_view="fixed_downstream",
    )
    assert package["summary"] == summary
    assert package["results"][0]["digest"] == passed["digest"]
    assert package["digest"].startswith("sha256:")


def test_five_clean_c3_wins_cross_preregistered_exact_threshold(task_value) -> None:
    task = _paired_task(task_value)
    summary = summarize(task, _paired_results(task))
    comparison = summary["primary_comparison"]

    assert summary["complete"] is True
    assert summary["all_arms_complete"] is False
    assert comparison["complete_pairs"] == 5
    assert comparison["treatment_wins"] == 5
    assert comparison["control_wins"] == 0
    assert comparison["ties"] == 0
    assert comparison["p_value"] == pytest.approx(0.03125)
    assert comparison["paired_risk_difference"] == 1.0
    assert comparison["execution_order_verifiable"] is True
    assert comparison["execution_order_valid"] is True
    assert comparison["claim_status"] == "supports_local_advantage"


def test_four_discordant_wins_are_insufficient_and_missing_pair_forbids_claim(task_value) -> None:
    task = _paired_task(task_value)
    rows = _paired_results(task, control_passes={5})
    comparison = summarize(task, rows)["primary_comparison"]
    assert comparison["treatment_wins"] == 4
    assert comparison["ties"] == 1
    assert comparison["p_value"] == pytest.approx(0.0625)
    assert comparison["claim_status"] == "does_not_support_local_advantage"

    incomplete = summarize(task, rows[:-1])["primary_comparison"]
    assert incomplete["complete"] is False
    assert incomplete["p_value"] is None
    assert incomplete["claim_status"] == "incomplete_no_claim"


def test_paired_summary_rejects_duplicate_attempts(task_value) -> None:
    task = _paired_task(task_value)
    rows = _paired_results(task)
    with pytest.raises(ValueError, match="duplicate arm attempts"):
        summarize(task, [*rows, rows[0]])


def test_paired_summary_refuses_claim_when_execution_order_drifted(task_value) -> None:
    task = _paired_task(task_value)
    rows = _paired_results(task)
    rows[0]["execution_started_at"], rows[1]["execution_started_at"] = (
        rows[1]["execution_started_at"],
        rows[0]["execution_started_at"],
    )

    comparison = summarize(task, rows)["primary_comparison"]

    assert comparison["complete"] is True
    assert comparison["execution_order_verifiable"] is True
    assert comparison["execution_order_valid"] is False
    assert comparison["execution_order_violations"][0]["position"] == 1
    assert comparison["claim_status"] == "execution_order_violation_no_claim"


def test_independent_judge_derives_checks_instead_of_accepting_candidate_claims(task_value) -> None:
    task = freeze_task(task_value)
    task["rubric"]["checks"] = [
        {"check_id": check_id, "stage": "implementation", "evaluation_mode": "deterministic", "mandatory": True, "description": check_id}
        for check_id in (
            "context_isolation",
            "protocol_fidelity",
            "native_skill_validation",
            "runner_conformance",
            "cpu_workflow_smoke",
            "evidence_manifest",
        )
    ]
    packet = {
        "packet_id": "packet-tlp-C3-1-fixed_downstream",
        "task_id": task["task_id"],
        "task_digest": task["digest"],
        "arm_id": "C3_typed_execution",
        "attempt_index": 1,
        "paired_seed": 17,
        "budget_view": "fixed_downstream",
        "budget": task["budget_views"]["fixed_downstream"],
        "artifact_inputs": [
            {
                "ref": "artifact://skill/tlp_direction/part0",
                "source_manifest_digest": "sha256:" + "1" * 64,
                "context_digest": "sha256:" + "2" * 64,
                "audience": "research.calibration.c3_typed_execution",
            }
        ],
        "instruction_inputs": [],
        "prohibited_actions": ["no hidden access"],
    }
    session = {
        "session_id": "dev-test",
        "artifact_inputs": [
            {
                "ref": "artifact://skill/tlp_direction/part0",
                "manifest_digest": "sha256:" + "1" * 64,
                "context_digest": "sha256:" + "2" * 64,
                "audience": "research.calibration.c3_typed_execution",
            }
        ],
        "instruction_inputs": [],
        "targets": {"primary": [{"ref": "skill:candidate"}], "secondary": []},
        "handoff": {"prohibited_actions": ["no hidden access"]},
    }
    spec = {
        "metadata": {
            "protocol_digest": task["expected_protocol_digest"],
            "stage": "workflow_smoke",
            "evidence_class": "workflow_smoke",
            "epochs": 3,
            "seeds": [17],
            "inference_allowed": False,
        },
        "resources": {"gpu_count": 0},
        "network": {"mode": "offline"},
    }

    candidate = build_independent_candidate(
        task=task,
        packet=packet,
        candidate_id="candidate",
        session=session,
        automation={"budget_usage": {"observed": {"model_tokens": 100, "wall_seconds": 10, "attempts": 2}}},
        validation={"ok": True, "digest": "sha256:" + "3" * 64, "source_digest": "sha256:" + "4" * 64},
        prepare={"ok": True, "execution_spec": spec},
        trial={"ok": False, "digest": "sha256:" + "5" * 64, "documents": {}, "outputs": []},
        dataset={"ok": True},
        verified_artifacts=[],
        collected=None,
    )

    statuses = {item["check_id"]: item["status"] for item in candidate["checks"]}
    assert statuses["context_isolation"] == "pass"
    assert statuses["protocol_fidelity"] == "pass"
    assert statuses["native_skill_validation"] == "pass"
    assert statuses["cpu_workflow_smoke"] == "fail"
    assert statuses["evidence_manifest"] == "fail"
    assert candidate["budget_usage"]["attempts"] == 2
    assert all(ref for check in candidate["checks"] for ref in check["evidence_refs"])
    evaluated = evaluate_candidate(task, candidate)
    assert evaluated["metrics"]["evidence_valid_completion"] is False
    assert evaluated["failure"]["stage"] == "implementation"

    canonical_run_log = {
        "stage": "workflow_smoke",
        "device": "cpu",
        "epochs_completed": 3,
        "seeds": ["seed-17"],
        "inference_allowed": False,
        "evidence_class": "workflow_smoke",
    }
    canonical_audit = {
        "per_stage": {"workflow_smoke": {"test_evaluations_count": 0}},
        "test_access": [],
    }
    accepted = build_independent_candidate(
        task=task,
        packet=packet,
        candidate_id="candidate",
        session=session,
        automation={
            "budget_usage": {
                "observed": {"model_tokens": 100, "wall_seconds": 10, "attempts": 1}
            }
        },
        validation={"ok": True, "digest": "sha256:" + "3" * 64},
        prepare={"ok": True, "execution_spec": spec},
        trial={
            "ok": True,
            "digest": "sha256:" + "5" * 64,
            "documents": {
                "run_log.json": canonical_run_log,
                "evaluation_audit.json": canonical_audit,
                "artifacts_index.json": {"files": []},
            },
            "outputs": [],
        },
        dataset={"ok": True},
        verified_artifacts=[],
        collected=None,
    )

    accepted_statuses = {
        item["check_id"]: item["status"] for item in accepted["checks"]
    }
    assert accepted_statuses["protocol_fidelity"] == "pass"
    assert accepted_statuses["cpu_workflow_smoke"] == "pass"


def test_independent_judge_rejects_string_pair_ids_as_rng_seeds(task_value, monkeypatch) -> None:
    task = freeze_task(task_value)
    monkeypatch.setattr(
        "evaluation.harness.artifact_context.materialize_context",
        lambda skill, group, audience: {
            "source_ref": f"artifact://skill/{skill}/{group}",
            "audience": audience,
            "digest": "sha256:" + "b" * 64,
            "source_manifest_digest": "sha256:" + "c" * 64,
            "root_path": "/isolated/view/files",
            "items": [],
        },
    )
    packet = prepare_arm(task, "C3_typed_execution", 1)
    session = {
        "session_id": "dev-test",
        "artifact_inputs": [
            {
                "ref": "artifact://skill/tlp_direction/part0",
                "manifest_digest": "sha256:" + "1" * 64,
                "context_digest": "sha256:" + "2" * 64,
                "audience": "research.calibration.c3_typed_execution",
            }
        ],
        "instruction_inputs": [],
        "targets": {"primary": [{"ref": "skill:candidate"}], "secondary": []},
        "handoff": {"prohibited_actions": ["no hidden access"]},
    }
    packet["instruction_inputs"] = []
    packet["prohibited_actions"] = ["no hidden access"]
    spec = {
        "metadata": {
            "protocol_digest": task["expected_protocol_digest"],
            "stage": "workflow_smoke",
            "evidence_class": "workflow_smoke",
            "epochs": 3,
            "seeds": ["seed-17"],
            "inference_allowed": False,
        },
        "resources": {"gpu_count": 0},
        "network": {"mode": "offline"},
    }

    candidate = build_independent_candidate(
        task=task,
        packet=packet,
        candidate_id="candidate",
        session=session,
        automation={},
        validation={"ok": True},
        prepare={"ok": True, "execution_spec": spec},
        trial=None,
        dataset={},
        verified_artifacts=[],
        collected=None,
    )

    statuses = {item["check_id"]: item["status"] for item in candidate["checks"]}
    assert statuses["protocol_fidelity"] == "fail"
    detail = next(
        item["detail"]
        for item in candidate["checks"]
        if item["check_id"] == "protocol_fidelity"
    )
    assert "seeds" in detail


def test_independent_judge_preserves_terminal_builder_failure_stage(task_value, monkeypatch) -> None:
    task = freeze_task(task_value)
    monkeypatch.setattr(
        "evaluation.harness.artifact_context.materialize_context",
        lambda skill, group, audience: {
            "source_ref": f"artifact://skill/{skill}/{group}",
            "audience": audience,
            "digest": "sha256:" + "b" * 64,
            "source_manifest_digest": "sha256:" + "c" * 64,
            "root_path": "/isolated/view/files",
            "items": [],
        },
    )
    task["rubric"]["checks"] = [
        {
            "check_id": check_id,
            "stage": "implementation",
            "evaluation_mode": "deterministic",
            "mandatory": True,
            "description": check_id,
        }
        for check_id in (
            "context_isolation",
            "protocol_fidelity",
            "native_skill_validation",
            "runner_conformance",
            "cpu_workflow_smoke",
            "evidence_manifest",
        )
    ]
    packet = prepare_arm(task, "C0_raw", 1)
    session = {
        "session_id": "dev-failed",
        "artifact_inputs": [
            {
                "ref": item["ref"],
                "manifest_digest": item["source_manifest_digest"],
                "context_digest": item["context_digest"],
                "audience": item["audience"],
            }
            for item in packet["artifact_inputs"]
        ],
        "instruction_inputs": [],
        "targets": {"primary": [{"ref": "skill:candidate"}], "secondary": []},
        "handoff": {"prohibited_actions": packet["prohibited_actions"]},
    }
    candidate = build_independent_candidate(
        task=task,
        packet=packet,
        candidate_id="candidate",
        session=session,
        automation={
            "status": "failed",
            "failure_stage": "runtime_infrastructure",
            "error": "worker host unavailable",
        },
        validation=None,
        prepare=None,
        trial=None,
        dataset=None,
        verified_artifacts=[],
        collected=None,
    )

    evaluated = evaluate_candidate(task, candidate)

    assert evaluated["failure"] == {
        "stage": "runtime_infrastructure",
        "code": "builder_automation.failed",
        "detail": "worker host unavailable",
    }


def test_builder_evaluation_returns_existing_result_without_reexecuting_or_releasing(monkeypatch) -> None:
    from handlers import main as handler_module

    stored = {
        "digest": "sha256:" + "9" * 64,
        "metrics": {"evidence_valid_completion": True},
    }

    class _Repository:
        @staticmethod
        def get_task(_task_id: str) -> dict:
            return {"task_id": "task"}

        @staticmethod
        def get_packet(*_args, **_kwargs) -> dict:
            return {"packet_id": "packet"}

        @staticmethod
        def find_result(*_args, **_kwargs) -> dict:
            return stored

    monkeypatch.setattr(handler_module, "EvaluationRepository", _Repository)
    monkeypatch.setattr(
        handler_module.automation,
        "get_state",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not read Builder state")),
    )
    monkeypatch.setattr(
        handler_module,
        "invoke_skill",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not invoke another skill")
        ),
    )

    response = handler_module.evaluate_builder_attempt(
        task_id="task",
        arm_id="C3_typed_execution",
        attempt_index=1,
        candidate_id="candidate",
        development_session_id="session",
        builder_webspace_id="builder",
    )

    assert response["idempotent_replay"] is True
    assert response["result"] is stored
    assert response["runtime_release"] == {
        "schema": "adaos.research.runtime_release_delegation.v1",
        "status": "deferred",
        "owner_ref": "skill:research_calibration_runner_skill",
        "reason": "evaluator_process_may_hold_candidate_native_modules",
    }
    assert response["lifecycle_errors"] == []


def test_builder_evaluation_runs_hidden_semantic_probe_over_paired_trials(
    tmp_path: Path, monkeypatch
) -> None:
    from handlers import main as handler_module

    rubric_path = tmp_path / "hidden-rubric.json"
    rubric_path.write_text(
        json.dumps({"implementation_profile": {"subject": "tlp"}}),
        encoding="utf-8",
    )
    plan = {
        "digest": "sha256:" + "1" * 64,
        "system": {"digest": "sha256:" + "2" * 64},
    }
    task = {
        "rubric": {"checks": [{"check_id": "scientific_implementation"}]},
        "hidden_inputs": [
            {
                "kind": "hidden_rubric",
                "path": str(rubric_path),
                "digest": _sha(rubric_path.read_bytes()),
            }
        ],
        "inputs": [],
    }
    packet = {"packet_id": "packet"}

    class _Repository:
        @staticmethod
        def get_task(_task_id: str) -> dict:
            return task

        @staticmethod
        def get_packet(*_args, **_kwargs) -> dict:
            return packet

        @staticmethod
        def find_result(*_args, **_kwargs):
            return None

        @staticmethod
        def put_result(value: dict) -> dict:
            return value

    instruction_values = {
        "research_compilation": {
            "digest": "sha256:" + "3" * 64,
            "experiment_plan": plan,
        },
        "automation_brief": {
            "digest": "sha256:" + "4" * 64,
            "compilation_digest": "sha256:" + "3" * 64,
        },
    }
    session = {
        "session_id": "session",
        "project_ref": "project:test",
        "instruction_inputs": [
            {"kind": kind, "content_digest": value["digest"]}
            for kind, value in instruction_values.items()
        ],
    }
    arm_trials = [{"arm": {"id": "maxpool", "role": "baseline"}}, {"arm": {"id": "tlp", "role": "intervention"}}]
    source_snapshot = {
        "source_digest": "sha256:" + "5" * 64,
        "digest": "sha256:" + "6" * 64,
        "files": [],
    }
    semantic_calls: list[dict] = []
    candidate_calls: list[dict] = []

    monkeypatch.setattr(handler_module, "EvaluationRepository", _Repository)
    monkeypatch.setattr(handler_module.development_sessions, "get", lambda _session_id: session)
    monkeypatch.setattr(
        handler_module.development_sessions,
        "get_instruction",
        lambda _session_id, kind: {"value": instruction_values[kind]},
    )
    monkeypatch.setattr(
        handler_module.automation,
        "get_state",
        lambda **_kwargs: {"automation": {"terminal": True, "status": "completed"}},
    )

    def invoke(_skill_id: str, operation_id: str, _arguments: dict, **_kwargs):
        if operation_id == "get_runner_contract":
            return {"digest": "sha256:" + "7" * 64}
        assert operation_id == "evaluate_development_candidate"
        return {
            "ok": True,
            "receipt_digest": "sha256:" + "8" * 64,
            "checks": [
                {
                    "id": "candidate.native_validation",
                    "ok": True,
                    "digest": "sha256:" + "9" * 64,
                    "source_digest": source_snapshot["source_digest"],
                }
            ],
            "evidence": {
                "execution_spec": {},
                "arm_trials": arm_trials,
            },
            "errors": [],
        }

    monkeypatch.setattr(handler_module, "invoke_skill", invoke)
    monkeypatch.setattr(handler_module, "inspect_skill_source", lambda _candidate: source_snapshot)
    monkeypatch.setattr(
        handler_module,
        "invoke_development_skill",
        lambda *_args, **_kwargs: {"schema": "probe-result"},
    )

    def semantic(**kwargs):
        semantic_calls.append(kwargs)
        return {"ok": True, "detail": "verified", "evidence_refs": ["evidence://semantic"]}

    monkeypatch.setattr(handler_module, "evaluate_tlp_implementation", semantic)

    def build_candidate(**kwargs):
        candidate_calls.append(kwargs)
        return {"candidate": True}

    monkeypatch.setattr(handler_module, "build_independent_candidate", build_candidate)
    monkeypatch.setattr(
        handler_module,
        "evaluate_candidate",
        lambda _task, _candidate: {
            "metrics": {"evidence_valid_completion": True},
            "digest": "sha256:" + "a" * 64,
        },
    )
    response = handler_module.evaluate_builder_attempt(
        task_id="task",
        arm_id="C3_typed_execution",
        attempt_index=1,
        candidate_id="candidate",
        development_session_id="session",
        builder_webspace_id="builder",
    )

    assert response["evidence_valid_completion"] is True
    assert len(semantic_calls) == 1
    assert semantic_calls[0]["arm_trials"] == arm_trials
    assert semantic_calls[0]["source_snapshot"] is source_snapshot
    assert semantic_calls[0]["expected_source_digest"] == source_snapshot["source_digest"]
    assert semantic_calls[0]["probe_request"]["experiment_plan_digest"] == plan["digest"]
    assert candidate_calls[0]["scientific_implementation"]["ok"] is True
    assert response["runtime_release"]["status"] == "deferred"
    assert response["lifecycle_errors"] == []
