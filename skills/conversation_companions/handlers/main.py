from __future__ import annotations

import copy
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool


SKILL_ID = "conversation_companions"
DEFAULT_ACTIVE_CHARACTER = "arseni"
SESSION_KEY = "conversation_companions.session"
PROFILES_KEY = "conversation_companions.profiles"
FEEDBACK_KEY = "conversation_companions.feedback"
MAX_HISTORY = 12
PANEL_CHARACTERS = ("arseni", "nika", "mira")
DIAGNOSTICS_SCHEMA = "conversation_companions.diagnostics.v1"
DIAGNOSTICS_RECEIVER = "conversation_companions.diagnostics"
DIALOG_CHANNEL_ID = "conversational"
CONVERSATION_ID = "conv.skill.conversation_companions.default"
VOICE_PROFILES = {
    "arseni": {"gender": "male", "voice": "ru-male", "lang": "ru-RU", "browser_voice_hint": "male"},
    "nika": {"gender": "female", "voice": "ru-female", "lang": "ru-RU", "browser_voice_hint": "female"},
    "mira": {"gender": "female", "voice": "ru-female", "lang": "ru-RU", "browser_voice_hint": "female"},
}

_FALLBACK_MEMORY: dict[str, Any] = {}
_LOG = logging.getLogger("adaos.skill.conversation_companions")


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


