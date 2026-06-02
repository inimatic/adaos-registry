from __future__ import annotations

import hashlib
from datetime import datetime
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping
import logging

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ProjectionContext, StreamReceiver, StreamRuntime, ctx_subnet
from adaos.sdk.data.skill_memory import get as memory_get, set as memory_set
from adaos.sdk.io import stream_publish
from adaos.sdk.io.out import chat_append, say
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.yjs.webspace import default_webspace_id
from adaos.skills.runtime_runner import execute_tool


_WEATHER_RE = re.compile(
    r"(?:какая\s+)?погода\w*\s+(?:в|во)\s+(.+)$",
    re.IGNORECASE | re.UNICODE,
)
_WEATHER_PREFIXES = (
    "какая погода в ",
    "какая погода во ",
    "погода в ",
    "погода во ",
    "weather in ",
)

_CITY_ALIASES: dict[str, tuple[str, str]] = {
    # ru (cases) -> (weather_skill city key, display)
    "москва": ("Moscow", "Москва"),
    "москве": ("Moscow", "Москва"),
    "москву": ("Moscow", "Москва"),
    "москвы": ("Moscow", "Москва"),
    "берлин": ("Berlin", "Berlin"),
    "берлине": ("Berlin", "Berlin"),
    "берлина": ("Berlin", "Berlin"),
    "париж": ("Paris", "Paris"),
    "париже": ("Paris", "Paris"),
    "парижа": ("Paris", "Paris"),
    "токио": ("Tokyo", "Tokyo"),
    "нью-йорк": ("New York", "New York"),
    "нью йорк": ("New York", "New York"),
    "нью-йорке": ("New York", "New York"),
    "нью йорке": ("New York", "New York"),
}

_log = logging.getLogger("adaos.voice_chat_skill")
REQUIRES_DATA_PROJECTIONS = ["voice_chat.state"]
_DATA_PROJECTION_ENTRIES = [
    {
        "scope": "subnet",
        "slot": "voice_chat.state",
        "targets": [
            {
                "backend": "yjs",
                "path": "data/voice_chat",
            },
        ],
    },
]
_MAX_MESSAGES = 80
_STREAM_TAIL_MAX_MESSAGES = 80
_MAX_STATE_KEYS = 32
_STATE_TTL_S = 3600.0
_VOICE_TAIL_RECEIVER = "voice_chat.messages"
_STATE_BY_KEY: dict[str, dict[str, Any]] = {}
_MAX_TIMER_SECONDS = 24 * 60 * 60
_TIME_RE = re.compile(
    r"\b(?:\u0441\u043a\u043e\u043b\u044c\u043a\u043e\s+\u0432\u0440\u0435\u043c\u0435\u043d\u0438|\u043a\u043e\u0442\u043e\u0440\u044b\u0439\s+\u0447\u0430\u0441|what\s+time\s+is\s+it)\b",
    re.IGNORECASE | re.UNICODE,
)
_MARKETPLACE_RE = re.compile(
    r"\b(?:\u043e\u0442\u043a\u0440\u043e\u0439|\u043f\u043e\u043a\u0430\u0436\u0438|\u0437\u0430\u043f\u0443\u0441\u0442\u0438|open|show)\s+(?:\u043c\u0430\u0440\u043a\u0435\u0442\u043f\u043b\u0435\u0439\u0441|marketplace)\b",
    re.IGNORECASE | re.UNICODE,
)
_TIMER_RE = re.compile(
    r"\b(?:\u043f\u043e\u0441\u0442\u0430\u0432\u044c|\u0443\u0441\u0442\u0430\u043d\u043e\u0432\u0438|\u0437\u0430\u043f\u0443\u0441\u0442\u0438|set|start)\s+(?:a\s+)?(?:\u0442\u0430\u0439\u043c\u0435\u0440|timer)(?:\s+(?:\u043d\u0430|for))?\s+(?P<duration>\d+\s*(?:\u0441\u0435\u043a\u0443\u043d\u0434(?:\u0443|\u044b)?|\u0441\u0435\u043a|\u043c\u0438\u043d\u0443\u0442(?:\u0443|\u044b)?|\u043c\u0438\u043d|\u0447\u0430\u0441(?:\u0430|\u043e\u0432)?|seconds?|secs?|minutes?|mins?|hours?))\b",
    re.IGNORECASE | re.UNICODE,
)


