from __future__ import annotations

from collections.abc import Mapping

from research.formulation import STAGES, assemble_candidate, provider_schema, stage_quality_issues, stage_schema, validate_stage
from research.compiler import build_compilation, project_execution_compilation


REF = "SRC-001"
EXACT_REF = "artifact://skill/tlp/part0/notebook#cell=5"


def _problem() -> dict:
    return {
        "assistant_message": "Сформулирован один проверяемый вопрос.",
        "title": "TLP against MaxPool",
        "background": "Исторический notebook мотивирует новое парное сравнение, но не доказывает преимущество TLP.",
        "research_question": "Меняет ли TLP validation accuracy относительно MaxPool при парной инициализации?",
        "hypotheses": [{"id": "H1", "statement": "TLP изменяет парную validation accuracy.", "falsification": "Парный контраст совместим с практически нулевым эффектом.", "status": "proposed", "source_refs": [REF], "effect_direction": "difference"}],
        "constraints": ["Первый профиль исполняется на CPU."],
        "assumptions": ["STL-10 доступен через объявленный data capability."],
        "open_questions": [],
        "source_assessment": {
            "sufficiency": "sufficient_for_protocol_draft",
            "observed_facts": [{"claim": "Notebook содержит два pooling comparator.", "source_refs": [REF]}],
            "author_interpretations": [],
            "coverage_limitations": ["Исторические outputs не являются подтверждением."],
            "unresolved_decisions": [],
        },
    }


def _protocol(*, unresolved: bool = False) -> dict:
    seed_values = [17, 23]
    decision_areas = ["data", "comparators", "budget", "pairing", "outcomes", "uncertainty", "stopping", "multiplicity", "practical_significance"]
    return {
        "assistant_message": "Предложен двухстадийный протокол.",
        "experimental_plan": {
            "comparators": ["MaxPool", "TLP"],
            "system_specification": {
                "subject": "Paired STL-10 pooling classifier",
                "components": [
                    {
                        "id": "convnet",
                        "role": "subject",
                        "specification": "Fixed source-derived convolutional classifier for both paired arms.",
                        "settings": [
                            {"key": "layer_sequence", "value": "conv32,pool1,conv64,pool2,conv128,pool3,fc256,fc10"},
                            {"key": "optimizer", "value": "Adam(lr=0.001)"},
                        ],
                        "decision_status": "source_derived",
                        "source_refs": [REF],
                    },
                    {
                        "id": "pool2",
                        "role": "intervention",
                        "specification": "Only pool2 varies between MaxPool and centered channel-wise max-plus pooling.",
                        "settings": [{"key": "window", "value": "2x2 stride 2"}],
                        "decision_status": "source_derived",
                        "source_refs": [REF],
                    },
                ],
                "locked_invariants": ["Every non-pool2 parameter and data stream remains identical."],
                "intervention_boundary": "Only the pool2 operator and its trainability may differ between arms.",
                "unresolved_choices": [],
            },
            "stages": [
                {"id": "smoke", "purpose": "Проверить исполнимость и сбор evidence.", "evidence_class": "workflow_smoke", "execution_profile": {"node": "current", "device": "cpu"}, "budget": {"epochs": 3, "seed_values": [17], "max_wall_time_minutes": 30}, "inference_allowed": False, "stop_conditions": ["Остановить при нечисловом loss."]},
                {"id": "confirmatory", "purpose": "Оценить заранее объявленный парный контраст.", "evidence_class": "confirmatory", "execution_profile": {"node": "member", "device": "cpu"}, "budget": {"epochs": 30, "seed_values": seed_values, "max_wall_time_minutes": 360}, "inference_allowed": True, "stop_conditions": ["Завершить фиксированный бюджет."]},
            ],
            "data_policy": {
                "dataset": "STL-10 version torchvision",
                "split_strategy": "Фиксированный train/validation split до запуска.",
                "evaluation_seal": "Test labels не используются до финальной оценки.",
                "leakage_controls": ["Не выбирать конфигурацию по test metric."],
                "evaluation_access": {
                    "development_split": "Fixed train/validation partition.",
                    "selection_source": "validation",
                    "final_test_policy": "once_per_trained_unit_after_seal",
                    "test_feedback_prohibited": True,
                },
            },
            "reproducibility": {
                "rng_streams": [
                    {"id": "initialization", "controls": "Одинаковая инициализация внутри пары."},
                    {"id": "sampling", "controls": "Одинаковый порядок samples внутри пары."},
                    {"id": "augmentation", "controls": "Одинаковые augmentation draws внутри пары."},
                    {"id": "analysis", "controls": "Отдельный фиксированный analysis seed."},
                ],
                "pairing": {"unit": "seed", "invariant_fields": ["initialization", "data order"], "varied_fields": ["pooling operator"], "allocation": {"strategy": "enumerated_units", "planned_units": seed_values, "sample_size": len(seed_values), "predeclared": True}},
                "environment": {"capture": ["code digest", "dependency lock", "hardware"], "requirements": ["Python runtime is recorded"]},
            },
        },
        "evaluation_plan": {
            "primary_estimand": {"name": "paired accuracy delta", "population": "predeclared STL-10 validation examples", "contrast": "TLP minus MaxPool", "metric": "top-1 accuracy", "aggregation": "mean over paired seeds"},
            "outcomes": [{"name": "validation accuracy delta", "role": "primary", "measurement": "paired TLP minus MaxPool top-1 accuracy", "unit": "proportion"}],
            "uncertainty": {"method": "paired bootstrap", "resampling_unit": "seed pair", "interval": "two-sided percentile", "confidence_level": 0.95},
            "stopping_rule": {"kind": "fixed_budget", "criterion": "Stop after every declared seed completes or fails terminally.", "adaptation_predeclared": True},
            "multiplicity": {"family": "single primary outcome", "strategy": "No adjustment for one primary outcome."},
            "negative_result_policy": "Retain and report negative or inconclusive results without redefining the question.",
        },
        "decision_spec": {"effect_direction": "difference", "practical_threshold": 1.0, "unit": "percentage point"},
        "decisions_by_area": {
            area: {
                "value_summary": f"Declared protocol choice for {area}.",
                "status": "unresolved" if unresolved and area == "budget" else "proposed",
                "rationale": f"A reviewable bounded choice is required for {area}.",
                "source_refs": [],
                "blocking_question": "Какой confirmatory budget должен быть принят?" if unresolved and area == "budget" else "",
            }
            for area in decision_areas
        },
    }


