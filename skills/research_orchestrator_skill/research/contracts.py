from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


_PROTOTYPE_MANAGED_FIELDS = {
    "schema",
    "schema_version",
    "direction",
    "revision",
    "parent_digest",
    "source_bundle_digest",
    "context_coverage",
    "admission_review",
    "created_at",
    "created_by",
    "digest",
}

_PLACEHOLDER_RE = re.compile(
    r"(?:\b(?:tbd|todo|placeholder)\b|"
    r"bounded operational condition|predeclared fixed or sequential condition|"
    r"exact dataset and version|consistent metrics|specific operational|"
    r"уточнить|не определено|заполнить|заменить)",
    re.I,
)
_EXACT_PLACEHOLDER_RE = re.compile(
    r"^(?:unknown|unspecified|not specified|уточнить|не определено)$",
    re.I,
)


def _has_placeholder(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_has_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_placeholder(item) for item in value)
    if not isinstance(value, str):
        return False
    normalized = value.strip().strip(".?!:;—–-").strip()
    return bool(
        _PLACEHOLDER_RE.search(value)
        or _EXACT_PLACEHOLDER_RE.fullmatch(normalized)
    )


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(value))
    payload.pop("digest", None)
    return f"sha256:{hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()}"


def load_schema(schema_name: str) -> dict[str, Any]:
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / schema_name
    return json.loads(schema_path.read_text(encoding="utf-8"))


def prototype_candidate_schema() -> dict[str, Any]:
    """Return the exact LLM-owned subset of ResearchPrototype's schema."""

    schema = copy.deepcopy(load_schema("research.prototype.v1.schema.json"))
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for field in _PROTOTYPE_MANAGED_FIELDS:
        properties.pop(field, None)
    schema["required"] = [
        field for field in schema.get("required") or [] if field not in _PROTOTYPE_MANAGED_FIELDS
    ]
    return schema