def _build_tail_stream_payload(context: ProjectionContext) -> dict[str, Any]:
    state = _state_for(context.webspace_id or default_webspace_id(), context.node_id)
    return _tail_stream_payload(state)


_STREAM_RUNTIME = StreamRuntime(
    "voice_chat_skill",
    receivers=[
        StreamReceiver(_VOICE_TAIL_RECEIVER, build=_build_tail_stream_payload),
    ],
    stream_publish=stream_publish,
)


def _webspace_id_from_meta(meta: Mapping[str, Any] | None) -> str:
    if isinstance(meta, Mapping):
        token = str(meta.get("webspace_id") or meta.get("workspace_id") or "").strip()
        if token:
            return token
    return default_webspace_id()


def _state_key(webspace_id: str, target_node_id: str | None = None) -> str:
    return f"{str(webspace_id or default_webspace_id()).strip() or default_webspace_id()}\0{str(target_node_id or '').strip()}"


def _state_memory_key(state_key: str) -> str:
    digest = hashlib.sha256(str(state_key or "").encode("utf-8")).hexdigest()[:24]
    return f"voice_chat.state.{digest}"


def _load_persisted_state(state_key: str) -> dict[str, Any] | None:
    try:
        raw = memory_get(_state_memory_key(state_key))
    except Exception:
        raw = None
    if not isinstance(raw, Mapping):
        return None
    messages = _bounded_messages(raw.get("messages"), limit=_MAX_MESSAGES)
    return {
        "messages": messages,
        "last_refresh_ts": raw.get("last_refresh_ts") or time.time(),
        "last_access_ts": time.time(),
    }


def _persist_state(state_key: str, state: Mapping[str, Any]) -> None:
    try:
        memory_set(
            _state_memory_key(state_key),
            {
                "messages": _bounded_messages(state.get("messages"), limit=_MAX_MESSAGES),
                "last_refresh_ts": state.get("last_refresh_ts") or time.time(),
            },
        )
    except Exception:
        _log.debug("voice_chat state persistence failed key=%s", state_key, exc_info=True)


def _state_for(webspace_id: str, target_node_id: str | None = None) -> dict[str, Any]:
    key = _state_key(webspace_id, target_node_id)
    state = _STATE_BY_KEY.get(key)
    if not isinstance(state, dict):
        state = _load_persisted_state(key) or {"messages": [], "last_refresh_ts": time.time()}
        _STATE_BY_KEY[key] = state
    state["last_access_ts"] = time.time()
    if not isinstance(state.get("messages"), list):
        state["messages"] = []
    _prune_state_cache()
    return state


def _prune_state_cache() -> None:
    if len(_STATE_BY_KEY) <= _MAX_STATE_KEYS:
        return
    now = time.time()
    stale_keys = [
        key
        for key, state in _STATE_BY_KEY.items()
        if now - float(state.get("last_access_ts") or state.get("last_refresh_ts") or 0.0) > _STATE_TTL_S
    ]
    for key in stale_keys:
        _STATE_BY_KEY.pop(key, None)
        if len(_STATE_BY_KEY) <= _MAX_STATE_KEYS:
            return
    overflow = len(_STATE_BY_KEY) - _MAX_STATE_KEYS
    if overflow <= 0:
        return
    oldest = sorted(
        _STATE_BY_KEY.items(),
        key=lambda item: float(item[1].get("last_access_ts") or item[1].get("last_refresh_ts") or 0.0),
    )
    for key, _state in oldest[:overflow]:
        _STATE_BY_KEY.pop(key, None)


def _bounded_messages(raw: Any, *, limit: int = _MAX_MESSAGES) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in raw[-limit:]:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text") or "")
        if len(text) > 2000:
            text = text[:2000] + "..."
        messages.append(
            {
                "id": str(item.get("id") or ""),
                "from": str(item.get("from") or "hub"),
                "text": text,
                "ts": item.get("ts"),
            }
        )
    return messages


