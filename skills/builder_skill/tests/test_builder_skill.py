from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import logging
import sys
import threading
import time
import types
from pathlib import Path

import yaml


SKILL_ROOT = Path(__file__).resolve().parents[1]


def _find_repo_root() -> Path:
    marker = Path("src") / "adaos" / "services"
    candidates = [Path.cwd(), *SKILL_ROOT.parents]
    for root in candidates:
        if (root / marker).exists():
            return root
    raise FileNotFoundError(f"Cannot find AdaOS repo root containing {marker}")


REPO_ROOT = _find_repo_root()
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _load_module():
    if "y_py" not in sys.modules:
        sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
    if "ypy_websocket" not in sys.modules:
        ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
        sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
        sys.modules["ypy_websocket.ystore"] = ystore_mod
    spec = importlib.util.spec_from_file_location("builder_skill_under_test", SKILL_ROOT / "handlers" / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_declares_builder_dialog_agent() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))

    tools = {item["name"] for item in manifest["tools"]}
    tools_by_name = {item["name"]: item for item in manifest["tools"]}
    assert {"start", "chat", "create_scenario_draft", "update_current_scenario", "get_preview_state"}.issubset(tools)
    assert tools_by_name["chat"]["timeout_seconds"] >= 120
    assert tools_by_name["update_current_scenario"]["timeout_seconds"] >= 120
    assert manifest["default_tool"] == "chat"
    assert manifest["conversation"]["dialog_channel"]["id"] == "builder"
    assert manifest["conversation"]["agents"][0]["id"] == "agent:builder_skill:builder"


def test_explicit_app_title_wins_over_incidental_shopping_list_mention() -> None:
    skill = _load_module()
    idea = (
        'Конструктор, создай мобильное приложение «Книга рецептов». '
        'В карточке рецепта добавь действие «Добавить ингредиенты в список покупок».'
    )

    assert skill._explicit_prototype_title(idea) == "Книга рецептов"
    assert skill._scenario_id_from_idea(idea).startswith("prototype_app_")
    assert [field["id"] for field in skill._build_fields(idea)] == ["title", "notes", "status"]


def test_explicit_shopping_list_title_keeps_shopping_scaffold() -> None:
    skill = _load_module()
    idea = 'Конструктор, создай приложение «Список покупок».'

    assert skill._scenario_id_from_idea(idea).startswith("shopping_list_")
    assert [field["id"] for field in skill._build_fields(idea)] == ["item", "quantity", "category", "done"]


def test_builder_topic_ref_normalizes_old_session_topic_without_store(monkeypatch) -> None:
    skill = _load_module()
    calls: list[dict] = []

    import adaos.services.conversation_links as conversation_links

    def _ensure_builder_topic(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return {"thread_id": "unexpected", "topic_id": "unexpected"}

    monkeypatch.setattr(conversation_links, "ensure_builder_topic", _ensure_builder_topic)

    topic = skill._builder_topic_ref(
        "desktop",
        session={
            "id": "builder_session",
            "draft_id": "draft.todo",
            "scenario_id": "todo_scenario",
            "topic_ref": {
                "schema": "adaos.conversation.topic_ref.v1",
                "thread_id": "thread.builder.desktop.draft.todo",
                "topic_id": "builder:desktop:draft.todo",
                "conversation_id": "conv.skill.builder_skill.default.desktop",
            },
        },
        binding={"dev_webspace_id": "desktop-dev"},
    )

    assert calls == []
    assert topic["thread_id"] == "prompt-project:scenario:todo_scenario"
    assert topic["topic_id"] == "prompt-project:scenario:todo_scenario"
    assert topic["scenario_id"] == "todo_scenario"
    assert topic["active_draft_id"] == "draft.todo"
    assert topic["dev_webspace_id"] == "desktop-dev"


def test_builder_topic_ref_replaces_stale_prompt_project_topic(monkeypatch) -> None:
    skill = _load_module()

    import adaos.services.conversation_links as conversation_links

    monkeypatch.setattr(
        conversation_links,
        "ensure_builder_topic",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not touch store")),
    )

    topic = skill._builder_topic_ref(
        "desktop",
        session={
            "id": "builder_session",
            "draft_id": "draft.prototype",
            "scenario_id": "prototype_app_4d5758e5",
            "topic_ref": {
                "schema": "adaos.conversation.topic_ref.v1",
                "thread_id": "prompt-project:scenario:todo_list_5b9319fa",
                "topic_id": "prompt-project:scenario:todo_list_5b9319fa",
                "scenario_id": "todo_list_5b9319fa",
                "project_id": "todo_list_5b9319fa",
            },
        },
        binding={"runtime_scenario_id": "prototype_app_4d5758e5", "dev_webspace_id": "desktop-dev"},
    )

    assert topic["thread_id"] == "prompt-project:scenario:prototype_app_4d5758e5"
    assert topic["topic_id"] == "prompt-project:scenario:prototype_app_4d5758e5"
    assert topic["scenario_id"] == "prototype_app_4d5758e5"
    assert topic["project_id"] == "prototype_app_4d5758e5"


def test_builder_aligns_stale_workbench_binding_to_incoming_prompt_topic(monkeypatch) -> None:
    skill = _load_module()
    calls: list[dict] = []

    class _Svc:
        def get_workspace_binding(self, webspace_id):
            calls.append({"method": "get_workspace_binding", "webspace_id": webspace_id})
            return {"runtime_scenario_id": "codex_eval_survey2", "dev_webspace_id": "desktop-dev"}

        def set_active_draft(self, **kwargs):
            calls.append({"method": "set_active_draft", **kwargs})
            return {
                "runtime_scenario_id": kwargs.get("runtime_scenario_id"),
                "dev_webspace_id": "desktop-dev",
            }

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Svc())

    binding = skill._align_workbench_binding_to_meta(
        "desktop",
        {"conversation_topic_id": "prompt-project:scenario:codex_eval_survey3"},
    )

    assert binding["runtime_scenario_id"] == "codex_eval_survey3"
    assert calls[-1]["method"] == "set_active_draft"
    assert calls[-1]["source_webspace_id"] == "desktop"
    assert calls[-1]["runtime_scenario_id"] == "codex_eval_survey3"
    assert calls[-1]["active_draft_id"] is None


def test_save_session_batches_sessions_and_current_pointer(monkeypatch) -> None:
    skill = _load_module()
    calls: list[dict] = []

    monkeypatch.setattr(skill, "_sessions", lambda webspace_id: {})
    monkeypatch.setattr(skill, "_mem_set_many", lambda values: calls.append(dict(values)))

    session = {"id": "builder_session", "scenario_id": "todo_scenario"}
    result = skill._save_session("desktop", session)

    assert result is session
    assert len(calls) == 1
    payload = calls[0]
    sessions_key = skill._scoped_key(skill.SESSIONS_KEY, "desktop")
    current_key = skill._scoped_key(skill.CURRENT_KEY, "desktop")
    assert set(payload) == {sessions_key, current_key}
    assert payload[current_key] == "builder_session"
    assert payload[sessions_key]["builder_session"]["scenario_id"] == "todo_scenario"