def _implementation() -> dict:
    requirement_categories = ("execution", "data", "reproducibility", "observability", "evidence", "recovery", "analysis", "security")
    check_categories = ("workflow", "data_integrity", "reproducibility", "evidence", "analysis", "failure_recovery", "security")
    requirements = {category: [] for category in requirement_categories}
    checks = {category: [] for category in check_categories}
    for category in requirement_categories[:5]:
        requirements[category] = [{"requirement": f"Implement a concrete {category} obligation.", "verification": f"Verify {category} with a deterministic report."}]
    for category in check_categories[:4]:
        checks[category] = [{"check": f"The {category} condition is observably satisfied.", "evidence": f"Stored {category} report"}]
    return {"assistant_message": "Инженерный контракт скомпилирован.", "requirements_by_category": requirements, "checks_by_category": checks}


def _assert_strict_objects(value: object) -> None:
    if isinstance(value, Mapping):
        if value.get("type") == "object" and isinstance(value.get("properties"), Mapping):
            assert value.get("additionalProperties") is False
            assert set(value["properties"]) == set(value.get("required") or [])
        for item in value.values():
            _assert_strict_objects(item)
    elif isinstance(value, list):
        for item in value:
            _assert_strict_objects(item)


def test_stage_schemas_are_provider_strict_and_candidate_is_deterministically_assembled() -> None:
    for factory in STAGES.values():
        _assert_strict_objects(factory())
    candidate = assemble_candidate(_problem(), _protocol(), _implementation(), source_ref_map={REF: EXACT_REF})
    assert candidate["readiness"] == {"decision": "ready_for_automation", "blocking_questions": []}
    assert candidate["hypotheses"][0].get("source_refs") is None
    assert [item["stance"] for item in candidate["source_grounding"]] == ["hypothesis", "observed"]
    assert all(item["source_refs"] == [EXACT_REF] for item in candidate["source_grounding"])
    assert [item["id"] for item in candidate["implementation_requirements"]] == [f"REQ-{index}" for index in range(1, 6)]
    assert [item["category"] for item in candidate["acceptance_checks"]] == ["workflow", "data_integrity", "reproducibility", "evidence"]
    assert candidate["evaluation_plan"]["decision_rules"] == [
        "Поддержано: 95% ДИ для Δ целиком выше +1 percentage point или целиком ниже -1 percentage point; опровергнуто как практически эквивалентное: ДИ целиком внутри [-1; +1] percentage point; иначе результат неконклюзивен."
    ]
    assert candidate["experimental_plan"]["data_policy"]["evaluation_access"]["selection_rule"].startswith(
        "Выбрать checkpoint с максимальной"
    )


