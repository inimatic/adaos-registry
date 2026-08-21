from __future__ import annotations

import asyncio
import ast
import importlib.util
from pathlib import Path

import pytest
import yaml


def _module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    spec = importlib.util.spec_from_file_location("builder_sdk_control_skill_test_handler", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_handler_has_sdk_only_adaos_imports() -> None:
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    adaos_imports = [name for name in imports if name == "adaos" or name.startswith("adaos.")]

    assert adaos_imports
    assert all(name == "adaos.sdk" or name.startswith("adaos.sdk.") for name in adaos_imports)


def test_manifest_declares_trusted_local_effects_for_interactive_tools() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "skill.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    tools = {item["name"]: item for item in manifest["tools"]}

    assert tools["save_project_file"]["side_effects"] == "local_write"
    assert tools["create_project"]["side_effects"] == "local_write"
    assert tools["start_automation"]["side_effects"] == "local_write"
    assert tools["release_candidate_runtime"]["side_effects"] == "local_write"
    assert tools["submit_automation"]["side_effects"] == "local_write"
    assert tools["save_prompt_context"]["side_effects"] == "local_write"
    assert tools["append_prompt_addendum"]["side_effects"] == "local_write"
    assert tools["set_llm_profile"]["side_effects"] == "local_write"
    assert tools["update_project_metadata"]["side_effects"] == "local_write"
    assert tools["archive_project"]["side_effects"] == "local_write"
    assert "update_project" not in tools
    assert tools["select_preview"]["side_effects"] == "ui_navigation"
    assert tools["select_preview_target"]["side_effects"] == "ui_navigation"
    assert tools["transition_workflow"]["side_effects"] == "local_write"
    transition_actions = tools["transition_workflow"]["input_schema"]["properties"]["action"]["enum"]
    assert {
        "reconcile_automation",
        "reconcile_verification",
        "reconcile_publication",
    }.issubset(transition_actions)
    assert tools["inspect_process_ref"]["side_effects"] == "local_write"
    assert tools["apply_semantic_ui_change"]["side_effects"] == "local_write"
    assert tools["register_review_constraint"]["side_effects"] == "local_write"
    assert tools["evaluate_review_constraints"]["side_effects"] == "local_write"
    assert tools["get_interaction_frame"]["side_effects"] == "none"
    assert tools["get_process"]["side_effects"] == "none"
    assert tools["get_change_context"]["side_effects"] == "none"
    assert tools["plan_change_set"]["side_effects"] == "local_write"
    assert tools["rebase_change"]["side_effects"] == "local_write"
    assert set(tools["rebase_change"]["input_schema"]["required"]) == {
        "change_id",
        "expected_project_generation",
        "verified_unchanged_refs",
    }
    assert tools["add_change_issues"]["side_effects"] == "local_write"
    assert tools["update_change_issue"]["side_effects"] == "local_write"
    assert tools["link_dependency_checkpoint"]["side_effects"] == "local_write"
    link_schema = tools["link_dependency_checkpoint"]["input_schema"]
    assert set(link_schema["required"]) == {
        "object_type",
        "object_id",
        "dependency_type",
        "dependency_id",
        "checkpoint_change_id",
    }
    assert tools["return_to_prototype"]["side_effects"] == "local_write"
    assert tools["recover_validated_automation"]["side_effects"] == "local_write"
    assert tools["record_development_feedback"]["side_effects"] == "local_write"
    assert tools["list_development_feedback"]["side_effects"] == "none"
    assert tools["reconcile_automation_checkpoint"]["side_effects"] == "external_write"
    assert tools["apply_subscription_update"]["side_effects"] == "external_write"
    assert tools["push_project"]["side_effects"] == "external_write"
    assert tools["publish_project"]["side_effects"] == "external_write"
    assert tools["delete_project"]["side_effects"] == "external_write"
    declared_effects = {"none", "local_write", "ui_navigation", "external_write"}
    assert all(tool.get("side_effects") in declared_effects for tool in tools.values())
    push_schema = tools["push_project"]["input_schema"]
    assert set(push_schema["required"]) == {"checkpoint_id", "confirmed"}
    assert push_schema["properties"]["checkpoint_id"]["minLength"] == 1


def test_release_preflight_requires_bound_consumer_acceptance(monkeypatch) -> None:
    module = _module()
    policy = {
        "session_id": "dev-research",
        "acceptance_requirements": [
            {
                "id": "research.consumer-contracts",
                "required": True,
            }
        ],
    }
    monkeypatch.setattr(
        module,
        "_bound_development_session",
        lambda *_args, **_kwargs: {"session": policy, "binding": {}},
    )
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **_kwargs: {
            "session": {
                "development_session_id": "dev-research",
                "completion_readiness": {
                    "acceptance": {
                        "ok": False,
                        "errors": ["prepare_attempt incompatible"],
                    }
                },
            }
        },
    )
    with pytest.raises(ValueError, match="consumer-owned acceptance"):
        module._required_acceptance_evidence("skill", "research", "research-dev")

    receipt = {
        "requirement_id": "research.consumer-contracts",
        "required": True,
        "ok": True,
        "digest": "sha256:" + "1" * 64,
    }
    acceptance = {
        "ok": True,
        "digest": "sha256:" + "2" * 64,
        "receipts": [receipt],
    }
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **_kwargs: {
            "session": {
                "development_session_id": "dev-research",
                "completion_readiness": {"acceptance": acceptance},
            }
        },
    )
    assert module._required_acceptance_evidence(
        "skill", "research", "research-dev"
    ) == acceptance


def test_subscription_update_projection_exposes_review_contract(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.projects,
        "inspect_subscription_update",
        lambda _project_id: {
            "ok": True,
            "subscription": {
                "project_id": "builder",
                "policy": "notify",
                "installed_release": "builder@1.0.0",
            },
            "pointer": {"release": "builder@1.1.0"},
            "available": True,
            "activation_allowed": True,
            "update_plan": {
                "plan_digest": "sha256:" + "a" * 64,
                "activation": {
                    "target_release": "builder@1.1.0",
                    "observed_lock_digest": "sha256:" + "b" * 64,
                    "component_changes": {
                        "added": ["skill:builder_skill"],
                        "changed": ["scenario:builder"],
                        "removed": [],
                        "retained": [],
                    },
                    "resolved_dependencies": [
                        {"kind": "skill", "artifact_id": "builder_skill", "version": "1.1.0"}
                    ],
                    "permissions": {"introduced": ["network.read"], "removed": []},
                    "schemas": {"added": ["builder.v2"], "changed": [], "removed": []},
                    "migrations": {"count": 1, "rollback_ready": True},
                    "rollback": {"available": True, "reason": "previous_workspace_lock"},
                    "warnings": [],
                },
            },
        },
    )

    result = module.get_subscription_update("scenario", "builder")

    assert result["available"] is True
    assert result["target_release"] == "builder@1.1.0"
    assert "scenario:builder" in result["components"]
    assert "skill:builder_skill @ 1.1.0" in result["dependencies"]
    assert result["permissions"] == "network.read"
    assert result["rollback_available"] is True


def test_subscription_update_apply_binds_digest_and_explicit_attempt(monkeypatch) -> None:
    module = _module()
    calls: list[dict] = []

    async def _apply(kind: str, project_id: str, **kwargs):
        calls.append({"kind": kind, "project_id": project_id, **kwargs})
        return {"ok": True, "mode": "package_activation"}

    monkeypatch.setattr(module.projects, "apply_subscription_update", _apply)
    result = asyncio.run(
        module.apply_subscription_update(
            "sha256:" + "a" * 64,
            "scenario",
            "builder",
            approve_permissions=True,
            webspace_id="desktop",
        )
    )

    assert result["idempotency_key"] == (
        "builder-update:scenario:builder:sha256:" + "a" * 64
    )
    assert calls[0]["expected_plan_digest"] == "sha256:" + "a" * 64
    assert calls[0]["permission_decision"]["actor"] == "builder.user"
    assert calls[0]["idempotency_key"] == result["idempotency_key"]


def test_push_requires_explicit_checkpoint_identity() -> None:
    module = _module()

    try:
        module.push_project(checkpoint_id="   ", confirmed=True)
    except ValueError as exc:
        assert str(exc) == "checkpoint_id is required"
    else:
        raise AssertionError("blank checkpoint identity must be rejected")


def test_push_requires_confirmation_before_forge_write(monkeypatch) -> None:
    module = _module()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        module.projects,
        "push",
        lambda *args, **kwargs: calls.append((*args, kwargs)) or {},
    )

    with pytest.raises(ValueError, match="explicit user confirmation"):
        module.push_project(checkpoint_id="checkpoint-not-confirmed")

    assert calls == []


def _dependency_link_setup(monkeypatch, module, *, declared: bool = True, delivery: dict | None = None):
    checkpoint_id = "dependency-checkpoint-1"
    change_set = {
        "change_set_id": "scenario-change-set-1",
        "status": "in_progress",
        "request": "Build candidate",
        "member_change_ids": ["scenario-change-set-1"],
    }
    manifest = "name: recipes\ndepends:\n  - control_skill\n" if declared else "name: recipes\ndepends: []\n"
    monkeypatch.setattr(
        module.projects,
        "read_file",
        lambda *args, **kwargs: {"content": manifest},
    )
    monkeypatch.setattr(module, "_context", lambda *args: {"archived": False})
    dependency_delivery = delivery if delivery is not None else {
        "status": "checkpoint",
        "checkpoint_change_id": checkpoint_id,
        "package_digest": "sha256:" + "a" * 64,
        "source_revision": "b" * 40,
    }

    def get_state(kind, project_id):
        if (kind, project_id) == ("skill", "control_skill"):
            return {"delivery": dict(dependency_delivery)}
        return {"change_set": change_set}

    transitions: list[dict] = []

    def transition(kind, project_id, action, **kwargs):
        transitions.append({"kind": kind, "project_id": project_id, "action": action, **kwargs})
        change_id = kwargs["metadata"]["change_id"]
        if change_id not in change_set["member_change_ids"]:
            change_set["member_change_ids"].append(change_id)
        return {"ok": True, "workflow": {"change_set": change_set}}

    monkeypatch.setattr(module.workflow, "get_state", get_state)
    monkeypatch.setattr(module.workflow, "transition", transition)
    monkeypatch.setattr(module, "_sync_change_set_record", lambda **kwargs: {"change_id": kwargs["change_set"]["change_set_id"]})
    return checkpoint_id, change_set, transitions


