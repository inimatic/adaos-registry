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
    create_endpoint_audio_session,
    endpoint_audio_diagnostics,
    endpoint_audio_policy,
    endpoint_audio_readiness,
    endpoint_audio_session,
    endpoint_audio_stt_status,
    process_endpoint_audio_event,
    stream_publish,
    stop_endpoint_audio_session,
    verify_audio_input_content,
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
_LAST_SNAPSHOT_KEY = "redevice_voice.last_stream_snapshot_by_webspace"
_MAX_EVENTS = 12
_POLL_INTERVAL_S = 4.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _int_or_default(value: Any, default: int) -> int:
    token = _text(value)
    if not token or token.startswith("$"):
        return default
    try:
        return int(token)
    except (TypeError, ValueError):
        return default


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
    state.setdefault("vad", {"state": "idle"})
    state.setdefault("record_button", {})
    state.setdefault("retention", {})
    state.setdefault("stt", {"available": False, "state": "not_checked"})
    state.setdefault("audio_check", {"state": "not_checked"})
    state.setdefault("session", {})
    return state


def _save_state(state: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(state)
    events = data.get("events")
    if isinstance(events, list):
        data["events"] = events[-_MAX_EVENTS:]
    _memory_set(_STATE_KEY, data)
    return data


def _last_snapshots() -> dict[str, dict[str, Any]]:
    raw = _memory_get(_LAST_SNAPSHOT_KEY, {})
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)}


def _set_last_snapshot(webspace_id: str, snapshot: Mapping[str, Any]) -> None:
    snapshots = _last_snapshots()
    snapshots[webspace_id or default_webspace_id()] = dict(snapshot)
    _memory_set(_LAST_SNAPSHOT_KEY, snapshots)


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
    selected_endpoint = _choose_endpoint(endpoints, selected)
    diagnostics = endpoint_audio_diagnostics(state, selected_endpoint)
    readiness = endpoint_audio_readiness(state, selected_endpoint)
    session = endpoint_audio_session(state, selected_endpoint)
    return {
        "ok": True,
        "selected_code": selected,
        "count": len(items),
        "items": items,
        "readiness": readiness,
        "diagnostics": diagnostics,
        "session": session,
        "last_command": _mapping(state.get("last_command")),
        "last_segment": _mapping(state.get("last_segment")),
        "stt": _mapping(state.get("stt")) or _vosk_status(),
        "vad": _mapping(state.get("vad")) or _mapping(diagnostics.get("vad")),
        "record_button": _mapping(state.get("record_button")),
        "retention": _mapping(state.get("retention")) or _mapping(diagnostics.get("retention")),
        "events": list(state.get("events") or [])[-_MAX_EVENTS:],
        "audio_check": _mapping(state.get("audio_check")),
        "updated_at": _now(),
    }


def _publish(state: Mapping[str, Any], endpoints: list[Mapping[str, Any]], webspace_id: str | None = None) -> None:
    ws = webspace_id or default_webspace_id()
    payload = _payload(state, endpoints)
    stream_publish(_RECEIVER, payload, _meta={"webspace_id": ws})
    _set_last_snapshot(ws, payload)


def _empty_payload(webspace_id: str | None = None) -> dict[str, Any]:
    state = _load_state()
    return {
        "ok": True,
        "selected_code": _text(state.get("selected_code")),
        "count": 0,
        "items": [],
        "readiness": endpoint_audio_readiness(state, None),
        "diagnostics": endpoint_audio_diagnostics(state, None),
        "session": endpoint_audio_session(state, None),
        "last_command": _mapping(state.get("last_command")),
        "last_segment": _mapping(state.get("last_segment")),
        "stt": _mapping(state.get("stt")) or _vosk_status(),
        "vad": _mapping(state.get("vad")) or {"state": "idle"},
        "record_button": _mapping(state.get("record_button")),
        "retention": _mapping(state.get("retention")),
        "events": list(state.get("events") or [])[-_MAX_EVENTS:],
        "audio_check": _mapping(state.get("audio_check")),
        "stream_state": "cached_empty",
        "updated_at": _now(),
    }


