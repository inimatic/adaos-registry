from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import skill_memory
from adaos.sdk.io import (
    build_capture_command,
    compact_audio_endpoint,
    endpoint_audio_policy,
    endpoint_audio_stt_status,
    process_endpoint_audio_event,
    stream_publish,
)
from adaos.sdk.redevice import ReDeviceBridge, choose_endpoint as sdk_choose_endpoint

try:
    from adaos.services.yjs.webspace import default_webspace_id
except Exception:  # pragma: no cover
    def default_webspace_id() -> str:
        return "default"


_LOG = logging.getLogger("adaos.skill.redevice_voice")
_RECEIVER = "redevice_voice.state"
_STATE_KEY = "redevice_voice.state"
_LAST_EVENT_KEY = "redevice_voice.last_event_id"
_MAX_EVENTS = 12


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _memory_get(key: str, default: Any = None) -> Any:
    try:
        return skill_memory.get(key, default)
    except Exception:
        return default


def _memory_set(key: str, value: Any) -> None:
    try:
        skill_memory.set(key, value)
    except Exception:
        _LOG.debug("failed to write skill memory key=%s", key, exc_info=True)


def _load_state() -> dict[str, Any]:
    raw = _memory_get(_STATE_KEY, {})
    state = dict(raw) if isinstance(raw, Mapping) else {}
    state.setdefault("selected_code", "")
    state.setdefault("events", [])
    state.setdefault("last_command", {})
    state.setdefault("last_segment", {})
    state.setdefault("stt", {"available": False, "state": "not_checked"})
    return state


def _save_state(state: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(state)
    events = data.get("events")
    if isinstance(events, list):
        data["events"] = events[-_MAX_EVENTS:]
    _memory_set(_STATE_KEY, data)
    return data


def _vosk_status(lang: str | None = None) -> dict[str, Any]:
    return endpoint_audio_stt_status(lang)


def _process_endpoint_event(
    state: dict[str, Any],
    endpoint: Mapping[str, Any],
    *,
    webspace_id: str | None = None,
) -> dict[str, Any] | None:
    previous = _text(state.get("last_event_id") or _memory_get(_LAST_EVENT_KEY))
    if previous:
        state["last_event_id"] = previous
    result = process_endpoint_audio_event(state, endpoint, webspace_id=webspace_id, source="redevice_voice")
    current = _text(state.get("last_event_id"))
    if current:
        _memory_set(_LAST_EVENT_KEY, current)
    if isinstance(state.get("events"), list):
        state["events"] = list(state.get("events") or [])[-_MAX_EVENTS:]
    return result


def _policy_report(endpoint: Mapping[str, Any]) -> dict[str, Any]:
    return endpoint_audio_policy(endpoint)


def _compact_endpoint(endpoint: Mapping[str, Any], selected_code: str) -> dict[str, Any]:
    return compact_audio_endpoint(endpoint, selected_code=selected_code)


def _load_endpoints() -> list[dict[str, Any]]:
    return ReDeviceBridge(timeout=12).list_endpoints(sync_registry=True)


def _choose_endpoint(endpoints: list[Mapping[str, Any]], code: str | None = None) -> Mapping[str, Any] | None:
    return sdk_choose_endpoint(endpoints, code)


def _payload(state: Mapping[str, Any], endpoints: list[Mapping[str, Any]]) -> dict[str, Any]:
    selected = _text(state.get("selected_code"))
    items = [_compact_endpoint(item, selected) for item in endpoints]
    return {
        "ok": True,
        "selected_code": selected,
        "count": len(items),
        "items": items,
        "last_command": _mapping(state.get("last_command")),
        "last_segment": _mapping(state.get("last_segment")),
        "stt": _mapping(state.get("stt")) or _vosk_status(),
        "events": list(state.get("events") or [])[-_MAX_EVENTS:],
        "updated_at": _now(),
    }


def _publish(state: Mapping[str, Any], endpoints: list[Mapping[str, Any]], webspace_id: str | None = None) -> None:
    stream_publish(_RECEIVER, _payload(state, endpoints), _meta={"webspace_id": webspace_id or default_webspace_id()})


@tool
def refresh_redevice_voice_state(code: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    state = _load_state()
    if code:
        state["selected_code"] = _text(code)
    endpoints = _load_endpoints()
    selected_endpoint = _choose_endpoint(endpoints, _text(state.get("selected_code")))
    if selected_endpoint is not None:
        state["selected_code"] = _text(selected_endpoint.get("code") or selected_endpoint.get("pair_code"))
    for endpoint in endpoints:
        _process_endpoint_event(state, endpoint, webspace_id=webspace_id)
    state["stt"] = _mapping(state.get("stt")) or _vosk_status()
    state = _save_state(state)
    _publish(state, endpoints, webspace_id)
    return _payload(state, endpoints)


@tool
def select_redevice_voice_endpoint(code: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    state = _load_state()
    state["selected_code"] = _text(code)
    state = _save_state(state)
    endpoints = _load_endpoints()
    _publish(state, endpoints, webspace_id)
    return {"ok": True, "selected_code": state["selected_code"]}


@tool
def start_redevice_voice(
    code: str | None = None,
    lang: str | None = "ru",
    mode: str | None = "vad",
    max_duration_ms: int = 5000,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    endpoints = _load_endpoints()
    endpoint = _choose_endpoint(endpoints, code or _text(state.get("selected_code")))
    if endpoint is None:
        return {"ok": False, "error": "no_redevice_endpoint"}
    pair_code = _text(endpoint.get("code") or endpoint.get("pair_code"))
    state["selected_code"] = pair_code
    command = build_capture_command(
        endpoint,
        code=pair_code,
        mode=mode or "vad",
        lang=lang,
        max_duration_ms=max_duration_ms,
        owner_node_id="member",
        owner_skill_id="redevice_voice",
    )
    command_id = _text(command.get("command_id"))
    session_id = _text(_mapping(command.get("payload")).get("session_id"))
    policy = _mapping(_mapping(command.get("payload")).get("endpoint_policy_check"))
    transport = _mapping(_mapping(command.get("payload")).get("transport"))
    result = ReDeviceBridge(timeout=12).send_command(pair_code, command)
    state["last_command"] = {
        "ok": bool(result.get("ok")),
        "command_id": command_id,
        "code": pair_code,
        "session_id": session_id,
        "mode": _text(mode) or "vad",
        "type": _text(command.get("type")),
        "policy": policy,
        "transport": transport,
        "result": result,
        "updated_at": _now(),
    }
    state = _save_state(state)
    _publish(state, endpoints, webspace_id)
    return {"ok": bool(result.get("ok")), "command_id": command_id, "code": pair_code, "result": result}


def _event_payload(evt: Any) -> Mapping[str, Any]:
    payload = getattr(evt, "payload", evt)
    return payload if isinstance(payload, Mapping) else {}


def _matches_receiver(payload: Mapping[str, Any]) -> bool:
    return _text(payload.get("receiver")) in {_RECEIVER, "redevice_voice.*"}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = _event_payload(evt)
    if not _matches_receiver(payload):
        return
    webspace_id = _text(payload.get("webspace_id") or payload.get("workspace_id")) or default_webspace_id()
    try:
        refresh_redevice_voice_state(webspace_id=webspace_id)
    except Exception:
        _LOG.exception("failed to publish ReDevice voice snapshot")


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = _event_payload(evt)
    if _matches_receiver(payload):
        on_webio_stream_snapshot_requested(evt)