def _compact_state_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    messages = _bounded_messages(state.get("messages"), limit=_MAX_MESSAGES)
    last = messages[-1] if messages else None
    return {
        "status": "ready" if messages else "empty",
        "message_count": len(messages),
        "last_message": last,
        "last_refresh_ts": state.get("last_refresh_ts") or time.time(),
        "stream_ref": _VOICE_TAIL_RECEIVER,
    }


def _tail_stream_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    messages = _bounded_messages(state.get("messages"), limit=_STREAM_TAIL_MAX_MESSAGES)
    return {
        "messages": messages,
        "last_refresh_ts": state.get("last_refresh_ts") or time.time(),
        "message_count": len(messages),
    }


def _message(from_: str, text: str) -> dict[str, Any]:
    ts = time.time()
    return {
        "id": f"m.{int(ts * 1000)}.{from_}",
        "from": str(from_ or "hub").strip() or "hub",
        "text": str(text or ""),
        "ts": ts,
    }


def _ensure_skill_data_projections() -> None:
    try:
        ctx = get_ctx()
        if ctx.projections.resolve("subnet", "voice_chat.state"):
            return
        ctx.projections.load_entries(_DATA_PROJECTION_ENTRIES)
    except Exception:
        pass


def _project_state(webspace_id: str, target_node_id: str | None = None) -> None:
    _ensure_skill_data_projections()
    state = _state_for(webspace_id, target_node_id)
    payload = _compact_state_payload(state)
    try:
        ctx_subnet.set("voice_chat.state", payload, webspace_id=webspace_id)
    except Exception as exc:
        _log.warning("voice_chat projection failed webspace=%s error=%s", webspace_id, exc)


def _publish_tail_stream(webspace_id: str, target_node_id: str | None = None, *, force: bool = False) -> None:
    state = _state_for(webspace_id, target_node_id)
    _STREAM_RUNTIME.publish_snapshot(
        _VOICE_TAIL_RECEIVER,
        _tail_stream_payload(state),
        webspace_id=webspace_id,
        force=force,
        meta={"target_node_id": target_node_id} if target_node_id else None,
    )


def _append_projected_message(
    webspace_id: str,
    target_node_id: str | None,
    *,
    from_: str,
    text: str,
) -> None:
    state = _state_for(webspace_id, target_node_id)
    messages = list(state.get("messages") or [])
    messages.append(_message(from_, text))
    state["messages"] = messages[-_MAX_MESSAGES:]
    state["last_refresh_ts"] = time.time()
    _persist_state(_state_key(webspace_id, target_node_id), state)
    _project_state(webspace_id, target_node_id)
    _publish_tail_stream(webspace_id, target_node_id)


def _append_reply(reply: str, *, webspace_id: str, target_node_id: str | None) -> None:
    chat_append(reply, from_="hub", _meta={"webspace_id": webspace_id, "target_node_id": target_node_id})
    _append_projected_message(webspace_id, target_node_id, from_="hub", text=reply)


def _payload(evt: Any) -> dict[str, Any]:
    if isinstance(evt, dict):
        return evt
    data = getattr(evt, "payload", None)
    return data if isinstance(data, dict) else {}


def _meta_from_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if isinstance(payload, Mapping) and isinstance(payload.get("_meta"), Mapping):
        return dict(payload.get("_meta") or {})
    return {}


def _target_node_id_from_meta(meta: Mapping[str, Any] | None) -> str | None:
    if not isinstance(meta, Mapping):
        return None
    return str(meta.get("target_node_id") or meta.get("node_id") or "").strip() or None


def _route_id(meta: Mapping[str, Any] | None) -> str:
    if not isinstance(meta, Mapping):
        return ""
    return str(meta.get("route_id") or meta.get("route") or "").strip()


def _format_now_reply() -> str:
    return "\u0421\u0435\u0439\u0447\u0430\u0441 " + datetime.now().astimezone().strftime("%H:%M") + "."


