from __future__ import annotations

from types import SimpleNamespace

import pytest

from handlers import main as handlers
from research import orchestrator as orchestrator_module
from research.orchestrator import ResearchOrchestrator


def _uninitialized_orchestrator(monkeypatch: pytest.MonkeyPatch) -> ResearchOrchestrator:
    repository = SimpleNamespace(get_direction=lambda _direction_id: None)
    orchestrator = ResearchOrchestrator(repository=repository)
    monkeypatch.setattr(
        orchestrator,
        "_require_direction_project",
        lambda _direction_id: {
            "project": {
                "id": "project-uninitialized",
                "ref": "project:project-uninitialized",
                "title": "Uninitialized research project",
                "profiles": ["adaos.research.direction.v1"],
            }
        },
    )
    monkeypatch.setattr(
        orchestrator_module.artifact_context,
        "source_bundle",
        lambda _direction_id, **_kwargs: {"digest": None, "sources": [], "generation": 0},
    )
    monkeypatch.setattr(orchestrator_module.artifact_context, "groups", lambda _direction_id: [])
    return orchestrator


def test_existing_uninitialized_direction_has_canonical_read_state(monkeypatch) -> None:
    orchestrator = _uninitialized_orchestrator(monkeypatch)

    state = orchestrator.get("research-uninitialized")

    assert state["ok"] is True
    assert state["initialized"] is False
    assert state["direction"]["status"] == "not_initialized"
    assert state["formulation"]["can_accept"] is False
    assert state["next_steps"][0]["id"] == "initialize"


def test_unknown_direction_still_fails_closed(monkeypatch) -> None:
    orchestrator = _uninitialized_orchestrator(monkeypatch)

    def _unknown_project(_direction_id: str):
        raise ValueError("unknown research project")

    monkeypatch.setattr(orchestrator, "_require_direction_project", _unknown_project)

    with pytest.raises(ValueError, match="unknown research project"):
        orchestrator.get("unknown-direction")


def test_uninitialized_direction_read_tools_return_empty_data_not_errors(monkeypatch) -> None:
    orchestrator = _uninitialized_orchestrator(monkeypatch)
    monkeypatch.setattr(handlers, "_orchestrator", lambda: orchestrator)

    direction = handlers.get_direction("research-uninitialized")
    artifacts = handlers.list_artifacts("research-uninitialized")
    consensus = handlers.get_consensus("research-uninitialized")
    automation = handlers.get_automation_brief("research-uninitialized")

    assert direction["ok"] is True
    assert direction["initialized"] is False
    assert artifacts == {
        "ok": True,
        "initialized": False,
        "direction_id": "research-uninitialized",
        "items": [],
        "count": 0,
    }
    assert consensus["ok"] is True
    assert consensus["initialized"] is False
    assert consensus["status"] == "not_formulated"
    assert automation["ok"] is True
    assert automation["available"] is False
    assert automation["initialized"] is False


def test_staged_discussion_rejects_accepted_task_before_source_or_llm_work(monkeypatch) -> None:
    repository = SimpleNamespace(
        get_direction=lambda _direction_id: {
            "direction_id": "accepted-direction",
            "active_task_id": "accepted-direction.task-001",
            "accepted_prototype_digest": "sha256:" + "a" * 64,
        }
    )
    orchestrator = ResearchOrchestrator(repository=repository)

    def _unexpected_source_access(*_args, **_kwargs):
        raise AssertionError("source compaction must not run for an immutable task")

    monkeypatch.setattr(
        orchestrator_module.artifact_context,
        "source_bundle",
        _unexpected_source_access,
    )

    with pytest.raises(ValueError, match="new branch ResearchTask"):
        orchestrator._discuss_staged(
            "accepted-direction",
            "Revise this accepted formulation.",
            dialog_payload={"task_id": "accepted-direction.task-001"},
        )
