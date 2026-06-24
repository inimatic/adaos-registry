from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import tool


SKILL_ID = "conversation_companions"
DEFAULT_ACTIVE_CHARACTER = "arseni"
SESSION_KEY = "conversation_companions.session"
PROFILES_KEY = "conversation_companions.profiles"
FEEDBACK_KEY = "conversation_companions.feedback"
MAX_HISTORY = 12
PANEL_CHARACTERS = ("arseni", "nika", "mira")

_FALLBACK_MEMORY: dict[str, Any] = {}


def _load_default_profiles() -> dict[str, dict[str, Any]]:
    path = Path(__file__).resolve().parents[1] / "profiles" / "default_characters.json"
    return json.loads(path.read_text(encoding="utf-8"))


DEFAULT_PROFILES = _load_default_profiles()


def _now() -> float:
    return time.time()


def _webspace_id(value: str | None = None, _meta: Mapping[str, Any] | None = None) -> str:
    token = str(value or "").strip()
    if token:
        return token
    if isinstance(_meta, Mapping):
        for key in ("webspace_id", "workspace_id"):
            raw = _meta.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return "default"


def _scoped_key(base: str, webspace_id: str) -> str:
    return f"{base}.{webspace_id or 'default'}"


def _mem_get(key: str, default: Any = None) -> Any:
    try:
        from adaos.sdk.data import skill_memory

        return skill_memory.get(key, default)
    except Exception:
        return copy.deepcopy(_FALLBACK_MEMORY.get(key, default))


def _mem_set(key: str, value: Any) -> None:
    try:
        from adaos.sdk.data import skill_memory

        skill_memory.set(key, value)
    except Exception:
        _FALLBACK_MEMORY[key] = copy.deepcopy(value)


def _profiles(webspace_id: str) -> dict[str, dict[str, Any]]:
    raw = _mem_get(_scoped_key(PROFILES_KEY, webspace_id))
    if isinstance(raw, dict) and raw:
        merged = copy.deepcopy(DEFAULT_PROFILES)
        for char_id, profile in raw.items():
            if isinstance(profile, dict):
                merged[str(char_id)] = profile
        return merged
    return copy.deepcopy(DEFAULT_PROFILES)


def _save_profiles(webspace_id: str, profiles: Mapping[str, Mapping[str, Any]]) -> None:
    _mem_set(_scoped_key(PROFILES_KEY, webspace_id), copy.deepcopy(dict(profiles)))


def _session(webspace_id: str) -> dict[str, Any]:
    raw = _mem_get(_scoped_key(SESSION_KEY, webspace_id))
    if isinstance(raw, dict):
        session = copy.deepcopy(raw)
    else:
        session = {}
    session.setdefault("active_character", DEFAULT_ACTIVE_CHARACTER)
    session.setdefault("history", [])
    session.setdefault("created_at", _now())
    session["updated_at"] = _now()
    return session


def _save_session(webspace_id: str, session: Mapping[str, Any]) -> None:
    _mem_set(_scoped_key(SESSION_KEY, webspace_id), dict(session))


def _normalize_character_id(value: Any, profiles: Mapping[str, Mapping[str, Any]]) -> str | None:
    token = str(value or "").strip().lower()
    if not token:
        return None
    aliases = {
        "арсений": "arseni",
        "советник": "arseni",
        "консультант": "arseni",
        "наставник": "arseni",
        "ника": "nika",
        "скептик": "nika",
        "спорщик": "nika",
        "адвокат": "nika",
        "мира": "mira",
        "рассказчик": "mira",
        "собеседник": "mira",
        "компаньон": "mira",
    }
    if token in profiles:
        return token
    if token in aliases and aliases[token] in profiles:
        return aliases[token]
    for char_id, profile in profiles.items():
        name = str(profile.get("name") or "").strip().lower()
        archetype = str(profile.get("archetype") or "").strip().lower()
        if token in {name, archetype}:
            return char_id
    return None


