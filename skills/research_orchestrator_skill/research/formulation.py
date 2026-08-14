from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from research.contracts import prototype_candidate_schema


STAGE_SCHEMA_VERSION = "1.0.0"
REQUIREMENT_CATEGORIES = ("execution", "data", "reproducibility", "observability", "evidence", "recovery", "analysis", "security")
CHECK_CATEGORIES = ("workflow", "data_integrity", "reproducibility", "evidence", "analysis", "failure_recovery", "security")


def _object(properties: Mapping[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": copy.deepcopy(dict(properties)),
        "required": list(required),
        "additionalProperties": False,
    }


def problem_frame_schema() -> dict[str, Any]:
    properties = prototype_candidate_schema()["properties"]
    selected = {
        name: properties[name]
        for name in (
            "assistant_message",
            "title",
            "background",
            "research_question",
            "hypotheses",
            "source_grounding",
            "constraints",
            "assumptions",
            "open_questions",
        )
    }
    selected["source_assessment"] = _object(
        {
            "sufficiency": {
                "enum": [
                    "sufficient_for_question",
                    "sufficient_for_protocol_draft",
                    "insufficient",
                ]
            },
            "observed_facts": {
                "type": "array",
                "minItems": 1,
                "items": _object(
                    {
                        "claim": {"type": "string", "minLength": 10},
                        "source_refs": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "pattern": "^artifact://"},
                        },
                    },
                    ["claim", "source_refs"],
                ),
            },
            "author_interpretations": {
                "type": "array",
                "items": _object(
                    {
                        "claim": {"type": "string", "minLength": 10},
                        "source_refs": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "pattern": "^artifact://"},
                        },
                    },
                    ["claim", "source_refs"],
                ),
            },
            "coverage_limitations": {"type": "array", "items": {"type": "string", "minLength": 5}},
            "unresolved_decisions": {"type": "array", "items": {"type": "string", "minLength": 5}},
        },
        ["sufficiency", "observed_facts", "author_interpretations", "coverage_limitations", "unresolved_decisions"],
    )
    return _object(selected, list(selected))


def protocol_design_schema() -> dict[str, Any]:
    properties = prototype_candidate_schema()["properties"]
    execution_profile = _object(
        {
            "node": {"type": "string", "minLength": 2},
            "device": {"enum": ["cpu", "cuda", "mps"]},
        },
        ["node", "device"],
    )
    budget = _object(
        {
            "epochs": {"type": "integer", "minimum": 1},
            "seed_values": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "integer"}]},
            },
            "max_wall_time_minutes": {"type": "integer", "minimum": 1},
        },
        ["epochs", "seed_values", "max_wall_time_minutes"],
    )
    experimental_plan = _object(
        {
            "comparators": properties["experimental_plan"]["properties"]["comparators"],
            "stages": {
                "type": "array",
                "minItems": 2,
                "items": _object(
                    {
                        "id": {"type": "string", "minLength": 1},
                        "purpose": {"type": "string", "minLength": 10},
                        "evidence_class": {"enum": ["workflow_smoke", "exploratory", "confirmatory"]},
                        "execution_profile": execution_profile,
                        "budget": budget,
                        "inference_allowed": {"type": "boolean"},
                        "stop_conditions": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 5}},
                    },
                    ["id", "purpose", "evidence_class", "execution_profile", "budget", "inference_allowed", "stop_conditions"],
                ),
            },
            "data_policy": _object(
                properties["experimental_plan"]["properties"]["data_policy"]["properties"],
                ["dataset", "split_strategy", "evaluation_seal", "leakage_controls"],
            ),
            "reproducibility": properties["experimental_plan"]["properties"]["reproducibility"],
        },
        ["comparators", "stages", "data_policy", "reproducibility"],
    )
    experimental_plan["properties"]["reproducibility"]["properties"]["environment"]["additionalProperties"] = False
    decision = _object(
        {
            "id": {"type": "string", "minLength": 2},
            "area": {
                "enum": [
                    "data",
                    "comparators",
                    "budget",
                    "pairing",
                    "outcomes",
                    "uncertainty",
                    "stopping",
                    "multiplicity",
                    "practical_significance",
                ]
            },
            "value_summary": {"type": "string", "minLength": 10},
            "status": {"enum": ["source_derived", "policy_default", "proposed", "unresolved"]},
            "rationale": {"type": "string", "minLength": 10},
            "source_refs": {"type": "array", "items": {"type": "string", "pattern": "^artifact://"}},
        },
        ["id", "area", "value_summary", "status", "rationale", "source_refs"],
    )
    return _object(
        {
            "assistant_message": properties["assistant_message"],
            "experimental_plan": experimental_plan,
            "evaluation_plan": properties["evaluation_plan"],
            "decision_register": {"type": "array", "minItems": 7, "items": decision},
            "open_questions": properties["open_questions"],
        },
        ["assistant_message", "experimental_plan", "evaluation_plan", "decision_register", "open_questions"],
    )


