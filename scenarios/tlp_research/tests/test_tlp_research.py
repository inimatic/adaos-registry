from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from adaos.sdk.scenarios.runtime import ActionRegistry, ScenarioRuntime, load_scenario
from adaos.services.conversational_pipeline import compile_conversational_package


ROOT = Path(__file__).resolve().parents[1]
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


def _json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_manifest_binds_only_declared_research_skill_routes() -> None:
    manifest = yaml.safe_load((ROOT / "scenario.yaml").read_text(encoding="utf-8"))

    assert manifest["depends"] == ["research_manager_skill", "mlflow_tracker_skill", "tlp_experiment_skill"]
    assert manifest["runtime"]["skills"]["required"] == [
        "research_manager_skill",
        "mlflow_tracker_skill",
        "tlp_experiment_skill",
    ]
    assert [step["call"] for step in manifest["steps"]] == [
        "research_manager_skill.create_study",
        "research_manager_skill.advance_workflow",
        "research_manager_skill.create_experiment",
    ]
    assert manifest["steps"][0]["args"]["mode"] == "confirmatory"
    assert manifest["steps"][1]["args"]["command"] == "submit_protocol_review"
    conditions = manifest["steps"][2]["args"]["conditions"]
    assert conditions["runner"] == {
        "contract": "adaos.research.runner.v1",
        "provider": "tlp_experiment_skill",
        "data_owner": "tlp_experiment_skill",
    }


def test_desktop_surface_is_an_operator_complete_single_experiment_workbench() -> None:
    manifest = yaml.safe_load((ROOT / "scenario.yaml").read_text(encoding="utf-8"))
    webui = _json("webui.json")
    page = webui["ui"]["application"]["desktop"]["pageSchema"]
    status = next(widget for widget in page["widgets"] if widget["id"] == "experiment-status")
    editor = next(widget for widget in page["widgets"] if widget["id"] == "experiment-conditions")
    actions = next(widget for widget in page["widgets"] if widget["id"] == "experiment-actions")
    views = next(widget for widget in page["widgets"] if widget["id"] == "experiment-views")
    help_modal = webui["ui"]["application"]["modals"]["tlp_research_help"]

    assert manifest["type"] == "desktop"
    assert manifest["ui"] == {"manifest": "webui.json"}
    assert status["dataSource"]["name"] == "research_manager_skill.get_experiment"
    assert status["dataSource"]["params"]["experiment_id"] == "$state.experimentId"
    assert editor["actions"][0]["target"] == "research_manager_skill.revise_experiment_json"
    assert views["area"] == "main"
    assert page["widgets"].index(views) < page["widgets"].index(editor)
    assert next(item for item in views["actions"] if item["on"] == "click:help") == {
        "on": "click:help",
        "type": "openModal",
        "params": {"modalId": "tlp_research_help"},
    }
    readme = (ROOT / "README.md").read_text(encoding="utf-8").rstrip()
    modal_readme = next(
        widget
        for widget in help_modal["schema"]["widgets"]
        if widget["id"] == "tlp-help-readme"
    )
    assert modal_readme["inputs"]["content"] == readme
    current_step = next(
        widget
        for widget in help_modal["schema"]["widgets"]
        if widget["id"] == "tlp-help-current-step"
    )
    assert current_step["dataSource"]["name"] == "research_manager_skill.describe_experiment"
    assert {item["target"] for item in actions["actions"] if "target" in item} >= {
        "research_manager_skill.lock_experiment",
        "research_manager_skill.start_experiment",
        "research_manager_skill.cancel_experiment",
        "research_manager_skill.reconcile_experiment",
        "research_manager_skill.finalize_experiment",
    }
    mlflow = next(item for item in actions["actions"] if item["on"] == "click:mlflow")
    assert mlflow["type"] == "openUrl"
    assert mlflow["params"] == {
        "url": "/api/services/mlflow_tracker_skill/ui-bootstrap",
        "target": "_blank",
        "withAuth": True,
    }
    assert page["initialState"]["experimentId"] == manifest["steps"][2]["args"]["experiment_id"]


def test_guidance_is_available_on_web_text_and_voice() -> None:
    manifest = yaml.safe_load((ROOT / "scenario.yaml").read_text(encoding="utf-8"))

    assert manifest["conversational"] == {"manifest": "conversational/manifest.yaml"}
    assert manifest["guidance"]["schema"] == "adaos.scenario.guidance.v1"
    assert manifest["guidance"]["presentation"]["channels"] == ["web", "text", "voice"]
    assert manifest["guidance"]["workflow"]["state_source"]["name"] == (
        "research_manager_skill.describe_experiment"
    )
    assert manifest["guidance"]["conversational"] == {
        "help_intent": "tlp_research.help",
        "next_steps_intent": "tlp_research.next_steps",
    }


def test_conversational_guidance_package_compiles_without_an_llm() -> None:
    result = compile_conversational_package(
        ROOT,
        manifest_name="scenario.yaml",
        operation_catalog={"research_manager_skill": ["describe_experiment"]},
    )

    assert result.valid is True, result.validation.report
    assert result.validation.report["diagnostics"] == []
    assert result.runtime_bundle is not None
    assert {item["intent_id"] for item in result.runtime_bundle["matchers"].values()} == {
        "tlp_research.help",
        "tlp_research.next_steps",
    }


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
    experiment = _json("fixtures/experiment-e001.v1.json")
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
    assert experiment["execution"]["preflight"]["epochs"] == 3
    assert experiment["operators"]["arms"][1]["constraint"] == "theta-minus-channel-mean"
    assert len(experiment["execution"]["confirmatory"]["seeds"]) == 10
    assert experiment["tracker"]["provider"] == "mlflow"
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

    def _experiment(args: dict) -> dict:
        calls.append(("create_experiment", dict(args)))
        return {"lifecycle": {"state": "draft", "generation": 0}}

    registry.register("research_manager_skill.create_study", _create)
    registry.register("research_manager_skill.advance_workflow", _advance)
    registry.register("research_manager_skill.create_experiment", _experiment)

    result = ScenarioRuntime(registry=registry).run(load_scenario(ROOT))

    assert [name for name, _args in calls] == ["create_study", "advance_workflow", "create_experiment"]
    assert result["steps"]["submit_protocol_review"]["result"] == {
        "accepted": True,
        "state": "protocol_review",
        "generation": 1,
    }
    assert result["steps"]["create_control_experiment"]["result"]["lifecycle"]["state"] == "draft"
