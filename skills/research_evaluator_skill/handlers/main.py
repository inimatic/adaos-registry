from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import tool
from adaos.sdk.core.environment import runtime_identity as sdk_runtime_identity
from adaos.domain.runtime_bindings import ContentRef
from adaos.sdk.builder import automation, development_sessions
from adaos.sdk.developer import validation as developer_validation
from adaos.sdk.data.blob import store as blob_store
from adaos.sdk.skills import invoke as invoke_skill


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from evaluation.contracts import ARM_IDS, freeze_task  # noqa: E402
from evaluation.harness import (  # noqa: E402
    build_recomputable_package,
    evaluate_candidate,
    prepare_arm,
    summarize,
)
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


@tool(summary="Derive and freeze a compact v1.2 calibration from an immutable audit task.", side_effects="local_write")
def derive_compact_calibration(
    baseline_task_id: str,
    task_id: str,
    core_commit: str,
    python_version: str,
    platform: str,
    model: str,
    skill_workspace_commit: str,
    orchestrator_version: str,
    evaluator_version: str,
    runner_version: str,
    reasoning_effort: str = "high",
    standard_prompt_version: str = "adaos-skill-realization/0.1.0",
    attempts_per_arm: int = 1,
    max_model_tokens: int = 5_000_000,
    max_wall_seconds: int = 10_800,
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    baseline = repository.get_task(str(baseline_task_id))
    local_identity = sdk_runtime_identity()
    runner_response = invoke_skill(
        "research_calibration_runner_skill",
        "environment_identity",
        {},
        timeout=120,
    )
    if not isinstance(runner_response, Mapping) or not runner_response.get("ok"):
        raise RuntimeError("calibration runner runtime identity is unavailable")
    runner_identity = runner_response.get("runtime_identity")
    if not isinstance(runner_identity, Mapping):
        raise RuntimeError("calibration runner returned no runtime identity")
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
    orchestrator_identity = response.get("runtime_identity")
    if not isinstance(orchestrator_identity, Mapping):
        raise RuntimeError("research orchestrator returned no runtime identity")
    expected_components = {
        "research_orchestrator_skill": str(orchestrator_version),
        "research_evaluator_skill": str(evaluator_version),
        "research_calibration_runner_skill": str(runner_version),
    }
    actual_components = {
        "research_orchestrator_skill": str(
            dict(orchestrator_identity.get("current_skill") or {}).get("version") or ""
        ),
        "research_evaluator_skill": str(
            dict(local_identity.get("current_skill") or {}).get("version") or ""
        ),
        "research_calibration_runner_skill": str(
            dict(runner_identity.get("current_skill") or {}).get("version") or ""
        ),
    }
    actual_environment = {
        "core_commit": str(dict(local_identity.get("core") or {}).get("git_commit") or ""),
        "python_version": str(local_identity.get("python_version") or ""),
        "platform": str(local_identity.get("platform") or ""),
        "core_source_tree_clean": dict(
            dict(local_identity.get("core") or {}).get("source_tree") or {}
        ).get("clean"),
        "core_source_tree_digest": str(
            dict(dict(local_identity.get("core") or {}).get("source_tree") or {}).get(
                "tracked_diff_digest"
            )
            or ""
        ),
    }
    expected_environment = {
        "core_commit": str(core_commit),
        "python_version": str(python_version),
        "platform": str(platform),
        "core_source_tree_clean": True,
        "core_source_tree_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    }
    mismatches = [
        key for key, value in expected_environment.items() if actual_environment.get(key) != value
    ]
    mismatches.extend(
        key for key, value in expected_components.items() if actual_components.get(key) != value
    )
    for identity in (orchestrator_identity, runner_identity):
        if str(dict(identity.get("core") or {}).get("git_commit") or "") != str(core_commit):
            mismatches.append("component_core_commit")
    if mismatches:
        raise RuntimeError(
            "cannot freeze a mismatched calibration environment: "
            + ", ".join(sorted(set(mismatches)))
        )
    input_store = blob_store("calibration_inputs")
    projected_inputs = {
        "research_compilation": dict(response["research_compilation"]),
        "automation_brief": dict(response["automation_brief"]),
    }
    replacements = {}
    for kind, value in projected_inputs.items():
        blob = input_store.put_json(f"{task_id}-{kind}.json", value)
        path = input_store.materialize_path(blob)
        replacements[kind] = {
            "path": str(path),
            "digest": str(value["digest"]),
            "blob_ref": str(blob["ref"]),
        }
    task = copy.deepcopy(baseline)
    for field in ("schema", "frozen_at", "digest"):
        task.pop(field, None)
    count = int(attempts_per_arm)
    seeds = list(baseline["repetitions"]["paired_seeds"])[:count]
    if count < 1 or len(seeds) != count:
        raise ValueError("attempts_per_arm exceeds the baseline paired workload seeds")
    task.update(
        {
            "schema_version": "1.2.0",
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
                "skill_workspace_commit": str(skill_workspace_commit),
                "component_versions": expected_components,
                "standard_prompt_version": str(standard_prompt_version),
                "core_source_tree_clean": True,
                "core_source_tree_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
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
            item.update({key: replacement[key] for key in ("path", "digest")})
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
            "blob_refs": {kind: item["blob_ref"] for kind, item in replacements.items()},
        },
    }


@tool(summary="Read one frozen calibration task without projecting hidden input paths.", side_effects="none")
def get_task(task_id: str, **_: Any) -> dict[str, Any]:
    task = EvaluationRepository().get_task(str(task_id))
    public = {key: value for key, value in task.items() if key != "hidden_inputs"}
    public["hidden_input_count"] = len(task.get("hidden_inputs") or [])
    return {"ok": True, "task": public, "runtime_identity": sdk_runtime_identity()}


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
        terminal_error = str(projection.get("error") or "").strip()
        errors.append(
            f"Builder Automation ended with status {projection.get('status')}"
            + (f": {terminal_error}" if terminal_error else "")
        )
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


@tool(summary="Read a path-free immutable calibration lineage for another skill.", side_effects="none")
def get_calibration_lineage(
    task_id: str,
    budget_view: str = "fixed_downstream",
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    task = repository.get_task(str(task_id))
    public_task = {key: value for key, value in task.items() if key != "hidden_inputs"}
    packets = repository.packets(str(task_id), budget_view=budget_view)
    results = repository.results(str(task_id), budget_view=budget_view)
    projected_packets = []
    for packet in packets:
        fingerprint = str(packet["digest"]).removeprefix("sha256:")[:12]
        arm = str(packet["arm_id"]).split("_", 1)[0].lower()
        view = "fts" if packet["budget_view"] == "fixed_total_system" else "fd"
        projected_packets.append(
            {
                "packet_id": packet["packet_id"],
                "digest": packet["digest"],
                "task_digest": packet["task_digest"],
                "arm_id": packet["arm_id"],
                "attempt_index": packet["attempt_index"],
                "paired_seed": packet["paired_seed"],
                "budget_view": packet["budget_view"],
                "candidate_id": f"tlp_cal_{arm}_a{int(packet['attempt_index'])}_{view}_{fingerprint}",
                "artifact_inputs": [
                    {
                        "ref": item["ref"],
                        "audience": item["audience"],
                        "context_digest": item["context_digest"],
                        "source_manifest_digest": item["source_manifest_digest"],
                        "items": [
                            {
                                key: artifact[key]
                                for key in ("artifact_id", "digest", "path", "size_bytes")
                                if key in artifact
                            }
                            for artifact in item.get("items") or []
                        ],
                    }
                    for item in packet.get("artifact_inputs") or []
                ],
                "instruction_inputs": [
                    {
                        key: item[key]
                        for key in ("input_id", "kind", "digest")
                        if key in item
                    }
                    for item in packet.get("instruction_inputs") or []
                ],
                "budget": copy.deepcopy(packet.get("budget") or {}),
                "created_at": packet["created_at"],
            }
        )
    return {
        "ok": True,
        "task": public_task,
        "budget_view": budget_view,
        "packets": projected_packets,
        "results": results,
        "summary": summarize(task, results),
        "runtime_identity": sdk_runtime_identity(),
    }


@tool(summary="Export a content-addressed, machine-recomputable calibration package.", side_effects="local_write")
def export_calibration_package(
    task_id: str,
    budget_view: str = "fixed_downstream",
    **_: Any,
) -> dict[str, Any]:
    repository = EvaluationRepository()
    task = repository.get_task(str(task_id))
    packets = repository.packets(str(task_id), budget_view=budget_view)
    results = repository.results(str(task_id), budget_view=budget_view)
    package = build_recomputable_package(
        task,
        packets,
        results,
        budget_view=budget_view,
    )
    blob = blob_store("calibration_packages").put_json(
        f"{task_id}-{budget_view}.json",
        package,
    )
    content_ref = ContentRef(
        uri=str(blob["ref"]),
        digest=str(blob["digest"]),
        size_bytes=int(blob["size_bytes"]),
        media_type=str(blob["media_type"]),
        owner_ref=str(blob["owner_ref"]),
        kind="calibration_package",
        metadata={
            "task_id": str(task_id),
            "budget_view": str(budget_view),
            "package_digest": package["digest"],
        },
    ).to_dict()
    return {
        "ok": True,
        "task_id": str(task_id),
        "budget_view": str(budget_view),
        "package_digest": package["digest"],
        "summary_digest": package["summary"]["digest"],
        "packet_count": len(package["packets"]),
        "result_count": len(package["results"]),
        "content_ref": content_ref,
        "exporter_runtime_identity": sdk_runtime_identity(),
    }


__all__ = [
    "ensure_schema",
    "freeze_calibration",
    "get_task",
    "get_calibration_lineage",
    "prepare_calibration_arm",
    "prepare_calibration_suite",
    "record_calibration_result",
    "evaluate_builder_attempt",
    "export_calibration_package",
    "summarize_calibration",
]
