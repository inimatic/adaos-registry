from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from adaos.services.resources import ResourceWorkbenchService


HANDLER_PATH = Path(__file__).resolve().parents[1] / "handlers" / "main.py"


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