def _agent_projection(active_character: str | None, profiles: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    char_id = str(active_character or DEFAULT_ACTIVE_CHARACTER).strip() or DEFAULT_ACTIVE_CHARACTER
    profile = (profiles or DEFAULT_PROFILES).get(char_id, {})
    label = str(profile.get("name") or char_id).strip() or char_id
    voice_profile = dict(VOICE_PROFILES.get(char_id) or VOICE_PROFILES[DEFAULT_ACTIVE_CHARACTER])
    return {
        "id": f"agent:{SKILL_ID}:{char_id}",
        "label": label,
        "owner": f"skill:{SKILL_ID}",
        "kind": "skill_agent",
        "skill_id": SKILL_ID,
        "character_id": char_id,
        "memory_scope": "agent_user",
        "gender": voice_profile.get("gender"),
        "voice": voice_profile.get("voice"),
        "voice_profile": voice_profile,
    }


def _dialog_state(webspace_id: str, active_character: str | None = None, *, state: str = "active") -> dict[str, Any]:
    profiles = _profiles(webspace_id or "default")
    agent = _agent_projection(active_character, profiles)
    return {
        "state": state,
        "dialog_channel_id": DIALOG_CHANNEL_ID,
        "conversation_id": f"{CONVERSATION_ID}.{webspace_id or 'default'}",
        "owner": f"skill:{SKILL_ID}",
        "surface": f"skill:{SKILL_ID}",
        "default_tool": f"{SKILL_ID}.talk",
        "active_agent_id": agent["id"],
        "active_agent_label": agent["label"],
        "active_agent": agent,
        "memory": {
            "status": "skill_memory_compat",
            "scopes": ["skill_user", "agent_user", "conversation"],
            "owner": f"skill:{SKILL_ID}",
            "active_agent_id": agent["id"],
        },
    }


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


def _agent_chat_meta(
    _meta: Mapping[str, Any] | None,
    *,
    webspace_id: str,
    character_id: str | None,
    profiles: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    agent = _agent_projection(character_id, profiles)
    meta = dict(_meta or {})
    meta.setdefault("webspace_id", webspace_id)
    meta.setdefault("active_agent_id", agent["id"])
    meta.setdefault("active_agent_label", agent["label"])
    if agent.get("gender"):
        meta.setdefault("active_agent_gender", agent["gender"])
        meta.setdefault("voice_gender", agent["gender"])
    if agent.get("voice"):
        meta.setdefault("active_agent_voice", agent["voice"])
        meta.setdefault("voice", agent["voice"])
    if isinstance(agent.get("voice_profile"), Mapping):
        meta.setdefault("voice_profile", dict(agent["voice_profile"]))
    return meta


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
        "Если пользователь спрашивает о тебе, имени, роли или стиле, отвечай от имени персонажа: назови имя, роль и манеру общения, без фразы 'я не имею мнения о себе'.",
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
    except Exception as exc:
        details = {
            key: getattr(exc, key, None)
            for key in ("status_code", "error_code", "payload")
            if getattr(exc, key, None) not in (None, "")
        }
        _LOG.warning("conversation_companions LLM reply failed: %s details=%s", exc, details, exc_info=True)
        return None
    return None


def _has_any(text: str, *tokens: str) -> bool:
    return any(token in text for token in tokens)


def _looks_like_question(text: str) -> bool:
    lowered = text.lower()
    return "?" in lowered or _has_any(
        lowered,
        "что ",
        "что такое",
        "кто ",
        "где ",
        "куда ",
        "когда ",
        "почему ",
        "зачем ",
        "как ",
        "какая ",
        "какой ",
        "какие ",
        "сколько ",
    )


def _topic_reply(profile: Mapping[str, Any], user_text: str) -> str | None:
    name = str(profile.get("name") or "Собеседник")
    lowered = user_text.lower()
    if "бейрут" in lowered or "beirut" in lowered:
        return (
            f"{name}: у города нет собственной столицы. Бейрут сам является столицей Ливана. "
            "Если ты имел в виду страну или регион, лучше формулировать так: столица Ливана - Бейрут."
        )
    if "шум" in lowered:
        return (
            f"{name}: шум - это помеха, которая мешает выделить полезный сигнал. "
            "В разговоре это лишние детали, в данных - случайные или нерелевантные отклонения, "
            "в звуке - нежелательные колебания. Смысл один: шум снижает ясность."
        )
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
    topic_reply = _topic_reply(profile, user_text)
    if topic_reply:
        return topic_reply
    if _looks_like_question(user_text):
        name = str(profile.get("name") or "Собеседник")
        return (
            f"{name}: отвечу прямо: мне не хватает подключённого знания или LLM-ответа, "
            "поэтому я не буду делать вид, что знаю точный факт. Могу разобрать вопрос, "
            "уточнить формулировку или помочь проверить его через профильный источник."
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


def _safe_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _history_summary(session: Mapping[str, Any]) -> dict[str, Any]:
    history = _safe_list(session.get("history"))
    by_role: dict[str, int] = {}
    by_character: dict[str, int] = {}
    recent: list[dict[str, Any]] = []
    for item in history:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "unknown").strip() or "unknown"
        character_id = str(item.get("character_id") or "").strip()
        by_role[role] = by_role.get(role, 0) + 1
        if character_id:
            by_character[character_id] = by_character.get(character_id, 0) + 1
        recent.append(
            {
                "role": role,
                "character_id": character_id or None,
                "ts": item.get("ts"),
                "text_chars": len(str(item.get("text") or "")),
            }
        )
    return {
        "count": len(history),
        "max_history": MAX_HISTORY,
        "by_role": by_role,
        "by_character": by_character,
        "recent": recent[-5:],
        "redaction": "message text is omitted from diagnostics",
    }


def _feedback_summary(webspace_id: str) -> dict[str, Any]:
    items = _safe_list(_mem_get(_scoped_key(FEEDBACK_KEY, webspace_id), []))
    ratings = [int(item.get("rating")) for item in items if isinstance(item, Mapping) and isinstance(item.get("rating"), int)]
    latest = None
    for item in reversed(items):
        if isinstance(item, Mapping):
            latest = {
                "rating": item.get("rating"),
                "active_character": item.get("active_character"),
                "has_expectation": bool(str(item.get("expectation") or "").strip()),
                "has_observation": bool(str(item.get("observation") or "").strip()),
                "ts": item.get("ts"),
            }
            break
    average = round(sum(ratings) / len(ratings), 2) if ratings else None
    return {
        "count": len(items),
        "ratings_count": len(ratings),
        "rating_average": average,
        "latest": latest,
        "redaction": "free-form feedback text is omitted from diagnostics",
    }


def _profile_override_summary(profiles: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for char_id, profile in profiles.items():
        default = DEFAULT_PROFILES.get(char_id, {})
        changed_fields = []
        for key in ("name", "archetype", "purpose", "tone", "verbosity", "style_rules", "boundaries", "opening", "first_impression"):
            if profile.get(key) != default.get(key):
                changed_fields.append(key)
        rows.append(
            {
                "id": char_id,
                "name": profile.get("name"),
                "archetype": profile.get("archetype"),
                "changed": bool(changed_fields),
                "changed_fields": changed_fields,
                "style_rules_count": len(_safe_list(profile.get("style_rules"))),
                "boundaries_count": len(_safe_list(profile.get("boundaries"))),
            }
        )
    return rows


def _diagnostic_row(row_id: str, title: str, status: str, subtitle: str, details: Mapping[str, Any]) -> dict[str, Any]:
    icon_by_status = {
        "ready": "checkmark-circle-outline",
        "warning": "warning-outline",
        "degraded": "alert-circle-outline",
        "unknown": "information-circle-outline",
    }
    return {
        "id": row_id,
        "title": title,
        "status": status,
        "icon": icon_by_status.get(status, "information-circle-outline"),
        "subtitle": subtitle,
        "details": dict(details),
    }


def _build_diagnostics(webspace_id: str) -> dict[str, Any]:
    session = _session(webspace_id)
    profiles = _profiles(webspace_id)
    active_id = str(session.get("active_character") or DEFAULT_ACTIVE_CHARACTER)
    if active_id not in profiles:
        active_id = DEFAULT_ACTIVE_CHARACTER
    history = _history_summary(session)
    feedback = _feedback_summary(webspace_id)
    profile_rows = _profile_override_summary(profiles)
    changed_profiles = [row for row in profile_rows if row["changed"]]
    active_profile = profiles.get(active_id, profiles[DEFAULT_ACTIVE_CHARACTER])
    active_agent = _agent_projection(active_id, profiles)
    rows = [
        _diagnostic_row(
            "conversation.session",
            "Session state",
            "ready",
            f"active={active_id} history={history['count']}/{MAX_HISTORY}",
            {
                "webspace_id": webspace_id,
                "active_character": active_id,
                "active_agent": active_agent,
                "created_at": session.get("created_at"),
                "updated_at": session.get("updated_at"),
                "history": history,
            },
        ),
        _diagnostic_row(
            "conversation.active_profile",
            "Active character profile",
            "ready",
            f"{active_profile.get('name')} / {active_profile.get('archetype')}",
            {
                "id": active_id,
                "name": active_profile.get("name"),
                "archetype": active_profile.get("archetype"),
                "tone": active_profile.get("tone"),
                "verbosity": active_profile.get("verbosity"),
                "style_rules_count": len(_safe_list(active_profile.get("style_rules"))),
                "boundaries_count": len(_safe_list(active_profile.get("boundaries"))),
            },
        ),
        _diagnostic_row(
            "conversation.profile_overrides",
            "Profile overrides",
            "warning" if changed_profiles else "ready",
            f"{len(changed_profiles)} changed profiles",
            {"profiles": profile_rows},
        ),
        _diagnostic_row(
            "conversation.feedback",
            "Control-group feedback",
            "ready" if feedback["count"] else "unknown",
            f"{feedback['count']} entries; avg={feedback['rating_average'] if feedback['rating_average'] is not None else '-'}",
            feedback,
        ),
        _diagnostic_row(
            "conversation.safety_contract",
            "Safety and scope",
            "ready",
            "local conversation state only; no device-control tools",
            {
                "side_effects": ["skill_memory.session", "skill_memory.profiles", "skill_memory.feedback", "io.out.chat.append"],
                "no_device_control": True,
                "default_tool": "talk",
                "panel_characters": list(PANEL_CHARACTERS),
            },
        ),
    ]
    return {
        "ok": True,
        "schema": DIAGNOSTICS_SCHEMA,
        "webspace_id": webspace_id,
        "summary": {
            "title": "Conversation Companions",
            "value": len(profiles),
            "subtitle": f"active={active_id}; feedback={feedback['count']}; history={history['count']}",
            "details": "operator diagnostics; conversation text redacted",
            "status": "ready",
        },
        "items": rows,
        "session": {
            "active_character": active_id,
            "active_agent": active_agent,
            "history": history,
        },
        "profiles": {
            "count": len(profiles),
            "changed_count": len(changed_profiles),
            "items": profile_rows,
        },
        "feedback": feedback,
        "privacy": {
            "conversation_text_redacted": True,
            "feedback_text_redacted": True,
        },
    }


def _publish_diagnostics_snapshot(webspace_id: str, _meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = _build_diagnostics(webspace_id)
    try:
        from adaos.sdk.io import stream_publish

        meta = dict(_meta or {})
        meta.setdefault("webspace_id", webspace_id)
        stream_publish(DIAGNOSTICS_RECEIVER, payload, _meta=meta)
    except Exception:
        return payload
    return payload


def _event_payload(evt: Any) -> Mapping[str, Any]:
    payload = getattr(evt, "payload", evt)
    if isinstance(payload, Mapping):
        nested = payload.get("payload") if "payload" in payload and "type" in payload else None
        return nested if isinstance(nested, Mapping) else payload
    return {}


def _matches_diagnostics_receiver(payload: Mapping[str, Any]) -> bool:
    receiver = str(payload.get("receiver") or "").strip()
    return receiver in {DIAGNOSTICS_RECEIVER, "conversation_companions.*"}


def _webspace_from_event_payload(payload: Mapping[str, Any]) -> str:
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else None
    return _webspace_id(str(payload.get("webspace_id") or payload.get("workspace_id") or "").strip() or None, meta)


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
    _safe_emit_chat(message, webspace_id=ws, _meta=_agent_chat_meta(_meta, webspace_id=ws, character_id=active, profiles=profiles))
    return {
        "ok": True,
        "webspace_id": ws,
        "active_character": active,
        "dialog": _dialog_state(ws, active),
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
        "dialog": _dialog_state(ws, active),
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
    _safe_emit_chat(message, webspace_id=ws, _meta=_agent_chat_meta(_meta, webspace_id=ws, character_id=resolved, profiles=profiles))
    return {
        "ok": True,
        "webspace_id": ws,
        "active_character": session.get("active_character", DEFAULT_ACTIVE_CHARACTER),
        "selected_character": resolved,
        "dialog": _dialog_state(ws, resolved if not temporary else session.get("active_character", DEFAULT_ACTIVE_CHARACTER)),
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
    _safe_emit_chat(reply, webspace_id=ws, _meta=_agent_chat_meta(_meta, webspace_id=ws, character_id=selected, profiles=profiles))
    if mode != "temporary" and not panel and detected:
        session = _session(ws)
        session["active_character"] = selected
        _save_session(ws, session)
    return {
        "ok": True,
        "webspace_id": ws,
        "active_character": _session(ws).get("active_character", DEFAULT_ACTIVE_CHARACTER),
        "selected_character": selected,
        "dialog": _dialog_state(ws, selected),
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
    _safe_emit_chat(message, webspace_id=ws, _meta=_agent_chat_meta(_meta, webspace_id=ws, character_id=selected, profiles=profiles))
    return {
        "ok": True,
        "webspace_id": ws,
        "character_id": selected,
        "dialog": _dialog_state(ws, selected),
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
    active = _session(ws).get("active_character", DEFAULT_ACTIVE_CHARACTER)
    _safe_emit_chat(message, webspace_id=ws, _meta=_agent_chat_meta(_meta, webspace_id=ws, character_id=active, profiles=_profiles(ws)))
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
    _safe_emit_chat(
        message,
        webspace_id=ws,
        _meta=_agent_chat_meta(_meta, webspace_id=ws, character_id=DEFAULT_ACTIVE_CHARACTER, profiles=_profiles(ws)),
    )
    return {
        "ok": True,
        "webspace_id": ws,
        "message": message,
        "active_character": DEFAULT_ACTIVE_CHARACTER,
        "dialog": _dialog_state(ws, DEFAULT_ACTIVE_CHARACTER),
    }


@tool(summary="Return compact diagnostics for character trial state.", side_effects="none")
def get_diagnostics(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return _build_diagnostics(_webspace_id(webspace_id, _meta))


@tool(summary="Publish compact diagnostics for the WebUI stream.", side_effects="external_io")
def publish_diagnostics(
    webspace_id: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return _publish_diagnostics_snapshot(_webspace_id(webspace_id, _meta), _meta)


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = _event_payload(evt)
    if not _matches_diagnostics_receiver(payload):
        return
    _publish_diagnostics_snapshot(_webspace_from_event_payload(payload), payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else None)


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = _event_payload(evt)
    if _matches_diagnostics_receiver(payload):
        on_webio_stream_snapshot_requested(evt)


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
    if topic.endswith("publish_diagnostics"):
        return publish_diagnostics(**data)
    if topic.endswith("diagnostics"):
        return get_diagnostics(**data)
    return talk(**data)
