from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from research.contracts import prototype_candidate_schema


STAGE_SCHEMA_VERSION = "1.2.0"
DEFAULT_WORKFLOW_SMOKE_POLICY = {
    "device": "cpu",
    "epochs": 3,
    "seed_values": [17],
    "inference_allowed": False,
}
REQUIREMENT_CATEGORIES = ("execution", "data", "reproducibility", "observability", "evidence", "recovery", "analysis", "security")
CHECK_CATEGORIES = ("workflow", "data_integrity", "reproducibility", "evidence", "analysis", "failure_recovery", "security")
PROTOCOL_DECISION_AREAS = (
    "data",
    "comparators",
    "budget",
    "pairing",
    "outcomes",
    "uncertainty",
    "stopping",
    "multiplicity",
    "practical_significance",
)


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
            "constraints",
            "assumptions",
            "open_questions",
        )
    }
    hypothesis = copy.deepcopy(properties["hypotheses"]["items"])
    hypothesis["properties"]["source_refs"] = {
        "type": "array",
        "minItems": 1,
        "items": {"type": "string", "pattern": "^SRC-[0-9]{3}$"},
    }
    hypothesis["properties"]["effect_direction"] = {
        "enum": ["increase", "decrease", "difference"],
    }
    hypothesis["required"].extend(["source_refs", "effect_direction"])
    selected["hypotheses"] = {
        "type": "array",
        "minItems": 1,
        "maxItems": 1,
        "items": hypothesis,
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
                            "items": {"type": "string", "pattern": "^SRC-[0-9]{3}$"},
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
                            "items": {"type": "string", "pattern": "^SRC-[0-9]{3}$"},
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
    scientific_entity = _object(
        {
            "id": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]*$"},
            "label": {"type": "string", "minLength": 2},
            "specification": {"type": "string", "minLength": 10},
        },
        ["id", "label", "specification"],
    )
    selected["experimental_signature"] = _object(
        {
            "subject": {"type": "string", "minLength": 10},
            "dataset": scientific_entity,
            "baseline": scientific_entity,
            "intervention": scientific_entity,
            "intervention_boundary": {"type": "string", "minLength": 10},
            "primary_outcome": _object(
                {
                    "name": {"type": "string", "minLength": 2},
                    "measurement": {"type": "string", "minLength": 10},
                    "unit": {"type": "string", "minLength": 1},
                },
                ["name", "measurement", "unit"],
            ),
        },
        ["subject", "dataset", "baseline", "intervention", "intervention_boundary", "primary_outcome"],
    )
    return _object(selected, list(selected))


