from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from research.contracts import prototype_candidate_schema


STAGE_SCHEMA_VERSION = "1.0.0"
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
    data_policy_properties = copy.deepcopy(
        properties["experimental_plan"]["properties"]["data_policy"]["properties"]
    )
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
                ["dataset", "split_strategy", "evaluation_seal", "leakage_controls", "evaluation_access"],
            ),
            "reproducibility": properties["experimental_plan"]["properties"]["reproducibility"],
        },
        ["comparators", "stages", "data_policy", "reproducibility"],
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
        return [reference_map.get(str(ref), str(ref)) for ref in item.get("source_refs") or []]

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