def _detect_character_from_text(text: str, profiles: Mapping[str, Mapping[str, Any]]) -> str | None:
    lowered = text.lower()
    for token in ("арсений", "советник", "консультант", "ника", "скептик", "мира", "рассказчик", "собеседник"):
        if token in lowered:
            resolved = _normalize_character_id(token, profiles)
            if resolved:
                return resolved
    return None


def _short_character_card(profile: Mapping[str, Any], *, active: bool = False) -> dict[str, Any]:
    return {
        "id": profile.get("id"),
        "name": profile.get("name"),
        "archetype": profile.get("archetype"),
        "purpose": profile.get("purpose"),
        "tone": profile.get("tone"),
        "active": active,
        "first_impression": profile.get("first_impression"),
    }


def _first_run_message(profiles: Mapping[str, Mapping[str, Any]]) -> str:
    names = ", ".join(f"{p['name']} ({p['first_impression']})" for p in profiles.values())
    return (
        "Можно начать с готового персонажа и поправить его по ходу. "
        f"Доступны: {names}. "
        "Для первого теста лучше выбрать одного и сразу задать живой вопрос."
    )


def _append_history(webspace_id: str, *, role: str, text: str, character_id: str | None = None) -> None:
    session = _session(webspace_id)
    history = session.setdefault("history", [])
    history.append(
        {
            "role": role,
            "text": str(text or "").strip()[:1200],
            "character_id": character_id,
            "ts": _now(),
        }
    )
    session["history"] = history[-MAX_HISTORY:]
    _save_session(webspace_id, session)


def _safe_emit_chat(text: str, *, webspace_id: str, _meta: Mapping[str, Any] | None = None) -> None:
    try:
        from adaos.sdk.io.out import chat_append

        meta = dict(_meta or {})
        meta.setdefault("webspace_id", webspace_id)
        chat_append(text, from_="hub", _meta=meta)
    except Exception:
        return