def test_target_session_recovers_selected_scenario_from_artifacts(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "prototype_app"
    revision_dir = artifact_root / "ui_revisions"
    revision_dir.mkdir(parents=True)
    (revision_dir / "current.txt").write_text("023\n", encoding="utf-8")
    (revision_dir / "023.json").write_text('{"revision":"023"}', encoding="utf-8")
    (artifact_root / "builder.draft.json").write_text(
        json.dumps(
            {
                "draft_id": "draft.prototype",
                "source": {"utterance": "Создай форму опроса"},
                "artifact": {"id": "prototype_app_4d5758e5", "draft_root": str(artifact_root)},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (artifact_root / "webui.json").write_text(
        json.dumps(
            {
                "schema": "adaos.webui.v1",
                "ui": {
                    "application": {
                        "desktop": {
                            "pageSchema": {
                                "id": "prototype_app_4d5758e5",
                                "title": "Conference Survey",
                                "layout": {"type": "single"},
                                "widgets": [{"id": "form", "type": "ui.form"}],
                            }
                        }
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    saved: list[dict] = []
    monkeypatch.setattr(skill, "_workbench_binding", lambda _ws: {
        "active_draft_id": "draft.prototype",
        "runtime_scenario_id": "prototype_app_4d5758e5",
    })
    monkeypatch.setattr(skill, "_sessions", lambda _ws: {})
    monkeypatch.setattr(skill, "_scenario_artifact_root_from_id", lambda _scenario_id: str(artifact_root))
    monkeypatch.setattr(skill, "_mem_set_many", lambda values: saved.append(dict(values)))

    session, binding = skill._target_session("desktop")

    assert binding["runtime_scenario_id"] == "prototype_app_4d5758e5"
    assert session["id"] == "draft.prototype"
    assert session["scenario_id"] == "prototype_app_4d5758e5"
    assert session["artifact_root"] == str(artifact_root.resolve())
    assert session["ui_revision"] == "023"
    assert session["title"] == "Conference Survey"
    assert session["preview_state"]["page_schema"]["widgets"][0]["type"] == "ui.form"
    assert saved


def test_sync_session_from_artifacts_refreshes_stale_current_revision(tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "prototype_app"
    revision_dir = artifact_root / "ui_revisions"
    revision_dir.mkdir(parents=True)
    (revision_dir / "current.txt").write_text("024\n", encoding="utf-8")
    (revision_dir / "024.json").write_text('{"revision":"024"}', encoding="utf-8")
    (artifact_root / "webui.json").write_text(
        json.dumps(
            {
                "schema": "adaos.webui.v1",
                "ui": {
                    "application": {
                        "desktop": {
                            "pageSchema": {
                                "id": "prototype_app_4d5758e5",
                                "title": "Updated Prototype",
                                "layout": {"type": "single"},
                                "widgets": [{"id": "form", "type": "ui.form"}],
                            }
                        }
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = {
        "id": "draft.prototype",
        "draft_id": "draft.prototype",
        "scenario_id": "prototype_app_4d5758e5",
        "artifact_root": str(artifact_root),
        "title": "Old Prototype",
        "version": "023",
        "ui_revision": "023",
        "ui_revisions": [{"revision": "023", "path": str(revision_dir / "023.json")}],
        "preview_state": {"version": "023", "title": "Old Prototype"},
    }

    changed = skill._sync_session_from_artifacts(session)

    assert changed is True
    assert session["version"] == "024"
    assert session["ui_revision"] == "024"
    assert session["title"] == "Updated Prototype"
    assert session["preview_state"]["version"] == "024"
    assert session["preview_state"]["title"] == "Updated Prototype"
    assert session["preview_state"]["page_schema"]["widgets"][0]["type"] == "ui.form"
    assert session["ui_revisions"][-1]["revision"] == "024"


def test_create_shopping_list_scenario_draft_writes_declarative_webui(tmp_path, monkeypatch) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "shopping_list"

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            (artifact_root / "scenario.json").write_text(
                '{"id":"shopping_list","version":"0.1.0","name":"shopping_list","steps":[]}',
                encoding="utf-8",
            )
            return {
                "ok": True,
                "draft": {"draft_id": "draft.shopping"},
                "artifact_root": str(artifact_root),
                "kwargs": kwargs,
            }

    import adaos.services.builder.workspace as workspace

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {"dev_webspace_id": "builder-skill-test-dev", "active_draft_id": kwargs.get("active_draft_id")}

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())

    result = skill.create_scenario_draft(
        idea="\u0421\u0442\u0440\u043e\u0438\u0442\u0435\u043b\u044c, \u0441\u043e\u0437\u0434\u0430\u0434\u0438\u043c \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435 \u0441\u043f\u0438\u0441\u043e\u043a \u043f\u043e\u043a\u0443\u043f\u043e\u043a",
        webspace_id="builder-skill-test",
    )

    assert result["ok"] is True
    assert result["dialog"]["dialog_channel_id"] == "builder"
    assert result["dialog"]["default_tool"] == "builder_skill.chat"
    assert result["topic"]["thread_id"].startswith("prompt-project:scenario:")
    assert result["dialog"]["thread_id"] == result["topic"]["thread_id"]
    assert result["scenario_id"].startswith("shopping_list_")
    assert result["preview_state"]["current_ui"]["type"] == "page"
    assert result["preview_state"]["datasources"][0]["type"] == "internal_crud"
    webui = artifact_root / "webui.json"
    assert webui.exists()
    webui_payload = json.loads(webui.read_text(encoding="utf-8"))
    assert webui_payload["schema"] == "adaos.webui.v1"
    assert "preview_state" not in webui_payload
    assert webui_payload["ui"]["application"]["desktop"]["pageSchema"]["widgets"]
    scenario = yaml.safe_load((artifact_root / "scenario.json").read_text(encoding="utf-8"))
    page_schema = scenario["ui"]["application"]["desktop"]["pageSchema"]
    assert page_schema["title"] == "\u0421\u043f\u0438\u0441\u043e\u043a \u043f\u043e\u043a\u0443\u043f\u043e\u043a"
    assert {item["type"] for item in page_schema["widgets"]} >= {"ui.form", "ui.table"}


def test_create_draft_does_not_publish_pending_action_for_reversible_local_revision(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "shopping_list"
    published: list[dict] = []

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **_kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            return {"ok": True, "draft": {"draft_id": "draft.shopping"}, "artifact_root": str(artifact_root)}

    import adaos.services.builder.workspace as workspace
    import adaos.services.pending_actions as pending_actions

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)

    def _publish_pending_action(**kwargs):
        published.append(dict(kwargs))
        return {
            "id": "pa.builder.draft",
            "kind": kwargs["kind"],
            "domain_ref": kwargs["domain_ref"],
            "metadata": kwargs["metadata"],
        }

    monkeypatch.setattr(pending_actions, "publish_pending_action", _publish_pending_action)

    result = skill.create_scenario_draft(
        idea="Builder, create a shopping list app",
        webspace_id="builder-pa-ws",
        _meta={
            "conversation_id": "conv.skill.builder_skill.default.builder-pa-ws",
            "thread_id": "thread.builder.1",
            "turn_trace_id": "trace.builder.1",
            "request_id": "req.builder.1",
            "message_id": "msg.builder.1",
        },
    )

    assert result["pending_action"] is None
    assert result["topic"]["thread_id"].startswith("prompt-project:scenario:")
    assert result["dialog"]["thread_id"] == result["topic"]["thread_id"]
    assert published == []


def test_update_current_scenario_adds_card_view(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "shopping_list"

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **_kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            return {"ok": True, "draft": {"draft_id": "draft.shopping"}, "artifact_root": str(artifact_root)}

    import adaos.services.builder.workspace as workspace

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    skill.create_scenario_draft("\u0441\u043e\u0437\u0434\u0430\u0439 \u0441\u043f\u0438\u0441\u043e\u043a \u043f\u043e\u043a\u0443\u043f\u043e\u043a", webspace_id="builder-skill-cards")

    result = skill.update_current_scenario("\u043f\u043e\u043a\u0430\u0436\u0438 \u043e\u0442\u0432\u0435\u0442\u044b \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0430\u043c\u0438", webspace_id="builder-skill-cards")

    assert result["ok"] is True
    assert result["patch"]["operation"] == "change_view_representation"
    assert any(item["type"] == "card_list" for item in result["preview_state"]["current_ui"]["children"])


def test_card_view_hides_table_in_generated_page_schema(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "todo_cards"

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **_kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            (artifact_root / "scenario.json").write_text(
                '{"id":"todo_cards","version":"0.1.0","name":"todo_cards","steps":[]}',
                encoding="utf-8",
            )
            return {"ok": True, "draft": {"draft_id": "draft.todo.cards"}, "artifact_root": str(artifact_root)}

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {"dev_webspace_id": "builder-cards-dev", "active_draft_id": kwargs.get("active_draft_id")}

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    import adaos.services.builder.workspace as workspace

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})

    skill.create_scenario_draft("create todo list", webspace_id="builder-cards")
    result = skill.update_current_scenario("\u041f\u043e\u043a\u0430\u0436\u0438 \u0441\u043f\u0438\u0441\u043e\u043a \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0430\u043c\u0438", webspace_id="builder-cards")

    assert result["patch"]["diff"]["hide_table"] is True
    page = json.loads((artifact_root / "scenario.json").read_text(encoding="utf-8"))
    widgets = page["ui"]["application"]["desktop"]["pageSchema"]["widgets"]
    assert any(item["id"] == "prototype-cards" for item in widgets)
    assert not any(item["id"] == "prototype-table" for item in widgets)
    cards = next(item for item in widgets if item["id"] == "prototype-cards")
    assert cards["inputs"]["previewKey"]


def test_update_current_scenario_swaps_input_and_cards(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "todo_swap"

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **_kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            (artifact_root / "scenario.json").write_text(
                '{"id":"todo_swap","version":"0.1.0","name":"todo_swap","steps":[]}',
                encoding="utf-8",
            )
            return {"ok": True, "draft": {"draft_id": "draft.todo.swap"}, "artifact_root": str(artifact_root)}

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {"dev_webspace_id": "builder-swap-dev", "active_draft_id": kwargs.get("active_draft_id")}

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    import adaos.services.builder.workspace as workspace

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})

    skill.create_scenario_draft("create todo list", webspace_id="builder-swap")
    result = skill.update_current_scenario("\u041f\u0435\u0440\u0435\u0441\u0442\u0430\u0432\u044c \u043c\u0435\u0441\u0442\u0430\u043c\u0438 \u043e\u0431\u043b\u0430\u0441\u0442\u044c Input \u0438 Cards", webspace_id="builder-swap")

    assert result["patch"]["operation"] == "swap_layout_areas"
    assert result["preview_state"]["layout_order"] == "cards_first"
    page = json.loads((artifact_root / "scenario.json").read_text(encoding="utf-8"))
    widgets = page["ui"]["application"]["desktop"]["pageSchema"]["widgets"]
    form = next(item for item in widgets if item["id"] == "prototype-form")
    cards = next(item for item in widgets if item["id"] == "prototype-cards")
    assert form["area"] == "right"
    assert cards["area"] == "main"
    assert cards["inputs"]["previewKey"]
    assert not any(item["id"] == "prototype-table" for item in widgets)


def test_update_current_scenario_swaps_input_and_cards_with_lost_cyrillic(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "todo_swap_lost_cyrillic"

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **_kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            (artifact_root / "scenario.json").write_text(
                '{"id":"todo_swap_lost_cyrillic","version":"0.1.0","name":"todo_swap_lost_cyrillic","steps":[]}',
                encoding="utf-8",
            )
            return {"ok": True, "draft": {"draft_id": "draft.todo.swap.lost"}, "artifact_root": str(artifact_root)}

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {"dev_webspace_id": "builder-swap-lost-dev", "active_draft_id": kwargs.get("active_draft_id")}

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    import adaos.services.builder.workspace as workspace

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})

    skill.create_scenario_draft("create todo list", webspace_id="builder-swap-lost")
    result = skill.update_current_scenario("\u041f\u0435\u0440\u0435\u0441\u0442\u0430\u0432\u0438\u0442\u044c \u043e\u0431\u043b\u0430\u0441\u0442\u0438 Input \u0438 Cards", webspace_id="builder-swap-lost")

    assert result["patch"]["operation"] == "swap_layout_areas"
    assert result["preview_state"]["layout_order"] == "cards_first"
    revision = json.loads((artifact_root / "ui_revisions" / "002.json").read_text(encoding="utf-8"))
    assert revision["request"]["text"] == "\u041f\u0435\u0440\u0435\u0441\u0442\u0430\u0432\u0438\u0442\u044c \u043e\u0431\u043b\u0430\u0441\u0442\u0438 Input \u0438 Cards"


def test_update_current_scenario_prefers_llm_for_ui_changes_when_enabled(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "todo_swap_no_llm"
    monkeypatch.setenv("ADAOS_BUILDER_LLM_IN_TESTS", "1")
    llm_calls: list[dict] = []

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **_kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            return {"ok": True, "draft": {"draft_id": "draft.todo.swap.no.llm"}, "artifact_root": str(artifact_root)}

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {"dev_webspace_id": "builder-swap-no-llm-dev", "active_draft_id": kwargs.get("active_draft_id")}

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    import adaos.services.builder.workspace as workspace

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    created = skill.create_scenario_draft("create todo list", webspace_id="builder-swap-no-llm")
    preview = dict(created["preview_state"])
    preview["layout_order"] = "cards_first"
    page_schema = skill._page_schema_from_preview(preview)
    payload = {"schema": "adaos.webui.v1", "generated_by": "builder_skill", "ui": {"application": {"desktop": {"pageSchema": page_schema}}}}
    monkeypatch.setattr(
        skill,
        "_apply_llm_webui_transform",
        lambda **kwargs: llm_calls.append(dict(kwargs)) or {
            "ok": True,
            "preview_state": preview,
            "payload": payload,
            "comment": "layout updated",
            "validation": {"ok": True},
        },
    )

    result = skill.update_current_scenario("swap input and cards", webspace_id="builder-swap-no-llm")

    assert result["patch"]["operation"] == "llm_webui_transform"
    assert result["preview_state"]["layout_order"] == "cards_first"
    assert result["ui_revision"]["revision"] == "002"
    assert len(llm_calls) == 1
    written_webui = json.loads((artifact_root / "webui.json").read_text(encoding="utf-8"))
    builder_meta = written_webui["ui"]["application"]["desktop"]["pageSchema"]["meta"]["builder"]
    assert builder_meta["ui_revision"] == "002"
    assert builder_meta["proto"] == "002"
    assert builder_meta["scenario_id"] == created["scenario_id"]

    result = skill.update_current_scenario(
        "apply review comments",
        webspace_id="builder-swap-no-llm",
        _meta={
            "prototype_review_notes": {
                "schema": "adaos.prototype_review_notes.v1",
                "source_webspace_id": "builder-swap-no-llm",
                "dev_webspace_id": "builder-swap-no-llm-dev",
                "revision_key": "todo:002",
                "notes": 'Prototype review notes for todo:002:\n- field field:prototype-form:title ("Title"): Move this field to the top.',
            }
        },
    )

    assert result["patch"]["operation"] == "llm_webui_transform"
    assert len(llm_calls) == 2
    assert "apply review comments" in llm_calls[-1]["instruction"]
    assert "Prototype review notes from the current dev preview" in llm_calls[-1]["instruction"]
    assert "Move this field to the top" in llm_calls[-1]["instruction"]


def test_api_request_chat_meta_uses_semantic_request_origin() -> None:
    skill = _load_module()

    review = skill._api_request_chat_meta(
        {
            "action_source": "api_tool_call",
            "request_origin_id": "prototype_review_notes",
            "request_origin_label": "Review notes",
        }
    )
    generic = skill._api_request_chat_meta({"action_source": "api_tool_call"})

    assert review["active_agent_id"] == "prototype_review_notes"
    assert review["active_agent_label"] == "Review notes"
    assert review["origin_label"] == "Review notes"
    assert review["recipient_label"] == skill.AGENT_LABEL
    assert generic["active_agent_id"] == "api"
    assert generic["active_agent_label"] == "API"


def test_update_current_scenario_recovers_artifact_root_for_ui_revisions(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "todo_recover_artifact"
    state_dir = tmp_path / "state"
    draft_id = "draft.todo.recover"

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **_kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            (artifact_root / "scenario.json").write_text(
                '{"id":"todo_recover","version":"0.1.0","name":"todo_recover","steps":[]}',
                encoding="utf-8",
            )
            return {"ok": True, "draft": {"draft_id": draft_id}, "artifact_root": str(artifact_root)}

    class _Workbench:
        def get_workspace_binding(self, _webspace_id):
            return {"active_draft_id": draft_id, "runtime_scenario_id": "todo_recover"}

        def set_active_draft(self, **kwargs):
            return {"dev_webspace_id": "builder-recover-dev", "active_draft_id": kwargs.get("active_draft_id")}

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    import adaos.services.builder.workspace as workspace
    import adaos.services.runtime_paths as runtime_paths

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    monkeypatch.setattr(runtime_paths, "current_state_dir", lambda: state_dir)
    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})

    skill.create_scenario_draft("create todo list", webspace_id="builder-recover")
    draft_payload = {
        "draft_id": draft_id,
        "artifact": {"kind": "scenario", "id": "todo_recover", "draft_root": str(artifact_root)},
    }
    draft_path = state_dir / "builder" / "drafts" / draft_id / "builder.draft.json"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(json.dumps(draft_payload), encoding="utf-8")
    sessions = skill._FALLBACK_MEMORY[skill._scoped_key(skill.SESSIONS_KEY, "builder-recover")]
    for session in sessions.values():
        session.pop("artifact_root", None)
        session["scenario_id"] = "todo_recover"
        session["draft_id"] = draft_id

    result = skill.update_current_scenario("show cards", webspace_id="builder-recover")

    assert result["ui_revision"]["revision"] == "002"
    assert "\u0420\u0435\u0432\u0438\u0437\u0438\u044f UI: 002" in result["message"]
    assert result["message_actions"]
    assert (artifact_root / "ui_revisions" / "002.json").exists()


def test_update_current_scenario_adds_execution_checkbox(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "todo_checkbox"

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **_kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            return {"ok": True, "draft": {"draft_id": "draft.todo.checkbox"}, "artifact_root": str(artifact_root)}

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {"dev_webspace_id": "builder-checkbox-dev", "active_draft_id": kwargs.get("active_draft_id")}

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    import adaos.services.builder.workspace as workspace

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})

    skill.create_scenario_draft("create todo list", webspace_id="builder-checkbox")
    result = skill.update_current_scenario("\u0414\u043e\u0431\u0430\u0432\u044c \u0447\u0435\u043a\u0431\u043e\u043a\u0441 \u0438\u0441\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u044f", webspace_id="builder-checkbox")

    assert result["patch"]["operation"] == "add_field"
    field = next(item for item in result["preview_state"]["datasources"][0]["fields"] if item["id"] == "done")
    assert field["type"] == "boolean"
    assert field["label"] == "\u0418\u0441\u043f\u043e\u043b\u043d\u0435\u043d\u043e"


def test_update_current_scenario_uses_llm_webui_fallback(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "llm_fallback"
    monkeypatch.setenv("ADAOS_BUILDER_LLM_IN_TESTS", "1")

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **_kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            (artifact_root / "scenario.json").write_text(
                '{"id":"llm_fallback","version":"0.1.0","name":"llm_fallback","steps":[]}',
                encoding="utf-8",
            )
            return {"ok": True, "draft": {"draft_id": "draft.llm"}, "artifact_root": str(artifact_root)}

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {"dev_webspace_id": "builder-llm-dev", "active_draft_id": kwargs.get("active_draft_id")}

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    import adaos.services.builder.workspace as workspace

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})

    created = skill.create_scenario_draft("create todo list", webspace_id="builder-llm")
    preview = dict(created["preview_state"])
    preview["title"] = "English Todo"
    page_schema = skill._page_schema_from_preview(preview)
    page_schema["title"] = "English Todo"
    payload = {"schema": "adaos.webui.v1", "generated_by": "builder_skill", "ui": {"application": {"desktop": {"pageSchema": page_schema}}}}
    monkeypatch.setattr(
        skill,
        "_apply_llm_webui_transform",
        lambda **_kwargs: {"ok": True, "payload": payload, "preview_state": preview, "validation": {"ok": True}},
    )

    result = skill.update_current_scenario("\u041d\u0430\u043f\u0438\u0448\u0438 \u0442\u0435\u043a\u0441\u0442 \u043d\u0430 \u0430\u043d\u0433\u043b\u0438\u0439\u0441\u043a\u043e\u043c \u044f\u0437\u044b\u043a\u0435", webspace_id="builder-llm")

    assert result["patch"]["operation"] == "llm_webui_transform"
    assert result["ui_revision"]["revision"] == "002"

    result = skill.update_current_scenario("\u0421\u0434\u0435\u043b\u0430\u0439 \u0431\u043e\u043b\u0435\u0435 \u043a\u043e\u043c\u043f\u0430\u043a\u0442\u043d\u044b\u0439 \u0432\u0432\u043e\u0434", webspace_id="builder-llm")

    assert result["patch"]["operation"] == "llm_webui_transform"
    assert result["preview_state"]["title"] == "English Todo"
    assert result["ui_revision"]["revision"] == "003"
    saved = json.loads((artifact_root / "webui.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "adaos.webui.v1"
    assert saved["ui"]["application"]["desktop"]["pageSchema"]["title"] == "English Todo"


def test_llm_webui_transform_uses_stable_request_id_and_compact_prompt(monkeypatch) -> None:
    skill = _load_module()
    import adaos.sdk.llm.llm_client as llm_client

    monkeypatch.setenv("ADAOS_BUILDER_LLM_TIMEOUT_S", "181")
    monkeypatch.setenv("ADAOS_BUILDER_LLM_MAX_TOKENS", "4321")
    monkeypatch.setenv("ADAOS_BUILDER_LLM_TEMPERATURE", "0.35")
    monkeypatch.setenv("ADAOS_BUILDER_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("ADAOS_BUILDER_LLM_PROMPT_PROFILE", "openai-default")
    page_schema = {
        "id": "todo_list",
        "title": "Todo List",
        "layout": {"type": "split", "areas": [{"id": "main"}]},
        "widgets": [{"id": "prototype-form", "type": "ui.form", "area": "main", "inputs": {"fields": []}}],
    }
    preview = {
        "title": "Todo List",
        "current_ui": {"layout_order": "input_first"},
        "datasources": [{"id": "prototype_items", "type": "array"}],
        "mock_data": {"prototype_items": [{"title": "Buy tickets"}]},
    }
    payload = {"schema": "adaos.webui.v1", "generated_by": "builder_skill", "ui": {"application": {"desktop": {"pageSchema": page_schema}}}}
    captured: dict[str, object] = {}

    def _send_response(messages, **kwargs):
        captured["messages"] = list(messages)
        captured["kwargs"] = dict(kwargs)
        return {"output_text": json.dumps({**payload, "comment": "Updated."}, ensure_ascii=False)}

    monkeypatch.setattr(llm_client, "send_response", _send_response)
    monkeypatch.setattr(skill, "_normalise_llm_webui_payload", lambda parsed, previous_preview: (parsed, {"title": "Todo List", "page_schema": page_schema}))
    monkeypatch.setattr(skill, "_validate_builder_webui_payload", lambda payload_arg, preview_arg: {"ok": True})

    result = skill._apply_llm_webui_transform(
        session={"id": "builder_session_todo", "scenario_id": "todo_list", "version": "001"},
        instruction="Adapt sample data for conference preparation",
        preview_state=preview,
    )

    assert result["ok"] is True
    kwargs = captured["kwargs"]
    assert kwargs["timeout"] == 181
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["max_tokens"] == 4321
    assert kwargs["temperature"] == 0.35
    assert str(kwargs["request_id"]).startswith("builder-ui-")
    stable_prompt = captured["messages"][1]["content"]
    user_prompt = captured["messages"][2]["content"]
    assert "\n" not in stable_prompt
    assert "\n" not in user_prompt
    assert "webui_v1_schema" in stable_prompt
    stable_payload = json.loads(stable_prompt)["stable_builder_context"]
    assert stable_payload["llm_prompt_profile"]["id"] == "openai-default"
    assert stable_payload["llm_prompt_profile"]["model"] == "gpt-4o-mini"
    assert json.loads(user_prompt)["builder_request"]["instruction"] == "Adapt sample data for conference preparation"
    assert "Prompt profile: openai-default" in captured["messages"][0]["content"]


def test_builder_llm_temperature_defaults_to_mild_prototyping(monkeypatch) -> None:
    skill = _load_module()

    monkeypatch.delenv("ADAOS_BUILDER_LLM_TEMPERATURE", raising=False)
    assert skill._builder_llm_temperature() == 0.2

    monkeypatch.setenv("ADAOS_BUILDER_LLM_TEMPERATURE", "bad")
    assert skill._builder_llm_temperature() == 0.2

    monkeypatch.setenv("ADAOS_BUILDER_LLM_TEMPERATURE", "2")
    assert skill._builder_llm_temperature() == 1.0

    monkeypatch.setenv("ADAOS_BUILDER_LLM_TEMPERATURE", "-1")
    assert skill._builder_llm_temperature() == 0.0


def test_builder_omits_temperature_for_reasoning_model_families() -> None:
    skill = _load_module()

    assert skill._builder_llm_temperature_for_model("gpt-5") is None
    assert skill._builder_llm_temperature_for_model("gpt-5-mini", repair=True) is None
    assert skill._builder_llm_temperature_for_model("o4-mini") is None
    assert skill._builder_llm_temperature_for_model("gpt-4.1", repair=True) == 0.0


def test_builder_configures_fast_complete_json_for_gpt5(monkeypatch) -> None:
    skill = _load_module()

    monkeypatch.delenv("ADAOS_BUILDER_LLM_MAX_TOKENS", raising=False)
    assert skill._builder_llm_reasoning_for_model("gpt-5") == {"effort": "minimal"}
    assert skill._builder_llm_max_tokens_for_model("gpt-5") == 12000
    assert skill._builder_llm_reasoning_for_model("gpt-5-pro") is None
    assert skill._builder_llm_reasoning_for_model("gpt-4.1") is None
    assert skill._builder_llm_max_tokens_for_model("gpt-4.1") == 5000


def test_builder_llm_prompt_profile_tracks_provider_and_model(monkeypatch) -> None:
    skill = _load_module()

    monkeypatch.setenv("ADAOS_BUILDER_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("ADAOS_BUILDER_LLM_PROVIDER", "openai")
    monkeypatch.setenv("ADAOS_BUILDER_LLM_PROMPT_PROFILE", "default")

    profile = skill._builder_llm_prompt_profile()

    assert profile["schema"] == "adaos.builder.llm_prompt_profile.v1"
    assert profile["id"] == "default"
    assert profile["provider"] == "openai"
    assert profile["model"] == "gpt-4o-mini"
    assert profile["strategy"] == "compact_abi_plus_affordance_map"


def test_builder_llm_job_submit_timeout_default_allows_root_fallback(monkeypatch) -> None:
    skill = _load_module()

    monkeypatch.delenv("ADAOS_BUILDER_LLM_JOB_SUBMIT_TIMEOUT_S", raising=False)

    assert skill._builder_llm_job_submit_timeout_s() == 15.0


def test_update_current_scenario_uses_async_llm_job(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    import adaos.sdk.llm.llm_client as llm_client

    artifact_root = tmp_path / "async_llm"
    artifact_root.mkdir(parents=True)
    (artifact_root / "scenario.json").write_text(
        '{"id":"async_llm","version":"0.1.0","name":"async_llm","steps":[]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ADAOS_BUILDER_LLM_IN_TESTS", "1")
    monkeypatch.setenv("ADAOS_BUILDER_LLM_ASYNC_IN_TESTS", "1")
    monkeypatch.setenv("ADAOS_BUILDER_LLM_TEMPERATURE", "0.25")
    emitted: list[str] = []
    finished = threading.Event()
    refresh_calls: list[dict] = []

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {
                "source_webspace_id": kwargs.get("source_webspace_id"),
                "dev_webspace_id": "builder-async-dev",
                "active_draft_id": kwargs.get("active_draft_id"),
                "runtime_scenario_id": kwargs.get("runtime_scenario_id"),
            }

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(skill, "_publish_prompt_project_changed", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(skill, "_publish_prompt_project_selection", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(skill, "_publish_review_pending_action", lambda **kwargs: {"id": "pa.async"})
    monkeypatch.setattr(
        skill,
        "_schedule_dev_runtime_reload_after_revision",
        lambda webspace_id, **kwargs: refresh_calls.append({"webspace_id": webspace_id, **kwargs})
        or {"ok": True, "scheduled": True, "webspace_id": "builder-async-dev"},
    )

    def _safe_emit_chat(text, **_kwargs):
        emitted.append(str(text))
        if "Ревизия UI" in str(text):
            finished.set()

    monkeypatch.setattr(skill, "_safe_emit_chat", _safe_emit_chat)

    session = {
        "id": "builder_session_async",
        "webspace_id": "builder-async",
        "status": "drafting",
        "title": "Todo List",
        "scenario_id": "async_llm",
        "draft_id": "draft.async",
        "artifact_root": str(artifact_root),
        "datasource_id": "prototype_items",
        "fields": [
            {"id": "title", "type": "string", "label": "Title", "required": True},
            {"id": "notes", "type": "string", "label": "Notes", "required": False},
        ],
        "patches": [],
        "version": "001",
    }
    preview = skill._preview_state(session=session)
    preview["mock_data"] = {
        "prototype_items": [
            {"title": "Book venue", "notes": "Conference room"},
            {"title": "Invite speakers", "notes": "Send CFP reminders"},
        ]
    }
    page_schema = skill._page_schema_from_preview(preview)
    payload = {"schema": "adaos.webui.v1", "generated_by": "builder_skill", "ui": {"application": {"desktop": {"pageSchema": page_schema}}}}
    skill._save_session("builder-async", session)

    submit_calls: list[dict] = []

    def _submit_response_job(messages, **kwargs):
        submit_calls.append({"messages": list(messages), "kwargs": dict(kwargs)})
        return {
            "ok": True,
            "schema": "adaos.root.llm.job.v1",
            "job_id": "llm_job_async_test",
            "request_id": kwargs.get("request_id"),
            "status": "queued",
            "_client": {"base_url": "https://ru.api.inimatic.com"},
        }

    def _wait_response_job(job_id, **kwargs):
        assert job_id == "llm_job_async_test"
        assert kwargs["base_url"] == "https://ru.api.inimatic.com"
        return {
            "ok": True,
            "schema": "adaos.root.llm.job.v1",
            "job_id": job_id,
            "request_id": "builder-ui-telemetry-test",
            "status": "succeeded",
            "output_text": json.dumps({**payload, "comment": "Adapted conference sample data."}, ensure_ascii=False),
            "response": {
                "id": "resp_builder_telemetry_test",
                "status": "completed",
                "model": "gpt-5",
                "service_tier": "default",
                "usage": {
                    "input_tokens": 1200,
                    "input_tokens_details": {"cached_tokens": 768},
                    "output_tokens": 340,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 1540,
                },
            },
            "_protocol": {
                "timing": {"queue_ms": 12, "execution_ms": 3456, "total_ms": 3468},
                "usage": {
                    "input_tokens": 1200,
                    "cached_input_tokens": 768,
                    "output_tokens": 340,
                    "reasoning_tokens": 0,
                    "total_tokens": 1540,
                },
                "provider": {
                    "response_id": "resp_builder_telemetry_test",
                    "service_tier": "default",
                    "upstream_request_id": "req_builder_telemetry_test",
                },
                "tools": {"requested_count": 0, "used_count": 0, "output_type_counts": {"message": 1}},
                "mcp": {"used_mcp": False, "item_count": 0, "items": []},
            },
        }

    monkeypatch.setattr(llm_client, "submit_response_job", _submit_response_job)
    monkeypatch.setattr(llm_client, "wait_response_job", _wait_response_job)

    result = skill.update_current_scenario(
        "Адаптируй пример данных для списка задач по подготовке к конференции",
        webspace_id="builder-async",
    )

    assert result["status"] == "llm_pending"
    assert result["llm_job"]["job_id"] == "llm_job_async_test"
    assert result["llm_job"]["local_job_id"].startswith("builder_llm_submit_")
    assert result["message_meta"]["progress_group_id"] == "llm_job_async_test"
    assert result["message_meta"]["progress_phase"] == "accepted"
    assert result["message_meta"]["progress_seq"] == 0
    assert submit_calls
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not refresh_calls:
        time.sleep(0.02)
    assert refresh_calls, emitted
    assert submit_calls
    webui = json.loads((artifact_root / "webui.json").read_text(encoding="utf-8-sig"))
    assert webui["schema"] == "adaos.webui.v1"
    assert "preview_state" not in webui
    widgets = webui["ui"]["application"]["desktop"]["pageSchema"]["widgets"]
    table = next(item for item in widgets if item["id"] == "prototype-table")
    rows = table["dataSource"]["value"]
    assert rows[0]["title"] == "Book venue"
    assert rows[1]["title"] == "Invite speakers"
    revision_files = sorted((artifact_root / "ui_revisions").glob("*.json"))
    assert revision_files
    revision = json.loads(revision_files[-1].read_text(encoding="utf-8"))
    assert revision["inference"]["response_id"] == "resp_builder_telemetry_test"
    assert revision["inference"]["service_tier"] == "default"
    telemetry = revision["llm"]["telemetry"]
    assert telemetry["timing"]["execution_ms"] == 3456
    assert telemetry["usage"]["cached_input_tokens"] == 768
    assert telemetry["provider"]["upstream_request_id"] == "req_builder_telemetry_test"
    assert telemetry["tools"]["used_count"] == 0
    assert telemetry["mcp"]["used_mcp"] is False
    assert refresh_calls and refresh_calls[0]["revision"] == "001"
    assert submit_calls[0]["kwargs"]["request_id"].startswith("builder-ui-")
    assert "-job-" in submit_calls[0]["kwargs"]["request_id"]
    assert submit_calls[0]["kwargs"]["temperature"] == 0.25
    assert "prototyping_affordances" in submit_calls[0]["messages"][1]["content"]
    assert "current_webui_json" in submit_calls[0]["messages"][2]["content"]


def test_update_current_scenario_blocks_parallel_llm_jobs(monkeypatch) -> None:
    skill = _load_module()

    monkeypatch.setenv("ADAOS_BUILDER_LLM_IN_TESTS", "1")
    monkeypatch.setenv("ADAOS_BUILDER_LLM_ASYNC_IN_TESTS", "1")
    started = time.time()
    session = {
        "id": "builder_session_busy",
        "webspace_id": "desktop",
        "status": "drafting",
        "title": "Busy Prototype",
        "scenario_id": "busy_scenario",
        "draft_id": "draft.busy",
        "artifact_root": "unused",
        "fields": [{"id": "title", "type": "string", "label": "Title"}],
        "patches": [],
        "pending_llm_jobs": {
            "builder_llm_submit_busy": {
                "schema": "adaos.builder.llm_job.v1",
                "job_id": "builder_llm_submit_busy",
                "status": "submitted",
                "root_job_id": "llm_job_busy",
                "created_at": started,
            },
            "llm_job_busy": {
                "schema": "adaos.builder.llm_job.v1",
                "job_id": "llm_job_busy",
                "local_job_id": "builder_llm_submit_busy",
                "status": "running",
                "created_at": started,
            },
        },
    }
    binding = {
        "source_webspace_id": "desktop",
        "dev_webspace_id": "desktop-dev",
        "runtime_scenario_id": "busy_scenario",
        "active_draft_id": "draft.busy",
    }

    monkeypatch.setattr(skill, "_target_session", lambda _ws: (session, binding))
    monkeypatch.setattr(skill, "_dialog_state", lambda *args, **kwargs: {"messages": []})

    def _unexpected_start_worker(**_kwargs):
        raise AssertionError("parallel LLM worker must not start")

    monkeypatch.setattr(skill, "_start_llm_webui_job_worker", _unexpected_start_worker)

    result = skill.update_current_scenario("Добавь поле", webspace_id="desktop")

    assert result["status"] == "llm_busy"
    assert result["active_llm_job"]["job_id"] == "llm_job_busy"

    skill._update_llm_job_status(session, "llm_job_busy", "succeeded")
    assert skill._active_llm_job(session) is None
    assert session["pending_llm_jobs"]["builder_llm_submit_busy"]["status"] == "succeeded"
    assert session["pending_llm_jobs"]["llm_job_busy"]["status"] == "succeeded"


def test_llm_job_link_normalises_terminal_status_after_stale_session_load() -> None:
    skill = _load_module()
    session = {
        "id": "builder_session_race",
        "pending_llm_jobs": {
            "builder_llm_submit_race": {
                "schema": "adaos.builder.llm_job.v1",
                "job_id": "builder_llm_submit_race",
                "status": "submitting",
                "request_text": "change ui",
                "created_at": time.time(),
            }
        },
    }

    skill._ensure_llm_job_link(
        session,
        local_job_id="builder_llm_submit_race",
        root_job_id="llm_job_race",
        request_id="request-race",
        base_url="https://api.inimatic.com",
        request_text="change ui",
        patch_id="patch-race",
        status="submitted",
    )
    skill._update_llm_job_status(session, "llm_job_race", "succeeded")

    assert skill._active_llm_job(session) is None
    assert session["pending_llm_jobs"]["builder_llm_submit_race"]["status"] == "succeeded"
    assert session["pending_llm_jobs"]["llm_job_race"]["status"] == "succeeded"
    assert session["pending_llm_jobs"]["builder_llm_submit_race"]["root_job_id"] == "llm_job_race"
    assert session["pending_llm_jobs"]["llm_job_race"]["local_job_id"] == "builder_llm_submit_race"


def test_active_llm_job_reconciles_orphan_local_job_from_ui_revision(tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "prototype"
    revision_dir = artifact_root / "ui_revisions"
    revision_dir.mkdir(parents=True)
    (revision_dir / "001.json").write_text(
        json.dumps(
            {
                "schema": "adaos.builder.ui_revision.v1",
                "revision": "001",
                "request": {"text": "change ui"},
                "patch": {
                    "id": "patch-race",
                    "operation": "llm_webui_transform",
                    "diff": {
                        "attempts": [
                            {
                                "attempt": 1,
                                "ok": True,
                                "request_id": "request-race",
                                "job_id": "llm_job_race",
                            }
                        ]
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = {
        "id": "builder_session_race",
        "artifact_root": str(artifact_root),
        "pending_llm_jobs": {
            "builder_llm_submit_race": {
                "schema": "adaos.builder.llm_job.v1",
                "job_id": "builder_llm_submit_race",
                "status": "submitting",
                "request_text": "change ui",
                "patch_id": "patch-race",
                "created_at": time.time(),
            }
        },
    }

    assert skill._active_llm_job(session) is None
    assert session["pending_llm_jobs"]["builder_llm_submit_race"]["status"] == "succeeded"
    assert session["pending_llm_jobs"]["llm_job_race"]["status"] == "succeeded"


def test_save_session_merges_pending_llm_jobs_without_downgrading_terminal_state(caplog) -> None:
    skill = _load_module()
    skill._FALLBACK_MEMORY.clear()
    webspace_id = "builder-merge-jobs"
    skill._save_session(
        webspace_id,
        {
            "id": "builder_session_merge",
            "pending_llm_jobs": {
                "builder_llm_submit_merge": {
                    "schema": "adaos.builder.llm_job.v1",
                    "job_id": "builder_llm_submit_merge",
                    "status": "submitted",
                    "root_job_id": "llm_job_merge",
                    "request_text": "change ui",
                    "patch_id": "patch-merge",
                    "created_at": time.time(),
                },
                "llm_job_merge": {
                    "schema": "adaos.builder.llm_job.v1",
                    "job_id": "llm_job_merge",
                    "local_job_id": "builder_llm_submit_merge",
                    "status": "succeeded",
                    "request_text": "change ui",
                    "patch_id": "patch-merge",
                    "finished_at": time.time(),
                },
            },
        },
    )

    caplog.set_level(logging.WARNING, logger=skill._LOG.name)
    skill._save_session(
        webspace_id,
        {
            "id": "builder_session_merge",
            "scenario_id": "merge_scenario",
            "pending_llm_jobs": {
                "builder_llm_submit_merge": {
                    "schema": "adaos.builder.llm_job.v1",
                    "job_id": "builder_llm_submit_merge",
                    "status": "submitting",
                    "request_text": "change ui",
                    "patch_id": "patch-merge",
                    "created_at": time.time(),
                }
            },
        },
    )

    loaded = skill._load_session(webspace_id, "builder_session_merge")
    assert loaded is not None
    pending = loaded["pending_llm_jobs"]
    assert pending["builder_llm_submit_merge"]["status"] == "succeeded"
    assert pending["builder_llm_submit_merge"]["root_job_id"] == "llm_job_merge"
    assert pending["llm_job_merge"]["status"] == "succeeded"
    assert pending["llm_job_merge"]["local_job_id"] == "builder_llm_submit_merge"
    assert "builder pending LLM job state race ignored" in caplog.text
    assert "merge_scenario" in caplog.text


def test_submit_llm_webui_transform_job_retries_request_id_conflict(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    import adaos.sdk.llm.llm_client as llm_client
    from adaos.services.root.client import RootHttpError

    artifact_root = tmp_path / "llm_conflict"
    artifact_root.mkdir(parents=True)
    session = {
        "id": "builder_session_conflict",
        "scenario_id": "llm_conflict",
        "artifact_root": str(artifact_root),
        "datasource_id": "prototype_items",
        "fields": [{"id": "title", "type": "string", "label": "Title"}],
    }
    preview = skill._preview_state(session=session)
    calls: list[str] = []

    def _submit_response_job(_messages, **kwargs):
        request_id = str(kwargs.get("request_id") or "")
        calls.append(request_id)
        if len(calls) == 1:
            raise RootHttpError(
                "llm_request_id_conflict",
                status_code=409,
                error_code="llm_request_id_conflict",
                payload={"code": "llm_request_id_conflict"},
            )
        return {
            "ok": True,
            "schema": "adaos.root.llm.job.v1",
            "job_id": "llm_job_conflict_retry",
            "request_id": request_id,
            "status": "queued",
            "_client": {"base_url": "https://ru.api.inimatic.com"},
        }

    monkeypatch.setattr(llm_client, "submit_response_job", _submit_response_job)

    result = skill._submit_llm_webui_transform_job(
        session=session,
        instruction="Add full name field",
        preview_state=preview,
        job_nonce="builder_llm_submit_deadbeef",
    )

    assert result["ok"] is True
    assert result["pending"] is True
    assert result["job_id"] == "llm_job_conflict_retry"
    assert result["request_id"] == calls[1]
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert all(item.startswith("builder-ui-") and "-job-" in item for item in calls)
    attempts = result["timing"]["submit_attempts"]
    assert attempts[0]["ok"] is False
    assert attempts[0]["error"] == "llm_request_id_conflict"
    assert attempts[1]["ok"] is True


def test_builder_llm_request_includes_runtime_context_and_project_prompt(tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "llm_context"
    artifact_root.mkdir(parents=True)
    page_schema = {
        "id": "llm_context",
        "title": "Todo List",
        "layout": {"type": "split", "areas": [{"id": "main"}, {"id": "right"}]},
        "widgets": [
            {
                "id": "prototype-cards",
                "type": "ui.list",
                "area": "main",
                "inputs": {"variant": "cards", "titleKey": "title", "subtitleKey": "notes", "previewKey": "status"},
            }
        ],
    }
    (artifact_root / "scenario.json").write_text(
        json.dumps({"id": "llm_context", "name": "llm_context", "ui": {"application": {"desktop": {"pageSchema": page_schema}}}}),
        encoding="utf-8",
    )
    (artifact_root / "builder_system_prompt.md").write_text("Always prefer conference vocabulary.\n", encoding="utf-8")
    before_page_schema = copy.deepcopy(page_schema)
    before_page_schema["widgets"] = [
        {
            "id": "prototype-cards",
            "type": "ui.list",
            "area": "main",
            "inputs": {"variant": "cards", "titleKey": "title", "subtitleKey": "notes", "previewKey": "status"},
            "actions": [
                {"on": "select", "type": "updateState", "params": {"selectedId": "$event.id"}},
                {"on": "select", "type": "openModal", "params": {"modalId": "request_detail_modal"}},
            ],
        }
    ]
    before_webui = {
        "schema": "adaos.webui.v1",
        "ui": {
            "application": {
                "desktop": {"pageSchema": before_page_schema},
                "modals": {
                    "request_detail_modal": {
                        "title": "Request details",
                        "schema": {
                            "id": "request_detail_modal_schema",
                            "layout": {"type": "single", "areas": [{"id": "modal-main"}]},
                            "widgets": [
                                {
                                    "id": "add-comment-action",
                                    "type": "ui.actions",
                                    "area": "modal-main",
                                    "title": "Add comment",
                                    "actions": [
                                        {"on": "click", "type": "openModal", "params": {"modalId": "comment_modal"}}
                                    ],
                                }
                            ],
                        },
                    },
                    "comment_modal": {
                        "title": "Add comment",
                        "schema": {
                            "id": "comment_modal_schema",
                            "layout": {"type": "single", "areas": [{"id": "form"}]},
                            "widgets": [{"id": "comment-form", "type": "ui.form", "area": "form"}],
                        },
                    },
                },
            }
        },
    }
    after_webui = {"schema": "adaos.webui.v1", "ui": {"application": {"desktop": {"pageSchema": page_schema}}}}
    revision_dir = artifact_root / "ui_revisions"
    revision_dir.mkdir()
    (revision_dir / "current.txt").write_text("002\n", encoding="utf-8")
    (revision_dir / "002.json").write_text(
        json.dumps(
            {
                "revision": "002",
                "request": {"text": "Move request details into a right panel"},
                "before_webui": before_webui,
                "after_webui": after_webui,
                "preview_state": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = {
        "id": "builder_session_context",
        "scenario_id": "llm_context",
        "artifact_root": str(artifact_root),
        "ui_revision": "002",
        "datasource_id": "prototype_items",
        "fields": [{"id": "title", "type": "string", "label": "Title"}],
        "user_summary": {"assumptions": ["The first data model uses fields: Title, Notes, Status"]},
    }
    preview = skill._preview_state(session=session)

    request = skill._builder_llm_webui_transform_request(
        session=session,
        instruction="Add date to cards",
        preview_state=preview,
    )

    stable_payload = json.loads(request["stable_user_prompt"])["stable_builder_context"]
    dynamic_payload = json.loads(request["user_prompt"])["builder_request"]
    user_payload = {**stable_payload, **dynamic_payload}
    current = user_payload["current_webui_json"]
    assert current["schema"] == "adaos.webui.v1"
    assert "preview_state" not in current
    assert current["ui"]["application"]["desktop"]["pageSchema"]["widgets"][0]["id"] == "prototype-cards"
    assert user_payload["llm_prompt_profile"]["schema"] == "adaos.builder.llm_prompt_profile.v1"
    assert user_payload["llm_prompt_profile"]["id"] == "default"
    assert user_payload["llm_prompt_profile"]["variant_policy"].startswith("Prompt profiles may vary")
    assert "current_page_schema" not in user_payload["runtime_context"]
    assert user_payload["current_webui_json"]["ui"]["application"]["desktop"]["pageSchema"]["widgets"][0]["inputs"]["previewKey"] == "status"
    assert user_payload["runtime_component_contracts"]["ui.list"]["inputs"]["previewKey"].startswith("Single object path")
    assert user_payload["runtime_component_contracts"]["ui.list"]["inputs"]["addButton"].startswith("Set true")
    assert "per-item/card commands" in request["system_prompt"]
    assert "preserve unrelated widgets" in request["system_prompt"]
    assert "input.commandBar" in user_payload["runtime_component_contracts"]
    assert "state_and_visibility" in user_payload["runtime_component_contracts"]
    assert "visibleIf" in user_payload["runtime_component_contracts"]["state_and_visibility"]
    assert "view an example" in user_payload["runtime_component_contracts"]["state_and_visibility"]["local_interaction"]
    delta = user_payload["last_revision_delta"]
    assert delta["revision"] == "002"
    assert delta["request"] == "Move request details into a right panel"
    assert {"id": "comment_modal", "title": "Add comment", "presentation": ""} in delta["removed_modals"]
    removed_by_id = {item["id"]: item for item in delta["removed_widgets"]}
    assert removed_by_id["add-comment-action"]["owner"] == "modal:request_detail_modal"
    assert removed_by_id["add-comment-action"]["opens_modals"] == ["comment_modal"]
    affordances = user_payload["prototyping_affordances"]
    assert affordances["role"] == "Adaptive UI prototyping designer-programmer."
    assert "separate requirement" in affordances["meaningful_transformation"][1]
    assert "visible semantic change" in affordances["meaningful_transformation"][2]
    assert "mock_data" in affordances["ui_freedom_map"]
    form_contract = user_payload["runtime_component_contracts"]["ui.form"]["inputs"]["fields"]
    assert "email" in form_contract["supported_field_types"]
    assert "textarea" in form_contract["supported_field_types"]
    assert "dateRange" in form_contract["supported_field_types"]
    assert "multiChoice" in form_contract["supported_field_types"]
    assert "fileUpload" in form_contract["supported_field_types"]
    assert "ratingGrid" in form_contract["supported_field_types"]
    assert "Choose the most semantically precise supported type" in form_contract["selection_guidance"][0]
    assert "Refactor existing generic text fields" in form_contract["selection_guidance"][1]
    assert "do not leave contacts" in form_contract["selection_guidance"][2]
    assert "every requested user answer" in form_contract["selection_guidance"][-1]
    assert form_contract["semantic_examples"]["contacts"].startswith("email plus phone")
    assert form_contract["semantic_examples"]["convenient dates or date interval"] == "dateRange"
    assert form_contract["semantic_examples"]["rate several factors"] == "ratingGrid or linearScale fields"
    assert form_contract["semantic_examples"]["mark choices by days/sections/categories"] == "checkboxGrid or radioGrid"
    assert "atomic fields" in affordances["ui_freedom_map"]["forms"]
    assert "explicit local control" in affordances["ui_freedom_map"]["interaction"]
    assert "input.commandBar/input.selector/ui.actions" in " ".join(affordances["self_check"])
    assert "static mock rows alone are not enough" in " ".join(affordances["self_check"])
    assert "internal checklist" in " ".join(affordances["self_check"])
    command_bar_contract = user_payload["runtime_component_contracts"]["input.commandBar"]
    command_bar_pattern = command_bar_contract["example_pattern"]
    assert command_bar_pattern["initialState"] == {"exampleMode": "empty"}
    assert command_bar_pattern["widgets"][0]["actions"][0]["params"] == {"exampleMode": "$event.id"}
    assert command_bar_pattern["widgets"][1]["visibleIf"] == "$state.exampleMode === 'sample'"
    schema_defs = user_payload["webui_v1_abi"]["schema_contract"]["defs"]
    assert "formInputs" in schema_defs
    assert "formField" in schema_defs
    assert "formInputType" in schema_defs
    assert "formFieldType" in schema_defs
    assert "email" in schema_defs["formInputType"]["enum"]
    assert "ratingGrid" in schema_defs["formInputType"]["enum"]
    assert "Always prefer conference vocabulary" in request["system_prompt"]
    assert "adaptive UI prototyping designer-programmer" in request["system_prompt"]
    assert "meaningful visible changes" in request["system_prompt"]
    assert "duplicate-only" in request["system_prompt"]
    assert "field's required 'type' property" in request["system_prompt"]
    assert "static content or sample rows" in request["system_prompt"]
    assert "Decompose the user's instruction into explicit requirements" in request["system_prompt"]
    assert "visibly reacts to the local state" in request["system_prompt"]
    assert "Do not preserve an existing generic text field" in request["system_prompt"]
    assert "Break broad or composite user concepts into atomic fields" in request["system_prompt"]
    assert "add an explicit local control" in request["system_prompt"]
    assert "data-capture requirements that need ui.form fields" in request["system_prompt"]
    assert "local development prototype until an explicit activation/release step" in request["system_prompt"]
    assert "meaningless placeholders like Request 1" in request["system_prompt"]
    assert "Static sample rows must match the active domain" in request["system_prompt"]
    assert (artifact_root / "builder_memory.md").exists()
    assert (artifact_root / "tz" / "base_tz.md").exists()
    assert "starting point only" in user_payload["project_memory"]["memory_text"]
    assert "not a fixed product contract" in user_payload["project_memory"]["user_summary"]["assumptions"][0]
    assert "local dev prototype" not in user_payload["project_memory"]["memory_text"]
    assert len(request["user_prompt"].encode("utf-8")) < 50_000


def test_builder_form_component_contract_validates_choice_and_grid_fields() -> None:
    skill = _load_module()

    missing_options = {
        "id": "form_contract",
        "widgets": [
            {
                "id": "form",
                "type": "ui.form",
                "inputs": {"fields": [{"id": "topics", "type": "multiChoice", "label": "Topics"}]},
            }
        ],
    }
    assert skill._validate_page_schema_component_contracts(missing_options)["ok"] is False

    missing_grid_columns = {
        "id": "form_contract",
        "widgets": [
            {
                "id": "form",
                "type": "ui.form",
                "inputs": {
                    "fields": [
                        {
                            "id": "session_interest",
                            "type": "checkboxGrid",
                            "label": "Session interest",
                            "rows": [{"label": "Day 1", "value": "day_1"}],
                        }
                    ]
                },
            }
        ],
    }
    grid_result = skill._validate_page_schema_component_contracts(missing_grid_columns)
    assert grid_result["ok"] is False
    assert "rows and columns" in grid_result["detail"]

    valid = copy.deepcopy(missing_grid_columns)
    valid["widgets"][0]["inputs"]["fields"][0]["columns"] = [{"label": "Urban planning", "value": "urban"}]
    valid["widgets"][0]["inputs"]["fields"].append(
        {
            "id": "format",
            "type": "radio",
            "label": "Format",
            "choices": [{"label": "Online", "value": "online"}],
        }
    )
    assert skill._validate_page_schema_component_contracts(valid)["ok"] is True


def test_builder_project_memory_repairs_mojibake_and_legacy_constraints(tmp_path) -> None:
    skill = _load_module()

    artifact_root = tmp_path / "prototype"
    (artifact_root / "tz").mkdir(parents=True)
    mojibake_fields = "РќР°Р·РІР°РЅРёРµ, Р—Р°РјРµС‚РєРё, РЎС‚Р°С‚СѓСЃ"
    (artifact_root / "builder_memory.md").write_text(
        f"# Memory\n- This is a local dev prototype, not an activated runtime change\n- The first data model uses fields: {mojibake_fields}\n",
        encoding="utf-8",
    )
    (artifact_root / "tz" / "base_tz.md").write_text(
        f"# Spec\n- The first data model uses fields: {mojibake_fields}\n",
        encoding="utf-8",
    )
    (artifact_root / "prompt_state.json").write_text(
        json.dumps({"base_tz": f"The first data model uses fields: {mojibake_fields}"}, ensure_ascii=False),
        encoding="utf-8",
    )

    memory = skill._project_memory(
        {
            "artifact_root": str(artifact_root),
            "user_summary": {"assumptions": [f"The first data model uses fields: {mojibake_fields}"]},
        }
    )

    assert "Рќ" not in memory["memory_text"]
    assert "Название, Заметки, Статус" in memory["memory_text"]
    assert "not a fixed product contract" in memory["memory_text"]
    assert "local dev prototype" not in memory["memory_text"]
    assert "Рќ" not in memory["technical_spec_text"]
    assert "Название, Заметки, Статус" in memory["technical_spec_text"]
    assert "not a fixed product contract" in memory["user_summary"]["assumptions"][0]

    skill._ensure_builder_project_files(artifact_root, {"title": "Prototype"})

    memory_file_text = (artifact_root / "builder_memory.md").read_text(encoding="utf-8")
    tz_file_text = (artifact_root / "tz" / "base_tz.md").read_text(encoding="utf-8")
    state = json.loads((artifact_root / "prompt_state.json").read_text(encoding="utf-8"))
    assert "Рќ" not in memory_file_text
    assert "Название, Заметки, Статус" in memory_file_text
    assert "not a fixed product contract" in tz_file_text
    assert "local dev prototype" not in memory_file_text
    assert "Рќ" not in state["base_tz"]
    assert "not a fixed product contract" in state["base_tz"]


def test_builder_webui_title_uses_scenario_yaml_as_canonical_metadata(tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "prototype_app_4d5758e5"
    artifact_root.mkdir(parents=True)
    (artifact_root / "scenario.yaml").write_text(
        "\n".join(
            [
                "id: prototype_app_4d5758e5",
                "name: prototype_app_4d5758e5",
                "type: desktop",
                "title: Prototype App E5",
                "title_i18n:",
                "  key: scenario.prototype_app_4d5758e5.title",
                "  fallback: Prototype App E5",
                "version: 0.1.0",
                "depends: []",
                "runtime:",
                "  skills:",
                "    required: []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    stale_page_schema = {
        "id": "prototype_app_4d5758e5",
        "title": "Latency Probe C 2df367",
        "layout": {"type": "stack", "areas": [{"id": "main"}]},
        "widgets": [{"id": "prototype-form", "type": "ui.form", "area": "main", "inputs": {"fields": []}}],
    }
    (artifact_root / "scenario.json").write_text(
        json.dumps(
            {
                "id": "prototype_app_4d5758e5",
                "name": "prototype_app_4d5758e5",
                "type": "desktop",
                "title": "Latency Probe C 2df367",
                "ui": {"application": {"desktop": {"pageSchema": stale_page_schema}}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (artifact_root / "webui.json").write_text(
        json.dumps(
            {
                "schema": "adaos.webui.v1",
                "ui": {"application": {"desktop": {"pageSchema": stale_page_schema}}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = {
        "id": "builder_session_title",
        "scenario_id": "prototype_app_4d5758e5",
        "artifact_root": str(artifact_root),
        "datasource_id": "prototype_items",
        "fields": [{"id": "title", "type": "string", "label": "Title"}],
    }
    preview = skill._preview_state(session=session)

    current = skill._current_webui_payload(session, preview)

    current_page_schema = current["ui"]["application"]["desktop"]["pageSchema"]
    assert current_page_schema["title"] == "Prototype App E5"
    assert current_page_schema["title_i18n"]["key"] == "scenario.prototype_app_4d5758e5.title"

    updated_page_schema = copy.deepcopy(current_page_schema)
    updated_page_schema["title"] = "City Growth Survey"
    payload = {
        "schema": "adaos.webui.v1",
        "ui": {"application": {"desktop": {"pageSchema": updated_page_schema}}},
    }
    skill._write_webui_payload(str(artifact_root), payload)

    saved_webui = json.loads((artifact_root / "webui.json").read_text(encoding="utf-8"))
    saved_scenario_json = json.loads((artifact_root / "scenario.json").read_text(encoding="utf-8"))
    saved_yaml = yaml.safe_load((artifact_root / "scenario.yaml").read_text(encoding="utf-8"))
    assert saved_webui["ui"]["application"]["desktop"]["pageSchema"]["title"] == "City Growth Survey"
    assert saved_scenario_json["title"] == "City Growth Survey"
    assert saved_scenario_json["ui"]["application"]["desktop"]["pageSchema"]["title"] == "City Growth Survey"
    assert saved_yaml["title"] == "City Growth Survey"


def test_async_llm_completion_repairs_missing_page_schema(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    import adaos.sdk.llm.llm_client as llm_client

    artifact_root = tmp_path / "repair_missing_page_schema"
    artifact_root.mkdir(parents=True)
    (artifact_root / "scenario.json").write_text(
        json.dumps({"id": "repair_missing_page_schema", "name": "repair_missing_page_schema", "version": "0.1.0"}),
        encoding="utf-8",
    )
    session = {
        "id": "builder_session_repair",
        "webspace_id": "builder-repair",
        "status": "drafting",
        "title": "Repair App",
        "scenario_id": "repair_missing_page_schema",
        "draft_id": "draft.repair",
        "artifact_root": str(artifact_root),
        "datasource_id": "prototype_items",
        "fields": [{"id": "title", "type": "string", "label": "Title"}],
        "patches": [],
        "version": "001",
        "pending_llm_jobs": {"llm_job_repair": {"status": "queued"}},
    }
    preview = skill._preview_state(session=session)
    page_schema = skill._page_schema_from_preview(preview)
    payload = {"schema": "adaos.webui.v1", "generated_by": "builder_skill", "ui": {"application": {"desktop": {"pageSchema": page_schema}}}}
    skill._save_session("builder-repair", session)
    emitted: list[str] = []

    monkeypatch.setattr(skill, "_workbench_service", lambda: type("_Workbench", (), {
        "set_active_draft": lambda self, **kwargs: {"dev_webspace_id": "builder-repair-dev", "active_draft_id": kwargs.get("active_draft_id")},
        "snapshot": lambda self, *args, **kwargs: {"preview_state": kwargs.get("preview_state") or {}},
    })())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(skill, "_publish_prompt_project_changed", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(skill, "_publish_prompt_project_selection", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(skill, "_publish_review_pending_action", lambda **kwargs: {"id": "pa.repair"})
    monkeypatch.setattr(skill, "_schedule_dev_runtime_reload_after_revision", lambda *args, **kwargs: {"ok": True, "scheduled": True})
    monkeypatch.setattr(skill, "_safe_emit_chat", lambda text, **_kwargs: emitted.append(str(text)))

    wait_calls = []

    def fake_wait_response_job(job_id, *args, **kwargs):
        wait_calls.append(str(job_id))
        if str(job_id) == "llm_job_repair_fix":
            return {
                "ok": True,
                "job_id": "llm_job_repair_fix",
                "status": "succeeded",
                "output_text": json.dumps({**payload, "comment": "Repaired."}, ensure_ascii=False),
            }
        return {
            "ok": True,
            "job_id": "llm_job_repair",
            "status": "succeeded",
            "output_text": json.dumps({"comment": "Missing page schema."}, ensure_ascii=False),
        }

    monkeypatch.setattr(llm_client, "wait_response_job", fake_wait_response_job)
    monkeypatch.setattr(
        llm_client,
        "submit_response_job",
        lambda *args, **kwargs: {
            "ok": True,
            "job_id": "llm_job_repair_fix",
            "status": "queued",
            "_client": {"base_url": "https://ru.api.inimatic.com"},
        },
    )

    skill._complete_llm_webui_job(
        ws="builder-repair",
        session_id="builder_session_repair",
        binding={},
        patch={
            "id": "patch_repair",
            "target": "ui",
            "operation": "noop",
            "status": "applied",
            "created_by": "llm_agent",
            "created_at": time.time(),
            "summary": "add field",
            "diff": {},
        },
        request_text="add field",
        before_webui={"preview_state": preview},
        job_id="llm_job_repair",
        base_url="https://ru.api.inimatic.com",
        request_id="builder-ui-repair",
        auto_apply=True,
        _meta={},
    )

    assert emitted
    assert "llm_job_repair_fix" in wait_calls
    assert not any("LLM payload must contain ui.application.desktop.pageSchema" in item for item in emitted)
    saved = json.loads((artifact_root / "webui.json").read_text(encoding="utf-8"))
    assert saved["ui"]["application"]["desktop"]["pageSchema"]["id"] == page_schema["id"]


def test_normalise_llm_payload_uses_webui_page_schema_as_source_of_truth() -> None:
    skill = _load_module()
    previous_page_schema = {
        "id": "todo",
        "layout": {"type": "split", "areas": [{"id": "main"}, {"id": "right"}]},
        "widgets": [
            {"id": "prototype-form", "type": "ui.form", "area": "main", "inputs": {"fields": []}},
            {
                "id": "prototype-cards",
                "type": "ui.list",
                "area": "right",
                "inputs": {"variant": "cards", "titleKey": "title", "subtitleKey": "notes", "previewKey": "status"},
            },
        ],
    }
    previous_preview = {
        "title": "Todo",
        "page_schema": previous_page_schema,
        "current_ui": {
            "id": "todo",
            "type": "page",
            "children": [
                {"id": "editor", "type": "section", "children": []},
                {"id": "items_cards", "type": "card_list", "title": "{{title}}", "subtitle": "{{notes}}", "preview": "{{date}}"},
            ],
        },
        "datasources": [
            {
                "id": "prototype_items",
                "fields": [
                    {"id": "title", "type": "string", "label": "Title"},
                    {"id": "notes", "type": "string", "label": "Notes"},
                    {"id": "date", "type": "date", "label": "Date"},
                ],
            }
        ],
        "mock_data": {"prototype_items": [{"title": "Talk", "notes": "CFP", "date": "2026-07-02"}]},
        "layout_order": "input_first",
    }
    next_page_schema = {
        "id": "todo",
        "title": "Todo",
        "layout": {"type": "split", "areas": [{"id": "main"}, {"id": "right"}]},
        "widgets": [
            {
                "id": "prototype-cards",
                "type": "ui.list",
                "area": "main",
                "inputs": {"variant": "cards", "titleKey": "title", "subtitleKey": "notes", "previewKey": "date"},
            },
            {"id": "prototype-form", "type": "ui.form", "area": "right", "inputs": {"fields": []}},
        ],
    }
    parsed = {
        "schema": "adaos.webui.v1",
        "generated_by": "builder_skill",
        "ui": {"application": {"desktop": {"pageSchema": next_page_schema}}},
    }

    _payload, preview = skill._normalise_llm_webui_payload(parsed, previous_preview=previous_preview)
    assert _payload["schema"] == "adaos.webui.v1"
    assert "preview_state" not in _payload
    assert "current_ui" not in preview
    assert preview["page_schema"] == next_page_schema
    form = next(item for item in preview["page_schema"]["widgets"] if item["id"] == "prototype-form")
    cards = next(item for item in preview["page_schema"]["widgets"] if item["id"] == "prototype-cards")
    assert form["area"] == "right"
    assert cards["area"] == "main"
    assert cards["inputs"]["previewKey"] == "date"


def test_normalise_llm_payload_accepts_webui_schema_wrapper() -> None:
    skill = _load_module()
    page_schema = {
        "id": "survey",
        "layout": {"type": "split", "areas": [{"id": "main"}]},
        "widgets": [
            {
                "id": "prototype-form",
                "type": "ui.form",
                "area": "main",
                "inputs": {
                    "fields": [
                        {"id": "fio", "label": "ФИО", "type": "text"},
                        {
                            "id": "gender",
                            "label": "Пол",
                            "type": "select",
                            "options": [
                                {"label": "Мужской", "value": "male"},
                                {"label": "Женский", "value": "female"},
                            ],
                        },
                    ]
                },
            }
        ],
    }
    parsed = {
        "adaos.webui.v1": {
            "schema": "adaos.webui.v1",
            "ui": {"application": {"desktop": {"pageSchema": page_schema}}},
        }
    }

    payload, preview = skill._normalise_llm_webui_payload(parsed, previous_preview={"title": "Survey"})

    assert payload["schema"] == "adaos.webui.v1"
    fields = preview["page_schema"]["widgets"][0]["inputs"]["fields"]
    assert fields[0]["id"] == "fio"
    assert fields[1]["id"] == "gender"
    assert fields[1]["options"][0]["value"] == "male"


def test_normalise_llm_payload_moves_root_modals_into_application() -> None:
    skill = _load_module()
    page_schema = {
        "id": "request_center",
        "layout": {"type": "split", "areas": [{"id": "main"}]},
        "widgets": [
            {
                "id": "open-comment",
                "type": "ui.actions",
                "area": "main",
                "actions": [{"on": "click", "type": "openModal", "params": {"modalId": "comment_modal"}}],
            }
        ],
    }
    parsed = {
        "schema": "adaos.webui.v1",
        "ui": {"application": {"desktop": {"pageSchema": page_schema}}},
        "modals": {
            "comment_modal": {
                "title": "Add comment",
                "schema": {"id": "comment_modal_schema", "layout": {"type": "stack"}, "widgets": []},
            }
        },
    }

    payload, preview = skill._normalise_llm_webui_payload(parsed, previous_preview={"title": "Requests"})

    assert "modals" not in payload
    assert payload["ui"]["application"]["modals"]["comment_modal"]["title"] == "Add comment"
    assert preview["page_schema"] == page_schema
    assert skill._validate_builder_webui_payload(payload, preview)["ok"] is True


def test_builder_system_prompt_allows_replaceable_picsum_placeholders() -> None:
    skill = _load_module()

    prompt = skill._builder_llm_system_prompt()

    assert "https://picsum.photos/" in prompt
    assert "replaceable placeholder image URLs" in prompt
    assert "local seed assets or generated images" in prompt


def test_builder_patch_prompt_distinguishes_add_from_replace() -> None:
    skill = _load_module()

    prompt = skill._builder_llm_system_prompt(output_mode="jsonl_patch_v1")

    assert "Use add when creating a missing object member" in prompt
    assert "replace only when the target member already exists" in prompt
    assert "RFC 6902 does not create intermediate containers" in prompt


def test_builder_component_contract_describes_nested_auto_action_shape() -> None:
    skill = _load_module()

    contract = skill._builder_runtime_component_contracts()["page_schema_auto_actions"]

    assert "action:{type,params?,...}" in contract["shape"]
    assert "nested action property is required" in contract["shape"]


def test_builder_patch_stream_applies_to_shadow_and_preserves_unrelated_ui() -> None:
    skill = _load_module()
    before = {
        "schema": "adaos.webui.v1",
        "ui": {
            "application": {
                "desktop": {
                    "pageSchema": {
                        "id": "recipes",
                        "title": "Recipe draft",
                        "layout": {"type": "stack", "areas": [{"id": "main", "role": "main"}]},
                        "widgets": [
                            {
                                "id": "recipe-title",
                                "type": "ui.jsonViewer",
                                "area": "main",
                                "inputs": {"text": "Recipe draft"},
                            }
                        ],
                    }
                }
            }
        },
    }
    base_hash = skill._webui_source_fingerprint(before)
    output = "\n".join(
        [
            json.dumps({"type": "meta", "schema": "adaos.builder.webui_patch_stream.v1", "base_hash": base_hash}),
            json.dumps(
                {
                    "type": "patch",
                    "seq": 1,
                    "op": "replace",
                    "path": "/ui/application/desktop/pageSchema/title",
                    "value": "Recipe book",
                }
            ),
            json.dumps({"type": "complete", "comment": "Renamed the recipe book."}),
        ]
    )

    result = skill._parse_llm_webui_transform_output(
        output_text=output,
        before_webui=before,
        previous_preview={},
        request_id="request-1",
        job_id="job-1",
    )

    assert result["ok"] is True
    assert before["ui"]["application"]["desktop"]["pageSchema"]["title"] == "Recipe draft"
    assert result["payload"]["ui"]["application"]["desktop"]["pageSchema"]["title"] == "Recipe book"
    assert result["payload"]["ui"]["application"]["desktop"]["pageSchema"]["widgets"][0]["id"] == "recipe-title"
    assert result["semantic_patch_stream"]["operation_count"] == 1
    assert result["attempts"][0]["output_mode"] == "jsonl_patch_v1"


def test_builder_patch_stream_rejects_wrong_base_hash() -> None:
    skill = _load_module()
    before = {
        "schema": "adaos.webui.v1",
        "ui": {
            "application": {
                "desktop": {
                    "pageSchema": {
                        "id": "recipes",
                        "layout": {"type": "stack"},
                        "widgets": [{"id": "title", "type": "ui.jsonViewer", "inputs": {"value": {"title": "Recipes"}}}],
                    }
                }
            }
        },
    }
    output = "\n".join(
        [
            json.dumps({"type": "meta", "schema": "adaos.builder.webui_patch_stream.v1", "base_hash": "wrong"}),
            json.dumps({"type": "patch", "seq": 1, "op": "replace", "path": "/schema", "value": "adaos.webui.v1"}),
            json.dumps({"type": "complete", "comment": "No-op"}),
        ]
    )

    try:
        skill._parse_llm_webui_transform_output(
            output_text=output,
            before_webui=before,
            previous_preview={},
        )
    except ValueError as exc:
        assert "base_hash mismatch" in str(exc)
    else:
        raise AssertionError("wrong patch base hash must be rejected")


def test_builder_patch_stream_repairs_only_missing_outer_line_closer() -> None:
    skill = _load_module()
    before = {
        "schema": "adaos.webui.v1",
        "ui": {
            "application": {
                "desktop": {
                    "pageSchema": {
                        "id": "recipes",
                        "layout": {"type": "stack", "areas": [{"id": "main", "role": "main"}]},
                        "widgets": [{"id": "title", "type": "ui.jsonViewer", "area": "main"}],
                    }
                }
            }
        },
    }
    base_hash = skill._webui_source_fingerprint(before)
    malformed_patch = json.dumps(
        {
            "type": "patch",
            "seq": 1,
            "op": "add",
            "path": "/ui/application/desktop/pageSchema/widgets/-",
            "value": {"id": "cards", "type": "ui.list", "area": "main", "inputs": {"variant": "cards"}},
        }
    )[:-1]
    output = "\n".join(
        [
            json.dumps({"type": "meta", "schema": "adaos.builder.webui_patch_stream.v1", "base_hash": base_hash}),
            malformed_patch,
            json.dumps({"type": "complete", "comment": "Added cards."}),
        ]
    )

    result = skill._parse_llm_webui_transform_output(
        output_text=output,
        before_webui=before,
        previous_preview={},
    )

    assert result["ok"] is True
    assert result["payload"]["ui"]["application"]["desktop"]["pageSchema"]["widgets"][-1]["id"] == "cards"
    assert result["semantic_patch_stream"]["syntax_repairs"] == [
        {"line": 2, "repair": "append_missing_container_closers", "added_closers": 1}
    ]


def test_builder_patch_stream_stable_id_path_survives_prior_array_remove() -> None:
    skill = _load_module()
    before = {
        "schema": "adaos.webui.v1",
        "ui": {
            "application": {
                "desktop": {
                    "pageSchema": {
                        "id": "recipes",
                        "layout": {"type": "stack", "areas": [{"id": "main", "role": "main"}]},
                        "widgets": [
                            {"id": "remove-me", "type": "input.selector", "area": "main"},
                            {
                                "id": "recipe-details",
                                "type": "item.details",
                                "area": "main",
                                "inputs": {"fields": [{"label": "Old", "path": "old"}]},
                            },
                        ],
                    }
                }
            }
        },
    }
    base_hash = skill._webui_source_fingerprint(before)
    output = "\n".join(
        [
            json.dumps({"type": "meta", "schema": "adaos.builder.webui_patch_stream.v1", "base_hash": base_hash}),
            json.dumps({"type": "patch", "seq": 1, "op": "remove", "path": "/ui/application/desktop/pageSchema/widgets/@remove-me"}),
            json.dumps(
                {
                    "type": "patch",
                    "seq": 2,
                    "op": "replace",
                    "path": "/ui/application/desktop/pageSchema/widgets/@recipe-details/inputs/fields",
                    "value": [{"label": "Title", "path": "title"}],
                }
            ),
            json.dumps({"type": "complete", "comment": "Updated details."}),
        ]
    )

    result = skill._parse_llm_webui_transform_output(
        output_text=output,
        before_webui=before,
        previous_preview={},
    )

    widgets = result["payload"]["ui"]["application"]["desktop"]["pageSchema"]["widgets"]
    assert result["ok"] is True
    assert [item["id"] for item in widgets] == ["recipe-details"]
    assert widgets[0]["inputs"]["fields"] == [{"label": "Title", "path": "title"}]


def test_builder_patch_stream_reports_missing_intermediate_parent() -> None:
    skill = _load_module()
    before = {
        "schema": "adaos.webui.v1",
        "ui": {
            "application": {
                "desktop": {
                    "pageSchema": {
                        "id": "recipes",
                        "layout": {"type": "stack", "areas": [{"id": "main", "role": "main"}]},
                        "widgets": [],
                    }
                }
            }
        },
    }
    base_hash = skill._webui_source_fingerprint(before)
    output = "\n".join(
        [
            json.dumps({"type": "meta", "schema": "adaos.builder.webui_patch_stream.v1", "base_hash": base_hash}),
            json.dumps(
                {
                    "type": "patch",
                    "seq": 1,
                    "op": "add",
                    "path": "/ui/application/modals/detail",
                    "value": {"title": "Details", "schema": {"id": "detail", "layout": {"type": "stack"}, "widgets": []}},
                }
            ),
            json.dumps({"type": "complete", "comment": "Added details."}),
        ]
    )

    try:
        skill._parse_llm_webui_transform_output(
            output_text=output,
            before_webui=before,
            previous_preview={},
        )
    except KeyError as exc:
        assert "JSON Patch parent path missing: /ui/application/modals" in str(exc)
        assert "add that parent container" in str(exc)
    else:
        raise AssertionError("missing intermediate JSON Patch parent must be rejected")


def test_builder_component_contract_rejects_data_source_nested_in_inputs() -> None:
    skill = _load_module()
    page_schema = {
        "id": "catalog",
        "layout": {"type": "stack", "areas": [{"id": "main", "role": "main"}]},
        "widgets": [
            {
                "id": "recipe-cards",
                "type": "ui.list",
                "area": "main",
                "inputs": {
                    "variant": "cards",
                    "dataSource": {"kind": "static", "value": [{"title": "Soup"}]},
                },
            }
        ],
    }

    validation = skill._validate_page_schema_component_contracts(page_schema)

    assert validation["ok"] is False
    assert validation["error"] == "component_contract_invalid"
    assert "move dataSource" in validation["detail"]


def test_builder_component_contract_rejects_unrendered_details_fields() -> None:
    skill = _load_module()
    page_schema = {
        "id": "details",
        "layout": {"type": "stack", "areas": [{"id": "main", "role": "main"}]},
        "widgets": [
            {
                "id": "recipe-detail",
                "type": "item.details",
                "area": "main",
                "dataSource": {"kind": "static", "value": {"title": "Soup"}},
                "inputs": {"fields": [{"id": "title", "type": "staticContent", "content": "$item.title"}]},
            }
        ],
    }

    validation = skill._validate_page_schema_component_contracts(page_schema)

    assert validation["ok"] is False
    assert "item.details ignores" in validation["detail"]


def test_builder_component_contract_rejects_invented_update_state_operators() -> None:
    skill = _load_module()
    page_schema = {
        "id": "actions",
        "layout": {"type": "stack", "areas": [{"id": "main", "role": "main"}]},
        "widgets": [
            {
                "id": "favorite",
                "type": "ui.actions",
                "area": "main",
                "actions": [
                    {
                        "on": "click",
                        "type": "updateState",
                        "params": {"favorites": {"$merge": True, "$value": {"$toggle": True}}},
                    }
                ],
            }
        ],
    }

    validation = skill._validate_page_schema_component_contracts(page_schema)

    assert validation["ok"] is False
    assert "unsupported updateState operator" in validation["detail"]
    assert skill._unsupported_action_param_operator({"favorite": {"$set": True}}) == "$set"


def test_builder_component_contract_rejects_unrendered_table_image_cells() -> None:
    skill = _load_module()
    page_schema = {
        "id": "catalog",
        "layout": {"type": "stack", "areas": [{"id": "main", "role": "main"}]},
        "widgets": [
            {
                "id": "catalog-table",
                "type": "ui.table",
                "area": "main",
                "inputs": {"columns": [{"key": "image", "label": "Image", "kind": "image"}]},
            }
        ],
    }

    result = skill._validate_page_schema_component_contracts(page_schema)

    assert result["ok"] is False
    assert result["error"] == "component_contract_invalid"
    assert "ui.list cards" in result["detail"]


def test_builder_webui_validation_rejects_select_without_options() -> None:
    skill = _load_module()
    page_schema = {
        "id": "city_survey",
        "title": "City survey",
        "layout": {"type": "split", "areas": [{"id": "main"}]},
        "widgets": [
            {
                "id": "prototype-form",
                "type": "ui.form",
                "area": "main",
                "inputs": {
                    "fields": [
                        {
                            "id": "growth_factor",
                            "label": "Strongest city growth factor?",
                            "type": "select",
                        }
                    ]
                },
            }
        ],
    }
    payload = {"schema": "adaos.webui.v1", "ui": {"application": {"desktop": {"pageSchema": page_schema}}}}

    validation = skill._validate_builder_webui_payload(payload, {"page_schema": page_schema})

    assert validation["ok"] is False
    assert validation["error"] == "component_contract_invalid"
    assert "options" in validation["detail"]

    fixed_page_schema = copy.deepcopy(page_schema)
    fixed_page_schema["widgets"][0]["inputs"]["fields"][0]["options"] = [
        {"label": "Economic", "value": "economic"},
        {"label": "Social", "value": "social"},
    ]
    fixed_payload = {"schema": "adaos.webui.v1", "ui": {"application": {"desktop": {"pageSchema": fixed_page_schema}}}}

    assert skill._validate_builder_webui_payload(fixed_payload, {"page_schema": fixed_page_schema})["ok"] is True


def test_builder_webui_validation_rejects_undeclared_modal_action() -> None:
    skill = _load_module()
    page_schema = {
        "id": "request_center",
        "title": "Request center",
        "layout": {"type": "split", "areas": [{"id": "main"}]},
        "widgets": [
            {
                "id": "open-detail",
                "type": "ui.actions",
                "area": "main",
                "actions": [{"on": "click", "type": "openModal", "params": {"modalId": "request_detail_modal"}}],
            }
        ],
    }
    payload = {"schema": "adaos.webui.v1", "ui": {"application": {"desktop": {"pageSchema": page_schema}}}}

    validation = skill._validate_builder_webui_payload(payload, {"page_schema": page_schema})

    assert validation["ok"] is False
    assert validation["error"] == "component_contract_invalid"
    assert "undeclared modal" in validation["detail"]

    fixed_payload = copy.deepcopy(payload)
    fixed_payload["ui"]["application"]["modals"] = {
        "request_detail_modal": {
            "title": "Request detail",
            "schema": {"id": "request_detail_modal_schema", "layout": {"type": "stack"}, "widgets": []},
        }
    }

    assert skill._validate_builder_webui_payload(fixed_payload, {"page_schema": page_schema})["ok"] is True


def test_builder_webui_validation_rejects_root_level_modals() -> None:
    skill = _load_module()
    page_schema = {
        "id": "request_center",
        "title": "Request center",
        "layout": {"type": "stack", "areas": [{"id": "main"}]},
        "widgets": [{"id": "requests", "type": "ui.list", "area": "main"}],
    }
    payload = {
        "schema": "adaos.webui.v1",
        "ui": {"application": {"desktop": {"pageSchema": page_schema}}},
        "modals": {
            "request_detail_modal": {
                "title": "Request detail",
                "schema": {"id": "request_detail_modal_schema", "layout": {"type": "stack"}, "widgets": []},
            }
        },
    }

    validation = skill._validate_builder_webui_payload(payload, {"page_schema": page_schema})

    assert validation["ok"] is False
    assert validation["error"] == "component_contract_invalid"
    assert "ui.application.modals" in validation["detail"]


def test_builder_webui_validation_rejects_question_mark_encoding_loss() -> None:
    skill = _load_module()
    page_schema = {
        "id": "request_center",
        "title": "Request center",
        "layout": {"type": "stack", "areas": [{"id": "main"}]},
        "widgets": [
            {
                "id": "open-detail",
                "type": "ui.actions",
                "area": "main",
                "inputs": {"buttons": [{"id": "open", "label": "??????? ?????? ??????"}]},
            }
        ],
    }
    payload = {"schema": "adaos.webui.v1", "ui": {"application": {"desktop": {"pageSchema": page_schema}}}}

    validation = skill._validate_builder_webui_payload(payload, {"page_schema": page_schema})

    assert validation["ok"] is False
    assert validation["error"] == "text_encoding_suspect"
    assert "question marks" in validation["detail"]


def test_write_webui_payload_projects_canonical_page_schema_to_scenario(tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "canonical_webui"
    artifact_root.mkdir(parents=True)
    (artifact_root / "scenario.json").write_text(
        json.dumps({"id": "canonical_webui", "name": "canonical_webui", "type": "desktop"}),
        encoding="utf-8",
    )
    page_schema = {
        "id": "canonical_webui",
        "title": "City survey",
        "layout": {"type": "split", "areas": [{"id": "main"}]},
        "widgets": [
            {
                "id": "prototype-form",
                "type": "ui.form",
                "area": "main",
                "inputs": {
                    "fields": [
                        {
                            "id": "growth_factor",
                            "label": "Strongest city growth factor?",
                            "type": "select",
                            "options": [
                                {"label": "Economic", "value": "economic"},
                                {"label": "Social", "value": "social"},
                            ],
                        }
                    ]
                },
            }
        ],
    }
    payload = {
        "schema": "adaos.webui.v1",
        "ui": {
            "application": {
                "desktop": {"pageSchema": page_schema},
                "modals": {
                    "comment_modal": {
                        "title": "Comment",
                        "schema": {
                            "id": "comment_modal_schema",
                            "layout": {"type": "single", "areas": [{"id": "main"}]},
                            "widgets": [],
                        },
                    }
                },
            }
        },
    }

    skill._write_webui_payload(str(artifact_root), payload)

    saved_webui = json.loads((artifact_root / "webui.json").read_text(encoding="utf-8"))
    saved_scenario = json.loads((artifact_root / "scenario.json").read_text(encoding="utf-8"))
    saved_field = saved_scenario["ui"]["application"]["desktop"]["pageSchema"]["widgets"][0]["inputs"]["fields"][0]
    assert saved_webui["schema"] == "adaos.webui.v1"
    assert "preview_state" not in saved_webui
    assert saved_field["options"][0]["value"] == "economic"
    assert saved_scenario["ui"]["application"]["modals"]["comment_modal"]["title"] == "Comment"


def test_legacy_page_schema_from_preview_preserves_select_options_from_current_ui() -> None:
    skill = _load_module()
    preview = {
        "title": "City survey",
        "current_ui": {
            "id": "city_survey",
            "type": "page",
            "children": [
                {
                    "id": "editor",
                    "type": "section",
                    "children": [
                        {
                            "id": "input_growth_factor",
                            "type": "select",
                            "label": "Strongest city growth factor?",
                            "binding": "draft.growth_factor",
                            "options": [
                                {"label": "Economic", "value": "economic"},
                                {"label": "Social", "value": "social"},
                                {"label": "Infrastructure", "value": "infrastructure"},
                            ],
                        }
                    ],
                }
            ],
        },
        "page_schema": {
            "id": "city_survey",
            "widgets": [
                {
                    "id": "prototype-form",
                    "type": "ui.form",
                    "area": "main",
                    "inputs": {
                        "fields": [
                            {
                                "id": "growth_factor",
                                "label": "Strongest city growth factor?",
                                "type": "select",
                            }
                        ]
                    },
                }
            ],
        },
    }

    derived = skill._page_schema_from_preview(preview)
    form = next(item for item in derived["widgets"] if item["id"] == "prototype-form")
    field = form["inputs"]["fields"][0]

    assert field["type"] == "select"
    assert field["options"] == [
        {"label": "Economic", "value": "economic"},
        {"label": "Social", "value": "social"},
        {"label": "Infrastructure", "value": "infrastructure"},
    ]


def test_page_schema_from_preview_derives_composite_card_preview() -> None:
    skill = _load_module()
    preview = {
        "title": "Todo",
        "current_ui": {
            "id": "todo",
            "type": "page",
            "children": [
                {"id": "editor", "type": "section", "children": []},
                {
                    "id": "items_cards",
                    "type": "card_list",
                    "title": "{{title}}",
                    "subtitle": "{{notes}}",
                    "preview": "{{status}} - {{date}}",
                },
            ],
        },
        "datasources": [
            {
                "id": "prototype_items",
                "fields": [
                    {"id": "title", "type": "string", "label": "Title"},
                    {"id": "notes", "type": "string", "label": "Notes"},
                    {"id": "status", "type": "string", "label": "Status"},
                    {"id": "date", "type": "date", "label": "Date"},
                ],
            }
        ],
        "mock_data": {
            "prototype_items": [
                {"title": "Talk", "notes": "CFP", "status": "Pending", "date": "2026-07-02"}
            ]
        },
        "layout_order": "cards_first",
    }

    derived = skill._page_schema_from_preview(preview)
    cards = next(item for item in derived["widgets"] if item["id"] == "prototype-cards")

    assert cards["inputs"]["previewKey"] == "card_preview"
    assert cards["dataSource"]["value"][0]["card_preview"] == "Pending - 2026-07-02"
    assert cards["dataSource"]["value"][0]["status"] == "Pending"


def test_repair_mojibake_text_handles_common_cyrillic_and_keeps_other_languages() -> None:
    skill = _load_module()

    assert (
        skill._repair_mojibake_text("Р”РѕР±Р°РІСЊ РІ РєР°СЂС‚РѕС‡РєРё РёРЅС„РѕСЂРјР°С†РёСЋ Рѕ РґР°С‚Рµ")
        == "Добавь в карточки информацию о дате"
    )
    assert (
        skill._repair_mojibake_text("РїРѕРјРµРЅСЏР№ РјРµСЃС‚Р°РјРё СЃРµРєС†РёСЋ Input Рё Cards")
        == "поменяй местами секцию Input и Cards"
    )
    assert skill._repair_mojibake_text("Переведи данные на китайский язык") == "Переведи данные на китайский язык"
    assert skill._repair_mojibake_text("翻译成中文") == "翻译成中文"


def test_update_current_scenario_sample_data_uses_llm_payload_and_refreshes_files(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "llm_sample_data"
    artifact_root.mkdir(parents=True)
    (artifact_root / "scenario.json").write_text(
        '{"id":"llm_sample_data","version":"0.1.0","name":"llm_sample_data","steps":[]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ADAOS_BUILDER_LLM_IN_TESTS", "1")
    monkeypatch.setenv("ADAOS_BUILDER_LLM_MODEL", "gpt-test-builder")
    published: list[tuple[str, dict]] = []

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {
                "source_webspace_id": kwargs.get("source_webspace_id"),
                "dev_webspace_id": "builder-llm-sample-dev",
                "active_draft_id": kwargs.get("active_draft_id"),
                "runtime_scenario_id": kwargs.get("runtime_scenario_id"),
            }

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    import adaos.sdk.data.events as events
    import adaos.services.pending_actions as pending_actions

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(pending_actions, "publish_pending_action", lambda **kwargs: {"id": "pa.builder.llm.sample"})
    refresh_calls: list[dict] = []
    monkeypatch.setattr(
        skill,
        "_schedule_dev_runtime_reload_after_revision",
        lambda webspace_id, **kwargs: refresh_calls.append({"webspace_id": webspace_id, **kwargs})
        or {"ok": True, "scheduled": True, "webspace_id": "builder-llm-sample-dev"},
    )
    monkeypatch.setattr(events, "publish", lambda topic, payload, source=None: published.append((topic, dict(payload))))

    def _llm_transform(**kwargs):
        preview = json.loads(json.dumps(kwargs["preview_state"]))
        preview["mock_data"] = {
            "prototype_items": [
                {"title": "Book venue", "notes": "Confirm room capacity and AV equipment", "status": "In progress", "date": "2026-07-01"},
                {"title": "Confirm speakers", "notes": "Collect talk titles and short bios", "status": "Planned", "date": "2026-07-02"},
            ]
        }
        page_schema = skill._page_schema_from_preview(preview)
        return {
            "ok": True,
            "payload": {"schema": "adaos.webui.v1", "generated_by": "builder_skill", "ui": {"application": {"desktop": {"pageSchema": page_schema}}}},
            "preview_state": preview,
            "comment": "Updated mock data for conference preparation.",
            "validation": {"ok": True},
        }

    monkeypatch.setattr(skill, "_apply_llm_webui_transform", _llm_transform)
    skill._save_session(
        "builder-llm-sample",
        {
            "id": "builder_session_llm_sample",
            "webspace_id": "builder-llm-sample",
            "status": "drafting",
            "title": "Todo List",
            "scenario_id": "llm_sample_data",
            "draft_id": "draft.llm.sample",
            "artifact_root": str(artifact_root),
            "datasource_id": "prototype_items",
            "fields": [
                {"id": "title", "type": "string", "label": "Title", "required": True},
                {"id": "notes", "type": "string", "label": "Notes", "required": False},
                {"id": "status", "type": "string", "label": "Status", "required": False},
                {"id": "date", "type": "date", "label": "Date", "required": False},
            ],
            "patches": [],
            "version": "001",
        },
    )

    result = skill.update_current_scenario(
        "\u0414\u0430\u043d\u043d\u044b\u0435 \u0441\u0434\u0435\u043b\u0430\u0439 \u043d\u0430 \u043f\u0440\u0438\u043c\u0435\u0440\u0435 \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043a\u0438 \u043a \u043a\u043e\u043d\u0444\u0435\u0440\u0435\u043d\u0446\u0438\u0438. \u041d\u0430\u043f\u0438\u0448\u0438 \u0438\u0445 \u043d\u0430 \u0430\u043d\u0433\u043b\u0438\u0439\u0441\u043a\u043e\u043c \u044f\u0437\u044b\u043a\u0435",
        webspace_id="builder-llm-sample",
    )

    assert result["patch"]["operation"] == "llm_webui_transform"
    rows = result["preview_state"]["mock_data"]["prototype_items"]
    assert rows[0]["title"] == "Book venue"
    assert rows[1]["title"] == "Confirm speakers"
    assert result["dev_runtime_refresh"]["scheduled"] is True
    assert refresh_calls[0]["webspace_id"] == "builder-llm-sample"
    assert refresh_calls[0]["revision"] == "001"
    assert refresh_calls[0]["session"]["scenario_id"] == "llm_sample_data"
    assert result["project_files_refresh"]["ok"] is True
    assert any(
        topic == "prompt.project.changed"
        and payload.get("object_type") == "scenario"
        and payload.get("object_id") == "llm_sample_data"
        and payload.get("reason") == "builder_ui_revision_written"
        for topic, payload in published
    )
    revision = json.loads((artifact_root / "ui_revisions" / "001.json").read_text(encoding="utf-8"))
    assert revision["llm"]["ok"] is True
    assert revision["inference"]["model"] == "gpt-test-builder"
    assert revision["inference"]["provider"] == "openai"
    assert revision["preview_state"]["mock_data"]["prototype_items"][0]["title"] == "Book venue"


def test_schedule_dev_runtime_reload_publishes_materialization_event_without_running_loop(monkeypatch) -> None:
    skill = _load_module()
    monkeypatch.setenv("ADAOS_BUILDER_DEV_RUNTIME_REFRESH_IN_TESTS", "1")
    published: list[dict] = []
    reload_calls: list[dict] = []

    import adaos.sdk.data.events as events
    import adaos.services.scenario.webspace_runtime as webspace_runtime

    monkeypatch.setattr(
        events,
        "publish",
        lambda topic, payload, source=None: published.append(
            {"topic": topic, "payload": dict(payload), "source": source}
        ),
    )

    async def _reload(webspace_id, *, scenario_id=None, action="reload", event_payload=None):
        reload_calls.append(
            {
                "webspace_id": webspace_id,
                "scenario_id": scenario_id,
                "action": action,
                "event_payload": dict(event_payload or {}),
            }
        )
        reload_done.set()
        return {"ok": True}

    monkeypatch.setattr(webspace_runtime, "reload_webspace_from_scenario", _reload)

    result = skill._schedule_dev_runtime_reload_after_revision(
        "desktop",
        session={"scenario_id": "todo_list", "draft_id": "draft.todo", "ui_revision": "016"},
        binding={"dev_webspace_id": "desktop-dev"},
        revision="016",
    )

    assert result["ok"] is True
    assert result["scheduled"] is True
    assert result["mode"] == "materialization_event_bus"
    assert result["webspace_id"] == "desktop-dev"
    assert reload_calls == []
    assert published[-1]["topic"] == "builder.ui_revision.materialize"
    assert published[-1]["source"] == "builder_skill"
    assert published[-1]["payload"]["webspace_id"] == "desktop-dev"
    assert published[-1]["payload"]["scenario_id"] == "todo_list"
    assert published[-1]["payload"]["revision"] == "016"
    assert published[-1]["payload"]["_meta"]["cmd_id"] == "builder.ui.todo_list.016"
    assert published[-1]["payload"]["delay_s"] == 0.0
    assert result["delay_s"] == 0.0


def test_schedule_dev_runtime_materialization_uses_running_event_loop(monkeypatch) -> None:
    skill = _load_module()
    monkeypatch.setenv("ADAOS_BUILDER_DEV_RUNTIME_REFRESH_IN_TESTS", "1")
    monkeypatch.setenv("ADAOS_BUILDER_REVISION_MATERIALIZATION_DELAY_S", "0")
    calls: list[dict] = []

    import adaos.services.scenario.webspace_runtime as webspace_runtime

    async def _apply(webspace_id, **kwargs):
        calls.append({"webspace_id": webspace_id, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(webspace_runtime, "apply_builder_revision_materialization", _apply)

    async def _run() -> dict:
        result = skill._schedule_dev_runtime_reload_after_revision(
            "desktop",
            session={"scenario_id": "todo_list", "draft_id": "draft.todo", "ui_revision": "019"},
            binding={"dev_webspace_id": "desktop-dev"},
            revision="019",
            source_fingerprint="fp-019",
            user_id="guest",
            roles=[],
        )
        await asyncio.sleep(0)
        return result

    result = asyncio.run(_run())

    assert result["ok"] is True
    assert result["scheduled"] is True
    assert result["mode"] == "materialization_event_loop_task"
    assert result["webspace_id"] == "desktop-dev"
    assert result["revision"] == "019"
    assert calls[-1]["webspace_id"] == "desktop-dev"
    assert calls[-1]["scenario_id"] == "todo_list"
    assert calls[-1]["revision"] == "019"
    assert calls[-1]["source_fingerprint"] == "fp-019"
    assert calls[-1]["user_id"] == "guest"


def test_update_current_scenario_does_not_generate_domain_mock_data_without_llm(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "sample_without_llm"
    artifact_root.mkdir(parents=True)
    (artifact_root / "scenario.json").write_text(
        '{"id":"sample_without_llm","version":"0.1.0","name":"sample_without_llm","steps":[]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ADAOS_BUILDER_LLM_PRIMARY", "0")

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {"dev_webspace_id": "builder-sample-no-llm-dev", "active_draft_id": kwargs.get("active_draft_id")}

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    import adaos.sdk.data.events as events
    import adaos.services.pending_actions as pending_actions

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(events, "publish", lambda *args, **kwargs: None)
    monkeypatch.setattr(pending_actions, "publish_pending_action", lambda **kwargs: {"id": "pa.builder.sample.no.llm"})
    skill._save_session(
        "builder-sample-no-llm",
        {
            "id": "builder_session_sample_no_llm",
            "webspace_id": "builder-sample-no-llm",
            "status": "drafting",
            "title": "Todo List",
            "scenario_id": "sample_without_llm",
            "draft_id": "draft.sample.no.llm",
            "artifact_root": str(artifact_root),
            "datasource_id": "prototype_items",
            "fields": [
                {"id": "title", "type": "string", "label": "Title", "required": True},
                {"id": "notes", "type": "string", "label": "Notes", "required": False},
                {"id": "date", "type": "date", "label": "Date", "required": False},
            ],
            "mock_rows": [
                {"title": "Existing task", "notes": "Existing note", "date": "2026-07-01"},
            ],
            "patches": [],
            "version": "001",
        },
    )

    result = skill.update_current_scenario(
        "\u0414\u0430\u043d\u043d\u044b\u0435 \u0441\u0434\u0435\u043b\u0430\u0439 \u043d\u0430 \u043f\u0440\u0438\u043c\u0435\u0440\u0435 \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043a\u0438 \u043a \u043a\u043e\u043d\u0444\u0435\u0440\u0435\u043d\u0446\u0438\u0438",
        webspace_id="builder-sample-no-llm",
    )

    assert result["status"] == "noop"
    assert result["patch"]["operation"] == "noop"
    rows = result["preview_state"]["mock_data"]["prototype_items"]
    assert rows == [{"title": "Existing task", "notes": "Existing note", "date": "2026-07-01"}]
    assert not (artifact_root / "ui_revisions").exists()


def test_update_current_scenario_translate_data_timeout_does_not_apply_ui_only_fallback(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "translate_data_timeout"
    artifact_root.mkdir(parents=True)
    (artifact_root / "scenario.json").write_text(
        '{"id":"translate_data_timeout","version":"0.1.0","name":"translate_data_timeout","steps":[]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ADAOS_BUILDER_LLM_IN_TESTS", "1")

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {"dev_webspace_id": "builder-translate-timeout-dev", "active_draft_id": kwargs.get("active_draft_id")}

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    calls: list[str] = []

    def _llm_timeout(**kwargs):
        calls.append(str(kwargs.get("instruction") or ""))
        return {
            "ok": False,
            "error": "llm_webui_transform_failed",
            "detail": "RootHttpError: POST /v1/llm/response failed: The read operation timed out",
        }

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(skill, "_apply_llm_webui_transform", _llm_timeout)
    skill._save_session(
        "builder-translate-timeout",
        {
            "id": "builder_session_translate_timeout",
            "webspace_id": "builder-translate-timeout",
            "status": "drafting",
            "title": "Todo List",
            "scenario_id": "translate_data_timeout",
            "draft_id": "draft.translate.timeout",
            "artifact_root": str(artifact_root),
            "datasource_id": "prototype_items",
            "fields": [
                {"id": "title", "type": "string", "label": "Title", "required": True},
                {"id": "notes", "type": "string", "label": "Notes", "required": False},
            ],
            "mock_rows": [
                {"title": "\u041a\u0443\u043f\u0438\u0442\u044c \u0431\u0438\u043b\u0435\u0442\u044b", "notes": "\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u0434\u0430\u0442\u044b"},
            ],
            "patches": [],
            "version": "001",
        },
    )

    result = skill.update_current_scenario(
        "\u041f\u0435\u0440\u0435\u0432\u0435\u0434\u0438 \u0434\u0430\u043d\u043d\u044b\u0435 \u043d\u0430 \u0430\u043d\u0433\u043b\u0438\u0439\u0441\u043a\u0438\u0439 \u044f\u0437\u044b\u043a",
        webspace_id="builder-translate-timeout",
    )

    assert calls
    assert result["status"] == "noop"
    assert result["patch"]["operation"] == "noop"
    assert result["patch"]["diff"]["llm_required"] is True
    assert "timed out" in result["message"]
    rows = result["preview_state"]["mock_data"]["prototype_items"]
    assert rows == [{"title": "\u041a\u0443\u043f\u0438\u0442\u044c \u0431\u0438\u043b\u0435\u0442\u044b", "notes": "\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u0434\u0430\u0442\u044b"}]
    assert not (artifact_root / "ui_revisions").exists()


def test_set_ui_revision_current_restores_stored_webui(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "revision_restore"

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **_kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            (artifact_root / "scenario.json").write_text(
                '{"id":"revision_restore","version":"0.1.0","name":"revision_restore","steps":[]}',
                encoding="utf-8",
            )
            return {"ok": True, "draft": {"draft_id": "draft.revision"}, "artifact_root": str(artifact_root)}

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {"dev_webspace_id": "builder-revision-dev", "active_draft_id": kwargs.get("active_draft_id")}

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    import adaos.services.builder.workspace as workspace
    import adaos.services.pending_actions as pending_actions

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(pending_actions, "publish_pending_action", lambda **kwargs: {"id": "pa.builder.revision"})
    refresh_calls: list[dict] = []
    monkeypatch.setattr(
        skill,
        "_schedule_dev_runtime_reload_after_revision",
        lambda webspace_id, **kwargs: refresh_calls.append({"webspace_id": webspace_id, **kwargs})
        or {"ok": True, "scheduled": True, "webspace_id": "builder-revision-dev"},
    )

    created = skill.create_scenario_draft("create todo list", webspace_id="builder-revision")
    assert created["ui_revision"]["revision"] == "001"
    created_revision = json.loads((artifact_root / "ui_revisions" / "001.json").read_text(encoding="utf-8"))
    assert created_revision["preview_state"]["version"] == "001"
    assert "preview_state" not in created_revision["after_webui"]
    assert created_revision["prompt_files"]["tz/base_tz.md"]["exists"] is True
    first_tz = created_revision["prompt_files"]["tz/base_tz.md"]["content"]
    (artifact_root / "tz" / "base_tz.md").write_text("spec revision 002", encoding="utf-8")
    (artifact_root / "prompt_state.json").write_text(
        json.dumps({"base_tz": "spec revision 002", "prepare": {}, "generate": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    updated = skill.update_current_scenario("show cards", webspace_id="builder-revision")
    assert updated["ui_revision"]["revision"] == "002"
    updated_revision = json.loads((artifact_root / "ui_revisions" / "002.json").read_text(encoding="utf-8"))
    assert updated_revision["preview_state"]["version"] == "002"
    assert "preview_state" not in updated_revision["after_webui"]
    assert updated_revision["prompt_files"]["tz/base_tz.md"]["content"] == "spec revision 002"
    assert any(item["type"] == "card_list" for item in updated["preview_state"]["current_ui"]["children"])
    emitted: list[dict[str, object]] = []

    def _unexpected_revision_chat_emit(*args, **kwargs):
        emitted.append({"args": args, "kwargs": kwargs})
        raise AssertionError("successful Set current must not append a persistent chat message")

    monkeypatch.setattr(skill, "_schedule_safe_emit_chat", _unexpected_revision_chat_emit)

    restored = skill.set_ui_revision_current("001", webspace_id="builder-revision")

    assert restored["ok"] is True
    assert restored["revision"] == "001"
    assert emitted == []
    assert restored["chat_emit"]["mode"] == "receipt_only"
    assert restored["chat_emit"]["persisted"] is False
    assert restored["timings_ms"]["emit_chat"] == 0.0
    assert restored["dev_runtime_refresh"]["scheduled"] is True
    assert refresh_calls[-1]["webspace_id"] == "builder-revision"
    assert refresh_calls[-1]["revision"] == "001"
    assert not any(item["type"] == "card_list" for item in restored["preview_state"]["current_ui"]["children"])
    saved = json.loads((artifact_root / "webui.json").read_text(encoding="utf-8"))
    widgets = saved["ui"]["application"]["desktop"]["pageSchema"]["widgets"]
    assert not any(item.get("id") == "prototype-cards" for item in widgets)
    assert (artifact_root / "tz" / "base_tz.md").read_text(encoding="utf-8") == first_tz
    state = json.loads((artifact_root / "prompt_state.json").read_text(encoding="utf-8"))
    assert state["base_tz"] == first_tz


def test_set_ui_revision_current_migrates_legacy_root_modals(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "legacy_modal_revision"
    revision_dir = artifact_root / "ui_revisions"
    revision_dir.mkdir(parents=True)
    (artifact_root / "scenario.json").write_text(
        '{"id":"legacy_modal_revision","version":"0.1.0","name":"legacy_modal_revision","steps":[]}',
        encoding="utf-8",
    )
    page_schema = {
        "id": "legacy_modal_revision",
        "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]},
        "widgets": [
            {
                "id": "open-details",
                "type": "ui.actions",
                "area": "main",
                "title": "Open details",
                "actions": [{"on": "click", "type": "openModal", "params": {"modalId": "details_modal"}}],
            }
        ],
    }
    modal_schema = {
        "id": "details_modal",
        "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]},
        "widgets": [{"id": "details", "type": "item.details", "area": "main", "title": "Details"}],
    }
    preview = {"title": "Legacy Modal Revision", "page_schema": page_schema, "version": "004"}
    (revision_dir / "004.json").write_text(
        json.dumps(
            {
                "schema": "adaos.builder.ui_revision.v1",
                "revision": "004",
                "after_webui": {
                    "schema": "adaos.webui.v1",
                    "ui": {"application": {"desktop": {"pageSchema": page_schema}}},
                    "modals": {"details_modal": {"title": "Details", "schema": modal_schema}},
                },
                "preview_state": preview,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (revision_dir / "current.txt").write_text("013\n", encoding="utf-8")
    skill._save_session(
        "builder-legacy-modal",
        {
            "id": "builder_session_legacy_modal",
            "webspace_id": "builder-legacy-modal",
            "status": "drafting",
            "title": "Legacy Modal Revision",
            "scenario_id": "legacy_modal_revision",
            "draft_id": "draft.legacy.modal",
            "artifact_root": str(artifact_root),
            "preview_state": preview,
            "ui_revision": "013",
            "version": "013",
        },
    )
    monkeypatch.setattr(
        skill,
        "_ensure_workbench",
        lambda *args, **kwargs: {
            "ok": True,
            "binding": {"runtime_scenario_id": "legacy_modal_revision", "active_draft_id": "draft.legacy.modal"},
        },
    )
    monkeypatch.setattr(
        skill,
        "_schedule_dev_runtime_reload_after_revision",
        lambda *args, **kwargs: {"ok": True, "scheduled": True, "revision": kwargs.get("revision")},
    )

    restored = skill.set_ui_revision_current("004", webspace_id="builder-legacy-modal")

    assert restored["ok"] is True
    saved = json.loads((artifact_root / "webui.json").read_text(encoding="utf-8"))
    assert "modals" not in saved
    assert saved["ui"]["application"]["modals"]["details_modal"]["schema"]["id"] == "details_modal"
    assert saved["ui"]["application"]["desktop"]["pageSchema"]["widgets"][0]["actions"][0]["params"]["modalId"] == "details_modal"


def test_write_ui_revision_does_not_overwrite_existing_revision(tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "revision_collision"
    revision_dir = artifact_root / "ui_revisions"
    revision_dir.mkdir(parents=True)
    for number in range(1, 5):
        (revision_dir / f"{number:03d}.json").write_text(
            json.dumps({"revision": f"{number:03d}", "marker": number}),
            encoding="utf-8",
        )
    original_004 = (revision_dir / "004.json").read_text(encoding="utf-8")
    session = {
        "id": "builder_session_collision",
        "scenario_id": "revision_collision",
        "draft_id": "draft.revision.collision",
        "artifact_root": str(artifact_root),
        "ui_revisions": [{"revision": "004", "path": str(revision_dir / "004.json")}],
    }

    written = skill._write_ui_revision(
        session=session,
        request_text="change UI",
        patch={"operation": "llm_webui_transform"},
        before_webui={},
        after_webui={"schema": "adaos.webui.v1"},
        preview_state={"title": "Revision Collision"},
        revision="004",
    )

    assert written["revision"] == "005"
    assert (revision_dir / "004.json").read_text(encoding="utf-8") == original_004
    assert (revision_dir / "005.json").exists()
    assert (revision_dir / "current.txt").read_text(encoding="utf-8").strip() == "005"
    assert session["ui_revision"] == "005"


def test_set_ui_revision_current_failure_keeps_project_topic(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "revision_failure_topic"
    artifact_root.mkdir(parents=True)
    (artifact_root / "scenario.json").write_text(
        '{"id":"revision_failure_topic","version":"0.1.0","name":"revision_failure_topic","steps":[]}',
        encoding="utf-8",
    )
    skill._save_session(
        "builder-revision-failure",
        {
            "id": "builder_session_revision_failure",
            "webspace_id": "builder-revision-failure",
            "status": "drafting",
            "title": "Revision Failure Topic",
            "scenario_id": "revision_failure_topic",
            "draft_id": "draft.revision.failure",
            "artifact_root": str(artifact_root),
            "preview_state": {"title": "Revision Failure Topic"},
        },
    )
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        skill,
        "_safe_emit_chat",
        lambda *args, **kwargs: emitted.append({"args": args, "kwargs": kwargs}),
    )

    result = skill.set_ui_revision_current("999", webspace_id="builder-revision-failure")

    expected_topic = "prompt-project:scenario:revision_failure_topic"
    assert result["ok"] is False
    assert result["dialog"]["topic_id"] == expected_topic
    assert result["dialog"]["thread_id"] == expected_topic
    assert emitted
    assert emitted[0]["kwargs"]["topic_ref"]["topic_id"] == expected_topic


def test_write_webui_keeps_builder_skill_out_of_runtime_dependencies(tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "prototype"
    artifact_root.mkdir(parents=True)
    (artifact_root / "scenario.json").write_text(
        json.dumps(
            {
                "id": "prototype",
                "name": "prototype",
                "depends": ["builder_skill", "voice_chat_skill"],
                "runtime": {"skills": {"required": ["builder_skill", "voice_chat_skill"]}},
            }
        ),
        encoding="utf-8",
    )
    preview = {
        "title": "Prototype",
        "current_ui": {
            "id": "prototype",
            "type": "page",
            "children": [
                {"id": "editor", "type": "section", "children": []},
                {"id": "items_table", "type": "table", "columns": [], "visible": True},
            ],
        },
        "datasources": [{"id": "items", "fields": []}],
        "mock_data": {"items": []},
    }

    skill._write_webui(str(artifact_root), preview)

    scenario = json.loads((artifact_root / "scenario.json").read_text(encoding="utf-8"))
    manifest = (artifact_root / "scenario.yaml").read_text(encoding="utf-8")
    assert "builder_skill" not in scenario["depends"]
    assert "builder_skill" not in scenario["runtime"]["skills"]["required"]
    assert "voice_chat_skill" in scenario["depends"]
    assert "voice_chat_skill" in manifest
    assert "builder_skill" not in manifest


def test_chat_meta_uses_prompt_project_topic_for_selected_scenario() -> None:
    skill = _load_module()

    meta = skill._chat_meta(
        None,
        webspace_id="desktop",
        session={"scenario_id": "todo_list_5b9319fa"},
        binding={"runtime_scenario_id": "todo_list_5b9319fa"},
    )

    assert meta["conversation_topic_id"] == "prompt-project:scenario:todo_list_5b9319fa"


def test_chat_meta_replaces_stale_client_topic_with_selected_scenario() -> None:
    skill = _load_module()
    stale_topic = {
        "thread_id": "prompt-project:scenario:todo_list_5b9319fa",
        "topic_id": "prompt-project:scenario:todo_list_5b9319fa",
        "scenario_id": "todo_list_5b9319fa",
        "conversation_id": "conv.skill.builder_skill.default.desktop",
    }

    meta = skill._chat_meta(
        {
            "conversation_topic_id": "prompt-project:scenario:todo_list_5b9319fa",
            "conversation_thread_id": "prompt-project:scenario:todo_list_5b9319fa",
            "thread_id": "prompt-project:scenario:todo_list_5b9319fa",
            "topic_id": "prompt-project:scenario:todo_list_5b9319fa",
            "builder_topic": stale_topic,
        },
        webspace_id="desktop",
        session={"scenario_id": "prototype_app_4d5758e5"},
        binding={"runtime_scenario_id": "prototype_app_4d5758e5"},
        topic_ref=stale_topic,
    )

    assert meta["conversation_topic_id"] == "prompt-project:scenario:prototype_app_4d5758e5"
    assert meta["conversation_thread_id"] == "prompt-project:scenario:prototype_app_4d5758e5"
    assert meta["thread_id"] == "prompt-project:scenario:prototype_app_4d5758e5"
    assert meta["topic_id"] == "prompt-project:scenario:prototype_app_4d5758e5"
    assert meta["builder_topic"]["thread_id"] == "prompt-project:scenario:prototype_app_4d5758e5"
    assert meta["builder_topic"]["scenario_id"] == "prototype_app_4d5758e5"


def test_chat_first_idea_creates_preview_and_accepts_correction(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "first_idea"
    emitted: list[dict] = []
    published: list[dict] = []

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            (artifact_root / "scenario.json").write_text(
                '{"id":"first_idea","version":"0.1.0","name":"first_idea","steps":[]}',
                encoding="utf-8",
            )
            return {
                "ok": True,
                "draft": {"draft_id": "draft.first.idea"},
                "artifact_root": str(artifact_root),
                "kwargs": kwargs,
            }

    class _Workbench:
        def set_active_draft(self, *, source_webspace_id=None, active_draft_id=None, runtime_scenario_id=None, persist_projection=True):
            return {
                "source_webspace_id": source_webspace_id,
                "dev_webspace_id": f"{source_webspace_id}-dev",
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
                "dialog": {"widget": "voice_chat", "dialog_channel_id": "builder"},
            }

        def snapshot(self, webspace_id, *, preview_state=None):
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

    import adaos.services.builder.workspace as workspace
    import adaos.services.pending_actions as pending_actions

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(skill, "_safe_emit_chat", lambda text, **kwargs: emitted.append({"text": text, "kwargs": kwargs}))
    monkeypatch.setattr(
        pending_actions,
        "publish_pending_action",
        lambda **kwargs: published.append(dict(kwargs)) or {"id": f"pa.builder.{len(published)}", "kind": kwargs["kind"]},
    )

    created = skill.chat("I have an idea. Let's build it.", webspace_id="builder-first-idea")

    assert created["ok"] is True
    assert created["scenario_id"].startswith("i_have_an_idea_let_s_build_it")
    assert created["dialog"]["dialog_channel_id"] == "builder"
    assert created["preview_state"]["current_ui"]["type"] == "page"
    assert created["preview_state"]["user_summary"]["assumptions"]
    assert "Assumptions:" in created["message"]
    assert (artifact_root / "webui.json").exists()
    assert published == []
    assert emitted[0]["kwargs"]["topic_ref"]["thread_id"] == created["topic"]["thread_id"]

    updated = skill.chat("show the result as cards", webspace_id="builder-first-idea")

    assert updated["ok"] is True
    assert updated["patch"]["operation"] == "change_view_representation"
    assert updated["topic"]["thread_id"] == created["topic"]["thread_id"]
    assert any(item["type"] == "card_list" for item in updated["preview_state"]["current_ui"]["children"])
    webui = json.loads((artifact_root / "webui.json").read_text(encoding="utf-8"))
    widgets = webui["ui"]["application"]["desktop"]["pageSchema"]["widgets"]
    cards = next(item for item in widgets if item["id"] == "prototype-cards")
    assert cards["type"] == "ui.list"
    assert cards["inputs"]["variant"] == "cards"
    assert published == []


def test_chat_guides_underspecified_first_idea(monkeypatch) -> None:
    skill = _load_module()
    emitted: list[dict] = []

    class _Workbench:
        def get_workspace_binding(self, webspace_id):
            return {
                "source_webspace_id": webspace_id,
                "dev_webspace_id": f"{webspace_id}-dev",
                "dialog": {"dialog_channel_id": "builder"},
            }

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_safe_emit_chat", lambda text, **kwargs: emitted.append({"text": text, "kwargs": kwargs}))

    result = skill.chat("I have an idea", webspace_id="builder-clarify")

    assert result["ok"] is True
    assert result["status"] == "clarification_required"
    assert result["needs_clarification"] is True
    assert result["dialog"]["dialog_channel_id"] == "builder"
    assert result["topic"]["thread_id"].startswith("thread.builder.builder-clarify")
    assert result["clarification"]["schema"] == "adaos.builder.guided_clarification.v1"
    assert [item["id"] for item in result["clarification"]["questions"]] == [
        "user_goal",
        "primary_objects",
        "first_action",
    ]
    assert result["clarification"]["next_turn_policy"]["creates_draft_when_answered"] is True
    assert "scenario_id" not in result
    assert emitted[0]["kwargs"]["topic_ref"]["thread_id"] == result["topic"]["thread_id"]


def test_update_current_scenario_handles_layout_column_and_date_requests(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "shopping_list"
    artifact_root.mkdir(parents=True, exist_ok=True)
    (artifact_root / "scenario.json").write_text(
        '{"id":"shopping_list","version":"0.1.0","name":"shopping_list","steps":[]}',
        encoding="utf-8",
    )

    class _Workbench:
        def get_workspace_binding(self, webspace_id):
            return {
                "source_webspace_id": webspace_id,
                "dev_webspace_id": f"{webspace_id}-dev",
                "active_draft_id": "draft.shopping",
                "runtime_scenario_id": "shopping_list",
            }

        def set_active_draft(self, **kwargs):
            return {
                "source_webspace_id": kwargs.get("source_webspace_id"),
                "dev_webspace_id": f"{kwargs.get('source_webspace_id')}-dev",
                "active_draft_id": kwargs.get("active_draft_id"),
                "runtime_scenario_id": kwargs.get("runtime_scenario_id"),
            }

        def snapshot(self, webspace_id, *, preview_state=None):
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

    import adaos.services.pending_actions as pending_actions

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(pending_actions, "publish_pending_action", lambda **kwargs: {"id": "pa.builder.layout"})
    skill._save_session(
        "builder-layout",
        {
            "id": "builder_session_layout",
            "webspace_id": "builder-layout",
            "status": "drafting",
            "title": "Shopping list",
            "scenario_id": "shopping_list",
            "draft_id": "draft.shopping",
            "artifact_root": str(artifact_root),
            "datasource_id": "shopping_items",
            "fields": [
                {"id": "item", "type": "string", "label": "\u0422\u043e\u0432\u0430\u0440", "required": True},
                {"id": "quantity", "type": "number", "label": "\u041a\u043e\u043b-\u0432\u043e", "required": False},
                {"id": "category", "type": "string", "label": "\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f", "required": False},
                {"id": "done", "type": "boolean", "label": "\u041a\u0443\u043f\u043b\u0435\u043d\u043e", "required": False},
            ],
            "patches": [],
            "version": "v1",
        },
    )

    moved = skill.update_current_scenario("\u041f\u0435\u0440\u0435\u043c\u0435\u0441\u0442\u0438\u043c \u043a\u043d\u043e\u043f\u043a\u0443 Add \u043d\u0430\u0434 \u0444\u043e\u0440\u043c\u043e\u0439", webspace_id="builder-layout")
    assert moved["patch"]["operation"] == "move_form_action"
    form = next(item for item in moved["preview_state"]["current_ui"]["children"] if item["id"] == "editor")
    assert form["action_position"] == "top"
    scenario = yaml.safe_load((artifact_root / "scenario.json").read_text(encoding="utf-8"))
    page_schema = scenario["ui"]["application"]["desktop"]["pageSchema"]
    page_form = next(item for item in page_schema["widgets"] if item["id"] == "prototype-form")
    assert page_form["inputs"]["submitPlacement"] == "top"

    checkbox = skill.update_current_scenario("\u0421\u0434\u0435\u043b\u0430\u0435\u043c \u043f\u0435\u0440\u0432\u043e\u0439 \u043a\u043e\u043b\u043e\u043d\u043a\u043e\u0439 \u0442\u0430\u0431\u043b\u0438\u0446\u044b \u0447\u0435\u043a\u0431\u043e\u043a\u0441 (\u043a\u0443\u043f\u043b\u0435\u043d\u043e)", webspace_id="builder-layout")
    assert checkbox["patch"]["operation"] == "set_checkbox_column"
    assert checkbox["preview_state"]["datasources"][0]["fields"][0]["id"] == "done"
    page_schema = yaml.safe_load((artifact_root / "scenario.json").read_text(encoding="utf-8"))["ui"]["application"]["desktop"]["pageSchema"]
    page_table = next(item for item in page_schema["widgets"] if item["id"] == "prototype-table")
    assert page_table["inputs"]["columns"][0] == {"key": "done", "label": "\u041a\u0443\u043f\u043b\u0435\u043d\u043e", "kind": "boolean", "width": "72px"}

    date_result = skill.update_current_scenario("\u0414\u043e\u0431\u0430\u0432\u044c \u0434\u0430\u043d\u043d\u044b\u0435 \u0432 \u043f\u043e\u043b\u0435 \u0434\u0430\u0442\u0430 \u0432 \u0442\u0430\u0431\u043b\u0438\u0446\u0443", webspace_id="builder-layout")
    assert date_result["patch"]["operation"] == "add_field"
    assert any(item["id"] == "date" and item["type"] == "date" for item in date_result["preview_state"]["datasources"][0]["fields"])
    rows = date_result["preview_state"]["mock_data"]["shopping_items"]
    assert [row["date"] for row in rows] == ["2026-07-01", "2026-07-02", "2026-07-03"]

    filled = skill.update_current_scenario(
        "\u0417\u0430\u043f\u043e\u043b\u043d\u0438 \u043a\u043e\u043b\u043e\u043d\u043a\u0443 \u0434\u0430\u0442\u0430 \u043d\u0435 \u0441\u043b\u043e\u0432\u043e\u043c \"\u0434\u0430\u0442\u0430\", \u0430 \u043f\u0440\u043e\u0438\u0437\u0432\u043e\u043b\u044c\u043d\u044b\u043c\u0438 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u044f\u043c\u0438 \u0442\u0438\u043f\u0430 \u0434\u0430\u0442\u0430",
        webspace_id="builder-layout",
    )
    assert filled["patch"]["operation"] == "update_mock_data"
    assert [row["date"] for row in filled["preview_state"]["mock_data"]["shopping_items"]] == [
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
    ]


def test_update_current_scenario_does_not_publish_pending_action_for_reversible_revision(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "shopping_list"
    published: list[dict] = []

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **_kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            return {"ok": True, "draft": {"draft_id": "draft.shopping"}, "artifact_root": str(artifact_root)}

    import adaos.services.builder.workspace as workspace
    import adaos.services.pending_actions as pending_actions

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    monkeypatch.setattr(
        pending_actions,
        "publish_pending_action",
        lambda **kwargs: published.append(dict(kwargs)) or {"id": f"pa.builder.{len(published)}"},
    )

    skill.create_scenario_draft("create shopping list", webspace_id="builder-pa-patch")
    result = skill.update_current_scenario(
        "show cards",
        webspace_id="builder-pa-patch",
        _meta={"turn_trace_id": "trace.patch.1", "conversation_id": "conv.skill.builder_skill.default.builder-pa-patch"},
    )

    assert published == []
    assert result["pending_action"] is None
    assert "pending_action_id" not in result["patch"]


def test_update_current_scenario_does_not_call_pending_action_service_for_local_revision(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "pending_timeout"

    class _Service:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **_kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            (artifact_root / "scenario.json").write_text(
                '{"id":"pending_timeout","version":"0.1.0","name":"pending_timeout","steps":[]}',
                encoding="utf-8",
            )
            return {"ok": True, "draft": {"draft_id": "draft.pending.timeout"}, "artifact_root": str(artifact_root)}

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {"dev_webspace_id": "builder-pending-timeout-dev", "active_draft_id": kwargs.get("active_draft_id")}

        def snapshot(self, *args, **kwargs):
            return {"preview_state": kwargs.get("preview_state") or {}}

    import adaos.services.builder.workspace as workspace
    import adaos.services.pending_actions as pending_actions

    def _slow_publish(**_kwargs):
        time.sleep(0.2)
        return {"id": "pa.too-late"}

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _Service)
    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(skill, "PENDING_ACTION_TIMEOUT_S", 0.02)
    monkeypatch.setattr(pending_actions, "publish_pending_action", _slow_publish)

    skill.create_scenario_draft("create todo list", webspace_id="builder-pending-timeout")
    result = skill.update_current_scenario("show cards", webspace_id="builder-pending-timeout")

    assert result["ui_revision"]["revision"] == "002"
    assert result["pending_action"] is None
    assert result["message_actions"]
    assert "\u0420\u0435\u0432\u0438\u0437\u0438\u044f UI: 002" in result["message"]


def test_update_current_scenario_adds_product_units_and_filters(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "shopping_list"
    artifact_root.mkdir(parents=True)
    (artifact_root / "scenario.json").write_text(
        '{"id":"shopping_list","version":"0.1.0","name":"shopping_list","steps":[]}',
        encoding="utf-8",
    )

    class _Workbench:
        def get_workspace_binding(self, webspace_id):
            return {}

        def set_active_draft(self, **kwargs):
            return dict(kwargs)

        def snapshot(self, webspace_id, *, preview_state=None):
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

    import adaos.services.pending_actions as pending_actions

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(pending_actions, "publish_pending_action", lambda **kwargs: {"id": "pa.builder.filters"})
    skill._save_session(
        "builder-filters",
        {
            "id": "builder_session_filters",
            "webspace_id": "builder-filters",
            "status": "drafting",
            "title": "Shopping list",
            "scenario_id": "shopping_list",
            "draft_id": "draft.shopping",
            "artifact_root": str(artifact_root),
            "datasource_id": "shopping_items",
            "fields": [
                {"id": "item", "type": "string", "label": "\u0422\u043e\u0432\u0430\u0440", "required": True},
                {"id": "quantity", "type": "number", "label": "\u041a\u043e\u043b-\u0432\u043e", "required": False},
                {"id": "done", "type": "boolean", "label": "\u041a\u0443\u043f\u043b\u0435\u043d\u043e", "required": False},
            ],
            "patches": [],
            "version": "v1",
        },
    )

    unit_result = skill.update_current_scenario("\u0414\u043e\u0431\u0430\u0432\u044c \u043c\u0435\u0440\u0443 \u043f\u043e \u0442\u043e\u0432\u0430\u0440\u0430\u043c. \u0422\u0438\u043f\u0430. \u0448\u0442., \u043a\u0433, \u0433., \u043b.", webspace_id="builder-filters")
    assert unit_result["patch"]["operation"] == "add_field"
    assert any(item["id"] == "unit" and item["options"] == ["\u0448\u0442", "\u043a\u0433", "\u0433", "\u043b"] for item in unit_result["preview_state"]["datasources"][0]["fields"])

    filter_result = skill.update_current_scenario("\u0414\u043e\u0431\u0430\u0432\u044c \u043f\u043e\u043b\u0435 \u041d\u0430\u043b\u0438\u0447\u0438\u0435. \u0414\u043e\u0431\u0430\u0432\u044c \u0444\u0438\u043b\u044c\u0442\u0440 \u043f\u043e \u041a\u0443\u043f\u043b\u0435\u043d\u043e \u0438 \u041d\u0430\u043b\u0438\u0447\u0438\u0435.", webspace_id="builder-filters")
    assert filter_result["patch"]["operation"] == "multi_update"
    assert filter_result["patch"]["diff"]["not_implemented"] == []
    filters = filter_result["preview_state"]["filters"]
    assert {item["field_id"] for item in filters} == {"done", "availability"}

    page_schema = yaml.safe_load((artifact_root / "scenario.json").read_text(encoding="utf-8"))["ui"]["application"]["desktop"]["pageSchema"]
    widget_ids = {widget["id"] for widget in page_schema["widgets"]}
    assert {"prototype-filter-done", "prototype-filter-availability", "prototype-table"}.issubset(widget_ids)
    table = next(widget for widget in page_schema["widgets"] if widget["id"] == "prototype-table")
    assert {item["key"] for item in table["inputs"]["filters"]} == {"done", "availability"}


def test_builder_pending_action_approve_marks_patch_and_emits_chat(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "shopping_list"
    artifact_root.mkdir(parents=True)
    (artifact_root / "scenario.json").write_text(
        '{"id":"shopping_list","version":"0.1.0","name":"shopping_list","steps":[]}',
        encoding="utf-8",
    )
    emitted: list[str] = []

    class _Workbench:
        def get_workspace_binding(self, webspace_id):
            return {}

        def set_active_draft(self, **kwargs):
            return dict(kwargs)

        def snapshot(self, webspace_id, *, preview_state=None):
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

    import adaos.sdk.io.out as io_out

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(io_out, "chat_append", lambda text, **_kwargs: emitted.append(text))
    skill._save_session(
        "builder-approve",
        {
            "id": "builder_session_approve",
            "webspace_id": "builder-approve",
            "status": "drafting",
            "title": "Shopping list",
            "scenario_id": "shopping_list",
            "draft_id": "draft.shopping",
            "artifact_root": str(artifact_root),
            "datasource_id": "shopping_items",
            "fields": [{"id": "item", "type": "string", "label": "\u0422\u043e\u0432\u0430\u0440", "required": True}],
            "patches": [{"id": "patch_1", "operation": "add_field", "status": "applied", "pending_action_id": "pa.builder.1"}],
            "pending_action_id": "pa.builder.1",
            "version": "v2",
        },
    )

    asyncio.run(
        skill._on_builder_pending_action_response(
            {
                "pending_action_id": "pa.builder.1",
                "response_action_id": "approve",
                "webspace_id": "builder-approve",
                "domain_ref": {
                    "session_id": "builder_session_approve",
                    "scenario_id": "shopping_list",
                    "patch_id": "patch_1",
                },
                "pending_action": {"id": "pa.builder.1", "webspace_id": "builder-approve"},
                "response": {"response_action_id": "approve"},
            }
        )
    )

    session = skill._load_session("builder-approve", "builder_session_approve")
    assert session["patches"][0]["review_status"] == "approved"
    assert "pending_action_id" not in session
    assert any("\u0443\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u044b" in text for text in emitted)


def test_chat_from_dev_webspace_updates_source_session_and_mirrors_response(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "shopping_list"
    artifact_root.mkdir(parents=True, exist_ok=True)
    (artifact_root / "scenario.json").write_text(
        '{"id":"shopping_list","version":"0.1.0","name":"shopping_list","steps":[]}',
        encoding="utf-8",
    )
    emitted: list[dict] = []
    monkeypatch.setenv("ADAOS_BUILDER_LLM_IN_TESTS", "1")

    class _Workbench:
        def get_workspace_binding(self, webspace_id):
            assert webspace_id == "desktop"
            return {
                "source_webspace_id": "desktop",
                "dev_webspace_id": "desktop-dev",
                "active_draft_id": "draft.shopping",
                "runtime_scenario_id": "shopping_list",
            }

        def set_active_draft(self, **kwargs):
            return {
                "source_webspace_id": kwargs.get("source_webspace_id"),
                "dev_webspace_id": "desktop-dev",
                "active_draft_id": kwargs.get("active_draft_id"),
                "runtime_scenario_id": kwargs.get("runtime_scenario_id"),
            }

        def snapshot(self, webspace_id, *, preview_state=None):
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

    import adaos.sdk.io.out as io_out
    import adaos.services.pending_actions as pending_actions

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(pending_actions, "publish_pending_action", lambda **kwargs: {"id": "pa.sample"})
    monkeypatch.setattr(
        io_out,
        "chat_append",
        lambda text, *, from_="hub", msg_id=None, ts=None, _meta=None: emitted.append({"text": text, "meta": dict(_meta or {})}) or {"ok": True},
    )

    def _llm_transform(**kwargs):
        preview = json.loads(json.dumps(kwargs["preview_state"]))
        preview["mock_data"] = {
            "shopping_items": [
                {"item": "Milk", "quantity": 2, "category": "Dairy", "done": False, "price": 89.9},
                {"item": "Bread", "quantity": 1, "category": "Bakery", "done": True, "price": 54.0},
            ]
        }
        page_schema = skill._page_schema_from_preview(preview)
        return {
            "ok": True,
            "payload": {"schema": "adaos.webui.v1", "generated_by": "builder_skill", "ui": {"application": {"desktop": {"pageSchema": page_schema}}}},
            "preview_state": preview,
            "comment": "Updated sample data.",
            "validation": {"ok": True},
        }

    monkeypatch.setattr(skill, "_apply_llm_webui_transform", _llm_transform)
    skill._save_session(
        "desktop",
        {
            "id": "builder_session_test",
            "webspace_id": "desktop",
            "status": "drafting",
            "title": "Shopping list",
            "scenario_id": "shopping_list",
            "draft_id": "draft.shopping",
            "artifact_root": str(artifact_root),
            "datasource_id": "shopping_items",
            "fields": [
                {"id": "item", "type": "string", "label": "\u0422\u043e\u0432\u0430\u0440", "required": True},
                {"id": "quantity", "type": "number", "label": "\u041a\u043e\u043b-\u0432\u043e", "required": False},
                {"id": "category", "type": "string", "label": "\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f", "required": False},
                {"id": "done", "type": "boolean", "label": "\u041a\u0443\u043f\u043b\u0435\u043d\u043e", "required": False},
                {"id": "price", "type": "number", "label": "\u0426\u0435\u043d\u0430", "required": False},
            ],
            "patches": [],
            "version": "v1",
        },
    )

    result = skill.chat("\u0421\u0434\u0435\u043b\u0430\u0439 \u043f\u0440\u0438\u043c\u0435\u0440 \u0434\u0430\u043d\u043d\u044b\u0445 \u043d\u0430 \u043e\u0441\u043d\u043e\u0432\u0435 \u043f\u0440\u043e\u0434\u0443\u043a\u0442\u043e\u0432 \u043f\u0438\u0442\u0430\u043d\u0438\u044f", webspace_id="desktop-dev")

    assert result["ok"] is True
    assert result["patch"]["operation"] == "llm_webui_transform"
    rows = result["preview_state"]["mock_data"]["shopping_items"]
    assert rows[0]["item"] == "Milk"
    assert {item["meta"]["webspace_id"] for item in emitted} == {"desktop", "desktop-dev"}


def test_chat_requires_selected_builder_target(monkeypatch) -> None:
    skill = _load_module()

    class _Workbench:
        def get_workspace_binding(self, webspace_id):
            return {
                "source_webspace_id": webspace_id,
                "dev_webspace_id": f"{webspace_id}-dev",
                "active_draft_id": None,
                "runtime_scenario_id": None,
            }

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_safe_emit_chat", lambda *args, **kwargs: None)

    result = skill.chat("\u0434\u043e\u0431\u0430\u0432\u044c \u043f\u043e\u043b\u0435 \u0446\u0435\u043d\u0430", webspace_id="desktop")

    assert result["ok"] is True
    assert result["status"] == "target_required"
    assert result["needs_selection"] is True
    assert "target" in result["message"].lower() or "\u0432\u044b\u0431\u0435\u0440" in result["message"].lower()


def test_chat_does_not_create_project_for_edit_like_request_without_target(monkeypatch) -> None:
    skill = _load_module()
    created: list[dict] = []

    class _Workbench:
        def get_workspace_binding(self, webspace_id):
            return {
                "source_webspace_id": webspace_id,
                "dev_webspace_id": f"{webspace_id}-dev",
                "active_draft_id": None,
                "runtime_scenario_id": None,
            }

    def _fake_create(*args, **kwargs):
        created.append({"args": args, "kwargs": kwargs})
        return {"ok": True, "scenario_id": "unexpected"}

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "create_scenario_draft", _fake_create)
    monkeypatch.setattr(skill, "_safe_emit_chat", lambda *args, **kwargs: None)

    result = skill.chat(
        "\u0414\u043e\u0431\u0430\u0432\u044c \u043f\u043e\u043b\u0435 \u041f\u0440\u043e\u0435\u043a\u0442 \u0438 \u0441\u0433\u0440\u0443\u043f\u043f\u0438\u0440\u0443\u0439 \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0438 \u043f\u043e \u043f\u0440\u043e\u0435\u043a\u0442\u0430\u043c. "
        "\u0421\u043e\u0437\u0434\u0430\u0439 \u043f\u0440\u0438\u043c\u0435\u0440 \u0434\u0430\u043d\u043d\u044b\u0445 \u0434\u043b\u044f \u0434\u0432\u0443\u0445 \u043f\u0440\u043e\u0435\u043a\u0442\u043e\u0432 \u0434\u043b\u044f \u043d\u0430\u0433\u043b\u044f\u0434\u043d\u043e\u0441\u0442\u0438.",
        webspace_id="desktop",
    )

    assert result["ok"] is True
    assert result["status"] == "target_required"
    assert result["needs_selection"] is True
    assert not created


def test_builder_command_parser_prioritises_project_commands() -> None:
    skill = _load_module()

    switch = skill._parse_builder_command("\u0421\u0442\u0440\u043e\u0438\u0442\u0435\u043b\u044c, \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0438\u0441\u044c \u043d\u0430 \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439 demo_scenario", has_session=True)
    delete_field = skill._parse_builder_command("\u0443\u0434\u0430\u043b\u0438 \u043f\u043e\u043b\u0435 \u0446\u0435\u043d\u0430", has_session=True)
    create = skill._parse_builder_command("\u0441\u043e\u0437\u0434\u0430\u0439 \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435 \u0441\u043f\u0438\u0441\u043e\u043a \u043f\u043e\u043a\u0443\u043f\u043e\u043a", has_session=True)
    edit_like_without_session = skill._parse_builder_command(
        "\u0434\u043e\u0431\u0430\u0432\u044c \u043f\u043e\u043b\u0435 \u043f\u0440\u043e\u0435\u043a\u0442 \u0438 \u0441\u043e\u0437\u0434\u0430\u0439 \u043f\u0440\u0438\u043c\u0435\u0440 \u0434\u0430\u043d\u043d\u044b\u0445",
        has_session=False,
    )

    assert switch["intent"] == "project.switch"
    assert switch["project_ref"] == "demo_scenario"
    assert delete_field["intent"] == "none"
    assert create["intent"] == "project.create"
    assert edit_like_without_session["intent"] == "none"


def test_prompt_project_selection_defers_heavy_events(monkeypatch) -> None:
    skill = _load_module()
    calls: list[str] = []
    async_seen = threading.Event()

    import adaos.sdk.data.events as events

    def _publish(topic, payload, source=None):
        calls.append(topic)
        if topic == "prompt.project.changed":
            time.sleep(0.3)
        if topic == "builder.preview.selected":
            async_seen.set()

    monkeypatch.setattr(events, "publish", _publish)

    started = time.perf_counter()
    result = skill._publish_prompt_project_selection(
        "desktop",
        session={"scenario_id": "todo_list", "draft_id": "draft.todo"},
        reason="test",
    )
    elapsed = time.perf_counter() - started

    assert result["ok"] is True
    assert result["published"] == ["scenario.workflow.set_state"]
    assert result["scheduled"] == ["prompt.project.changed", "builder.preview.selected"]
    assert elapsed < 0.2
    assert calls[:1] == ["scenario.workflow.set_state"]
    assert async_seen.wait(timeout=1.0)


def test_chat_handles_builder_project_commands(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    emitted: list[dict] = []
    published: list[dict] = []
    calls: list[dict] = []
    binding = {
        "source_webspace_id": "desktop",
        "dev_webspace_id": "desktop-dev",
        "active_draft_id": "draft.beta",
        "runtime_scenario_id": "beta_scenario",
    }

    class _Workbench:
        def get_workspace_binding(self, webspace_id):
            return dict(binding)

        def set_active_draft(self, *, source_webspace_id=None, active_draft_id=None, runtime_scenario_id=None, persist_projection=True):
            binding.update(
                {
                    "source_webspace_id": source_webspace_id,
                    "dev_webspace_id": f"{source_webspace_id}-dev",
                    "active_draft_id": active_draft_id,
                    "runtime_scenario_id": runtime_scenario_id,
                }
            )
            calls.append(
                {
                    "method": "set_active_draft",
                    "active_draft_id": active_draft_id,
                    "runtime_scenario_id": runtime_scenario_id,
                    "persist_projection": persist_projection,
                }
            )
            return dict(binding)

        def snapshot(self, webspace_id, *, preview_state=None):
            calls.append({"method": "snapshot", "webspace_id": webspace_id})
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

    import adaos.services.pending_actions as pending_actions

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(skill, "_safe_emit_chat", lambda text, **kwargs: emitted.append({"text": text, "kwargs": kwargs}))
    monkeypatch.setattr(
        pending_actions,
        "publish_pending_action",
        lambda **kwargs: published.append(dict(kwargs)) or {"id": f"pa.builder.{len(published)}", "kind": kwargs["kind"]},
    )

    base_session = {
        "webspace_id": "desktop",
        "status": "drafting",
        "datasource_id": "items",
        "fields": [{"id": "title", "type": "string", "label": "Title", "required": True}],
        "patches": [],
        "version": "v1",
        "artifact_root": str(tmp_path),
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    skill._save_session(
        "desktop",
        {
            **base_session,
            "id": "session_alpha",
            "title": "Alpha",
            "scenario_id": "alpha_scenario",
            "draft_id": "draft.alpha",
        },
    )
    skill._save_session(
        "desktop",
        {
            **base_session,
            "id": "session_beta",
            "title": "Beta",
            "scenario_id": "beta_scenario",
            "draft_id": "draft.beta",
        },
    )

    listed = skill.chat("\u043f\u043e\u043a\u0430\u0436\u0438 \u043f\u0440\u043e\u0435\u043a\u0442\u044b", webspace_id="desktop")
    current = skill.chat("\u0447\u0442\u043e \u0432\u044b\u0431\u0440\u0430\u043d\u043e", webspace_id="desktop")
    switched = skill.chat("\u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0438\u0441\u044c \u043d\u0430 \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439 alpha_scenario", webspace_id="desktop")
    delete = skill.chat("\u0443\u0434\u0430\u043b\u0438 \u0442\u0435\u043a\u0443\u0449\u0438\u0439", webspace_id="desktop")

    assert listed["status"] == "project_list"
    assert {item["scenario_id"] for item in listed["items"]} == {"alpha_scenario", "beta_scenario"}
    assert current["status"] == "project_current"
    assert current["scenario_id"] == "beta_scenario"
    assert switched["status"] == "project_switched"
    assert switched["scenario_id"] == "alpha_scenario"
    assert binding["active_draft_id"] == "draft.alpha"
    assert binding["runtime_scenario_id"] == "alpha_scenario"
    assert delete["status"] == "delete_review_required"
    assert delete["pending_action"]["id"] == "pa.builder.1"
    assert published[0]["kind"] == "builder.scenario_delete.review"
    assert published[0]["domain_ref"]["operation"] == "delete_draft"
    assert published[0]["domain_ref"]["draft_id"] == "draft.alpha"
    assert emitted[-1]["kwargs"]["topic_ref"]["thread_id"] == delete["topic"]["thread_id"]
    assert any(item["method"] == "set_active_draft" and item["active_draft_id"] == "draft.alpha" for item in calls)


def test_builder_delete_pending_action_approve_deletes_draft(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    calls: list[dict] = []
    emitted: list[dict] = []

    class _Workbench:
        def get_workspace_binding(self, webspace_id):
            return {
                "source_webspace_id": webspace_id,
                "dev_webspace_id": f"{webspace_id}-dev",
                "active_draft_id": "draft.to_delete",
                "runtime_scenario_id": "delete_scenario",
            }

        def delete_development_skill(self, draft_id, webspace_id):
            calls.append({"method": "delete", "draft_id": draft_id, "webspace_id": webspace_id})
            return {"ok": True, "draft_id": draft_id}

        def set_active_draft(self, *, source_webspace_id=None, active_draft_id=None, runtime_scenario_id=None, persist_projection=True):
            calls.append({"method": "set_active_draft", "active_draft_id": active_draft_id})
            return {
                "source_webspace_id": source_webspace_id,
                "dev_webspace_id": f"{source_webspace_id}-dev",
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
            }

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_safe_emit_chat", lambda text, **kwargs: emitted.append({"text": text, "kwargs": kwargs}))
    skill._save_session(
        "desktop",
        {
            "id": "session_delete",
            "webspace_id": "desktop",
            "status": "drafting",
            "title": "Delete me",
            "scenario_id": "delete_scenario",
            "draft_id": "draft.to_delete",
            "artifact_root": str(tmp_path),
            "datasource_id": "items",
            "fields": [{"id": "title", "type": "string", "label": "Title"}],
            "patches": [],
            "version": "v1",
        },
    )

    asyncio.run(
        skill._on_builder_pending_action_response(
            {
                "webspace_id": "desktop",
                "response_action_id": "approve",
                "pending_action_id": "pa.delete",
                "domain_ref": {
                    "session_id": "session_delete",
                    "scenario_id": "delete_scenario",
                    "draft_id": "draft.to_delete",
                    "operation": "delete_draft",
                },
            }
        )
    )

    assert calls[0] == {"method": "delete", "draft_id": "draft.to_delete", "webspace_id": "desktop"}
    assert skill._load_session("desktop", "session_delete") is None
    assert "draft.to_delete" in emitted[0]["text"]


def test_builder_skill_exposes_workbench_tools() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))

    tools = {item["name"] for item in manifest["tools"]}
    assert {
        "ensure_dev_webspace",
        "get_workspace_binding",
        "open_dev_webspace",
        "attach_dialog_widget",
        "set_active_draft",
        "list_development_skills",
        "delete_development_skill",
    }.issubset(tools)
    routes = {item["path"] for item in manifest["data_routes"]}
    assert "data.builder" in routes


def test_get_session_exposes_developer_evidence(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "shopping_list"
    artifact_root.mkdir(parents=True)
    (artifact_root / "webui.json").write_text('{"preview_state":{}}', encoding="utf-8")
    (artifact_root / "scenario.json").write_text('{"id":"shopping_list"}', encoding="utf-8")

    class _Workbench:
        def set_active_draft(self, **kwargs):
            return {
                "source_webspace_id": kwargs.get("source_webspace_id"),
                "dev_webspace_id": f"{kwargs.get('source_webspace_id')}-dev",
                "active_draft_id": kwargs.get("active_draft_id"),
                "runtime_scenario_id": kwargs.get("runtime_scenario_id"),
            }

        def snapshot(self, webspace_id, *, preview_state=None):
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(skill, "_request_workbench_refresh", lambda payload: {"ok": True, "payload": dict(payload)})
    monkeypatch.setattr(
        skill,
        "_builder_topic_ref",
        lambda webspace_id, **_kwargs: {
            "schema": "adaos.conversation.topic_ref.v1",
            "topic_id": f"builder:{webspace_id}:shopping_list",
            "thread_id": f"thread.builder.{webspace_id}.shopping_list",
            "conversation_id": f"conv.skill.builder_skill.default.{webspace_id}",
            "channel_id": "builder",
            "owner": "skill:builder_skill",
        },
    )
    skill._save_session(
        "builder-evidence",
        {
            "id": "builder_session_evidence",
            "webspace_id": "builder-evidence",
            "status": "drafting",
            "title": "Shopping list",
            "scenario_id": "shopping_list",
            "draft_id": "draft.shopping",
            "artifact_root": str(artifact_root),
            "datasource_id": "shopping_items",
            "fields": [{"id": "item", "type": "string", "label": "Item", "required": True}],
            "preview_state": {
                "current_ui": {"type": "page"},
                "datasources": [{"id": "shopping_items", "type": "internal_crud"}],
                "pending_patches": [{"id": "patch_1"}],
            },
            "patches": [
                {
                    "id": "patch_1",
                    "operation": "add_field",
                    "status": "applied",
                    "pending_action_id": "pa.patch",
                    "diff": {"fields": [{"id": "price"}], "not_implemented": []},
                }
            ],
            "pending_action_id": "pa.draft",
            "version": "v2",
        },
    )

    session_result = skill.get_session(webspace_id="builder-evidence")
    evidence = session_result["developer_evidence"]

    assert session_result["ok"] is True
    assert evidence["schema"] == "adaos.builder.developer_evidence.v1"
    assert evidence["route_plan"]["thread_id"] == "thread.builder.builder-evidence.shopping_list"
    assert evidence["route_plan"]["default_tool"] == "builder_skill.chat"
    assert evidence["preview_refs"]["current_ui_type"] == "page"
    assert evidence["preview_refs"]["datasource_ids"] == ["shopping_items"]
    assert set(evidence["pending_action_ids"]) == {"pa.draft", "pa.patch"}
    assert evidence["patches"][0]["diff_keys"] == ["fields", "not_implemented"]
    files = {item["role"]: item for item in evidence["files"]}
    assert files["runtime_preview"]["exists"] is True
    assert files["scenario_manifest_json"]["exists"] is True

    preview_result = skill.get_preview_state(webspace_id="builder-evidence")
    assert preview_result["developer_evidence"]["preview_refs"]["pending_patch_count"] == 1


def test_create_scenario_draft_updates_builder_workbench(monkeypatch, tmp_path) -> None:
    skill = _load_module()
    artifact_root = tmp_path / "shopping_list"
    calls: list[dict] = []

    class _DraftService:
        @classmethod
        def from_context(cls):
            return cls()

        def create_draft(self, **kwargs):
            artifact_root.mkdir(parents=True, exist_ok=True)
            return {"ok": True, "draft": {"draft_id": "draft.shopping"}, "artifact_root": str(artifact_root), "kwargs": kwargs}

    class _Workbench:
        def set_active_draft(self, *, source_webspace_id=None, active_draft_id=None, runtime_scenario_id=None, persist_projection=True):
            calls.append({
                "method": "set_active_draft",
                "webspace_id": source_webspace_id,
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
                "persist_projection": persist_projection,
            })
            return {
                "source_webspace_id": source_webspace_id,
                "dev_webspace_id": f"{source_webspace_id}-dev",
                "scenario_id": "prompt_engineer_scenario",
                "runtime_scenario_id": runtime_scenario_id,
                "active_draft_id": active_draft_id,
                "dialog": {"widget": "voice_chat", "dialog_channel_id": "builder"},
            }

        def snapshot(self, webspace_id, *, preview_state=None):
            calls.append({"method": "snapshot", "webspace_id": webspace_id, "preview_state": preview_state})
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

    import adaos.services.builder.workspace as workspace

    monkeypatch.setattr(workspace, "BuilderWorkspaceService", _DraftService)
    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(
        skill,
        "_request_workbench_refresh",
        lambda payload: calls.append({"method": "event", "payload": dict(payload)}) or {"ok": True},
    )
    refresh_calls: list[dict] = []
    monkeypatch.setattr(
        skill,
        "_schedule_dev_runtime_reload_after_revision",
        lambda webspace_id, **kwargs: refresh_calls.append({"webspace_id": webspace_id, **kwargs})
        or {"ok": True, "scheduled": True, "webspace_id": "desktop-dev"},
    )

    result = skill.create_scenario_draft("Builder, create a shopping list app", webspace_id="desktop")

    assert result["ok"] is True
    assert result["workbench"]["binding"]["dev_webspace_id"] == "desktop-dev"
    assert result["workbench"]["binding"]["active_draft_id"] == "draft.shopping"
    assert calls[0] == {
        "method": "set_active_draft",
        "webspace_id": "desktop",
        "active_draft_id": "draft.shopping",
        "runtime_scenario_id": result["scenario_id"],
        "persist_projection": False,
    }
    assert [item["method"] for item in calls[:1]] == ["set_active_draft"]
    assert {item["method"] for item in calls}.issubset({"set_active_draft", "ensure_dev_webspace"})
    assert result["dev_runtime_refresh"]["scheduled"] is True
    assert refresh_calls[-1]["webspace_id"] == "desktop"
    assert refresh_calls[-1]["revision"] == "001"
    assert refresh_calls[-1]["session"]["scenario_id"] == result["scenario_id"]


def test_ensure_workbench_prefers_direct_dev_runtime_switch(monkeypatch) -> None:
    skill = _load_module()
    calls: list[dict] = []

    class _Workbench:
        def set_active_draft(self, *, source_webspace_id=None, active_draft_id=None, runtime_scenario_id=None, persist_projection=True):
            calls.append({
                "method": "set_active_draft",
                "source_webspace_id": source_webspace_id,
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
                "persist_projection": persist_projection,
            })
            return {
                "source_webspace_id": source_webspace_id,
                "dev_webspace_id": f"{source_webspace_id}-dev",
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
            }

        def snapshot(self, webspace_id, *, preview_state=None):
            calls.append({"method": "snapshot", "webspace_id": webspace_id, "preview_state": preview_state})
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

        def ensure_dev_webspace(self, source_webspace_id, *, active_draft_id=None, runtime_scenario_id=None, preview_state=None, wait_for_rebuild=None):
            calls.append({
                "method": "ensure_dev_webspace",
                "source_webspace_id": source_webspace_id,
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
                "preview_state": preview_state,
                "wait_for_rebuild": wait_for_rebuild,
            })
            return {
                "source_webspace_id": source_webspace_id,
                "dev_webspace_id": f"{source_webspace_id}-dev",
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
                "runtime": {"ok": True, "scenario_id": runtime_scenario_id},
            }

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(
        skill,
        "_request_workbench_refresh",
        lambda payload: calls.append({"method": "event", "payload": dict(payload)}) or {"ok": True},
    )

    result = skill._ensure_workbench(
        "desktop",
        active_draft_id="draft.todo",
        runtime_scenario_id="todo_scenario",
        preview_state={"title": "Todo"},
    )

    assert result["ok"] is True
    assert result["binding"]["runtime_scenario_id"] == "todo_scenario"
    assert result["projection"]["event"]["skipped"] == "direct_workbench_ensure"
    assert result["projection"]["direct"]["result"]["runtime"]["ok"] is True
    assert [item["method"] for item in calls] == ["set_active_draft", "snapshot", "ensure_dev_webspace"]
    assert calls[2]["runtime_scenario_id"] == "todo_scenario"
    assert calls[2]["wait_for_rebuild"] is False


def test_ensure_workbench_can_defer_runtime_switch_for_ui_revision_updates(monkeypatch) -> None:
    skill = _load_module()
    calls: list[dict] = []

    class _Workbench:
        def set_active_draft(self, *, source_webspace_id=None, active_draft_id=None, runtime_scenario_id=None, persist_projection=True):
            calls.append({
                "method": "set_active_draft",
                "source_webspace_id": source_webspace_id,
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
                "persist_projection": persist_projection,
            })
            return {
                "source_webspace_id": source_webspace_id,
                "dev_webspace_id": f"{source_webspace_id}-dev",
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
            }

        def snapshot(self, webspace_id, *, preview_state=None):
            calls.append({"method": "snapshot", "webspace_id": webspace_id, "preview_state": preview_state})
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

        def ensure_dev_webspace(self, *args, **kwargs):
            calls.append({"method": "ensure_dev_webspace", "args": args, "kwargs": kwargs})
            return {"source_webspace_id": "unexpected", "runtime": {"ok": True}}

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(
        skill,
        "_request_workbench_refresh",
        lambda payload: calls.append({"method": "event", "payload": dict(payload)}) or {"ok": True},
    )

    result = skill._ensure_workbench(
        "desktop",
        active_draft_id="draft.todo",
        runtime_scenario_id="todo_scenario",
        preview_state={"title": "Todo"},
        refresh_runtime=False,
    )

    assert result["ok"] is True
    assert result["binding"]["runtime_scenario_id"] == "todo_scenario"
    assert result["projection"]["direct"]["skipped"] == "runtime_refresh_deferred_to_dev_reload"
    assert result["projection"]["event"]["skipped"] == "runtime_refresh_deferred_to_dev_reload"
    methods = [item["method"] for item in calls]
    assert methods[:2] == ["set_active_draft", "snapshot"]
    assert set(methods).issubset({"set_active_draft", "snapshot", "ensure_dev_webspace"})


def test_ensure_workbench_can_defer_snapshot_projection_for_pointer_switches(monkeypatch) -> None:
    skill = _load_module()
    calls: list[dict] = []

    class _Workbench:
        def set_active_draft(self, *, source_webspace_id=None, active_draft_id=None, runtime_scenario_id=None, persist_projection=True):
            calls.append({
                "method": "set_active_draft",
                "source_webspace_id": source_webspace_id,
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
                "persist_projection": persist_projection,
            })
            return {
                "source_webspace_id": source_webspace_id,
                "dev_webspace_id": f"{source_webspace_id}-dev",
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
            }

        def snapshot(self, webspace_id, *, preview_state=None):
            calls.append({"method": "snapshot", "webspace_id": webspace_id, "preview_state": preview_state})
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())

    result = skill._ensure_workbench(
        "desktop",
        active_draft_id="draft.todo",
        runtime_scenario_id="todo_scenario",
        preview_state={"title": "Todo"},
        refresh_runtime=False,
        snapshot_projection=False,
    )

    assert result["ok"] is True
    assert result["projection"]["snapshot"]["skipped"] == "snapshot_projection_deferred"
    assert result["projection"]["snapshot_deferred"] is True
    assert [item["method"] for item in calls] == ["set_active_draft"]


def test_ensure_workbench_schedules_async_direct_runtime_switch(monkeypatch) -> None:
    skill = _load_module()
    calls: list[dict] = []

    class _Workbench:
        def set_active_draft(self, *, source_webspace_id=None, active_draft_id=None, runtime_scenario_id=None, persist_projection=True):
            calls.append({"method": "set_active_draft", "runtime_scenario_id": runtime_scenario_id})
            return {
                "source_webspace_id": source_webspace_id,
                "dev_webspace_id": f"{source_webspace_id}-dev",
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
            }

        def snapshot(self, webspace_id, *, preview_state=None):
            calls.append({"method": "snapshot", "webspace_id": webspace_id})
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

        async def ensure_dev_webspace(self, source_webspace_id, *, active_draft_id=None, runtime_scenario_id=None, preview_state=None, wait_for_rebuild=None):
            calls.append({"method": "ensure_dev_webspace", "runtime_scenario_id": runtime_scenario_id})
            await asyncio.sleep(1.0)
            return {"source_webspace_id": source_webspace_id, "runtime_scenario_id": runtime_scenario_id}

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(
        skill,
        "_request_workbench_refresh",
        lambda payload: calls.append({"method": "event", "payload": dict(payload)}) or {"ok": True, "payload": dict(payload)},
    )

    started = time.perf_counter()
    result = skill._ensure_workbench(
        "desktop",
        active_draft_id="draft.todo",
        runtime_scenario_id="todo_scenario",
        preview_state={"title": "Todo"},
    )
    elapsed = time.perf_counter() - started

    assert result["ok"] is True
    assert result["projection"]["direct"]["scheduled"] is True
    assert result["projection"]["direct"]["mode"] == "thread"
    assert result["projection"]["event"]["skipped"] == "direct_workbench_ensure"
    assert elapsed < 0.5
    methods = [item["method"] for item in calls]
    assert methods[:2] == ["set_active_draft", "snapshot"]
    assert methods[2:] in ([], ["ensure_dev_webspace"])


def test_safe_emit_chat_does_not_wait_for_stuck_append(monkeypatch) -> None:
    skill = _load_module()

    import adaos.sdk.io.out as io_out

    calls: list[str] = []

    def _slow_chat_append(text, **_kwargs):
        calls.append(text)
        time.sleep(1.0)

    monkeypatch.setattr(skill, "CHAT_APPEND_TIMEOUT_S", 0.02)
    monkeypatch.setattr(io_out, "chat_append", _slow_chat_append)

    started = time.perf_counter()
    skill._safe_emit_chat("hello", webspace_id="desktop")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert calls


def test_workbench_tool_wrappers_use_voice_widget_and_active_draft(monkeypatch) -> None:
    skill = _load_module()
    calls: list[dict] = []

    class _Workbench:
        def set_active_draft(self, *, source_webspace_id=None, active_draft_id=None, runtime_scenario_id=None, persist_projection=True):
            calls.append({
                "method": "set_active_draft",
                "webspace_id": source_webspace_id,
                "active_draft_id": active_draft_id,
                "runtime_scenario_id": runtime_scenario_id,
                "persist_projection": persist_projection,
            })
            return {"source_webspace_id": source_webspace_id, "dev_webspace_id": f"{source_webspace_id}-dev", "active_draft_id": active_draft_id}

        def get_workspace_binding(self, webspace_id):
            return {"source_webspace_id": webspace_id, "dev_webspace_id": f"{webspace_id}-dev", "active_draft_id": "draft.one"}

        def open_dev_webspace(self, webspace_id, *, base_url=None):
            return {"ok": True, "url": f"{base_url}/?webspace={webspace_id}-dev", "webspace_id": f"{webspace_id}-dev"}

        def snapshot(self, webspace_id, *, preview_state=None):
            calls.append({"method": "snapshot", "webspace_id": webspace_id, "preview_state": preview_state})
            return {"source_webspace_id": webspace_id, "preview_state": preview_state or {}}

        def dialog_widget_config(self, webspace_id):
            return {"widget": "voice_chat", "dialog_channel_id": "builder", "source_webspace_id": webspace_id}

        def list_development_skills(self, webspace_id):
            return {"ok": True, "items": [{"draft_id": "draft.one", "active": True}], "active_draft_id": "draft.one"}

        def delete_development_skill(self, draft_id, webspace_id):
            calls.append({"method": "delete", "webspace_id": webspace_id, "draft_id": draft_id})
            return {"ok": True, "draft_id": draft_id}

    monkeypatch.setattr(skill, "_workbench_service", lambda: _Workbench())
    monkeypatch.setattr(
        skill,
        "_request_workbench_refresh",
        lambda payload: calls.append({"method": "event", "payload": dict(payload)}) or {"ok": True},
    )

    assert skill.ensure_dev_webspace(webspace_id="desktop", active_draft_id="draft.one")["binding"]["dev_webspace_id"] == "desktop-dev"
    assert skill.get_workspace_binding(webspace_id="desktop")["binding"]["active_draft_id"] == "draft.one"
    assert skill.open_dev_webspace(webspace_id="desktop", base_url="http://localhost:8100")["url"] == "http://localhost:8100/?webspace=desktop-dev"
    assert skill.attach_dialog_widget(webspace_id="desktop")["widget"]["widget"] == "voice_chat"
    assert skill.set_active_draft("draft.two", webspace_id="desktop")["binding"]["active_draft_id"] == "draft.two"
    assert skill.list_development_skills(webspace_id="desktop")["items"][0]["draft_id"] == "draft.one"
    assert skill.delete_development_skill("draft.one", webspace_id="desktop")["ok"] is True
    assert calls[0] == {
        "method": "set_active_draft",
        "webspace_id": "desktop",
        "active_draft_id": "draft.one",
        "runtime_scenario_id": None,
        "persist_projection": False,
    }
    assert calls[-1] == {"method": "delete", "webspace_id": "desktop", "draft_id": "draft.one"}