def test_link_dependency_checkpoint_records_exact_immutable_receipt(monkeypatch) -> None:
    module = _module()
    checkpoint_id, change_set, transitions = _dependency_link_setup(monkeypatch, module)

    result = module.link_dependency_checkpoint(
        "skill", "control_skill", checkpoint_id, "scenario", "recipes", webspace_id="desktop"
    )

    assert result["linked"] is True
    assert result["idempotent"] is False
    assert change_set["member_change_ids"][-1] == checkpoint_id
    assert result["dependency_receipt"] == {
        "object_type": "skill",
        "object_id": "control_skill",
        "status": "checkpoint",
        "checkpoint_change_id": checkpoint_id,
        "package_digest": "sha256:" + "a" * 64,
        "source_revision": "b" * 40,
    }
    assert transitions[0]["action"] == "change_evidence_recorded"
    assert transitions[0]["metadata"] == {
        "change_set_id": "scenario-change-set-1",
        "change_id": checkpoint_id,
    }


def test_link_dependency_checkpoint_is_idempotent(monkeypatch) -> None:
    module = _module()
    checkpoint_id, change_set, _ = _dependency_link_setup(monkeypatch, module)

    module.link_dependency_checkpoint("skill", "control_skill", checkpoint_id, "scenario", "recipes")
    result = module.link_dependency_checkpoint("skill", "control_skill", checkpoint_id, "scenario", "recipes")

    assert result["linked"] is False
    assert result["idempotent"] is True
    assert change_set["member_change_ids"].count(checkpoint_id) == 1


def test_link_dependency_checkpoint_rejects_undeclared_dependency(monkeypatch) -> None:
    module = _module()
    checkpoint_id, _, transitions = _dependency_link_setup(monkeypatch, module, declared=False)

    try:
        module.link_dependency_checkpoint("skill", "control_skill", checkpoint_id, "scenario", "recipes")
    except ValueError as exc:
        assert "not declared" in str(exc)
    else:
        raise AssertionError("undeclared dependency must be rejected")
    assert transitions == []


def test_link_dependency_checkpoint_rejects_checkpoint_mismatch(monkeypatch) -> None:
    module = _module()
    _, _, transitions = _dependency_link_setup(monkeypatch, module)

    try:
        module.link_dependency_checkpoint("skill", "control_skill", "different", "scenario", "recipes")
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("checkpoint mismatch must be rejected")
    assert transitions == []


def test_link_dependency_checkpoint_rejects_incomplete_receipt(monkeypatch) -> None:
    module = _module()
    delivery = {
        "status": "checkpoint",
        "checkpoint_change_id": "dependency-checkpoint-1",
        "package_digest": "",
        "source_revision": "b" * 40,
    }
    checkpoint_id, _, transitions = _dependency_link_setup(monkeypatch, module, delivery=delivery)

    try:
        module.link_dependency_checkpoint("skill", "control_skill", checkpoint_id, "scenario", "recipes")
    except ValueError as exc:
        assert "receipt is incomplete" in str(exc)
    else:
        raise AssertionError("incomplete immutable receipt must be rejected")
    assert transitions == []


def test_get_state_keeps_capability_failures_separate(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module.projects, "describe", lambda *args: {"ok": True, "id": "builder"})
    monkeypatch.setattr(module.projects, "list_files", lambda *args, **kwargs: [{"path": "scenario.json"}])
    monkeypatch.setattr(module.preview, "dev_webspace_id", lambda source: f"{source}-dev")
    monkeypatch.setattr(module.preview, "canonical_source_webspace_id", lambda source: source)
    monkeypatch.setattr(module.preview, "get_binding", lambda source: {"ok": True, "source_webspace_id": source})
    monkeypatch.setattr(module.preview, "open_workspace", lambda source: {"url": f"/?webspace={source}-dev"})
    monkeypatch.setattr(module.automation, "get_state", lambda **kwargs: {"ok": False, "error": "idle"})
    monkeypatch.setattr(module.conversation, "list_development_changes", lambda **kwargs: [])

    result = module.get_state(webspace_id="desktop")

    assert result["ok"] is True
    assert result["schema"] == "adaos.builder.sdk_control.v2"
    assert result["checks"]["project"]["value"]["id"] == "builder"
    assert result["checks"]["automation"]["ok"] is False
    assert result["checks"]["changes"]["value"] == []


def test_file_and_automation_tools_forward_stable_project_identity(monkeypatch) -> None:
    module = _module()
    calls: list[tuple[str, tuple, dict]] = []
    run_calls: list[dict] = []
    monkeypatch.setattr(
        module.projects,
        "read_file",
        lambda *args, **kwargs: calls.append(("read", args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        module.projects,
        "write_file",
        lambda *args, **kwargs: calls.append(("write", args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        module.automation,
        "start",
        lambda **kwargs: calls.append(("start", (), kwargs)) or {"ok": True, "status": "queued"},
    )
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *args: {
            "change": {
                "change_id": "CS-builder",
                "context_packet_digest": "sha256:" + "a" * 64,
            },
            "change_set": {"change_set_id": "CS-builder"},
        },
    )
    monkeypatch.setattr(module.preview, "dev_webspace_id", lambda source: f"{source}-dev")
    monkeypatch.setattr(
        module.conversation,
        "ensure_builder_topic",
        lambda **kwargs: {"conversation_id": "conv", "topic_id": "topic"},
    )
    monkeypatch.setattr(
        module.conversation,
        "upsert_development_change",
        lambda **kwargs: {"change_id": kwargs["change_id"], "status": kwargs["status"]},
    )
    monkeypatch.setattr(
        module.conversation,
        "get_development_change",
        lambda change_id: {"change_id": change_id},
    )
    monkeypatch.setattr(
        module.conversation,
        "upsert_development_run",
        lambda **kwargs: run_calls.append(kwargs)
        or {
            "schema": "adaos.builder.run.v1",
            "run_id": kwargs["run_id"],
            "change_id": kwargs["change_id"],
            "status": kwargs["status"],
        },
    )

    module.read_project_file("builder_memory.md")
    saved = module.save_project_file("builder_memory.md", "memory", webspace_id="desktop")
    started = module.start_automation("Implement it", webspace_id="desktop")

    assert calls[0][1][:2] == ("scenario", "builder")
    assert calls[1][1][:2] == ("scenario", "builder")
    assert calls[2][2]["object_type"] == "scenario"
    assert calls[2][2]["object_id"] == "builder"
    assert calls[2][2]["change_set_id"] == "CS-builder"
    assert calls[2][2]["conversation_id"] == "conv"
    assert saved["evidence"]["status"] == "accepted"
    assert saved["evidence"]["canonical_change_id"] == "CS-builder"
    assert saved["evidence"]["run_synced"] is True
    assert run_calls[0]["change_id"] == "CS-builder"
    assert run_calls[0]["run_id"] == saved["evidence"]["change_id"]
    assert run_calls[0]["context_packet_digest"] == "sha256:" + "a" * 64
    assert started["status"] == "queued"


def test_start_automation_uses_exact_bound_instruction_without_manual_paste(monkeypatch) -> None:
    module = _module()
    digest = "sha256:" + "8" * 64
    brief = {
        "schema": "adaos.research.automation_brief.v1",
        "digest": digest,
        "objective": "Build a reproducible CPU experiment base.",
        "implementation_requirements": [
            {
                "id": "REQ-EXEC-1",
                "requirement": "Implement an immutable typed RunSpec.",
                "verification": "RunSpec conformance test",
            }
        ],
        "acceptance_checks": [
            {
                "id": "AC-EXEC-1",
                "check": "The smoke profile runs without scientific inference.",
                "evidence": "Smoke report",
            }
        ],
        "prohibited_actions": ["Do not start scientific execution."],
    }
    planned: list[dict] = []
    launched: list[dict] = []
    monkeypatch.setattr(
        module,
        "_bound_automation_instruction",
        lambda *_args: {
            "value": brief,
            "instruction": {"path": "C:/state/instructions/automation_brief.json"},
            "session": {"session_id": "dev-tlp"},
        },
    )
    monkeypatch.setattr(
        module,
        "_project_topic",
        lambda *_args, **_kwargs: {"conversation_id": "conv", "topic_id": "topic"},
    )
    monkeypatch.setattr(module.workflow, "get_state", lambda *_args: {})
    monkeypatch.setattr(
        module,
        "plan_change_set",
        lambda **kwargs: planned.append(kwargs)
        or {"workflow": {"change_set": {"change_set_id": "change-tlp"}}},
    )
    monkeypatch.setattr(
        module.automation,
        "start",
        lambda **kwargs: launched.append(kwargs) or {"ok": True, "status": "queued"},
    )

    result = module.start_automation(
        object_type="skill",
        object_id="tlp_research_03",
        webspace_id="desktop-dev",
    )

    assert result["status"] == "queued"
    assert len(planned[0]["request"]) < 4000
    assert [item["issue_id"] for item in planned[0]["issues"]] == ["REQ-EXEC-1", "AC-EXEC-1"]
    assert launched[0]["implementation_brief"] == module._instruction_text(brief)
    assert launched[0]["brief_path"].endswith("automation_brief.json")
    assert launched[0]["change_set_id"] == "change-tlp"
    assert launched[0]["development_session_id"] == "dev-tlp"


def test_get_automation_exposes_compact_workflow_head(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module,
        "_project_topic",
        lambda *_args, **_kwargs: {"conversation_id": "conv", "topic_id": "topic"},
    )
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **_kwargs: {
            "ok": True,
            "automation": {"status": "completed", "task_id": "task-old"},
        },
    )
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *_args: {
            "governed": {"state": "published", "generation": 15},
            "change_set": {"change_set_id": "change-old", "status": "published"},
            "delivery": {"status": "published"},
            "capabilities": {"can_plan_change_set": True},
        },
    )

    result = module.get_automation(
        object_type="skill",
        object_id="tlp_direction",
        webspace_id="research-dev",
    )

    assert result["workflow_head"] == {
        "schema": "adaos.builder.workflow_head.v1",
        "available": True,
        "error": None,
        "state": "published",
        "generation": 15,
        "change_set_id": "change-old",
        "change_status": "published",
        "delivery_status": "published",
        "can_plan_change_set": True,
    }


def test_submit_automation_rebinds_terminal_session_to_current_builder_host(monkeypatch) -> None:
    module = _module()
    submitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        module,
        "_bound_development_session",
        lambda *_args: {"session": {"session_id": "dev-current"}, "binding": {}},
    )
    monkeypatch.setattr(
        module,
        "_project_topic",
        lambda *_args, **_kwargs: {"conversation_id": "conv", "topic_id": "topic"},
    )
    monkeypatch.setattr(
        module.automation,
        "submit",
        lambda text, **kwargs: submitted.append((text, kwargs)) or {"ok": True},
    )

    result = module.submit_automation(
        "Rebase the exact current Development Session.",
        object_type="skill",
        object_id="tlp_direction",
        webspace_id="research-dev",
    )

    assert result["ok"] is True
    assert submitted[0][1]["development_session_id"] == "dev-current"


