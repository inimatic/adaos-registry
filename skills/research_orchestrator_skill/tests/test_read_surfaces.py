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


def test_provider_compatible_smoke_policy_is_resolved_through_manager_capabilities() -> None:
    calls = []

    def invoke(skill_id, operation, payload, **kwargs):
        calls.append((skill_id, operation, payload, kwargs))
        return {
            "schema": "adaos.execution.provider_status.v1",
            "provider": {
                "provider_id": "local-process",
                "features": ["process", "network_observation"],
            },
            "provider_digest": "sha256:" + "4" * 64,
            "admission_contract": "adaos.execution.admission.v1",
        }

    orchestrator = ResearchOrchestrator(
        repository=SimpleNamespace(),
        skill_invoker=invoke,
    )

    binding = orchestrator._resolve_workflow_smoke_policy(
        "provider_compatible_noninferential"
    )

    assert calls == [
        (
            "research_manager_skill",
            "execution_provider_status",
            {},
            {"timeout": 120},
        )
    ]
    assert binding["requirements"]["network_mode"] == "unrestricted"
    assert binding["network_enforcement"] == "not_required"


def test_parent_contract_inheritance_resolves_only_an_accepted_bound_compilation() -> None:
    problem = {"title": "parent problem"}
    protocol = {"title": "parent protocol"}
    parent = {
        "task_id": "direction.task-001",
        "ref": "research-task:direction.task-001",
        "status": "accepted",
        "current_prototype_digest": "sha256:" + "1" * 64,
        "accepted_compilation_digest": "sha256:" + "2" * 64,
    }
    repository = SimpleNamespace(
        get_task=lambda task_id: parent if task_id == parent["task_id"] else None,
        get_compilation_record=lambda digest: {
            "prototype_digest": parent["current_prototype_digest"],
            "source_bundle_digest": "sha256:" + "3" * 64,
            "payload": {
                "facets": {
                    "research_problem": {
                        "source_stage": "problem_frame",
                        "payload": problem,
                    },
                    "experimental_protocol": {
                        "source_stage": "protocol_design",
                        "payload": protocol,
                    },
                }
            },
        },
    )
    orchestrator = ResearchOrchestrator(repository=repository)

    inheritance = orchestrator._resolve_formulation_inheritance(
        {
            "branch_of_task_id": parent["task_id"],
            "parent_task_id": None,
        },
        "preserve_parent_scientific_contract",
    )

    assert inheritance["parent_task_ref"] == parent["ref"]
    assert inheritance["problem_frame"] == problem
    assert inheritance["protocol_design"] == protocol
    assert inheritance["parent_compilation_digest"] == parent[
        "accepted_compilation_digest"
    ]


def test_implementation_project_keeps_direction_identity_outside_task_lifecycle(monkeypatch) -> None:
    stale = {
        "schema": "adaos.project.v1",
        "kind": "project",
        "id": "direction_implementation",
        "version": "0.1.0",
        "profiles": ["adaos.research.implementation.v1"],
        "components": {
            "owned": [{"ref": "skill:direction", "role": "primary"}],
            "dependencies": [],
        },
        "entrypoints": [
            {
                "id": "research",
                "presentation": "scenario:research_workbench",
                "default": True,
                "bindings": {
                    "direction_ref": "research-direction:direction",
                    "task_ref": "research-task:direction.task-001",
                },
            }
        ],
        "catalog": {
            "title": "Direction implementation",
            "description": "Project-scoped implementation for research-task:direction.task-001.",
            "categories": ["research"],
            "tags": [],
        },
        "compatibility": {},
        "lifecycle": {"uninstall": {}},
        "ref": "project:direction_implementation",
        "manifest_digest": "sha256:" + "a" * 64,
        "source_path": "dev/projects/direction_implementation",
    }
    captured: dict[str, object] = {}
    monkeypatch.setattr(orchestrator_module.compositions, "get", lambda _project_id: stale)

    def replace(project_id, value, *, expected_manifest_digest):
        captured.update(
            project_id=project_id,
            value=value,
            expected_manifest_digest=expected_manifest_digest,
        )
        return {**value, "ref": f"project:{project_id}", "manifest_digest": "sha256:" + "b" * 64}

    monkeypatch.setattr(orchestrator_module.compositions, "replace", replace)
    orchestrator = ResearchOrchestrator(repository=object())

    project = orchestrator._ensure_implementation_project(
        {
            "direction_id": "direction",
            "title": "Direction",
            "artifact_owner_skill_id": "direction",
            "legacy_project_ref": None,
        },
        {"task_id": "direction.task-004"},
    )

    bindings = project["entrypoints"][0]["bindings"]
    assert bindings == {"direction_ref": "research-direction:direction"}
    assert project["catalog"]["description"] == (
        "Project-scoped implementation workspace for research-direction:direction."
    )
    assert captured["expected_manifest_digest"] == stale["manifest_digest"]
