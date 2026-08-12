from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from jsonschema import Draft202012Validator

from research.contracts import materialize_automation_brief, materialize_prototype, prototype_admission_issues, prototype_candidate_schema, validate
from research.orchestrator import (
    _address_builder_url,
    _json_object,
    _llm_failure,
    _normalize_candidate_shape,
    _repair_prompt,
)


def _candidate() -> dict:
    source_ref = "artifact://skill/tlp_direction_skill/part0/s1#cell=1"
    return {
        "title": "TLP paired workflow study",
        "background": "The historical notebook motivates a clean paired experiment but is not confirmatory evidence.",
        "research_question": "Under a locked CPU protocol, how does TLP validation accuracy differ from MaxPool?",
        "hypotheses": [{"id": "H1", "statement": "TLP changes paired validation accuracy.", "falsification": "The interval and decision rule do not support the declared effect.", "status": "exploratory"}],
        "source_grounding": [
            {"claim_id": "OBS-1", "claim": "The source notebook contains a TLP implementation prototype.", "stance": "observed", "source_refs": [source_ref]},
            {"claim_id": "H1", "claim": "The historical artifact motivates but does not confirm a paired TLP contrast.", "stance": "hypothesis", "source_refs": [source_ref]},
        ],
        "evidence_policy": {"historical_results": "exploratory_source_only", "workflow_smoke": "workflow_evidence_only", "negative_results": "retain_and_report"},
        "experimental_plan": {
            "comparators": ["maxpool", "tlp"],
            "stages": [
                {"id": "smoke", "purpose": "validate the complete local workflow", "evidence_class": "workflow_smoke", "execution_profile": {"device": "cpu", "epochs": 3}, "budget": {"epochs": 3, "seeds": 1}, "inference_allowed": False, "stop_conditions": ["both paired arms finish or one fails"]},
                {"id": "series", "purpose": "estimate the locked paired scientific contrasts", "evidence_class": "confirmatory", "execution_profile": {"device": "declared member"}, "budget": {"seeds": 10}, "inference_allowed": True, "stop_conditions": ["enumerated trials complete or budget exhausts"]},
            ],
            "data_policy": {
                "dataset": "STL-10 pinned release",
                "split_strategy": "fixed train/validation split declared before any run",
                "evaluation_seal": "test labels and metrics remain sealed until the series is locked",
                "leakage_controls": ["split digest is immutable", "no test-guided tuning"],
            },
            "reproducibility": {
                "rng_streams": [
                    {"id": "initialization", "controls": "shared arm initialization per seed"},
                    {"id": "sampling", "controls": "shared data order per paired seed"},
                    {"id": "augmentation", "controls": "shared transform draws per sample"},
                    {"id": "analysis", "controls": "declared paired resampling stream"},
                ],
                "pairing": {
                    "unit": "seed",
                    "invariant_fields": ["initial weights", "data order", "augmentation draws"],
                    "varied_fields": ["pooling operator"],
                    "allocation": {
                        "strategy": "enumerated_units",
                        "planned_units": [17, 23, 29, 31, 37, 41, 43, 47, 53, 59],
                        "sample_size": 10,
                        "predeclared": True,
                    },
                },
                "environment": {
                    "capture": ["code digest", "dependency lock", "hardware fingerprint"],
                    "requirements": ["CPU smoke must run on current or member node"],
                },
            },
        },
        "evaluation_plan": {
            "primary_estimand": {
                "name": "validation_top1_accuracy_tlp_minus_maxpool",
                "population": "locked STL-10 validation examples and enumerated training seeds",
                "contrast": "TLP minus MaxPool within paired seed",
                "metric": "top-1 accuracy",
                "aggregation": "mean of paired seed-level deltas",
            },
            "outcomes": [{"name": "paired_accuracy_delta", "role": "primary", "measurement": "TLP minus MaxPool validation top-1 accuracy", "unit": "paired seed"}],
            "uncertainty": {"method": "paired bootstrap over seed-level deltas", "resampling_unit": "paired seed", "interval": "two-sided percentile interval", "confidence_level": 0.95},
            "stopping_rule": {"kind": "fixed_budget", "criterion": "finish every declared seed or exhaust the predeclared infrastructure budget", "adaptation_predeclared": True},
            "decision_rules": ["smoke makes no inferential decision", "report estimate and interval for the locked series"],
            "multiplicity": {"family": "single primary contrast", "strategy": "secondary outcomes remain descriptive"},
            "practical_significance": "report the paired accuracy effect against a predeclared one percentage-point reference",
            "negative_result_policy": "negative and inconclusive outcomes are valid completions",
        },
        "constraints": ["Ray deferred", "current/member node execution"],
        "assumptions": ["dataset acquisition will be declared before execution"],
        "open_questions": [],
        "implementation_requirements": [
            {"id": "REQ-1", "category": "execution", "requirement": "Implement a deterministic paired CPU runner.", "verification": "Run the paired smoke conformance test."},
            {"id": "REQ-2", "category": "data", "requirement": "Use typed immutable experiment configs.", "verification": "Validate and digest the serialized RunSpec."},
            {"id": "REQ-3", "category": "reproducibility", "requirement": "Record every named RNG stream identity.", "verification": "Inspect the emitted reproducibility manifest."},
            {"id": "REQ-4", "category": "evidence", "requirement": "Emit ContentRef-bound artifacts and evidence.", "verification": "Resolve every result reference by digest."},
            {"id": "REQ-5", "category": "observability", "requirement": "Keep primary data in owner-scoped skill storage.", "verification": "Resolve storage capability ownership in the run report."},
        ],
        "acceptance_checks": [
            {"id": "AC-1", "category": "workflow", "check": "Three-epoch smoke produces both paired arms with shared initialization.", "evidence": "paired smoke report"},
            {"id": "AC-2", "category": "evidence", "check": "Notebook output is classified as non-confirmatory source material.", "evidence": "evidence manifest"},
            {"id": "AC-3", "category": "data_integrity", "check": "The test split remains sealed throughout implementation and smoke.", "evidence": "data seal log"},
            {"id": "AC-4", "category": "reproducibility", "check": "Native AdaOS validation and skill tests pass.", "evidence": "CLI test report"},
        ],
        "readiness": {"decision": "ready_for_automation", "blocking_questions": []},
    }