def _build_system_prompt(
    profile: Mapping[str, Any],
    *,
    panel: bool = False,
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    core = [
        "Ты работаешь внутри AdaOS как разговорный персонаж.",
        "Персонаж - это стиль общения, а не утверждение о реальной личности.",
        "Не утверждай, что у тебя есть физическое тело, реальные чувства или доступ к устройствам.",
        "Не выполняй команды управления устройствами и не делай вид, что выполнил действие.",
        "Если вопрос требует профессиональной экспертизы, отвечай как общий помощник и обозначай пределы уверенности.",
        "Отвечай по-русски, без markdown-заголовков и без искусственной торжественности.",
    ]
    if panel and profiles:
        panel_lines = []
        for char_id in PANEL_CHARACTERS:
            item = profiles.get(char_id)
            if not item:
                continue
            panel_lines.append(
                f"{item['name']}: роль={item['purpose']}; тон={item['tone']}; краткость={item['verbosity']}"
            )
        return "\n".join(
            [
                *core,
                "Режим панели: дай по одной короткой реплике от каждого персонажа и в конце общий вывод в одну строку.",
                "Персонажи панели:",
                *panel_lines,
            ]
        )
    style_rules = "\n".join(f"- {rule}" for rule in profile.get("style_rules", []) if isinstance(rule, str))
    boundaries = "\n".join(f"- {rule}" for rule in profile.get("boundaries", []) if isinstance(rule, str))
    return "\n".join(
        [
            *core,
            f"Имя персонажа: {profile.get('name')}",
            f"Назначение: {profile.get('purpose')}",
            f"Тон: {profile.get('tone')}",
            f"Длина ответа: {profile.get('verbosity')}",
            "Правила стиля:",
            style_rules or "- отвечай естественно и по делу",
            "Границы:",
            boundaries or "- не выполняй действия вне разговора",
        ]
    )


def _messages_for_llm(
    *,
    user_text: str,
    system_prompt: str,
    history: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": system_prompt}]
    for item in history[-8:]:
        role = "assistant" if item.get("role") == "assistant" else "user"
        text = str(item.get("text") or "").strip()
        if text:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": user_text})
    return messages


def _llm_reply(messages: list[Mapping[str, str]]) -> str | None:
    try:
        from adaos.sdk.llm.llm_client import send_response

        result = send_response(messages, temperature=0.7, max_tokens=700, timeout=35)
        text = result.get("output_text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    except Exception:
        return None
    return None


def _draft_reply(profile: Mapping[str, Any], text: str) -> str:
    user_text = text.strip()
    if not user_text:
        return str(profile.get("opening") or "Я готов к разговору.")
    if any(word in user_text.lower() for word in ("управ", "включи", "выключи", "открой", "закрой")):
        return (
            f"{profile.get('name')}: я пока работаю только как собеседник и не управляю устройствами. "
            "Могу помочь сформулировать запрос или разобраться в задаче."
        )
    if profile.get("id") == "nika":
        return f"Ника: главный вопрос - где эта идея может сломаться. Я бы сначала проверила допущения, сроки и критерий успеха."
    if profile.get("id") == "mira":
        return f"Мира: можно сделать это легче. Сначала поймать настроение разговора, потом уже усложнять форму."
    return f"Арсений: начни с цели и одного ближайшего шага. Если цель не ясна, любое решение будет выглядеть шумом."


def _profile_patch_from_instruction(instruction: str) -> dict[str, Any]:
    text = instruction.lower()
    patch: dict[str, Any] = {"style_rules_add": []}
    if any(word in text for word in ("короче", "кратко", "лаконич")):
        patch["verbosity"] = "коротко, одна-две главные мысли"
    if any(word in text for word in ("теплее", "мягче", "дружелюб")):
        patch["tone_append"] = "теплее"
    if any(word in text for word in ("строже", "жёстче", "жестче", "прямее")):
        patch["tone_append"] = "прямее"
    if any(word in text for word in ("не задавай", "без вопросов", "не спрашивай")):
        patch["style_rules_add"].append("Не заканчивает ответ вопросом, если вопрос не нужен по смыслу.")
    if any(word in text for word in ("не спорь", "меньше спор", "без спора")):
        patch["style_rules_add"].append("Не спорит без запроса на критический разбор.")
    if any(word in text for word in ("больше спор", "скептич", "крити")):
        patch["style_rules_add"].append("Аккуратно показывает слабые места идеи.")
    if any(word in text for word in ("без сюсю", "не сюсю")):
        patch["style_rules_add"].append("Не использует приторную поддержку и уменьшительно-ласкательный тон.")
    patch["style_rules_add"] = list(dict.fromkeys(patch["style_rules_add"]))
    return patch


def _apply_patch(profile: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(dict(profile))
    if patch.get("verbosity"):
        updated["verbosity"] = patch["verbosity"]
    tone_append = str(patch.get("tone_append") or "").strip()
    if tone_append and tone_append.lower() not in str(updated.get("tone") or "").lower():
        updated["tone"] = f"{updated.get('tone')}, {tone_append}"
    additions = [item for item in patch.get("style_rules_add", []) if isinstance(item, str) and item.strip()]
    if additions:
        current = [str(item) for item in updated.get("style_rules", []) if isinstance(item, str)]
        updated["style_rules"] = list(dict.fromkeys([*current, *additions]))
    updated["updated_at"] = _now()
    return updated


@tool(summary="Start character onboarding.", side_effects="local_write")
def start(
    profile_hint: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _webspace_id(webspace_id, _meta)
    profiles = _profiles(ws)
    session = _session(ws)
    hint = str(profile_hint or "").strip()
    detected = _detect_character_from_text(hint, profiles) if hint else None
    if detected:
        session["active_character"] = detected
    _save_session(ws, session)
    active = session["active_character"]
    profile = profiles.get(active, profiles[DEFAULT_ACTIVE_CHARACTER])
    message = (
        f"{profile.get('opening')}\n\n"
        f"{_first_run_message(profiles)}"
    )
    _safe_emit_chat(message, webspace_id=ws, _meta=_meta)
    return {
        "ok": True,
        "webspace_id": ws,
        "active_character": active,
        "message": message,
        "characters": [_short_character_card(p, active=(cid == active)) for cid, p in profiles.items()],
        "next_actions": [
            "Скажи: позови Нику / позови Миру / оставь Арсения.",
            "Задай реальный вопрос персонажу.",
            "Поправь стиль: говори короче, теплее, скептичнее.",
        ],
        "research_probe": "После 2-3 реплик спроси пользователя: что он ожидал от такого персонажа и что показалось лишним.",
    }


@tool(summary="List character profiles.", side_effects="none")
def list_characters(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _webspace_id(webspace_id, _meta)
    profiles = _profiles(ws)
    active = _session(ws).get("active_character", DEFAULT_ACTIVE_CHARACTER)
    return {
        "ok": True,
        "webspace_id": ws,
        "active_character": active,
        "characters": [_short_character_card(p, active=(cid == active)) for cid, p in profiles.items()],
    }


@tool(summary="Switch active character.", side_effects="local_write")
def switch_character(
    character_id: str,
    temporary: bool = False,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _webspace_id(webspace_id, _meta)
    profiles = _profiles(ws)
    resolved = _normalize_character_id(character_id, profiles)
    if not resolved:
        return {
            "ok": False,
            "error": "unknown_character",
            "message": "Не нашёл такого персонажа. Доступны: Арсений, Ника, Мира.",
        }
    profile = profiles[resolved]
    session = _session(ws)
    if not temporary:
        session["active_character"] = resolved
        _save_session(ws, session)
    message = (
        f"Временно отвечает {profile['name']}: {profile['opening']}"
        if temporary
        else f"Активный персонаж: {profile['name']}. {profile['opening']}"
    )
    _safe_emit_chat(message, webspace_id=ws, _meta=_meta)
    return {
        "ok": True,
        "webspace_id": ws,
        "active_character": session.get("active_character", DEFAULT_ACTIVE_CHARACTER),
        "selected_character": resolved,
        "temporary": bool(temporary),
        "message": message,
        "profile": _short_character_card(profile, active=not temporary),
    }


@tool(summary="Talk as a character or panel.", side_effects="local_write")
def talk(
    text: str | None = None,
    character_id: str | None = None,
    mode: str = "single",
    preview: bool = False,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _webspace_id(webspace_id, _meta)
    profiles = _profiles(ws)
    session = _session(ws)
    user_text = str(text or "").strip()
    mode = str(mode or "single").strip().lower()
    if not user_text:
        return start(webspace_id=ws, _meta=_meta)

    detected = _normalize_character_id(character_id, profiles) if character_id else _detect_character_from_text(user_text, profiles)
    selected = detected or str(session.get("active_character") or DEFAULT_ACTIVE_CHARACTER)
    if selected not in profiles:
        selected = DEFAULT_ACTIVE_CHARACTER

    panel = mode == "panel"
    if panel:
        system_prompt = _build_system_prompt(profiles[selected], panel=True, profiles=profiles)
    else:
        system_prompt = _build_system_prompt(profiles[selected])

    _append_history(ws, role="user", text=user_text, character_id=selected)
    history = _session(ws).get("history", [])
    reply = None if preview else _llm_reply(_messages_for_llm(user_text=user_text, system_prompt=system_prompt, history=history))
    used_llm = bool(reply)
    if not reply:
        if panel:
            reply = (
                "Арсений: сначала зафиксируй цель и критерий успеха.\n"
                "Ника: отдельно проверь, где решение может сломаться.\n"
                "Мира: оставь место для живого разговора, не превращай всё в анкету.\n"
                "Итог: тестируй на маленьком сценарии и собирай ожидания, а не только оценки."
            )
        else:
            reply = _draft_reply(profiles[selected], user_text)
    _append_history(ws, role="assistant", text=reply, character_id=selected)
    _safe_emit_chat(reply, webspace_id=ws, _meta=_meta)
    if mode != "temporary" and not panel and detected:
        session = _session(ws)
        session["active_character"] = selected
        _save_session(ws, session)
    return {
        "ok": True,
        "webspace_id": ws,
        "active_character": _session(ws).get("active_character", DEFAULT_ACTIVE_CHARACTER),
        "selected_character": selected,
        "mode": "panel" if panel else mode,
        "message": reply,
        "used_llm": used_llm,
        "preview": bool(preview),
    }


@tool(summary="Update a character profile.", side_effects="local_write")
def update_profile(
    instruction: str,
    character_id: str | None = None,
    persist: bool = True,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _webspace_id(webspace_id, _meta)
    profiles = _profiles(ws)
    session = _session(ws)
    selected = _normalize_character_id(character_id, profiles) if character_id else session.get("active_character")
    if selected not in profiles:
        selected = DEFAULT_ACTIVE_CHARACTER
    patch = _profile_patch_from_instruction(instruction)
    if not patch.get("verbosity") and not patch.get("tone_append") and not patch.get("style_rules_add"):
        patch["note"] = "Правка сохранена как свободная заметка к стилю."
        patch["style_rules_add"] = [str(instruction).strip()[:240]]
    updated = _apply_patch(profiles[selected], patch)
    if persist:
        profiles[selected] = updated
        _save_profiles(ws, profiles)
    message = (
        f"Обновил профиль {updated['name']}: "
        f"тон - {updated.get('tone')}; длина - {updated.get('verbosity')}."
        if persist
        else f"Примерил временную правку для {updated['name']} без сохранения."
    )
    _safe_emit_chat(message, webspace_id=ws, _meta=_meta)
    return {
        "ok": True,
        "webspace_id": ws,
        "character_id": selected,
        "persisted": bool(persist),
        "message": message,
        "patch": patch,
        "profile": updated,
    }


@tool(summary="Capture control group feedback.", side_effects="local_write")
def capture_feedback(
    rating: int | None = None,
    expectation: str | None = None,
    observation: str | None = None,
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _webspace_id(webspace_id, _meta)
    key = _scoped_key(FEEDBACK_KEY, ws)
    items = _mem_get(key, [])
    if not isinstance(items, list):
        items = []
    entry = {
        "rating": int(rating) if isinstance(rating, int) else None,
        "expectation": str(expectation or "").strip()[:1000],
        "observation": str(observation or "").strip()[:1000],
        "active_character": _session(ws).get("active_character", DEFAULT_ACTIVE_CHARACTER),
        "ts": _now(),
    }
    items.append(entry)
    _mem_set(key, items[-200:])
    message = "Записал обратную связь. Для контрольной группы важнее ожидания и отторжение, чем только оценка."
    _safe_emit_chat(message, webspace_id=ws, _meta=_meta)
    return {"ok": True, "webspace_id": ws, "message": message, "feedback_count": len(items)}


@tool(summary="Reset session state.", side_effects="local_write")
def reset_session(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ws = _webspace_id(webspace_id, _meta)
    _mem_set(
        _scoped_key(SESSION_KEY, ws),
        {
            "active_character": DEFAULT_ACTIVE_CHARACTER,
            "history": [],
            "created_at": _now(),
            "updated_at": _now(),
        },
    )
    message = "Сессия сброшена. Активный персонаж снова Арсений."
    _safe_emit_chat(message, webspace_id=ws, _meta=_meta)
    return {"ok": True, "webspace_id": ws, "message": message, "active_character": DEFAULT_ACTIVE_CHARACTER}


def handle(topic: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    data = dict(payload or {})
    if topic.endswith("start"):
        return start(**data)
    if topic.endswith("switch_character"):
        return switch_character(**data)
    if topic.endswith("update_profile"):
        return update_profile(**data)
    if topic.endswith("feedback"):
        return capture_feedback(**data)
    return talk(**data)
