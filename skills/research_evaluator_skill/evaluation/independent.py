from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from evaluation.contracts import digest


_CHECK_IDS = (
    "context_isolation",
    "protocol_fidelity",
    "native_skill_validation",
    "runner_conformance",
    "scientific_implementation",
    "cpu_workflow_smoke",
    "evidence_manifest",
)


def _check(check_id: str, passed: bool, refs: Sequence[str], detail: str) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": "pass" if passed else "fail",
        "evidence_refs": list(dict.fromkeys(str(ref).strip() for ref in refs if str(ref).strip())),
        "detail": str(detail),
    }


def _context_check(
    task: Mapping[str, Any],
    packet: Mapping[str, Any],
    session: Mapping[str, Any],
    candidate_id: str,
) -> tuple[bool, str]:
    expected_artifacts = [
        (
            str(item["ref"]),
            str(item["source_manifest_digest"]),
            str(item["context_digest"]),
            str(item["audience"]),
        )
        for item in packet["artifact_inputs"]
    ]
    actual_artifacts = [
        (
            str(item["ref"]),
            str(item["manifest_digest"]),
            str(item.get("context_digest") or ""),
            str(item.get("audience") or ""),
        )
        for item in session["artifact_inputs"]
    ]
    expected_instructions = [
        (str(item["kind"]), str(item["digest"])) for item in packet["instruction_inputs"]
    ]
    actual_instructions = [
        (str(item["kind"]), str(item.get("declared_digest") or item["content_digest"]))
        for item in session.get("instruction_inputs") or []
    ]
    targets = [
        str(item["ref"])
        for group in session["targets"].values()
        for item in group
    ]
    serialized = json.dumps(session, ensure_ascii=False, sort_keys=True)
    hidden_leaks = []
    for hidden in task["hidden_inputs"]:
        for forbidden in (hidden["input_id"], hidden["ref"], hidden["path"]):
            if str(forbidden) and str(forbidden) in serialized:
                hidden_leaks.append(str(hidden["input_id"]))
    problems = []
    if expected_artifacts != actual_artifacts:
        problems.append("artifact inputs differ from packet")
    if expected_instructions != actual_instructions:
        problems.append("instruction inputs differ from packet")
    if targets != [f"skill:{candidate_id}"]:
        problems.append("write target is not the disposable candidate only")
    if list(session["handoff"]["prohibited_actions"]) != list(packet["prohibited_actions"]):
        problems.append("prohibited actions differ from packet")
    budget = session["handoff"].get("execution_budget")
    if budget is not None:
        expected_budget = {"budget_view": packet["budget_view"], **dict(packet["budget"])}
        if dict(budget) != expected_budget:
            problems.append("execution budget differs from packet")
    profile = session["handoff"].get("agent_profile")
    if packet.get("agent_profile") and dict(profile or {}) != dict(packet["agent_profile"]):
        problems.append("agent profile differs from packet")
    if hidden_leaks:
        problems.append("hidden evaluator input leaked")
    return not problems, "; ".join(problems) or "exact packet inputs and target verified"