def _coverage() -> dict:
    source_ref = "artifact://skill/tlp_direction_skill/part0/s1"
    return {
        "sources_total": 1,
        "sources_represented": 1,
        "selected_characters": 1000,
        "truncated_sources": [],
        "unreadable_sources": [],
        "items": [
            {
                "artifact_ref": source_ref,
                "digest": "sha256:" + "2" * 64,
                "strategy": "notebook_source_cells_without_outputs",
                "selected_characters": 1000,
                "truncated": False,
                "provenance_refs": [source_ref + "#cell=1"],
            }
        ],
    }


def test_prototype_and_automation_brief_bind_exact_inputs() -> None:
    source_digest = "sha256:" + "1" * 64
    prototype = materialize_prototype(
        _candidate(),
        direction_id="tlp_direction_skill",
        source_bundle_digest=source_digest,
        context_coverage=_coverage(),
        revision=1,
        parent_digest=None,
        actor="user:test",
    )
    assert validate("research.prototype.v1.schema.json", prototype)["digest"] == prototype["digest"]
    assert prototype["admission_review"]["decision"] == "admitted"
    assert prototype_admission_issues(prototype) == []
    brief = materialize_automation_brief(
        direction_id="tlp_direction_skill",
        project={"id": "tlp_research", "ref": "project:tlp_research", "version": "0.1.0", "manifest_digest": "sha256:" + "6" * 64, "source_path": "/dev/projects/tlp_research"},
        artifact_groups=[{"ref": "artifact://skill/tlp_direction_skill/part0", "group_id": "part0", "digest": "sha256:" + "7" * 64, "root_path": "/dev/skills/tlp_direction_skill/artifacts/part0", "manifest_path": "/dev/skills/tlp_direction_skill/artifacts/part0/manifest.yaml"}],
        source_bundle={"digest": source_digest, "sources": [{"source_id": "s1", "name": "study.ipynb", "digest": "sha256:" + "2" * 64, "media_type": "application/x-ipynb+json", "role": "notebook", "analysis": {"warnings": ["notebook_outputs_are_untrusted_source_material"]}, "artifact_ref": "artifact://skill/tlp_direction_skill/part0/s1", "group_id": "part0"}]},
        prototype=prototype,
        checkpoint={"package_digest": "sha256:" + "3" * 64, "source_revision": "abc", "source_tree": "sha256:" + "4" * 64, "sha256": "sha256:" + "5" * 64},
        actor="user:test",
    )
    assert brief["source_bundle_digest"] == source_digest
    assert brief["prototype_digest"] == prototype["digest"]
    assert brief["handoff_state"] == "ready_for_codex"
    assert brief["project"]["manifest_digest"] == "sha256:" + "6" * 64
    assert brief["development_scope"]["targets"][0]["access"] == "read-write"
    assert brief["development_scope"]["context_members"][0]["access"] == "read-only"
    assert any("direction-specific scenario" in item["check"] for item in brief["acceptance_checks"])
    assert any("three-epoch" in item.lower() for item in brief["prohibited_actions"])