def test_release_candidate_runtime_uses_exact_sdk_binding(monkeypatch) -> None:
    module = _module()
    calls: list[dict] = []
    monkeypatch.setattr(
        module.automation,
        "release_candidate_runtime",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
    )

    result = module.release_candidate_runtime(
        object_id="candidate_skill",
        development_session_id="dev_candidate_01",
    )

    assert result["ok"] is True
    assert calls == [
        {
            "object_type": "skill",
            "object_id": "candidate_skill",
            "development_session_id": "dev_candidate_01",
        }
    ]


def test_start_automation_rejects_free_form_replacement_of_bound_instruction(monkeypatch) -> None:
    module = _module()
    brief = {"digest": "sha256:" + "9" * 64, "objective": "Exact objective"}
    monkeypatch.setattr(
        module,
        "_bound_automation_instruction",
        lambda *_args: {
            "value": brief,
            "instruction": {"path": "C:/state/instructions/automation_brief.json"},
            "session": {"session_id": "dev-tlp"},
        },
    )

    with pytest.raises(ValueError, match="free-form replacement"):
        module.start_automation(
            "Do something else",
            object_type="skill",
            object_id="tlp_research_03",
        )


def test_plan_change_set_persists_workflow_and_durable_change_evidence(monkeypatch) -> None:
    module = _module()
    transitions: list[dict] = []
    evidence_calls: list[dict] = []
    change_set = {
        "schema": "adaos.builder.change_set.v1",
        "change_set_id": "CS-builder-layout",
        "request": "Move the preview action into the Lifecycle node.",
        "route": "prototype_first",
        "gate": "prototype",
        "status": "planned",
        "issues": [
            {
                "issue_id": "layout",
                "title": "Move preview action",
                "lane": "prototype",
                "status": "open",
                "acceptance_criteria": ["The selected node exposes Show in Preview."],
            }
        ],
        "member_change_ids": ["CS-builder-layout"],
        "source_message_ids": ["message-1"],
    }
    monkeypatch.setattr(
        module.workflow,
        "transition",
        lambda *args, **kwargs: transitions.append(kwargs["metadata"])
        or {"ok": True, "workflow": {"change_set": change_set}},
    )
    monkeypatch.setattr(
        module.workflow,
        "build_context_packet",
        lambda *args, **kwargs: {
            "schema": "adaos.builder.context_packet.v1",
            "digest": "sha256:" + "a" * 64,
        },
    )
    monkeypatch.setattr(
        module.conversation,
        "ensure_builder_topic",
        lambda **kwargs: {"conversation_id": "conv", "topic_id": "topic", "thread_id": "thread"},
    )
    monkeypatch.setattr(
        module.conversation,
        "upsert_development_change",
        lambda **kwargs: evidence_calls.append(kwargs) or {"change_id": kwargs["change_id"], "status": kwargs["status"]},
    )

    result = module.plan_change_set(
        request=change_set["request"],
        issues=change_set["issues"],
        object_type="scenario",
        object_id="builder",
        change_set_id="CS-builder-layout",
        source_message_ids=["message-1"],
        webspace_id="desktop",
    )

    assert transitions[0]["change_set_id"] == "CS-builder-layout"
    assert transitions[0]["issues"][0]["lane"] == "prototype"
    assert evidence_calls[0]["status"] == "planned"
    assert evidence_calls[0]["meta"]["change_set"]["route"] == "prototype_first"
    assert result["evidence_synced"] is True


def test_rebase_change_uses_project_generation_and_verified_refs(monkeypatch) -> None:
    module = _module()
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        module.workflow,
        "rebase_change",
        lambda *args, **kwargs: calls.append((args, kwargs))
        or {"ok": True, "project": {"artifact_generation": 3}},
    )

    result = module.rebase_change(
        "CH-reviewed-repair",
        12,
        ["skill:research_runner", "", "  "],
        object_type="skill",
        object_id="research_runner",
    )

    assert result["ok"] is True
    assert calls == [
        (
            ("skill", "research_runner", "CH-reviewed-repair"),
            {
                "expected_project_generation": 12,
                "verified_unchanged_refs": ["skill:research_runner"],
            },
        )
    ]


def test_get_automation_exposes_missing_session_as_idle(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **kwargs: {
            "ok": False,
            "error": "automation_session_not_found",
            "automation": {"status": "idle", "webspace_id": kwargs["webspace_id"]},
        },
    )

    result = module.get_automation(webspace_id="builder-dev")

    assert result["ok"] is True
    assert result["session_present"] is False
    assert result["automation"]["status"] == "idle"
    assert "error" not in result


def test_get_automation_exposes_source_prototype_metadata(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **kwargs: {
            "ok": True,
            "automation": {
                "status": "running",
                "version": "0.4.0",
                "source_prototype_version": "UI 037",
                "updated_at": 1784790000,
            },
        },
    )

    result = module.get_automation(webspace_id="builder-dev")

    assert len(result["version"].split(".")) == 3
    assert all(part.isdigit() for part in result["version"].split("."))
    assert result["source_prototype_version"] == "UI 037"
    assert result["updated_at"].endswith("Z")


def test_lifecycle_exposes_bounded_automation_result_children(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module.projects, "describe", lambda *args: {"version": "0.2.0"})
    monkeypatch.setattr(module.projects, "list_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(module.conversation, "list_development_changes", lambda **kwargs: [])
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **kwargs: {
            "ok": True,
            "automation": {
                "status": "succeeded",
                "phase": "verification",
                "task_id": "task-42",
                "summary": "Implementation and checks completed",
                "result_branch": "builder/task-42",
                "evidence": {
                    "result_path": "results/task-42.json",
                    "events_path": "events/task-42.jsonl",
                    "stderr_path": "logs/task-42.stderr",
                    "extra_path": "logs/task-42.extra",
                    "trace_path": "logs/task-42.trace",
                    "overflow_path": "logs/task-42.overflow",
                },
            },
        },
    )

    lifecycle = module.get_lifecycle("skill", "builder")
    child = lifecycle[0]["children"][0]["children"][0]

    assert child["kind"] == "automation_result"
    assert child["title"] == "v DEV"
    assert child["version"] in child["title"]
    assert child["version"] != child["source_prototype_version"]
    assert child["source_prototype_version"] == "0.2.0"
    assert child["status"] == "succeeded"
    assert child["phase"] == "verification"
    assert child["summary"] == "Implementation and checks completed"
    assert child["task_id"] == "task-42"
    assert child["result_branch"] == "builder/task-42"
    assert len(child["evidence"]) == module.MAX_LIFECYCLE_CHILDREN
    assert child["lifecycleStage"] == "automation"
    assert child["conversationLabel"] == "Automation conversation"


def test_new_scenario_has_only_prototype_before_handoff(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.projects,
        "describe",
        lambda *args: {"version": "9.9.9", "title": "stale scenario.json title"},
    )
    monkeypatch.setattr(
        module.projects,
        "read_file",
        lambda *args, **kwargs: {
            "content": "name: test04_recipes\ntitle: Recipes\nversion: 0.1.0\n"
            if args[2] == "scenario.yaml"
            else ""
        },
    )
    monkeypatch.setattr(module.projects, "list_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *args: {
            "active_phase": "prototype",
            "automation": {"status": "not_started"},
            "publication": {"status": "not_started"},
            "capabilities": {},
        },
    )
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **kwargs: {
            "ok": True,
            "automation": {"status": "idle", "phase": "not_started", "version": "0.1.0"},
        },
    )
    monkeypatch.setattr(module.conversation, "list_development_changes", lambda **kwargs: [])

    lifecycle = module.get_lifecycle("scenario", "test04_recipes")

    assert len(lifecycle) == 1
    assert lifecycle[0]["version"] in lifecycle[0]["children"][0]["title"]
    assert len(lifecycle[0]["version"].split(".")) == 3
    assert all(part.isdigit() for part in lifecycle[0]["version"].split("."))
    assert lifecycle[0]["children"][0]["title"] == "v 0.1.0"
    assert lifecycle[0]["children"][0]["children"] == []
    assert lifecycle[0]["automationUpdatedAt"] is None
    assert lifecycle[0]["publicationUpdatedAt"] is None


