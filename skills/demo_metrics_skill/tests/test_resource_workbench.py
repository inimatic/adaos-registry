from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

from adaos.services.resources import ResourceWorkbenchService


HANDLER_PATH = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
SKILL_PATH = Path(__file__).resolve().parents[1] / "skill.yaml"


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