def test_llm_candidate_schema_matches_the_typed_contract_and_reports_all_violations() -> None:
    schema = prototype_candidate_schema()
    Draft202012Validator(schema).validate(_candidate())
    assert "digest" not in schema["properties"]
    assert schema["properties"]["implementation_requirements"]["minItems"] == 5
    assert schema["properties"]["acceptance_checks"]["minItems"] == 4

    invalid = _candidate()
    invalid["evaluation_plan"]["primary_estimand"] = {"name": "x"}
    invalid["acceptance_checks"] = [{"id": "one", "check": "only one acceptance check", "evidence": "report"}]
    with pytest.raises(ValueError) as raised:
        materialize_prototype(
            invalid,
            direction_id="tlp_direction_skill",
            source_bundle_digest="sha256:" + "1" * 64,
            context_coverage=_coverage(),
            revision=1,
            parent_digest=None,
            actor="user:test",
        )
    detail = str(raised.value)
    assert "acceptance_checks" in detail
    assert "evaluation_plan.primary_estimand" in detail


def test_contract_accepts_compact_scientific_units_and_one_declared_pairing_invariant() -> None:
    candidate = _candidate()
    candidate["evaluation_plan"]["outcomes"][0]["unit"] = "%"
    candidate["experimental_plan"]["reproducibility"]["pairing"]["invariant_fields"] = [
        "architecture"
    ]

    prototype = materialize_prototype(
        candidate,
        direction_id="tlp_direction_skill",
        source_bundle_digest="sha256:" + "1" * 64,
        context_coverage=_coverage(),
        revision=1,
        parent_digest=None,
        actor="user:test",
    )

    assert prototype["admission_review"]["decision"] == "admitted"


def test_deterministic_admission_review_cannot_be_self_asserted_or_cite_omitted_context() -> None:
    candidate = _candidate()
    candidate["source_grounding"][0]["source_refs"] = [
        "artifact://skill/tlp_direction_skill/part0/s1#cell=999"
    ]
    prototype = materialize_prototype(
        candidate,
        direction_id="tlp_direction_skill",
        source_bundle_digest="sha256:" + "1" * 64,
        context_coverage=_coverage(),
        revision=1,
        parent_digest=None,
        actor="user:test",
    )

    assert "source_grounding may cite only context fragments" in "; ".join(
        prototype_admission_issues(prototype)
    )

    prototype["admission_review"] = {
        **prototype["admission_review"],
        "decision": "admitted",
        "blockers": [],
    }
    assert prototype_admission_issues(prototype) == [
        "admission_review does not match the deterministic AdaOS review"
    ]