def test_return_to_prototype_adaptation_does_not_replace_automation_head(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_project_descriptor", lambda *_args: {"version": "0.4.2", "title": "Recipes"})
    monkeypatch.setattr(module, "_context", lambda *_args: {})
    monkeypatch.setattr(module.projects, "list_files", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module.projects, "read_file", lambda *_args, **_kwargs: {"content": ""})
    monkeypatch.setattr(module.conversation, "list_development_changes", lambda **_kwargs: [])
    monkeypatch.setattr(
        module,
        "_workflow_projection",
        lambda *_args: {
            "active_phase": "prototype",
            "automation": {
                "status": "frozen",
                "head_task_id": "automation-real",
                "snapshot_task_id": "automation-real",
                "result_version": "0.4.2",
                "source_prototype_revision": "050",
            },
            "publication": {"status": "not_started"},
            "capabilities": {},
        },
    )
    monkeypatch.setattr(
        module,
        "get_automation",
        lambda *_args, **_kwargs: {
            "automation": {
                "status": "succeeded",
                "task_id": "adaptation-task",
                "current_task_id": "adaptation-task",
                "pending_workflow_transition": "return_to_prototype",
                "summary": "Derived Prototype UI 051",
            }
        },
    )

    lifecycle = module.get_lifecycle("scenario", "recipes")
    automation_nodes = lifecycle[0]["children"][0]["children"]

    assert len(automation_nodes) == 1
    assert automation_nodes[0]["task_id"] == "automation-real"
    version_parts = automation_nodes[0]["version"].split(".")
    assert len(version_parts) == 3
    assert all(part.isdigit() for part in version_parts)


def test_real_automation_task_does_not_borrow_prototype_version(monkeypatch) -> None:
    module = _module()
    projection = {"status": "running", "task_id": "task-real", "phase": "implementation"}

    child = module._automation_children(projection, project_version="0.1.0")[0]

    assert child["task_id"] == "task-real"
    assert child["version"] in child["previewLabel"]
    assert child["version"] != child["source_prototype_version"]
    assert child["source_prototype_version"] == "0.1.0"
    assert child["previewLabel"] == "active: DEV"


def test_preview_display_preserves_runtime_semantic_label(monkeypatch) -> None:
    module = _module()
    binding = {
        "ok": True,
        "runtime_scenario_id": "test04_recipes",
        "preview_target": {
            "stage": "automation",
            "revision": "task-internal-42",
            "label": "active: 0.4.2",
        },
    }
    monkeypatch.setattr(module.preview, "get_binding", lambda *_args: binding)
    monkeypatch.setattr(module.preview, "open_workspace", lambda *_args: {"url": "/preview"})

    preview = module.get_preview(webspace_id="desktop")

    assert preview["preview_target"]["label"] == "active: 0.4.2"
    assert preview["viewing"] == "active: 0.4.2"
    assert preview["viewing_revision"] == "task-internal-42"


def test_project_display_preserves_runtime_semantic_preview_label(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_project_descriptor", lambda *_args: {"title": "Recipes", "version": "0.4.2"})
    monkeypatch.setattr(module, "_context", lambda *_args: {})
    monkeypatch.setattr(
        module,
        "_workflow_projection",
        lambda *_args: {
            "active_phase": "automation",
            "automation": {"head_task_id": "task-internal-42", "result_version": "0.4.2"},
            "capabilities": {},
        },
    )
    monkeypatch.setattr(module, "_project_topic", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        module.preview,
        "get_binding",
        lambda *_args: {
            "preview_target": {
                "stage": "automation",
                "revision": "task-internal-42",
                "label": "active: 0.4.2",
            }
        },
    )

    project = module.get_project("scenario", "recipes", webspace_id="desktop")

    assert project["viewing_label"] == "active: 0.4.2"
    assert "task-internal-42" not in project["viewing_label"]


def test_transport_guard_accepts_russian_unchanged_and_rejects_lossy_text(monkeypatch) -> None:
    module = _module()
    request = "Добавить поиск — быстро, точно и без потерь?"
    issue = {"issue_id": "поиск", "title": "Поиск по рецептам!"}
    transitions = []
    evidence = []
    monkeypatch.setattr(
        module.workflow,
        "transition",
        lambda *args, **kwargs: transitions.append(kwargs["metadata"]) or {
            "workflow": {"change_set": kwargs["metadata"]}
        },
    )
    monkeypatch.setattr(
        module,
        "_sync_change_set_record",
        lambda **kwargs: evidence.append(kwargs["change_set"]) or {"ok": True},
    )
    monkeypatch.setattr(
        module.workflow,
        "build_context_packet",
        lambda *args, **kwargs: {
            "schema": "adaos.builder.context_packet.v1",
            "digest": "sha256:" + "b" * 64,
        },
    )

    result = module.plan_change_set(request, [issue], object_type="scenario", object_id="recipes")

    assert transitions[0]["request"] == request
    assert transitions[0]["issues"] == [issue]
    assert evidence[0]["request"] == request
    assert result["change_set"]["request"] == request
    for corrupt in ("Исправить \ufffd текст", "????????"):
        with pytest.raises(ValueError, match="transport integrity"):
            module.plan_change_set(corrupt, [issue], object_type="scenario", object_id="recipes")


def test_transport_guard_preserves_russian_launch_arguments(monkeypatch) -> None:
    module = _module()
    brief = "Реализовать карточки: имя, цена, описание — всё на русском."
    submitted = "Продолжай; проверь кавычки «ёлочки» и вопрос?"
    launched = []
    followups = []
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *_args: {"change_set": {"change_set_id": "change-1", "status": "planned"}},
    )
    monkeypatch.setattr(
        module.automation,
        "start",
        lambda **kwargs: launched.append(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(
        module.automation,
        "submit",
        lambda text, **kwargs: followups.append((text, kwargs)) or {"ok": True},
    )

    module.start_automation(brief, object_type="scenario", object_id="recipes")
    module.submit_automation(submitted, object_type="scenario", object_id="recipes")

    assert launched[0]["implementation_brief"] == brief
    assert followups[0][0] == submitted
    with pytest.raises(ValueError, match="transport integrity"):
        module.start_automation("Сломано ???", object_type="scenario", object_id="recipes")


def test_publication_release_children_exclude_dry_runs_and_are_bounded(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module.projects, "describe", lambda *args: {"version": "0.2.0"})
    monkeypatch.setattr(module.projects, "list_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **kwargs: {"ok": True, "automation": {"status": "idle"}},
    )
    changes = [
        {
            "change_id": "dry",
            "status": "accepted",
            "source_refs": {"action": "publication"},
            "meta": {"dry_run": True, "version": "0.2.1"},
        },
        *[
            {
                "change_id": f"release-{index}",
                "status": "accepted",
                "summary": f"Published v0.2.{index}",
                "updated_at": f"2026-07-23T10:0{index}:00+00:00",
                "source_refs": {"action": "publication"},
                "meta": {"dry_run": False, "version": f"0.2.{index}"},
            }
            for index in range(8)
        ],
        {"change_id": "checkpoint", "source_refs": {"action": "checkpoint"}},
    ]
    monkeypatch.setattr(module.conversation, "list_development_changes", lambda **kwargs: changes)

    lifecycle = module.get_lifecycle("skill", "builder")
    prototype = lifecycle[0]
    automation_nodes = prototype["children"][0]["children"]
    releases = [release for automation_node in automation_nodes for release in automation_node["children"]]

    assert len(lifecycle) == 1
    assert prototype["publicationStatus"] == "published"
    assert len(releases) == module.MAX_LIFECYCLE_CHILDREN
    assert all(child["kind"] == "publication_release" for child in releases)
    assert all(child["title"].startswith("v ") for child in releases)
    assert all(child["updated_at"] == child["created_at"] for child in releases)
    assert {child["change_id"] for child in releases} == {
        "release-0", "release-1", "release-2", "release-3", "release-4"
    }
    assert all(child["updated_at"].endswith("Z") for child in releases)


def test_lifecycle_uses_one_stage_contract_and_timestamp_format(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module.projects, "describe", lambda *args: {"version": "0.2.0"})
    monkeypatch.setattr(
        module.projects,
        "list_files",
        lambda *args, **kwargs: [
            {"path": "skill.yaml", "updated_at": 1784790000},
        ],
    )
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **kwargs: {"ok": True, "automation": {"status": "idle", "updated_at": 1784790001}},
    )
    monkeypatch.setattr(module.conversation, "list_development_changes", lambda **kwargs: [])
    monkeypatch.setattr(module, "_context", lambda *args: {"updated_at": 1784790002})

    lifecycle = module.get_lifecycle("skill", "builder")

    assert len(lifecycle) == 1
    assert lifecycle[0]["lifecycleStage"] == "prototype"
    assert lifecycle[0]["conversationLabel"] == "Prototype conversation"
    assert lifecycle[0]["dependentStages"] == ["prototype", "automation", "publication"]
    assert str(lifecycle[0]["updated_at"]).endswith("Z")
    assert lifecycle[0]["automationUpdatedAt"] is None
    assert lifecycle[0]["publicationUpdatedAt"] is None


def test_lifecycle_nests_automation_and_publication_under_source_prototype(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module.projects, "describe", lambda *args: {"version": "0.3.0"})
    monkeypatch.setattr(
        module.projects,
        "list_files",
        lambda *args, **kwargs: [
            {"path": "ui_revisions/037.json", "updated_at": 1784790000},
            {"path": "ui_revisions/038.json", "updated_at": 1784790100},
        ],
    )
    monkeypatch.setattr(
        module.projects,
        "read_file",
        lambda *args, **kwargs: {"content": "038"},
    )
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *args: {
            "active_phase": "automation",
            "prototype": {"status": "frozen", "head_revision": "037"},
            "automation": {
                "status": "completed",
                "head_task_id": "task-37",
                "snapshot_task_id": "task-37",
                "source_prototype_revision": "037",
                "result_version": "0.3.0",
            },
            "publication": {"status": "published", "current_version": "0.3.1"},
            "capabilities": {
                "can_preview_prototype": True,
                "can_preview_automation": True,
                "can_preview_publication": True,
            },
        },
    )
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **kwargs: {
            "ok": True,
            "automation": {
                "status": "completed",
                "task_id": "task-37",
                "version": "0.9.9",
                "source_prototype_version": "037",
            },
        },
    )
    monkeypatch.setattr(
        module.conversation,
        "list_development_changes",
        lambda **kwargs: [
            {
                "change_id": "publication-031",
                "status": "published",
                "updated_at": "2026-07-28T10:00:00Z",
                "source_refs": {"action": "publication"},
                "meta": {
                    "version": "0.3.1",
                    "source_automation_task": "task-37",
                    "source_automation_version": "0.3.0",
                    "source_prototype_revision": "037",
                },
            }
        ],
    )

    lifecycle = module.get_lifecycle("scenario", "builder")
    revisions = {item["revision"]: item for item in lifecycle[0]["children"]}
    automation_node = revisions["037"]["children"][0]
    publication_node = automation_node["children"][0]

    assert revisions["038"]["children"] == []
    assert automation_node["kind"] == "automation_result"
    assert automation_node["title"] == "v 0.3.0"
    assert all(part.isdigit() for part in automation_node["version"].split("."))
    assert automation_node["title"] == f"v {automation_node['version']}"
    assert automation_node["previewLabel"] == "active: 0.3.0"
    assert automation_node["canPreview"] is True
    assert publication_node["kind"] == "publication_release"
    assert publication_node["title"] == "v 0.3.1"
    assert publication_node["previewLabel"] == "public: 0.3.1"
    assert publication_node["canPreview"] is True


