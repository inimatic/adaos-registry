from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import yaml


SKILL_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location("conversation_companions_under_test", SKILL_ROOT / "handlers" / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_declares_tools_and_nlu_actions() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))

    tools = {item["name"] for item in manifest["tools"]}
    assert {
        "start",
        "talk",
        "switch_character",
        "update_profile",
        "capture_feedback",
        "get_companion_context",
        "list_companion_activity",
        "execute_companion_action",
        "capture_capability_request",
    }.issubset(tools)
    assert manifest["default_tool"] == "talk"
    assert any(
        agent["id"] == "agent:conversation_companions:sage"
        for agent in manifest["conversation"]["agents"]
    )
    assert "conversation.start" in manifest["nlu"]["intents"]
    assert manifest["nlu"]["intents"]["conversation.talk"]["actions"][0]["tool"] == "talk"


def test_start_is_deterministic_and_lists_characters() -> None:
    skill = _load_module()
    skill.reset_session(webspace_id="test-start")

    result = skill.start(profile_hint="хочу советника", webspace_id="test-start")

    assert result["ok"] is True
    assert result["active_character"] == "arseni"
    assert result["dialog"]["dialog_channel_id"] == "conversational"
    assert result["dialog"]["default_tool"] == "conversation_companions.talk"
    assert result["dialog"]["active_agent_label"] == "Арсений"
    assert result["dialog"]["active_agent"]["kind"] == "skill_agent"
    assert result["dialog"]["active_agent"]["gender"] == "male"
    assert result["dialog"]["active_agent"]["voice"] == "ru-male"
    assert result["dialog"]["active_agent"]["icon"] == "male-outline"
    assert result["dialog"]["active_agent"]["voice_profile"]["lang"] == "ru-RU"
    assert "Арсений" in result["message"]
    assert len(result["characters"]) >= 3
    assert result["next_actions"]


def test_switch_character_accepts_russian_alias() -> None:
    skill = _load_module()
    skill.reset_session(webspace_id="test-switch")

    result = skill.switch_character("скептик", webspace_id="test-switch")
    listing = skill.list_characters(webspace_id="test-switch")

    assert result["ok"] is True
    assert result["selected_character"] == "nika"
    assert result["dialog"]["active_agent_id"] == "agent:conversation_companions:nika"
    assert result["dialog"]["active_agent_label"] == "Ника"
    assert result["dialog"]["active_agent"]["gender"] == "female"
    assert result["dialog"]["active_agent"]["voice"] == "ru-female"
    assert result["dialog"]["active_agent"]["icon"] == "female-outline"
    assert listing["active_character"] == "nika"


def test_update_profile_applies_bounded_style_patch() -> None:
    skill = _load_module()
    skill.reset_session(webspace_id="test-profile")

    result = skill.update_profile("говори короче и теплее, не задавай вопрос в конце", webspace_id="test-profile")

    assert result["ok"] is True
    assert result["dialog"]["dialog_channel_id"] == "conversational"
    assert result["patch"]["verbosity"] == "коротко, одна-две главные мысли"
    assert "теплее" in result["profile"]["tone"]
    assert any("Не заканчивает ответ вопросом" in rule for rule in result["profile"]["style_rules"])


def test_talk_routes_style_correction_to_profile_update() -> None:
    skill = _load_module()
    skill.reset_session(webspace_id="test-talk-profile")

    result = skill.talk("говори короче и теплее", preview=True, webspace_id="test-talk-profile")

    assert result["ok"] is True
    assert result["character_id"] == "arseni"
    assert result["patch"]["verbosity"] == "коротко, одна-две главные мысли"
    assert "Обновил профиль" in result["message"]


def test_talk_preview_uses_local_fallback_without_llm() -> None:
    skill = _load_module()
    skill.reset_session(webspace_id="test-talk")

    result = skill.talk("дай совет, как тестировать первого персонажа", preview=True, webspace_id="test-talk")

    assert result["ok"] is True
    assert result["selected_character"] == "arseni"
    assert result["dialog"]["active_agent_id"] == "agent:conversation_companions:arseni"
    assert "Арсений" in result["message"]