def _publish_cached(webspace_id: str | None = None) -> None:
    ws = webspace_id or default_webspace_id()
    payload = _last_snapshots().get(ws) or _empty_payload(ws)
    stream_publish(_RECEIVER, payload, _meta={"webspace_id": ws})


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
def check_redevice_audio_input(code: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    state = _load_state()
    endpoints = _load_endpoints()
    endpoint = _choose_endpoint(endpoints, code or _text(state.get("selected_code")))
    if endpoint is not None:
        state["selected_code"] = _text(endpoint.get("code") or endpoint.get("pair_code"))
        _process_endpoint_event(state, endpoint, webspace_id=webspace_id)
    check = verify_audio_input_content(state, endpoint)
    state["audio_check"] = check
    state = _save_state(state)
    _publish(state, endpoints, webspace_id)
    return check


@tool
def start_redevice_voice(
    code: str | None = None,
    lang: str | None = "ru",
    mode: str | None = "vad",
    max_duration_ms: int = 5000,
    min_rms: int = 1200,
    silence_ms: int = 900,
    pre_roll_ms: int = 700,
    min_segment_ms: int = 700,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    state = _load_state()
    endpoints = _load_endpoints()
    endpoint = _choose_endpoint(endpoints, code or _text(state.get("selected_code")))
    if endpoint is None:
        return {"ok": False, "error": "no_redevice_endpoint"}
    pair_code = _text(endpoint.get("code") or endpoint.get("pair_code"))
    state["selected_code"] = pair_code
    session = create_endpoint_audio_session(
        state,
        endpoint,
        mode="command",
        owner_node_id="member",
        owner_skill_id="redevice_voice",
        lang=lang,
        response_route={"display_endpoint": True, "audio_output_endpoint": False},
    )
    command = build_capture_command(
        endpoint,
        code=pair_code,
        mode=mode or "vad",
        lang=lang,
        max_duration_ms=max_duration_ms,
        owner_node_id="member",
        owner_skill_id="redevice_voice",
        activation={
            "min_rms": _int_or_default(min_rms, 1200),
            "silence_ms": _int_or_default(silence_ms, 900),
            "pre_roll_ms": _int_or_default(pre_roll_ms, 700),
            "min_segment_ms": _int_or_default(min_segment_ms, 700),
        },
    )
    command_id = _text(command.get("command_id"))
    session_id = _text(session.get("session_id")) or _text(_mapping(command.get("payload")).get("session_id"))
    payload = _mapping(command.get("payload"))
    payload["session_id"] = session_id
    command["payload"] = payload
    policy = _mapping(_mapping(command.get("payload")).get("endpoint_policy_check"))
    transport = _mapping(_mapping(command.get("payload")).get("transport"))
    result = ReDeviceBridge(timeout=12).send_command(pair_code, command)
    if not bool(result.get("ok")):
        session = stop_endpoint_audio_session(state, reason="command_enqueue_failed")
    state["last_command"] = {
        "ok": bool(result.get("ok")),
        "command_id": command_id,
        "code": pair_code,
        "session_id": session_id,
        "mode": _text(mode) or "vad",
        "type": _text(command.get("type")),
        "policy": policy,
        "transport": transport,
        "activation": _mapping(_mapping(_mapping(command.get("payload")).get("input_policy")).get("activation")),
        "session": session,
        "result": result,
        "updated_at": _now(),
    }
    state = _save_state(state)
    _publish(state, endpoints, webspace_id)
    return {"ok": bool(result.get("ok")), "command_id": command_id, "code": pair_code, "session": session, "result": result}


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
        _publish_cached(webspace_id)
    except Exception:
        _LOG.exception("failed to publish ReDevice voice snapshot")


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = _event_payload(evt)
    if _matches_receiver(payload):
        on_webio_stream_snapshot_requested(evt)