def test_lifecycle_does_not_attach_unproven_old_publication_to_current_automation(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module.projects, "describe", lambda *args: {"version": "0.4.0"})
    monkeypatch.setattr(module.projects, "list_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *args: {
            "active_phase": "automation",
            "prototype": {"status": "frozen"},
            "automation": {
                "status": "completed",
                "head_task_id": "task-current",
                "source_prototype_revision": "0.4.0",
                "result_version": "0.4.0",
            },
            "publication": {"status": "published", "current_version": "0.4.0"},
            "capabilities": {
                "can_preview_automation": True,
                "can_preview_publication": True,
            },
        },
    )
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **kwargs: {
            "ok": True,
            "automation": {"status": "completed", "task_id": "task-current", "version": "0.4.1"},
        },
    )
    monkeypatch.setattr(
        module.conversation,
        "list_development_changes",
        lambda **kwargs: [
            {
                "change_id": "publication-current",
                "status": "published",
                "source_refs": {"action": "publication"},
                "meta": {"version": "0.4.0"},
            },
            {
                "change_id": "publication-old",
                "status": "published",
                "source_refs": {"action": "publication"},
                "meta": {"version": "0.3.0"},
            },
        ],
    )

    lifecycle = module.get_lifecycle("skill", "builder")
    automation_nodes = lifecycle[0]["children"][0]["children"]
    current = next(
        item for item in automation_nodes if item["children"][0]["change_id"] == "publication-current"
    )
    historical = next(
        item for item in automation_nodes if item["children"][0]["change_id"] == "publication-old"
    )

    assert [item["version"] for item in current["children"]] == ["0.4.0"]
    assert [item["version"] for item in historical["children"]] == ["0.3.0"]
    assert historical["canPreview"] is False


def test_lifecycle_treats_explicit_historical_publication_lineage_as_proven(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module.projects, "describe", lambda *args: {"version": "0.4.0"})
    monkeypatch.setattr(module.projects, "list_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *args: {
            "active_phase": "automation",
            "automation": {
                "status": "completed",
                "head_task_id": "task-current",
                "source_prototype_revision": "0.4.0",
                "result_version": "0.4.0",
            },
            "publication": {"status": "published", "current_version": "0.4.0"},
            "capabilities": {},
        },
    )
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **kwargs: {
            "ok": True,
            "automation": {"status": "completed", "task_id": "task-current", "version": "0.4.0"},
        },
    )
    monkeypatch.setattr(
        module.conversation,
        "list_development_changes",
        lambda **kwargs: [
            {
                "change_id": "publication-historical",
                "status": "published",
                "source_refs": {"action": "publication"},
                "meta": {
                    "version": "0.3.1",
                    "source_automation_task": "task-historical",
                    "source_automation_version": "0.3.0",
                    "source_prototype_revision": "0.2.9",
                },
            },
            {
                "change_id": "publication-legacy",
                "status": "published",
                "source_refs": {"action": "publication"},
                "meta": {"version": "0.2.0"},
            },
        ],
    )

    lifecycle = module.get_lifecycle("skill", "builder")
    automation_nodes = lifecycle[0]["children"][0]["children"]
    proven = next(item for item in automation_nodes if item.get("task_id") == "task-historical")
    inferred = next(
        item
        for item in automation_nodes
        if item.get("lineageWarning") == "publication_source_metadata_missing"
    )

    assert all(part.isdigit() for part in proven["version"].split("."))
    assert proven["title"] == f"v {proven['version']}"
    assert proven["previewLabel"] == f"active: {proven['version']}"
    assert proven["source_prototype_version"] == "0.2.9"
    assert "lineageInferred" not in proven
    assert proven.get("lineageWarning") != "publication_source_metadata_missing"
    assert "lineageInferred" not in proven["children"][0]
    assert inferred["lineageInferred"] is True
    assert inferred["children"][0]["lineageInferred"] is True


def test_publish_records_only_successful_non_dry_run_releases(monkeypatch) -> None:
    module = _module()
    promoted_results = iter(
        [
            {"ok": True, "version": "0.2.1", "release": "builder@0.2.1"},
            {"ok": False, "dry_run": False, "error": "validation failed"},
        ]
    )
    monkeypatch.setattr(
        module.projects,
        "prepare_candidate",
        lambda *args, **kwargs: {
            "ok": True,
            "candidate": {
                "candidate_id": "candidate-21",
                "release_digest": "sha256:" + "1" * 64,
                "package_digest": "sha256:" + "2" * 64,
                "base_release": "builder@0.2.0",
            },
            "release": {
                "project_id": "builder",
                "version": "0.2.1",
                "release_digest": "sha256:" + "1" * 64,
            },
            "trial_workspace": "trials/candidate-21/workspace",
        },
    )
    monkeypatch.setattr(
        module.projects,
        "decide_candidate",
        lambda *args, **kwargs: {"ok": True, "candidate": {"trials": []}},
    )
    monkeypatch.setattr(
        module.projects,
        "promote_candidate",
        lambda *args, **kwargs: next(promoted_results),
    )
    recorded: list[dict] = []
    monkeypatch.setattr(
        module,
        "_record_project_change",
        lambda **kwargs: recorded.append(kwargs) or {"change_id": "publication-change", "status": "accepted"},
    )
    workflow_states = iter(
        [
            {
                "capabilities": {"can_prepare_candidate": True},
                "automation": {"head_task_id": "task.21"},
                "delivery": {
                    "status": "checkpoint",
                    "checkpoint_change_id": "checkpoint-change",
                    "package_digest": "sha256:" + "2" * 64,
                    "source_revision": "a" * 40,
                },
            },
            {
                "capabilities": {"can_decide_candidate": True, "can_publish": False},
                "automation": {"head_task_id": "task.21"},
                "delivery": {
                    "status": "trial",
                    "candidate_id": "candidate-21",
                    "package_digest": "sha256:" + "2" * 64,
                },
            },
            {
                "capabilities": {"can_decide_candidate": False, "can_publish": True},
                "automation": {"head_task_id": "task.21"},
                "delivery": {
                    "status": "accepted",
                    "candidate_id": "candidate-21",
                    "package_digest": "sha256:" + "2" * 64,
                },
            },
        ]
    )
    monkeypatch.setattr(module.workflow, "get_state", lambda *args: next(workflow_states))
    transitions: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        module.workflow,
        "transition",
        lambda *args, **kwargs: transitions.append((args, kwargs))
        or {"ok": True, "workflow": {"publication": {"status": "published"}}},
    )

    dry_run = module.publish_project(dry_run=True, confirmed=True)
    published = module.publish_project(dry_run=False, confirmed=True, webspace_id="desktop")
    failed = module.publish_project(dry_run=False, confirmed=True)

    assert dry_run["trial_ready"] is True
    assert published["change_id"] == "publication-change"
    assert "change_id" not in failed
    assert len(recorded) == 1
    assert recorded[0]["action"] == "publication"
    assert recorded[0]["meta"] == {
        "dry_run": False,
        "version": "0.2.1",
            "release": "builder@0.2.1",
            "bump": "patch",
            "source_automation_task": "task.21",
        }
    by_action = {args[2]: kwargs for args, kwargs in transitions}
    assert by_action["candidate_accepted"]["metadata"]["candidate_digest"] == "sha256:" + "2" * 64
    assert by_action["publish"]["metadata"]["candidate_digest"] == "sha256:" + "2" * 64


@pytest.mark.parametrize("compatibility_mismatch", [False, True])
@pytest.mark.parametrize("legacy_missing_version", [False, True])
def test_publish_recovers_exact_running_trial_without_repeating_activation(
    monkeypatch,
    compatibility_mismatch: bool,
    legacy_missing_version: bool,
) -> None:
    module = _module()
    package_digest = "sha256:" + "2" * 64
    source_revision = "a" * 40
    candidate_id = "builder-0-2-1-" + package_digest[-12:]
    checkpoint = {
        "capabilities": {"can_prepare_candidate": not compatibility_mismatch},
        "change_set": {"change_set_id": "change-1", "member_change_ids": ["change-1"]},
        "automation": {"head_task_id": "task.21"},
        "delivery": {
            "status": "activating" if compatibility_mismatch else "checkpoint",
            "checkpoint_change_id": "checkpoint-change",
            "package_digest": package_digest,
            "source_revision": source_revision,
            **({} if legacy_missing_version else {"version": "0.2.1"}),
        },
        "governed": {"state": "trial_ready"},
    }
    waiting = {
        **checkpoint,
        "delivery": {**checkpoint["delivery"], "status": "activating"},
        "governed": {"state": "trial_waiting"},
    }
    states = iter([checkpoint, checkpoint if compatibility_mismatch else waiting])
    monkeypatch.setattr(module.workflow, "get_state", lambda *args: next(states))
    monkeypatch.setattr(
        module.projects,
        "describe",
        lambda *args: {"version": "0.2.1"},
    )
    transitions: list[str] = []
    monkeypatch.setattr(
        module.workflow,
        "transition",
        lambda *args, **kwargs: transitions.append(args[2])
        or {"ok": True, "workflow": checkpoint},
    )
    monkeypatch.setattr(
        module.projects,
        "get_candidate",
        lambda requested: {
            "ok": True,
            "candidate": {
                "candidate_id": requested,
                "project_id": "builder",
                "version": "0.2.1",
                "package_digest": package_digest,
                "release_digest": "sha256:" + "1" * 64,
                "source_ref": {"revision": source_revision},
                "status": "trial",
                "trials": [{"trial_id": "trial-1", "status": "running"}],
            },
            "trial_workspace": "trials/candidate-21/workspace",
        },
    )
    monkeypatch.setattr(
        module.projects,
        "prepare_candidate",
        lambda *args, **kwargs: pytest.fail("external Trial activation must not repeat"),
    )

    result = module.publish_project("scenario", "builder", dry_run=True, confirmed=True)

    assert result["trial_ready"] is True
    assert result["recovered"] is True
    assert result["candidate"]["candidate_id"] == candidate_id
    assert transitions == [
        "candidate_preparation_started",
        *(["candidate_preparation_started"] if compatibility_mismatch else []),
        "candidate_prepared",
    ]