def validate(schema_name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    schema = load_schema(schema_name)
    errors = sorted(Draft202012Validator(schema).iter_errors(dict(value)), key=lambda item: list(item.absolute_path))
    if errors:
        issues = []
        for error in errors[:20]:
            location = ".".join(str(item) for item in error.absolute_path) or "$"
            issues.append(f"{location}: {error.message}")
        suffix = f"; plus {len(errors) - 20} more" if len(errors) > 20 else ""
        raise ValueError(f"{schema_name} invalid: {'; '.join(issues)}{suffix}")
    return dict(value)


def _check(checks: list[dict[str, Any]], check_id: str, gate: str, passed: bool, message: str) -> None:
    checks.append({"id": check_id, "gate": gate, "passed": bool(passed), "message": message})


def build_admission_review(value: Mapping[str, Any]) -> dict[str, Any]:
    """Build the deterministic scientific/automation gate owned by AdaOS.

    The LLM proposes content; it cannot author or waive these checks.
    """

    checks: list[dict[str, Any]] = []
    plan = value.get("experimental_plan") if isinstance(value.get("experimental_plan"), Mapping) else {}
    stages = [item for item in plan.get("stages") or [] if isinstance(item, Mapping)]
    smoke = [item for item in stages if item.get("evidence_class") == "workflow_smoke"]
    confirmatory = [item for item in stages if item.get("evidence_class") == "confirmatory"]
    _check(checks, "stages.workflow_smoke", "quality", bool(smoke), "experimental_plan requires an explicit workflow_smoke stage")
    _check(checks, "stages.smoke_no_inference", "quality", bool(smoke) and all(item.get("inference_allowed") is False for item in smoke), "workflow_smoke stages must set inference_allowed=false")
    _check(checks, "stages.confirmatory", "quality", bool(confirmatory), "experimental_plan requires an explicit confirmatory stage")
    _check(checks, "stages.confirmatory_inference", "quality", bool(confirmatory) and all(item.get("inference_allowed") is True for item in confirmatory), "confirmatory stages must set inference_allowed=true")

    comparators = [str(item).strip().lower() for item in plan.get("comparators") or [] if str(item).strip()]
    _check(checks, "design.comparators", "quality", len(set(comparators)) >= 2, "at least two distinct comparators are required")
    reproducibility = plan.get("reproducibility") if isinstance(plan.get("reproducibility"), Mapping) else {}
    streams = {
        str(item.get("id") or "").strip().lower()
        for item in reproducibility.get("rng_streams") or []
        if isinstance(item, Mapping)
    }
    required_streams = {"initialization", "sampling", "augmentation", "analysis"}
    _check(checks, "reproducibility.rng_streams", "quality", required_streams.issubset(streams), "named RNG streams must cover initialization, sampling, augmentation, and analysis")
    pairing = reproducibility.get("pairing") if isinstance(reproducibility.get("pairing"), Mapping) else {}
    invariant = {str(item).strip().lower() for item in pairing.get("invariant_fields") or [] if str(item).strip()}
    varied = {str(item).strip().lower() for item in pairing.get("varied_fields") or [] if str(item).strip()}
    _check(checks, "reproducibility.pairing", "quality", bool(invariant) and bool(varied) and invariant.isdisjoint(varied), "paired design must declare disjoint invariant_fields and varied_fields")
    allocation = pairing.get("allocation") if isinstance(pairing.get("allocation"), Mapping) else {}
    planned_units = list(allocation.get("planned_units") or [])
    sample_size = int(allocation.get("sample_size") or 0)
    allocation_valid = (
        allocation.get("predeclared") is True
        and bool(planned_units)
        and sample_size == len(planned_units)
    )
    _check(checks, "reproducibility.allocation", "quality", allocation_valid, "paired units must be predeclared and sample_size must equal the distinct planned_units")

    evaluation = value.get("evaluation_plan") if isinstance(value.get("evaluation_plan"), Mapping) else {}
    outcomes = [item for item in evaluation.get("outcomes") or [] if isinstance(item, Mapping)]
    primary = [item for item in outcomes if item.get("role") == "primary"]
    _check(checks, "evaluation.one_primary", "quality", len(primary) == 1, "evaluation_plan must declare exactly one primary outcome")
    stopping = evaluation.get("stopping_rule") if isinstance(evaluation.get("stopping_rule"), Mapping) else {}
    _check(checks, "evaluation.predeclared_stopping", "quality", stopping.get("adaptation_predeclared") is True, "stopping and adaptation rules must be predeclared")

    coverage = value.get("context_coverage") if isinstance(value.get("context_coverage"), Mapping) else {}
    total_sources = int(coverage.get("sources_total") or 0)
    represented_sources = int(coverage.get("sources_represented") or 0)
    unreadable = list(coverage.get("unreadable_sources") or [])
    _check(checks, "sources.coverage", "quality", total_sources > 0 and represented_sources == total_sources and not unreadable, "every source artifact must be represented by readable, disclosed context")
    admitted_refs = {
        str(ref)
        for item in coverage.get("items") or []
        if isinstance(item, Mapping)
        for ref in item.get("provenance_refs") or []
    }
    grounding = [item for item in value.get("source_grounding") or [] if isinstance(item, Mapping)]
    cited_refs = {str(ref) for item in grounding for ref in item.get("source_refs") or []}
    hypothesis_ids = {str(item.get("id") or "") for item in value.get("hypotheses") or [] if isinstance(item, Mapping)}
    grounded_ids = {str(item.get("claim_id") or "") for item in grounding}
    hypothesis_grounding = {
        str(item.get("claim_id") or "")
        for item in grounding
        if item.get("stance") == "hypothesis"
    }
    observed_claims = [
        item
        for item in grounding
        if item.get("stance") == "observed"
        and str(item.get("claim_id") or "") not in hypothesis_ids
    ]
    _check(checks, "sources.provenance", "quality", bool(cited_refs) and cited_refs.issubset(admitted_refs), "source_grounding may cite only context fragments actually supplied to the formulation model")
    _check(checks, "sources.hypotheses_grounded", "quality", bool(hypothesis_ids) and hypothesis_ids.issubset(grounded_ids), "every hypothesis id must have an explicit source-grounding record")
    _check(checks, "sources.hypothesis_stance", "quality", bool(hypothesis_ids) and hypothesis_ids.issubset(hypothesis_grounding), "every hypothesis must be grounded with stance=hypothesis, never promoted to an observed source fact")
    _check(checks, "sources.observations_separated", "quality", bool(observed_claims), "at least one independent observed source claim must be separated from hypothesis identifiers")

    requirements = [item for item in value.get("implementation_requirements") or [] if isinstance(item, Mapping)]
    requirement_ids = [str(item.get("id") or "") for item in requirements]
    requirement_categories = {str(item.get("category") or "") for item in requirements}
    acceptance = [item for item in value.get("acceptance_checks") or [] if isinstance(item, Mapping)]
    acceptance_ids = [str(item.get("id") or "") for item in acceptance]
    acceptance_categories = {str(item.get("category") or "") for item in acceptance}
    _check(checks, "automation.requirements", "quality", len(requirements) >= 5 and len(requirement_ids) == len(set(requirement_ids)), "implementation requirements must be typed, independently verifiable, and uniquely identified")
    _check(checks, "automation.requirement_coverage", "quality", {"execution", "data", "reproducibility", "observability", "evidence"}.issubset(requirement_categories), "implementation requirements must cover execution, data, reproducibility, observability, and evidence")
    _check(checks, "automation.acceptance", "quality", len(acceptance) >= 4 and len(acceptance_ids) == len(set(acceptance_ids)), "acceptance checks must be typed, observable, and uniquely identified")
    _check(checks, "automation.acceptance_coverage", "quality", {"workflow", "data_integrity", "reproducibility", "evidence"}.issubset(acceptance_categories), "acceptance checks must cover workflow, data integrity, reproducibility, and evidence")

    critical_fields = {
        "question": value.get("research_question"),
        "plan": plan,
        "evaluation": evaluation,
        "requirements": requirements,
        "acceptance": acceptance,
    }
    _check(checks, "automation.no_placeholders", "quality", not _has_placeholder(critical_fields), "automation-critical fields cannot contain unresolved placeholders")

    readiness = value.get("readiness") if isinstance(value.get("readiness"), Mapping) else {}
    open_questions = list(value.get("open_questions") or [])
    blocking_questions = list(readiness.get("blocking_questions") or [])
    _check(checks, "readiness.decision", "admission", readiness.get("decision") == "ready_for_automation", "readiness.decision must be ready_for_automation")
    _check(checks, "readiness.blockers", "admission", not blocking_questions, "ready_for_automation cannot retain blocking_questions")
    _check(checks, "readiness.open_questions", "admission", not open_questions, "ready_for_automation cannot retain unresolved open_questions")
    blockers = [item["message"] for item in checks if not item["passed"]]
    return {
        "schema": "adaos.research.admission_review.v1",
        "decision": "admitted" if not blockers else "needs_discussion",
        "checks": checks,
        "blockers": blockers,
    }


def prototype_quality_issues(value: Mapping[str, Any]) -> list[str]:
    review = build_admission_review(value)
    return [item["message"] for item in review["checks"] if item["gate"] == "quality" and not item["passed"]]


def prototype_admission_issues(value: Mapping[str, Any]) -> list[str]:
    try:
        validate("research.prototype.v1.schema.json", value)
    except ValueError as exc:
        return [str(exc)]
    expected = build_admission_review(value)
    if value.get("admission_review") != expected:
        return ["admission_review does not match the deterministic AdaOS review"]
    return list(expected["blockers"])


def materialize_prototype(
    value: Mapping[str, Any],
    *,
    direction_id: str,
    source_bundle_digest: str,
    context_coverage: Mapping[str, Any],
    revision: int,
    parent_digest: str | None,
    actor: str,
) -> dict[str, Any]:
    candidate = copy.deepcopy(dict(value))
    candidate.update(
        {
            "schema": "adaos.research.prototype.v1",
            "schema_version": "1.2.0",
            "direction": {"kind": "skill", "id": direction_id, "ref": f"skill:{direction_id}"},
            "revision": int(revision),
            "parent_digest": parent_digest,
            "source_bundle_digest": source_bundle_digest,
            "context_coverage": copy.deepcopy(dict(context_coverage)),
            "created_at": now(),
            "created_by": str(actor or "user:local"),
        }
    )
    candidate["admission_review"] = build_admission_review(candidate)
    candidate["digest"] = digest(candidate)
    return validate("research.prototype.v1.schema.json", candidate)


def materialize_automation_brief(
    *,
    direction_id: str,
    project: Mapping[str, Any],
    artifact_groups: list[Mapping[str, Any]],
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
        "project": {
            "id": project["id"],
            "ref": project["ref"],
            "version": project["version"],
            "manifest_digest": project["manifest_digest"],
            "source_path": project["source_path"],
        },
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
                "artifact_ref": item.get("artifact_ref"),
                "group_id": item.get("group_id"),
            }
            for item in source_bundle.get("sources") or []
        ],
        "artifact_groups": [
            {
                "ref": item["ref"],
                "group_id": item["group_id"],
                "manifest_digest": item["digest"],
                "root_path": item["root_path"],
                "manifest_path": item["manifest_path"],
            }
            for item in artifact_groups
        ],
        "development_scope": {
            "targets": [
                {
                    "ref": f"skill:{direction_id}",
                    "access": "read-write",
                    "context": "full",
                    "source_path": str(Path(project["source_path"]).parent.parent / "skills" / direction_id),
                }
            ],
            "context_members": [
                {"ref": "scenario:research_workbench", "relation": "presentation", "access": "read-only", "context": "contract"},
                {"ref": "skill:research_orchestrator_skill", "relation": "dependency", "access": "read-only", "context": "contract"},
            ],
            "artifact_inputs": [
                {"ref": item["ref"], "access": "read-only", "manifest_digest": item["digest"], "root_path": item["root_path"]}
                for item in artifact_groups
            ],
        },
        "implementation_requirements": implementation_requirements,
        "acceptance_checks": [
            {"id": "adaos.direction_boundary", "check": "Preserve the Project-owned direction-skill boundary; do not create a direction-specific scenario.", "evidence": "Project manifest and package inventory"},
            {"id": "adaos.data_ownership", "check": "Keep primary experimental data in this direction skill's scoped runtime data bucket.", "evidence": "Resolved capability bindings and runtime paths"},
            {"id": "adaos.runner_contract", "check": "Implement the declared runner contract and deterministic CPU smoke profile.", "evidence": "Native runner conformance and smoke reports"},
            {"id": "adaos.content_identity", "check": "Bind experiment inputs, code, environment, metrics and evidence by digest.", "evidence": "ContentRef and tracker records"},
            {"id": "adaos.historical_evidence", "check": "Treat imported notebook outputs as untrusted exploratory source material.", "evidence": "Evidence classification in produced records"},
            {"id": "adaos.native_validation", "check": "Pass native AdaOS skill validation, package tests and research runner conformance checks.", "evidence": "CLI validation and test reports"},
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


__all__ = ["build_admission_review", "canonical_json", "digest", "load_schema", "materialize_automation_brief", "materialize_prototype", "now", "prototype_admission_issues", "prototype_candidate_schema", "prototype_quality_issues", "validate"]