def test_talk_fallback_answers_common_factual_and_term_questions() -> None:
    skill = _load_module()
    skill.reset_session(webspace_id="test-talk-qa")

    beirut = skill.talk("Арсений, какая столица Бейрута?", preview=True, webspace_id="test-talk-qa")
    noise = skill.talk("Что такое шум?", preview=True, webspace_id="test-talk-qa")

    assert beirut["ok"] is True
    assert "Бейрут" in beirut["message"]
    assert "столицей Ливана" in beirut["message"]
    assert noise["ok"] is True
    assert "помеха" in noise["message"]
    assert noise["message"] != beirut["message"]


def test_sage_routes_allowlisted_request_through_companion_plane(monkeypatch) -> None:
    skill = _load_module()
    webspace_id = "test-sage-control"
    skill.reset_session(webspace_id=webspace_id)
    context = {
        "webspace_id": webspace_id,
        "context_digest": "sha256:" + ("a" * 64),
        "current_scenario": "web_desktop",
        "affordances": [{"id": "status.node_cpu.read", "available": True}],
        "published_voice_affordances": [],
    }
    calls = []

    monkeypatch.setattr(skill, "_companion_context", lambda _webspace_id: context)

    def call(tool_id, arguments=None):
        calls.append((tool_id, arguments))
        return {
            "receipt": {
                "action_id": "companion-action:test",
                "operation": "status.node_cpu.read",
                "status": "completed",
                "result": {"cpu": {"percent": 17.5}, "observed_at": "2026-10-05T12:00:00Z"},
            }
        }

    monkeypatch.setattr(skill, "_companion_mcp_call", call)

    message, result = skill._sage_control_reply(
        "Мудрец, какая сейчас загрузка процессора?",
        webspace_id,
    )

    assert result["used_mcp"] is True
    assert result["receipt"]["status"] == "completed"
    assert "17.5%" in message
    assert calls[0][0] == "companion.action.execute"
    assert calls[0][1]["context_digest"] == context["context_digest"]


def test_sage_parser_uses_published_affordance_and_typed_state() -> None:
    skill = _load_module()
    context = {
        "context_digest": "sha256:" + ("b" * 64),
        "published_voice_affordances": [
            {
                "id": "diagnostics.open",
                "labels": {"ru": "Диагностика"},
                "aliases": ["диагностику"],
            }
        ],
    }

    affordance = skill._sage_action_request(
        "Мудрец, открой диагностику",
        context,
        "desktop",
    )
    state = skill._sage_action_request(
        "Мудрец, установи флаг companion.verbose = false",
        context,
        "desktop",
    )

    assert affordance["operation"] == "ui.affordance.activate"
    assert affordance["params"] == {"affordance_id": "diagnostics.open"}
    assert state["operation"] == "ui.state.set"
    assert state["params"] == {"key": "companion.verbose", "value": False}


def test_sage_resolves_scenario_and_modal_by_published_catalog_name() -> None:
    skill = _load_module()
    context = {
        "context_digest": "sha256:" + ("d" * 64),
        "available_modal_ids": ["node:1:diagnostics_modal"],
        "published_voice_affordances": [],
        "catalog_apps": [
            {
                "id": "scenario:media_center",
                "scenario_id": "media_center",
                "title": "Media Center",
            },
            {
                "id": "diagnostics_app",
                "title": "Диагностика",
                "launchModal": "node:1:diagnostics_modal",
            },
        ],
    }

    scenario = skill._sage_action_request(
        "Мудрец, открой сценарий «Media Center»",
        context,
        "desktop",
    )
    modal = skill._sage_action_request(
        "Мудрец, открой Диагностику",
        context,
        "desktop",
    )

    assert scenario["operation"] == "ui.scenario.open"
    assert scenario["params"] == {"scenario_id": "media_center"}
    assert modal["operation"] == "ui.modal.open"
    assert modal["params"] == {"modal_id": "node:1:diagnostics_modal"}


