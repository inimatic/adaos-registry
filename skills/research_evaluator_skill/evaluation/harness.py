from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

from adaos.sdk.developer import artifact_context

from evaluation.contracts import ARM_IDS, canonical, digest, freeze_task, now, validate, validate_result


_FAILURE_STAGE_ORDER = (
    "source_understanding",
    "formulation",
    "operationalization",
    "engineering_compilation",
    "implementation",
    "runtime_infrastructure",
    "scientific_evaluation",
)


def prepare_arm(
    task_value: Mapping[str, Any],
    arm_id: str,
    attempt_index: int,
    *,
    budget_view: str = "fixed_downstream",
) -> dict[str, Any]:
    task = freeze_task(task_value) if "digest" not in task_value else dict(task_value)
    arm = next((dict(item) for item in task["arms"] if item["arm_id"] == arm_id), None)
    if not arm:
        raise ValueError(f"unknown calibration arm: {arm_id}")
    index = int(attempt_index)
    seeds = list(task["repetitions"]["paired_seeds"])
    if index < 1 or index > len(seeds):
        raise ValueError("attempt_index is outside the preregistered paired seeds")
    if budget_view not in {"fixed_downstream", "fixed_total_system"}:
        raise ValueError("budget_view must be fixed_downstream or fixed_total_system")
    views = [
        artifact_context.materialize_context(
            str(task["direction_skill_id"]),
            str(group_id),
            str(arm["artifact_audience"]),
        )
        for group_id in task["artifact_groups"]
    ]
    selected_ids = set(str(item) for item in arm["instruction_input_ids"])
    instructions = [
        {
            key: copy.deepcopy(item[key])
            for key in ("input_id", "kind", "ref", "digest", "path")
        }
        for item in task["inputs"]
        if item["input_id"] in selected_ids
    ]
    identity: dict[str, Any] = {
        "schema": "adaos.research.calibration_packet.v1",
        "packet_id": f"packet-{task['task_id']}-{arm_id}-{index}-{budget_view}",
        "task_id": task["task_id"],
        "task_digest": task["digest"],
        "arm_id": arm_id,
        "attempt_index": index,
        "paired_seed": int(seeds[index - 1]),
        "base_request": task["base_request"],
        "budget_view": budget_view,
        "budget": copy.deepcopy(task["budget_views"][budget_view]),
        **({"agent_profile": copy.deepcopy(task["agent_profile"])} if task.get("agent_profile") else {}),
        **({"environment_spec": copy.deepcopy(task["environment_spec"])} if task.get("environment_spec") else {}),
        **({"measurement_policy": copy.deepcopy(task["measurement_policy"])} if task.get("measurement_policy") else {}),
        "artifact_inputs": [
            {
                "ref": view["source_ref"],
                "audience": view["audience"],
                "context_digest": view["digest"],
                "source_manifest_digest": view["source_manifest_digest"],
                "root_path": view["root_path"],
                "items": copy.deepcopy(view["items"]),
            }
            for view in views
        ],
        "instruction_inputs": instructions,
        "prohibited_actions": [
            "Do not inspect parent directories of admitted artifact or instruction roots.",
            "Do not access evaluator tools, hidden rubrics, expert oracles, or legacy implementations.",
            "Do not change the scientific protocol to make implementation easier.",
        ],
    }
    packet = {**identity, "created_at": now(), "digest": digest(identity)}
    projected = canonical(
        {
            "artifact_inputs": packet["artifact_inputs"],
            "instruction_inputs": packet["instruction_inputs"],
        }
    ).decode("utf-8")
    for hidden in task["hidden_inputs"]:
        for forbidden in (hidden["input_id"], hidden["ref"], hidden["path"]):
            if str(forbidden) and str(forbidden) in projected:
                raise ValueError("calibration packet leaks a hidden evaluator input")
    return packet


