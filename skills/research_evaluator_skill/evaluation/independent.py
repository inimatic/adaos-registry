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
    "cpu_workflow_smoke",
    "evidence_manifest",
)


def _check(check_id: str, passed: bool, refs: Sequence[str], detail: str) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": "pass" if passed else "fail",
        "evidence_refs": list(refs) if passed else list(refs),
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
    protocol_ok = bool(execution_spec) and all(
        (
            str(metadata.get("protocol_digest") or "") == expected_protocol,
            str(metadata.get("stage") or "") == "workflow_smoke",
            str(metadata.get("evidence_class") or "") == "workflow_smoke",
            int(metadata.get("epochs") or 0) == 3,
            list(metadata.get("seeds") or []) == ["seed-17"],
            metadata.get("inference_allowed") is False,
            int(resources.get("gpu_count") or 0) == 0,
            str(network.get("mode") or "") == "offline",
        )
    )
    validation_ok = bool(validation and validation.get("ok"))
    documents = dict((trial or {}).get("documents") or {})
    run_log = dict(documents.get("run_log.json") or {})
    audit = dict(documents.get("evaluation_audit.json") or {})
    evidence_index = dict(documents.get("artifacts_index.json") or {})
    trial_outputs = {str(item["path"]): dict(item) for item in (trial or {}).get("outputs") or []}
    smoke_ok = bool(trial and trial.get("ok")) and all(
        (
            run_log.get("stage") == "workflow_smoke",
            run_log.get("device") == "cpu",
            int(run_log.get("epochs_completed") or 0) == 3,
            list(run_log.get("seeds") or []) == ["seed-17"],
            run_log.get("inference_allowed") is False,
            str(run_log.get("evidence_class") or "") == "workflow_smoke",
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
        bool(item.get("verified")) for item in verified_artifacts
    )
    collection_ok = bool(collected) and all(
        (
            int(collected.get("tracker_session_calls") or 0) == 0,
            len(collected.get("artifact_refs") or []) == len(evidence_files),
        )
    )
    runner_ok = bool(dataset is not None) and bool(prepare and prepare.get("ok")) and verifier_ok and collection_ok
    evidence_ok = smoke_ok and identity_ok and verifier_ok and collection_ok
    checks = [
        _check("context_isolation", context_ok, refs, context_detail),
        _check("protocol_fidelity", protocol_ok, refs, "frozen smoke protocol preserved" if protocol_ok else "execution spec drifted from frozen protocol"),
        _check("native_skill_validation", validation_ok, [str((validation or {}).get("digest") or "")], "strict validation, probing and packaged tests passed" if validation_ok else "native validation or packaged tests failed"),
        _check("runner_conformance", runner_ok, refs, "public runner operations passed consumer checks" if runner_ok else "runner operation checks failed"),
        _check("cpu_workflow_smoke", smoke_ok, [str((trial or {}).get("digest") or "")], "real three-epoch CPU workflow smoke completed" if smoke_ok else "CPU smoke or no-test audit failed"),
        _check("evidence_manifest", evidence_ok, [str((trial or {}).get("digest") or "")], "content identities reconstructed and smoke remained non-confirmatory" if evidence_ok else "evidence identities or classification failed"),
    ]
    if operation_errors:
        detail = "; ".join(str(item) for item in operation_errors)
        for check in checks:
            if check["check_id"] in {"runner_conformance", "cpu_workflow_smoke", "evidence_manifest"} and check["status"] != "pass":
                check["detail"] = f"{check['detail']}: {detail}"
    observed = dict(dict(automation.get("budget_usage") or {}).get("observed") or {})
    return {
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
            "attempts": 1,
            "human_interventions": 0,
            "formulation_tokens": 0,
            "expert_minutes": 0,
        },
        "checks": checks,
    }


__all__ = ["build_independent_candidate"]
