from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml

from adaos.services.resources import ResourceWorkbenchService
from adaos.skills.runtime_runner import _should_expand_keywords


HANDLER_PATH = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
SKILL_PATH = Path(__file__).resolve().parents[1] / "skill.yaml"
WEBUI_PATH = Path(__file__).resolve().parents[1] / "webui.json"


def _load_handler():
    module_name = "demo_metrics_skill_handler_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, HANDLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.stream_publish = lambda *args, **kwargs: {"ok": True}
    return module


def _bind_state_dir(handler, state_dir: Path) -> None:
    handler.ResourceWorkbenchService = lambda *args, **kwargs: ResourceWorkbenchService(state_dir=state_dir)


def test_resource_workbench_snapshot_exposes_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    handler = _load_handler()
    _bind_state_dir(handler, tmp_path / "state")

    result = handler.get_resource_workbench_snapshot({"role": "owner"})

    assert result["ok"] is True
    resource_types = {item["resource_type"] for item in result["items"]}
    assert "adaos.dev.ticket" in resource_types
    assert "demo.metric_note" in resource_types
    assert result["snapshot"]["metrics"]["count"] >= 1


def test_resource_workbench_snapshot_includes_non_persistent_builder_note(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    handler = _load_handler()
    state_dir = tmp_path / "state"
    _bind_state_dir(handler, state_dir)

    result = handler.get_resource_workbench_snapshot({"role": "owner"})

    notes = result["snapshot"]["notes"]["items"]
    validation_note = next(item for item in notes if item["title"] == "Builder E2E validation")
    assert validation_note["non_persistent"] is True
    persisted = ResourceWorkbenchService(state_dir=state_dir).query(
        {
            "schema": "adaos.resource.query.v1",
            "resource_type": "demo.metric_note",
            "search": "Builder E2E validation",
            "actor": {"id": "demo_metrics:owner", "role": "owner"},
        }
    )
    assert persisted["count"] == 0


def test_demo_metric_query_includes_live_open_dev_tickets_metric(monkeypatch):
    handler = _load_handler()

    class StubService:
        calls = []

        def query(self, request):
            self.calls.append(request)
            if request["resource_type"] == "demo.metric":
                return {"ok": True, "items": [{"id": "cpu", "value": 42}], "count": 1}
            return {"ok": True, "items": [{"id": "ticket-1"}], "count": 7}

    handler.ResourceWorkbenchService = StubService
    result = handler.query_resource_workbench({"resource_type": "demo.metric"})

    metric = next(item for item in result["items"] if item.get("title") == "Open change requests")
    assert metric["value"] == 7
    assert metric["group"] == "subnet"
    assert metric["unit"] == "tickets"
    assert metric["non_persistent"] is True
    ticket_call = next(call for call in StubService.calls if call["resource_type"] == "adaos.dev.ticket")
    assert ticket_call["filters"] == {"status_group": "open"}


def test_demo_metric_query_degrades_and_preserves_synthetic_rows(monkeypatch):
    handler = _load_handler()

    class FailingTicketService:
        def query(self, request):
            if request["resource_type"] == "adaos.dev.ticket":
                raise RuntimeError("ticket provider unavailable")
            return {"ok": True, "items": [{"id": "cpu", "value": 42}], "count": 1}

    handler.ResourceWorkbenchService = FailingTicketService
    result = handler.query_resource_workbench({"resource_type": "demo.metric"})

    assert result["ok"] is True
    assert any(item["id"] == "cpu" for item in result["items"])
    metric = next(item for item in result["items"] if item["id"] == "open-dev-tickets")
    assert metric["value"] == 0
    assert metric["source_state"] == "degraded"
    assert result["source_state"] == "degraded"


def test_resource_role_matrix_uses_role_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    handler = _load_handler()
    _bind_state_dir(handler, tmp_path / "state")

    result = handler.list_resource_role_matrix({"role": "owner"})

    assert result["ok"] is True
    assert result["resource_type"] == "resource.role_policy"
    assert any(item["resource_type"] == "demo.metric_note" and item["role"] == "guest" for item in result["items"])


def test_workbench_read_tools_declare_read_only_side_effects():
    manifest = yaml.safe_load(SKILL_PATH.read_text(encoding="utf-8")) or {}
    tools = {item["name"]: item for item in manifest.get("tools") or []}

    for name in (
        "get_demo_snapshot",
        "list_demo_series",
        "get_resource_workbench_snapshot",
        "list_resource_role_matrix",
        "query_resource_workbench",
    ):
        assert tools[name]["side_effects"] == "read_only"
        assert tools[name]["side_effect_class"] == "read_only"

    assert tools["operate_metric_note"]["side_effects"] == "local_write"


def test_resource_workbench_webui_exposes_visible_crud_controls():
    webui = json.loads(WEBUI_PATH.read_text(encoding="utf-8"))
    widgets = webui["registry"]["modals"]["demo_metrics_resource_workbench_modal"]["schema"]["widgets"]
    notes = next(widget for widget in widgets if widget["id"] == "workbench-notes")
    form = next(widget for widget in widgets if widget["id"] == "workbench-note-form")

    assert notes["title"] == "Metric notes · Builder E2E validation"
    assert notes["dataSource"]["kind"] == "skill"
    assert notes["dataSource"]["name"] == "demo_metrics_skill.query_resource_workbench"
    first_column = notes["inputs"]["columns"][1]
    assert first_column["kind"] == "buttons"
    assert [button["id"] for button in first_column["buttons"]] == ["edit", "delete"]
    assert [item["id"] for item in form["inputs"]["secondaryActions"]] == ["save_draft", "cancel"]
    skill_actions = {action["on"]: action for action in form["actions"] if action["type"] == "callSkill"}
    assert skill_actions["submit"]["params"]["operation_id"] == "create"
    assert skill_actions["save_draft"]["params"]["operation_id"] == "update"
    assert skill_actions["cancel"]["params"]["operation_id"] == "delete"


def test_main_demo_metrics_modal_is_a_resource_workbench():
    webui = json.loads(WEBUI_PATH.read_text(encoding="utf-8"))
    modal = webui["registry"]["modals"]["demo_metrics_modal"]["schema"]
    compatibility_table = next(widget for widget in modal["widgets"] if widget["id"] == "demo-metrics-table")
    compatibility_chart = next(
        widget for widget in modal["widgets"] if widget["id"] == "demo-metrics-chart-payload"
    )
    semantic_table = next(view for view in modal["semantic"]["views"] if view["id"] == "metrics_grid")
    semantic_chart = next(view for view in modal["semantic"]["views"] if view["id"] == "metrics_chart")
    semantic_notes = next(view for view in modal["semantic"]["views"] if view["id"] == "metrics_notes")
    semantic_form = next(view for view in modal["semantic"]["views"] if view["id"] == "metric_note_form")
    notes = next(widget for widget in modal["widgets"] if widget["id"] == "demo-metric-notes")
    form = next(widget for widget in modal["widgets"] if widget["id"] == "demo-metric-note-form")

    assert compatibility_table["title"] == "Live metrics"
    assert semantic_table["title"] == "Live metrics"
    assert compatibility_chart["title"] == "Recent metric history"
    assert semantic_chart["title"] == "Recent metric history"
    assert compatibility_table["dataSource"]["name"] == "demo_metrics_skill.query_resource_workbench"
    assert compatibility_table["dataSource"]["params"]["resource_type"] == "demo.metric"
    assert semantic_table["source"]["ref"] == "demo_metrics_skill.query_resource_workbench"
    assert semantic_table["source"]["params"]["resource_type"] == "demo.metric"
    assert semantic_notes["kind"] == "collection_grid"
    assert semantic_notes["source"]["kind"] == "skill"
    assert semantic_notes["source"]["ref"] == "demo_metrics_skill.query_resource_workbench"
    assert semantic_notes["source"]["params"] == {
        "resource_type": "demo.metric_note",
        "search": "$state.demo_metrics.selection.metric_id",
    }
    assert [column["key"] for column in semantic_notes["config"]["columns"]] == ["title", "revision"]
    assert semantic_notes["selection"] == {
        "kind": "view",
        "ref": "view:demoMetricNote",
        "scope": "local",
    }
    assert semantic_notes["actions"] == [
        {
            "ref": "action:view.demo_metrics.select_metric_note",
            "kind": "set_view_state",
            "trigger": "select",
            "payload": {
                "id": "$event.id",
                "revision": "$event.revision",
                "title": "$event.title",
                "body": "$event.body",
            },
        }
    ]
    assert semantic_form["kind"] == "form"
    assert [field["id"] for field in semantic_form["config"]["fields"]] == ["title", "body"]
    assert [field["stateKey"] for field in semantic_form["config"]["fields"]] == [
        "demoMetricNote.title",
        "demoMetricNote.body",
    ]
    update_action = next(action for action in semantic_form["actions"] if action["trigger"] == "save_draft")
    delete_action = next(action for action in semantic_form["actions"] if action["trigger"] == "cancel")
    for action in (update_action, delete_action):
        assert action["enabledIf"] == "$state.demoMetricNote.id"
        assert action["payload"]["record_id"] == "$state.demoMetricNote.id"
        assert action["payload"]["expected_revision"] == "$state.demoMetricNote.revision"
    semantic_actions = [
        action
        for action in semantic_notes["actions"] + semantic_form["actions"]
        if action["kind"] == "invoke_skill_action"
    ]
    assert {action["payload"]["operation_id"] for action in semantic_actions} == {
        "create",
        "update",
        "delete",
    }
    assert all(
        action["target"] == "demo_metrics_skill.operate_metric_note"
        for action in semantic_actions
    )
    assert semantic_form["actions"][0]["payload"]["payload"]["metric_id"] == (
        "$state.demo_metrics.selection.metric_id"
    )
    assert notes["dataSource"]["params"]["resource_type"] == "demo.metric_note"
    assert notes["dataSource"]["params"]["search"] == "$state.demo_metrics.selection.metric_id"
    note_actions = [action for action in notes["actions"] if action["type"] == "callSkill"]
    form_actions = [action for action in form["actions"] if action["type"] == "callSkill"]
    assert {action["params"]["operation_id"] for action in note_actions + form_actions} == {
        "create",
        "update",
        "delete",
    }
    assert all(action["target"] == "demo_metrics_skill.operate_metric_note" for action in note_actions + form_actions)
    assert form_actions[0]["params"]["payload"]["metric_id"] == "$state.demo_metrics.selection.metric_id"


def test_summary_actions_put_resource_workbench_after_open_modal():
    webui = json.loads(WEBUI_PATH.read_text(encoding="utf-8"))
    expected_buttons = [
        {"id": "open-demo", "label": "Open modal"},
        {"id": "open-workbench", "label": "Resource Workbench"},
        {"id": "open-workspace", "label": "Data workspace"},
        {"id": "open-operations", "label": "Runtime operations"},
        {"id": "emit-skill", "label": "Skill event"},
        {"id": "emit-host", "label": "Host event"},
    ]

    assert webui["ydoc_defaults"]["data/demo_metrics/summary"]["buttons"] == expected_buttons
    assert _load_handler()._snapshot()["summary"]["buttons"] == expected_buttons
    summary_actions = webui["widgets"][0]["actions"]
    workbench_action = next(action for action in summary_actions if action["on"] == "click:open-workbench")
    assert workbench_action["params"]["to"] == "demo_metrics_skill.demo_metrics_resource_workbench_modal"


def test_metric_note_operation_records_success_and_denial(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    handler = _load_handler()
    state_dir = tmp_path / "state"
    _bind_state_dir(handler, state_dir)

    created = handler.operate_metric_note(
        {
            "role": "owner",
            "payload": {
                "metric_id": "cpu",
                "title": "Unique workbench note 9fd1",
                "body": "Captured from the Resource Workbench demo.",
            },
        }
    )
    denied = handler.operate_metric_note(
        {
            "role": "guest",
            "payload": {
                "metric_id": "cpu",
                "title": "Guest write",
            },
        }
    )

    assert created["ok"] is True
    assert created["result"]["record"]["title"] == "Unique workbench note 9fd1"
    assert denied["ok"] is False
    assert denied["error_type"] == "permission_denied"

    notes = handler.query_resource_workbench({"resource_type": "demo.metric_note", "search": "9fd1"})
    assert notes["ok"] is True
    assert notes["count"] == 1

    traces = ResourceWorkbenchService(state_dir=state_dir).traces(resource_type="demo.metric_note", limit=20)
    assert any(item["status"] == "completed" for item in traces)
    assert any(item["status"] == "permission_denied" for item in traces)


def test_metric_note_operation_updates_and_deletes_with_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    handler = _load_handler()
    state_dir = tmp_path / "state"
    _bind_state_dir(handler, state_dir)

    created = handler.operate_metric_note(
        {
            "role": "owner",
            "payload": {
                "metric_id": "cpu",
                "title": "Editable workbench note",
                "body": "Before update.",
            },
        }
    )
    record = created["result"]["record"]

    updated = handler.operate_metric_note(
        {
            "operation_id": "update",
            "role": "owner",
            "record_id": record["id"],
            "expected_revision": record["revision"],
            "payload": {
                "metric_id": "memory",
                "title": "Updated workbench note",
                "body": "After update.",
            },
        }
    )
    stale = handler.operate_metric_note(
        {
            "operation_id": "update",
            "role": "owner",
            "record_id": record["id"],
            "expected_revision": record["revision"],
            "payload": {
                "title": "Stale update",
            },
        }
    )
    deleted = handler.operate_metric_note(
        {
            "operation_id": "delete",
            "role": "owner",
            "record_id": record["id"],
            "expected_revision": updated["result"]["record"]["revision"],
        }
    )

    assert updated["ok"] is True
    assert updated["result"]["record"]["metric_id"] == "memory"
    assert updated["result"]["record"]["title"] == "Updated workbench note"
    assert updated["result"]["record"]["revision"] == 2
    assert stale["ok"] is False
    assert stale["error_type"] == "conflict"
    assert deleted["ok"] is True
    assert deleted["result"]["deleted"] is True

    notes = handler.query_resource_workbench({"resource_type": "demo.metric_note", "search": "Updated workbench note"})
    assert notes["ok"] is True
    assert notes["count"] == 0


def test_metric_note_operation_accepts_ui_action_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    handler = _load_handler()
    state_dir = tmp_path / "state"
    _bind_state_dir(handler, state_dir)

    created = handler.operate_metric_note(
        {
            "target": "demo_metrics_skill.operate_metric_note",
            "params": {
                "operation_id": "create",
                "role": "owner",
                "payload": {
                    "metric_id": "cpu",
                    "title": "Envelope workbench note",
                    "body": "Captured from a UI action envelope.",
                },
            },
            "context": {
                "widgetId": "workbench-actions",
                "eventId": "sample",
            },
            "webspace_id": "desktop",
        }
    )

    assert created["ok"] is True
    assert created["result"]["record"]["title"] == "Envelope workbench note"


def test_metric_note_operation_preserves_runtime_command_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    handler = _load_handler()

    assert _should_expand_keywords(
        handler.operate_metric_note,
        {
            "operation_id": "create",
            "role": "owner",
            "payload": {
                "metric_id": "cpu",
                "title": "Runtime payload",
            },
        },
    ) is False
