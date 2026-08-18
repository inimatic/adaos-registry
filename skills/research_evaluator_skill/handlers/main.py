from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import tool
from adaos.sdk.builder import automation, development_sessions
from adaos.sdk.developer import validation as developer_validation
from adaos.sdk.skills import invoke as invoke_skill


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from evaluation.contracts import ARM_IDS, freeze_task  # noqa: E402
from evaluation.harness import evaluate_candidate, prepare_arm, summarize  # noqa: E402
from evaluation.independent import build_independent_candidate  # noqa: E402
from evaluation.repository import EvaluationRepository  # noqa: E402


@tool(summary="Ensure the independent research-evaluation ledger.", side_effects="local_write")
def ensure_schema() -> dict[str, Any]:
    repository = EvaluationRepository()
    return {"ok": True, "binding": repository._db.binding.to_dict(), "health": dict(repository._db.health())}


@tool(summary="Freeze one immutable C0-C4 calibration task and preregistration.", side_effects="local_write")
def freeze_calibration(task: Mapping[str, Any], **_: Any) -> dict[str, Any]:
    repository = EvaluationRepository()
    existing = repository.find_task(str(task.get("task_id") or ""))
    stored = existing if existing else repository.put_task(freeze_task(task))
    return {
        "ok": True,
        "task": stored,
        "task_id": stored["task_id"],
        "task_digest": stored["digest"],
        "message": "Calibration task frozen. Visible and hidden inputs are digest-verified.",
    }