def _normalize_duration_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _parse_duration_seconds(value: Any) -> tuple[int, str] | None:
    text = _normalize_duration_text(value).lower()
    match = re.search(r"(?P<num>\d+)\s*(?P<unit>[^\d\s]+)", text, flags=re.IGNORECASE | re.UNICODE)
    if not match:
        return None
    amount = int(match.group("num"))
    unit = match.group("unit").strip().lower()
    if amount <= 0:
        return None
    if unit.startswith(("sec", "сек")):
        seconds = amount
        label = f"{amount} " + ("\u0441\u0435\u043a\u0443\u043d\u0434" if amount != 1 else "\u0441\u0435\u043a\u0443\u043d\u0434\u0443")
    elif unit.startswith(("min", "мин")):
        seconds = amount * 60
        label = f"{amount} " + ("\u043c\u0438\u043d\u0443\u0442" if amount != 1 else "\u043c\u0438\u043d\u0443\u0442\u0443")
    elif unit.startswith(("hour", "час")):
        seconds = amount * 60 * 60
        label = f"{amount} " + ("\u0447\u0430\u0441\u043e\u0432" if amount != 1 else "\u0447\u0430\u0441")
    else:
        return None
    if seconds > _MAX_TIMER_SECONDS:
        return None
    return seconds, label


def _speak_reply(reply: str, meta: Mapping[str, Any] | None) -> None:
    try:
        say(reply, lang=(meta or {}).get("lang") or "ru-RU")
    except Exception:
        _log.debug("voice_chat say failed", exc_info=True)


def _publish_modal_open(
    *,
    modal_id: str,
    webspace_id: str,
    target_node_id: str | None,
    meta: Mapping[str, Any] | None,
    suppress_voice_ack: bool = False,
) -> None:
    event_meta = dict(meta or {})
    event_meta.setdefault("webspace_id", webspace_id)
    event_meta.setdefault("route_id", "voice_chat")
    if target_node_id:
        event_meta.setdefault("target_node_id", target_node_id)
    if suppress_voice_ack:
        event_meta["_voice_chat_ack_suppressed"] = True
    bus_emit(
        get_ctx().bus,
        "desktop.modal.open",
        {
            "modal_id": modal_id,
            "webspace_id": webspace_id,
            "_meta": event_meta,
        },
        source="voice_chat_skill",
    )


def _start_timer(duration_text: Any, *, webspace_id: str, target_node_id: str | None, meta: Mapping[str, Any] | None) -> Mapping[str, Any]:
    parsed = _parse_duration_seconds(duration_text)
    if not parsed:
        reply = "\u041d\u0435 \u043f\u043e\u043d\u044f\u043b \u0434\u043b\u0438\u0442\u0435\u043b\u044c\u043d\u043e\u0441\u0442\u044c \u0442\u0430\u0439\u043c\u0435\u0440\u0430. \u0421\u043a\u0430\u0436\u0438, \u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440: \u00ab\u041f\u043e\u0441\u0442\u0430\u0432\u044c \u0442\u0430\u0439\u043c\u0435\u0440 \u043d\u0430 10 \u043c\u0438\u043d\u0443\u0442\u00bb."
        _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
        _speak_reply(reply, meta)
        return {"ok": False, "error": "duration_required"}
    seconds, label = parsed
    reply = f"\u0422\u0430\u0439\u043c\u0435\u0440 \u043d\u0430 {label} \u0437\u0430\u043f\u0443\u0449\u0435\u043d."
    _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
    _speak_reply(reply, meta)

    def _done() -> None:
        done = f"\u0422\u0430\u0439\u043c\u0435\u0440 \u043d\u0430 {label} \u0438\u0441\u0442\u0435\u043a."
        _append_reply(done, webspace_id=webspace_id, target_node_id=target_node_id)
        _speak_reply(done, meta)

    timer = threading.Timer(seconds, _done)
    timer.daemon = True
    timer.start()
    return {"ok": True, "reply": reply, "duration_seconds": seconds}