def test_admission_rejects_hypothesis_as_observation_and_template_language() -> None:
    candidate = _candidate()
    candidate["source_grounding"][1]["stance"] = "observed"
    candidate["experimental_plan"]["stages"][0]["stop_conditions"] = [
        "bounded operational condition"
    ]

    prototype = materialize_prototype(
        candidate,
        direction_id="tlp_direction_skill",
        source_bundle_digest="sha256:" + "1" * 64,
        context_coverage=_coverage(),
        revision=1,
        parent_digest=None,
        actor="user:test",
    )
    issues = "; ".join(prototype_admission_issues(prototype))

    assert "stance=hypothesis" in issues
    assert "unresolved placeholders" in issues


def test_placeholder_gate_distinguishes_unknown_outcomes_from_unknown_values() -> None:
    candidate = _candidate()
    candidate["implementation_requirements"][0]["requirement"] = (
        "Reconcile retries after unknown outcomes without duplicate runs."
    )
    accepted = materialize_prototype(
        candidate,
        direction_id="tlp_direction_skill",
        source_bundle_digest="sha256:" + "1" * 64,
        context_coverage=_coverage(),
        revision=1,
        parent_digest=None,
        actor="user:test",
    )
    assert accepted["admission_review"]["decision"] == "admitted"

    candidate["implementation_requirements"][0]["requirement"] = "unknown"
    rejected = materialize_prototype(
        candidate,
        direction_id="tlp_direction_skill",
        source_bundle_digest="sha256:" + "1" * 64,
        context_coverage=_coverage(),
        revision=1,
        parent_digest=None,
        actor="user:test",
    )
    assert "unresolved placeholders" in "; ".join(
        prototype_admission_issues(rejected)
    )


def test_admission_requires_predeclared_pairing_units_and_category_coverage() -> None:
    candidate = _candidate()
    candidate["experimental_plan"]["reproducibility"]["pairing"]["allocation"][
        "sample_size"
    ] = 9
    candidate["implementation_requirements"][4]["category"] = "execution"
    candidate["acceptance_checks"][3]["category"] = "workflow"

    prototype = materialize_prototype(
        candidate,
        direction_id="tlp_direction_skill",
        source_bundle_digest="sha256:" + "1" * 64,
        context_coverage=_coverage(),
        revision=1,
        parent_digest=None,
        actor="user:test",
    )
    issues = "; ".join(prototype_admission_issues(prototype))

    assert "sample_size must equal" in issues
    assert "observability" in issues
    assert "data integrity" in issues


def test_builder_url_carries_a_declared_first_paint_address() -> None:
    addressed = _address_builder_url(
        "https://inimatic.com/?intent=webspace.open&webspace_id=desktop-dev",
        direction_id="tlp_research_03",
        title="TLP direction",
    )
    query = parse_qs(urlsplit(addressed).query)

    assert query["intent"] == ["webspace.open"]
    assert query["webspace_id"] == ["desktop-dev"]
    assert query["builder_object_type"] == ["skill"]
    assert query["builder_object_id"] == ["tlp_research_03"]
    assert query["builder_object_ref"] == ["skill:tlp_research_03"]
    assert query["builder_object_title"] == ["TLP direction"]


def test_llm_failure_is_bounded_and_does_not_dump_provider_payload() -> None:
    failure = _llm_failure(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"text": "sensitive and very large provider payload"}],
        },
        operation="repair",
    )

    assert str(failure) == (
        "Root LLM repair ended with status=incomplete: max_output_tokens"
    )
    assert "sensitive" not in str(failure)


