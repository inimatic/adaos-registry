import json
from pathlib import Path


def objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)


def test_flat_process_actions_use_list_event_shape():
    document = json.loads((Path(__file__).resolve().parents[1] / "webui.json").read_text(encoding="utf-8"))
    widgets = [item for item in objects(document) if item.get("id") in {"process-tree", "design-process"}]
    assert len(widgets) == 2
    for widget in widgets:
        assert widget["type"] == "ui.list"
        assert "$event.item." not in json.dumps(widget.get("actions"))
        assert widget["inputs"]["currentKey"] == "current"
        assert widget["inputs"]["disabledKey"] == "disabled"


def test_form_state_bindings_are_not_literal_defaults():
    document = json.loads((Path(__file__).resolve().parents[1] / "webui.json").read_text(encoding="utf-8"))
    for widget in objects(document):
        if widget.get("type") == "ui.form":
            for field in (widget.get("inputs") or {}).get("fields") or []:
                assert not str(field.get("default", "")).startswith("$state."), (widget["id"], field["id"])


def test_document_save_updates_the_opened_digest_only_after_success():
    document = json.loads((Path(__file__).resolve().parents[1] / "webui.json").read_text(encoding="utf-8"))
    actions = [item for item in objects(document) if item.get("target") == "builder_sdk_control_skill.save_readme"]
    assert len(actions) == 2
    for action in actions:
        assert action["params"]["expected_digest"] == "$state." + action["resultStateKey"]
        assert action["resultPath"] == "digest"


def test_automation_correction_remains_accessible_after_checkpoint():
    document = json.loads((Path(__file__).resolve().parents[1] / "webui.json").read_text(encoding="utf-8"))
    widgets = {item["id"]: item for item in objects(document) if item.get("id")}
    correction = "$state.commands.retry_automation || ($state.workbench.can_edit_automation && $state.commands.invalidate_candidate && !$state.workbench.clarification.pending)"
    assert widgets["automation-followup"]["visibleIf"] == correction
    assert widgets["automation-start"]["visibleIf"] == "$state.commands.start_automation"
    assert widgets["implement"]["visibleIf"] == "$state.commands.start_automation || " + correction


def test_existing_trial_projection_can_be_reconciled_from_native_form():
    document = json.loads((Path(__file__).resolve().parents[1] / "webui.json").read_text(encoding="utf-8"))
    items = list(objects(document))
    opener = next(item for item in items if item.get("id") == "publication" and "visibleIf" in item)
    assert opener["visibleIf"] == "$state.canPrepareCandidate || $state.canAcceptCandidate || $state.canPublish"
    actions = [item for item in items if item.get("on") == "click:dry-run"]
    assert len(actions) == 2
    for action in actions:
        assert action["enabledIf"] == "$state.canPrepareCandidate === true || $state.workbench.delivery.status === 'trial'"
        assert action["type"] == "openModal"
        assert action["params"]["modalId"] == "confirm-trial-access"
    modal = document["ui"]["application"]["modals"]["confirm-trial-access"]
    publish = next(
        action
        for widget in modal["schema"]["widgets"]
        for action in widget.get("actions", [])
        if action.get("target") == "builder_sdk_control_skill.publish_project"
    )
    assert publish["params"]["dry_run"] is True
    assert publish["params"]["confirmed"] is True
    assert publish["params"]["approve_permissions"] is True
    assert publish["params"]["verification_evidence"] == "$state.trialVerificationEvidence"