def test_candidate_assembly_canonicalizes_repeated_source_refs() -> None:
    problem = _problem()
    problem["hypotheses"][0]["source_refs"] = [REF, REF]

    candidate = assemble_candidate(
        problem,
        _protocol(),
        _implementation(),
        source_ref_map={REF: EXACT_REF},
    )

    assert candidate["source_grounding"][0]["source_refs"] == [EXACT_REF]


def test_research_compiler_emits_four_facets_and_source_to_acceptance_traceability() -> None:
    bundle = {
        "digest": "sha256:" + "9" * 64,
        "audience": "research.formulation",
        "excluded": [{"artifact_id": "hidden-oracle", "reason": "evaluation_only"}],
        "sources": [
            {
                "source_id": "notebook",
                "name": "experiment.ipynb",
                "digest": "sha256:" + "8" * 64,
                "media_type": "application/x-ipynb+json",
                "role": "research_source",
                "artifact_ref": EXACT_REF.split("#", 1)[0],
                "analysis": {"kind": "notebook"},
            }
        ],
    }
    context = {
        "coverage": {
            "sources_total": 1,
            "sources_represented": 1,
            "selected_characters": 100,
            "truncated_sources": [],
            "unreadable_sources": [],
            "items": [
                {
                    "artifact_ref": EXACT_REF.split("#", 1)[0],
                    "strategy": "notebook_semantic_digest_v1",
                    "selected_characters": 100,
                    "truncated": False,
                    "provenance_refs": [EXACT_REF],
                }
            ],
        }
    }

    package = build_compilation(
        direction_id="tlp_direction",
        run_id="formulation-1",
        source_bundle=bundle,
        source_context=context,
        problem_frame=_problem(),
        protocol_design=_protocol(),
        implementation_contract=_implementation(),
        source_ref_map={REF: EXACT_REF},
    )

    assert list(package["facets"]) == [
        "source_analysis",
        "research_problem",
        "experimental_protocol",
        "engineering_contract",
    ]
    assert package["context_receipt"]["excluded_count"] == 1
    assert package["traceability_coverage"]["valid"] is True
    assert package["traceability_coverage"]["coverage"] == 1.0
    assert package["readiness"] == {"decision": "ready_for_acceptance", "blockers": []}
    assert package["digest"].startswith("sha256:")
    projection = project_execution_compilation(package)
    assert projection["compilation_digest"] == package["digest"]
    assert projection["traceability"]["protocol_digest"] == package["facets"]["experimental_protocol"]["digest"]
    assert "inventory" not in projection["source_analysis"]
    assert all(node["kind"] != "acceptance_check" for node in projection["traceability"]["nodes"])


def test_research_compiler_deduplicates_repeated_grounding_refs() -> None:
    problem = _problem()
    problem["hypotheses"][0]["source_refs"] = [REF, REF]
    bundle = {
        "digest": "sha256:" + "9" * 64,
        "audience": "research.formulation",
        "excluded": [],
        "sources": [
            {
                "source_id": "notebook",
                "name": "experiment.ipynb",
                "digest": "sha256:" + "8" * 64,
                "media_type": "application/x-ipynb+json",
                "role": "research_source",
                "artifact_ref": EXACT_REF.split("#", 1)[0],
                "analysis": {"kind": "notebook"},
            }
        ],
    }
    context = {
        "coverage": {
            "sources_total": 1,
            "sources_represented": 1,
            "selected_characters": 100,
            "truncated_sources": [],
            "unreadable_sources": [],
            "items": [
                {
                    "artifact_ref": EXACT_REF.split("#", 1)[0],
                    "strategy": "notebook_semantic_digest_v1",
                    "selected_characters": 100,
                    "truncated": False,
                    "provenance_refs": [EXACT_REF],
                }
            ],
        }
    }

    package = build_compilation(
        direction_id="tlp_direction",
        run_id="formulation-duplicate-grounding",
        source_bundle=bundle,
        source_context=context,
        problem_frame=problem,
        protocol_design=_protocol(),
        implementation_contract=_implementation(),
        source_ref_map={REF: EXACT_REF},
    )

    grounding = [
        edge
        for edge in package["traceability_graph"]["edges"]
        if edge["relation"] == "motivates"
    ]
    assert len(grounding) == 1


