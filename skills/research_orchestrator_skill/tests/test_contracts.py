from __future__ import annotations

from research.contracts import materialize_automation_brief, materialize_prototype, validate


def _candidate() -> dict:
    return {
        "title": "TLP paired workflow study",
        "background": "The historical notebook motivates a clean paired experiment but is not confirmatory evidence.",
        "research_question": "Under a locked CPU protocol, how does TLP validation accuracy differ from MaxPool?",
        "hypotheses": [{"id": "H1", "statement": "TLP changes paired validation accuracy.", "falsification": "The interval and decision rule do not support the declared effect.", "status": "exploratory"}],
        "experimental_plan": {
            "comparators": ["maxpool", "tlp"],
            "stages": [
                {"id": "smoke", "purpose": "validate the complete local workflow", "evidence_class": "workflow_smoke", "execution_profile": {"device": "cpu", "epochs": 3}, "budget": {"epochs": 3, "seeds": 1}, "inference_allowed": False, "stop_conditions": ["both paired arms finish or one fails"]},
                {"id": "series", "purpose": "estimate the locked paired scientific contrasts", "evidence_class": "confirmatory", "execution_profile": {"device": "declared member"}, "budget": {"seeds": 10}, "inference_allowed": True, "stop_conditions": ["enumerated trials complete or budget exhausts"]},
            ],
            "data_policy": {"dataset": "STL-10", "splits": "train and validation locked; test sealed"},
            "reproducibility": {"rng_streams": ["initialization", "sampling", "augmentation", "analysis"], "pairing": "same initialization and stochastic streams within pair", "environment": "digest-bound AdaOS execution environment"},
        },
        "evaluation_plan": {
            "primary_estimand": "paired validation accuracy delta between TLP and MaxPool",
            "outcomes": [{"name": "paired_accuracy_delta", "role": "primary", "measurement": "TLP minus MaxPool validation top-1 accuracy"}],
            "uncertainty": "paired interval across enumerated seeds; none for smoke",
            "decision_rules": ["smoke makes no inferential decision", "report estimate and interval for the locked series"],
            "multiplicity": "secondary outcomes are labelled and adjusted as declared",
            "negative_result_policy": "negative and inconclusive outcomes are valid completions",
        },
        "constraints": ["Ray deferred", "current/member node execution"],
        "assumptions": ["dataset acquisition will be declared before execution"],
        "open_questions": [],
        "implementation_requirements": ["Implement a deterministic paired CPU runner.", "Use typed immutable configs.", "Record named RNG streams.", "Emit ContentRef artifacts.", "Keep primary data skill-scoped."],
        "acceptance_checks": ["Three-epoch smoke produces both paired arms with shared initialization.", "Notebook output is not evidence.", "Test split remains sealed.", "Native AdaOS validation passes."],
        "readiness": {"decision": "ready_for_automation", "blocking_questions": []},
    }


def test_prototype_and_automation_brief_bind_exact_inputs() -> None:
    source_digest = "sha256:" + "1" * 64
    prototype = materialize_prototype(
        _candidate(),
        direction_id="tlp_direction_skill",
        source_bundle_digest=source_digest,
        revision=1,
        parent_digest=None,
        actor="user:test",
    )
    assert validate("research.prototype.v1.schema.json", prototype)["digest"] == prototype["digest"]
    brief = materialize_automation_brief(
        direction_id="tlp_direction_skill",
        source_bundle={"digest": source_digest, "sources": [{"source_id": "s1", "name": "study.ipynb", "digest": "sha256:" + "2" * 64, "media_type": "application/x-ipynb+json", "role": "notebook", "analysis": {"warnings": ["notebook_outputs_are_untrusted_source_material"]}}]},
        prototype=prototype,
        checkpoint={"package_digest": "sha256:" + "3" * 64, "source_revision": "abc", "source_tree": "sha256:" + "4" * 64, "sha256": "sha256:" + "5" * 64},
        actor="user:test",
    )
    assert brief["source_bundle_digest"] == source_digest
    assert brief["prototype_digest"] == prototype["digest"]
    assert brief["handoff_state"] == "ready_for_codex"
    assert any("direction-specific scenario" in item for item in brief["acceptance_checks"])
    assert any("three-epoch" in item.lower() for item in brief["prohibited_actions"])
