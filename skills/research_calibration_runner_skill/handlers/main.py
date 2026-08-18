from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from adaos.sdk.builder import development_sessions
from adaos.sdk.core.decorators import tool
from adaos.sdk.core.environment import runtime_identity as sdk_runtime_identity
from adaos.sdk.developer import compositions
from adaos.sdk.skills import invoke as invoke_skill


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_ARMS = {
    "C0_raw",
    "C1_reviewed_prose",
    "C2_staged",
    "C3_typed_execution",
    "C4_over_specified",
}


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _validate_packet(value: Mapping[str, Any]) -> dict[str, Any]:
    packet = dict(value)
    if packet.get("schema") != "adaos.research.calibration_packet.v1":
        raise ValueError("evaluator returned an unsupported calibration packet")
    identity = {key: item for key, item in packet.items() if key not in {"created_at", "digest"}}
    if str(packet.get("digest") or "") != _digest(identity):
        raise ValueError("calibration packet digest does not match its content")
    if packet.get("arm_id") not in _ARMS:
        raise ValueError("calibration packet arm is invalid")
    if not packet.get("artifact_inputs"):
        raise ValueError("calibration packet has no admitted source artifacts")
    prohibited = [str(item).strip() for item in packet.get("prohibited_actions") or []]
    if not prohibited:
        raise ValueError("calibration packet must fail closed with prohibited actions")
    return packet


def _packet(
    task_id: str,
    arm_id: str,
    attempt_index: int,
    budget_view: str,
) -> dict[str, Any]:
    response = invoke_skill(
        "research_evaluator_skill",
        "prepare_calibration_arm",
        {
            "task_id": str(task_id),
            "arm_id": str(arm_id),
            "attempt_index": int(attempt_index),
            "budget_view": str(budget_view),
        },
        timeout=120,
    )
    if not isinstance(response, Mapping) or not response.get("ok"):
        raise RuntimeError("independent evaluator did not return a calibration packet")
    packet = response.get("packet")
    if not isinstance(packet, Mapping):
        raise RuntimeError("independent evaluator returned no packet object")
    return _validate_packet(packet)


def _environment_preflight(task_id: str) -> dict[str, Any]:
    response = invoke_skill(
        "research_evaluator_skill",
        "get_task",
        {"task_id": str(task_id)},
        timeout=120,
    )
    if not isinstance(response, Mapping) or not response.get("ok"):
        raise RuntimeError("independent evaluator did not return the frozen task")
    task = response.get("task")
    evaluator_identity = response.get("runtime_identity")
    if not isinstance(task, Mapping) or not isinstance(evaluator_identity, Mapping):
        raise RuntimeError("frozen task environment identity is unavailable")
    expected = task.get("environment_spec")
    if not isinstance(expected, Mapping):
        raise RuntimeError("calibration task does not freeze its environment")
    local = sdk_runtime_identity()
    components = expected.get("component_versions")
    if not isinstance(components, Mapping):
        raise RuntimeError("calibration task does not freeze component versions")
    actual = {
        "core_commit": str(dict(local.get("core") or {}).get("git_commit") or ""),
        "core_source_tree_clean": dict(
            dict(local.get("core") or {}).get("source_tree") or {}
        ).get("clean"),
        "core_source_tree_digest": str(
            dict(dict(local.get("core") or {}).get("source_tree") or {}).get(
                "tracked_diff_digest"
            )
            or ""
        ),
        "python_version": str(local.get("python_version") or ""),
        "platform": str(local.get("platform") or ""),
        "runner_version": str(dict(local.get("current_skill") or {}).get("version") or ""),
        "evaluator_version": str(
            dict(evaluator_identity.get("current_skill") or {}).get("version") or ""
        ),
        "evaluator_core_commit": str(
            dict(evaluator_identity.get("core") or {}).get("git_commit") or ""
        ),
    }
    required = {
        "core_commit": str(expected.get("core_commit") or ""),
        "core_source_tree_clean": expected.get("core_source_tree_clean"),
        "core_source_tree_digest": str(expected.get("core_source_tree_digest") or ""),
        "python_version": str(expected.get("python_version") or ""),
        "platform": str(expected.get("platform") or ""),
        "runner_version": str(components.get("research_calibration_runner_skill") or ""),
        "evaluator_version": str(components.get("research_evaluator_skill") or ""),
        "evaluator_core_commit": str(expected.get("core_commit") or ""),
    }
    mismatches = [key for key, value in required.items() if actual.get(key) != value]
    if mismatches:
        raise RuntimeError(
            "calibration environment mismatch: "
            + ", ".join(f"{key} expected={required[key]!r} actual={actual[key]!r}" for key in mismatches)
        )
    return {"task_digest": str(task["digest"]), "expected": required, "actual": actual}