def test_trial_result_reconciles_lost_local_waiting_state_without_external_activation(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *args: {
            "delivery": {"status": "checkpoint"},
            "governed": {"state": "trial_ready"},
        },
    )
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        module.workflow,
        "transition",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )

    module._ensure_trial_waiting_before_result(
        "scenario",
        "builder",
        admitted_workflow={"governed": {"state": "trial_waiting"}},
        run_id="candidate:builder:activate",
        canonical_change_id="change-1",
        context_packet_digest="sha256:" + "3" * 64,
        package_digest="sha256:" + "2" * 64,
    )

    assert [args[2] for args, _kwargs in calls] == ["candidate_preparation_started"]
    assert calls[0][1]["metadata"]["reconciliation"] == "external_trial_result_observed"


def test_trial_result_repairs_legacy_half_applied_waiting_state(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *args: {
            "delivery": {"status": "activating"},
            "governed": {"state": "trial_ready"},
        },
    )
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        module.workflow,
        "transition",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )

    module._ensure_trial_waiting_before_result(
        "scenario",
        "builder",
        admitted_workflow={"governed": {"state": "trial_ready"}},
        run_id="candidate:builder:activate",
        canonical_change_id="change-1",
        context_packet_digest="sha256:" + "3" * 64,
        package_digest="sha256:" + "2" * 64,
    )

    assert [args[2] for args, _kwargs in calls] == ["candidate_preparation_started"]
    assert calls[0][1]["metadata"]["idempotency_key"].endswith(":waiting-reconcile")
    assert calls[0][1]["metadata"]["reconciliation"] == "external_trial_result_observed"


def test_publication_result_reconciles_lost_local_waiting_state_without_repromotion(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *args: {
            "delivery": {"status": "accepted"},
            "governed": {"state": "publication_ready"},
        },
    )
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        module.workflow,
        "transition",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )

    module._ensure_publication_waiting_before_result(
        "scenario",
        "builder",
        admitted_workflow={"governed": {"state": "publication_waiting"}},
        run_id="candidate:candidate-21:publish",
        candidate_id="candidate-21",
        canonical_change_id="change-1",
        context_packet_digest="sha256:" + "3" * 64,
    )

    assert [args[2] for args, _kwargs in calls] == ["publication_started"]
    assert calls[0][1]["metadata"]["reconciliation"] == "external_publication_result_observed"


def test_publication_attempt_identity_advances_after_reconciliation(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *args: {
            "generation": 31,
            "governed": {"state": "publication_ready", "generation": 17},
            "capabilities": {"can_publish": True},
            "automation": {"head_task_id": "task.31"},
            "change_set": {"change_set_id": "change-31"},
            "delivery": {
                "status": "accepted",
                "candidate_id": "candidate-31",
                "package_digest": "sha256:" + "2" * 64,
            },
        },
    )
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        module.workflow,
        "transition",
        lambda *args, **kwargs: calls.append((args, kwargs))
        or {
            "ok": True,
            "workflow": {
                "delivery": {"status": "publication_waiting"},
                "governed": {"state": "publication_waiting"},
            },
        },
    )
    monkeypatch.setattr(
        module.projects,
        "promote_candidate",
        lambda *_args, **_kwargs: {
            "ok": False,
            "status": "failed",
            "error": "bounded-test-failure",
        },
    )

    module.publish_project(dry_run=False, confirmed=True)

    started = next(item for item in calls if item[0][2] == "publication_started")
    metadata = started[1]["metadata"]
    assert metadata["run_id"] == "candidate:candidate-31:publish:g17"
    assert metadata["idempotency_key"] == "candidate:candidate-31:publish:g17:start"


def test_publication_can_finalize_a_partial_local_wait_from_completed_external_result(
    monkeypatch,
) -> None:
    module = _module()
    state = {
        "generation": 32,
        "governed": {"state": "publication_ready", "generation": 18},
        "capabilities": {"can_publish": False},
        "automation": {"head_task_id": "task.32", "result_version": "0.1.2"},
        "change_set": {"change_set_id": "change-32"},
        "delivery": {
            "status": "publication_waiting",
            "candidate_id": "candidate-32",
            "package_digest": "sha256:" + "2" * 64,
        },
    }
    current_state = dict(state)
    monkeypatch.setattr(module.workflow, "get_state", lambda *args: current_state)
    transitions: list[str] = []

    def _transition(*args, **_kwargs):
        transitions.append(args[2])
        if args[2] == "publication_started":
            current_state["governed"] = {
                "state": "publication_waiting",
                "generation": 19,
            }
        return {
            "ok": True,
            "workflow": {
                **current_state,
            },
        }

    monkeypatch.setattr(module.workflow, "transition", _transition)
    monkeypatch.setattr(
        module.projects,
        "promote_candidate",
        lambda *_args, **_kwargs: {
            "ok": True,
            "candidate_id": "candidate-32",
            "version": "0.1.2",
            "release": "workflow_lab_dashboard@0.1.2",
            "package_digest": "sha256:" + "2" * 64,
            "apply_evidence": {
                "approval": {"actor_id": "builder.user"},
                "activation": {"operation_id": "activation-32"},
                "rollback": {"mode": "workspace_lock_restore"},
            },
        },
    )
    monkeypatch.setattr(
        module,
        "_record_project_change",
        lambda **_kwargs: {"change_id": "publication-change-32"},
    )
    monkeypatch.setattr(module, "_sync_change_set_record", lambda **_kwargs: None)

    result = module.publish_project(dry_run=False, confirmed=True)

    assert result["ok"] is True
    assert transitions == ["publication_started", "publish"]


def test_publish_does_not_bypass_non_promotable_candidate_state(monkeypatch) -> None:
    module = _module()
    promotion_results = iter(
        [
            {"ok": True, "status": "rejected", "candidate_id": "candidate-21"},
            {"ok": True, "status": "stale", "candidate_id": "candidate-21"},
        ]
    )
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *args: {
            "capabilities": {"can_publish": True},
            "automation": {"head_task_id": "task.21"},
            "delivery": {"status": "accepted", "candidate_id": "candidate-21"},
        },
    )
    transitions: list[str] = []
    monkeypatch.setattr(
        module.workflow,
        "transition",
        lambda *args, **kwargs: transitions.append(args[2]) or {"workflow": {}},
    )
    monkeypatch.setattr(
        module.projects,
        "promote_candidate",
        lambda *args, **kwargs: next(promotion_results),
    )
    recorded: list[dict] = []
    monkeypatch.setattr(
        module,
        "_record_project_change",
        lambda **kwargs: recorded.append(kwargs) or {"change_id": "publication-change"},
    )

    rejected = module.publish_project(dry_run=False, confirmed=True)
    stale = module.publish_project(dry_run=False, confirmed=True)

    assert rejected["ok"] is False
    assert rejected["status"] == "rejected"
    assert "change_id" not in rejected
    assert stale["status"] == "stale"
    assert stale["requires_reapply"] is True
    assert "change_id" not in stale
    assert transitions == [
        "publication_started",
        "publication_failed",
        "publication_started",
        "publication_failed",
        "candidate_stale",
    ]
    assert recorded == []


def test_project_collections_are_browser_ready(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.projects,
        "list_projects",
        lambda **kwargs: [
            {
                "kind": "scenario",
                "id": "builder",
                "title": "Builder",
                "description": "Workbench",
                "version": "0.2.0",
            },
            {"kind": "skill", "id": ".runtime", "title": ".runtime"},
        ],
    )
    monkeypatch.setattr(
        module.projects,
        "list_files",
        lambda *args, **kwargs: [
            {"path": "scenario.yaml", "editable": True, "size_bytes": 100},
            {"path": "ui_revisions/001.json", "editable": False, "size_bytes": 200},
            {"path": "tests/__pycache__/test_builder.pyc", "editable": False, "size_bytes": 300},
        ],
    )
    monkeypatch.setattr(
        module.projects,
        "read_file",
        lambda *args, **kwargs: {
            "content": "name: builder\ntitle: Builder\ndescription: Workbench\nversion: 0.2.0\n"
        },
    )

    project = module.list_projects()[0]
    files = module.list_project_files("scenario", "builder")

    assert project["id"] == "scenario:builder"
    assert project["object_id"] == "builder"
    assert project["subtitle"] == "Workbench"
    assert project["type_i18n"] == {"key": "builder.project_type.scenario"}
    assert project["stage_i18n"] == {"key": "builder.project_stage.prototype"}
    assert project["sync_i18n"] == {"key": "builder.project_sync.available_dev"}
    assert len(module.list_projects()) == 1
    assert files[0]["title"] == "scenario.yaml"
    assert files[0]["protected"] is False
    assert files[1]["protected"] is True
    assert len(files) == 2


def test_project_collection_hides_archived_projects_by_default(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.projects,
        "list_projects",
        lambda **kwargs: [
            {"kind": "scenario", "id": "active", "version": "1.0.0"},
            {"kind": "scenario", "id": "archived", "version": "0.9.0"},
        ],
    )
    monkeypatch.setattr(
        module,
        "_context",
        lambda _kind, project_id: {"archived": project_id == "archived"},
    )

    assert [item["object_id"] for item in module.list_projects()] == ["active"]
    assert [item["object_id"] for item in module.list_projects(include_archived=True)] == [
        "active",
        "archived",
    ]


