import importlib.util
from pathlib import Path

import pytest


def load():
    path = Path(__file__).resolve().parents[1] / "handlers/main.py"
    spec = importlib.util.spec_from_file_location("companions_lab_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_enabled_sage_delegates_to_core_tool_loop(monkeypatch):
    from adaos.services.companion import agent
    monkeypatch.setenv("ADAOS_COMPANION_SAGE_ENABLED", "true")
    skill = load()
    skill.reset_session(webspace_id="test-lab-loop")
    calls = []
    def run(text, **kwargs):
        calls.append((text, kwargs))
        return {"message": "По данным системы: 17.5%", "session_id": "s", "turn_id": "t", "used_llm": True, "used_mcp": True}
    monkeypatch.setattr(agent, "run_turn", run)
    monkeypatch.setattr(skill, "_companion_context", lambda *a: pytest.fail("eager context"))
    result = skill.talk("Мудрец, какая загрузка CPU?", webspace_id="test-lab-loop")
    assert result["selected_character"] == "sage" and result["session_id"] == "s"
    assert len(calls) == 1 and not calls[0][1]["history"]
    assert skill._session("test-lab-loop")["learning_turn_id"] == "t"


def test_disabled_sage_absent_and_explicit_execution_rejected(monkeypatch):
    monkeypatch.delenv("ADAOS_COMPANION_SAGE_ENABLED", raising=False)
    skill = load()
    assert "sage" not in skill._profiles("test-lab-off")
    with pytest.raises(PermissionError):
        skill.talk("привет", character_id="sage", webspace_id="test-lab-off")


def test_user_message_not_duplicated():
    skill = load()
    messages = skill._messages_for_llm(system_prompt="system", history=[{"role": "user", "text": "привет"}], user_text="привет")
    assert sum(m.get("content") == "привет" for m in messages) == 1
