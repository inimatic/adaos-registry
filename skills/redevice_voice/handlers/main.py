from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import skill_memory
from adaos.sdk.data.skill_env import skill_env_path
from adaos.sdk.io import stream_publish
from adaos.sdk.redevice import ReDeviceBridge, compact_endpoint as sdk_compact_endpoint, select_transport
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit

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


def _data_dir() -> Path:
    try:
        env_path = skill_env_path()
        base = env_path.parents[1] if env_path.parent.name == "db" else env_path.parent
        root = base / "internal" / "redevice_voice"
    except Exception:
        root = Path(__file__).resolve().parents[1] / ".skill_state"
    root.mkdir(parents=True, exist_ok=True)
    return root


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


def _event_id(event: Mapping[str, Any]) -> str:
    raw = json.dumps(
        {
            "type": event.get("type"),
            "action": event.get("action"),
            "endpoint_id": event.get("endpoint_id"),
            "session_id": event.get("session_id"),
            "command_id": event.get("command_id"),
            "record_button": event.get("record_button"),
            "audio_bytes": _mapping(event.get("audio")).get("bytes"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


def _target_model(lang: str | None) -> str:
    token = _text(lang).lower()
    if token.startswith("en"):
        return "en-us"
    return "ru-ru"


def _model_dir(target: str) -> Path:
    return Path(get_ctx().paths.base_dir()) / "models" / "vosk" / target


def _vosk_status(lang: str | None = None) -> dict[str, Any]:
    target = _target_model(lang)
    path = _model_dir(target)
    try:
        import vosk  # type: ignore  # noqa: F401
    except Exception as exc:
        return {"available": False, "state": "vosk_unavailable", "target": target, "detail": str(exc)}
    if not path.exists() or not any(path.iterdir()):
        return {"available": False, "state": "model_missing", "target": target, "model_dir": str(path)}
    return {"available": True, "state": "ready", "target": target, "model_dir": str(path)}


def _transcribe_wav(path: Path, *, lang: str | None = None) -> dict[str, Any]:
    status = _vosk_status(lang)
    if not status.get("available"):
        return {"ok": False, **status}
    try:
        import vosk  # type: ignore

        with wave.open(str(path), "rb") as wf:
            if wf.getsampwidth() != 2:
                return {"ok": False, "state": "unsupported_wav", "detail": f"sampwidth={wf.getsampwidth()}"}
            frames = wf.readframes(wf.getnframes())
            rate = wf.getframerate()
        model = vosk.Model(str(status["model_dir"]))
        rec = vosk.KaldiRecognizer(model, rate)
        rec.SetWords(False)
        rec.AcceptWaveform(frames)
        result = json.loads(rec.FinalResult() or "{}")
        text = _text(result.get("text"))
        return {"ok": True, **status, "text": text, "raw": result}
    except Exception as exc:
        return {"ok": False, **status, "state": "transcribe_failed", "detail": str(exc)}


def _dispatch_transcript(text: str, *, webspace_id: str | None, event: Mapping[str, Any]) -> dict[str, Any]:
    token = _text(text)
    if not token:
        return {"ok": False, "state": "empty_transcript"}
    request_id = f"redevice_voice:{_event_id(event)}"
    payload = {
        "text": token,
        "utterance": token,
        "webspace_id": webspace_id or default_webspace_id(),
        "request_id": request_id,
        "_meta": {
            "route_id": "voice_chat",
            "source": "redevice_voice",
            "endpoint_id": event.get("endpoint_id"),
            "session_id": event.get("session_id"),
            "surface_id": event.get("surface_id"),
        },
    }
    try:
        bus_emit(get_ctx().bus, "nlp.intent.detect.rasa", payload, source="redevice_voice")
        return {"ok": True, "state": "dispatched", "request_id": request_id}
    except Exception as exc:
        return {"ok": False, "state": "dispatch_failed", "detail": str(exc), "request_id": request_id}


def _save_audio_segment(event: Mapping[str, Any]) -> dict[str, Any]:
    audio = _mapping(event.get("audio"))
    data = _text(audio.get("data_b64"))
    if not data:
        return {"ok": False, "state": "missing_audio"}
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception as exc:
        return {"ok": False, "state": "invalid_audio_base64", "detail": str(exc)}
    event_token = _event_id(event)
    path = _data_dir() / "audio" / f"{event_token}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {
        "ok": True,
        "event_id": event_token,
        "path": str(path),
        "bytes": len(raw),
        "mime": _text(audio.get("mime")) or "audio/wav",
        "duration_ms": _mapping(event.get("record_button")).get("duration_ms"),
    }


def _process_endpoint_event(
    state: dict[str, Any],
    endpoint: Mapping[str, Any],
    *,
    webspace_id: str | None = None,
) -> dict[str, Any] | None:
    event = _mapping(endpoint.get("last_event"))
    if not event:
        return None
    event_type = _text(event.get("type"))
    if event_type not in {"endpoint.audio.segment", "endpoint.audio.record_button"}:
        return None
    event_token = _event_id(event)
    if _text(_memory_get(_LAST_EVENT_KEY)) == event_token:
        return None
    _memory_set(_LAST_EVENT_KEY, event_token)

    compact_event = {
        "id": event_token,
        "type": event_type,
        "action": _text(event.get("action")),
        "endpoint_id": _text(event.get("endpoint_id")),
        "session_id": _text(event.get("session_id")),
        "duration_ms": _mapping(event.get("record_button")).get("duration_ms"),
        "audio_bytes": _mapping(event.get("audio")).get("bytes"),
        "updated_at": _now(),
    }
    result: dict[str, Any] = {"ok": True, "event": compact_event}
    if event_type == "endpoint.audio.segment":
        segment = _save_audio_segment(event)
        result["segment"] = segment
        state["last_segment"] = segment
        if segment.get("ok"):
            stt = _transcribe_wav(Path(str(segment["path"])), lang=_text(event.get("lang")) or "ru")
            result["stt"] = stt
            state["stt"] = stt
            if stt.get("ok") and _text(stt.get("text")):
                result["dispatch"] = _dispatch_transcript(_text(stt.get("text")), webspace_id=webspace_id, event=event)
    events = list(state.get("events") or [])
    events.append({**compact_event, "result": {k: v for k, v in result.items() if k != "event"}})
    state["events"] = events[-_MAX_EVENTS:]
    return result


def _policy_report(endpoint: Mapping[str, Any]) -> dict[str, Any]:
    manifest = _mapping(endpoint.get("endpoint_manifest"))
    policy = _mapping(endpoint.get("endpoint_policy"))
    services = _mapping(manifest.get("services"))
    audio = _mapping(services.get("audio_input_endpoint"))
    capabilities = _mapping(manifest.get("capabilities"))
    mic = _mapping(capabilities.get("audio.input"))
    enabled = bool(audio.get("enabled") or mic.get("available"))
    trust = _text(policy.get("trust_level") or manifest.get("trust_level")) or "limited"
    return {
        "microphone_allowed": enabled,
        "trust_level": trust,
        "service_enabled": bool(audio.get("enabled")),
        "mic_available": bool(mic.get("available")),
        "capture": "only_while_button_held",
        "local_stt": False,
        "local_tts": False,
    }


def _compact_endpoint(endpoint: Mapping[str, Any], selected_code: str) -> dict[str, Any]:
    compact = sdk_compact_endpoint(endpoint, selected_codes={selected_code} if selected_code else set())
    policy = _policy_report(endpoint)
    return {
        "id": compact.get("id"),
        "code": compact.get("code"),
        "title": compact.get("title"),
        "subtitle": f"{compact.get('online_state')} | mic={policy['microphone_allowed']} | trust={policy['trust_level']}",
        "online_state": compact.get("online_state"),
        "online": compact.get("online"),
        "selected": compact.get("selected"),
        "selected_label": compact.get("selected_label"),
        "last_seen": compact.get("last_seen"),
        "endpoint_id": compact.get("endpoint_id"),
        "policy": policy,
        "transport": select_transport(endpoint, intent="audio.capture.ptt", allow_root_relay=True),
    }


def _load_endpoints() -> list[dict[str, Any]]:
    return ReDeviceBridge(timeout=12).list_endpoints(sync_registry=True)


def _choose_endpoint(endpoints: list[Mapping[str, Any]], code: str | None = None) -> Mapping[str, Any] | None:
    token = _text(code)
    if token:
        for item in endpoints:
            if _text(item.get("code") or item.get("pair_code")) == token:
                return item
    admitted = [
        item for item in endpoints if _text(item.get("state")) in {"approved", "consumed"} and _text(item.get("code") or item.get("pair_code"))
    ]
    if not admitted:
        return None
    admitted.sort(key=lambda item: 0 if sdk_compact_endpoint(item).get("online_state") == "online" else 1)
    return admitted[0]


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
    policy = _policy_report(endpoint)
    command_id = "cmd:voice:" + hashlib.sha256(f"{pair_code}:{time.time()}".encode("utf-8")).hexdigest()[:16]
    session_id = "voice:" + command_id.split(":")[-1]
    transport = select_transport(endpoint, intent="audio.capture.ptt", allow_root_relay=True)
    command = {
        "command_id": command_id,
        "type": "audio.capture.ptt",
        "owner": {"node_id": "member", "skill_id": "redevice_voice"},
        "payload": {
            "surface_id": f"surface:voice:{command_id.split(':')[-1]}",
            "surface_ref": "voice.ptt",
            "session_id": session_id,
            "title": "Voice Endpoint",
            "body": "Hold the button while speaking. Audio is captured only while held.",
            "lang": _target_model(lang),
            "max_duration_ms": max(1000, min(12000, int(max_duration_ms or 5000))),
            "active_app": {"app_id": "redevice_voice", "skill_id": "redevice_voice", "label": "ReDevice Voice"},
            "input_policy": {
                "microphone_required": True,
                "capture": "only_while_button_held",
                "local_stt": False,
                "local_tts": False,
            },
            "endpoint_policy_check": policy,
            "transport": transport,
        },
    }
    result = ReDeviceBridge(timeout=12).send_command(pair_code, command)
    state["last_command"] = {
        "ok": bool(result.get("ok")),
        "command_id": command_id,
        "code": pair_code,
        "session_id": session_id,
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