def _candidate_id(packet: Mapping[str, Any], explicit: str | None = None) -> str:
    requested = str(explicit or "").strip().lower()
    if requested:
        if not _ID_RE.fullmatch(requested):
            raise ValueError("candidate_id must be a lowercase AdaOS identifier")
        return requested
    arm = str(packet["arm_id"]).split("_", 1)[0].lower()
    view = "fts" if packet["budget_view"] == "fixed_total_system" else "fd"
    fingerprint = str(packet["digest"]).removeprefix("sha256:")[:12]
    return f"tlp_cal_{arm}_a{int(packet['attempt_index'])}_{view}_{fingerprint}"


def _ensure_project(candidate_id: str, packet: Mapping[str, Any]) -> dict[str, Any]:
    try:
        project = compositions.get(candidate_id)
        created = False
        profiles = set(project.get("profiles") or [])
        primary = next(
            item
            for item in project["components"]["owned"]
            if item.get("role") == "primary"
        )
        if (
            "adaos.research.direction.v1" in profiles
            or primary.get("exposure") != "project_only"
        ):
            replacement = {
                key: value
                for key, value in project.items()
                if key not in {"ref", "manifest_digest", "source_path"}
            }
            replacement["profiles"] = sorted(
                (profiles - {"adaos.research.direction.v1"})
                | {
                    "adaos.research.implementation.v1",
                    "adaos.research.calibration_candidate.v1",
                }
            )
            primary.update(
                {
                    "exposure": "project_only",
                    "lifecycle": "bound",
                    "relations": ["realizes"],
                }
            )
            replacement["components"]["owned"] = [
                primary if item.get("role") == "primary" else item
                for item in replacement["components"]["owned"]
            ]
            replacement["compatibility"] = {
                "required_contracts": ["adaos.research.calibration_packet.v1"],
                "validation_profiles": [
                    "project.conformance",
                    "research.calibration_candidate",
                ],
            }
            project = compositions.replace(
                candidate_id,
                replacement,
                expected_manifest_digest=str(project["manifest_digest"]),
            )
    except compositions.ProjectCompositionNotFound:
        result = compositions.create_with_primary_component(
            candidate_id,
            kind="skill",
            component_id=candidate_id,
            template="research_direction",
            title=f"[Calibration] {packet['arm_id']} attempt {packet['attempt_index']}",
            description=(
                "Disposable research-compiler calibration candidate. Its admitted context is "
                f"frozen by packet {packet['digest']}."
            ),
            profiles=(
                "adaos.research.implementation.v1",
                "adaos.research.calibration_candidate.v1",
            ),
            dependencies=(
                {
                    "ref": "project:adaos_research_platform",
                    "version": "^0.2",
                    "lifecycle": "shared",
                    "relations": ["uses"],
                },
            ),
            tags=["research-calibration", str(packet["arm_id"]).lower()],
            categories=("research", "development", "calibration"),
            member={
                "role": "primary",
                "exposure": "project_only",
                "lifecycle": "bound",
                "relations": ["realizes"],
            },
            compatibility={
                "required_contracts": ["adaos.research.calibration_packet.v1"],
                "validation_profiles": [
                    "project.conformance",
                    "research.calibration_candidate",
                ],
            },
            actor="skill:research_calibration_runner_skill",
        )
        project = result["project"]
        created = True
    primary_ref = next(
        str(item["ref"])
        for item in project["components"]["owned"]
        if item.get("role") == "primary"
    )
    if primary_ref != f"skill:{candidate_id}":
        raise ValueError("calibration Project primary target does not match candidate_id")
    return {"created": created, "project": project, "primary_ref": primary_ref}


