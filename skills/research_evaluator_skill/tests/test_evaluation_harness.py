from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evaluation.contracts import ARM_IDS, freeze_task
from evaluation.harness import evaluate_candidate, prepare_arm, summarize


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
        "task_id": "tlp-calibration-v1",
        "title": "TLP clean research compilation calibration",
        "direction_skill_id": "tlp_research_03",
        "base_request": "Build a clean executable TLP experiment from the supplied source material without using hidden answers.",
        "artifact_groups": ["part0"],
        "expected_protocol_digest": "sha256:" + "a" * 64,
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
        "repetitions": {"attempts_per_arm": 2, "paired_seeds": [17, 23]},
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