@tool(summary="Derive and freeze a compact v1.1 calibration from an immutable audit task.", side_effects="local_write")
def derive_compact_calibration(
    baseline_task_id: str,
    task_id: str,
    core_commit: str,
    python_version: str,
    platform: str,
    model: str,
    reasoning_effort: str = "high",
    attempts_per_arm: int = 1,
    max_model_tokens: int = 5_000_000,
    max_wall_seconds: int = 10_800,
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    baseline = repository.get_task(str(baseline_task_id))
    visible = {str(item["kind"]): dict(item) for item in baseline["inputs"]}
    compilation_path = Path(visible["research_compilation"]["path"]).resolve()
    brief_path = Path(visible["automation_brief"]["path"]).resolve()
    compilation = json.loads(compilation_path.read_text(encoding="utf-8-sig"))
    brief = json.loads(brief_path.read_text(encoding="utf-8-sig"))
    response = invoke_skill(
        "research_orchestrator_skill",
        "project_execution_contracts",
        {"compilation": compilation, "automation_brief": brief},
        timeout=120,
    )
    if not isinstance(response, Mapping) or not response.get("ok"):
        raise RuntimeError("research orchestrator did not return compact execution contracts")
    data_root = Path(str(os.environ.get("ADAOS_SKILL_DATA_DIR") or "")).resolve()
    if not str(os.environ.get("ADAOS_SKILL_DATA_DIR") or "").strip():
        raise RuntimeError("ADAOS_SKILL_DATA_DIR is required for durable calibration inputs")
    input_root = data_root / "files" / "calibrations" / str(task_id)
    input_root.mkdir(parents=True, exist_ok=True)
    projected_inputs = {
        "research_compilation": dict(response["research_compilation"]),
        "automation_brief": dict(response["automation_brief"]),
    }
    replacements = {}
    for kind, value in projected_inputs.items():
        path = input_root / f"{kind}.json"
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if path.is_file() and path.read_text(encoding="utf-8") != encoded:
            raise ValueError(f"frozen compact input drifted: {kind}")
        path.write_text(encoded, encoding="utf-8")
        replacements[kind] = {"path": str(path), "digest": str(value["digest"])}
    task = copy.deepcopy(baseline)
    for field in ("schema", "frozen_at", "digest"):
        task.pop(field, None)
    count = int(attempts_per_arm)
    seeds = list(baseline["repetitions"]["paired_seeds"])[:count]
    if count < 1 or len(seeds) != count:
        raise ValueError("attempts_per_arm exceeds the baseline paired workload seeds")
    task.update(
        {
            "schema_version": "1.1.0",
            "task_id": str(task_id),
            "title": str(baseline["title"]) + " (compact execution contracts)",
            "agent_profile": {
                "provider": "openai-codex-cli",
                "model": str(model),
                "reasoning_effort": str(reasoning_effort),
                "tool_profile": "adaos-local-bounded-v1",
            },
            "environment_spec": {
                "core_commit": str(core_commit),
                "python_version": str(python_version),
                "platform": str(platform),
                "executor_provider": "adaos.local_skill_factory",
                "hostile_isolation": False,
                "network_enforcement": False,
            },
            "measurement_policy": {
                "model_token_charge": "input_plus_output_including_cached",
                "wall_clock": "builder_automation_elapsed_seconds",
                "attempt_count": "initial_plus_automatic_repairs",
                "human_intervention_count": "post_start_operator_directives",
            },
            "budget_views": {
                "fixed_downstream": {
                    "max_model_tokens": int(max_model_tokens),
                    "max_wall_seconds": int(max_wall_seconds),
                    "max_attempts": 2,
                    "max_human_interventions": 0,
                },
                "fixed_total_system": {
                    "max_model_tokens": int(max_model_tokens) + 500_000,
                    "max_wall_seconds": int(max_wall_seconds) + 1800,
                    "max_attempts": 2,
                    "max_human_interventions": 0,
                },
            },
            "repetitions": {
                "attempts_per_arm": count,
                "paired_seeds": seeds,
                "model_random_seed_control": "unsupported_not_claimed",
            },
        }
    )
    for item in task["inputs"]:
        replacement = replacements.get(str(item["kind"]))
        if replacement:
            item.update(replacement)
            item["ref"] = f"calibration-input://{task_id}/{item['kind']}/{replacement['digest']}"
    stored = repository.put_task(freeze_task(task))
    return {
        "ok": True,
        "task_id": stored["task_id"],
        "task_digest": stored["digest"],
        "task": stored,
        "projection_receipt": {
            "audit_compilation_digest": response["audit_compilation_digest"],
            "audit_automation_brief_digest": response["audit_automation_brief_digest"],
            "execution_compilation_digest": projected_inputs["research_compilation"]["digest"],
            "execution_automation_brief_digest": projected_inputs["automation_brief"]["digest"],
        },
    }


@tool(summary="Read one frozen calibration task without projecting hidden input paths.", side_effects="none")
def get_task(task_id: str, **_: Any) -> dict[str, Any]:
    task = EvaluationRepository().get_task(str(task_id))
    public = {key: value for key, value in task.items() if key != "hidden_inputs"}
    public["hidden_input_count"] = len(task.get("hidden_inputs") or [])
    return {"ok": True, "task": public}


@tool(summary="Materialize one contamination-checked arm packet for a paired attempt.", side_effects="local_write")
def prepare_calibration_arm(
    task_id: str,
    arm_id: str,
    attempt_index: int,
    budget_view: str = "fixed_downstream",
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    task = repository.get_task(str(task_id))
    packet = prepare_arm(task, arm_id, attempt_index, budget_view=budget_view)
    return {"ok": True, "packet": repository.put_packet(packet)}


@tool(summary="Materialize every preregistered arm packet for one budget view.", side_effects="local_write")
def prepare_calibration_suite(
    task_id: str,
    budget_view: str = "fixed_downstream",
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    task = repository.get_task(str(task_id))
    stored = []
    for attempt_index in range(1, int(task["repetitions"]["attempts_per_arm"]) + 1):
        for arm_id in ARM_IDS:
            packet = repository.put_packet(
                prepare_arm(task, arm_id, attempt_index, budget_view=budget_view)
            )
            stored.append(
                {
                    "packet_id": packet["packet_id"],
                    "digest": packet["digest"],
                    "arm_id": packet["arm_id"],
                    "attempt_index": packet["attempt_index"],
                    "paired_seed": packet["paired_seed"],
                    "instruction_kinds": [item["kind"] for item in packet["instruction_inputs"]],
                    "context_digests": [item["context_digest"] for item in packet["artifact_inputs"]],
                }
            )
    return {
        "ok": True,
        "task_id": task["task_id"],
        "task_digest": task["digest"],
        "budget_view": budget_view,
        "count": len(stored),
        "packets": stored,
    }


@tool(summary="Independently score one candidate against the frozen calibration rubric.", side_effects="local_write")
def record_calibration_result(
    task_id: str,
    candidate: Mapping[str, Any],
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    task = repository.get_task(str(task_id))
    stored = repository.put_result(evaluate_candidate(task, candidate))
    return {
        "ok": True,
        "result": stored,
        "evidence_valid_completion": stored["metrics"]["evidence_valid_completion"],
    }


@tool(summary="Execute the hidden deterministic judge over one terminal Builder candidate.", side_effects="local_write")
def evaluate_builder_attempt(
    task_id: str,
    arm_id: str,
    attempt_index: int,
    candidate_id: str,
    development_session_id: str,
    builder_webspace_id: str,
    budget_view: str = "fixed_downstream",
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    task = repository.get_task(str(task_id))
    packet = repository.get_packet(task_id, arm_id, attempt_index, budget_view)
    session = development_sessions.get(development_session_id)
    enriched_instructions = []
    for descriptor in session.get("instruction_inputs") or []:
        restored = development_sessions.get_instruction(
            development_session_id, str(descriptor["kind"])
        )
        item = dict(descriptor)
        value = restored.get("value")
        if isinstance(value, Mapping) and value.get("digest"):
            item["declared_digest"] = str(value["digest"])
        enriched_instructions.append(item)
    if enriched_instructions:
        session = {**session, "instruction_inputs": enriched_instructions}
    state = automation.get_state(
        object_type="skill",
        object_id=candidate_id,
        webspace_id=builder_webspace_id,
    )
    projection = dict(state.get("automation") or {})
    if not projection.get("terminal"):
        return {"ok": False, "ready": False, "status": projection.get("status"), "automation": projection}
    validation = None
    prepare = None
    trial = None
    dataset = None
    verified = []
    collected = None
    errors = []
    if projection.get("status") == "completed":
        try:
            validation = developer_validation.validate_skill(candidate_id)
            if validation.get("ok"):
                developer_validation.activate_skill(candidate_id)
                prepare = developer_validation.invoke_skill(
                    candidate_id,
                    "prepare_attempt",
                    {"request_id": f"evaluation-{packet['packet_id']}", "stage": "workflow_smoke"},
                    timeout=120,
                )
                if prepare and prepare.get("execution_spec"):
                    trial = developer_validation.execute_spec(
                        candidate_id,
                        dict(prepare["execution_spec"]),
                        idempotency_key=f"smoke-{arm_id.lower()}-{attempt_index}",
                        timeout=300,
                    )
                dataset = developer_validation.invoke_skill(candidate_id, "dataset_status", {}, timeout=60)
                refs = [
                    dict(item.get("content_ref") or {})
                    for item in dict((trial or {}).get("documents") or {}).get("artifacts_index.json", {}).get("files", [])
                ]
                for ref in refs:
                    verified.append(
                        developer_validation.invoke_skill(
                            candidate_id,
                            "verify_artifact",
                            {"artifact": ref},
                            timeout=60,
                        )
                    )
                collected = developer_validation.invoke_skill(
                    candidate_id,
                    "collect_attempt",
                    {"attempt": {"status": "succeeded", "outputs": refs}},
                    timeout=60,
                )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    else:
        errors.append(f"Builder Automation ended with status {projection.get('status')}")
    candidate = build_independent_candidate(
        task=task,
        packet=packet,
        candidate_id=candidate_id,
        session=session,
        automation=projection,
        validation=validation,
        prepare=prepare,
        trial=trial,
        dataset=dataset,
        verified_artifacts=verified,
        collected=collected,
        operation_errors=errors,
    )
    stored = repository.put_result(evaluate_candidate(task, candidate))
    return {
        "ok": True,
        "ready": True,
        "result": stored,
        "evidence_valid_completion": stored["metrics"]["evidence_valid_completion"],
        "operation_errors": errors,
    }


@tool(summary="Summarize C0-C4 evidence-valid completion and failure attribution.", side_effects="none")
def summarize_calibration(
    task_id: str,
    budget_view: str = "fixed_downstream",
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    task = repository.get_task(str(task_id))
    summary = summarize(task, repository.results(str(task_id), budget_view=budget_view))
    lines = [
        f"- **{item['arm_id']}** — `{item['evidence_valid_completions']}/{item['completed']}` "
        f"evidence-valid · complete `{item['complete']}` · failures `{item['failure_stages']}`"
        for item in summary["arms"]
    ]
    content = (
        "## Research compilation calibration\n\n"
        + "\n".join(lines)
        + f"\n\n**Budget:** `{summary['budget_view']}` · **Frozen task:** `{summary['task_digest']}` "
        + f"· **complete:** `{summary['complete']}`"
    )
    return {"ok": True, "summary": summary, "content": content}


__all__ = [
    "ensure_schema",
    "freeze_calibration",
    "get_task",
    "prepare_calibration_arm",
    "prepare_calibration_suite",
    "record_calibration_result",
    "evaluate_builder_attempt",
    "summarize_calibration",
]