def protocol_design_schema() -> dict[str, Any]:
    properties = prototype_candidate_schema()["properties"]
    system_specification = copy.deepcopy(
        properties["experimental_plan"]["properties"]["system_specification"]
    )
    system_specification["properties"]["components"]["items"]["properties"]["source_refs"] = {
        "type": "array",
        "items": {"type": "string", "pattern": "^SRC-[0-9]{3}$"},
    }
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
    data_policy_properties = copy.deepcopy(
        properties["experimental_plan"]["properties"]["data_policy"]["properties"]
    )
    data_policy_properties["dataset_id"] = {
        "type": "string",
        "pattern": "^[a-z][a-z0-9_.-]*$",
    }
    data_policy_properties["evaluation_access"] = _object(
        {
            "development_split": {"type": "string", "minLength": 10},
            "selection_source": {
                "enum": ["validation", "fixed_predeclared_final_state", "not_applicable"],
            },
            "final_test_policy": {
                "enum": ["once_per_trained_unit_after_seal", "not_applicable"],
            },
            "test_feedback_prohibited": {"const": True},
        },
        [
            "development_split",
            "selection_source",
            "final_test_policy",
            "test_feedback_prohibited",
        ],
    )
    experimental_plan = _object(
        {
            "comparators": properties["experimental_plan"]["properties"]["comparators"],
            "comparison_design": _object(
                {
                    "arms": {
                        "type": "array",
                        "minItems": 2,
                        "items": _object(
                            {
                                "id": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]*$"},
                                "label": {"type": "string", "minLength": 2},
                                "role": {"enum": ["baseline", "intervention", "diagnostic"]},
                                "specification": {"type": "string", "minLength": 10},
                            },
                            ["id", "label", "role", "specification"],
                        ),
                    },
                    "primary_contrast": _object(
                        {
                            "minuend": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]*$"},
                            "subtrahend": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]*$"},
                        },
                        ["minuend", "subtrahend"],
                    ),
                },
                ["arms", "primary_contrast"],
            ),
            "system_specification": system_specification,
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
                data_policy_properties,
                ["dataset", "dataset_id", "split_strategy", "evaluation_seal", "leakage_controls", "evaluation_access"],
            ),
            "reproducibility": properties["experimental_plan"]["properties"]["reproducibility"],
        },
        ["comparators", "comparison_design", "system_specification", "stages", "data_policy", "reproducibility"],
    )
    experimental_plan["properties"]["reproducibility"]["properties"]["environment"]["additionalProperties"] = False
    decision = _object(
        {
            "value_summary": {"type": "string", "minLength": 10},
            "status": {"enum": ["source_derived", "policy_default", "proposed", "unresolved"]},
            "rationale": {"type": "string", "minLength": 10},
            "source_refs": {"type": "array", "items": {"type": "string", "pattern": "^SRC-[0-9]{3}$"}},
            "blocking_question": {"type": "string"},
        },
        ["value_summary", "status", "rationale", "source_refs", "blocking_question"],
    )
    evaluation_plan = copy.deepcopy(properties["evaluation_plan"])
    evaluation_plan["properties"].pop("decision_rules", None)
    evaluation_plan["properties"].pop("practical_significance", None)
    evaluation_plan["required"] = [
        item
        for item in evaluation_plan["required"]
        if item not in {"decision_rules", "practical_significance"}
    ]
    decision_spec = _object(
        {
            "effect_direction": {"enum": ["increase", "decrease", "difference"]},
            "practical_threshold": {"type": "number", "exclusiveMinimum": 0},
            "unit": {"type": "string", "minLength": 1},
        },
        ["effect_direction", "practical_threshold", "unit"],
    )
    return _object(
        {
            "assistant_message": properties["assistant_message"],
            "experimental_plan": experimental_plan,
            "evaluation_plan": evaluation_plan,
            "decision_spec": decision_spec,
            "decisions_by_area": _object(
                {area: decision for area in PROTOCOL_DECISION_AREAS},
                list(PROTOCOL_DECISION_AREAS),
            ),
        },
        ["assistant_message", "experimental_plan", "evaluation_plan", "decision_spec", "decisions_by_area"],
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
            "scientific_bindings": _object(
                {
                    "protocol_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    "dataset_id": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]*$"},
                    "baseline_arm_id": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]*$"},
                    "intervention_arm_id": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]*$"},
                    "primary_outcome_name": {"type": "string", "minLength": 2},
                    "runner_contract": {"const": "adaos.research.runner.v1"},
                },
                [
                    "protocol_digest",
                    "dataset_id",
                    "baseline_arm_id",
                    "intervention_arm_id",
                    "primary_outcome_name",
                    "runner_contract",
                ],
            ),
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
        ["assistant_message", "scientific_bindings", "requirements_by_category", "checks_by_category"],
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


def stage_schema(stage: str, *, allowed_source_refs: set[str] | None = None) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f"unknown formulation stage: {stage}")
    schema = STAGES[stage]()
    allowed = sorted(str(item) for item in (allowed_source_refs or set()) if str(item))
    if not allowed:
        return schema

    def constrain(value: Any, *, key: str = "") -> None:
        if isinstance(value, dict):
            if key == "source_refs" and isinstance(value.get("items"), dict):
                value["items"] = {"type": "string", "enum": allowed}
            for child_key, child in value.items():
                constrain(child, key=str(child_key))
        elif isinstance(value, list):
            for child in value:
                constrain(child, key=key)

    constrain(schema)
    return schema