def test_preview_selection_waits_until_materialized(monkeypatch) -> None:
    module = _module()
    captured: dict = {}

    def _select(*args, **kwargs):
        captured.update({"args": args, **kwargs})
        return {"ok": True, "selected": True}

    monkeypatch.setattr(module.preview, "select_target", _select)
    monkeypatch.setattr(
        module.preview,
        "canonical_source_webspace_id",
        lambda source: source.removesuffix("-dev"),
    )
    monkeypatch.setattr(module.workflow, "get_state", lambda *args: {"generation": 4})
    context_updates: list[dict] = []
    monkeypatch.setattr(
        module.workflow,
        "update_interaction_context",
        lambda *args, **kwargs: context_updates.append(
            {"args": args, "kwargs": kwargs}
        )
        or {"workflow": {"interaction": args[2]}},
    )

    result = module.select_preview("scenario", "builder", webspace_id="builder-smoke-dev")

    assert result["selected"] is True
    assert captured["args"] == ("scenario", "builder")
    assert captured["source_webspace_id"] == "builder-smoke"
    assert captured["stage"] == "prototype"
    assert captured["follow_active"] is True
    assert result["interaction_updated"] is True
    assert context_updates[0]["args"][2] == {"preview_target": "prototype:builder:current"}
    assert context_updates[0]["kwargs"]["expected_generation"] == 4


@pytest.mark.parametrize("tool_name", ["select_preview", "select_preview_target"])
def test_scenario_preview_selection_preserves_atomic_sdk_event_contract(monkeypatch, tool_name: str) -> None:
    module = _module()
    events: list[tuple[str, dict]] = []
    expected = {
        "ok": True,
        "target": {"stage": "automation", "revision": "task.accepted"},
        "binding": {
            "selection": {
                "object_type": "scenario",
                "object_id": "recipes",
                "title": "Рецепты",
                "description": "Русское описание — без потерь",
            }
        },
    }

    def _select_target(*args, **kwargs):
        selection = expected["binding"]["selection"]
        events.append(
            (
                "builder.context.selected",
                {
                    "source_webspace_id": kwargs["source_webspace_id"],
                    "project_kind": selection["object_type"],
                    "project_id": selection["object_id"],
                    "object_type": selection["object_type"],
                    "object_id": selection["object_id"],
                    "title": selection["title"],
                    "description": selection["description"],
                },
            )
        )
        return expected

    monkeypatch.setattr(module.preview, "select_target", _select_target)
    monkeypatch.setattr(module.preview, "canonical_source_webspace_id", lambda source: source.removesuffix("-dev"))

    if tool_name == "select_preview":
        result = module.select_preview("scenario", "recipes", webspace_id="builder-host-dev")
    else:
        result = module.select_preview_target(
            "automation",
            "task.accepted",
            object_type="scenario",
            object_id="recipes",
            webspace_id="builder-host-dev",
        )

    assert result is expected
    assert [topic for topic, _payload in events] == ["builder.context.selected"]
    assert events[0][1] == {
        "source_webspace_id": "builder-host",
        "project_kind": "scenario",
        "project_id": "recipes",
        "object_type": "scenario",
        "object_id": "recipes",
        "title": "Рецепты",
        "description": "Русское описание — без потерь",
    }
    assert all(topic != "builder.preview.desired" for topic, _payload in events)


def test_create_project_selects_preview_and_returns_durable_conversation(monkeypatch) -> None:
    module = _module()
    selections: list[dict] = []
    monkeypatch.setattr(
        module.projects,
        "create",
        lambda *args, **kwargs: {
            "ok": True,
            "title": "Recipes",
            "description": "Recipe workspace",
        },
    )
    monkeypatch.setattr(
        module.preview,
        "select_target",
        lambda *args, **kwargs: selections.append({"args": args, **kwargs}) or {"ok": True},
    )
    monkeypatch.setattr(module.preview, "canonical_source_webspace_id", lambda source: "dev1")
    monkeypatch.setattr(module.preview, "dev_webspace_id", lambda source: f"{source}-dev")
    monkeypatch.setattr(
        module.conversation,
        "ensure_builder_topic",
        lambda **kwargs: {
            "conversation_id": "conv.skill.builder_skill.default",
            "topic_id": "prompt-project:scenario:recipes",
            "thread_id": "prompt-project:scenario:recipes",
        },
    )

    result = module.create_project(
        "scenario",
        "recipes",
        template="default",
        webspace_id="dev1-dev",
    )

    assert result["preview_selected"] is True
    assert result["conversation_id"] == "conv.skill.builder_skill.default"
    assert selections == [
        {
            "args": ("scenario", "recipes"),
            "stage": "prototype",
            "source_webspace_id": "dev1",
            "follow_active": True,
        }
    ]


def test_create_project_from_builder_preview_keeps_current_webspace_as_host(monkeypatch) -> None:
    module = _module()
    selections: list[dict] = []
    source_calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        module.projects,
        "create",
        lambda *args, **kwargs: {"ok": True, "title": "Recipes"},
    )
    monkeypatch.setattr(
        module.preview,
        "action_source_webspace_id",
        lambda source, current_scenario_id=None: source_calls.append(
            (source, current_scenario_id)
        )
        or source,
    )
    monkeypatch.setattr(
        module.preview,
        "select_target",
        lambda *args, **kwargs: selections.append({"args": args, **kwargs})
        or {"ok": True},
    )
    monkeypatch.setattr(module.preview, "dev_webspace_id", lambda source: f"{source}-dev")
    monkeypatch.setattr(
        module.conversation,
        "ensure_builder_topic",
        lambda **kwargs: {
            "conversation_id": "conv.skill.builder_skill.default",
            "topic_id": "prompt-project:scenario:test05_recipes",
            "thread_id": "prompt-project:scenario:test05_recipes",
        },
    )

    result = module.create_project(
        "scenario",
        "test05_recipes",
        template="scenario_default",
        _meta={"webspace_id": "dev1-dev", "scenario_id": "builder"},
    )

    assert result["preview_selected"] is True
    assert source_calls == [
        ("dev1-dev", "builder"),
        ("dev1-dev", "builder"),
    ]
    assert selections == [
        {
            "args": ("scenario", "test05_recipes"),
            "stage": "prototype",
            "source_webspace_id": "dev1-dev",
            "follow_active": True,
        }
    ]


def test_preview_state_exposes_open_and_qr_targets(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module.preview, "canonical_source_webspace_id", lambda source: "dev1")
    monkeypatch.setattr(
        module.preview,
        "get_binding",
        lambda source: {"ok": True, "runtime_scenario_id": "builder", "dev_webspace_id": "dev1-dev"},
    )
    monkeypatch.setattr(module.preview, "open_workspace", lambda source: {"url": "/?webspace=dev1-dev"})

    result = module.get_preview(webspace_id="dev1-dev")

    assert result["source_webspace_id"] == "dev1"
    assert result["preview_url"] == "/?webspace=dev1-dev"
    assert result["qr_text"] == "/?webspace=dev1-dev"
    assert result["status"] == "ready"


