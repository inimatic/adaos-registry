from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location("workflow_lab_handler", ROOT / "handlers" / "main.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_conversation_projects_only_context_dependent_actions(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_STATE_DIR", str(tmp_path / "state"))
    module = _module()
    initial = module.chat(text="What can I do?", webspace_id="lab-test", locale="en")
    assert initial["workflow"]["state"] == "collecting"
    assert [item["command"] for item in initial["workflow"]["allowed_commands"]] == ["submit", "cancel"]

    submitted = module.workflow_action(
        "submit",
        idempotency_key="lab-test:submit",
        webspace_id="lab-test",
        locale="ru",
    )
    assert submitted["ok"] is True
    assert submitted["workflow"]["state"] == "review"
    assert {item["command"] for item in submitted["workflow"]["allowed_commands"]} == {"approve", "cancel", "revise"}

    duplicate = module.workflow_action(
        "submit",
        idempotency_key="lab-test:submit",
        webspace_id="lab-test",
        locale="ru",
    )
    assert duplicate["ok"] is True
    assert duplicate["execution"]["status"] == "duplicate"
    assert duplicate["workflow"]["state"] == "review"

    approved = module.workflow_action(
        "approve",
        idempotency_key="lab-test:approve",
        confirmed=True,
        webspace_id="lab-test",
        locale="ru",
    )
    assert approved["ok"] is True
    assert approved["workflow"]["state"] == "completed"
    assert approved["message"] == "Запрос одобрен."