def build_independent_candidate(
    *,
    task: Mapping[str, Any],
    packet: Mapping[str, Any],
    candidate_id: str,
    session: Mapping[str, Any],
    automation: Mapping[str, Any],
    validation: Mapping[str, Any] | None,
    prepare: Mapping[str, Any] | None,
    trial: Mapping[str, Any] | None,
    dataset: Mapping[str, Any] | None,
    verified_artifacts: Sequence[Mapping[str, Any]],
    collected: Mapping[str, Any] | None,
    scientific_implementation: Mapping[str, Any] | None = None,
    operation_errors: Sequence[str] = (),
) -> dict[str, Any]:
    refs = [f"builder://automation/{candidate_id}", f"builder://development-session/{session['session_id']}"]
    context_ok, context_detail = _context_check(task, packet, session, candidate_id)
    execution_spec = (
        dict(prepare.get("execution_spec") or {}) if isinstance(prepare, Mapping) else {}
    )
    metadata = (
        dict(execution_spec.get("metadata") or {})
        if isinstance(execution_spec.get("metadata"), Mapping)
        else {}
    )
    resources = dict(execution_spec.get("resources") or {})
    network = dict(execution_spec.get("network") or {})
    expected_protocol = str(task.get("expected_protocol_digest") or "")
    expected_smoke = dict(task.get("expected_smoke_profile") or {})
    expected_stage = str(expected_smoke.get("stage_id") or "workflow_smoke")
    expected_evidence_class = str(
        expected_smoke.get("evidence_class") or "workflow_smoke"
    )
    expected_epochs = int(expected_smoke.get("epochs") or 3)
    expected_seeds = list(expected_smoke.get("seeds") or [17])
    expected_seed_labels = [f"seed-{int(item)}" for item in expected_seeds]
    expected_inference = bool(expected_smoke.get("inference_allowed", False))
    expected_gpu_count = int(expected_smoke.get("gpu_count") or 0)
    expected_network_mode = str(expected_smoke.get("network_mode") or "offline")
    expected_workload = dict(expected_smoke.get("workload") or {})
    expected_input_policy = dict(expected_smoke.get("input_policy") or {})
    expected_wall_seconds = int(expected_smoke.get("max_wall_seconds") or 0)
    protocol_fields: dict[str, tuple[Any, Any]] = {
        "protocol_digest": (
            str(metadata.get("protocol_digest") or ""),
            expected_protocol,
        ),
        "stage": (str(metadata.get("stage") or ""), expected_stage),
        "evidence_class": (
            str(metadata.get("evidence_class") or ""),
            expected_evidence_class,
        ),
        "epochs": (int(metadata.get("epochs") or 0), expected_epochs),
        "seeds": (list(metadata.get("seeds") or []), expected_seeds),
        "inference_allowed": (
            metadata.get("inference_allowed"),
            expected_inference,
        ),
        "gpu_count": (int(resources.get("gpu_count") or 0), expected_gpu_count),
        "network_mode": (str(network.get("mode") or ""), expected_network_mode),
    }
    if "max_wall_seconds" in expected_smoke:
        protocol_fields["wall_time_seconds"] = (
            int(resources.get("wall_time_s") or 0),
            expected_wall_seconds,
        )
    if "workload" in expected_smoke:
        protocol_fields["workload"] = (
            dict(metadata.get("workload") or {}),
            expected_workload,
        )
    if "input_policy" in expected_smoke:
        protocol_fields["input_policy"] = (
            dict(metadata.get("input_policy") or {}),
            expected_input_policy,
        )
    protocol_mismatches = [
        name for name, (actual, expected) in protocol_fields.items() if actual != expected
    ]
    protocol_ok = bool(execution_spec) and not protocol_mismatches
    validation_ok = bool(validation and validation.get("ok"))
    documents = dict((trial or {}).get("documents") or {})
    run_log = dict(documents.get("run_log.json") or {})
    audit = dict(documents.get("evaluation_audit.json") or {})
    evidence_index = dict(documents.get("artifacts_index.json") or {})
    run_workload = dict(run_log.get("workload") or {})
    workload_limits = [
        dict(item)
        for item in expected_workload.get("limits") or []
        if isinstance(item, Mapping)
    ]
    observed_workload = dict(run_workload.get("observed") or {})
    workload_ok = "workload" not in expected_smoke or (
        str(run_workload.get("mode") or "") == str(expected_workload.get("mode") or "")
        and [dict(item) for item in run_workload.get("limits") or []]
        == workload_limits
        and all(
            isinstance(observed_workload.get(str(item.get("name") or "")), int)
            and not isinstance(observed_workload.get(str(item.get("name") or "")), bool)
            and int(observed_workload[str(item.get("name") or "")])
            <= int(item["maximum"])
            for item in workload_limits
        )
    )
    run_network = dict(run_log.get("network") or {})
    provider = dict((trial or {}).get("provider") or {})
    extended_execution_contract = str(task.get("schema_version") or "") in {
        "1.6.0",
        "1.7.0",
        "1.8.0",
        "1.9.0",
    }
    network_ok = not extended_execution_contract or all(
        (
            str(run_network.get("mode") or "") == expected_network_mode,
            expected_network_mode != "offline" or run_network.get("accessed") is False,
            str(provider.get("network_intent") or "") == expected_network_mode,
            not bool(expected_smoke.get("network_enforcement_required"))
            or provider.get("network_enforced") is True,
        )
    )
    trial_outputs = {str(item["path"]): dict(item) for item in (trial or {}).get("outputs") or []}
    smoke_ok = bool(trial and trial.get("ok")) and all(
        (
            run_log.get("stage") == expected_stage,
            run_log.get("device") == str(expected_smoke.get("device") or "cpu"),
            int(run_log.get("epochs_completed") or 0) == expected_epochs,
            list(run_log.get("seeds") or []) == expected_seed_labels,
            run_log.get("inference_allowed") is expected_inference,
            str(run_log.get("evidence_class") or "") == expected_evidence_class,
            "input_policy" not in expected_smoke
            or dict(run_log.get("input_policy") or {}) == expected_input_policy,
            workload_ok,
            network_ok,
            int(dict(audit.get("per_stage") or {}).get("workflow_smoke", {}).get("test_evaluations_count") or 0) == 0,
            not list(audit.get("test_access") or []),
        )
    )
    evidence_files = [dict(item) for item in evidence_index.get("files") or []]
    identity_ok = bool(evidence_files)
    for item in evidence_files:
        output = trial_outputs.get(str(item.get("path") or ""))
        content_ref = dict(item.get("content_ref") or {})
        identity_ok = identity_ok and bool(output) and all(
            (
                str(item.get("digest") or "") == str((output or {}).get("digest") or ""),
                str(content_ref.get("digest") or "") == str((output or {}).get("digest") or ""),
                str(dict(content_ref.get("metadata") or {}).get("evidence_class") or "") == "workflow_smoke",
            )
        )
    verifier_ok = len(verified_artifacts) == len(evidence_files) and all(
        bool(item.get("ok") or item.get("verified")) for item in verified_artifacts
    )
    collected_artifacts = (
        list(collected.get("artifacts") or collected.get("artifact_refs") or [])
        if isinstance(collected, Mapping)
        else []
    )
    collection_ok = bool(collected) and all(
        (
            int(collected.get("tracker_session_calls") or 0) == 0,
            bool(collected.get("complete")),
            len(collected_artifacts) == len(evidence_files),
        )
    )
    runner_ok = bool(dataset is not None) and bool(prepare and prepare.get("ok")) and verifier_ok and collection_ok
    evidence_ok = smoke_ok and identity_ok and verifier_ok and collection_ok
    checks = [
        _check("context_isolation", context_ok, refs, context_detail),
        _check(
            "protocol_fidelity",
            protocol_ok,
            refs,
            "frozen smoke protocol preserved"
            if protocol_ok
            else "execution spec drifted in fields: "
            + ", ".join(protocol_mismatches or ["execution_spec"]),
        ),
        _check("native_skill_validation", validation_ok, [str((validation or {}).get("digest") or "")], "strict validation, probing and packaged tests passed" if validation_ok else "native validation or packaged tests failed"),
        _check("runner_conformance", runner_ok, refs, "public runner operations passed consumer checks" if runner_ok else "runner operation checks failed"),
    ]
    rubric_ids = {
        str(item.get("check_id") or "")
        for item in task.get("rubric", {}).get("checks") or []
        if isinstance(item, Mapping)
    }
    if "scientific_implementation" in rubric_ids:
        semantic = dict(scientific_implementation or {})
        checks.append(
            _check(
                "scientific_implementation",
                bool(semantic.get("ok")),
                list(semantic.get("evidence_refs") or refs),
                str(semantic.get("detail") or "scientific implementation was not independently evaluated"),
            )
        )
    checks.extend(
        [
            _check("cpu_workflow_smoke", smoke_ok, [str((trial or {}).get("digest") or "")], "real three-epoch CPU workflow smoke completed" if smoke_ok else "CPU smoke or no-test audit failed"),
            _check("evidence_manifest", evidence_ok, [str((trial or {}).get("digest") or "")], "content identities reconstructed and smoke remained non-confirmatory" if evidence_ok else "evidence identities or classification failed"),
        ]
    )
    if operation_errors:
        detail = "; ".join(str(item) for item in operation_errors)
        for check in checks:
            if check["check_id"] in {"runner_conformance", "cpu_workflow_smoke", "evidence_manifest"} and check["status"] != "pass":
                check["detail"] = f"{check['detail']}: {detail}"
    observed = dict(dict(automation.get("budget_usage") or {}).get("observed") or {})
    terminal_status = str(automation.get("status") or "").strip()
    terminal_failure = None
    if terminal_status and terminal_status != "completed":
        reported_stage = str(automation.get("failure_stage") or "").strip()
        stage = (
            reported_stage
            if reported_stage in {
                "source_understanding",
                "formulation",
                "operationalization",
                "engineering_compilation",
                "implementation",
                "runtime_infrastructure",
                "scientific_evaluation",
            }
            else "engineering_compilation"
        )
        terminal_failure = {
            "stage": stage,
            "code": f"builder_automation.{terminal_status}",
            "detail": str(automation.get("error") or "Builder Automation did not deliver a candidate."),
        }
    candidate = {
        "arm_id": packet["arm_id"],
        "attempt_index": packet["attempt_index"],
        "paired_seed": packet["paired_seed"],
        "budget_view": packet["budget_view"],
        "model": str(dict(packet.get("agent_profile") or {}).get("model") or "codex-local-default"),
        "environment_digest": digest({"environment": dict(packet.get("environment_spec") or {}), "candidate_source_digest": str((validation or {}).get("source_digest") or "")}),
        "protocol_digest": str(metadata.get("protocol_digest") or "") or None,
        "budget_usage": {
            "model_tokens": int(observed.get("model_tokens") or 0),
            "wall_seconds": float(observed.get("wall_seconds") or 0),
            "attempts": max(1, int(observed.get("attempts") or 0)),
            "human_interventions": 0,
            "formulation_tokens": 0,
            "expert_minutes": 0,
        },
        "checks": checks,
    }
    execution_started_at = str(
        automation.get("created_at") or session.get("created_at") or ""
    ).strip()
    if execution_started_at:
        # Builder owns this timestamp.  Keeping it in the evaluator input lets
        # the recomputable package prove the preregistered arm order without
        # granting the evaluator access to Builder's private state directory.
        candidate["execution_started_at"] = execution_started_at
    if terminal_failure is not None:
        candidate["failure"] = terminal_failure
    return candidate


__all__ = ["build_independent_candidate"]