def test_project_lifecycle_tools_stay_behind_sdk(monkeypatch) -> None:
    module = _module()
    calls: list[tuple[str, tuple, dict]] = []
    transitions: list[dict] = []
    run_calls: list[dict] = []
    packet_digest = "sha256:" + "9" * 64
    monkeypatch.setattr(
        module.projects,
        "create",
        lambda *args, **kwargs: calls.append(("create", args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        module.projects,
        "push",
        lambda *args, **kwargs: calls.append(("push", args, kwargs))
        or {
            "ok": True,
            "commit": "a" * 40,
            "source_revision": "a" * 40,
            "package_digest": "sha256:" + "2" * 64,
            "version": "0.2.1",
        },
    )
    monkeypatch.setattr(
        module.projects,
        "prepare_candidate",
        lambda *args, **kwargs: calls.append(("prepare_candidate", args, kwargs))
        or {
            "ok": True,
            "candidate": {
                "candidate_id": "candidate-1",
                "release_digest": "sha256:" + "3" * 64,
                "package_digest": "sha256:" + "2" * 64,
            },
            "release": {
                "project_id": "builder",
                "version": "0.2.1",
                "release_digest": "sha256:" + "3" * 64,
            },
            "trial_workspace": "trials/candidate-1/workspace",
        },
    )
    monkeypatch.setattr(module.preview, "dev_webspace_id", lambda source: f"{source}-dev")
    monkeypatch.setattr(
        module.conversation,
        "ensure_builder_topic",
        lambda **kwargs: {"conversation_id": "conv", "topic_id": "topic"},
    )
    captured_change: dict = {}

    def _upsert_change(**kwargs):
        captured_change.update(kwargs)
        return {"change_id": kwargs["change_id"], "status": kwargs["status"]}

    monkeypatch.setattr(module.conversation, "upsert_development_change", _upsert_change)
    monkeypatch.setattr(
        module.conversation,
        "get_development_change",
        lambda change_id: {"change_id": change_id},
    )
    monkeypatch.setattr(
        module.conversation,
        "upsert_development_run",
        lambda **kwargs: run_calls.append(kwargs) or {"run_id": kwargs["run_id"], "change_id": kwargs["change_id"]},
    )
    monkeypatch.setattr(
        module.workflow,
        "transition",
        lambda *args, **kwargs: transitions.append(kwargs.get("metadata") or {})
        or {"ok": True, "workflow": {"delivery": {"status": args[2]}}},
    )
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *args: {
            "capabilities": {"can_prepare_candidate": True},
            "change": {"change_id": "CH-builder", "context_packet_digest": packet_digest},
            "change_set": {
                "change_set_id": "CH-builder",
                "member_change_ids": ["CH-builder", captured_change.get("change_id")],
            },
            "context_packet": {"digest": packet_digest},
            "automation": {"head_task_id": "task-1"},
            "delivery": {
                "status": "checkpoint",
                "checkpoint_change_id": captured_change.get("change_id"),
                "package_digest": "sha256:" + "2" * 64,
                "source_revision": "a" * 40,
            },
        },
    )
    monkeypatch.setattr(
        module,
        "get_automation",
        lambda *args, **kwargs: {
            "automation": {
                "status": "completed",
                "task_id": "task-1",
                "project": {"companion_skill_id": "builder_skill"},
            }
        },
    )

    module.create_project("skill", "demo_skill", template="default")
    pushed = module.push_project(
        "scenario",
        "builder",
        message="checkpoint",
        checkpoint_id="checkpoint-change",
        confirmed=True,
        webspace_id="desktop",
    )
    result = module.publish_project("scenario", "builder", dry_run=True, confirmed=True)

    assert calls[0] == ("create", ("skill", "demo_skill"), {"template": "skill_default"})
    assert calls[1][0:2] == ("push", ("skill", "builder_skill"))
    assert calls[2][0:2] == ("push", ("scenario", "builder"))
    assert calls[1][2]["message"] == calls[2][2]["message"] == "checkpoint"
    assert calls[1][2]["metadata"] == calls[2][2]["metadata"]
    assert calls[1][2]["metadata"]["change_id"] == captured_change["change_id"]
    assert calls[1][2]["metadata"]["canonical_change_id"] == "CH-builder"
    assert calls[1][2]["metadata"]["context_packet_digest"] == packet_digest
    assert pushed["change_id"] == "checkpoint-change"
    assert calls[3][0] == "prepare_candidate"
    assert calls[3][2]["change_ids"] == ["CH-builder", captured_change["change_id"]]
    assert calls[3][2]["validation_evidence"]["canonical_change_id"] == "CH-builder"
    assert calls[3][2]["validation_evidence"]["context_packet_digest"] == packet_digest
    assert pushed["evidence"]["status"] == "pushed"
    assert pushed["evidence"]["run_synced"] is True
    assert run_calls[0]["change_id"] == "CH-builder"
    assert transitions[0]["run_id"] == "checkpoint-change"
    assert transitions[0]["context_packet_digest"] == packet_digest
    assert transitions[0]["version"] == "0.2.1"
    assert transitions[1]["run_id"] == "candidate:builder:activate"
    assert transitions[2]["run_id"] == "candidate:candidate-1:prepare"
    assert len(pushed["checkpoint_artifacts"]) == 2
    assert captured_change["artifact_refs"] == [{"kind": "scenario", "id": "builder"}]
    assert result["trial_ready"] is True


def test_prompt_ide_compatibility_projections_are_browser_ready(monkeypatch) -> None:
    module = _module()

    def _describe(kind, object_id):
        return {
            "kind": kind,
            "id": object_id,
            "title": object_id.replace("_", " ").title(),
            "description": "DEV project",
            "version": "0.2.2",
            "depends": ["builder_skill"] if kind == "scenario" else [],
        }

    monkeypatch.setattr(module.projects, "describe", _describe)
    monkeypatch.setattr(
        module.projects,
        "list_files",
        lambda *args, **kwargs: [
            {"path": "builder_memory.md", "editable": True, "size_bytes": 20},
            {"path": "ui_revisions/029.json", "editable": False, "size_bytes": 200},
            {"path": "ui_revisions/030.json", "editable": False, "size_bytes": 300},
            {"path": "ui_revisions/current.txt", "editable": False, "size_bytes": 3},
        ],
    )
    monkeypatch.setattr(
        module.projects,
        "read_file",
        lambda *args, **kwargs: {"ok": True, "content": "030"},
    )
    monkeypatch.setattr(
        module.prompt_context,
        "get",
        lambda *args, **kwargs: {"workflow_state": "prototype", "archived": False},
    )
    monkeypatch.setattr(
        module.automation,
        "get_state",
        lambda **kwargs: {"ok": True, "automation": {"status": "idle"}},
    )

    objects = module.list_project_objects("scenario", "builder")
    tree = module.list_project_file_tree("scenario", "builder")
    lifecycle = module.get_lifecycle("scenario", "builder", webspace_id="desktop")

    assert [item["id"] for item in objects] == ["scenario:builder", "skill:builder_skill"]
    assert [item["id"] for item in tree] == ["builder_memory.md", "ui_revisions"]
    assert lifecycle[0]["children"][0]["revision"] == "030"
    assert lifecycle[0]["children"][0]["canMakeCurrent"] is False
    assert lifecycle[0]["title_i18n"] == {"key": "builder.lifecycle.stage.prototype"}
    assert lifecycle[0]["children"][0]["status_i18n"] == {
        "key": "builder.lifecycle.status.current"
    }
    assert len(lifecycle) == 1
    assert lifecycle[0]["dependentStages"] == ["prototype", "automation", "publication"]
    assert lifecycle[0]["automationStatus"] == "not_started"
    assert lifecycle[0]["publicationStatus"] == "not_started"
    assert lifecycle[0]["children"][0]["children"] == []


def test_transition_returns_current_frame_for_stale_action(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.workflow,
        "transition",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("stale Builder action generation: expected 2, current 3")
        ),
    )
    monkeypatch.setattr(module.workflow, "get_state", lambda *args: {"generation": 3})
    monkeypatch.setattr(
        module.workflow,
        "get_interaction_frame",
        lambda *args: {"schema": "adaos.builder.interaction_frame.v1", "generation": 3},
    )

    result = module.transition_workflow(
        "stabilize_prototype",
        "scenario",
        "builder",
        expected_generation=2,
    )

    assert result["ok"] is False
    assert result["stale"] is True
    assert result["workflow"]["generation"] == 3
    assert result["interaction_frame"]["generation"] == 3


def test_process_nests_implementation_under_its_source_prototype(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.workflow,
        "get_state",
        lambda *args: {
            "generation": 7,
            "interaction": {"inspected_ref": None, "preview_target": "prototype:recipes:002"},
            "change": {
                "change_id": "CH-recipes",
                "request": "Add recipe search",
                "status": "trial",
                "issues": [],
                "runs": [{"run_id": "RUN-implementation", "activity": "automation_completed"}],
            },
            "prototype": {"head_revision": "003", "status": "frozen"},
            "automation": {
                "status": "completed",
                "source_prototype_revision": "002",
                "head_task_id": "task-1",
            },
            "delivery": {"status": "trial", "candidate_id": "candidate-1"},
            "publication": {"status": "not_started"},
        },
    )
    monkeypatch.setattr(
        module.projects,
        "list_files",
        lambda *args, **kwargs: [
            {"path": "ui_revisions/002.json"},
            {"path": "ui_revisions/003.json"},
        ],
    )

    result = module.get_process("scenario", "recipes")
    revisions = result["tree"][0]["children"][1]["children"]
    source = next(item for item in revisions if item["ref"] == "prototype:recipes:002")
    implementation = source["children"][0]

    assert implementation["kind"] == "implementation"
    assert implementation["source_prototype_revision"] == "002"
    assert implementation["children"][0]["kind"] == "trial"
    assert result["interaction"]["preview_target"] == "prototype:recipes:002"


def test_semantic_ui_tool_forwards_typed_local_reversible_operation(monkeypatch) -> None:
    module = _module()
    operations: list[dict] = []
    monkeypatch.setattr(
        module.semantic_ui,
        "apply",
        lambda operation: operations.append(operation) or {"ok": True, "revision": "055"},
    )

    result = module.apply_semantic_ui_change(
        "RUN-rename",
        "CH-builder",
        "widget:project-header",
        "054",
        "Current project",
        object_type="scenario",
        object_id="builder",
    )

    assert result["revision"] == "055"
    assert operations[0]["project_ref"] == "scenario:builder"
    assert operations[0]["risk"] == "local_reversible"
    assert operations[0]["operation"] == "rename"


def test_review_constraint_tools_forward_structured_review_and_revision(monkeypatch) -> None:
    module = _module()
    registered: list[dict] = []
    evaluated: list[dict] = []
    monkeypatch.setattr(
        module.review,
        "register_constraint",
        lambda anchor, **kwargs: registered.append({"anchor": anchor, **kwargs})
        or {"ok": True, "constraint": {"constraint_id": "constraint.recipe-label"}},
    )
    monkeypatch.setattr(
        module.review,
        "evaluate_current",
        lambda *args, **kwargs: evaluated.append({"args": args, "kwargs": kwargs})
        or {"ok": True, "evaluations": []},
    )

    created = module.register_review_constraint(
        "review.recipe-label",
        "CH-recipes",
        "field:recipe-form:recipe-name",
        "Use the full label.",
        "label_equals",
        "Recipe name",
        "006",
        object_id="recipes",
        expected_generation=12,
    )
    checked = module.evaluate_review_constraints(
        object_id="recipes",
        revision="007",
        expected_generation=13,
    )

    assert created["ok"] is True
    assert registered[0]["anchor"]["artifact_ref"] == "scenario:recipes@ui_revision:006"
    assert registered[0]["anchor"]["target_ref"] == "field:recipe-form:recipe-name"
    assert registered[0]["kind"] == "label_equals"
    assert registered[0]["expected"] == "Recipe name"
    assert registered[0]["expected_generation"] == 12
    assert checked["ok"] is True
    assert evaluated == [
        {
            "args": ("scenario", "recipes"),
            "kwargs": {"revision": "007", "expected_generation": 13},
        }
    ]


def test_project_context_and_metadata_mutations_stay_behind_sdk(monkeypatch) -> None:
    module = _module()
    calls: list[tuple[str, tuple, dict]] = []
    monkeypatch.setattr(
        module.prompt_context,
        "save_base",
        lambda *args, **kwargs: calls.append(("save_base", args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        module.prompt_context,
        "append_addendum",
        lambda *args, **kwargs: calls.append(("append", args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        module.prompt_context,
        "set_preferences",
        lambda *args, **kwargs: calls.append(("preferences", args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        module.projects,
        "update_metadata",
        lambda *args, **kwargs: calls.append(("metadata", args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        module,
        "_record_project_change",
        lambda **kwargs: {"change_id": "chg", "status": "recorded"},
    )

    saved = module.save_prompt_context("Base", webspace_id="desktop")
    appended = module.append_prompt_addendum("Delta", iteration_ref="030", webspace_id="desktop")
    module.archive_project(True)
    module.update_project_metadata(title="Builder 2")

    assert calls[0][0] == "save_base"
    assert calls[1][0] == "append"
    assert calls[2] == ("preferences", ("scenario", "builder"), {"archived": True})
    assert calls[3][0] == "metadata"
    assert saved["evidence"]["status"] == "recorded"
    assert appended["change_id"] == "chg"
