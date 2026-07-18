from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    spec = importlib.util.spec_from_file_location("builder_automation_skill_test_handler", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeService:
    def start_from_execute(self, **kwargs):
        return {
            "ok": True,
            "status": "queued",
            "automation": {
                "schema": "adaos.builder.automation_projection.v1",
                "status": "queued",
                "iteration": 0,
                "project": {"type": kwargs["object_type"], "id": kwargs["object_id"]},
            },
        }

    def submit_turn(self, **_kwargs):
        return {
            "ok": True,
            "handled": True,
            "status": "automation_queued",
            "automation": {
                "schema": "adaos.builder.automation_projection.v1",
                "status": "queued",
                "iteration": 2,
            },
        }

    def projection(self, **_kwargs):
        return {
            "ok": False,
            "error": "automation_session_not_found",
            "automation": {
                "schema": "adaos.builder.automation_projection.v1",
                "status": "idle",
                "iteration": 0,
            },
        }


def test_start_returns_render_safe_projection(monkeypatch) -> None:
    module = _module()
    service = _FakeService()
    monkeypatch.setattr(module.builder_automation, "start", service.start_from_execute)

    result = module.start(
        object_type="scenario",
        object_id="recipes",
        implementation_brief="Implement the approved interactions.",
        webspace_id="desktop-dev",
        _meta={"locale": "en"},
    )

    assert result["ok"] is True
    assert result["status"] == "queued"
    assert result["automation"]["project"]["id"] == "recipes"
    assert result["message"] == "The automation task has been queued."


def test_chat_localizes_iteration_receipt(monkeypatch) -> None:
    module = _module()
    service = _FakeService()
    monkeypatch.setattr(module.builder_automation, "submit", service.submit_turn)
    monkeypatch.setattr(module.builder_automation, "get_state", service.projection)

    result = module.chat(text="Добавь тесты", webspace_id="desktop-dev")

    assert result["status"] == "automation_queued"
    assert result["message"] == "Итерация 2 поставлена в очередь."


def test_get_state_exposes_idle_state_without_faking_a_session(monkeypatch) -> None:
    module = _module()
    service = _FakeService()
    monkeypatch.setattr(module.builder_automation, "get_state", service.projection)

    result = module.get_state(webspace_id="desktop-dev")

    assert result["ok"] is False
    assert result["status"] == "automation_session_not_found"
    assert result["automation"]["status"] == "idle"
    assert "нет сессии" in result["message"]