def implementation_contract_schema() -> dict[str, Any]:
    requirement = _object(
        {
            "requirement": {"type": "string", "minLength": 10},
            "verification": {"type": "string", "minLength": 10},
        },
        ["requirement", "verification"],
    )
    check = _object(
        {
            "check": {"type": "string", "minLength": 10},
            "evidence": {"type": "string", "minLength": 5},
        },
        ["check", "evidence"],
    )
    required_requirements = REQUIREMENT_CATEGORIES[:5]
    required_checks = CHECK_CATEGORIES[:4]
    return _object(
        {
            "assistant_message": {"type": "string"},
            "requirements_by_category": _object(
                {
                    category: {"type": "array", "minItems": 1 if category in required_requirements else 0, "items": requirement}
                    for category in REQUIREMENT_CATEGORIES
                },
                list(REQUIREMENT_CATEGORIES),
            ),
            "checks_by_category": _object(
                {
                    category: {"type": "array", "minItems": 1 if category in required_checks else 0, "items": check}
                    for category in CHECK_CATEGORIES
                },
                list(CHECK_CATEGORIES),
            ),
        },
        ["assistant_message", "requirements_by_category", "checks_by_category"],
    )


STAGES = {
    "problem_frame": problem_frame_schema,
    "protocol_design": protocol_design_schema,
    "implementation_contract": implementation_contract_schema,
}


def validate_stage(stage: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f"unknown formulation stage: {stage}")
    payload = dict(value)
    errors = sorted(Draft202012Validator(STAGES[stage]()).iter_errors(payload), key=lambda item: list(item.absolute_path))
    if errors:
        issues = []
        for error in errors[:20]:
            location = ".".join(str(item) for item in error.absolute_path) or "$"
            issues.append(f"{location}: {error.message}")
        suffix = f"; plus {len(errors) - 20} more" if len(errors) > 20 else ""
        raise ValueError(f"{stage}.v{STAGE_SCHEMA_VERSION} invalid: {'; '.join(issues)}{suffix}")
    return payload


def stage_quality_issues(stage: str, value: Mapping[str, Any], *, allowed_source_refs: set[str] | None = None) -> list[str]:
    payload = validate_stage(stage, value)
    issues: list[str] = []
    if stage == "problem_frame":
        hypotheses = {str(item.get("id") or "") for item in payload.get("hypotheses") or [] if isinstance(item, Mapping)}
        grounding = [item for item in payload.get("source_grounding") or [] if isinstance(item, Mapping)]
        grounded_hypotheses = {str(item.get("claim_id") or "") for item in grounding if item.get("stance") == "hypothesis"}
        if not hypotheses.issubset(grounded_hypotheses):
            issues.append("every hypothesis id requires a source_grounding record with stance=hypothesis")
        if not any(item.get("stance") == "observed" and str(item.get("claim_id") or "") not in hypotheses for item in grounding):
            issues.append("at least one source observation must be separate from hypothesis ids")
        cited = {str(ref) for item in grounding for ref in item.get("source_refs") or []}
        if allowed_source_refs is not None and (not cited or not cited.issubset(allowed_source_refs)):
            issues.append("source_grounding must cite only provenance refs supplied to this stage")
    elif stage == "protocol_design":
        plan = payload["experimental_plan"]
        stages = [item for item in plan["stages"] if isinstance(item, Mapping)]
        smoke = [item for item in stages if item.get("evidence_class") == "workflow_smoke"]
        confirmation = [item for item in stages if item.get("evidence_class") == "confirmatory"]
        if not smoke or not all(item.get("inference_allowed") is False for item in smoke):
            issues.append("declare a workflow_smoke stage with inference_allowed=false")
        if not confirmation or not all(item.get("inference_allowed") is True for item in confirmation):
            issues.append("declare a confirmatory stage with inference_allowed=true")
        streams = {str(item.get("id") or "") for item in plan["reproducibility"]["rng_streams"]}
        if not {"initialization", "sampling", "augmentation", "analysis"}.issubset(streams):
            issues.append("rng_streams must cover initialization, sampling, augmentation, and analysis")
        pairing = plan["reproducibility"]["pairing"]
        invariant = set(str(item) for item in pairing["invariant_fields"])
        varied = set(str(item) for item in pairing["varied_fields"])
        if invariant & varied:
            issues.append("pairing invariant_fields and varied_fields must be disjoint")
        allocation = pairing["allocation"]
        if int(allocation["sample_size"]) != len(allocation["planned_units"]):
            issues.append("pairing allocation sample_size must equal planned_units length")
        outcomes = payload["evaluation_plan"]["outcomes"]
        if sum(1 for item in outcomes if item.get("role") == "primary") != 1:
            issues.append("evaluation_plan must contain exactly one primary outcome")
        areas = {str(item.get("area") or "") for item in payload["decision_register"] if isinstance(item, Mapping)}
        required_areas = {"data", "comparators", "budget", "pairing", "outcomes", "uncertainty", "stopping", "multiplicity", "practical_significance"}
        if not required_areas.issubset(areas):
            issues.append("decision_register must cover every protocol decision area")
        for item in payload["decision_register"]:
            if item.get("status") == "source_derived" and not item.get("source_refs"):
                issues.append(f"source-derived decision {item.get('id')} requires source_refs")
            if item.get("status") == "unresolved" and not payload.get("open_questions"):
                issues.append("unresolved decisions require an explicit open question")
    return list(dict.fromkeys(issues))


