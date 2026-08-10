from __future__ import annotations

import importlib.util
import sys
import uuid
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
    spec = importlib.util.spec_from_file_location("conversation_companions_under_test", SKILL_ROOT / "handlers" / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_declares_tools_and_nlu_actions() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))

    tools = {item["name"] for item in manifest["tools"]}
    assert {"start", "talk", "switch_character", "update_profile", "capture_feedback"}.issubset(tools)
    assert manifest["default_tool"] == "talk"
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