def stage_quality_issues(
    stage: str,
    value: Mapping[str, Any],
    *,
    allowed_source_refs: set[str] | None = None,
    expected_effect_direction: str | None = None,
    expected_experimental_signature: Mapping[str, Any] | None = None,
    required_workflow_smoke: Mapping[str, Any] | None = None,
    expected_protocol_digest: str | None = None,
) -> list[str]:
    payload = validate_stage(stage, value)
    issues: list[str] = []
    if stage == "problem_frame":
        assessment = payload["source_assessment"]
        cited = {
            str(ref)
            for collection in (
                payload.get("hypotheses") or [],
                assessment.get("observed_facts") or [],
                assessment.get("author_interpretations") or [],
            )
            for item in collection
            if isinstance(item, Mapping)
            for ref in item.get("source_refs") or []
        }
        if allowed_source_refs is not None and (not cited or not cited.issubset(allowed_source_refs)):
            issues.append("problem frame must cite only source ids supplied to this stage")
        primary = payload["hypotheses"][0]
        if primary.get("effect_direction") == "difference" and re.search(
            r"(?i)opposite direction|\u043f\u0440\u043e\u0442\u0438\u0432\u043e\u043f\u043e\u043b\u043e\u0436\w*\s+\u0441\u0442\u043e\u0440\u043e\u043d",
            str(primary.get("falsification") or ""),
        ):
            issues.append("a two-sided difference hypothesis cannot treat the opposite direction as falsification")
        signature = payload["experimental_signature"]
        if signature["baseline"]["id"] == signature["intervention"]["id"]:
            issues.append("experimental_signature baseline and intervention ids must be distinct")
    elif stage == "protocol_design":
        plan = payload["experimental_plan"]
        comparison = plan["comparison_design"]
        arms = [dict(item) for item in comparison["arms"]]
        arm_ids = [str(item["id"]) for item in arms]
        arm_labels = [str(item["label"]) for item in arms]
        if len(arm_ids) != len(set(arm_ids)):
            issues.append("comparison_design arm ids must be unique")
        comparator_values = [str(item) for item in plan["comparators"]]
        if comparator_values not in (arm_labels, arm_ids):
            issues.append("comparators must be either the exact ordered arm ids or exact ordered arm labels")
        contrast = comparison["primary_contrast"]
        if contrast["minuend"] == contrast["subtrahend"]:
            issues.append("primary contrast must reference two distinct arms")
        if not {str(contrast["minuend"]), str(contrast["subtrahend"])}.issubset(set(arm_ids)):
            issues.append("primary contrast must reference declared comparison arms")
        if sum(1 for item in arms if item["role"] == "baseline") != 1:
            issues.append("comparison_design must declare exactly one baseline arm")
        if not any(item["role"] == "intervention" for item in arms):
            issues.append("comparison_design must declare at least one intervention arm")
        system_spec = plan["system_specification"]
        component_ids = [str(item["id"]) for item in system_spec["components"]]
        if len(component_ids) != len(set(component_ids)):
            issues.append("system_specification component ids must be unique")
        for component in system_spec["components"]:
            refs = {str(ref) for ref in component.get("source_refs") or []}
            status = str(component.get("decision_status") or "")
            if status == "source_derived" and not refs:
                issues.append(f"source-derived system component {component['id']} requires source_refs")
            if status != "source_derived" and refs:
                issues.append(f"non-source system component {component['id']} must leave source_refs empty")
            if allowed_source_refs is not None and not refs.issubset(allowed_source_refs):
                issues.append(f"system component {component['id']} cites an unavailable source id")
            setting_keys = [str(item["key"]) for item in component["settings"]]
            if len(setting_keys) != len(set(setting_keys)):
                issues.append(f"system component {component['id']} setting keys must be unique")
        if system_spec.get("unresolved_choices"):
            issues.append("system_specification unresolved_choices must be empty before automation")
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
        outcome_dependent_stop = re.compile(
            r"(?i)(?:statistically significant|significant (?:result|difference|improvement)|"
            r"target (?:accuracy|metric|score)|desired (?:accuracy|metric|score))"
        )
        if any(
            outcome_dependent_stop.search(str(condition))
            for item in stages
            for condition in item.get("stop_conditions") or []
        ):
            issues.append("stage stop conditions must not depend on a desired scientific outcome")
        outcomes = payload["evaluation_plan"]["outcomes"]
        if sum(1 for item in outcomes if item.get("role") == "primary") != 1:
            issues.append("evaluation_plan must contain exactly one primary outcome")
        decisions = payload["decisions_by_area"]
        for area, item in decisions.items():
            if item.get("status") == "source_derived" and not item.get("source_refs"):
                issues.append(f"source-derived decision {area} requires source_refs")
            question = str(item.get("blocking_question") or "").strip()
            if item.get("status") == "unresolved" and len(question) < 5:
                issues.append(f"unresolved decision {area} requires its own concrete blocking_question")
            if item.get("status") != "unresolved" and question:
                issues.append(f"resolved decision {area} must leave blocking_question empty")
        if expected_effect_direction and payload["decision_spec"]["effect_direction"] != expected_effect_direction:
            issues.append(
                "decision_spec.effect_direction must match the primary hypothesis "
                f"({expected_effect_direction})"
            )
        signature = dict(expected_experimental_signature or {})
        if signature:
            baseline = dict(signature.get("baseline") or {})
            intervention = dict(signature.get("intervention") or {})
            dataset = dict(signature.get("dataset") or {})
            outcome = dict(signature.get("primary_outcome") or {})
            expected_arms = [
                (str(baseline.get("id") or ""), str(baseline.get("label") or ""), "baseline"),
                (str(intervention.get("id") or ""), str(intervention.get("label") or ""), "intervention"),
            ]
            actual_primary_arms = [(str(item["id"]), str(item["label"]), str(item["role"])) for item in arms]
            if actual_primary_arms != expected_arms:
                issues.append("comparison_design must exactly preserve baseline and intervention identity from experimental_signature")
            expected_ids = [item[0] for item in expected_arms]
            expected_labels = [item[1] for item in expected_arms]
            if comparator_values not in (expected_ids, expected_labels):
                issues.append("comparators must exactly preserve ordered ids or labels from experimental_signature")
            if contrast != {"minuend": expected_arms[1][0], "subtrahend": expected_arms[0][0]}:
                issues.append("primary contrast must be intervention minus baseline from experimental_signature")
            data_policy = plan["data_policy"]
            if str(data_policy.get("dataset_id") or "") != str(dataset.get("id") or ""):
                issues.append("data_policy.dataset_id must exactly preserve experimental_signature dataset id")
            if str(data_policy.get("dataset") or "") != str(dataset.get("label") or ""):
                issues.append("data_policy.dataset must exactly preserve experimental_signature dataset label")
            if str(system_spec.get("subject") or "") != str(signature.get("subject") or ""):
                issues.append("system_specification.subject must exactly preserve experimental_signature subject")
            if str(system_spec.get("intervention_boundary") or "") != str(signature.get("intervention_boundary") or ""):
                issues.append("system_specification.intervention_boundary must exactly preserve experimental_signature boundary")
            primary_outcomes = [item for item in outcomes if item.get("role") == "primary"]
            expected_outcome = {
                "name": str(outcome.get("name") or ""),
                "measurement": str(outcome.get("measurement") or ""),
                "unit": str(outcome.get("unit") or ""),
            }
            if len(primary_outcomes) != 1 or {
                key: str(primary_outcomes[0].get(key) or "") for key in expected_outcome
            } != expected_outcome:
                issues.append("primary outcome must exactly preserve experimental_signature name, measurement, and unit")
        smoke_policy = dict(required_workflow_smoke or {})
        if smoke_policy:
            if len(smoke) != 1:
                issues.append("protocol must contain exactly one workflow_smoke stage")
            elif (
                str(smoke[0]["execution_profile"]["device"]) != str(smoke_policy.get("device"))
                or int(smoke[0]["budget"]["epochs"]) != int(smoke_policy.get("epochs") or 0)
                or list(smoke[0]["budget"]["seed_values"]) != list(smoke_policy.get("seed_values") or [])
                or bool(smoke[0]["inference_allowed"]) is not bool(smoke_policy.get("inference_allowed"))
            ):
                issues.append("workflow_smoke must exactly preserve the AdaOS execution policy")
    elif stage == "implementation_contract":
        obligations = [
            str(value)
            for grouped in (
                payload.get("requirements_by_category") or {},
                payload.get("checks_by_category") or {},
            )
            for items in grouped.values()
            for item in items or []
            if isinstance(item, Mapping)
            for value in item.values()
        ]
        per_epoch_test = re.compile(
            r"(?i)(?:test.{0,40}(?:per[- ]?epoch|every epoch|each epoch|\u043a\u0430\u0436\u0434\w*\s+\u044d\u043f\u043e\u0445)|"
            r"(?:per[- ]?epoch|every epoch|each epoch|\u043a\u0430\u0436\u0434\w*\s+\u044d\u043f\u043e\u0445).{0,40}test)"
        )
        if any(per_epoch_test.search(item) for item in obligations):
            issues.append("implementation contract must not observe final-test metrics per epoch")
        signature = dict(expected_experimental_signature or {})
        bindings = dict(payload.get("scientific_bindings") or {})
        if expected_protocol_digest and bindings.get("protocol_digest") != expected_protocol_digest:
            issues.append("scientific_bindings.protocol_digest must bind the exact protocol_design artifact")
        if signature:
            expected_bindings = {
                "dataset_id": str((signature.get("dataset") or {}).get("id") or ""),
                "baseline_arm_id": str((signature.get("baseline") or {}).get("id") or ""),
                "intervention_arm_id": str((signature.get("intervention") or {}).get("id") or ""),
                "primary_outcome_name": str((signature.get("primary_outcome") or {}).get("name") or ""),
                "runner_contract": "adaos.research.runner.v1",
            }
            if any(str(bindings.get(key) or "") != value for key, value in expected_bindings.items()):
                issues.append("scientific_bindings must exactly preserve the experimental_signature and runner contract")
    return list(dict.fromkeys(issues))


