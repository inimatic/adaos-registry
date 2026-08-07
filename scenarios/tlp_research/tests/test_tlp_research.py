from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from adaos.sdk.scenarios.runtime import ActionRegistry, ScenarioRuntime, load_scenario


ROOT = Path(__file__).resolve().parents[1]
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


def _json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_manifest_binds_only_declared_research_skill_routes() -> None:
    manifest = yaml.safe_load((ROOT / "scenario.yaml").read_text(encoding="utf-8"))

    assert manifest["depends"] == ["research_manager_skill"]
    assert manifest["runtime"]["skills"]["required"] == ["research_manager_skill"]
    assert [step["call"] for step in manifest["steps"]] == [
        "research_manager_skill.create_study",
        "research_manager_skill.advance_workflow",
    ]
    assert manifest["steps"][0]["args"]["mode"] == "confirmatory"
    assert manifest["steps"][1]["args"]["command"] == "submit_protocol_review"


def test_package_preserves_research_gates_and_seals_test_access() -> None:
    workflow = _json("workflow.json")
    transitions = {item["transition_id"]: item for item in workflow["transitions"]}

    assert [state["id"] for state in workflow["states"]] == [
        "draft",
        "protocol_review",
        "locked",
        "smoke",
        "executing",
        "qc",
        "unblinded",
        "analysis",
        "claim_review",
        "complete",
    ]
    assert transitions["lock_protocol"]["approval"]["required"] is True
    assert transitions["unblind_test"]["approval"]["required"] is True
    assert transitions["unblind_test"]["recovery"]["reconciliation"] == "always"
    assert transitions["decide_claim"]["evidence"]["minimum"] == 2


def test_fixtures_are_paired_deterministic_and_provenance_only() -> None:
    protocol = _json("fixtures/protocol.v1.json")
    analysis = _json("fixtures/analysis-plan.v1.json")
    evidence = _json("fixtures/evidence-policy.v1.json")
    provenance = _json("provenance/exploratory-notebook.v1.json")

    assert protocol["design"]["paired_seeds"] == 10
    assert protocol["design"]["shared_initialization"] is True
    assert protocol["stopping"]["test_access"] == "after_qc_only"
    assert analysis["inference"]["seed_is_unit"] is True
    assert evidence["raw_notebook_outputs_admitted"] is False
    assert provenance["classification"] == "exploratory_only"
    assert provenance["sanitization"]["source_code_embedded"] is False
    assert provenance["sanitization"]["cell_outputs_embedded"] is False
    assert provenance["selected_cells"]
    assert all(SHA256.fullmatch(cell["source_digest"]) for cell in provenance["selected_cells"])


def test_scenario_dry_run_reaches_protocol_review_with_typed_calls() -> None:
    calls: list[tuple[str, dict]] = []
    registry = ActionRegistry()

    def _create(args: dict) -> dict:
        calls.append(("create_study", dict(args)))
        return {"workflow": {"state": "draft", "generation": 0}}

    def _advance(args: dict) -> dict:
        calls.append(("advance_workflow", dict(args)))
        return {"accepted": True, "state": "protocol_review", "generation": 1}

    registry.register("research_manager_skill.create_study", _create)
    registry.register("research_manager_skill.advance_workflow", _advance)

    result = ScenarioRuntime(registry=registry).run(load_scenario(ROOT))

    assert [name for name, _args in calls] == ["create_study", "advance_workflow"]
    assert result["steps"]["submit_protocol_review"]["result"] == {
        "accepted": True,
        "state": "protocol_review",
        "generation": 1,
    }
