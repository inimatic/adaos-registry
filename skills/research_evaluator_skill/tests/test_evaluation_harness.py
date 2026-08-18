from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evaluation.contracts import ARM_IDS, freeze_task
from evaluation.harness import evaluate_candidate, prepare_arm, summarize
from evaluation.independent import build_independent_candidate


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


def _candidate(*, arm_id: str, attempt_index: int, seed: int, failed: str | None = None, tokens: int = 9000) -> dict:
    return {
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


def test_evaluator_computes_primary_endpoint_budget_and_first_failure(task_value) -> None:
    task = freeze_task(task_value)
    passed = evaluate_candidate(task, _candidate(arm_id="C0_raw", attempt_index=1, seed=17))
    failed = evaluate_candidate(task, _candidate(arm_id="C3_typed_execution", attempt_index=1, seed=17, failed="runner_conformance"))
    over_budget = evaluate_candidate(task, _candidate(arm_id="C4_over_specified", attempt_index=1, seed=17, tokens=10001))

    assert passed["metrics"]["evidence_valid_completion"] is True
    assert failed["failure"]["stage"] == "implementation"
    assert over_budget["failure"]["code"] == "budget_exceeded"

    summary = summarize(task, [passed, failed, over_budget])
    assert summary["complete"] is False
    assert summary["arms"][0]["rate"] == 1.0
    assert summary["digest"].startswith("sha256:")


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