def _artifact_source(ref: str, audience: str) -> dict[str, str]:
    match = re.fullmatch(
        r"artifact://skill/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", str(ref or "")
    )
    if not match:
        raise ValueError(f"unsupported calibration artifact ref: {ref}")
    return {"skill_id": match.group(1), "group_id": match.group(2), "audience": audience}


def _attach_instruction(session_id: str, item: Mapping[str, Any]) -> dict[str, Any]:
    source = Path(str(item["path"])).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"calibration input is unavailable: {item['input_id']}")
    kind = str(item["kind"])
    declared = str(item["digest"])
    if source.suffix.lower() == ".json":
        try:
            value = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"calibration JSON input is invalid: {item['input_id']}") from exc
        if isinstance(value, Mapping) and str(value.get("digest") or "") == declared:
            return development_sessions.attach_instruction(
                session_id,
                kind,
                value,
                expected_digest=declared,
                media_type="application/json",
            )
        return development_sessions.attach_instruction_file(
            session_id,
            kind,
            source,
            expected_digest=declared,
            media_type="application/json",
        )
    media_type = "text/markdown" if source.suffix.lower() in {".md", ".markdown"} else "text/plain"
    return development_sessions.attach_instruction_file(
        session_id,
        kind,
        source,
        expected_digest=declared,
        media_type=media_type,
    )


def _prepare(
    task_id: str,
    arm_id: str,
    attempt_index: int,
    budget_view: str,
    candidate_id: str | None,
) -> dict[str, Any]:
    environment = _environment_preflight(task_id)
    packet = _packet(task_id, arm_id, attempt_index, budget_view)
    if str(packet["task_digest"]) != str(environment["task_digest"]):
        raise ValueError("calibration packet task digest differs from environment preflight")
    candidate = _candidate_id(packet, candidate_id)
    project = _ensure_project(candidate, packet)
    automation_digest = next(
        (
            str(item["digest"])
            for item in packet["instruction_inputs"]
            if item["kind"] == "automation_brief"
        ),
        str(packet["digest"]),
    )
    session_id = "devcal2_" + str(packet["digest"]).removeprefix("sha256:")[:24]
    artifact_sources = [
        _artifact_source(str(item["ref"]), str(item.get("audience") or ""))
        for item in packet["artifact_inputs"]
    ]
    created = development_sessions.create(
        candidate,
        automation_brief_digest=automation_digest,
        research_prototype_digest=str(packet["task_digest"]),
        artifact_groups=[],
        artifact_sources=artifact_sources,
        request=str(packet["base_request"]),
        execution_budget={"budget_view": packet["budget_view"], **dict(packet["budget"])},
        agent_profile=dict(packet["agent_profile"]) if packet.get("agent_profile") else None,
        prohibited_actions=list(packet["prohibited_actions"]),
        primary_targets=[f"skill:{candidate}"],
        focus_ref=f"skill:{candidate}",
        session_id=session_id,
        actor="skill:research_calibration_runner_skill",
    )
    session = created["session"]
    expected_contexts = [str(item["context_digest"]) for item in packet["artifact_inputs"]]
    actual_contexts = [str(item.get("context_digest") or "") for item in session["artifact_inputs"]]
    if actual_contexts != expected_contexts:
        raise ValueError("Development Session artifact views differ from the frozen packet")
    expected_budget = {"budget_view": packet["budget_view"], **dict(packet["budget"])}
    if dict(session["handoff"].get("execution_budget") or {}) != expected_budget:
        raise ValueError("Development Session execution budget differs from the frozen packet")
    if packet.get("agent_profile") and dict(session["handoff"].get("agent_profile") or {}) != dict(packet["agent_profile"]):
        raise ValueError("Development Session agent profile differs from the frozen packet")
    attached = [_attach_instruction(session_id, item) for item in packet["instruction_inputs"]]
    builder_webspace_id = "builder-cal-" + str(packet["digest"]).removeprefix("sha256:")[:16]
    binding = development_sessions.bind(session_id, builder_webspace_id)
    return {
        "packet": packet,
        "candidate_id": candidate,
        "project": project,
        "session": binding["session"],
        "binding": binding["binding"],
        "attached": [item["instruction"] for item in attached],
        "environment": environment,
    }