def test_dynamic_stage_schema_limits_every_source_reference_to_supplied_short_ids() -> None:
    schema = stage_schema("problem_frame", allowed_source_refs={"SRC-001", "SRC-002"})
    _assert_strict_objects(schema)
    hypothesis_refs = schema["properties"]["hypotheses"]["items"]["properties"]["source_refs"]["items"]
    observation_refs = schema["properties"]["source_assessment"]["properties"]["observed_facts"]["items"]["properties"]["source_refs"]["items"]
    assert hypothesis_refs == observation_refs == {"type": "string", "enum": ["SRC-001", "SRC-002"]}


def test_provider_schema_removes_unsupported_keywords_without_weakening_local_validation() -> None:
    local = stage_schema("protocol_design", allowed_source_refs={"SRC-001"})
    projected = provider_schema(local)

    def keywords(value: object) -> set[str]:
        if isinstance(value, Mapping):
            return set(value) | {key for item in value.values() for key in keywords(item)}
        if isinstance(value, list):
            return {key for item in value for key in keywords(item)}
        return set()

    assert "uniqueItems" in keywords(local)
    assert not {"uniqueItems", "minLength", "maxLength"} & keywords(projected)
    predeclared = projected["properties"]["experimental_plan"]["properties"]["reproducibility"]["properties"]["pairing"]["properties"]["allocation"]["properties"]["predeclared"]
    assert predeclared == {"const": True, "type": "boolean"}
    _assert_strict_objects(projected)


def test_unresolved_protocol_decision_is_a_deterministic_readiness_blocker() -> None:
    candidate = assemble_candidate(_problem(), _protocol(unresolved=True), _implementation(), source_ref_map={REF: EXACT_REF})
    assert candidate["readiness"]["decision"] == "needs_discussion"
    assert "Какой confirmatory budget" in candidate["readiness"]["blocking_questions"][0]


def test_problem_discovery_questions_do_not_survive_resolved_protocol_choices() -> None:
    problem = _problem()
    problem["open_questions"] = ["Какой confirmatory budget нужен?"]
    problem["source_assessment"]["unresolved_decisions"] = ["Нужно выбрать confirmatory budget."]
    candidate = assemble_candidate(problem, _protocol(), _implementation(), source_ref_map={REF: EXACT_REF})

    assert candidate["open_questions"] == []
    assert candidate["readiness"]["decision"] == "ready_for_automation"


def test_resolved_protocol_decision_cannot_retain_a_blocking_question() -> None:
    protocol = _protocol()
    protocol["decisions_by_area"]["budget"]["blocking_question"] = "Нужно ещё раз выбрать budget."

    assert stage_quality_issues("protocol_design", protocol) == [
        "resolved decision budget must leave blocking_question empty"
    ]


def test_protocol_system_specification_cannot_hide_an_unresolved_implementation_choice() -> None:
    protocol = _protocol()
    protocol["experimental_plan"]["system_specification"]["unresolved_choices"] = [
        "optimizer is not selected"
    ]

    assert stage_quality_issues("protocol_design", protocol) == [
        "system_specification unresolved_choices must be empty before automation"
    ]


def test_implementation_contract_rejects_per_epoch_final_test_observation() -> None:
    implementation = _implementation()
    implementation["requirements_by_category"]["evidence"][0]["requirement"] = (
        "Store train, validation, and test metrics after every epoch."
    )

    assert stage_quality_issues("implementation_contract", implementation) == [
        "implementation contract must not observe final-test metrics per epoch"
    ]


def test_hypothesis_and_decision_direction_must_match() -> None:
    protocol = _protocol()
    protocol["decision_spec"]["effect_direction"] = "increase"

    try:
        assemble_candidate(_problem(), protocol, _implementation(), source_ref_map={REF: EXACT_REF})
    except ValueError as exc:
        assert "effect_direction must match" in str(exc)
    else:
        raise AssertionError("expected a cross-stage direction mismatch")


def test_problem_quality_gate_rejects_a_ref_not_supplied_to_the_stage() -> None:
    value = validate_stage("problem_frame", _problem())
    assert stage_quality_issues("problem_frame", value, allowed_source_refs={"SRC-999"}) == [
        "problem frame must cite only source ids supplied to this stage"
    ]


def test_two_sided_hypothesis_cannot_call_the_other_direction_falsification() -> None:
    problem = _problem()
    problem["hypotheses"][0]["falsification"] = (
        "Гипотеза опровергается при эффекте в противоположную сторону."
    )

    assert stage_quality_issues("problem_frame", problem, allowed_source_refs={REF}) == [
        "a two-sided difference hypothesis cannot treat the opposite direction as falsification"
    ]