def test_sage_records_only_explicit_capability_request(monkeypatch) -> None:
    skill = _load_module()
    webspace_id = "test-sage-wanted"
    skill.reset_session(webspace_id=webspace_id)
    context = {
        "webspace_id": webspace_id,
        "context_digest": "sha256:" + ("c" * 64),
        "current_scenario": "web_desktop",
        "affordances": [],
        "published_voice_affordances": [],
    }
    calls = []
    monkeypatch.setattr(skill, "_companion_context", lambda _webspace_id: context)

    def call(tool_id, arguments=None):
        calls.append((tool_id, arguments))
        return {
            "capability_request": {
                "status": "recorded",
                "ticket": {"id": "dticket.test-wanted"},
            }
        }

    monkeypatch.setattr(skill, "_companion_mcp_call", call)

    message, result = skill._sage_control_reply(
        "Мудрец, запиши в реестр хотелок: научиться запускать вечерний сценарий",
        webspace_id,
    )

    assert result["used_mcp"] is True
    assert result["capability_request"]["status"] == "recorded"
    assert "dticket.test-wanted" in message
    assert calls[0][0] == "companion.capability_request.capture"
    assert calls[0][1]["context_digest"] == context["context_digest"]
    assert calls[0][1]["summary"] == "научиться запускать вечерний сценарий"


def test_sage_refuses_cancel_when_no_receipt_is_cancellable(monkeypatch) -> None:
    skill = _load_module()
    webspace_id = "test-sage-cancel"
    skill.reset_session(webspace_id=webspace_id)
    context = {
        "webspace_id": webspace_id,
        "context_digest": "sha256:" + ("e" * 64),
        "current_scenario": "web_desktop",
        "affordances": [{"id": "action.cancel", "available": True}],
        "published_voice_affordances": [],
    }
    calls = []
    monkeypatch.setattr(skill, "_companion_context", lambda _webspace_id: context)

    def call(tool_id, arguments=None):
        calls.append((tool_id, arguments))
        return {
            "receipts": [
                {
                    "action_id": "companion-action:done",
                    "status": "completed",
                    "cancellable": False,
                }
            ]
        }

    monkeypatch.setattr(skill, "_companion_mcp_call", call)

    message, result = skill._sage_control_reply(
        "Мудрец, отмени последнее действие",
        webspace_id,
    )

    assert result["receipt"] is None
    assert "нет действия" in message
    assert calls == [("companion.activity.list", {"webspace_id": webspace_id, "limit": 30})]


def test_capture_feedback_stores_trial_observation() -> None:
    skill = _load_module()
    webspace_id = f"test-feedback-{uuid.uuid4().hex}"
    skill.reset_session(webspace_id=webspace_id)

    result = skill.capture_feedback(
        rating=4,
        expectation="хотелось быстро понять, кто говорит",
        observation="старт понятный",
        webspace_id=webspace_id,
    )

    assert result["ok"] is True
    assert result["feedback_count"] == 1


def test_diagnostics_cache_is_bounded_and_disposable(monkeypatch) -> None:
    skill = _load_module()
    monkeypatch.setattr(skill, "_build_diagnostics", lambda webspace_id: {"ok": True, "webspace_id": webspace_id})

    for index in range(skill._DIAGNOSTICS_CACHE_MAX_ITEMS + 5):
        skill._diagnostics_snapshot(f"ws-{index}")

    assert len(skill._DIAGNOSTICS_CACHE) == skill._DIAGNOSTICS_CACHE_MAX_ITEMS
    assert "ws-0" not in skill._DIAGNOSTICS_CACHE
    result = skill.dispose()
    assert result["ok"] is True
    assert result["cleared"] == skill._DIAGNOSTICS_CACHE_MAX_ITEMS
    assert not skill._DIAGNOSTICS_CACHE
    assert not skill._DIAGNOSTICS_STREAM_FINGERPRINTS