def _public_preparation(prepared: Mapping[str, Any]) -> dict[str, Any]:
    packet = prepared["packet"]
    session = prepared["session"]
    return {
        "ok": True,
        "task_id": packet["task_id"],
        "task_digest": packet["task_digest"],
        "packet_id": packet["packet_id"],
        "packet_digest": packet["digest"],
        "arm_id": packet["arm_id"],
        "attempt_index": packet["attempt_index"],
        "paired_seed": packet["paired_seed"],
        "budget_view": packet["budget_view"],
        "candidate_id": prepared["candidate_id"],
        "project_ref": prepared["project"]["project"]["ref"],
        "project_created": prepared["project"]["created"],
        "session_id": session["session_id"],
        "builder_webspace_id": prepared["binding"]["builder_webspace_id"],
        "artifact_context_digests": [
            item.get("context_digest") for item in session["artifact_inputs"]
        ],
        "instruction_kinds": [item["kind"] for item in session.get("instruction_inputs") or []],
        "base_request": packet["base_request"],
        "budget": packet["budget"],
        "environment": prepared["environment"],
    }


@tool(summary="Return the runner runtime identity used by calibration preflight.", side_effects="none")
def environment_identity(**_: Any) -> dict[str, Any]:
    return {"ok": True, "runtime_identity": sdk_runtime_identity()}


@tool(summary="Prepare one isolated Builder candidate from a frozen calibration packet.", side_effects="local_write")
def prepare_attempt(
    task_id: str,
    arm_id: str,
    attempt_index: int,
    budget_view: str = "fixed_downstream",
    candidate_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    return _public_preparation(_prepare(task_id, arm_id, attempt_index, budget_view, candidate_id))


@tool(summary="Start native Builder Automation for one frozen calibration attempt.", side_effects="external_io")
def start_attempt(
    task_id: str,
    arm_id: str,
    attempt_index: int,
    budget_view: str = "fixed_downstream",
    candidate_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    prepared = _prepare(task_id, arm_id, attempt_index, budget_view, candidate_id)
    public = _public_preparation(prepared)
    arguments = {
        "object_type": "skill",
        "object_id": public["candidate_id"],
        "webspace_id": public["builder_webspace_id"],
        "conversation_id": f"conv.research.calibration.{public['packet_id']}",
    }
    if "automation_brief" not in set(public["instruction_kinds"]):
        arguments["implementation_brief"] = public["base_request"]
    result = invoke_skill(
        "builder_sdk_control_skill",
        "start_automation",
        arguments,
        timeout=120,
    )
    return {**public, "automation": result}


@tool(summary="Read native Builder Automation state for one prepared candidate.", side_effects="none")
def get_attempt(candidate_id: str, builder_webspace_id: str, **_: Any) -> dict[str, Any]:
    if not _ID_RE.fullmatch(str(candidate_id or "")):
        raise ValueError("candidate_id is invalid")
    result = invoke_skill(
        "builder_sdk_control_skill",
        "get_automation",
        {
            "object_type": "skill",
            "object_id": str(candidate_id),
            "webspace_id": str(builder_webspace_id),
        },
        timeout=120,
    )
    return {"ok": True, "candidate_id": candidate_id, "automation": result}


__all__ = ["environment_identity", "get_attempt", "prepare_attempt", "start_attempt"]