def test_repair_prompt_keeps_the_candidate_out_of_an_instruction_envelope() -> None:
    prompt = _repair_prompt(
        validation_error="readiness must be ready_for_automation",
        candidate={"title": "candidate"},
        rules=["Return JSON only."],
        user_request="Make the protocol operational.",
        allowed_provenance_refs=["artifact://skill/example/part0/a#lines=1-2"],
    )

    assert not prompt.lstrip().startswith("{")
    assert 'CANDIDATE JSON TO CORRECT AND RETURN:\n{"title": "candidate"}' in prompt
    assert '"validation_error"' not in prompt


def test_json_object_conservatively_repairs_trailing_commas_only() -> None:
    assert _json_object('```json\n{"title": "Study", "items": [1, 2,],}\n```') == {
        "title": "Study",
        "items": [1, 2],
    }

    with pytest.raises(ValueError, match="invalid JSON"):
        _json_object('{"title": unquoted}')


def test_candidate_shape_normalization_only_lifts_known_contract_fields() -> None:
    normalized = _normalize_candidate_shape(
        {
            "title": "Study",
            "rules": ["instruction, not candidate data"],
            "hypotheses": [
                {
                    "id": "H1",
                    "source_grounding": {
                        "claim_id": "H1",
                        "stance": "hypothesis",
                    },
                }
            ],
            "experimental_plan": {
                "comparators": ["a", "b"],
                "evaluation_plan": {
                    "primary_estimand": {"name": "delta"},
                    "decision_rules": [
                        {"id": "DR-1", "description": "Report the interval."}
                    ],
                    "constraints": ["CPU"],
                    "readiness": {
                        "decision": "needs_discussion",
                        "blocking_questions": ["review"],
                    },
                },
                "reproducibility": {
                    "pairing": {
                        "allocation": {
                            "strategy": "Enumerated Units",
                        }
                    }
                },
                "domain_extension": {"preserved": True},
            },
            "acceptance_checks": [
                {"category": "data integrity"}
            ],
            "implementation_requirements": [
                {"category": "storage", "requirement": "Keep data scoped."}
            ],
        }
    )

    assert "rules" not in normalized
    assert normalized["evaluation_plan"]["primary_estimand"]["name"] == "delta"
    assert normalized["evaluation_plan"]["decision_rules"] == [
        "Report the interval."
    ]
    assert normalized["constraints"] == ["CPU"]
    assert normalized["readiness"]["decision"] == "needs_discussion"
    assert normalized["source_grounding"][0]["claim_id"] == "H1"
    assert "source_grounding" not in normalized["hypotheses"][0]
    assert normalized["acceptance_checks"][0]["category"] == "data_integrity"
    assert normalized["acceptance_checks"][0]["id"] == "AC-1"
    assert normalized["implementation_requirements"][0]["id"] == "REQ-1"
    assert normalized["implementation_requirements"][0]["category"] == "data"
    assert (
        normalized["experimental_plan"]["reproducibility"]["pairing"][
            "allocation"
        ]["strategy"]
        == "enumerated_units"
    )
    assert normalized["experimental_plan"]["domain_extension"] == {"preserved": True}


def test_candidate_shape_lifts_explicit_confirmatory_units_into_allocation() -> None:
    normalized = _normalize_candidate_shape(
        {
            "experimental_plan": {
                "stages": [
                    {
                        "evidence_class": "confirmatory",
                        "budget": {"planned_seeds": [17, 23, 29]},
                    }
                ],
                "reproducibility": {
                    "pairing": {"unit": "seed"},
                },
            }
        }
    )

    assert normalized["experimental_plan"]["reproducibility"]["pairing"][
        "allocation"
    ] == {
        "strategy": "enumerated_units",
        "planned_units": [17, 23, 29],
        "sample_size": 3,
        "predeclared": True,
    }