def schema_text_format(stage: str) -> dict[str, Any]:
    """Responses Structured Outputs envelope; callers may fall back to json_object."""

    return {
        "format": {
            "type": "json_schema",
            "name": f"research_{stage}_v1",
            "strict": True,
            "schema": STAGES[stage](),
        }
    }


def stage_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _flatten_requirements(grouped: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for category in REQUIREMENT_CATEGORIES:
        for item in grouped.get(category) or []:
            if isinstance(item, Mapping):
                result.append(
                    {
                        "id": f"REQ-{len(result) + 1}",
                        "category": category,
                        "requirement": str(item.get("requirement") or ""),
                        "verification": str(item.get("verification") or ""),
                    }
                )
    return result


def _flatten_checks(grouped: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for category in CHECK_CATEGORIES:
        for item in grouped.get(category) or []:
            if isinstance(item, Mapping):
                result.append(
                    {
                        "id": f"AC-{len(result) + 1}",
                        "category": category,
                        "check": str(item.get("check") or ""),
                        "evidence": str(item.get("evidence") or ""),
                    }
                )
    return result


def assemble_candidate(
    problem_frame: Mapping[str, Any],
    protocol_design: Mapping[str, Any],
    implementation_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile validated stage artifacts into the LLM-owned prototype subset."""

    problem = validate_stage("problem_frame", problem_frame)
    protocol = validate_stage("protocol_design", protocol_design)
    implementation = validate_stage("implementation_contract", implementation_contract)
    questions = list(dict.fromkeys([*list(problem.get("open_questions") or []), *list(protocol.get("open_questions") or [])]))
    unresolved = [
        str(item.get("value_summary") or item.get("id") or "unresolved protocol decision")
        for item in protocol.get("decision_register") or []
        if isinstance(item, Mapping) and item.get("status") == "unresolved"
    ]
    questions = list(dict.fromkeys([*questions, *unresolved]))
    candidate = {
        "assistant_message": "\n\n".join(
            str(item).strip()
            for item in (
                problem.get("assistant_message"),
                protocol.get("assistant_message"),
                implementation.get("assistant_message"),
            )
            if str(item or "").strip()
        ),
        "title": problem["title"],
        "background": problem["background"],
        "research_question": problem["research_question"],
        "hypotheses": problem["hypotheses"],
        "source_grounding": problem["source_grounding"],
        "evidence_policy": {
            "historical_results": "exploratory_source_only",
            "workflow_smoke": "workflow_evidence_only",
            "negative_results": "retain_and_report",
        },
        "experimental_plan": protocol["experimental_plan"],
        "evaluation_plan": protocol["evaluation_plan"],
        "constraints": problem["constraints"],
        "assumptions": problem["assumptions"],
        "open_questions": questions,
        "implementation_requirements": _flatten_requirements(implementation["requirements_by_category"]),
        "acceptance_checks": _flatten_checks(implementation["checks_by_category"]),
        "readiness": {
            "decision": "needs_discussion" if questions else "ready_for_automation",
            "blocking_questions": questions,
        },
    }
    errors = sorted(Draft202012Validator(prototype_candidate_schema()).iter_errors(candidate), key=lambda item: list(item.absolute_path))
    if errors:
        raise ValueError("assembled ResearchPrototype candidate invalid: " + "; ".join(error.message for error in errors[:20]))
    return candidate


__all__ = [
    "CHECK_CATEGORIES",
    "REQUIREMENT_CATEGORIES",
    "STAGES",
    "STAGE_SCHEMA_VERSION",
    "assemble_candidate",
    "implementation_contract_schema",
    "problem_frame_schema",
    "protocol_design_schema",
    "schema_text_format",
    "stage_digest",
    "stage_quality_issues",
    "validate_stage",
]