def schema_text_format(stage: str, *, schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Responses Structured Outputs envelope; callers may fall back to json_object."""

    return {
        "format": {
            "type": "json_schema",
            "name": f"research_{stage}_v1",
            "strict": True,
            "schema": provider_schema(schema or STAGES[stage]()),
        }
    }


def provider_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Project the full local contract onto the portable Structured Outputs subset.

    The provider constrains shape and enums. AdaOS still applies the complete
    JSON Schema plus semantic gates after generation. Keeping these contracts
    separate prevents a provider keyword limitation from silently weakening
    the accepted ResearchPrototype.
    """

    unsupported = {"uniqueItems", "minLength", "maxLength"}

    def visit(value: Any) -> Any:
        if isinstance(value, Mapping):
            projected = {
                str(key): visit(item)
                for key, item in value.items()
                if str(key) not in unsupported
            }
            if "type" not in projected and "const" in projected:
                projected["type"] = _json_type(projected["const"])
            if "type" not in projected and isinstance(projected.get("enum"), list):
                types = {_json_type(item) for item in projected["enum"]}
                if len(types) == 1:
                    projected["type"] = types.pop()
            return projected
        if isinstance(value, list):
            return [visit(item) for item in value]
        return copy.deepcopy(value)

    return visit(schema)


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return "null"


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


def _format_number(value: float) -> str:
    return f"{float(value):g}"


def _compile_decision_rule(spec: Mapping[str, Any], evaluation: Mapping[str, Any]) -> tuple[str, str]:
    direction = str(spec["effect_direction"])
    threshold = float(spec["practical_threshold"])
    threshold_text = _format_number(threshold)
    unit = str(spec["unit"]).strip()
    confidence = float(evaluation["uncertainty"]["confidence_level"]) * 100
    confidence_text = _format_number(confidence)
    if direction == "increase":
        practical = f"Предзаданный минимальный практический рост: Δ > +{threshold_text} {unit}."
        rule = (
            f"Поддержано: нижняя граница {confidence_text}% ДИ для Δ выше +{threshold_text} {unit}; "
            f"опровергнуто: верхняя граница не выше +{threshold_text} {unit}; "
            "иначе результат неконклюзивен."
        )
    elif direction == "decrease":
        practical = f"Предзаданное минимальное практическое снижение: Δ < -{threshold_text} {unit}."
        rule = (
            f"Поддержано: верхняя граница {confidence_text}% ДИ для Δ ниже -{threshold_text} {unit}; "
            f"опровергнуто: нижняя граница не ниже -{threshold_text} {unit}; "
            "иначе результат неконклюзивен."
        )
    else:
        practical = f"Предзаданная область практической эквивалентности: [-{threshold_text}; +{threshold_text}] {unit}."
        rule = (
            f"Поддержано: {confidence_text}% ДИ для Δ целиком выше +{threshold_text} {unit} "
            f"или целиком ниже -{threshold_text} {unit}; опровергнуто как практически эквивалентное: "
            f"ДИ целиком внутри [-{threshold_text}; +{threshold_text}] {unit}; иначе результат неконклюзивен."
        )
    return rule, practical


def _compile_selection_rule(selection_source: str) -> str:
    if selection_source == "validation":
        return (
            "Выбрать checkpoint с максимальной заранее объявленной primary validation-метрикой; "
            "при равенстве выбрать более раннюю эпоху. Test-метрики не используются."
        )
    if selection_source == "fixed_predeclared_final_state":
        return (
            "Использовать final state после полного предзаданного бюджета без выбора по метрике; "
            "test-метрики не используются."
        )
    return "Выбор checkpoint, модели или hyperparameter в этом протоколе не применяется."


def assemble_candidate(
    problem_frame: Mapping[str, Any],
    protocol_design: Mapping[str, Any],
    implementation_contract: Mapping[str, Any],
    *,
    source_ref_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compile validated stage artifacts into the LLM-owned prototype subset."""

    problem = validate_stage("problem_frame", problem_frame)
    protocol = validate_stage("protocol_design", protocol_design)
    implementation = validate_stage("implementation_contract", implementation_contract)
    protocol_issues = stage_quality_issues(
        "protocol_design",
        protocol,
        expected_effect_direction=str(problem["hypotheses"][0]["effect_direction"]),
        expected_experimental_signature=problem["experimental_signature"],
        required_workflow_smoke=DEFAULT_WORKFLOW_SMOKE_POLICY,
    )
    implementation_issues = stage_quality_issues(
        "implementation_contract",
        implementation,
        expected_experimental_signature=problem["experimental_signature"],
        expected_protocol_digest=stage_digest(protocol),
    )
    if protocol_issues or implementation_issues:
        raise ValueError("cross-stage formulation contract: " + "; ".join(protocol_issues + implementation_issues))
    hypothesis_direction = str(problem["hypotheses"][0]["effect_direction"])
    decision_spec = protocol["decision_spec"]
    if str(decision_spec["effect_direction"]) != hypothesis_direction:
        raise ValueError("protocol decision_spec.effect_direction must match the primary hypothesis")
    # Problem-frame questions are discovery input for protocol_design, not final
    # blockers. The later stage must explicitly resolve them into a bounded
    # choice or carry one concrete blocker on the corresponding decision area.
    questions = [
        str(item.get("blocking_question") or item.get("value_summary") or area or "unresolved protocol decision")
        for area, item in (protocol.get("decisions_by_area") or {}).items()
        if isinstance(item, Mapping) and item.get("status") == "unresolved"
    ]
    questions = list(dict.fromkeys(item for item in questions if item.strip()))
    reference_map = dict(source_ref_map or {})
    def resolve_refs(item: Mapping[str, Any]) -> list[str]:
        return list(
            dict.fromkeys(
                reference_map.get(str(ref), str(ref))
                for ref in item.get("source_refs") or []
            )
        )

    hypotheses = []
    grounding = []
    for item in problem["hypotheses"]:
        hypothesis = {
            key: copy.deepcopy(value)
            for key, value in item.items()
            if key not in {"source_refs", "effect_direction"}
        }
        hypotheses.append(hypothesis)
        grounding.append(
            {
                "claim_id": str(hypothesis["id"]),
                "claim": str(hypothesis["statement"]),
                "stance": "hypothesis",
                "source_refs": resolve_refs(item),
            }
        )
    assessment = problem["source_assessment"]
    for prefix, stance, collection in (
        ("OBS", "observed", assessment["observed_facts"]),
        ("INT", "interpretation", assessment["author_interpretations"]),
    ):
        for index, item in enumerate(collection, start=1):
            grounding.append(
                {
                    "claim_id": f"{prefix}-{index}",
                    "claim": str(item["claim"]),
                    "stance": stance,
                    "source_refs": resolve_refs(item),
                }
            )
    experimental_plan = copy.deepcopy(protocol["experimental_plan"])
    # Machine arm identities belong to the compiled ExperimentPlan.  The
    # ResearchPrototype retains the human-facing comparator descriptions and
    # remains backward compatible with the established scientific schema.
    experimental_plan.pop("comparison_design", None)
    for component in experimental_plan["system_specification"]["components"]:
        component["source_refs"] = resolve_refs(component)
    evaluation_access = experimental_plan["data_policy"]["evaluation_access"]
    evaluation_access["selection_rule"] = _compile_selection_rule(
        str(evaluation_access["selection_source"])
    )
    evaluation_plan = copy.deepcopy(protocol["evaluation_plan"])
    decision_rule, practical_significance = _compile_decision_rule(decision_spec, evaluation_plan)
    evaluation_plan["decision_rules"] = [decision_rule]
    evaluation_plan["practical_significance"] = practical_significance
    candidate = {
        "assistant_message": (
            f"Сформирована доказательно привязанная постановка «{problem['title']}»: "
            f"один основной вопрос, раздельные smoke и confirmatory стадии, "
            f"{len(_flatten_requirements(implementation['requirements_by_category']))} проверяемых инженерных требований. "
            + (
                f"Для автоматизации нужно разрешить ещё {len(questions)} блокирующих решений."
                if questions
                else "Блокирующих решений не осталось; кандидат готов к человеческому принятию."
            )
        ),
        "title": problem["title"],
        "background": problem["background"],
        "research_question": problem["research_question"],
        "hypotheses": hypotheses,
        "source_grounding": grounding,
        "evidence_policy": {
            "historical_results": "exploratory_source_only",
            "workflow_smoke": "workflow_evidence_only",
            "negative_results": "retain_and_report",
        },
        "experimental_plan": experimental_plan,
        "evaluation_plan": evaluation_plan,
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
    "DEFAULT_WORKFLOW_SMOKE_POLICY",
    "PROTOCOL_DECISION_AREAS",
    "REQUIREMENT_CATEGORIES",
    "STAGES",
    "STAGE_SCHEMA_VERSION",
    "assemble_candidate",
    "implementation_contract_schema",
    "problem_frame_schema",
    "provider_schema",
    "protocol_design_schema",
    "schema_text_format",
    "stage_schema",
    "stage_digest",
    "stage_quality_issues",
    "validate_stage",
]
