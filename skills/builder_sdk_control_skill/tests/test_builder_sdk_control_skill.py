from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

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
    assert tools["submit_automation"]["side_effects"] == "local_write"
    assert tools["save_prompt_context"]["side_effects"] == "local_write"
    assert tools["append_prompt_addendum"]["side_effects"] == "local_write"
    assert tools["set_llm_profile"]["side_effects"] == "local_write"
    assert tools["update_project_metadata"]["side_effects"] == "local_write"
    assert tools["archive_project"]["side_effects"] == "local_write"
    assert tools["update_project"]["side_effects"] == "local_write"
    assert tools["select_preview"]["side_effects"] == "ui_navigation"
    assert "side_effects" not in tools["push_project"]
    assert "side_effects" not in tools["publish_project"]
    assert "side_effects" not in tools["delete_project"]


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

    module.read_project_file("builder_memory.md")
    saved = module.save_project_file("builder_memory.md", "memory", webspace_id="desktop")
    started = module.start_automation("Implement it", webspace_id="desktop")

    assert calls[0][1][:2] == ("scenario", "builder")
    assert calls[1][1][:2] == ("scenario", "builder")
    assert calls[2][2]["object_type"] == "scenario"
    assert calls[2][2]["object_id"] == "builder"
    assert saved["evidence"]["status"] == "accepted"
    assert started["status"] == "queued"


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

    automation_stage = module.get_lifecycle("skill", "builder")[1]
    child = automation_stage["children"][0]

    assert child["kind"] == "automation_result"
    assert child["status"] == "succeeded"
    assert child["phase"] == "verification"
    assert child["summary"] == "Implementation and checks completed"
    assert child["task_id"] == "task-42"
    assert child["result_branch"] == "builder/task-42"
    assert len(child["evidence"]) == module.MAX_LIFECYCLE_CHILDREN


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
                "source_refs": {"action": "publication"},
                "meta": {"dry_run": False, "version": f"0.2.{index}"},
            }
            for index in range(8)
        ],
        {"change_id": "checkpoint", "source_refs": {"action": "checkpoint"}},
    ]
    monkeypatch.setattr(module.conversation, "list_development_changes", lambda **kwargs: changes)

    publication_stage = module.get_lifecycle("skill", "builder")[2]

    assert publication_stage["lifecycleState"] == "active"
    assert len(publication_stage["children"]) == module.MAX_LIFECYCLE_CHILDREN
    assert all(child["kind"] == "publication_release" for child in publication_stage["children"])
    assert {child["change_id"] for child in publication_stage["children"]} == {
        "release-0", "release-1", "release-2", "release-3", "release-4"
    }


def test_publish_records_only_successful_non_dry_run_releases(monkeypatch) -> None:
    module = _module()
    results = iter(
        [
            {"ok": True, "dry_run": True, "version": "0.2.1"},
            {"ok": True, "dry_run": False, "version": "0.2.1", "release_id": "rel-21"},
            {"ok": False, "dry_run": False, "error": "validation failed"},
        ]
    )
    monkeypatch.setattr(module.projects, "publish", lambda *args, **kwargs: next(results))
    recorded: list[dict] = []
    monkeypatch.setattr(
        module,
        "_record_project_change",
        lambda **kwargs: recorded.append(kwargs) or {"change_id": "publication-change", "status": "accepted"},
    )

    dry_run = module.publish_project(dry_run=True)
    published = module.publish_project(dry_run=False, webspace_id="desktop")
    failed = module.publish_project(dry_run=False)

    assert "change_id" not in dry_run
    assert published["change_id"] == "publication-change"
    assert "change_id" not in failed
    assert len(recorded) == 1
    assert recorded[0]["action"] == "publication"
    assert recorded[0]["meta"] == {
        "dry_run": False,
        "version": "0.2.1",
        "release": "rel-21",
        "bump": "patch",
    }


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


def test_preview_selection_waits_until_materialized(monkeypatch) -> None:
    module = _module()
    captured: dict = {}

    def _select(*args, **kwargs):
        captured.update({"args": args, **kwargs})
        return {"ok": True, "selected": True}

    monkeypatch.setattr(module.preview, "select_project", _select)
    monkeypatch.setattr(
        module.preview,
        "canonical_source_webspace_id",
        lambda source: source.removesuffix("-dev"),
    )

    result = module.select_preview("scenario", "builder", webspace_id="builder-smoke-dev")

    assert result["selected"] is True
    assert captured["args"] == ("scenario", "builder")
    assert captured["source_webspace_id"] == "builder-smoke"
    assert captured["ensure_ready"] is True
    assert captured["wait_for_rebuild"] is True


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
    monkeypatch.setattr(
        module.projects,
        "create",
        lambda *args, **kwargs: calls.append(("create", args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        module.projects,
        "push",
        lambda *args, **kwargs: calls.append(("push", args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        module.projects,
        "publish",
        lambda *args, **kwargs: calls.append(("publish", args, kwargs)) or {"ok": True, "dry_run": True},
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

    module.create_project("skill", "demo_skill", template="default")
    pushed = module.push_project("scenario", "builder", message="checkpoint", webspace_id="desktop")
    result = module.publish_project("scenario", "builder", dry_run=True)

    assert calls[0] == ("create", ("skill", "demo_skill"), {"template": "default"})
    assert calls[1] == ("push", ("scenario", "builder"), {"message": "checkpoint"})
    assert calls[2][2]["dry_run"] is True
    assert pushed["evidence"]["status"] == "pushed"
    assert captured_change["artifact_refs"] == [{"kind": "scenario", "id": "builder"}]
    assert result["dry_run"] is True


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
    assert lifecycle[1]["lifecycleState"] == "not_started"
    assert lifecycle[1]["title_i18n"] == {"key": "builder.lifecycle.stage.automation"}
    assert lifecycle[1]["status_i18n"] == {"key": "builder.lifecycle.status.not_started"}
    assert lifecycle[2]["lifecycleState"] == "not_started"
    assert lifecycle[2]["title_i18n"] == {"key": "builder.lifecycle.stage.publication"}


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
