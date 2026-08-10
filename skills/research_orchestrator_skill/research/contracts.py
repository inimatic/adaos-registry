from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(value))
    payload.pop("digest", None)
    return f"sha256:{hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()}"


def validate(schema_name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / schema_name
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(dict(value)), key=lambda item: list(item.absolute_path))
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "$"
        raise ValueError(f"{schema_name} invalid at {location}: {error.message}")
    return dict(value)


def prototype_admission_issues(value: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    try:
        validate("research.prototype.v1.schema.json", value)
    except ValueError as exc:
        issues.append(str(exc))
        return issues
    plan = value.get("experimental_plan") if isinstance(value.get("experimental_plan"), Mapping) else {}
    stages = [item for item in plan.get("stages") or [] if isinstance(item, Mapping)]
    smoke = [item for item in stages if item.get("evidence_class") == "workflow_smoke"]
    if not smoke:
        issues.append("experimental_plan requires an explicit workflow_smoke stage before scientific execution")
    if any(item.get("inference_allowed") is not False for item in smoke):
        issues.append("workflow_smoke stages must set inference_allowed=false")
    confirmatory = [item for item in stages if item.get("evidence_class") == "confirmatory"]
    if confirmatory and any(item.get("inference_allowed") is not True for item in confirmatory):
        issues.append("confirmatory stages must explicitly set inference_allowed=true")
    readiness = value.get("readiness") if isinstance(value.get("readiness"), Mapping) else {}
    if readiness.get("decision") == "ready_for_automation" and readiness.get("blocking_questions"):
        issues.append("ready_for_automation cannot retain blocking_questions")
    return issues


def materialize_prototype(
    value: Mapping[str, Any],
    *,
    direction_id: str,
    source_bundle_digest: str,
    revision: int,
    parent_digest: str | None,
    actor: str,
) -> dict[str, Any]:
    candidate = copy.deepcopy(dict(value))
    candidate.update(
        {
            "schema": "adaos.research.prototype.v1",
            "schema_version": "1.0.0",
            "direction": {"kind": "skill", "id": direction_id, "ref": f"skill:{direction_id}"},
            "revision": int(revision),
            "parent_digest": parent_digest,
            "source_bundle_digest": source_bundle_digest,
            "created_at": now(),
            "created_by": str(actor or "user:local"),
        }
    )
    candidate["digest"] = digest(candidate)
    return validate("research.prototype.v1.schema.json", candidate)


def materialize_automation_brief(
    *,
    direction_id: str,
    source_bundle: Mapping[str, Any],
    prototype: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    actor: str,
) -> dict[str, Any]:
    implementation_requirements = list(prototype.get("implementation_requirements") or [])
    brief = {
        "schema": "adaos.research.automation_brief.v1",
        "schema_version": "1.0.0",
        "brief_id": f"automation-{prototype['digest'].removeprefix('sha256:')[:20]}",
        "direction": {"kind": "skill", "id": direction_id, "ref": f"skill:{direction_id}"},
        "source_bundle_digest": str(source_bundle["digest"]),
        "prototype_digest": str(prototype["digest"]),
        "builder_checkpoint": {
            "package_digest": checkpoint.get("package_digest"),
            "source_revision": checkpoint.get("source_revision") or checkpoint.get("commit"),
            "source_tree": checkpoint.get("source_tree"),
            "sha256": checkpoint.get("sha256"),
        },
        "objective": str(prototype["research_question"]),
        "research_prototype": dict(prototype),
        "source_inventory": [
            {
                "source_id": item.get("source_id"),
                "name": item.get("name"),
                "digest": item.get("digest"),
                "media_type": item.get("media_type"),
                "role": item.get("role"),
                "analysis": item.get("analysis"),
            }
            for item in source_bundle.get("sources") or []
        ],
        "implementation_requirements": implementation_requirements,
        "acceptance_checks": [
            "Preserve the one-direction-one-skill boundary; do not create a direction-specific scenario.",
            "Keep primary experimental data in this direction skill's scoped runtime data bucket.",
            "Implement the declared runner contract and deterministic CPU smoke profile.",
            "Bind experiment inputs, code, environment, metrics and evidence by digest.",
            "Treat imported notebook outputs as untrusted exploratory source material.",
            "Pass native AdaOS skill validation, package tests and research runner conformance checks.",
            *list(prototype.get("acceptance_checks") or []),
        ],
        "prohibited_actions": [
            "Do not start scientific execution as part of code generation.",
            "Do not mutate research_manager_skill governance records directly.",
            "Do not infer secrets, permissions, datasets or external services not declared by the accepted prototype.",
            "Do not claim confirmation from the historical notebook or from a three-epoch workflow smoke run.",
        ],
        "handoff_state": "ready_for_codex",
        "created_at": now(),
        "created_by": str(actor or "user:local"),
    }
    brief["digest"] = digest(brief)
    return validate("research.automation_brief.v1.schema.json", brief)


__all__ = ["canonical_json", "digest", "materialize_automation_brief", "materialize_prototype", "now", "prototype_admission_issues", "validate"]
