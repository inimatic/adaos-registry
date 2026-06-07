from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import device_access as sdk_device_access
from adaos.sdk.data import devices as sdk_devices
from adaos.sdk.data import skill_memory_get, skill_memory_set
from adaos.sdk.io import stream_publish
from adaos.services.redevice_versions import endpoint_version_info

try:
    from adaos.services.yjs.webspace import default_webspace_id
except Exception:  # pragma: no cover
    def default_webspace_id() -> str:
        return "default"


_RECEIVER = "redevice_settings.state"
_SELECTED_BY_WS_KEY = "selected_by_webspace"
_ASSIGNMENTS_KEY = "endpoint_assignments"
_LAST_COMMAND_KEY = "last_command"
_ASSIGNMENT_PRESETS = ["assistant", "slideshow", "voice_endpoint", "media_center", "webcam", "idle"]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def _age(value: Any) -> str:
    try:
        ts = float(value or 0)
    except Exception:
        return "-"
    if ts <= 0:
        return "-"
    sec = max(0, int(time.time() - ts))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h {sec % 3600 // 60}m"


def _memory_dict(key: str) -> dict[str, Any]:
    try:
        value = skill_memory_get(key, {})
    except Exception:
        value = {}
    return dict(value) if isinstance(value, Mapping) else {}


def _set_memory_dict(key: str, value: Mapping[str, Any]) -> None:
    try:
        skill_memory_set(key, dict(value))
    except Exception:
        pass


def _webspace_id(value: str | None = None) -> str:
    return _text(value) or default_webspace_id()


def _selected_by_ws() -> dict[str, str]:
    return {str(k): str(v) for k, v in _memory_dict(_SELECTED_BY_WS_KEY).items() if str(k) and str(v)}


def _assignments() -> dict[str, str]:
    return {str(k): str(v) for k, v in _memory_dict(_ASSIGNMENTS_KEY).items() if str(k)}


def _set_selected(webspace_id: str, device_ref: str) -> None:
    state = _selected_by_ws()
    state[_webspace_id(webspace_id)] = _text(device_ref)
    _set_memory_dict(_SELECTED_BY_WS_KEY, state)


def _device_ref_from_item(item: Mapping[str, Any]) -> str:
    ref = _text(item.get("ref"))
    if ref:
        return ref
    identity = _mapping(item.get("identity"))
    endpoint_id = _text(identity.get("endpoint_id") or identity.get("link_id") or item.get("endpoint_id") or item.get("code"))
    return f"redevice:{endpoint_id}" if endpoint_id else ""


def _normalize_device(item: Mapping[str, Any], *, selected_ref: str | None = None) -> dict[str, Any]:
    identity = _mapping(item.get("identity"))
    policy = _mapping(item.get("policy"))
    observation = _mapping(item.get("observation"))
    runtime = _mapping(item.get("runtime"))
    diagnostics = _mapping(item.get("diagnostics"))
    ref = _device_ref_from_item(item)
    endpoint_id = _text(identity.get("endpoint_id") or identity.get("link_id") or item.get("endpoint_id"))
    pair_code = _text(identity.get("pair_code") or item.get("code"))
    effective_name = _text(policy.get("effective_name") or policy.get("display_name") or item.get("display_name") or item.get("title")) or endpoint_id or pair_code or "ReDevice"
    connection_state = _text(observation.get("connection_state") or runtime.get("snapshot_state")) or "unknown"
    active_app = _mapping(runtime.get("active_app"))
    active_surface = _mapping(runtime.get("active_surface"))
    assignment = _assignments().get(ref) or _text(active_app.get("app_id")) or "idle"
    endpoint_health = _mapping(diagnostics.get("endpoint_health"))
    diagnostic_report = _mapping(diagnostics.get("diagnostic_report"))
    service_state = _mapping(diagnostics.get("service_state"))
    battery = _mapping(endpoint_health.get("battery") or diagnostic_report.get("battery"))
    network = _mapping(endpoint_health.get("network") or endpoint_health.get("connectivity") or diagnostic_report.get("network"))
    audio = _mapping(diagnostic_report.get("audio") or service_state.get("audio_output_endpoint"))
    display = _mapping(diagnostic_report.get("screen") or diagnostic_report.get("display") or service_state.get("display_endpoint"))
    bluetooth = _mapping(diagnostic_report.get("bluetooth") or service_state.get("bluetooth_endpoint"))
    location = _mapping(diagnostic_report.get("location") or service_state.get("location_endpoint"))
    selected = bool(ref and ref == _text(selected_ref))
    endpoint_policy = _mapping(diagnostics.get("endpoint_policy"))
    version_info = endpoint_version_info(item)
    software_version = _text(version_info.get("software_version")) or "-"
    served_version = _text(version_info.get("served_version")) or "unknown"
    version_status = _text(version_info.get("version_status")) or "unknown"
    return {
        "id": ref or pair_code or endpoint_id,
        "ref": ref,
        "code": pair_code,
        "endpoint_id": endpoint_id,
        "title": effective_name,
        "selected": selected,
        "selected_label": "selected" if selected else "",
        "online": bool(observation.get("online")),
        "online_state": connection_state,
        "last_seen": _age(observation.get("last_seen_at")),
        "trust_level": _text(policy.get("trust_level")) or _text(endpoint_policy.get("trust_level")) or "limited",
        "assignment": assignment,
        "active_app": _text(active_app.get("label") or active_app.get("app_id")) or "-",
        "active_surface": _text(active_surface.get("surface_ref") or active_surface.get("surface_id")) or "-",
        "software_version": software_version,
        "served_version": served_version,
        "version_status": version_status,
        "version_info": version_info,
        "aliases": ", ".join(str(item or "").strip() for item in list(policy.get("aliases") or policy.get("labels") or []) if str(item or "").strip()),
        "battery": battery or {},
        "network": network or {},
        "audio": audio or {},
        "display": display or {},
        "bluetooth": bluetooth or {},
        "location": location or {},
        "diagnostics": diagnostics,
    }


def _load_devices(selected_ref: str | None = None) -> list[dict[str, Any]]:
    try:
        raw_items = sdk_devices.list_devices(kind="redevice")
        if not raw_items:
            raw_items = sdk_device_access.list_endpoint_devices("redevice", sync_registry=True)
    except Exception:
        raw_items = sdk_device_access.list_endpoint_devices("redevice", sync_registry=True)
    return [_normalize_device(item, selected_ref=selected_ref) for item in raw_items if isinstance(item, Mapping)]


def _first_selected(items: list[dict[str, Any]], webspace_id: str | None = None) -> str:
    ws = _webspace_id(webspace_id)
    selected = _selected_by_ws().get(ws, "")
    refs = {_text(item.get("ref")) for item in items}
    if selected and selected in refs:
        return selected
    for item in items:
        ref = _text(item.get("ref"))
        if ref and bool(item.get("online")):
            _set_selected(ws, ref)
            return ref
    for item in items:
        ref = _text(item.get("ref"))
        if ref:
            _set_selected(ws, ref)
            return ref
    return ""


def _status_cards(selected: Mapping[str, Any] | None, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected_map = dict(selected or {})
    online_count = sum(1 for item in items if item.get("online"))
    return [
        {
            "id": "fleet",
            "title": "Endpoints",
            "value": len(items),
            "subtitle": f"{online_count} online",
        },
        {
            "id": "selected",
            "title": "Selected",
            "value": _text(selected_map.get("title")) or "-",
            "subtitle": _text(selected_map.get("online_state")) or "not selected",
        },
        {
            "id": "assignment",
            "title": "Assignment",
            "value": _text(selected_map.get("assignment")) or "-",
            "subtitle": _text(selected_map.get("active_app")) or "-",
        },
        {
            "id": "version",
            "title": "Agent",
            "value": _text(selected_map.get("software_version")) or "-",
            "subtitle": f"served {_text(selected_map.get('served_version')) or 'unknown'} | {_text(selected_map.get('version_status')) or 'unknown'}",
        },
        {
            "id": "trust",
            "title": "Trust",
            "value": _text(selected_map.get("trust_level")) or "-",
            "subtitle": f"last seen {_text(selected_map.get('last_seen')) or '-'}",
        },
    ]


def _summary(selected: Mapping[str, Any] | None, items: list[dict[str, Any]]) -> dict[str, Any]:
    selected_map = dict(selected or {})
    online_count = sum(1 for item in items if item.get("online"))
    assignment = _text(selected_map.get("assignment")) or "idle"
    last_seen = _text(selected_map.get("last_seen")) or "-"
    code = _text(selected_map.get("code")) or "-"
    trust_level = _text(selected_map.get("trust_level")) or "-"
    version = _text(selected_map.get("software_version")) or "-"
    served_version = _text(selected_map.get("served_version")) or "unknown"
    return {
        "fleet": {
            "value": len(items),
            "label": "ReDevice endpoints",
            "subtitle": f"{online_count} online",
            "description": "Endpoint Registry snapshot",
            "color": "success" if online_count else "warning",
        },
        "selected": {
            "value": _text(selected_map.get("title")) or "No endpoint selected",
            "label": _text(selected_map.get("online_state")) or "not selected",
            "subtitle": f"seen {last_seen} | code {code}",
            "description": f"{assignment} | trust {trust_level} | agent {version}/{served_version}",
            "color": "success" if selected_map.get("online") else "warning" if selected_map else "danger",
        },
        "assignment": {
            "value": assignment.replace("_", " ").title(),
            "label": _text(selected_map.get("active_app")) or "-",
            "subtitle": _text(selected_map.get("active_surface")) or "-",
            "description": "Current endpoint role and active surface",
            "color": "primary" if assignment and assignment != "idle" else "",
        },
    }


def _inspection_groups(selected: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    item = dict(selected or {})
    network = _mapping(item.get("network"))
    bluetooth = _mapping(item.get("bluetooth"))
    audio = _mapping(item.get("audio"))
    display = _mapping(item.get("display"))
    battery = _mapping(item.get("battery"))
    diagnostics = _mapping(item.get("diagnostics"))
    manifest = _mapping(diagnostics.get("endpoint_manifest"))
    policy = _mapping(diagnostics.get("endpoint_policy"))
    software_version = _text(item.get("software_version")) or "-"
    served_version = _text(item.get("served_version")) or "unknown"
    version_status = _text(item.get("version_status")) or "unknown"
    return [
        {
            "id": "connectivity",
            "title": "Connectivity",
            "description": _text(network.get("ssid") or network.get("state")) or _text(item.get("online_state")) or "unknown",
            "subtitle": "Wi-Fi and subnet reachability",
            "icon": "wifi-outline",
        },
        {
            "id": "bluetooth",
            "title": "Bluetooth",
            "description": _text(bluetooth.get("state")) or "unknown",
            "subtitle": "Output pairing and reconnect hints",
            "icon": "bluetooth-outline",
        },
        {
            "id": "io",
            "title": "Audio and display",
            "description": f"audio {_text(audio.get('state') or audio.get('quality')) or 'unknown'} | screen {_text(display.get('readability') or display.get('state')) or 'unknown'}",
            "subtitle": "Speaker, microphone, screen and active surface",
            "icon": "tablet-landscape-outline",
        },
        {
            "id": "power",
            "title": "Power and sensors",
            "description": _text(battery.get("level") or battery.get("state")) or "unknown",
            "subtitle": "Battery, location and degraded role hints",
            "icon": "battery-half-outline",
        },
        {
            "id": "contracts",
            "title": "Contracts",
            "description": f"agent {software_version}/{served_version} | {version_status}",
            "subtitle": "Manifest, policy and diagnostics payload",
            "icon": "document-text-outline",
        },
    ]


def _section_rows(selected: Mapping[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    item = dict(selected or {})
    network = _mapping(item.get("network"))
    bluetooth = _mapping(item.get("bluetooth"))
    audio = _mapping(item.get("audio"))
    display = _mapping(item.get("display"))
    battery = _mapping(item.get("battery"))
    location = _mapping(item.get("location"))
    diagnostics = _mapping(item.get("diagnostics"))
    endpoint_health = _mapping(diagnostics.get("endpoint_health"))
    manifest = _mapping(diagnostics.get("endpoint_manifest"))
    policy = _mapping(diagnostics.get("endpoint_policy"))
    version_info = _mapping(item.get("version_info"))
    return {
        "overview": [
            {"id": "name", "title": "Name", "description": _text(item.get("title")) or "-"},
            {"id": "ref", "title": "Device ref", "description": _text(item.get("ref")) or "-", "subtitle": _text(item.get("endpoint_id")) or "-"},
            {"id": "assignment", "title": "Current assignment", "description": _text(item.get("assignment")) or "idle"},
            {"id": "active", "title": "Active app", "description": _text(item.get("active_app")) or "-", "subtitle": _text(item.get("active_surface")) or "-"},
            {"id": "agent_version", "title": "Agent version", "description": _text(item.get("software_version")) or "-", "subtitle": f"served {_text(item.get('served_version')) or 'unknown'} | {_text(item.get('version_status')) or 'unknown'}"},
        ],
        "network": [
            {"id": "wifi", "title": "Wi-Fi", "description": _text(network.get("ssid") or network.get("state")) or "read-only", "subtitle": "Agent can assist but does not manage physical network."},
            {"id": "connected", "title": "Subnet", "description": _text(item.get("online_state")) or "-", "subtitle": f"last seen {_text(item.get('last_seen')) or '-'}"},
        ],
        "bluetooth": [
            {"id": "available", "title": "Bluetooth", "description": _text(bluetooth.get("state")) or "unknown", "subtitle": "A2DP auto-connect is best-effort unless privileged."},
            {"id": "preferred", "title": "Preferred speaker", "description": _text(bluetooth.get("preferred_output") or bluetooth.get("preferred_device") or "-")},
        ],
        "audio": [
            {"id": "output", "title": "Audio output", "description": _text(audio.get("state") or audio.get("quality")) or "unknown"},
            {"id": "volume", "title": "Volume", "description": _text(audio.get("volume") or "-"), "subtitle": "Volume commands are policy-bound and may be OS-limited."},
        ],
        "display": [
            {"id": "screen", "title": "Display", "description": _text(display.get("readability") or display.get("state")) or "unknown"},
            {"id": "surface", "title": "Surface", "description": _text(item.get("active_surface")) or "-"},
        ],
        "battery": [
            {"id": "level", "title": "Battery", "description": _text(battery.get("level") or battery.get("state") or endpoint_health.get("battery") or "-")},
            {"id": "power", "title": "Power role", "description": _text(battery.get("power_role") or "-"), "subtitle": "Weak batteries should prefer fixed-power roles."},
        ],
        "location": [
            {"id": "permission", "title": "Location", "description": _text(location.get("state") or "not enabled")},
            {"id": "policy", "title": "Policy", "description": "disabled by default", "subtitle": "Location streams require explicit endpoint policy."},
        ],
        "apps": [
            {"id": "settings", "title": "Settings", "description": "service skill", "subtitle": "This modal owns device settings UX."},
            {"id": "slideshow", "title": "Slideshow", "description": "scenario dependency", "subtitle": "Use selected endpoint from slideshow modal."},
            {"id": "voice", "title": "ReDevice Voice", "description": "scenario dependency", "subtitle": "PTT and VAD debug surfaces."},
        ],
        "about": [
            {"id": "version", "title": "Version", "description": _text(item.get("version_status")) or "unknown", "subtitle": f"used {_text(item.get('software_version')) or '-'} | served {_text(item.get('served_version')) or 'unknown'}", "details": version_info},
            {"id": "manifest", "title": "Manifest", "description": _text(manifest.get("schema_version") or "-"), "details": manifest},
            {"id": "policy", "title": "Policy", "description": _text(policy.get("policy_id") or policy.get("id") or "-"), "details": policy},
            {"id": "diagnostics", "title": "Diagnostics", "description": _text(diagnostics.get("policy_source") or "-"), "details": diagnostics},
        ],
        "right_summary": [
            {"id": "name", "title": "Name", "description": _text(item.get("title")) or "-"},
            {"id": "state", "title": "State", "description": _text(item.get("online_state")) or "-", "subtitle": f"seen {_text(item.get('last_seen')) or '-'}"},
            {"id": "role", "title": "Role", "description": _text(item.get("assignment")) or "idle", "subtitle": _text(item.get("active_app")) or "-"},
            {"id": "version", "title": "Agent", "description": _text(item.get("software_version")) or "-", "subtitle": f"served {_text(item.get('served_version')) or 'unknown'}"},
            {"id": "trust", "title": "Trust", "description": _text(item.get("trust_level")) or "-", "subtitle": f"code {_text(item.get('code')) or '-'}"},
        ],
    }


def _build_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    ws = _webspace_id(webspace_id)
    first_items = _load_devices()
    selected_ref = _first_selected(first_items, ws)
    items = _load_devices(selected_ref)
    selected = next((item for item in items if item.get("selected")), None)
    last_command = _memory_dict(_LAST_COMMAND_KEY)
    return {
        "ok": True,
        "selected_ref": selected_ref,
        "selected": selected or {},
        "items": items,
        "count": len(items),
        "summary": _summary(selected, items),
        "status": _status_cards(selected, items),
        "inspection": _inspection_groups(selected),
        "sections": _section_rows(selected),
        "assignment_presets": [{"id": item, "label": item.replace("_", " ").title()} for item in _ASSIGNMENT_PRESETS],
        "last_command": last_command,
        "scenario": {
            "id": "redevice_user_face",
            "required_skills": ["redevice_settings", "slideshow_skill", "redevice_voice"],
            "sdk_boundary": "sdk.data.devices + sdk.data.device_access",
        },
        "updated_at": _now_iso(),
    }


def _publish(webspace_id: str | None = None) -> dict[str, Any]:
    snapshot = _build_snapshot(webspace_id)
    stream_publish(_RECEIVER, snapshot, _meta={"webspace_id": _webspace_id(webspace_id)})
    return snapshot


def _event_payload(evt: Any) -> Mapping[str, Any]:
    payload = getattr(evt, "payload", evt)
    return payload if isinstance(payload, Mapping) else {}


def _matches_receiver(payload: Mapping[str, Any]) -> bool:
    receiver = _text(payload.get("receiver"))
    return receiver in {_RECEIVER, "redevice_settings.*"}


@tool
def refresh_redevice_settings_state(webspace_id: str | None = None) -> dict[str, Any]:
    return _publish(webspace_id)


@tool
def select_redevice_settings_endpoint(device_ref: str | None = None, code: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    token = _text(device_ref)
    if not token and _text(code):
        for item in _load_devices():
            if _text(item.get("code")) == _text(code):
                token = _text(item.get("ref"))
                break
    if not token:
        return {"ok": False, "error": "device_ref_required"}
    _set_selected(_webspace_id(webspace_id), token)
    return _publish(webspace_id)


@tool
def rename_redevice_settings_endpoint(
    device_ref: str | None = None,
    code: str | None = None,
    display_name: str | None = None,
    aliases: str | list[str] | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    selected = _build_snapshot(webspace_id).get("selected") or {}
    resolved_ref = _text(device_ref) or _text(selected.get("ref"))
    resolved_code = _text(code) or _text(selected.get("code"))
    raw_aliases = aliases if isinstance(aliases, list) else str(aliases or "").split(",")
    alias_list: list[str] = []
    seen: set[str] = set()
    for item in raw_aliases:
        alias = _text(item)
        folded = alias.casefold()
        if alias and folded not in seen:
            seen.add(folded)
            alias_list.append(alias)
    result = sdk_device_access.update_endpoint_profile(
        device_ref=resolved_ref,
        code=resolved_code,
        display_name=display_name,
        aliases=alias_list,
    )
    snapshot = _publish(webspace_id)
    return {"ok": bool(result.get("ok")), "result": result, "state": snapshot}


@tool
def set_redevice_assignment(
    assignment: str,
    device_ref: str | None = None,
    code: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    selected = _build_snapshot(webspace_id).get("selected") or {}
    ref = _text(device_ref) or _text(selected.get("ref"))
    if not ref and _text(code):
        for item in _load_devices():
            if _text(item.get("code")) == _text(code):
                ref = _text(item.get("ref"))
                break
    if not ref:
        return {"ok": False, "error": "device_ref_required"}
    normalized = _text(assignment).lower() or "idle"
    if normalized not in _ASSIGNMENT_PRESETS:
        normalized = "idle"
    state = _assignments()
    state[ref] = normalized
    _set_memory_dict(_ASSIGNMENTS_KEY, state)
    return _publish(webspace_id)


@tool
def send_redevice_settings_command(
    action: str,
    device_ref: str | None = None,
    code: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    selected = _build_snapshot(webspace_id).get("selected") or {}
    ref = _text(device_ref) or _text(selected.get("ref"))
    pair_code = _text(code) or _text(selected.get("code"))
    command_type_by_action = {
        "open_wifi": "settings.open_wifi",
        "open_bluetooth": "settings.open_bluetooth",
        "bluetooth_reconnect": "bluetooth.reconnect_output",
        "speaker_test": "audio.test_output",
        "volume_up": "audio.volume_up",
        "volume_down": "audio.volume_down",
        "keep_awake": "display.keep_awake",
        "run_diagnostics": "diagnostics.run",
        "logout": "endpoint.logout",
    }
    token = _text(action).lower()
    command_type = command_type_by_action.get(token)
    if not command_type:
        return {"ok": False, "error": "unknown_action", "action": token}
    command = {
        "command_id": f"cmd:settings:{int(time.time() * 1000)}",
        "type": command_type,
        "payload": {
            "surface_id": f"settings:{token}",
            "surface_ref": "redevice.settings",
            "title": "ReDevice settings",
            "body": f"Requested action: {token}. This action is policy-bound and may require native support on the endpoint.",
            "active_app": {
                "app_id": "redevice_settings",
                "skill_id": "redevice_settings",
                "label": "ReDevice Settings",
                "fullscreen": False,
            },
        },
    }
    result = sdk_device_access.send_endpoint_command(device_ref=ref, code=pair_code, command=command)
    _set_memory_dict(_LAST_COMMAND_KEY, {"action": token, "command": command, "result": result, "updated_at": _now_iso()})
    snapshot = _publish(webspace_id)
    return {"ok": bool(result.get("ok")), "result": result, "state": snapshot}


@tool
def revoke_redevice_settings_endpoint(device_ref: str | None = None, code: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    selected = _build_snapshot(webspace_id).get("selected") or {}
    result = sdk_device_access.revoke_endpoint(device_ref=device_ref or _text(selected.get("ref")), code=code or _text(selected.get("code")))
    snapshot = _publish(webspace_id)
    return {"ok": bool(result.get("ok")), "result": result, "state": snapshot}


@tool
def retire_redevice_settings_endpoint(device_ref: str | None = None, code: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    selected = _build_snapshot(webspace_id).get("selected") or {}
    result = sdk_device_access.retire_endpoint(device_ref=device_ref or _text(selected.get("ref")), code=code or _text(selected.get("code")))
    snapshot = _publish(webspace_id)
    return {"ok": bool(result.get("ok")), "result": result, "state": snapshot}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = _event_payload(evt)
    if not _matches_receiver(payload):
        return
    _publish(_text(payload.get("webspace_id") or payload.get("workspace_id")) or None)


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = _event_payload(evt)
    if _matches_receiver(payload):
        _publish(_text(payload.get("webspace_id") or payload.get("workspace_id")) or None)