def evaluate_candidate(task_value: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    task = dict(task_value)
    arm_id = str(candidate.get("arm_id") or "")
    if arm_id not in ARM_IDS:
        raise ValueError("candidate arm_id is not part of C0-C4")
    attempt_index = int(candidate.get("attempt_index") or 0)
    seeds = list(task["repetitions"]["paired_seeds"])
    if attempt_index < 1 or attempt_index > len(seeds):
        raise ValueError("candidate attempt_index is outside preregistration")
    paired_seed = int(candidate.get("paired_seed"))
    if paired_seed != int(seeds[attempt_index - 1]):
        raise ValueError("candidate paired_seed does not match preregistration")
    rubric = {str(item["check_id"]): dict(item) for item in task["rubric"]["checks"]}
    checks = [dict(item) for item in candidate.get("checks") or []]
    check_ids = [str(item.get("check_id") or "") for item in checks]
    if len(check_ids) != len(set(check_ids)) or set(check_ids) != set(rubric):
        raise ValueError("candidate must report every rubric check exactly once")
    for check in checks:
        status = str(check.get("status") or "")
        if status not in {"pass", "fail", "not_evaluated"}:
            raise ValueError(f"invalid status for rubric check {check.get('check_id')}")
        refs = [str(item) for item in check.get("evidence_refs") or []]
        if status == "pass" and not refs:
            raise ValueError(f"passing rubric check {check.get('check_id')} requires evidence refs")
        check["evidence_refs"] = refs
        check["detail"] = str(check.get("detail") or "")
    mandatory = [item for item in checks if rubric[item["check_id"]]["mandatory"]]
    mandatory_passed = sum(1 for item in mandatory if item["status"] == "pass")
    budget_view = str(candidate.get("budget_view") or "fixed_downstream")
    if budget_view not in task["budget_views"]:
        raise ValueError("candidate budget_view is not preregistered")
    usage = {
        "model_tokens": int((candidate.get("budget_usage") or {}).get("model_tokens") or 0),
        "wall_seconds": float((candidate.get("budget_usage") or {}).get("wall_seconds") or 0),
        "attempts": int((candidate.get("budget_usage") or {}).get("attempts") or 1),
        "human_interventions": int((candidate.get("budget_usage") or {}).get("human_interventions") or 0),
        "formulation_tokens": int((candidate.get("budget_usage") or {}).get("formulation_tokens") or 0),
        "expert_minutes": float((candidate.get("budget_usage") or {}).get("expert_minutes") or 0),
    }
    budget = task["budget_views"][budget_view]
    charged_tokens = usage["model_tokens"] + (
        usage["formulation_tokens"] if budget_view == "fixed_total_system" else 0
    )
    budget_compliant = (
        charged_tokens <= int(budget["max_model_tokens"])
        and usage["wall_seconds"] <= int(budget["max_wall_seconds"])
        and usage["attempts"] <= int(budget["max_attempts"])
        and usage["human_interventions"] <= int(budget["max_human_interventions"])
    )
    expected_protocol = str(task.get("expected_protocol_digest") or "")
    protocol_digest = str(candidate.get("protocol_digest") or "") or None
    protocol_drift = bool(expected_protocol and protocol_digest != expected_protocol)
    failure = copy.deepcopy(candidate.get("failure")) if isinstance(candidate.get("failure"), Mapping) else None
    evidence_valid = (
        mandatory_passed == len(mandatory)
        and budget_compliant
        and not protocol_drift
        and failure is None
    )
    if not evidence_valid and failure is None:
        failed = [item for item in mandatory if item["status"] != "pass"]
        if failed:
            first = min(failed, key=lambda item: _FAILURE_STAGE_ORDER.index(rubric[item["check_id"]]["stage"]))
            failure = {
                "stage": rubric[first["check_id"]]["stage"],
                "code": f"rubric.{first['check_id']}.{first['status']}",
                "detail": first["detail"] or "Mandatory rubric check did not pass.",
            }
        elif protocol_drift:
            failure = {"stage": "implementation", "code": "protocol_drift", "detail": "Produced protocol digest differs from the frozen task."}
        else:
            failure = {"stage": "runtime_infrastructure", "code": "budget_exceeded", "detail": "Candidate exceeded the selected preregistered budget."}
    result: dict[str, Any] = {
        "schema": "adaos.research.calibration_result.v1",
        "schema_version": "1.0.0",
        "result_id": f"result-{task['task_id']}-{arm_id}-{attempt_index}-{budget_view}",
        "task_id": task["task_id"],
        "task_digest": task["digest"],
        "arm_id": arm_id,
        "attempt_index": attempt_index,
        "paired_seed": paired_seed,
        "model": str(candidate.get("model") or "unknown"),
        "environment_digest": str(candidate.get("environment_digest") or ""),
        "protocol_digest": protocol_digest,
        "budget_view": budget_view,
        "budget_usage": usage,
        "checks": checks,
        "metrics": {
            "evidence_valid_completion": evidence_valid,
            "mandatory_passed": mandatory_passed,
            "mandatory_total": len(mandatory),
            "protocol_drift": protocol_drift,
            "budget_compliant": budget_compliant,
        },
        "failure": failure,
        "evaluated_at": now(),
    }
    result["digest"] = digest(result)
    return validate_result(result)


def _wilson(successes: int, total: int) -> list[float] | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [max(0.0, center - margin), min(1.0, center + margin)]


def summarize(task: Mapping[str, Any], results: list[Mapping[str, Any]]) -> dict[str, Any]:
    budget_views = {str(item.get("budget_view") or "") for item in results}
    if len(budget_views) > 1:
        raise ValueError("summarize requires results from exactly one budget view")
    budget_view = next(iter(budget_views), "fixed_downstream")
    expected = int(task["repetitions"]["attempts_per_arm"])
    arms = []
    for arm_id in ARM_IDS:
        rows = [dict(item) for item in results if item.get("arm_id") == arm_id]
        successes = sum(1 for item in rows if item.get("metrics", {}).get("evidence_valid_completion"))
        failures: dict[str, int] = {}
        for item in rows:
            stage = str((item.get("failure") or {}).get("stage") or "none")
            failures[stage] = failures.get(stage, 0) + 1
        arms.append(
            {
                "arm_id": arm_id,
                "completed": len(rows),
                "expected": expected,
                "complete": len(rows) == expected,
                "evidence_valid_completions": successes,
                "rate": successes / len(rows) if rows else None,
                "wilson_95": _wilson(successes, len(rows)),
                "failure_stages": failures,
                "mean_model_tokens": sum(item["budget_usage"]["model_tokens"] for item in rows) / len(rows) if rows else None,
                "mean_human_interventions": sum(item["budget_usage"]["human_interventions"] for item in rows) / len(rows) if rows else None,
            }
        )
    identity = {
        "schema": "adaos.research.calibration_summary.v1",
        "task_id": task["task_id"],
        "task_digest": task["digest"],
        "primary_endpoint": "evidence_valid_completion",
        "budget_view": budget_view,
        "complete": all(item["complete"] for item in arms),
        "arms": arms,
    }
    return {**identity, "digest": digest(identity)}


def build_recomputable_package(
    task: Mapping[str, Any],
    packets: list[Mapping[str, Any]],
    results: list[Mapping[str, Any]],
    *,
    budget_view: str,
) -> dict[str, Any]:
    """Bind every immutable scoring input to its independently recomputed summary."""

    selected_packets = [
        dict(item)
        for item in packets
        if str(item.get("budget_view") or "") == str(budget_view)
    ]
    selected_results = [
        dict(item)
        for item in results
        if str(item.get("budget_view") or "") == str(budget_view)
    ]
    summary = summarize(task, selected_results)
    identity = {
        "schema": "adaos.research.calibration_package.v1",
        "schema_version": "1.0.0",
        "task": dict(task),
        "budget_view": str(budget_view),
        "packets": selected_packets,
        "results": selected_results,
        "summary": summary,
        "recompute": {
            "algorithm": "evaluation.harness:summarize",
            "algorithm_contract": "adaos.research.calibration_summary.v1",
            "canonicalization": "utf8-json-sort-keys-compact",
        },
    }
    package = {**identity, "digest": digest(identity)}
    return validate("research.calibration_package.v1.schema.json", package)


__all__ = ["build_recomputable_package", "evaluate_candidate", "prepare_arm", "summarize"]
