from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _page() -> dict:
    manifest = json.loads((ROOT / "webui.json").read_text(encoding="utf-8"))
    return manifest["ui"]["application"]["desktop"]["pageSchema"]


def test_portfolio_and_direction_are_independent_full_surface_layouts() -> None:
    page = _page()
    variants = {item["id"]: item for item in page["layout"]["variants"]}

    assert page["initialState"]["researchViewMode"] == "portfolio"
    assert variants["portfolio"]["default"] is True
    assert [item["id"] for item in variants["portfolio"]["areas"]] == ["portfolio"]
    assert variants["direction"]["pattern"] == "focus-detail"
    assert [item["id"] for item in variants["direction"]["areas"]] == ["workspace", "context"]
    assert "$state.selectedDirectionId != null" in variants["direction"]["when"]


def test_navigation_changes_view_and_selection_atomically() -> None:
    widgets = {item["id"]: item for item in _page()["widgets"]}
    select = widgets["directions"]["actions"][0]["params"]
    back = widgets["direction-navigation"]["actions"][0]["params"]

    assert select["researchViewMode"] == "direction"
    assert select["selectedDirectionId"] == "$event.direction_id"
    assert back["researchViewMode"] == "portfolio"
    assert back["selectedDirectionId"] is None


def test_acceptance_action_is_bound_to_core_owned_admission_review() -> None:
    widgets = {item["id"]: item for item in _page()["widgets"]}
    status_bindings = widgets["direction-status"]["inputs"]["stateBindings"]
    accept = next(item for item in widgets["development-actions"]["inputs"]["buttons"] if item["id"] == "accept")

    assert status_bindings["researchAdmissionDecision"] == "formulation.admission_decision"
    assert status_bindings["researchCanAccept"] == "formulation.can_accept"
    assert accept["enabledIf"] == "$state.researchCanAccept === true && $state.researchAdmissionDecision === 'admitted'"
    assert widgets["research-consensus"]["title"] == "Current draft and AdaOS review"