def _try_handle_local_command(text: str, *, webspace_id: str, target_node_id: str | None, meta: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if _MARKETPLACE_RE.search(text):
        reply = "\u041e\u0442\u043a\u0440\u044b\u0432\u0430\u044e Marketplace."
        _publish_modal_open(
            modal_id="apps_catalog",
            webspace_id=webspace_id,
            target_node_id=target_node_id,
            meta=meta,
            suppress_voice_ack=True,
        )
        _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
        _speak_reply(reply, meta)
        return {"ok": True, "reply": reply, "intent": "desktop.open_marketplace"}
    if _TIME_RE.search(text):
        reply = _format_now_reply()
        _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
        _speak_reply(reply, meta)
        return {"ok": True, "reply": reply, "intent": "voice.time.now"}
    timer_match = _TIMER_RE.search(text)
    if timer_match:
        result = _start_timer(timer_match.group("duration"), webspace_id=webspace_id, target_node_id=target_node_id, meta=meta)
        return {**dict(result), "intent": "voice.timer.start"}
    return None


def _normalize_city_key(text: str) -> str:
    return (
        str(text or "")
        .strip()
        .lower()
        .replace("ё", "е")
        .replace("‑", "-")
    )


def _canon_city_for_weather(raw_city: str) -> tuple[str, str]:
    """
    Return (city_for_weather_skill, city_for_display).

    `weather_skill` currently supports a small built-in catalog, so for common
    Russian city names we map them to the canonical keys used by that skill.
    """
    cleaned = str(raw_city or "").strip()
    if not cleaned:
        return ("", "")
    key = _normalize_city_key(cleaned)
    if key in _CITY_ALIASES:
        return _CITY_ALIASES[key]
    return (cleaned, cleaned)


def _extract_city(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    m = _WEATHER_RE.search(raw)
    if not m:
        lowered = raw.lower()
        city_from_prefix = None
        for prefix in _WEATHER_PREFIXES:
            if lowered.startswith(prefix):
                city_from_prefix = raw[len(prefix) :]
                break
        if city_from_prefix is None:
            return None
        city = city_from_prefix.strip().strip("?.!,;:()[]{}\"'")
    else:
        city = m.group(1).strip().strip("?.!,;:()[]{}\"'")
    if not city:
        return None
    city = re.sub(r"^(город|г\.)\s+", "", city, flags=re.IGNORECASE).strip()
    if not city:
        return None
    return city


def _call_weather_tool(city: str) -> dict:
    ctx = get_ctx()
    weather_dir = Path(ctx.paths.skills_workspace_dir()) / "weather_skill"

    prev = ctx.skill_ctx.get()
    try:
        ctx.skill_ctx.set("weather_skill", weather_dir)
        return execute_tool(
            weather_dir,
            module="handlers.main",
            attr="get_weather",
            payload={"city": city, "silent": True},
        )
    finally:
        if prev is None:
            try:
                ctx.skill_ctx.clear()
            except Exception:
                pass
        else:
            try:
                ctx.skill_ctx.set(prev.name, prev.path)
            except Exception:
                pass


@tool("handle_text")
def handle_text(text: str, _meta: Mapping[str, Any] | None = None, **_: Any) -> Mapping[str, Any]:
    """
    Web voice-chat MVP pipeline:
      text in -> derive weather request -> publish chat reply + TTS request.
    """
    _log.debug("voice_chat_skill.handle_text text=%r meta=%r", text, _meta)
    meta = dict(_meta or {})
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "text_required"}
    text = text.strip()
    webspace_id = _webspace_id_from_meta(meta)
    target_node_id = str(meta.get("target_node_id") or meta.get("node_id") or "").strip() or None
    _append_projected_message(webspace_id, target_node_id, from_="user", text=text)

    local_result = _try_handle_local_command(text, webspace_id=webspace_id, target_node_id=target_node_id, meta=meta)
    if local_result is not None:
        return dict(local_result)

    city_raw = _extract_city(text)
    if not city_raw:
        return {"ok": False, "error": "intent_not_supported"}

    city_for_weather, city_display = _canon_city_for_weather(city_raw)
    if not city_for_weather:
        reply = "Не понял город. Попробуй: «Какая погода в Москве?»"
        _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
        return {"ok": False, "error": "city_required"}

    try:
        result = _call_weather_tool(city_for_weather)
    except Exception as exc:
        reply = f"Ошибка при получении погоды: {exc}"
        _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
        return {"ok": False, "error": str(exc)}

    ok = isinstance(result, dict) and bool(result.get("ok"))
    if not ok:
        err = result.get("error") if isinstance(result, dict) else None
        reply = f"Не удалось получить погоду в {city_display}." + (f" ({err})" if err else "")
        _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
        return {"ok": False, "error": err or "weather_failed"}

    temp = result.get("temp_c") if result.get("temp_c") is not None else result.get("temp")
    desc = result.get("condition") or result.get("description") or ""
    reply = f"Погода в {city_display}: {temp}°C, {desc}".strip().rstrip(",")

    _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
    say(reply, lang=meta.get("lang") or "ru-RU")
    return {"ok": True, "reply": reply, "ts": time.time()}


@tool("get_snapshot")
def get_snapshot(
    _payload: dict[str, Any] | None = None,
    webspace_id: str | None = None,
    target_node_id: str | None = None,
    node_id: str | None = None,
    **_: Any,
) -> Mapping[str, Any]:
    selected_node_id = str(target_node_id or node_id or "").strip() or None
    selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    snapshot = dict(_state_for(selected_webspace, selected_node_id))
    _project_state(selected_webspace, selected_node_id)
    _publish_tail_stream(selected_webspace, selected_node_id, force=True)
    return {
        "voice_chat": snapshot,
        **snapshot,
    }


@subscribe("voice.chat.time_now")
def on_voice_time_now(evt: Any) -> None:
    payload = _payload(evt)
    meta = _meta_from_payload(payload)
    webspace_id = _webspace_id_from_meta({**meta, **payload})
    target_node_id = _target_node_id_from_meta({**meta, **payload})
    reply = _format_now_reply()
    _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
    _speak_reply(reply, meta)


@subscribe("voice.chat.timer_start")
def on_voice_timer_start(evt: Any) -> None:
    payload = _payload(evt)
    meta = _meta_from_payload(payload)
    webspace_id = _webspace_id_from_meta({**meta, **payload})
    target_node_id = _target_node_id_from_meta({**meta, **payload})
    slots = payload.get("slots") if isinstance(payload.get("slots"), Mapping) else {}
    duration = payload.get("duration") or slots.get("duration")
    _start_timer(duration, webspace_id=webspace_id, target_node_id=target_node_id, meta=meta)


@subscribe("desktop.modal.open")
def on_desktop_modal_open(evt: Any) -> None:
    payload = _payload(evt)
    meta = _meta_from_payload(payload)
    if _route_id(meta) != "voice_chat" or bool(meta.get("_voice_chat_ack_suppressed")):
        return
    modal_id = str(payload.get("modal_id") or payload.get("modalId") or "").strip()
    if modal_id != "apps_catalog":
        return
    webspace_id = _webspace_id_from_meta({**meta, **payload})
    target_node_id = _target_node_id_from_meta({**meta, **payload})
    reply = "\u041e\u0442\u043a\u0440\u044b\u0432\u0430\u044e Marketplace."
    _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
    _speak_reply(reply, meta)


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, Mapping):
        return
    if str(payload.get("receiver") or "").strip() != _VOICE_TAIL_RECEIVER:
        return
    webspace_id = str(payload.get("webspace_id") or payload.get("workspace_id") or default_webspace_id()).strip() or default_webspace_id()
    target_node_id = str(payload.get("target_node_id") or payload.get("node_id") or "").strip() or None
    _publish_tail_stream(webspace_id, target_node_id, force=True)


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if isinstance(payload, Mapping) and str(payload.get("action") or "").strip().lower() == "unsubscribed":
        _STREAM_RUNTIME.handle_subscription_changed(evt, receiver_prefix="voice_chat.")
        return
    on_webio_stream_snapshot_requested(evt)

